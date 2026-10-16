#!/bin/bash
# boot.sh — the base's own boot on any host. Installed to <volume>/comfy-base/boot.sh; on RunPod podctl points the pod's
# start command at it, on a VM host the base's systemd unit runs it (comfy-base-boot.service, 2.2.0). bash 3.2-clean
# (the Mac runs these functions in the suite). Never `set -e`: a boot that dies takes the machine with it, and the whole
# point of this file is that it always comes up reachable.
#
#     bash boot.sh                          the boot (sshd → JupyterLab → ComfyUI → app ports → sleep infinity); BOOT_HOME=<volume>/comfy-base
#     bash boot.sh --print-sshd-bootstrap   the self-contained sshd snippet podctl puts in a RunPod start command
#     bash boot.sh --apps                   (re)start the app ports only (3.9.0; the install runs it)
#
# BOOT_HOME defaults to this file's own directory (the image's ENV or the unit sets it explicitly). BOOT_ROOT prefixes
# /root and /run for the suite's fake pods; empty on a machine. BASE_LISTEN is 0.0.0.0 on RunPod and 127.0.0.1 elsewhere.

_boot_sshd_bin(){ # sshd on PATH; on a pod (no BOOT_ROOT) also the usual /usr/sbin/sshd, which is off PATH in some images
  command -v sshd 2>/dev/null && return 0
  if [ -z "${BOOT_ROOT:-}" ] && [ -x /usr/sbin/sshd ]; then echo /usr/sbin/sshd; return 0; fi
  return 1
}
boot_sshd(){ # authorized_keys from $PUBLIC_KEY, openssh-server when the image has none, sshd on 22 (keys only). Idempotent.
  # 2.1.0: no key, no sshd. A customer's pod from the public template has no PUBLIC_KEY (the toggle that sets it exists
  # only on RunPod's official templates), and a listener nobody can log in to is a port with no purpose.
  local root="${BOOT_ROOT:-}" sshd key="${PUBLIC_KEY:-}"
  if [ -z "$key" ]; then echo "boot: PUBLIC_KEY is empty — sshd not started (set PUBLIC_KEY on the pod to enable ssh)"; return 0; fi
  mkdir -p "$root/root/.ssh" && chmod 700 "$root/root/.ssh"
  printf '%s\n' "$key" > "$root/root/.ssh/authorized_keys" && chmod 600 "$root/root/.ssh/authorized_keys"
  if ! _boot_sshd_bin >/dev/null; then
    echo "boot: installing openssh-server"
    (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server) >/dev/null 2>&1 || echo "boot: apt could not install openssh-server"
  fi
  mkdir -p "$root/run/sshd"
  ssh-keygen -A >/dev/null 2>&1 || true
  if pgrep -x sshd >/dev/null 2>&1; then echo "boot: sshd already running"; return 0; fi
  sshd="$(_boot_sshd_bin)" || { echo "boot: no sshd binary — the pod is reachable through JupyterLab only"; return 0; }
  if "$sshd" -p 22 -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password; then echo "boot: sshd up on 22"
  else echo "boot: sshd did not start"; fi
  return 0
}

boot_tcmalloc(){ # the allocator the images preload; harmless when absent
  local so; so="$(ldconfig -p 2>/dev/null | grep -o 'libtcmalloc[^ ]*so[^ ]*' | head -1 || true)"
  [ -n "$so" ] && export LD_PRELOAD="$so" || true
}
boot_launch_lib(){ # 85-launch.sh: on the pod beside boot.sh's lib/, in the suite beside boot.sh itself
  local d here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for d in "$BOOT_HOME/lib" "$here" "$here/lib"; do
    if [ -f "$d/85-launch.sh" ]; then . "$d/85-launch.sh"; return 0; fi
  done
  echo "boot: 85-launch.sh not found (looked in $BOOT_HOME/lib and beside boot.sh) — not starting ComfyUI"; return 1
}
boot_host(){ # 2.2.0: which host this is and where it may listen. RunPod's own variable, else the driver's state/host.env, else a VM.
  local f="$BOOT_HOME/state/host.env" h=""
  if [ -f "$f" ]; then h="$(sed -n 's/^BASE_HOST=//p' "$f" | head -1)"; fi
  if [ -n "${RUNPOD_POD_ID:-}" ]; then h=runpod; fi
  BASE_HOST="${BASE_HOST:-${h:-vm}}"
  if [ -z "${BASE_LISTEN:-}" ]; then if [ "$BASE_HOST" = runpod ]; then BASE_LISTEN=0.0.0.0; else BASE_LISTEN=127.0.0.1; fi; fi
  BOOT_VOLUME="$(dirname "$BOOT_HOME")"
  # 2.4.0: the shared library, so the machine's own boot starts ComfyUI on the same output/ and input/ the
  # install pointed it at. The unit RequiresMountsFor it, so by here it is mounted or the server never ran.
  if [ -z "${BASE_LIBRARY:-}" ] && [ -f "$f" ]; then BASE_LIBRARY="$(sed -n 's/^BASE_LIBRARY=//p' "$f" | head -1)"; fi
  BASE_LIBRARY="${BASE_LIBRARY%/}"
  if [ -n "${BASE_LIBRARY:-}" ] && [ ! -d "$BASE_LIBRARY" ]; then
    echo "boot: BASE_LIBRARY=$BASE_LIBRARY is not a directory, starting WITHOUT the shared library"
    BASE_LIBRARY=""
  fi
  export BASE_HOST BASE_LISTEN BASE_LIBRARY
  export DO_NOT_TRACK=1 COMFY_NO_TELEMETRY=1   # comfy-cli ships mixpanel and posthog; rule 9 says nothing is uploaded
  echo "boot: host $BASE_HOST · listen $BASE_LISTEN · volume $BOOT_VOLUME · library ${BASE_LIBRARY:-none}"
}
boot_extensions(){ # 2.2.0: a project's own boot stages, ext/*.sh, sourced in name order. Looked for on the volume first, then beside
  # this very file: on a baked image's FIRST boot the volume is empty and the seed's own ext/ (beside the seed's boot.sh) is the
  # copy that has to run, because copying the seed onto the volume is what such an extension does. The base ships no extension.
  local d f n=0 here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for d in "$BOOT_HOME/ext" "$here/ext"; do
    [ -d "$d" ] || continue
    for f in "$d"/*.sh; do
      [ -f "$f" ] || continue
      echo "boot: extension $f"; . "$f"; n=$((n+1))
    done
    if [ "$n" -gt 0 ]; then break; fi
  done
  [ "$n" -gt 0 ] || echo "boot: no extensions (no ext/*.sh on the volume or beside boot.sh)"
  return 0
}
boot_jupyter(){ # JupyterLab from the base's tools venv; the token rides the environment (never argv); never tokenless
  # 2.1.0: the tokenless start is gone: an unauthenticated JupyterLab on a *.proxy.runpod.net URL is root on the box.
  # 3.9.0: and it is never dark for want of a token either. A pod's own JUPYTER_TOKEN (podctl create sets a fresh one per
  # pod) or JUPYTER_PASSWORD wins; else the one saved in state/tokens.env; else one is GENERATED and saved, on every bind.
  # 2.5.6 generated only on loopback and 2.1.0 refused the public bind, because nobody had anywhere to read a generated
  # token. Now somebody does: podctl status, jupyter and tunnel read it over ssh, and on RunPod's public bind the boot
  # writes ONE line naming it to the CONTAINER log (RunPod's Logs tab, the pod owner's), which is how a pod from the
  # public template, with no ssh key, gets its terminal. Never to the boot log, which is a file on the volume.
  local jl="$BOOT_HOME/tools/bin/jupyter-lab" tok="${JUPYTER_TOKEN:-${JUPYTER_PASSWORD:-}}" made="" own=1
  local listen="${BASE_LISTEN:-127.0.0.1}"
  if [ ! -x "$jl" ]; then echo "boot: no JupyterLab in $BOOT_HOME/tools (the base install builds it) — port 8888 stays dark"; return 0; fi
  # read the saved one FIRST, so this does not depend on boot_tokens having run: a second boot must reuse the
  # token, never append a new line. My own test caught it doing exactly that.
  local tf="$BOOT_HOME/state/tokens.env"
  if [ -n "$tok" ]; then own=0; fi
  if [ -z "$tok" ] && [ -f "$tf" ]; then tok="$(sed -n 's/^JUPYTER_TOKEN=//p' "$tf" | tail -1)"; fi
  if [ -z "$tok" ]; then
    tok="$( (openssl rand -hex 32 2>/dev/null) || (head -c32 /dev/urandom | od -An -tx1 | tr -d " \n") )"
    if [ -z "$tok" ]; then echo "boot: JupyterLab not started — no token could be generated and none was set (never tokenless)"; return 0; fi
    mkdir -p "$BOOT_HOME/state"; ( umask 077; printf 'JUPYTER_TOKEN=%s\n' "$tok" >> "$tf" )
    chmod 600 "$tf" 2>/dev/null || true
    made=" (token generated, saved to state/tokens.env)"
  fi
  export JUPYTER_TOKEN="$tok"
  mkdir -p "$BOOT_HOME/state/logs"
  JUPYTER_TOKEN="$tok" nohup "$jl" --ip "$listen" --port 8888 --allow-root --no-browser --ServerApp.allow_origin='*' --notebook-dir "${BOOT_VOLUME:-$(dirname "$BOOT_HOME")}" >> "$BOOT_HOME/state/logs/jupyter.log" 2>&1 &
  echo "boot: JupyterLab starting on 8888 (the Jupyter Terminal, bound to $listen)$made"
  # The token is NEVER echoed here. This log is a file on the volume, and with a shared store that volume is mounted
  # by every machine and readable by every session on it; the suite has forbidden logging an operator-supplied token
  # since 2.1.0 and a generated one is the same secret. What goes in the log is how to GET it.
  case "$listen" in
    127.0.0.1|localhost|::1|'')
      local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
      echo "boot: to use the terminal and upload files, from YOUR OWN computer run:"
      echo "boot:     podctl jupyter <this machine>     # prints the URL with the token already in it"
      echo "boot:     podctl tunnel <this machine>      # the one tunnel; prints every named link, the terminal's with its token"
      echo "boot:   or by hand:  ssh -L 8888:127.0.0.1:8888 root@${ip:-THE-MACHINE-IP}"
      echo "boot:     then open http://127.0.0.1:8888/lab and paste the token from state/tokens.env (owner-only)"
      ;;
    *)
      echo "boot: the token: podctl status <pod> prints the link with it; on RunPod also the pod's Logs tab (not this file)"
      boot_console_token "$tok" "$own"
      ;;
  esac
  return 0
}
boot_console_token(){ # <token> <own: 1 = generated or saved, 0 = the pod's own env> → one line on the CONTAINER log, RunPod only
  # fd 3 is the start command's own stdout, saved by boot_main before it sends everything else to the boot log. On RunPod
  # that is the pod's Logs tab, which only the pod's owner reads; the boot log is a file on a volume others may mount.
  # A token the pod's env set is the operator's own and is not repeated; one the boot made is the only copy they can see.
  [ "${2:-1}" = 1 ] || return 0
  [ "${BASE_HOST:-}" = runpod ] || return 0
  { true >&3; } 2>/dev/null || return 0
  local url=""
  if [ -n "${RUNPOD_POD_ID:-}" ]; then url="https://${RUNPOD_POD_ID}-8888.proxy.runpod.net/lab?token=$1"; fi
  echo "Jupyter Terminal token: $1${url:+   (open $url)}" >&3
  return 0
}
boot_tokens(){ # 2.1.0: the tokens an earlier install saved (state/tokens.env, 0600) join the pod's own; the Hub browser
  # pack reads its own name. Values are exported, never echoed. 20-comfyui.sh's _base_tokens_export_buttons is the
  # install-time twin; boot.sh sources none of lib/[0-9]* so it carries its own lines.
  local f="$BOOT_HOME/state/tokens.env" name val n=0
  if [ -f "$f" ]; then
    while IFS='=' read -r name val; do
      [[ "$name" =~ ^[A-Z_]+$ ]] || continue
      if [ -z "${!name:-}" ] && [ -n "$val" ]; then export "$name=$val"; n=$((n+1)); fi   # the pod's own value wins over a saved one
    done < "$f"
  fi
  local names=""
  if [ -n "${HF_TOKEN:-}" ]; then export HUGGINGFACE_API_KEY="$HF_TOKEN"; names="HUGGINGFACE_API_KEY"; fi
  echo "boot: tokens — $n loaded from state/tokens.env; exported for the download buttons: ${names:-(none)}"
  return 0
}
boot_comfy(){ # ComfyUI from the base's venv with the base's launch line, only when that venv can boot
  local f="$BOOT_HOME/state/boot.env"
  if [ ! -f "$f" ]; then echo "boot: no $f — the base has not installed ComfyUI on this volume yet; nothing to start"; return 0; fi
  . "$f"
  PY="$VENV/bin/python"; HOSTPORT="127.0.0.1:$PORT"; COMFY_LOG="$BOOT_HOME/state/logs/comfyui.log"
  BASE_PERSIST_ROOT="${PERSIST:-}"; BASE_PREVIEW_SIZE="${PREVIEW_SIZE:-1024}"; ARGS_IMPORT=""
  if [ ! -x "$PY" ]; then echo "boot: $PY is missing — not starting ComfyUI. Fix: bash '<package>-script.sh' rescue"; return 0; fi
  if ! "$PY" -c 'import torch' >/dev/null 2>&1; then echo "boot: $VENV cannot import torch — not starting ComfyUI. Fix: bash '<package>-script.sh' rescue, or re-run a package script"; return 0; fi
  boot_launch_lib || return 0
  if command -v nvidia-smi >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && [ -f "$BOOT_HOME/py/gpu_facts.py" ]; then
    echo "boot: gpu $(python3 "$BOOT_HOME/py/gpu_facts.py" 2>/dev/null || echo 'unreadable')"     # NOT-OURS here = memory held outside this container: stop/start or redeploy (2.0.26)
  fi
  # 3.0.3: this boot has started nothing yet, so any ComfyUI running now is one it does not own: an earlier install's
  # detached start, outside the unit. Left running, it keeps the port and its VRAM and this boot's server is the second
  # GPU process beside it (MEASURED on a Verda machine: two main.py after `systemctl restart comfy-base-boot`).
  if [ -n "$(_base_comfy_pids 2>/dev/null || true)" ]; then
    echo "boot: a ComfyUI this boot did not start is running: stopping it first (one GPU process per machine)"
    if ! _base_stop_comfy; then echo "boot: it would not stop, so no second ComfyUI is started beside it"; return 0; fi
  fi
  echo "boot: starting ComfyUI: $(_base_start_cmd)"
  if _base_start_comfy; then echo "boot: ComfyUI answers on $HOSTPORT"; else echo "boot: ComfyUI did not answer within the wait — see $COMFY_LOG"; fi
  return 0
}
boot_apps_py(){ # the Python the forwarder runs on: the tools venv's, else the machine's python3 (standard library only)
  if [ -x "$BOOT_HOME/tools/bin/python" ]; then echo "$BOOT_HOME/tools/bin/python"; return 0; fi
  command -v python3 2>/dev/null && return 0
  return 1
}
boot_apps_stop(){ # every forwarder THIS volume's boot started on this machine (matched by its own path, so a shared store's
  # other machines, and anything else that happens to be a python, are never touched)
  local pids i
  pids="$(pgrep -f "app_proxy\.py --state $BOOT_HOME/state" 2>/dev/null | tr '\n' ' ' || true)"; pids="${pids% }"
  [ -n "$pids" ] || return 0
  kill $pids 2>/dev/null || true
  for i in 1 2 3 4 5; do
    sleep 1; [ -n "$(pgrep -f "app_proxy\.py --state $BOOT_HOME/state" 2>/dev/null || true)" ] || return 0
  done
  kill -9 $pids 2>/dev/null || true
  return 0
}
boot_apps(){ # 3.9.0: each customer app at the root of its own port (state/apps.d/*.list, one file per package's APP_PORTS)
  # A host whose console links PORTS, not paths (RunPod's Connect tab), can only name an app that has a port of its own;
  # ComfyUI serves the app at /apps/<slug>/ and py/app_proxy.py forwards <port>/ there. Restarted on every call, so an
  # install that adds or drops an app takes effect at once (boot.sh --apps). Not on the GPU: it only moves bytes.
  local py proxy="$BOOT_HOME/py/app_proxy.py" port="${PORT:-}" rows n
  if [ -z "$port" ] && [ -f "$BOOT_HOME/state/boot.env" ]; then port="$(sed -n "s/^PORT='\{0,1\}\([0-9]*\).*/\1/p" "$BOOT_HOME/state/boot.env" | head -1)"; fi
  port="${port:-8188}"
  boot_apps_stop
  if [ ! -f "$proxy" ]; then echo "boot: no $proxy, so no app ports (the base install puts it there)"; return 0; fi
  if ! py="$(boot_apps_py)"; then echo "boot: no python for the app ports: none started"; return 0; fi
  rows="$("$py" "$proxy" --state "$BOOT_HOME/state" --list 2>&1)" || { echo "boot: app ports unreadable: $rows"; return 0; }
  n="$(printf '%s\n' "$rows" | grep -c '^[0-9]' || true)"
  if [ "${n:-0}" = 0 ]; then echo "boot: app ports: none declared (no package ships a customer app)"; return 0; fi
  mkdir -p "$BOOT_HOME/state/logs"
  nohup "$py" "$proxy" --state "$BOOT_HOME/state" --upstream "127.0.0.1:$port" --listen "${BASE_LISTEN:-127.0.0.1}" >> "$BOOT_HOME/state/logs/app_proxy.log" 2>&1 &
  printf '%s\n' "$rows" | while IFS='|' read -r p s nm; do
    case "$p" in [0-9]*) echo "boot: app port $p: $nm (/apps/$s/ on ComfyUI's $port)";; *) echo "boot: $p";; esac
  done
  return 0
}
boot_main(){
  case "${1:-}" in
    --print-sshd-bootstrap) declare -f _boot_sshd_bin; declare -f boot_sshd; echo "boot_sshd"; return 0 ;;
    --apps)                                        # 3.9.0: the install restarts the app ports without a reboot
      BOOT_HOME="${BOOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
      boot_host >/dev/null; boot_apps; return 0 ;;
  esac
  BOOT_HOME="${BOOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"   # the image's ENV or the unit sets it; this file's own directory otherwise
  mkdir -p "$BOOT_HOME/state/logs"
  echo $$ > "$BOOT_HOME/state/boot.pid"        # the pod tier tells a base boot from the image's by this pid's cmdline (2.0.6)
  BOOT_LOG="$BOOT_HOME/state/logs/boot_$(date +%Y%m%d-%H%M%S).log"
  exec 3>&1                                    # 3.9.0: the start command's own stdout (RunPod's Logs tab), for boot_console_token
  exec >> "$BOOT_LOG" 2>&1
  trap 'exit 0' TERM
  echo "== comfy-base boot $(date -u +%Y-%m-%dT%H:%M:%SZ) · $(hostname 2>/dev/null || echo ?) =="
  boot_tcmalloc
  boot_host
  boot_sshd
  boot_jupyter
  boot_tokens
  boot_extensions
  boot_comfy
  boot_apps
  echo "boot: done — sleeping forever so the pod stays up (this script never exits on its own)"
  if [ "${BOOT_ONCE:-0}" = "1" ]; then return 0; fi
  sleep infinity
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then boot_main "$@"; fi
