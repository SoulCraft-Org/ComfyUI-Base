# 85-launch.sh — ComfyUI's launch line, in ONE place: the base's restart (80-server.sh) and the pod's boot (boot.sh) both
# run exactly this. Pure functions over COMFY, PY, PORT, HOSTPORT, ARGS_FILE, COMFY_LOG, BASE_PERSIST_ROOT; no side effects
# beyond starting the server. boot.sh sources this file from <volume>/comfy-base/lib/. BASE_LISTEN (2.2.0) is the bind
# address, resolved by base_env_setup or boot_host: 0.0.0.0 on RunPod, whose HTTP proxy needs it; 127.0.0.1 on every
# other host, reached through the driver's tunnel. A caller that sources this file raw gets the pre-2.2.0 default, 0.0.0.0.
# 3.14.0: on a public bind ComfyUI itself listens on 127.0.0.1 (_base_comfy_listen): py/app_proxy.py --front serves its port
# on the machine's other addresses, behind the per-machine key, so a proxy URL never reaches an unlocked ComfyUI.
# 3.0.3: which processes ARE ComfyUI (the matcher below) lives here too, so the boot can find and stop one it did not start,
# and so the boot's own wait can tell a live server from a dead one (before 3.0.3 boot.sh had no matcher at all).

_base_pids_from_table(){ # stdin: pid|ppid|cwd|argv → the pids of ComfyUI servers. A python running main.py from $COMFY
  # (by cwd), or one naming a …/ComfyUI/main.py path (an image's own launch, whatever its cwd). PID 1 is reported, never printed.
  local pid ppid cwd argv script dir
  while IFS='|' read -r pid ppid cwd argv; do
    [ -n "$pid" ] || continue
    set -- $argv; [ $# -ge 2 ] || continue
    case "$1" in *python*) ;; *) continue;; esac
    script="$2"; dir=""
    case "$script" in main.py) ;; */main.py) dir="${script%/main.py}";; *) continue;; esac
    if [ "$cwd" = "$COMFY" ] || _base_run_cwd "$cwd" || { [ -n "$dir" ] && { [ "$dir" = "$COMFY" ] || [ "$(basename "$dir")" = "ComfyUI" ]; }; }; then
      if [ "$pid" = "1" ]; then echo "  ! PID 1 is ComfyUI itself ($argv) — never killed: stopping it would stop the pod" >&2; continue; fi
      echo "$pid"
    fi
  done
  return 0
}
_base_run_cwd(){ # <cwd> → 0 when it is this tree's copy on the machine's disk (3.15.0, lib/84-run-local.sh): <run root>/g…$COMFY
  [ -n "${COMFY:-}" ] || return 1
  case "$1" in "${BASE_RUN_ROOT:-/opt/comfy-run}"/g*"$COMFY") return 0 ;; esac
  return 1
}
_base_comfy_pids(){ # pids of ComfyUI servers on this pod: /proc → the one matcher above
  local pid
  [ -d /proc ] || return 0
  for pid in $(pgrep -f 'main\.py' 2>/dev/null || true); do
    [ -r "/proc/$pid/cmdline" ] || continue
    printf '%s|%s|%s|%s\n' "$pid" "$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo 0)" "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
  done | _base_pids_from_table
  return 0
}
_base_stop_comfy(){ # 3.0.3: stop EVERY ComfyUI server on this machine (TERM, then KILL); 1 when one survives both
  # The house rule is one GPU process per machine: two main.py hold the port and the VRAM between them, and the one that
  # holds the port is not necessarily the newest code.
  local pids i
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"; pids="${pids% }"
  [ -n "$pids" ] || return 0
  echo "  stopping ComfyUI: $pids"; kill $pids 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    sleep 1; if [ -z "$(_base_comfy_pids 2>/dev/null || true)" ]; then return 0; fi
  done
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "$pids" ]; then kill -9 $pids 2>/dev/null || true; sleep 2; fi
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "$pids" ]; then echo "  ComfyUI $pids survived TERM and KILL: not starting a second one beside it"; return 1; fi
  return 0
}
_base_args_extra(){ # the base's args file, verbatim, one word per line (before its first creation: the image's file it will import)
  local line _w f="$ARGS_FILE"
  [ -f "$f" ] || f="${ARGS_IMPORT:-}"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  while IFS= read -r line; do case "$line" in ''|'#'*) continue;; esac; read -r -a _w <<< "$line"; printf '%s\n' ${_w[@]+"${_w[@]}"}; done < "$f"
}
_base_mount_of(){ # <path> → the mount point holding it; a path not made yet is judged by its nearest existing ancestor
  local p="$1"
  while [ -n "$p" ] && [ ! -e "$p" ]; do p="$(dirname "$p")"; done
  df -P "${p:-/}" 2>/dev/null | awk 'NR==2{print $NF}'
}
_base_off_store(){ # <dir> → 0 when the store is a mount of its own and <dir> is on the machine's OS disk instead
  # 3.0.3, MEASURED on a Verda machine on a shared store: the args file still named a library mount the machine no longer
  # had, and ComfyUI made that path on the OS disk and wrote every render and upload there, off the store. On an owned box
  # whose store IS the OS disk there is no second disk to land on by mistake, so nothing is off the store there.
  local os store
  os="$(_base_mount_of /)"; store="$(_base_mount_of "${BASE_HOME:-$COMFY}")"
  [ -n "$os" ] && [ -n "$store" ] && [ "$store" != "$os" ] || return 1
  [ "$(_base_mount_of "$1")" = "$os" ]
}
_base_launch_args(){ # 3.0.3: _base_args_extra minus an --output-directory or --input-directory that is off the store
  # The launch line is the last guard: the install's hygiene cleans the args file, but a boot runs no hygiene, and a
  # library that is not mounted at boot would otherwise send every render to the OS disk. Dropped, ComfyUI falls back
  # to its own output/ and input/ inside the tree, which is on the store. Said on stderr, once per launch.
  local w prev=""
  while IFS= read -r w; do
    if [ -n "$prev" ]; then
      if _base_off_store "$w"; then echo "  !! $prev $w is on this machine's OS disk, not on the store: left off the launch line" >&2
      else printf '%s\n' "$prev" "$w"; fi
      prev=""; continue
    fi
    case "$w" in --output-directory|--input-directory) prev="$w" ;; *) printf '%s\n' "$w" ;; esac
  done < <(_base_args_extra)
  if [ -n "$prev" ]; then printf '%s\n' "$prev"; fi
  return 0
}
_base_preview_flags(){ # the args file is appended LAST and argparse takes the last flag, so never emit a flag the file already carries
  local have out=()
  have="$(_base_args_extra)"
  grep -qx -- '--preview-method' <<< "$have" || out+=(--preview-method auto)
  grep -qx -- '--preview-size'   <<< "$have" || out+=(--preview-size "${BASE_PREVIEW_SIZE:-1024}")
  printf '%s' "${out[*]-}"
}
_base_hf_home(){ # packs that fetch their own weights at first queue (RMBG, Florence2, DepthAnything, SAM, SeedVR2: ~21 GB) use
  # ~/.cache/huggingface, which is container disk on the pod — re-downloaded after every stop. On the volume it persists.
  if [ -n "$BASE_PERSIST_ROOT" ]; then echo "$BASE_PERSIST_ROOT/huggingface"; fi
}
_base_ffmpeg_path(){ # a system ffmpeg WITH NVENC, for the packs that encode video
  # VideoHelperSuite prefers its own bundled imageio_ffmpeg binary, and that build carries NO nvenc encoders at
  # all. A package whose video format asks for h264_nvenc therefore dies at the very last node, after the whole
  # sample has been computed and paid for: "Unknown encoder 'h264_nvenc'". Measured on a GPU 2026-09-19, on a
  # render that had already cleared its policy gate and produced frames.
  # VHS honours VHS_FORCE_FFMPEG_PATH, so the fix is to find a real ffmpeg and name it. Only when it HAS nvenc:
  # forcing a system build without it would trade one silent wrong encoder for another.
  local f; f="$(command -v ffmpeg 2>/dev/null || true)"
  [ -n "$f" ] || return 0
  "$f" -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc || return 0
  echo "$f"
}
_base_local_dirs(){ # 2.5.6: every directory the launch line names on the MACHINE must exist before it runs
  # The args file lives on the SHARED store, so --user-directory and --temp-directory reach every machine, while
  # the directories they name are per machine and were created only by the install that wrote them. A machine
  # that joined the store without installing therefore had the flags and not the folders, and ComfyUI REFUSED to
  # start: "argument --user-directory: The path ... does not exist". Measured on two machines at once, 2.5.6.
  # 3.0.3: only what the launch line will actually carry, so a path off the store is never made on the OS disk
  local d
  for d in $(_base_launch_args 2>/dev/null | awk '/^\//{print}'); do
    case "$d" in /*) mkdir -p "$d" 2>/dev/null || true ;; esac
  done
}
# 3.10.0: the LAUNCH ENVIRONMENT. ComfyUI's process environment is the base's (a package cannot reach the pod's start
# command), so a package that needs a variable set for ComfyUI declares it, and the base starts ComfyUI with it, at every
# restart and every boot. The contract (handbook §4):
#   LAUNCH_ENV=( "KEY=value" ... )   in the package script; the install writes it to state/launch.d/<PKG_ID>.env
# Only the keys below, and CT2_* (CTranslate2's own), may be set: thread counts and allocator tuning, nothing that names
# a path, a library, a credential or a variable the base itself owns (PATH, LD_PRELOAD, PYTHONPATH, HF_HOME, the tokens).
# A value is one word of [A-Za-z0-9_.,:+-]: a number, a flag, an allocator string, never a path or a shell expansion.
# Precedence: a value the machine's own environment already has (the pod's env, the unit's) > the packages' files, in
# name order, the first one naming a key winning (a different value later is named on stderr) > the base's default.
# The base's defaults: OMP_NUM_THREADS from the container's CPU QUOTA (_base_cpu_quota), never the host's count; and 3.28.0,
# CUDNN_ENABLE_DEFAULT_KERNEL_CACHE=0 while the venv's cuDNN is 9.22 to 9.24 (_base_cudnn_cache_bug). A package cannot set it.
BASE_LAUNCH_ENV_KEYS="OMP_NUM_THREADS MKL_NUM_THREADS OPENBLAS_NUM_THREADS NUMEXPR_NUM_THREADS VECLIB_MAXIMUM_THREADS"
BASE_LAUNCH_ENV_KEYS="$BASE_LAUNCH_ENV_KEYS TOKENIZERS_PARALLELISM PYTORCH_CUDA_ALLOC_CONF PYTORCH_ALLOC_CONF CUDA_MODULE_LOADING"
# 3.18.0: and the attention order a node pack reads when it loads (Generate-AI's Render Speed: off, cudnn or fa4), a
# one-word flag like the rest: which exact attention kernel runs first, never a path or a library to load
BASE_LAUNCH_ENV_KEYS="$BASE_LAUNCH_ENV_KEYS RENDER_SPEED_ATTENTION"
# 3.28.0: and the same pack's thread count for every lanczos resize in the process (RENDER_SPEED_LANCZOS_THREADS=N, off when
# unset): several frames resized at once, the same bytes, a number like the thread counts above
BASE_LAUNCH_ENV_KEYS="$BASE_LAUNCH_ENV_KEYS RENDER_SPEED_LANCZOS_THREADS"
_base_launch_env_ok(){ # KEY=value → 0 when it is an entry the contract above allows
  local k v
  case "$1" in *=*) ;; *) return 1 ;; esac
  k="${1%%=*}"; v="${1#*=}"
  case "$v" in ''|*[!A-Za-z0-9_.,:+-]*) return 1 ;; esac
  case "$k" in CT2_*) case "$k" in *[!A-Z0-9_]*) return 1 ;; esac; return 0 ;; esac
  case " $BASE_LAUNCH_ENV_KEYS " in *" $k "*) return 0 ;; esac
  return 1
}
_base_launch_env_rows(){ # prints the package's validated LAUNCH_ENV entries; 1 naming each bad one (a key set twice is bad)
  local kv k keys=" " bad=0
  for kv in ${LAUNCH_ENV[@]+"${LAUNCH_ENV[@]}"}; do
    if ! _base_launch_env_ok "$kv"; then
      echo "  !! LAUNCH_ENV '$kv' is not KEY=value with an allowed key ($BASE_LAUNCH_ENV_KEYS CT2_*) and a one-word value of [A-Za-z0-9_.,:+-]" >&2
      bad=1; continue
    fi
    k="${kv%%=*}"
    case "$keys" in *" $k "*) echo "  !! LAUNCH_ENV sets $k twice" >&2; bad=1; continue ;; esac
    keys="$keys$k "; echo "$kv"
  done
  return $bad
}
_base_cpus_online(){ nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || true; }
_base_cpu_quota(){ # → the whole CPUs this container's cgroup quota allows (rounded up, at most the CPUs online); nothing when unlimited
  # cgroup v2 says `<quota> <period>` in cpu.max ("max 100000" when unlimited); v1 says it in cpu.cfs_quota_us (-1) and
  # cpu.cfs_period_us. A RunPod container is a slice of a big host: nproc and torch both see the HOST's CPUs (every
  # core of a multi-GPU server) while the CFS quota lets the container run only its own (16 vCPU, say), so a pool sized by the host is
  # throttled. BASE_CGROUP_ROOT is the suite's fake /sys/fs/cgroup.
  local root="${BASE_CGROUP_ROOT:-/sys/fs/cgroup}" q="" p="" d n on
  if [ -r "$root/cpu.max" ]; then read -r q p < "$root/cpu.max" || true
  else
    for d in "$root/cpu" "$root/cpu,cpuacct"; do
      if [ -r "$d/cpu.cfs_quota_us" ] && [ -r "$d/cpu.cfs_period_us" ]; then q="$(cat "$d/cpu.cfs_quota_us")"; p="$(cat "$d/cpu.cfs_period_us")"; break; fi
    done
  fi
  case "$q" in ''|*[!0-9]*) return 0 ;; esac                # max, -1, or nothing readable: no quota
  case "$p" in ''|*[!0-9]*) return 0 ;; esac
  [ "$p" -gt 0 ] && [ "$q" -gt 0 ] || return 0
  n=$(( (q + p - 1) / p ))
  on="$(_base_cpus_online)"
  case "$on" in ''|*[!0-9]*) ;; *) if [ "$on" -gt 0 ] && [ "$n" -gt "$on" ]; then n="$on"; fi ;; esac
  echo "$n"
}
_base_launch_env_dir(){ local s="${BASE_STATE:-${BASE_HOME:+$BASE_HOME/state}}"; [ -n "$s" ] && echo "$s/launch.d"; }
_base_cudnn_version(){ # 3.28.0: → the cuDNN the venv's torch loads, from its nvidia-cudnn-cu* wheel's name (9.24.0.37); the newest when
  # there are two; empty when the venv has none (a torch built against a system cuDNN). A directory name: no Python, no torch import.
  local venv="${VENV:-}" d v all=""
  if [ -z "$venv" ] && [ -n "${PY:-}" ]; then case "$PY" in */bin/*) venv="${PY%/bin/*}" ;; esac; fi
  [ -n "$venv" ] || return 0
  for d in "$venv"/lib/python*/site-packages/nvidia_cudnn_cu*-*.dist-info; do
    [ -d "$d" ] || continue
    v="${d##*/}"; v="${v%.dist-info}"; v="${v#*-}"
    case "$v" in [0-9]*) all="$all$v"$'\n' ;; esac
  done
  if [ -n "$all" ]; then printf '%s' "$all" | sort -V | tail -1; fi
  return 0
}
_base_cudnn_cache_bug(){ # 3.28.0: <version> → 0 for cuDNN 9.22.x to 9.24.x. NVIDIA, cuDNN 9.25.1 release notes (Fixed Issues): "A data
  # corruption issue in this cache that could cause a segmentation fault has been fixed. For cuDNN 9.22-9.24, a workaround
  # was to set the environment variable CUDNN_ENABLE_DEFAULT_KERNEL_CACHE=0 to disable the cache." (the default per-process
  # cache of runtime-compiled kernels, added in 9.22.0). torch 2.14.1 pins 9.24.0. Turning the cache off changes no result.
  local maj min rest
  IFS=. read -r maj min rest <<< "${1:-}"
  case "$maj" in ''|*[!0-9]*) return 1 ;; esac
  case "$min" in ''|*[!0-9]*) return 1 ;; esac
  [ "$maj" -eq 9 ] && [ "$min" -ge 22 ] && [ "$min" -le 24 ]
}
_base_launch_env(){ # → the KEY=value lines ComfyUI is started with (precedence above); what was dropped, and why, on stderr
  local dir f kv k v seen="" n s first
  dir="$(_base_launch_env_dir)"
  if [ -n "$dir" ] && [ -d "$dir" ]; then
    for f in "$dir"/*.env; do
      [ -f "$f" ] || continue
      while IFS= read -r kv || [ -n "$kv" ]; do
        case "$kv" in ''|'#'*) continue ;; esac
        if ! _base_launch_env_ok "$kv"; then echo "  ! $(basename "$f"): '$kv' is not an allowed launch variable: left out" >&2; continue; fi
        k="${kv%%=*}"; v="${kv#*=}"
        case "$seen" in
          *"|$k="*)
            first="$(printf '%s' "$seen" | tr '|' '\n' | sed -n "s/^$k=//p" | head -1)"
            if [ "${first%%@*}" != "$v" ]; then echo "  ! $k: ${first#*@} sets ${first%%@*}, $(basename "$f" .env) sets $v: ${first%%@*} is used (the first by name)" >&2; fi
            continue ;;
        esac
        seen="$seen|$k=$v@$(basename "$f" .env)"
      done < "$f"
    done
  fi
  case "$seen" in
    *"|OMP_NUM_THREADS="*) ;;
    *) n="$(_base_cpu_quota)"
       if [ -n "$n" ]; then
         # 3.20.0: N servers share the quota, so each one's pool is its share (at least 1), never the whole of it N times
         s="$(_base_server_count)"
         if [ "$s" -gt 1 ]; then n=$(( n / s )); [ "$n" -ge 1 ] || n=1; seen="$seen|OMP_NUM_THREADS=$n@the CPU quota over $s servers"
         else seen="$seen|OMP_NUM_THREADS=$n@the CPU quota"; fi
       fi ;;
  esac
  v="$(_base_cudnn_version)"                                        # 3.28.0: no package may set it, so only the machine's env wins
  if _base_cudnn_cache_bug "$v"; then seen="$seen|CUDNN_ENABLE_DEFAULT_KERNEL_CACHE=0@cuDNN $v (its default kernel cache; fixed in 9.25.1)"; fi
  printf '%s\n' "$seen" | tr '|' '\n' | while IFS= read -r kv; do         # KEY=value@source, one per line (the last one too)
    [ -n "$kv" ] || continue
    k="${kv%%=*}"; v="${kv#*=}"; v="${v%%@*}"
    if [ -n "${!k+x}" ]; then echo "  ! $k=${!k} is set in this machine's environment: it wins over $k=$v (${kv#*@})" >&2
    else echo "$k=$v"; fi
  done
}
_base_comfy_listen(){ # 3.14.0: where ComfyUI binds: BASE_LISTEN when that is loopback; on any public bind 127.0.0.1, behind the key's front
  case "${BASE_LISTEN:-0.0.0.0}" in 127.*|localhost|::1) echo "$BASE_LISTEN" ;; *) echo 127.0.0.1 ;; esac
}
# 3.20.0: ONE COMFYUI PER GPU. A machine with N GPUs runs N servers, each on its own GPU, so a package can render N parts of
# one job at once (a video's shots) at the speed of one GPU each, and every part's output is what one GPU makes of it.
#   server 0      $PORT (8188), everything as before: the args file, BASE_LISTEN behind the front, the app ports, the links
#   server i>0    127.0.0.1:$((BASE_EXTRA_PORT_BASE + i)) only (18189…): never a host port, never a link; the runner on the
#                 machine reaches it. Not 8188+i: 8189 and up are the packages' app ports (APP_PORTS).
# Every server: CUDA_VISIBLE_DEVICES its own GPU (when N > 1), the same tree, venv, packs, models, launch env and args file,
# the SAME input, output and user folders (a part's input is written once, its output lands where server 0 joins the parts,
# and every server's packs see the same apps), but its OWN temp folder and database: ComfyUI empties its temp folder at
# start (app/assets/lifecycle.py, 0.38) and N processes would migrate one SQLite file at once. Its own log and pid file.
# ComfyUI's environment names them all, the same string in every process, server 0 first:
#   COMFY_BASE_SERVERS=127.0.0.1:8188,127.0.0.1:18189,...    COMFY_BASE_SERVER_INDEX=<i>
# and state/servers.list holds `index|port|gpu` per server. BASE_SERVERS (the machine's env or the pod's) caps N: 1 is the
# single server of every base before 3.20.0. No GPU at all (a Mac, the testbed) is one server, as before.
BASE_EXTRA_PORT_BASE=18188
_base_gpu_ids(){ # → the device ids ComfyUI may use, one per line: the machine's own CUDA_VISIBLE_DEVICES when set, else nvidia-smi's
  local v
  if [ -n "${CUDA_VISIBLE_DEVICES+x}" ]; then
    printf '%s\n' "$CUDA_VISIBLE_DEVICES" | tr ',' '\n' | tr -d ' ' | grep -v -e '^$' -e '^-' || true     # "" or -1: no GPU
    return 0
  fi
  command -v "${BASE_NVIDIA_SMI:-nvidia-smi}" >/dev/null 2>&1 || return 0                                  # the suite's stub
  "${BASE_NVIDIA_SMI:-nvidia-smi}" --query-gpu=index --format=csv,noheader 2>/dev/null | tr -d ' ' | grep -E '^[0-9]+$' || true
}
_base_servers_cap(){ # → BASE_SERVERS from this shell, else from the pod's own environment (an install's ssh shell does not inherit it)
  local v="${BASE_SERVERS:-}"
  if [ -z "$v" ] && declare -F _base_pid1_env >/dev/null; then v="$(_base_pid1_env BASE_SERVERS)"; fi
  if [ -z "$v" ] && declare -F boot_pid1_env >/dev/null; then v="$(boot_pid1_env BASE_SERVERS)"; fi
  echo "$v"
}
_base_server_count(){ # → how many ComfyUI servers this machine runs: one per GPU, at least one, at most BASE_SERVERS
  local n cap
  if [ -n "${_BASE_SERVER_N:-}" ]; then echo "$_BASE_SERVER_N"; return 0; fi     # _base_start_comfy counts once
  n="$(_base_gpu_ids | grep -c . || true)"
  case "$n" in ''|*[!0-9]*|0) n=1 ;; esac
  cap="$(_base_servers_cap)"
  case "$cap" in ''|*[!0-9]*|0) ;; *) if [ "$n" -gt "$cap" ]; then n="$cap"; fi ;; esac
  echo "$n"
}
_base_server_port(){ if [ "${1:-0}" = 0 ]; then echo "$PORT"; else echo $(( BASE_EXTRA_PORT_BASE + $1 )); fi; }
_base_server_gpu(){ _base_gpu_ids | sed -n "$(( ${1:-0} + 1 ))p"; }     # <i> → its device id; nothing without a GPU
_base_servers_csv(){ # → COMFY_BASE_SERVERS: every server's loopback host:port, server 0 first
  local i=1 n out
  n="$(_base_server_count)"; out="127.0.0.1:$(_base_server_port 0)"
  while [ "$i" -lt "$n" ]; do out="$out,127.0.0.1:$(_base_server_port "$i")"; i=$((i + 1)); done
  echo "$out"
}
_base_server_state(){ local s="${BASE_STATE:-${BASE_HOME:+$BASE_HOME/state}}"; echo "${s:-$(dirname "$COMFY_LOG")}"; }
_base_server_log(){ if [ "${1:-0}" = 0 ]; then echo "$COMFY_LOG"; else echo "${COMFY_LOG%.log}-gpu$1.log"; fi; }
_base_server_dir(){ # <i> → server i's own folder (its temp and its database): beside server 0's temp folder, never inside it
  local t
  t="$(_base_launch_args 2>/dev/null | awk 'p{print; exit} $0=="--temp-directory"{p=1}')"
  if [ -n "$t" ]; then echo "${t%/}/gpu$1"; else echo "$COMFY/temp-gpu$1"; fi     # ComfyUI's temp is <--temp-directory>/temp
}
_base_server_args(){ # <i> → the words server i>0 adds after the base's line (argparse takes the last): its own temp and database
  local d; [ "${1:-0}" -gt 0 ] || return 0
  d="$(_base_server_dir "$1")"
  printf '%s\n' --temp-directory "$d"
  # a ComfyUI without --database-url (before 0.3.4x) has no database to share, and would refuse the unknown flag
  if grep -q -- '"--database-url"' "$COMFY/comfy/cli_args.py" 2>/dev/null; then printf '%s\n' --database-url "sqlite:///$d/comfyui.db"; fi
}
_base_servers_write(){ # state/servers.list: `index|port|gpu` per server, replaced whole at every start
  local i=0 n f; n="$(_base_server_count)"; f="$(_base_server_state)/servers.list"
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  { while [ "$i" -lt "$n" ]; do echo "$i|$(_base_server_port "$i")|$(_base_server_gpu "$i")"; i=$((i + 1)); done; } > "$f.tmp" && mv -f "$f.tmp" "$f"
}
_base_start_cmd(){ # the exact launch line, in one place, so the hand-off prints what would actually run (server 0's, 3.20.0)
  local extra pv hf ff le cv=""
  extra="$(_base_launch_args 2>/dev/null | tr '\n' ' ')"; extra="${extra% }"
  pv="$(_base_preview_flags)"; hf="$(_base_hf_home)"; ff="$(_base_ffmpeg_path)"
  le="$(_base_launch_env 2>/dev/null | tr '\n' ' ')"; le="${le% }"     # 3.10.0: the launch environment, after the base's own two
  if [ "$(_base_server_count)" -gt 1 ]; then cv="CUDA_VISIBLE_DEVICES=$(_base_server_gpu 0)"; le="$cv${le:+ $le}"; fi    # 3.20.0
  # `env HF_HOME=…`, not a bare assignment: the line is printed after `nohup`, and nohup would execute the assignment (2.0.11)
  printf '%s%s%s%s main.py --listen %s --port %s --enable-cors-header%s%s' "${hf:+env HF_HOME=$hf }" "${ff:+env VHS_FORCE_FFMPEG_PATH=$ff }" "${le:+env $le }" "$PY" "$(_base_comfy_listen)" "$PORT" "${pv:+ $pv}" "${extra:+ $extra}"
}
_base_start_comfy(){ # every server this machine runs (3.20.0: one per GPU), started together; 0 when server 0 answers
  _base_spawn_comfy || return 1
  _base_wait_spawned
}
_base_spawn_comfy(){ # 3.27.0: the start without the wait, so an install can start ComfyUI beside its model downloads and wait
  # after them (lib/80-server.sh _base_launch_early). What the wait needs is left in BASE_SPAWN_N and BASE_SPAWN_PID.
  local extra=() w pv=() i
  _base_local_dirs
  local _BASE_SERVER_N="" _BASE_WAIT_PID=""                         # read by the calls below (bash's dynamic scope)
  _BASE_SERVER_N="$(_base_server_count)"                            # counted once: every call below reads it
  while IFS= read -r w; do extra+=("$w"); done < <(_base_launch_args)
  echo "  $(_base_start_cmd)"
  read -r -a pv <<< "$(_base_preview_flags)"
  mkdir -p "$(dirname "$COMFY_LOG")" 2>/dev/null || true
  local hf; hf="$(_base_hf_home)"; [ -n "$hf" ] && mkdir -p "$hf" 2>/dev/null || true
  # HF_HOME is EXPORTED in the subshell: an expansion that yields `HF_HOME=/x` in command position is a command name to
  # bash, not an assignment — the pod's restart died on "HF_HOME=/workspace/huggingface: No such file or directory" (2.0.10)
  local ff; ff="$(_base_ffmpeg_path)"; [ -n "$ff" ] && echo "  ffmpeg with nvenc: $ff (VHS_FORCE_FFMPEG_PATH)" || true
  # 3.10.0: computed once, here, so what it dropped is said once; exported in the subshell only, so this shell (and
  # through it a JupyterLab terminal's next install) never mistakes the base's own value for the machine's
  local le kv; le="$(_base_launch_env || true)"; [ -n "$le" ] && echo "  launch env: $(echo $le)" || true
  # 3.15.0: from the copy on the machine's own disk when there is a good one (lib/84-run-local.sh), else from the volume
  RUN_HERE_DIR="$COMFY"; RUN_HERE_PY="$PY"; if declare -F _base_run_here >/dev/null; then _base_run_here; fi
  local servers st; servers="$(_base_servers_csv)"; st="$(_base_server_state)"
  _base_servers_write
  if [ "$_BASE_SERVER_N" -gt 1 ]; then echo "  $_BASE_SERVER_N servers, one per GPU: $servers (state/servers.list)"; fi
  i=0
  while [ "$i" -lt "$_BASE_SERVER_N" ]; do
    _base_spawn_server "$i"
    i=$((i + 1))
  done
  BASE_SPAWN_N="$_BASE_SERVER_N"; BASE_SPAWN_PID=""
  if [ "$_BASE_SERVER_N" -gt 1 ]; then BASE_SPAWN_PID="$(cat "$st/comfyui.pid" 2>/dev/null || true)"; fi
  return 0
}
_base_wait_spawned(){ # 3.27.0: wait for what _base_spawn_comfy started; 0 when server 0 answers
  local rc i
  local _BASE_SERVER_N="${BASE_SPAWN_N:-1}" _BASE_WAIT_PID="${BASE_SPAWN_PID:-}"   # the spawn's view, for the calls below
  # MEASURED on a live pod: the custom nodes alone import for over three minutes
  # (ComfyUI-SeedVR2_VideoUpscaler 67 s, and thirty more packs behind it), so a flat 180 s window
  # declared `restart FAILED`, skipped smoke and combos, and failed the whole install — while the
  # server came up healthy a minute later. Two packages hit it; fixed here.
  #
  # Waiting longer alone would be worse, not better: a server that DIED would then cost ten minutes
  # of silence. So the wait is patient while the process lives and gives up the moment it does not,
  # and it says which of the two happened.
  # 3.20.0: server 0's answer is the result; with N servers its own pid says whether it lives (any main.py would)
  _base_wait_comfy; rc=$?
  i=1
  while [ "$i" -lt "$_BASE_SERVER_N" ]; do      # started together, so by now these are up or nearly; one that fails is named
    _base_wait_server "$i" || echo "  server $i (GPU $(_base_server_gpu "$i"), port $(_base_server_port "$i")) did not answer: $(_base_server_log "$i")"
    i=$((i + 1))
  done
  return $rc
}
_base_spawn_server(){ # <i> → server i started in the background with the launch line's words, plus its own (3.20.0); its pid in state/
  # Called by _base_start_comfy, whose locals (extra, pv, hf, ff, le, servers, st) it reads: bash's dynamic scope.
  local i="$1" port listen log pidf gpu srv=() w
  port="$(_base_server_port "$i")"; log="$(_base_server_log "$i")"; gpu="$(_base_server_gpu "$i")"
  if [ "$i" = 0 ]; then listen="$(_base_comfy_listen)"; pidf="$st/comfyui.pid"
  else listen=127.0.0.1; pidf="$st/comfyui-gpu$i.pid"; mkdir -p "$(_base_server_dir "$i")" 2>/dev/null || true
    while IFS= read -r w; do srv+=("$w"); done < <(_base_server_args "$i")
    echo "  server $i: GPU ${gpu:-?} on 127.0.0.1:$port ${srv[*]-}"
  fi
  (cd "$RUN_HERE_DIR" && { if [ -n "$hf" ]; then export HF_HOME="$hf"; fi
     if [ -n "$ff" ]; then export VHS_FORCE_FFMPEG_PATH="$ff"; fi
     while IFS= read -r kv; do if [ -n "$kv" ]; then export "$kv"; fi; done <<< "$le"
     # 3.1.0: where the base keeps its state, so a package's own route reads state/progress.json without guessing
     # (boot.sh knows BASE_HOME from boot.env; an install knows BASE_STATE)
     _cbs="${BASE_STATE:-${BASE_HOME:+$BASE_HOME/state}}"; if [ -n "$_cbs" ]; then export COMFY_BASE_STATE="$_cbs"; fi
     export COMFY_BASE_SERVERS="$servers" COMFY_BASE_SERVER_INDEX="$i"          # 3.20.0: every server, and which one this is
     if [ "$_BASE_SERVER_N" -gt 1 ] && [ -n "$gpu" ]; then export CUDA_VISIBLE_DEVICES="$gpu"; fi
     unset BASE_LINK_TOKEN                        # 3.14.0: the key is the forwarder's alone; no pack in ComfyUI reads it
     nohup "$RUN_HERE_PY" main.py --listen "$listen" --port "$port" --enable-cors-header ${pv[@]+"${pv[@]}"} ${extra[@]+"${extra[@]}"} ${srv[@]+"${srv[@]}"} >> "$log" 2>&1 &
     if [ "$_BASE_SERVER_N" -gt 1 ]; then echo $! > "$pidf" 2>/dev/null || true; fi; })      # one server needs no pid file
}
_base_wait_server(){ # <i> → 0 when server i>0 answers on its port; patient while its own process lives (3.20.0)
  local i="$1" pid port waited=0 half=0 limit="${BASE_START_WAIT:-600}" tries="${BASE_START_TRIES:-}"
  [ -n "$tries" ] && limit=$((tries * 2))
  port="$(_base_server_port "$i")"; pid="$(cat "$(_base_server_state)/comfyui-gpu$i.pid" 2>/dev/null || true)"
  while [ "$waited" -lt "$limit" ]; do
    if curl -sf --max-time 5 "http://127.0.0.1:$port/system_stats" >/dev/null 2>&1; then echo "  server $i up on 127.0.0.1:$port"; return 0; fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then echo "  server $i: its process is gone after $waited s"; return 1; fi
    sleep 0.5; half=$((half + 1)); waited=$((half / 2))
  done
  return 1
}
_base_wait_comfy(){ # [grace s] → 0 when HOSTPORT answers; patient while a ComfyUI process lives, done the moment none does
  # grace (3.0.3): how long no process at all still means "not started yet" rather than "died". A start through the boot
  # unit runs sshd, JupyterLab and the extensions first, so ComfyUI appears seconds after the unit does, not at once.
  # 3.20.0 _BASE_WAIT_PID: server 0's own pid, when N servers run; then only that process counts as alive
  local grace="${1:-0}" seen=0 tries="${BASE_START_TRIES:-}" waited=0 limit="${BASE_START_WAIT:-600}" alive half=0
  [ -n "$tries" ] && limit=$((tries * 2))            # older callers (and the suite) size it in 2 s tries
  # 3.27.0: asked every half second (every second since 3.26.0, every 2 s before); the process
  # check and the progress lines keep their 2 s rhythm, and `waited` stays whole seconds
  while [ "$waited" -lt "$limit" ]; do
    sleep 0.5; half=$((half + 1)); waited=$((half / 2))
    if curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then echo "  up after $waited$([ $((half % 2)) = 1 ] && echo .5) s"; return 0; fi
    [ $((half % 4)) -eq 0 ] || continue
    if [ -n "${_BASE_WAIT_PID:-}" ]; then alive="$(kill -0 "$_BASE_WAIT_PID" 2>/dev/null && echo 1 || true)"
    else alive="$(_base_comfy_pids 2>/dev/null || true)"; fi
    if [ -n "$alive" ]; then seen=1
    elif [ "$seen" = 1 ] || [ "$waited" -ge "$grace" ]; then
      if [ "$seen" = 1 ] || [ "$grace" = 0 ]; then echo "  the ComfyUI process is gone after $waited s — it did not survive its own start"
      else echo "  no ComfyUI process after $waited s: the boot never started one (see state/logs/boot_*.log)"; fi
      return 1
    fi
    case "$waited" in 60|120|240|360|480) echo "  still importing after $waited s (of $limit)";; esac
  done
  if [ "$seen" = 0 ] && [ -z "$(_base_comfy_pids 2>/dev/null || true)" ]; then echo "  no answer after $limit s, and no ComfyUI process was ever seen"
  else echo "  no answer after $limit s, and the process is still alive — importing, hung, or wedged"; fi
  return 1
}
