# 84-run-local.sh — ComfyUI runs from a copy of its code on the machine's own disk (3.15.0). The volume stays the truth:
# every install writes it exactly as before, and every ComfyUI process this base starts (the boot's, the install's
# restart, the import check's quick test) starts from a generation py/run_local.py keeps in step with it.
#
# MEASURED on a RunPod pod with a network volume (the Video Swapper's install, 2026-10-16): the import check ran 5+
# minutes at near-zero CPU, 262 of 265 threads waiting on futexes and one in request_wait_answer, a FUSE read. The
# venv (12 GB, tens of thousands of files), the uv Python and custom_nodes were all on the volume, so every import,
# every stat of every sys.path entry and every mmap page of torch's libraries crossed it, at every start.
#
#   BASE_LOCAL_RUN  auto (default): on where the host is RunPod and the volume is a network filesystem; 1: on, any host;
#                   0: off (everything runs from the volume, as before 3.15.0). Off for a dry run and a seed build.
#   BASE_RUN_ROOT   where the generations live: /opt/comfy-run, the container disk (gone at every stop, so a pod
#                   restarted on a fresh container disk copies again; the volume is never written by this)
#   BASE_RUN_JOBS   parallel reads (default 32: the cost is the network's latency per file, not its bandwidth)
#   BASE_RUN_CHECK  the module the copy's python must import before it is used (default torch)
# Sourced by base.sh (with every lib) and by boot.sh (beside 85-launch.sh). bash 3.2-clean; never fatal: any failure
# says why on one line and the process starts from the volume, the pre-3.15.0 behaviour.

_base_run_root(){ echo "${BASE_RUN_ROOT:-/opt/comfy-run}"; }
_base_run_state(){ echo "${BASE_STATE:-${BASE_HOME:+$BASE_HOME/state}}"; }
_base_run_mode(){ # → off | auto | on
  if [ "${BASE_DRY:-0}" = "1" ] || [ "${BASE_SEED:-0}" = "1" ]; then echo off; return 0; fi
  case "${BASE_LOCAL_RUN:-auto}" in
    0|off|no|false) echo off ;;
    1|on|yes|true) echo on ;;
    *) if [ "${BASE_HOST:-}" = runpod ]; then echo auto; else echo off; fi ;;
  esac
}
_base_run_tool_py(){ # the machine's own python3 (on its own disk) runs the copy; the tools venv's only when there is none
  local p
  p="$(command -v python3 2>/dev/null || true)"; if [ -n "$p" ]; then echo "$p"; return 0; fi
  if [ -n "${BASE_HOME:-}" ] && [ -x "$BASE_HOME/tools/bin/python" ]; then echo "$BASE_HOME/tools/bin/python"; return 0; fi
  return 1
}
_base_run_gen_bump(){ # the code on the volume may have changed: the next start walks it again instead of trusting the stamps
  local s; s="$(_base_run_state)"
  [ -n "$s" ] || return 0
  mkdir -p "$s" 2>/dev/null || return 0
  printf '%s %s\n' "$(date +%s)" "$$" > "$s/run.gen" 2>/dev/null || true
}
_base_run_local(){ # sync the copy; sets BASE_RUN_COMFY / BASE_RUN_PY (empty: start from the volume) and BASE_RUN_VERDICT
  BASE_RUN_COMFY=""; BASE_RUN_PY=""; BASE_RUN_VERDICT="off"
  local mode tool lib out line v tree py secs why
  mode="$(_base_run_mode)"
  [ "$mode" != off ] || return 0
  if [ -z "${COMFY:-}" ] || [ -z "${VENV:-}" ]; then return 0; fi
  # a copy that was refused (no room, an error) is not walked again for the same code: the boot asks twice, once for its
  # torch gate and once at the launch, and a walk over the network is the very cost this exists to avoid
  local key; key="$COMFY|$VENV|$(cat "$(_base_run_state)/run.gen" 2>/dev/null || true)"
  if [ "$key" = "${BASE_RUN_LAST_KEY:-}" ] && [ -n "${BASE_RUN_LAST_VERDICT:-}" ]; then
    case "$BASE_RUN_LAST_VERDICT" in fresh|synced) ;; *) BASE_RUN_VERDICT="$BASE_RUN_LAST_VERDICT"; return 0 ;; esac
  fi
  lib="${BASE_DIR:-${BOOT_HOME:-}}/py/run_local.py"
  if [ ! -f "$lib" ]; then echo "  ! no $lib: ComfyUI starts from the volume"; BASE_RUN_VERDICT="no tool"; return 0; fi
  if ! tool="$(_base_run_tool_py)"; then echo "  ! no python3 to copy with: ComfyUI starts from the volume"; BASE_RUN_VERDICT="no python"; return 0; fi
  out="$("$tool" "$lib" sync --root "$(_base_run_root)" --tree "$COMFY" --venv "$VENV" --state "$(_base_run_state)" \
           --python-root "${BASE_HOME:+$BASE_HOME/python}" --jobs "${BASE_RUN_JOBS:-32}" --check-import "${BASE_RUN_CHECK:-torch}" \
           $([ "$mode" = auto ] && echo --auto) 2>&1 || true)"
  printf '%s\n' "$out" | grep -v '^RUN_LOCAL ' | grep -v '^$' || true
  line="$(printf '%s\n' "$out" | grep '^RUN_LOCAL ' | tail -1 || true)"
  read -r _ v tree py secs why <<< "$line"
  BASE_RUN_VERDICT="${v:-silent}"; BASE_RUN_LAST_KEY="$key"; BASE_RUN_LAST_VERDICT="$BASE_RUN_VERDICT"
  case "$v" in
    fresh|synced)
      BASE_RUN_COMFY="$tree"; BASE_RUN_PY="$py"
      if [ "$v" = fresh ]; then echo "  from this machine's disk: $tree (the copy is current, ${secs} s to check)"
      else echo "  from this machine's disk: $tree (copied from the volume in ${secs} s)"; fi ;;
    skip) [ "$mode" = auto ] || echo "  not copied: $why" ;;
    noroom|failed) echo "  ! not copied ($v: $why): ComfyUI starts from the volume" ;;
    *) echo "  ! the copy said nothing it should (${out:+$(printf '%s' "$out" | tail -1 | cut -c1-200)}): ComfyUI starts from the volume" ;;
  esac
  return 0
}
_base_run_here(){ # → RUN_HERE_DIR, RUN_HERE_PY: where a ComfyUI process starts from now (the copy when it is good, else the volume)
  RUN_HERE_DIR="$COMFY"; RUN_HERE_PY="$PY"
  _base_run_local
  if [ -n "$BASE_RUN_PY" ] && [ -x "$BASE_RUN_PY" ] && [ -f "$BASE_RUN_COMFY/main.py" ]; then RUN_HERE_DIR="$BASE_RUN_COMFY"; RUN_HERE_PY="$BASE_RUN_PY"
  else                                               # from the volume after all: its code read in parallel, beside the start
    case "$BASE_RUN_VERDICT" in skip) ;; *) _base_run_warm_bg ;; esac   # skip: the volume is a local disk, nothing to warm
  fi
  return 0
}
_base_run_warm_bg(){ # the volume's venv, uv Python and code read 64 files at a time, in the background, once per code change
  # MEASURED by the platform orchestrator on a RunPod pod: 18 s for the whole venv and custom_nodes, after which the
  # imports took seconds, not minutes (1.72 ms per stat on the volume, 0.01 ms on the container disk). Only for a start
  # that runs from the volume: a good copy already read every one of those files once. Only on a network filesystem.
  [ "${BASE_DRY:-0}" != "1" ] && [ "${BASE_SEED:-0}" != "1" ] || return 0
  [ -n "${COMFY:-}" ] && [ -n "${VENV:-}" ] || return 0
  local tool lib="${BASE_DIR:-${BOOT_HOME:-}}/py/run_local.py" key st log
  [ -f "$lib" ] && tool="$(_base_run_tool_py)" || return 0
  st="$(_base_run_state)"
  key="$COMFY|$VENV|$(cat "$st/run.gen" 2>/dev/null || true)"
  [ "$key" != "${BASE_RUN_WARM_KEY:-}" ] || return 0
  BASE_RUN_WARM_KEY="$key"
  log="${st:-/tmp}/logs/run-warm.log"; mkdir -p "$(dirname "$log")" 2>/dev/null || true
  nohup "$tool" "$lib" warm --tree "$COMFY" --venv "$VENV" --python-root "${BASE_HOME:+$BASE_HOME/python}" \
    --jobs "${BASE_WARM_JOBS:-64}" --auto >> "$log" 2>&1 &
  echo "  reading ComfyUI's code from the volume in parallel, in the background (pid $!, $log)"
}
_base_run_prewarm(){ # 3.15.0: a head start, in the background, beside other work: the copy (or, without one, the warm read).
  # The boot calls it before sshd and JupyterLab; an install calls it once its venv is final. The start that follows
  # waits for it on the copy's lock and finds it current, or walks again only what changed meanwhile. ONE process
  # (run_local.py prewarm), never a forked shell: a bash subshell shows its parent's whole command line to ps.
  local mode tool lib="${BASE_DIR:-${BOOT_HOME:-}}/py/run_local.py" st log
  mode="$(_base_run_mode)"
  [ "$mode" != off ] || { _base_run_warm_bg; return 0; }
  [ -n "${COMFY:-}" ] && [ -n "${VENV:-}" ] || return 0
  [ -f "$lib" ] && tool="$(_base_run_tool_py)" || return 0
  st="$(_base_run_state)"; log="${st:-/tmp}/logs/run-local.log"; mkdir -p "$(dirname "$log")" 2>/dev/null || true
  nohup "$tool" "$lib" prewarm --root "$(_base_run_root)" --tree "$COMFY" --venv "$VENV" --state "$st" \
    --python-root "${BASE_HOME:+$BASE_HOME/python}" --jobs "${BASE_RUN_JOBS:-32}" --check-import "${BASE_RUN_CHECK:-torch}" \
    --warm-jobs "${BASE_WARM_JOBS:-64}" $([ "$mode" = auto ] && echo --auto) >> "$log" 2>&1 &
  BASE_RUN_PREWARM_PID=$!
  echo "  ComfyUI's code: copying to this machine's disk in the background (pid $!, $log)"
}
_base_run_status(){ # one line for `base.sh status`
  local tool lib="${BASE_DIR:-${BOOT_HOME:-}}/py/run_local.py"
  [ -f "$lib" ] && tool="$(_base_run_tool_py)" || return 0
  echo "  local copy (BASE_LOCAL_RUN=${BASE_LOCAL_RUN:-auto}, $(_base_run_mode)): $("$tool" "$lib" status --root "$(_base_run_root)" 2>&1 | head -1)"
}
