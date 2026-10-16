# 80-server.sh — the running ComfyUI: import check in the venv, the opt-in restart, smoke, combos. The launch line itself
# lives in 85-launch.sh, shared with boot.sh.
# By default nothing here stops a server: on a shared pod that process may be mid-render, so restarting is the
# user's call. BASE_RESTART=1 opts in.

_base_preview_effective(){ # what preview method is ACTUALLY in effect: the UI setting overrides the CLI flag per queue
  local flag="" setting="" cfg="$(_base_user_dir)/default/comfy.settings.json"
  if [ -n "$RUN_PID" ] && [ -r "/proc/$RUN_PID/cmdline" ]; then tr '\0' '\n' < "/proc/$RUN_PID/cmdline" 2>/dev/null | grep -qx -- '--preview-method' && flag=1 || true
  else flag="?"; fi
  if [ -r "$cfg" ]; then setting="$("$SYS_PY" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("Comfy.Execution.PreviewMethod", "") or "")
except Exception: print("")' "$cfg" 2>/dev/null || true)"; fi
  case "$setting" in
    taesd|auto|latent2rgb) ok "live preview method is \"$setting\" in the UI — that overrides the CLI, previews are on" ;;
    none) warn "previews off: the UI has Live preview method = \"none\", which overrides the launch flag (Settings > Execution > Live preview method → auto)" ;;
    *) if [ "$flag" = "1" ]; then ok "running server carries --preview-method and the UI defers to it — previews are on"
       elif [ "$flag" = "?" ]; then note "no running server to check — previews depend on the launch line printed above"
       else warn "previews off: the running server was started WITHOUT --preview-method and the UI defers to it (fix without restarting: Settings > Execution > Live preview method → auto)"; fi ;;
  esac
}

_base_quick_cpu(){ # 3.10.0: "--cpu" for a seed build. The template image is built where no GPU is, and ComfyUI's startup
  # died "Found no NVIDIA driver" before importing a single pack, so no image was ever built. --cpu changes only the
  # device ComfyUI picks; every pack is still imported and judged. A real machine never sets BASE_SEED, so it still
  # starts on its GPU here (as 3.9.1's BASE_NO_GPU drops the device question for venv_verify.py and nowhere else).
  [ "${BASE_SEED:-0}" = "1" ] && echo "--cpu"; return 0
}
_base_quick_test(){ # <log> → main.py --quick-test-for-ci's exit status (ComfyUI loads, imports every pack, exits)
  (cd "$COMFY" && _base_timeout 900 "$PY" main.py --quick-test-for-ci --disable-auto-launch $(_base_quick_cpu) >"$1" 2>&1)
}
_base_core_failed(){ # <log> <rc> → 0 when ComfyUI ITSELF did not start (as opposed to a pack that failed to import)
  local log="$1" rc="$2"
  # a pack that fails to import prints its traceback and an "(IMPORT FAILED)" line, and ComfyUI still exits 0: a
  # non-zero exit with no such line is ComfyUI itself (the same judgement 2.x made)
  [ "$rc" -ne 0 ] && ! grep -q "IMPORT FAILED" "$log"
}
base_import_check(){ # main.py --quick-test-for-ci, ONCE, after every hook. 3.0.0: every dependency is at its newest, so:
  #   a PACK that does not import with them is named as "needs upgrading upstream" (not a failure, the restart goes on);
  #   ComfyUI's OWN startup failing is the one thing rolled back: only the packages this run changed, from the snapshot.
  hdr "IMPORT CHECK · main.py --quick-test-for-ci in $VENV"
  if [ "$BASE_DRY" = "1" ]; then note "skipped in --check"; BASE_IMPORT_RESULT="skipped (--check)"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "skipped (BASE_NO_NET: fake venv)"; BASE_IMPORT_RESULT="skipped (fake)"; return 0; fi
  case "$BASE_VENV_RESULT" in failed*|rolled*) note "skipped — the venv step failed"; BASE_IMPORT_RESULT="skipped (venv failed)"; BASE_BLOCK_RESTART=1; return 0;; esac
  if [ ! -x "$PY" ]; then note "skipped — no interpreter at $PY"; BASE_IMPORT_RESULT="skipped (no venv)"; return 0; fi
  _base_tmp; local log="$BASE_TMPD/import_check.log" rc=0 name failed=() diag
  _base_quick_test_scoped "$log" || rc=$?            # 3.2.0: just the own packs when nothing else changed (lib/62-own-packs.sh)
  # ---- ComfyUI itself did not start: that stops every machine on the store, so the newest set is rolled back
  if _base_core_failed "$log" "$rc"; then
    miss "ComfyUI itself did not start (exit $rc): last lines:"; tail -12 "$log" | sed 's/^/     /'
    cp "$log" "$BASE_STATE/import_check.log" 2>/dev/null || true
    if [ -n "$BASE_VENV_BACKUP" ] && _base_confirm_yes "Roll the venv back to $BASE_VENV_BACKUP (the old server keeps running either way)? [Y/n] "; then
      _base_venv_rollback || true
      BASE_FAILED+=("import check: ComfyUI did not start in the rebuilt venv; rolled back to $BASE_VENV_BACKUP"); BASE_IMPORT_RESULT="FAILED (rebuilt venv rolled back)"; BASE_BLOCK_RESTART=1; return 0
    fi
    if _base_runtime_on; then                         # 3.1.0: nothing was changed, so nothing is rolled back
      BASE_FAILED+=("import check: ComfyUI did not start from the proven runtime (log: $BASE_STATE/import_check.log)"); BASE_IMPORT_RESULT="FAILED (runtime)"; BASE_BLOCK_RESTART=1; return 0
    fi
    local changed=""
    if base_lift_rollback_core; then
      changed="$(tr '\n' ' ' < "$BASE_LIFT_DIR/rollback.txt" 2>/dev/null | cut -c1-400)"
      rc=0; _base_quick_test "$log" || rc=$?
      if ! _base_core_failed "$log" "$rc"; then
        err "ComfyUI starts again with the packages this run changed put back: ${changed:-none}"
        BASE_FAILED+=("import check: ComfyUI did not start with the newest of: ${changed:-?}; those were rolled back, ComfyUI starts, and they need upgrading upstream")
        BASE_UPSTREAM+=("ComfyUI $COMFY_NEW does not start with the newest of: ${changed:-?}")
        BASE_IMPORT_RESULT="FAILED (core; rolled back, ComfyUI starts)"
      else
        BASE_FAILED+=("import check: ComfyUI does not start (exit $rc), even with this run's changes rolled back; see $BASE_STATE/import_check.log")
        BASE_IMPORT_RESULT="FAILED (core, exit $rc)"; BASE_BLOCK_RESTART=1; return 0
      fi
    else
      BASE_FAILED+=("import check: ComfyUI does not start (exit $rc) and there was no snapshot to roll back to"); BASE_IMPORT_RESULT="FAILED (core, exit $rc)"; BASE_BLOCK_RESTART=1; return 0
    fi
  fi
  # ---- packs: named, never blocking. The pack's own newest code cannot run with a newest dependency.
  while IFS= read -r name; do [ -n "$name" ] && failed+=("$name"); done < <(grep -E '\(IMPORT FAILED\):' "$log" | sed -E 's/.*\(IMPORT FAILED\):[[:space:]]*//' | xargs -n1 basename 2>/dev/null | sort -u || true)
  # 3.3.0: a pack that fails on a module nothing declared gets it (from the reviewed map, lib/47-latest.sh), and is judged again
  if [ "${#failed[@]}" -gt 0 ] && _base_repair_modules "$log"; then
    rc=0; _base_quick_test "$log" || rc=$?; failed=()
    while IFS= read -r name; do [ -n "$name" ] && failed+=("$name"); done < <(grep -E '\(IMPORT FAILED\):' "$log" | sed -E 's/.*\(IMPORT FAILED\):[[:space:]]*//' | xargs -n1 basename 2>/dev/null | sort -u || true)
  fi
  if [ "${#failed[@]}" -gt 0 ]; then
    warn "pack(s) that do not import with the newest dependencies (named, not blocking): ${failed[*]}"
    for name in "${failed[@]}"; do
      diag="$(grep -n -B2 -A25 -F "$name" "$log" 2>/dev/null | grep -E 'Error|error' 2>/dev/null | grep -v 'IMPORT FAILED' | tail -1 | sed -E 's/^[0-9]+[-:]//' | cut -c1-200 || true)"
      echo "  ── $name: ${diag:-see the log}"
      BASE_UPSTREAM+=("pack $name does not import with the newest dependencies (${diag:-see $BASE_STATE/import_check.log}): the pack needs upgrading upstream")
    done
    cp "$log" "$BASE_STATE/import_check.log" 2>/dev/null || true; echo "  full log: $BASE_STATE/import_check.log"
  fi
  if declare -F pkg_import_check >/dev/null; then pkg_import_check || { BASE_BLOCK_RESTART=1; BASE_IMPORT_RESULT="FAILED (package check)"; return 0; }; fi
  if [ -z "$BASE_IMPORT_RESULT" ] || [ "${BASE_IMPORT_RESULT#FAILED}" = "$BASE_IMPORT_RESULT" ]; then
    if [ "${#failed[@]}" -gt 0 ]; then BASE_IMPORT_RESULT="ok: ComfyUI starts; ${#failed[@]} pack(s) named for upstream (${failed[*]})"
    else BASE_IMPORT_RESULT="ok (${#PACK_DIRS[@]} packs import)"; ok "every pack imports; ComfyUI loaded and exited cleanly"; fi
  fi
}

_base_restart_comfy(){ # 3.0.3: stop every ComfyUI on this machine, bring exactly ONE up, wait for it; 0 when it answers
  # MEASURED on a Verda machine: the install's restart started ComfyUI as a detached process (ppid 1) outside the boot
  # unit's cgroup, so a later `systemctl restart comfy-base-boot` started a SECOND one beside it: two main.py, the old
  # one holding :8188 and 34 GB of VRAM. Where the unit owns ComfyUI the restart goes through the unit, so the server
  # is always the unit's and a unit restart replaces it. BASE_RESTART_VIA says which way it went (unit | direct).
  BASE_RESTART_VIA=direct
  if _base_boot_unit_owns; then
    echo "  through the boot unit: systemctl stop, then start, comfy-base-boot (JupyterLab restarts with it)"
    if _base_root systemctl stop comfy-base-boot.service; then
      BASE_RESTART_VIA=unit
      _base_stop_comfy || return 1                 # one the unit never owned: an earlier install's detached start
      if ! _base_root systemctl start comfy-base-boot.service; then echo "  systemctl start comfy-base-boot failed"; return 1; fi
      _base_wait_comfy 180                         # the boot starts sshd, JupyterLab and its extensions before ComfyUI
      return
    fi
    echo "  could not stop comfy-base-boot: starting ComfyUI directly (the unit's next start replaces it)"
  fi
  _base_stop_comfy || return 1
  _base_start_comfy
}
_base_restart_hint(){ # the exact commands that restart ComfyUI by hand, for the hand-off
  local pids
  if _base_boot_unit_enabled; then
    echo "        $([ "$(id -u 2>/dev/null)" = 0 ] || echo 'sudo ')systemctl restart comfy-base-boot"
    return 0
  fi
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "${pids// /}" ]; then echo "        kill ${pids% }"; fi
  echo "        cd $COMFY && nohup $(_base_start_cmd) >> $COMFY_LOG 2>&1 &"
}

base_restart(){ # reports what needs a restart and prints the exact launch line; BASE_RESTART=1 actually does it
  hdr "COMFYUI RESTART"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_RESTART_RESULT="skipped"; return 0; fi
  BASE_RESTART_WHY=()
  if [ -n "$COMFY_NEW" ] && [ "$COMFY_NEW" != "$COMFY_OLD" ] && [ "$COMFY_NEW" != "?" ]; then BASE_RESTART_WHY+=("ComfyUI moved $COMFY_OLD → $COMFY_NEW; the running process is the old code"); fi
  if [ "${#MODEL_DL[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#MODEL_DL[@]} model file(s) downloaded"); fi
  if [ "${#MODEL_MOVED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#MODEL_MOVED[@]} model file(s) relocated"); fi
  if [ "${#PACK_CLONED[@]}" -gt 0 ] || [ "${#PACK_UPDATED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("node packs cloned or moved to their pinned commits"); fi
  # every value that means the venv CHANGED — matching one string here once silently missed "rebuilt"
  case "$BASE_VENV_RESULT" in built|rebuilt|adopted) BASE_RESTART_WHY+=("the venv was $BASE_VENV_RESULT");; esac
  if [ "${#BASE_CHANGED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#BASE_CHANGED[@]} hygiene change(s), including launch args"); fi
  # A package's own reasons, set by a hook BEFORE this runs (pkg_pre_models / pkg_post_models).
  # ComfyUI registers custom nodes at import time, so a package that ships one has no other way
  # to say "the running process cannot see this yet".
  if [ "${#PKG_RESTART_WHY[@]}" -gt 0 ]; then BASE_RESTART_WHY+=(${PKG_RESTART_WHY[@]+"${PKG_RESTART_WHY[@]}"}); fi
  # 3.3.0: Python packages this run moved (the newest pass, a repaired module): the running server has the old ones loaded
  if [ "${BASE_PY_MOVED:-0}" -gt 0 ] 2>/dev/null; then BASE_RESTART_WHY+=("$BASE_PY_MOVED Python package(s) moved; the running server still has the old ones loaded"); fi
  local live=0
  curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1 && live=1
  if [ "$live" = 0 ]; then
    if [ "${BASE_BLOCK_RESTART:-0}" = "1" ]; then miss "do NOT start ComfyUI — the import check failed; fix that first"; BASE_RESTART_RESULT="do not start (import check failed)"; BASE_RESTART_NEEDED=1; return 0; fi
    if [ "$BASE_RESTART" = "1" ]; then       # opting in means a running server at the end, whether or not one was running before (2.0.11)
      if _base_restart_comfy; then ok "ComfyUI started (BASE_RESTART=1, $BASE_RESTART_VIA)"; BASE_RESTART_RESULT="started"; RUN_PID="$(_base_comfy_pids | head -1 || true)"; _base_preview_effective
      else err "ComfyUI did not answer within ${BASE_START_WAIT:-600} s — last 40 lines of $COMFY_LOG:"; tail -40 "$COMFY_LOG" 2>/dev/null | sed 's/^/     /'; BASE_RESTART_RESULT="FAILED"; BASE_FAILED+=("ComfyUI did not come up"); fi
      return 0
    fi
    BASE_RESTART_NEEDED=1; BASE_RESTART_RESULT="ComfyUI is not running"
    todo "ComfyUI is not running on $HOSTPORT — start it:"
    echo ""; _base_restart_hint; echo ""
    echo "      Then verify:  bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
    return 0
  fi
  if [ "${BASE_BLOCK_RESTART:-0}" = "1" ]; then miss "do NOT restart — the import check failed; the running server is still healthy"; BASE_RESTART_RESULT="do not restart (import check failed)"; return 0; fi
  if [ "${#BASE_RESTART_WHY[@]}" -eq 0 ]; then ok "nothing changed that needs a restart"; BASE_RESTART_RESULT="not needed"; _base_preview_effective; return 0; fi
  if [ "$BASE_RESTART" = "1" ]; then
    if _base_restart_comfy; then ok "ComfyUI restarted (BASE_RESTART=1, $BASE_RESTART_VIA)"; BASE_RESTART_RESULT="restarted"; RUN_PID="$(_base_comfy_pids | head -1 || true)"; _base_preview_effective
    else err "ComfyUI did not answer within ${BASE_START_WAIT:-600} s — last 40 lines of $COMFY_LOG:"; tail -40 "$COMFY_LOG" 2>/dev/null | sed 's/^/     /'; BASE_RESTART_RESULT="FAILED"; BASE_FAILED+=("ComfyUI did not come back up"); fi
    return 0
  fi
  BASE_RESTART_NEEDED=1; BASE_RESTART_RESULT="YOURS TO DO"
  todo "ComfyUI needs a restart before it can see this:"
  local r; for r in "${BASE_RESTART_WHY[@]}"; do echo "        - $r"; done
  echo "      Nothing has been stopped — the server you had running is untouched."
  echo "      From this shell that would be:"; echo ""
  _base_restart_hint; echo ""
  echo "      Then verify:  bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
}

base_smoke(){ # every node type the workflow uses is registered on the live server; never blocks
  hdr "SMOKE · $HOSTPORT"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_SMOKE_RESULT="skipped"; return 0; fi
  if [ -z "${WF_NAME:-}" ] || [ ! -f "${PKG_DIR:-.}/$WF_NAME" ]; then note "skipped — no workflow to smoke"; BASE_SMOKE_RESULT="skipped (no workflow)"; return 0; fi
  if ! curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then
    note "skipped — nothing is listening on $HOSTPORT; start ComfyUI, then: bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
    BASE_SMOKE_RESULT="skipped (ComfyUI not running)"; return 0
  fi
  local label=""; [ "$BASE_RESTART_NEEDED" = "1" ] && label=" (pre-restart: the server is the OLD code)"
  local api="${PKG_DIR}/${WF_NAME%.json}_api.json"; [ -f "$api" ] || api=""
  if "$SYS_PY" "$BASE_DIR/py/smoke.py" "$PKG_DIR/$WF_NAME" "$HOSTPORT" $api 2>&1 | sed 's/^/  /'; then BASE_SMOKE_RESULT="ok$label"; ok "smoke passed$label"
  elif [ "$BASE_RESTART_NEEDED" = "1" ]; then BASE_SMOKE_RESULT="failed pre-restart"; warn "smoke failed against the pre-restart server — restart, then: bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
  else BASE_SMOKE_RESULT="FAILED"; BASE_FAILED+=("smoke failed (see above)"); fi
}

base_combos(){ # stored dropdown values → live /object_info; repairs renames (never with a restart pending), fails on values no pack offers
  hdr "COMBO CHECK · stored dropdown values → live /object_info"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_COMBO_RESULT="skipped"; return 0; fi
  if [ -z "${WF_NAME:-}" ] || [ ! -f "${PKG_DIR:-.}/$WF_NAME" ]; then note "skipped — no workflow"; BASE_COMBO_RESULT="skipped (no workflow)"; return 0; fi
  if ! curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then note "skipped — no server to ask"; BASE_COMBO_RESULT="skipped (no server)"; return 0; fi
  local pristine="$PKG_DIR/$WF_NAME" copy="$(_base_workflows_dir)/$WF_NAME" t out pre ts f b n_fix=0 n_bad=0 flags=()
  # with a restart pending the server is the old code: report, never rewrite
  [ "$BASE_RESTART_NEEDED" = "1" ] && flags=(--check-only)
  for t in "$pristine" "$copy"; do
    [ -f "$t" ] || continue
    pre="$(_base_mktemp_d)/pre.json"; cp -p "$t" "$pre"
    out="$("$SYS_PY" "$BASE_DIR/py/combofix.py" "$HOSTPORT" "$t" ${flags[@]+"${flags[@]}"} 2>&1)" || true
    printf '%s\n' "$out" | sed '/^COMBOFIX /d' | sed 's/^/  /'
    f="$(printf '%s' "$out" | sed -n 's/^COMBOFIX \([0-9]*\) repaired.*/\1/p' | tail -1)"
    b="$(printf '%s' "$out" | sed -n 's/^COMBOFIX [0-9]* repaired \([0-9]*\).*/\1/p' | tail -1)"
    n_fix=$(( n_fix + ${f:-0} )); n_bad=$(( n_bad + ${b:-0} ))
    if ! cmp -s "$t" "$pre"; then ts="$(_base_ts)"; cp -p "$pre" "$t.$ts.bak"; note "changed — previous version kept as $(basename "$t").$ts.bak"; fi
  done
  if [ "${n_bad:-0}" != "0" ]; then
    if [ "$BASE_RESTART_NEEDED" = "1" ]; then warn "$n_bad dropdown value(s) unresolved against the pre-restart server — re-check after the restart"; BASE_COMBO_RESULT="pre-restart: $n_bad unresolved"
    else err "$n_bad stored dropdown value(s) are not offered by the installed packs — ComfyUI will reject them at queue time"; BASE_FAILED+=("combo check: $n_bad value(s) no pack offers"); BASE_COMBO_RESULT="FAILED ($n_bad unresolved)"; fi
  elif [ "${n_fix:-0}" != "0" ]; then
    if [ "$BASE_RESTART_NEEDED" = "1" ]; then BASE_COMBO_RESULT="pre-restart: $n_fix would be repaired"; note "$n_fix renamed value(s) would be repaired after the restart"
    else ok "repaired $n_fix dropdown value(s) a pack had renamed"; BASE_COMBO_RESULT="repaired $n_fix"; BASE_CHANGED+=("combo: repaired $n_fix renamed dropdown value(s)"); fi
  else ok "every stored dropdown value is offered by the installed packs"; BASE_COMBO_RESULT="ok"; fi
}
