# 15-system.sh: 3.0.0: the machine's OS packages and its NVIDIA driver at their newest, on every host, every run.
#
# It is the FIRST stage of a run (after base_init, before the store's install lock: the OS is the MACHINE's, not the
# work's), and it runs before the driver gate so an upgrade can lift a driver that is below DRIVER_MIN.
#
#   OS       apt-get full-upgrade, non-interactive, config files kept, needrestart told to only LIST what it would
#            restart (Ubuntu 24.04 ships it, and it would otherwise restart comfy-base-boot in the middle of an install).
#   driver   NVIDIA's own family: nvidia-open and cuda-toolkit, the newest-tracking metapackages from NVIDIA's CUDA
#            repository, so every later full-upgrade keeps them newest without a branch name in this code. A machine on
#            another family (Verda's images ship Ubuntu's nvidia-driver-580-server-open) switches ONCE, in ONE apt
#            transaction that purges the old family's packages, prebuilt kernel modules included, and installs the new
#            one; a separate full-upgrade first would pull the old family's module back for the new kernel.
#   restart  a new driver cannot load under a running one. The run stops HERE, before anything touches the GPU, red:
#            "restart required: A -> B". The base never reboots a machine itself (Verda forbids an in-guest reboot;
#            hosts/verda/provider.py); podctl prints the restart command, which a person approves.
#   RunPod   the driver belongs to the host: the container's packages are upgraded, libnvidia-* and cuda-compat* held.
#   no root  warn and continue: everything else the base installs still goes to its newest.
#
# BASE_FAKE_APT=<dir> puts the suite's stand-ins (apt-get, apt-cache, apt-mark, dpkg-query, dkms, modinfo, systemctl,
# nvidia-smi, uname, sudo) first on PATH; without it a fake or dry run changes nothing here.

BASE_SYSTEM_RESULT=""
BASE_SYSTEM_ROWS=()
BASE_REBOOT_REQUIRED=""
BASE_DRIVER_BEFORE=""
BASE_DRIVER_AFTER=""
BASE_KERNEL_INFO=""
BASE_NVIDIA_REPO="https://developer.download.nvidia.com/compute/cuda/repos"

_base_can_root(){ # 0 when this run may change the OS: root, passwordless sudo, or the suite's stand-ins
  if [ -n "${BASE_FAKE_APT:-}" ]; then [ "${BASE_FAKE_APT_NOROOT:-0}" != "1" ]; return; fi   # the suite: a CI runner has real sudo
  [ "$(id -u)" = "0" ] && return 0
  command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}
_base_root(){ # <VAR=val ...> <cmd ...>: as root; `env` carries the variables through sudo, which resets the environment
  if [ -n "${BASE_FAKE_APT:-}" ] || [ "$(id -u)" = "0" ]; then env "$@"; else sudo -n env "$@"; fi
}
_base_apt(){ # apt-get, never interactive, never restarting a service behind our back, waiting for another apt's lock
  # 3.24.0: on RunPod dpkg skips its fsync per unpacked file (--force-unsafe-io). What that guards against is a crash in
  # the middle of an unpack leaving the OS broken on disk; a RunPod container's disk is the image's again at every start,
  # so nothing of the sort outlives the crash. A VM keeps its disk, and its dpkg keeps every fsync.
  local io=(); if [ "${BASE_HOST:-}" = runpod ]; then io=(-o Dpkg::Options::=--force-unsafe-io); fi
  _base_root DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
    apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o DPkg::Lock::Timeout=600 ${io[@]+"${io[@]}"} "$@"
}
_base_running_driver(){ # → the driver the running kernel has loaded, "mismatch" when NVML says the libraries moved, "" with none
  command -v nvidia-smi >/dev/null 2>&1 || { echo ""; return 0; }
  local out; out="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1)"
  case "$out" in *ismatch*) echo mismatch;; *[0-9].[0-9]*) echo "$out" | tr -d ' ';; *) echo "";; esac
}
_base_sysroot(){ printf '%s' "${BASE_FAKE_APT:+$BASE_FAKE_APT/root}"; }   # the suite's stand-in for / (empty on a real machine)
_base_newest_kernel(){ ls "$(_base_sysroot)/lib/modules" 2>/dev/null | sort -V | tail -1; }
_base_module_version(){ # <kernel> → the nvidia module version built for that kernel, "" when there is none
  modinfo -k "$1" -F version nvidia 2>/dev/null | head -1
}
_base_has_nvidia_gpu(){
  command -v nvidia-smi >/dev/null 2>&1 && return 0
  command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi 'nvidia' && return 0
  [ -d /proc/driver/nvidia ]
}
_base_nvidia_repo_url(){ # → NVIDIA's CUDA repository for this distro and architecture
  local id ver arch
  id="$(. /etc/os-release 2>/dev/null; echo "${ID:-ubuntu}")"; ver="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-}" | tr -d .)"
  case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in arm64|aarch64) arch=sbsa;; *) arch=x86_64;; esac
  echo "$BASE_NVIDIA_REPO/$id$ver/$arch"
}
_base_ensure_nvidia_repo(){ # NVIDIA's CUDA repository, from its own newest cuda-keyring package; 0 when nvidia-open is installable
  apt-cache policy nvidia-open 2>/dev/null | grep -q 'Candidate: [0-9]' && return 0
  local url deb tmp
  url="$(_base_nvidia_repo_url)"
  deb="$(curl -fsSL --max-time 60 "$url/" 2>/dev/null | grep -oE 'cuda-keyring_[0-9][^"<>]*_all\.deb' | sort -uV | tail -1)"
  [ -n "$deb" ] || { miss "NVIDIA's CUDA repository lists no cuda-keyring at $url"; return 1; }
  _base_tmp; tmp="$BASE_TMPD/$deb"
  curl -fsSL --max-time 120 -o "$tmp" "$url/$deb" || { miss "could not download $url/$deb"; return 1; }
  _base_root dpkg -i "$tmp" >/dev/null 2>&1 || { miss "could not install $deb"; return 1; }
  note "NVIDIA's CUDA repository added ($deb from $url)"
  _base_apt update >/dev/null 2>&1 || true
  apt-cache policy nvidia-open 2>/dev/null | grep -q 'Candidate: [0-9]'
}
_base_other_driver_family(){ # → the installed packages of a driver family that is NOT NVIDIA's nvidia-open, one per line
  dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 2>/dev/null | awk '$1 ~ /^[hi]i/ {print $2}' \
    | grep -E '^(nvidia-driver-[0-9]|nvidia-dkms-[0-9]|nvidia-kernel-common-[0-9]|nvidia-kernel-source-[0-9]|nvidia-utils-[0-9]|nvidia-compute-utils-[0-9]|nvidia-firmware-[0-9]|libnvidia-[a-z0-9-]*-[0-9]{3}|linux-modules-nvidia-|linux-objects-nvidia-|linux-signatures-nvidia-|xserver-xorg-video-nvidia-[0-9])' \
    | grep -vE '^(nvidia-container|libnvidia-container)' || true
}
_base_machine_record(){ # the store's per-machine record: which driver and CUDA each machine on it runs (it coordinates the WORK)
  [ "$BASE_DRY" = "1" ] && return 0
  local d="$BASE_STATE/machines" h cuda
  mkdir -p "$d" 2>/dev/null || return 0
  h="$(hostname -s 2>/dev/null || echo machine)"
  cuda="$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | awk '{print $3}' | head -1)"
  { echo "host=$h"; echo "driver=${BASE_DRIVER_AFTER:-$(_base_running_driver)}"; echo "cuda=${cuda:-}"; echo "kernel=$(uname -r 2>/dev/null)"; echo "ts=$(_base_ts)"; } > "$d/$h.env" 2>/dev/null || true
}

base_system(){ # the stage; sets BASE_REBOOT_REQUIRED and returns 10 when a restart must come before anything uses the GPU
  hdr "SYSTEM · the OS and the NVIDIA driver at their newest"
  BASE_SYSTEM_ROWS=()
  local fake=0
  if [ -n "$BASE_FAKE_ROOT" ] || [ "$BASE_NO_NET" = "1" ] || [ "$BASE_DRY" = "1" ]; then fake=1; fi
  if [ "$BASE_DRY" = "1" ]; then
    would "apt-get update && apt-get full-upgrade (non-interactive, needrestart listing only)"
    would "keep the NVIDIA driver on NVIDIA's newest-tracking nvidia-open + cuda-toolkit, switching families once if another is installed"
    would "stop before any GPU step with 'restart required' if the driver changes"
    BASE_SYSTEM_RESULT="would upgrade"; return 0
  fi
  if [ "$fake" = "1" ] && [ -z "${BASE_FAKE_APT:-}" ]; then note "skipped: a fake run changes no OS (BASE_FAKE_APT stands the suite's apt in)"; BASE_SYSTEM_RESULT="skipped (fake)"; return 0; fi
  if [ -n "${BASE_FAKE_APT:-}" ]; then export PATH="$BASE_FAKE_APT:$PATH"; hash -r; fi
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "not an apt system: the OS and driver are not upgraded here; everything else still goes to its newest"
    BASE_WARN+=("OS and driver not upgraded: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-this OS}") is not apt-based")
    BASE_SYSTEM_RESULT="not upgraded (not apt)"; return 0
  fi
  if ! _base_can_root; then
    warn "no root and no passwordless sudo: the OS and driver are not upgraded here; everything else still goes to its newest"
    BASE_WARN+=("OS and driver not upgraded: no root and no passwordless sudo on this machine")
    BASE_SYSTEM_RESULT="not upgraded (no root)"; return 0
  fi
  local host="${BASE_HOST:-vm}" before_k after_k running newmod purge=() upg held gpu=0 unit_stopped=0 out rc=0
  _base_has_nvidia_gpu && gpu=1
  running="$(_base_running_driver)"; BASE_DRIVER_BEFORE="${running:-none}"; before_k="$(_base_newest_kernel)"
  if ! _base_apt update >/dev/null 2>&1; then miss "apt-get update failed: the OS is not upgraded this run"; BASE_FAILED+=("system: apt-get update failed"); BASE_SYSTEM_RESULT="FAILED (apt update)"; return 0; fi
  if [ "$host" = runpod ]; then
    # the driver is the host's: a container that installs one gets a library that does not match the kernel module
    local h; h="$(dpkg-query -W -f '${Package}\n' 'libnvidia-*' 'cuda-compat*' 'nvidia-*' 2>/dev/null | grep -v '^nvidia-container\|^libnvidia-container' || true)"
    if [ -n "$h" ]; then _base_root apt-mark hold $h >/dev/null 2>&1 || true; note "RunPod: the driver is the host's: $(echo $h | wc -w | tr -d ' ') NVIDIA package(s) held for this upgrade"; fi
  elif [ "$gpu" = "1" ]; then
    # ---- the family: NVIDIA's nvidia-open, switched in ONE transaction, BEFORE the full-upgrade
    if ! dpkg-query -W -f '${db:Status-Abbrev}' nvidia-open 2>/dev/null | grep -q '^ii'; then
      if _base_ensure_nvidia_repo; then
        while IFS= read -r p; do [ -n "$p" ] && purge+=("$p-"); done < <(_base_other_driver_family)
        if systemctl list-unit-files comfy-base-boot.service >/dev/null 2>&1 && systemctl is-active --quiet comfy-base-boot.service 2>/dev/null; then
          note "stopping comfy-base-boot: this machine's renders end here, the new driver cannot load under the running one"
          _base_root systemctl stop comfy-base-boot.service >/dev/null 2>&1 && unit_stopped=1 || true
        fi
        note "switching the NVIDIA driver to NVIDIA's family: nvidia-open + cuda-toolkit${purge[*]:+, purging ${#purge[@]} package(s) of the old family}"
        if out="$(_base_apt install --purge nvidia-open cuda-toolkit ${purge[@]+"${purge[@]}"} 2>&1)"; then
          ok "nvidia-open $(dpkg-query -W -f '${Version}' nvidia-open 2>/dev/null) and cuda-toolkit installed"; BASE_SYSTEM_ROWS+=("driver family → NVIDIA nvidia-open")
        else
          miss "the driver switch failed: $(printf '%s\n' "$out" | grep -E '^E:' | head -2 | tr '\n' ' ')"
          BASE_FAILED+=("system: the switch to nvidia-open failed; the old driver is still installed"); BASE_SYSTEM_RESULT="FAILED (driver switch)"
        fi
      else
        BASE_WARN+=("driver: NVIDIA's CUDA repository is not reachable here; the installed driver stays and is upgraded by apt only")
      fi
    fi
  fi
  # ---- everything, newest
  if out="$(_base_apt full-upgrade 2>&1)"; then
    upg="$(printf '%s\n' "$out" | sed -nE 's/^([0-9]+) upgraded, ([0-9]+) newly installed.*/\1 upgraded, \2 newly installed/p' | tail -1)"
    ok "full-upgrade: ${upg:-done}"; BASE_SYSTEM_ROWS+=("OS: ${upg:-full-upgrade done}")
  else
    miss "full-upgrade failed: $(printf '%s\n' "$out" | grep -E '^E:' | head -2 | tr '\n' ' ')"; BASE_FAILED+=("system: apt-get full-upgrade failed"); rc=1
  fi
  # the base's own packages: present, and newest (full-upgrade moved what was installed; this adds what was not)
  local want=(python3-venv ffmpeg) p missing=()
  [ "$host" = verda ] || [ "${BASE_VOLUME_SHARED:-0}" = "1" ] && want+=(nfs-common)
  command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ] || want+=(openssh-server)
  for p in "${want[@]}"; do dpkg-query -W -f '${db:Status-Abbrev}' "$p" 2>/dev/null | grep -q '^ii' || missing+=("$p"); done
  if [ "${#missing[@]}" -gt 0 ]; then _base_apt install "${missing[@]}" >/dev/null 2>&1 && ok "installed ${missing[*]}" || { miss "could not install ${missing[*]}"; BASE_WARN+=("system: could not install ${missing[*]}"); }; fi
  if [ "$host" = runpod ]; then _base_root apt-mark unhold $(apt-mark showhold 2>/dev/null) >/dev/null 2>&1 || true; fi
  held="$(apt-mark showhold 2>/dev/null | tr '\n' ' ')"
  [ -n "$held" ] && { note "held by apt-mark (apt skipped them): $held"; BASE_WARN+=("system: packages held by apt-mark were not upgraded: $held"); } || true
  # ---- after: the module for the newest kernel, and whether the machine must restart before the GPU is used
  after_k="$(_base_newest_kernel)"; BASE_KERNEL_INFO="${after_k:-?}$( [ "$after_k" != "$(uname -r 2>/dev/null)" ] && echo " (running $(uname -r 2>/dev/null))")"
  if [ "$host" != runpod ] && [ "$gpu" = "1" ]; then
    newmod="$(_base_module_version "$after_k")"; BASE_DRIVER_AFTER="${newmod:-$BASE_DRIVER_BEFORE}"
    if [ -z "$newmod" ]; then
      err "no nvidia module is built for kernel $after_k (dkms: $(dkms status 2>/dev/null | grep -i nvidia | head -1 || echo none)): do NOT restart this machine until it is"
      BASE_FAILED+=("system: the nvidia module is not built for kernel $after_k; a restart would come back without a GPU")
      BASE_SYSTEM_RESULT="FAILED (no module for $after_k)"; _base_machine_record; return 10
    fi
    if [ "$running" = mismatch ] || { [ -n "$running" ] && [ "$newmod" != "$running" ]; }; then
      BASE_REBOOT_REQUIRED="driver ${BASE_DRIVER_BEFORE} -> $newmod${after_k:+ (kernel $after_k)}"
      err "restart required: $BASE_REBOOT_REQUIRED: nothing that uses the GPU runs until the machine restarts"
      [ "$unit_stopped" = "1" ] && note "comfy-base-boot stays stopped; the restart starts it on the new driver"
      BASE_FAILED+=("restart required: $BASE_REBOOT_REQUIRED")
      BASE_SYSTEM_RESULT="restart required ($BASE_REBOOT_REQUIRED)"; BASE_SYSTEM_ROWS+=("driver: $BASE_REBOOT_REQUIRED, restart required")
      _base_machine_record; return 10
    fi
    [ "$unit_stopped" = "1" ] && { _base_root systemctl start comfy-base-boot.service >/dev/null 2>&1 || true; }
    BASE_SYSTEM_ROWS+=("driver: $newmod (newest, loaded)")
  fi
  if [ -f "$(_base_sysroot)/var/run/reboot-required" ] || { [ -n "$after_k" ] && [ "$after_k" != "$(uname -r 2>/dev/null)" ]; }; then
    BASE_WARN+=("system: kernel $after_k is installed, the machine runs $(uname -r 2>/dev/null); restart when convenient")
  fi
  _base_machine_record
  [ -n "$BASE_SYSTEM_RESULT" ] || BASE_SYSTEM_RESULT="ok$( [ "$rc" = 0 ] || echo " (with failures)")"
  return 0
}

# ---------------------------------------------------------------- 3.5.0: the OS stage beside the network checks
# apt (update, full-upgrade) is the slowest check before the venv, and nothing after it until the venv needs what it
# decides: the tokens, the ComfyUI tag and the pack HEADs only ask the network. So it runs in the background and the run
# joins it before the venv snapshot, where the driver gate and a "restart required" stop apply exactly as before. Its
# output is shown whole at the join; everything it decided comes back as plain assignments (a sourced `declare` inside a
# function would make them local, and bash 3.2 has no `declare -g`). Runtime mode, a dry run and BASE_SYSTEM_FOREGROUND=1
# keep it in the foreground.
_BASE_SYSTEM_PID=""; _BASE_SYSTEM_DIR=""
_base_system_start(){ # → 0 when the OS stage is now running in the background, 1 when the caller must run it itself
  if _base_runtime_on || [ "$BASE_DRY" = "1" ] || [ "${BASE_SYSTEM_FOREGROUND:-0}" = "1" ]; then return 1; fi
  _BASE_SYSTEM_DIR="$(_base_mktemp_d)"
  local dir="$_BASE_SYSTEM_DIR" nf="${#BASE_FAILED[@]}" nw="${#BASE_WARN[@]}"
  (
    trap - ERR EXIT; set +e
    t0="$SECONDS"; base_system; rc=$?
    {
      printf 'BASE_SYSTEM_RESULT=%q\n' "${BASE_SYSTEM_RESULT:-}"
      printf 'BASE_DRIVER_BEFORE=%q\n' "${BASE_DRIVER_BEFORE:-}"
      printf 'BASE_DRIVER_AFTER=%q\n' "${BASE_DRIVER_AFTER:-}"
      printf 'BASE_KERNEL_INFO=%q\n' "${BASE_KERNEL_INFO:-}"
      printf 'BASE_NVIDIA_REPO=%q\n' "${BASE_NVIDIA_REPO:-}"
      printf 'BASE_REBOOT_REQUIRED=%q\n' "${BASE_REBOOT_REQUIRED:-}"
      printf 'BASE_SYSTEM_ROWS=('; for x in ${BASE_SYSTEM_ROWS[@]+"${BASE_SYSTEM_ROWS[@]}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_base_sys_failed=('; for x in ${BASE_FAILED[@]+"${BASE_FAILED[@]:$nf}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_base_sys_warn=('; for x in ${BASE_WARN[@]+"${BASE_WARN[@]:$nw}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_base_sys_t0=%q\n_base_sys_secs=%q\n_base_sys_rc=%q\n' "$t0" "$(( SECONDS - t0 ))" "$rc"
    } > "$dir/vars.sh"
  ) > "$dir/out" 2>&1 &
  _BASE_SYSTEM_PID=$!
  note "the OS and the NVIDIA driver are being checked in the background; the run joins them before the venv"
  return 0
}
_base_system_join(){ # → the OS stage's status (10: the machine must restart first); its output shown, its results in this run
  [ -n "$_BASE_SYSTEM_PID" ] || return 0
  local _base_sys_failed=() _base_sys_warn=() _base_sys_t0=0 _base_sys_secs=0 _base_sys_rc=0 x
  wait "$_BASE_SYSTEM_PID" 2>/dev/null || true
  _BASE_SYSTEM_PID=""
  cat "$_BASE_SYSTEM_DIR/out" 2>/dev/null || true
  if [ -f "$_BASE_SYSTEM_DIR/vars.sh" ]; then . "$_BASE_SYSTEM_DIR/vars.sh"
  else BASE_FAILED+=("system: the background OS stage ended without a result (its output is above)"); return 0; fi
  for x in ${_base_sys_failed[@]+"${_base_sys_failed[@]}"}; do BASE_FAILED+=("$x"); done
  for x in ${_base_sys_warn[@]+"${_base_sys_warn[@]}"}; do BASE_WARN+=("$x"); done
  BASE_STAGE_TIMES+=("$_base_sys_secs|SYSTEM (in the background)")
  _base_span "$_base_sys_t0" "$(( _base_sys_t0 + _base_sys_secs ))" "SYSTEM (in the background)"
  return "$_base_sys_rc"
}
