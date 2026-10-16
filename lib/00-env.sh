# 00-env.sh — flags, output helpers, result registers, portable primitives, prompts, traps, env setup.
# Everything here is shared plumbing; nothing knows about ComfyUI yet.

# ---------------------------------------------------------------- modes and test hooks (read by every stage)
# BASE_FAKE_ROOT=<dir>   discovery uses <dir>/ComfyUI and <dir>/runpod-slim/comfyui_args.txt; state, logs and
#                        staging stay inside <dir>; no /proc scan, no nvidia-smi
# BASE_SEARCH_ROOTS="a b"  overrides the model-index roots (the suite confines the search to a temp tree)
# BASE_NO_NET=1          no HEAD requests, no downloads (sparse files at the declared size), no git network,
#                        a fake venv; import-check / restart / smoke are skipped with a note
# BASE_FAKE_FREE_GB=<n>  overrides the measured free space for the disk gate (fake trees are sparse)
# BASE_FAKE_DL=fail      with BASE_NO_NET: every fake download fails (the suite asserts the non-zero exit)
# BASE_FAKE_DL=real      with BASE_NO_NET: run the REAL download branches (hf / curl stubbed on PATH by the suite)
# BASE_TOKEN_CHECK_URL_HF   where a token is validated (the suite points it at a fake service)
# BASE_INNER=1           set by fake-pod tests: do not run a suite inside a suite run
# BASE_YES=1             unattended: yes to the deletion prompt (failure-path rollback questions default to yes anyway)
# BASE_RESTART=1         actually restart ComfyUI; the default prints the launch line and stops
# BASE_DECLARE_ONLY=1    base_main returns right after validating the declarations (list-packs, tests)
# BASE_FAKE_IMAGE_ROOT=<dir>  the fake container disk (<dir>/ComfyUI is the image's own code, never adopted)
# BASE_FAKE_PID1_ENV=<file>   stands in for /proc/1/environ (tokens, PUBLIC_KEY, the pod's own variables)
# BASE_FAKE_NO_VOLUME=1       the pod has no network volume: discovery must refuse
# BASE_NONINTERACTIVE=1  2.1.0: a run nobody is watching (the boot on a customer pod): every prompt takes its default,
#                        no TTY is assumed even when one is attached; BASE_YES still answers the deletion prompt
# COMFY_REF=<ref>        3.0.0: ComfyUI at a ref NEWER than the newest release (master, a branch, a newer tag, a full 40-hex
#                        commit), for a fix not yet released. Refused when it is an ancestor of the newest release. Per
#                        run: the next run without it returns to the newest release. `podctl install --comfy-ref` sets it.
# BASE_FAKE_APT=<dir>    3.0.0: the suite's stand-ins for apt-get, apt-cache, apt-mark, dpkg-query, dkms, modinfo,
#                        systemctl, nvidia-smi and uname (and <dir>/root for /lib/modules); without it a fake run leaves
#                        the OS alone
# BASE_FAKE_HOLDERS=<file>  3.0.0: the suite's stand-in for py/holders.py (the packages that refuse a forced version)
# RETIRED in 3.0.0 (every run takes everything to its newest; each is noted and ignored when set):
#   BASE_PINNED  COMFY_TAG  BASE_LATEST (--latest is a plain run)  SAGE_WHEEL  a 40-hex SAGE_REF  BASE_FORCE_SELF
# BASE_HOST              2.2.0: runpod | verda | crusoe | local | vm. Explicit, else RunPod's own RUNPOD_POD_ID in PID 1's
#                        environment, else the driver's <volume>/comfy-base/state/host.env, else vm (a VM whose host is unknown)
# BASE_VOLUME            2.2.0: the persistent root (/workspace on RunPod, Verda and Crusoe; a directory of your choosing on
#                        an owned box). Every path the base keeps across a restart derives from it.
# BASE_VOLUME_KIND       2.2.0: mount (the default: the root must be a mountpoint on a pod) | dir (an owned box)
# BASE_LISTEN            2.2.0: the address ComfyUI and JupyterLab bind. 0.0.0.0 on RunPod (its HTTP proxy needs it),
#                        127.0.0.1 everywhere else (Verda has no cloud firewall; the driver's tunnel is the way in)
# BASE_VOLUME_SHARED     2.5.0: 1 when BASE_VOLUME IS the shared store, which is the RunPod shape carried to a VM host:
#                        one network volume holds the base, ComfyUI, the venv, the packages and the models, so the
#                        machine is disposable and the volume is the asset. Read from the driver's state/host.env,
#                        else detected from the root's own filesystem. Two things follow: ComfyUI's user/ and temp/
#                        move to BASE_LOCAL_STATE, because saved workflows and scratch are per machine and two
#                        servers writing one user/ tread on each other; and an install takes a lock, because two
#                        machines writing one venv corrupts it. RUNNING takes no lock: running only reads, which is
#                        what makes several GPUs on one store safe.
# BASE_MCP               2.7.0: 1 (the default) installs Comfy MCP into the run's venv, so an agent can drive this
#                        machine — `podctl mcp <machine>` writes the client configuration. 0 skips it entirely.
#                        comfy-cli's telemetry is disabled either way (lib/45-mcp.sh explains how, and why it has to be).
# BASE_LOCAL_STATE       2.5.0: the per-machine root (default /var/lib/comfy-base-machine) for exactly those things.
# BASE_LIBRARY           2.4.0: the SHARED library's mount point, or empty. One store several machines mount at once
#                        (on Verda an NVMe_Shared volume over NFS, mounted by hosts/verda/startup.sh): the model
#                        library is <it>/models, and ComfyUI's output/ and input/ are <it>/output and <it>/input, so a
#                        LoRA one machine trains and a still one machine renders are visible to every other machine
#                        with no copy. Explicit, else the driver's state/host.env. Unlike BASE_VOLUME it is optional:
#                        with none, every machine keeps its own library on its own volume, as before 2.4.0.
# BASE_NO_SUITE=1        2.1.0: the package's suite does not run at the end of the install (a baked image's first boot:
#                        the suite ran when the image was built; a customer's first boot is not the place for pytest)
# BASE_SEED=1            2.1.0: a baked image's seed build (a brand's image generator): toolchain and packs only; base_install
#                        stops after the ledger (no models, no restart, no smoke, no suite). SAGE_ARCHS="9.0;12.0" builds
#                        SageAttention for those architectures with no GPU present.
# BASE_RUNTIME=<manifest>  3.1.0: a BUYER's machine (Paul, 2026-09-25). The install runs from the proven runtime that
#                        `runtime-apply` unpacked (ComfyUI, the uv Python and venv, the packs, the SageAttention wheel),
#                        whose versions live ONLY in that manifest: no OS or driver upgrade, no git, no pip, no uv
#                        resolve, each skip noted. The driver gate still holds, at the manifest's floor. The one
#                        exception to "everything newest": a newbie's first run never meets an untested upstream change.
# BASE_STAGED=1          3.1.0: act on the package's MODELS_LATER: those rows get 0-byte stand-ins, the server starts
#                        on the first flow's files, and a detached `base.sh fetch-later` brings the rest
#                        (state/later.queue, state/standins.list, state/progress.json). Every other run ignores it.
# BASE_FETCH_JOBS=<n>    3.1.0: how many model files download at once (default 1, which is exactly the 3.0 behaviour).
BASE_DRY="${BASE_DRY:-0}"                 # --check
BASE_FAKE_ROOT="${BASE_FAKE_ROOT:-}"
BASE_FAKE_IMAGE_ROOT="${BASE_FAKE_IMAGE_ROOT:-}"   # fake: the container disk — a code tree the base must never adopt or scan
BASE_FAKE_PID1_ENV="${BASE_FAKE_PID1_ENV:-}"       # fake: the file that stands in for /proc/1/environ
BASE_FAKE_NO_VOLUME="${BASE_FAKE_NO_VOLUME:-0}"    # fake: /workspace is not a mounted network volume
BASE_LIBRARY="${BASE_LIBRARY:-}"                   # 2.4.0: the shared library's mount point (empty: no shared library)
BASE_VOLUME_SHARED="${BASE_VOLUME_SHARED:-0}"      # 2.5.0: 1 when BASE_VOLUME itself is network storage SEVERAL machines mount
BASE_LOCAL_STATE="${BASE_LOCAL_STATE:-/var/lib/comfy-base-machine}"   # 2.5.0: the per-machine root, never on the shared volume
_base_user_dir(){ # the user/ the SERVER will actually read, which is NOT always ComfyUI's own
  # 2.5.6. 2.5.0 moved the server's --user-directory to BASE_LOCAL_STATE (lib/70-hygiene.sh) because two
  # machines cannot share one user/, and left every WRITER pointing at $COMFY/user. So the installer put the
  # workflow in ComfyUI's tree, the server read the machine's, and GET /api/userdata?dir=workflows answered []:
  # no package could be opened from the server's own list, which is exactly what an App Mode pass requires
  # (root CLAUDE.md §1 phase 3). Measured on three machines 2026-09-18. One accessor, so the halves cannot
  # disagree again.
  if [ "${BASE_VOLUME_SHARED:-0}" = "1" ]; then printf '%s\n' "$BASE_LOCAL_STATE/user"
  else printf '%s\n' "$COMFY/user"; fi
}
_base_workflows_dir(){ printf '%s\n' "$(_base_user_dir)/default/workflows"; }
BASE_LOCK_WAIT="${BASE_LOCK_WAIT:-1800}"           # 2.5.0: seconds to wait for another machine's install lock (0: do not wait)
BASE_LOCK_STALE="${BASE_LOCK_STALE:-7200}"         # 2.5.0: an install lock older than this was left by a dead run
BASE_NO_NET="${BASE_NO_NET:-0}"
BASE_YES="${BASE_YES:-0}"
BASE_RESTART="${BASE_RESTART:-0}"
BASE_NONINTERACTIVE="${BASE_NONINTERACTIVE:-0}"
export BASE_NONINTERACTIVE              # a package script the boot runs inherits the mode it was started in
BASE_HOST="${BASE_HOST:-}"; BASE_VOLUME="${BASE_VOLUME:-/workspace}"; BASE_VOLUME="${BASE_VOLUME%/}"; [ -n "$BASE_VOLUME" ] || BASE_VOLUME=/
BASE_VOLUME_KIND="${BASE_VOLUME_KIND:-}"; BASE_LISTEN="${BASE_LISTEN:-}"   # resolved by _base_host_resolve once every lib is loaded
# The ONE filesystem a pod stop/start keeps. Empty off the pod, which makes every persistence check
# vacuous — correct, because $HOME on a workstation is durable and only RunPod restores the container
# disk from the image on every start. BASE_PERSIST_ROOT_FORCE lets the suite drive the predicate.
BASE_PERSIST_ROOT="${BASE_PERSIST_ROOT_FORCE:-}"
BASE_HOME=""; BASE_STATE=""              # set by base_env_setup
BASE_TTY=0; [ -t 0 ] && [ -t 1 ] && BASE_TTY=1
[ "$BASE_NONINTERACTIVE" = "1" ] && BASE_TTY=0   # 2.1.0: a watched terminal is still not a person who can answer
BASE_LOG=""; BASE_LOGGING=0; BASE_SUMMARY_DONE=0
BASE_TMPDIRS=(); BASE_TMPD=""
SYS_PY="${SYS_PY:-python3}"              # the pod's system python; only ever used for tools, never for pip

# ---------------------------------------------------------------- output helpers
if [ "$BASE_TTY" = "1" ] && [ -z "${NO_COLOR:-}" ]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; YEL='\033[1;33m'; CYA='\033[0;36m'; NC='\033[0m'
else
  GREEN=''; RED=''; YEL=''; CYA=''; NC=''
fi
ok(){    echo -e "${GREEN}  ✔ $*${NC}"; }
miss(){  echo -e "${YEL}  ✖ $*${NC}"; }
err(){   echo -e "${RED}  ‼ $*${NC}"; }
note(){  echo "  ○ $*"; }
hdr(){   _base_stage_mark "$*"; echo -e "\n${CYA}══ $* ══${NC}"; }
# 3.2.0: every header closes the stage before it with its seconds (bash's own SECONDS: no fork, bash 3.2 too).
# The summary prints them slowest first and the store keeps them, so where an install spends its minutes is read
# from the run itself, never estimated.
BASE_STAGE_NAME=""; BASE_STAGE_T0=0; BASE_STAGE_TIMES=()
_base_stage_mark(){
  local now="$SECONDS"
  if [ -n "$BASE_STAGE_NAME" ]; then BASE_STAGE_TIMES+=("$(( now - BASE_STAGE_T0 ))|$BASE_STAGE_NAME"); fi
  BASE_STAGE_NAME="$1"; BASE_STAGE_T0="$now"
}
would(){ echo -e "${CYA}  → would $*${NC}"; }   # --check wording
todo(){  echo -e "${CYA}  → $*${NC}"; }          # queued work, not a fault: this run is about to do it
warn(){  echo -e "${YEL}  ! $*${NC}"; BASE_WARN+=("$*"); }   # reported in the summary, never blocks

# ---------------------------------------------------------------- result registers (read by base_summary)
BASE_FAILED=()    # anything here → exit 1 at the summary
BASE_WARN=()      # reported, never blocks
BASE_CHANGED=()   # every line base_hygiene changed
BASE_STASH_CMD=""
COMFY_OLD="?"; COMFY_NEW="?"
PACK_PRESENT=(); PACK_UPDATED=(); PACK_CLONED=(); PACK_DIRTY=(); PACK_DIRS=()
MODEL_OK=(); MODEL_MOVED=(); MODEL_DL=(); MODEL_PARTIAL=(); MODEL_FAIL=(); MODEL_LOCAL_MISSING=()
MODEL_STANDIN=(); LATER_PENDING=""; BASE_RUNTIME_NOTE=""
MODEL_OK_GB=0; MODEL_MOVED_GB=0; MODEL_DL_GB=0; MODEL_FAIL_GB=0
DEL_FILES=(); DEL_BYTES=0; UNKNOWN_FILES=(); UNCLAIMED_FILES=()
BASE_TEST_RESULT="not run"; BASE_TEST_RC=0; BASE_SMOKE_RESULT="not run"; BASE_IMPORT_RESULT="not run"
BASE_COMBO_RESULT="not run"; BASE_SYNC_RESULT="not run"; BASE_RESTART_RESULT="not run"
BASE_BOOT_RESULT="not run"; BASE_TOOLS_RESULT="not run"; BASE_MCP_RESULT="not run"
BASE_VENV_RESULT="not run"; BASE_TORCH_INFO="?"; BASE_VENV_BACKUP=""
BASE_BLOCK_RESTART=0; BASE_RESTART_NEEDED=0; BASE_RESTART_WHY=()
# A package's own reasons to restart. base_restart() REBUILDS BASE_RESTART_WHY from the
# base's own signals, so a hook appending to that array had its reason silently discarded —
# a package that installs its own node ended up with the file on disk, unregistered, and a
# GREEN install report. Hooks append here instead, and base_restart folds it in.
PKG_RESTART_WHY=()

# ---------------------------------------------------------------- portable primitives (GNU on the pod, BSD on the Mac that runs the suite)
_base_uname(){ echo "${BASE_FAKE_UNAME:-$(uname -s)}"; }
_base_gpu_node(){ # the first NVIDIA device node: usually node 0, but node 3 on the pod of 2026-09-05 (2.0.4)
  local d="${BASE_DEV_DIR:-/dev}" n
  for n in "$d"/nvidia[0-9]*; do if [ -e "$n" ]; then echo "$n"; return 0; fi; done
  return 1
}
_base_on_pod(){ [ "$(_base_uname)" = Linux ] && [ -n "$(_base_gpu_node)" ]; }
_base_xargs0(){ # `xargs -0` that runs NOTHING on empty input everywhere: GNU xargs needs -r for that, BSD's never runs on empty input
  if [ -z "${BASE_XARGS_R+x}" ]; then if xargs -r </dev/null true >/dev/null 2>&1; then BASE_XARGS_R=-r; else BASE_XARGS_R=""; fi; fi
  xargs -0 ${BASE_XARGS_R:+"$BASE_XARGS_R"} "$@"
}
_base_stat_flavour(){ if stat -c %s / >/dev/null 2>&1; then echo gnu; else echo bsd; fi; }
BASE_STAT="$(_base_stat_flavour)"
_base_fsize(){ if [ "$BASE_STAT" = gnu ]; then stat -c %s "$1"; else stat -f %z "$1"; fi; }
_base_fdev(){  if [ "$BASE_STAT" = gnu ]; then stat -c %d "$1"; else stat -f %d "$1"; fi; }
# the 0 fallback sits INSIDE the awk: awk exits 0 on no output, so `|| echo 0` would never fire
_base_free_kb(){ df -Pk "$1" 2>/dev/null | awk 'NR==2{print $4+0; f=1} END{if(!f) print 0}'; }
_base_lower(){ printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
_base_ts(){ date +%Y%m%d-%H%M%S; }
_base_gb_to_bytes(){ awk -v g="$1" 'BEGIN{printf "%.0f", g*1000000000}'; }   # decimal GB, as the Hub shows them
_base_bytes_to_gb(){ awk -v b="$1" 'BEGIN{printf "%.2f", b/1000000000}'; }
_base_tmp(){ if [ -z "$BASE_TMPD" ]; then BASE_TMPD="$(mktemp -d)"; BASE_TMPDIRS+=("$BASE_TMPD"); fi; }
_base_mktemp_d(){ local d; d="$(mktemp -d)"; BASE_TMPDIRS+=("$d"); echo "$d"; }
_base_timeout(){ local s="$1"; shift; if command -v timeout >/dev/null 2>&1; then timeout "$s" "$@"; else "$@"; fi; }
_base_in_list(){ # _base_in_list <needle> <item>...  (exact, case-insensitive)
  local n x; n="$(_base_lower "$1")"; shift
  for x in "$@"; do if [ -n "$x" ] && [ "$(_base_lower "$x")" = "$n" ]; then return 0; fi; done; return 1
}
_base_vge(){ [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }   # _base_vge A B → A >= B
_base_git(){ git -c safe.directory='*' "$@"; }       # a network volume's uid mismatch trips git's dubious-ownership check
_base_sha256(){ if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
_base_replace_file(){ local f="$1"; cat > "$f.basetmp" && cat "$f.basetmp" > "$f" && rm -f "$f.basetmp"; }   # in-place edit without sed -i flavours
# A RunPod network volume is MooseFS, and df on a distributed store reports the WHOLE CLUSTER: hundreds
# of TB "free" on a 400 GB volume. A disk gate that trusts that number can never fire, so say the check
# could not be made rather than implying it passed.
_base_fs_is_pool(){
  local dev free
  if [ -n "$BASE_FAKE_ROOT" ]; then [ "${BASE_FAKE_POOL:-0}" = "1" ]; return $?; fi      # the suite decides
  dev="$(df -Pk "$1" 2>/dev/null | awk 'NR==2{print $1; f=1} END{if(!f) print ""}')"
  case "$dev" in
    mfs#*|ceph*|*glusterfs*|//*|*fuse*) return 0 ;;
    *:/*|*:[0-9]*)                      return 0 ;;   # nfs, or host:port
  esac
  free="$(_base_free_kb "$1")"
  if [ "${free:-0}" -gt 53687091200 ]; then return 0; fi        # >50 TiB free is a pool, not a quota
  return 1
}
base_mkdir(){ if [ "$BASE_DRY" = "1" ]; then would "mkdir -p $*"; else mkdir -p "$@"; fi; }
base_run(){ if [ "$BASE_DRY" = "1" ]; then would "$*"; else "$@"; fi; }   # a dry-run aware command for hooks (handbook §4); the pipeline is base_install (3.7.2)

# ---------------------------------------------------------------- prompts (exactly two in a normal run: tokens, deletion)
_base_confirm(){ # _base_confirm "<question>" → 0 = yes.  Default N; BASE_YES=1 → yes; no TTY → No.
  local q="$1" ans=""
  if [ "$BASE_YES" = "1" ]; then echo "$q y   (BASE_YES=1)"; return 0; fi
  if [ "$BASE_TTY" != "1" ]; then echo "$q N   (no TTY → default No)"; return 1; fi
  printf '%s' "$q"; IFS= read -r ans || ans=""
  [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]
}
_base_confirm_yes(){ # failure-path question whose safe answer is yes: [Y/n] on a TTY; BASE_YES=1 or no TTY → yes
  local q="$1" ans=""
  if [ "$BASE_YES" = "1" ]; then echo "$q Y   (BASE_YES=1)"; return 0; fi
  if [ "$BASE_TTY" != "1" ]; then echo "$q Y   (no TTY → the safe default)"; return 0; fi
  printf '%s' "$q"; IFS= read -r ans || ans=""
  ! [[ "$ans" =~ ^[Nn]([Oo])?$ ]]
}

# ---------------------------------------------------------------- long steps: heartbeat, output only on failure
_base_watch(){ # -q plus `| tail` emits nothing for minutes — a run that is working looks hung
  local label="$1" pid="$2" t0 el sz
  t0=$(date +%s)
  while sleep 60; do
    kill -0 "$pid" 2>/dev/null || break
    el=$(( $(date +%s) - t0 )); sz="$(du -sh "${VENV:-/nonexistent}" 2>/dev/null | cut -f1)"
    printf '    … %s — %dm%02ds%s\n' "$label" "$((el/60))" "$((el%60))" "${sz:+, venv $sz}"
  done
}
_base_run_watched(){ # _base_run_watched <label> <cmd...> → the command's status; its output only when it fails
  local label="$1"; shift
  local lf rc=0 cpid wpid
  lf="$(mktemp)"
  "$@" >"$lf" 2>&1 & cpid=$!
  _base_watch "$label" "$cpid" & wpid=$!
  wait "$cpid" || rc=$?
  kill "$wpid" 2>/dev/null || true; wait "$wpid" 2>/dev/null || true
  if [ "$rc" -ne 0 ]; then echo "  ── $label failed (exit $rc) — last 15 lines ──"; tail -15 "$lf"; fi
  rm -f "$lf"; return $rc
}

# ---------------------------------------------------------------- traps (installed by base_main, so a declare-only source leaves the caller's traps alone)
_base_on_err(){ # ERR trap: set -e is about to end the run — say where, and where the log is
  local line="$1" fn="$2" cmd="$3"
  echo -e "${RED}\n‼ unexpected failure at line $line in ${fn}: ${cmd}${NC}" >&2
  [ -n "$BASE_LOG" ] && echo "  log: $BASE_LOG" >&2
  exit 4
}
_base_on_exit(){
  local rc=$? d
  _base_install_unlock                               # 2.5.0: never leave a shared volume locked by a dead run
  for d in ${BASE_TMPDIRS[@]+"${BASE_TMPDIRS[@]}"}; do if [ -n "$d" ] && [ -d "$d" ]; then rm -rf "$d"; fi; done
  if [ "$rc" -ne 0 ] && [ "$BASE_SUMMARY_DONE" = "0" ] && [ -n "$BASE_LOG" ]; then echo "  aborted (exit $rc) — log: $BASE_LOG"; fi
  [ "$BASE_LOGGING" = "1" ] && sleep 0.3   # let tee flush the last lines
  exit "$rc"
}
_base_install_traps(){
  trap '_base_on_err "$LINENO" "${FUNCNAME[0]:-main}" "$BASH_COMMAND"' ERR
  trap _base_on_exit EXIT
}

# ---------------------------------------------------------------- the host (2.2.0): what differs between RunPod, a VM and an owned box
_base_host_resolve(){ # BASE_HOST from the evidence at hand, then the two settings that follow from it (each still overridable)
  local f="$BASE_VOLUME/comfy-base/state/host.env"
  if [ -z "$BASE_HOST" ]; then
    if [ -n "$(_base_pid1_env RUNPOD_POD_ID)" ]; then BASE_HOST=runpod
    elif [ -n "$BASE_FAKE_ROOT" ] && [ -f "$BASE_FAKE_ROOT/comfy-base/state/host.env" ]; then f="$BASE_FAKE_ROOT/comfy-base/state/host.env"; BASE_HOST="$(sed -n 's/^BASE_HOST=//p' "$f" | head -1)"
    elif [ -z "$BASE_FAKE_ROOT" ] && [ -f "$f" ]; then BASE_HOST="$(sed -n 's/^BASE_HOST=//p' "$f" | head -1)"
    fi
    # no evidence: a fake pod is RunPod-shaped (the suite's layouts; the volume layout writes host.env), a real machine is a VM
    if [ -z "$BASE_HOST" ]; then if [ -n "$BASE_FAKE_ROOT" ]; then BASE_HOST=runpod; else BASE_HOST=vm; fi; fi
  fi
  case "$BASE_HOST" in runpod|verda|crusoe|local|vm) ;; *) echo "  !! BASE_HOST='$BASE_HOST' is not one of runpod, verda, crusoe, local, vm" >&2; BASE_HOST=vm;; esac
  if [ -z "$BASE_VOLUME_KIND" ]; then if [ "$BASE_HOST" = local ]; then BASE_VOLUME_KIND=dir; else BASE_VOLUME_KIND=mount; fi; fi
  if [ -z "$BASE_LISTEN" ]; then if [ "$BASE_HOST" = runpod ]; then BASE_LISTEN=0.0.0.0; else BASE_LISTEN=127.0.0.1; fi; fi
  # 2.4.0: the shared library, from the same host.env the driver wrote. Read whatever BASE_HOST turned out to be:
  # a machine can be told its host explicitly and still have a library recorded.
  if [ -z "$BASE_LIBRARY" ]; then
    local lf="$BASE_VOLUME/comfy-base/state/host.env"
    [ -n "$BASE_FAKE_ROOT" ] && lf="$BASE_FAKE_ROOT/comfy-base/state/host.env"
    [ -f "$lf" ] && BASE_LIBRARY="$(sed -n 's/^BASE_LIBRARY=//p' "$lf" | head -1)"
  fi
  BASE_LIBRARY="${BASE_LIBRARY%/}"
  # a recorded library that is not there is a broken machine, not a silent fallback: say so, then carry on
  # without it, so the run fails on the models it cannot find rather than on a path nobody mentioned.
  if [ -n "$BASE_LIBRARY" ] && [ ! -d "$BASE_LIBRARY" ]; then
    echo "  !! BASE_LIBRARY=$BASE_LIBRARY is recorded but is not a directory on this machine, continuing WITHOUT the shared library" >&2
    BASE_LIBRARY=""
  fi
  # 2.5.0: is this root shared with other machines? The driver records it; a network device says so on its own.
  if [ "$BASE_VOLUME_SHARED" != "1" ]; then
    local sf="$BASE_VOLUME/comfy-base/state/host.env"
    [ -n "$BASE_FAKE_ROOT" ] && sf="$BASE_FAKE_ROOT/comfy-base/state/host.env"
    if [ -f "$sf" ] && [ "$(sed -n 's/^BASE_VOLUME_SHARED=//p' "$sf" | head -1)" = "1" ]; then BASE_VOLUME_SHARED=1; fi
  fi
  if [ "$BASE_VOLUME_SHARED" != "1" ] && [ -z "$BASE_FAKE_ROOT" ] && [ -d "$BASE_VOLUME" ]; then
    case "$(df -Pk "$BASE_VOLUME" 2>/dev/null | awk 'NR==2{print $1}')" in
      *:/*) BASE_VOLUME_SHARED=1 ;;                 # an nfs export as the root is a shared root by definition
    esac
  fi
  # 2.5.2: the two shapes are alternatives, not layers. With the WHOLE root on the store, a separate library
  # pointing at the same store mounts it twice, makes the run warn about "another ComfyUI tree" that is its own
  # through the second path, and sends --output-directory at the other mount. MEASURED on a live machine.
  if [ "$BASE_VOLUME_SHARED" = "1" ] && [ -n "$BASE_LIBRARY" ]; then
    echo "  ○ the whole root is the shared store, so BASE_LIBRARY=$BASE_LIBRARY is redundant and is ignored" >&2
    BASE_LIBRARY=""
  fi
  export BASE_HOST BASE_VOLUME BASE_VOLUME_KIND BASE_LISTEN BASE_LIBRARY BASE_VOLUME_SHARED BASE_LOCAL_STATE
}

# ---------------------------------------------------------------- the install lock (2.5.0), for a SHARED volume only
# Two machines installing into one venv is how a shared workspace breaks: pip and uv both write it in place and
# neither expects a second writer. mkdir is the lock rather than flock: it is atomic over NFSv4 and needs no file
# descriptor held across the run. A lock whose owner died is honoured for BASE_LOCK_STALE and then broken with it
# named, and BASE_LOCK_WAIT bounds the wait (0 refuses at once) so no run can hang on another machine forever.
BASE_INSTALL_LOCK=""
_base_install_lock(){
  [ "$BASE_VOLUME_SHARED" = "1" ] || return 0
  [ "$BASE_DRY" = "1" ] && return 0                  # --check writes nothing, so it needs nothing
  local d="$BASE_VOLUME/comfy-base/state/install.lock" waited=0 age now since who
  mkdir -p "$(dirname "$d")" 2>/dev/null || true
  while :; do
    if mkdir "$d" 2>/dev/null; then
      printf 'host=%s
pid=%s
since=%s
' "$(hostname 2>/dev/null)" "$$" "$(date -u +%s)" > "$d/owner" 2>/dev/null || true
      BASE_INSTALL_LOCK="$d"
      return 0
    fi
    who="$(sed -n 's/^host=//p' "$d/owner" 2>/dev/null | head -1)"
    now="$(date -u +%s)"; since="$(sed -n 's/^since=//p' "$d/owner" 2>/dev/null | head -1)"
    age=$(( now - ${since:-$now} ))
    if [ "$age" -gt "$BASE_LOCK_STALE" ]; then
      warn "breaking a ${age}s-old install lock left by ${who:-an unknown machine} on the shared volume"
      rm -rf "$d" 2>/dev/null; continue
    fi
    if [ "$BASE_LOCK_WAIT" -le 0 ]; then
      err "${who:-another machine} is installing on this shared volume (lock: $d); not waiting (BASE_LOCK_WAIT=0)"
      BASE_FAILED+=("install lock held by ${who:-another machine}")
      return 1
    fi
    if [ "$waited" = "0" ]; then note "${who:-another machine} is installing on this shared volume: waiting (up to $((BASE_LOCK_WAIT / 60)) min)"; fi
    sleep 5; waited=$((waited + 5))
    if [ "$waited" -ge "$BASE_LOCK_WAIT" ]; then
      err "gave up after ${waited}s waiting for ${who:-another machine} to finish installing (lock: $d)"
      BASE_FAILED+=("install lock held by ${who:-another machine} for over ${waited}s")
      return 1
    fi
  done
}
_base_install_unlock(){ [ -n "$BASE_INSTALL_LOCK" ] && rm -rf "$BASE_INSTALL_LOCK" 2>/dev/null; BASE_INSTALL_LOCK=""; return 0; }

# ---------------------------------------------------------------- environment for uv / pip / the Hub
base_env_setup(){
  _base_host_resolve
  # the RunPod image exports PIP_CONSTRAINT (pins torch) and the index URLs system-wide (seen on a live pod)
  unset PIP_CONSTRAINT PIP_INDEX_URL PIP_EXTRA_INDEX_URL
  export PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
  # The legacy HF transfer accelerator is deprecated; the Hub is fully Xet-backed and hf-xet is the only
  # fast path. HF_HUB_DISABLE_XET is cleared too in case an image sets it.
  unset HF_HUB_ENABLE_HF_TRANSFER HF_HUB_DISABLE_XET
  export HF_XET_HIGH_PERFORMANCE=1                                   # saturate the link, use every core
  export HF_XET_NUM_CONCURRENT_RANGE_GETS="${BASE_XET_CONCURRENCY:-32}"   # upstream default 16; 32 saturates a pod link
  export HF_XET_CHUNK_CACHE_SIZE_BYTES=0                             # one-shot pulls: a chunk cache can only cost a second write
  export HF_HUB_DOWNLOAD_TIMEOUT=60 HF_HUB_DISABLE_UPDATE_CHECK=1 HF_HUB_DISABLE_TELEMETRY=1
  # Where the base lives and keeps its state. On a machine: <BASE_VOLUME>/comfy-base (the volume; /workspace unless told
  # otherwise). In fake mode: inside the fake root. Off-pod with neither: beside the library (a repo checkout running its own suite).
  if [ -n "$BASE_FAKE_ROOT" ]; then
    BASE_HOME="$BASE_FAKE_ROOT/comfy-base"
  elif [ -d "$BASE_VOLUME" ] && [ -w "$BASE_VOLUME" ]; then
    BASE_HOME="$BASE_VOLUME/comfy-base"
    # the ONE filesystem a pod stop/start keeps
    BASE_PERSIST_ROOT="${BASE_PERSIST_ROOT:-$BASE_VOLUME}"
    # uv/pip caches belong on the volume: wheels survive a restart, and uv hardlinks into a venv on the
    # same filesystem instead of copying
    export UV_CACHE_DIR="${UV_CACHE_DIR:-$BASE_VOLUME/.cache/uv}" PIP_CACHE_DIR="${PIP_CACHE_DIR:-$BASE_VOLUME/.cache/pip}"
    mkdir -p "$UV_CACHE_DIR" "$PIP_CACHE_DIR" 2>/dev/null || true
    # the Hub cache too: node packs that fetch their own weights at first queue would otherwise refill ~/.cache after every stop
    export HF_HOME="${HF_HOME:-$BASE_PERSIST_ROOT/huggingface}"
    mkdir -p "$HF_HOME" 2>/dev/null || true
  else
    BASE_HOME="$BASE_DIR"
  fi
  BASE_STATE="$BASE_HOME/state"
  mkdir -p "$BASE_STATE/packages" "$BASE_STATE/logs" 2>/dev/null || true   # state is ours even under --check
  # The venv's INTERPRETER is a runtime dependency, not a cache. `uv venv --python X` fetches a managed
  # CPython into $XDG_DATA_HOME/uv/python (container disk) and the venv points at it with an ABSOLUTE
  # symlink; after a pod stop/start the interpreter is gone, start.sh's `python main.py & wait` dies at
  # once, PID 1 exits and THE POD STOPS with no terminal to repair from (seen on a live pod). So the interpreter
  # lives where the venv lives — and NOT under UV_CACHE_DIR, because `uv cache clean` is documented as safe.
  export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$BASE_HOME/python}"
  mkdir -p "$UV_PYTHON_INSTALL_DIR" 2>/dev/null || true
  export UV_MANAGED_PYTHON=1        # one source of Python: never bind a venv to an interpreter a future image ships
  # The Xet cache does NOT go on the volume: upstream wants local SSD, a RunPod volume is a network filesystem,
  # and with the chunk cache off there is nothing in it worth persisting.
  local xb="${BASE_FAKE_ROOT:-${HOME:-/root}}"; xb="${xb%/}"
  export HF_XET_CACHE="${HF_XET_CACHE:-$xb/.cache/xet}"
  mkdir -p "$HF_XET_CACHE" 2>/dev/null || true
  export PATH="$HOME/.local/bin:$PATH"
  umask 022
}
