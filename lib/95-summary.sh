# 95-summary.sh — the pipeline (base_install), the dispatcher (base_main), the suite runner (base_test), the summary,
# and base_cli for `bash base.sh <command>`.

_base_base_min_gate(){ # a package written against a newer base than this one is refused, both versions named
  if _base_vge "$BASE_VERSION" "${BASE_MIN:-0}"; then return 0; fi
  echo "  !! ${PKG_NAME:-this package} needs base ${BASE_MIN} or newer; this base is ${BASE_VERSION} — upload the newer base zip first" >&2
  return 1
}
_base_validate_tables(){ # every required declaration present and every table row well-formed
  local bad=0 v
  for v in PKG_ID PKG_NAME PKG_VERSION BASE_MIN COMFY_MIN; do
    if [ -z "${!v:-}" ]; then echo "  !! $v is not declared by the package script" >&2; bad=1; fi
  done
  _base_base_min_gate || bad=1
  if ! [[ "${PKG_ID:-}" =~ ^[a-z0-9_-]+$ ]]; then echo "  !! PKG_ID must match [a-z0-9_-]+ (got '${PKG_ID:-}')" >&2; bad=1; fi
  _base_pack_rows >/dev/null || bad=1
  _base_model_rows >/dev/null || bad=1
  _base_later_rows >/dev/null || bad=1
  _base_own_rows >/dev/null || bad=1      # 3.2.0: each VENDORED_PACKS entry is a node pack beside the script
  return $bad
}
_base_declare_dump(){ # BASE_DECLARE_ONLY=1: the validated tables, one row per line, for list-packs / gen-models / suites
  local rc=0
  _base_pack_rows | sed 's/^/PACKROW /' || rc=1
  _base_model_rows | sed 's/^/MODELROW /' || rc=1
  _base_later_rows | sed 's/^/MODELLATER /' || rc=1
  _base_own_rows | sed 's/^/OWNROW /' || rc=1      # 3.2.0: dir|version|digest, the package's own packs (they ship in its zip)
  _base_model_hashes | sed 's/^/MODELHASH /' || rc=1   # 3.4.0: file|sha256, from models.sha256 (base.sh gen-hashes)
  return $rc
}
_base_hook(){ # run a package hook if the package defines it; a failing hook fails the run (no fallbacks)
  if declare -F "$1" >/dev/null; then
    hdr "HOOK · $1"
    if ! "$1"; then err "$1 failed"; BASE_FAILED+=("hook $1 failed"); fi
  fi
  return 0
}

base_init(){
  base_env_setup
  _base_logging
  local drc=0; base_discover quiet || drc=$?
  if [ "$drc" = 3 ]; then base_discover || true; exit 3; fi             # no network volume: the refusal, printed
  if [ "$drc" = 4 ] && _base_runtime_on; then err "runtime mode and no ComfyUI tree: run 'runtime-apply <manifest>' first"; exit 3; fi
  if [ "$drc" = 4 ]; then                                               # a volume without a ComfyUI tree: make one
    local mrc=0; base_comfy_materialize || mrc=$?
    if [ "$mrc" = 4 ]; then note "--check stops here: the tree does not exist yet. The install creates it first; the rest of this report applies once it does."; exit 0; fi
    if [ "$mrc" != 0 ]; then err "could not materialise a ComfyUI tree on $VOL"; exit 3; fi
    if ! base_discover; then exit 3; fi
  fi
  if ! _base_validate_tables; then err "the package's declarations are invalid (see above)"; exit 2; fi
  if _base_runtime_on; then _base_runtime_init; fi               # 3.1.0: a buyer's machine installs from the proven runtime
  _base_library_link          # 2.4.0: models/ IS the shared library, before anything imports, downloads or renders
  _base_tmp
  base_banner || true            # in a || list, as it was in the `if` it used to sit in: its df probes must not fire the ERR trap
}
base_require_workflow(){ # the workflow beside the script must carry this script's version under extra.<WF_VERSION_KEY>
  [ -n "${WF_NAME:-}" ] || return 0
  local wf="$PKG_DIR/$WF_NAME" key="${WF_VERSION_KEY:-package_version}" have
  if [ ! -f "$wf" ]; then err "$WF_NAME is not beside the script in $PKG_DIR — the package is incomplete"; BASE_FAILED+=("workflow missing: $WF_NAME"); base_summary; fi
  have="$("$SYS_PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("extra", {}).get(sys.argv[2], ""))' "$wf" "$key" 2>/dev/null || echo "?")"
  if [ -z "$have" ] || [ "$have" = "?" ]; then err "$WF_NAME carries no extra.$key — the script cannot tell whether this workflow matches it"; BASE_FAILED+=("workflow has no extra.$key"); base_summary; fi
  if [ "$have" != "$PKG_VERSION" ]; then
    err "$WF_NAME is version $have but this script is $PKG_VERSION — they ship together; use the matching pair"
    BASE_FAILED+=("workflow $have != script $PKG_VERSION"); base_summary
  fi
  ok "workflow $WF_NAME (extra.$key = $have)"
}

base_install(){ # the install, in the order the spec fixes; hooks run where a package declares them
  base_init
  # 3.0.0: the OS and the NVIDIA driver at their newest FIRST: they are the machine's, not the store's, so this runs
  # before the store's lock, and before the driver gate so an upgrade can lift a driver below DRIVER_MIN. A new driver
  # stops the run here, before anything touches the GPU: the machine must restart first (a person approves it).
  local src=0 sysbg=0
  if _base_runtime_on; then _base_runtime_skip "the OS and the NVIDIA driver"; BASE_SYSTEM_RESULT="runtime (not upgraded)"
  elif _base_system_start; then sysbg=1          # 3.5.0: apt runs beside the network checks; joined before the venv
  else base_system || src=$?; fi
  if [ "$sysbg" = 0 ]; then
    if [ "$src" = 10 ]; then base_summary; fi
    if ! _base_driver_gate; then base_summary; fi
  fi
  # 2.5.0: on a SHARED workspace only, one installer at a time. Nothing above this line writes to the store.
  if ! _base_install_lock; then base_summary; fi
  base_require_workflow
  local nfail="${#BASE_FAILED[@]}"      # the OS stage may have recorded a failure that does not stop the run; tokens are judged alone
  base_tokens || true
  if [ "${#BASE_FAILED[@]}" -gt "$nfail" ]; then err "a token was rejected: stopping before anything downloads"; base_summary; fi
  if _base_runtime_on; then            # 3.1.0: the tree, the packs and the venv are the runtime's, checked, never moved
    if ! _base_runtime_comfy; then base_summary; fi
  else base_update_comfyui || true; fi
  if ! base_comfy_gate; then base_summary; fi
  base_consolidate || true
  base_packs git || true               # in runtime mode: located and recorded only; a pack the runtime lacks fails by name
  _base_own_packs_save || true         # 3.2.0: the live copy of each own pack about to change, kept before any hook writes over it
  if _base_runtime_on; then
    _base_hook pkg_pre_venv
    if ! _base_runtime_venv; then base_summary; fi
    _base_hook pkg_post_venv           # SageAttention accepts only the runtime's own build (lib/40-packs.sh)
    base_own_packs || true             # 3.2.0: the package's own packs come from its zip, never from the runtime
    _base_runtime_skip "the packs' requirements"; _base_runtime_skip "Comfy MCP"; BASE_MCP_RESULT="skipped (runtime)"
    _base_runtime_skip "the newest-everything pass"; BASE_LIFT_RESULT="runtime (not lifted)"
  else
  if [ "$sysbg" = 1 ]; then             # 3.5.0: the OS stage joins here, before anything touches the venv or the GPU
    src=0; _base_system_join || src=$?
    if [ "$src" = 10 ]; then base_summary; fi
    if ! _base_driver_gate; then base_summary; fi
  fi
  base_venv_snapshot                   # 3.0.0: the venv before this run changed any of it (the core rollback's source)
  _base_hook pkg_pre_venv
  if ! base_venv; then err "the venv step failed — stopping before anything else changes"; base_summary; fi
  base_cuda_toolchain || true          # before the hook that builds, so a broken toolkit is named (and fixed) up front
  _base_hook pkg_post_venv
  base_own_packs || true               # 3.2.0: mirrored before the resolve, so an own pack's requirements file is in it
  base_packs pip || true
  base_mcp || true                     # the venv and the packs exist; models do not yet, and MCP needs none
  base_venv_latest || true             # 3.0.0: everything still behind is forced to its newest; the import check judges it
  fi
  if [ "${BASE_SEED:-0}" = "1" ]; then
    # 2.1.0: the template image's seed build (dockerize.py): the toolchain and the packs, never a model, a server or a suite
    hdr "SEED · BASE_SEED=1: no models, no prune, no restart, no smoke, no suite — the image carries the toolchain, the volume gets the models at boot"
    base_import_check || true
    _base_own_packs_judge "${BASE_TMPD:-/nonexistent}/import_check.log" || true
    base_hygiene || true
    base_boot_install || true
    base_ledger_write || true
    _base_hook pkg_post_install
    base_summary
  fi
  _base_hook pkg_pre_models
  base_models || true
  base_prune || true
  base_sync || true
  _base_hook pkg_post_models
  base_import_check || true
  _base_own_packs_judge "${BASE_TMPD:-/nonexistent}/import_check.log" || true   # 3.2.0: an own pack that does not import is put back
  base_hygiene || true
  base_boot_install || true
  base_ledger_write || true
  base_restart || true
  base_smoke || true
  base_combos || true
  _base_later_launch || true           # 3.1.0: a staged install's later files download behind the server that just started
  # the live server is only a valid oracle when it runs the code this run installed
  if [ "$BASE_RESTART_NEEDED" != "1" ] && [ -z "${BASE_SERVER:-}" ] && curl -sf --max-time 3 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then BASE_SERVER="$HOSTPORT"; fi
  _base_run_suite || true             # 3.2.0: never in a dry run (lib/92-onepass.sh)
  _base_hook pkg_post_install
  base_summary
}

_base_use_testbed(){ # only from `test`: the shared ComfyUI testbed. Since 2.6.0 testbed.sh ships in this repository, so the
                     # tree is <base>/testbed; a brand repository keeps the base as a submodule and has base/testbed beside
                     # base/comfyui-base. Both are checked, in that order, and each root is taken whole — the port file is
                     # read from the SAME root that supplied the tree, never from the other one. Anchored on the base either
                     # way, so a package three levels down and the base itself find the same tree.
  local root tb port
  if [ -n "${BASE_NODE_SRC:-}" ]; then return 0; fi
  for root in "$BASE_DIR" "$BASE_DIR/.."; do
    tb="$root/testbed"
    if [ -f "$root/testbed.sh" ] && [ -f "$tb/main.py" ]; then
      BASE_NODE_SRC="$(cd "$tb" && pwd -P)"; note "testbed: BASE_NODE_SRC=$BASE_NODE_SRC"
      port="${TESTBED_PORT:-}"
      [ -z "$port" ] && [ -f "$root/.testbed-server.port" ] && port="$(tr -d '[:space:]' < "$root/.testbed-server.port")"   # 2.2.0: the running testbed says which port it took
      port="${port:-8199}"
      if [ -z "${BASE_SERVER:-}" ] && curl -sf --max-time 2 "http://127.0.0.1:$port/system_stats" >/dev/null 2>&1; then
        BASE_SERVER="127.0.0.1:$port"; note "testbed: BASE_SERVER=$BASE_SERVER"
      fi
      break
    fi
  done
  return 0
}
base_test(){ # [tier] — the package's suite.py beside the script (the base's own suite/ when PKG_ID=base); sets BASE_TEST_RESULT / BASE_TEST_RC
  local tier="${1:-}" suite ini runner=() out rc=0 on_pod=0 tiers cargs=()
  hdr "TEST SUITE${tier:+ · tier $tier}"
  BASE_TEST_RC=0
  if [ "${BASE_INNER:-}" = "1" ]; then note "skipped — this is a fake run started by a suite"; BASE_TEST_RESULT="skipped (inner)"; return 0; fi
  if [ "${BASE_NO_SUITE:-0}" = "1" ]; then note "skipped — BASE_NO_SUITE=1 (a baked image's first boot: the suite ran when the image was built)"; BASE_TEST_RESULT="skipped (BASE_NO_SUITE)"; return 0; fi
  if [ "${PKG_ID:-base}" = "base" ]; then suite="$BASE_DIR/suite"; ini="$BASE_DIR/pytest.ini"; else suite="$PKG_DIR/suite.py"; ini="$PKG_DIR/pytest.ini"; fi
  if [ ! -e "$suite" ]; then
    if [ "${PKG_NO_SUITE:-0}" = "1" ]; then note "this package ships no suite (PKG_NO_SUITE=1)"; BASE_TEST_RESULT="none"; return 0; fi
    miss "no suite at $suite — the suite did NOT run"; BASE_TEST_RESULT="NOT RUN (no suite.py)"; BASE_WARN+=("suite not run: no suite.py beside the script"); BASE_TEST_RC=1; return 0
  fi
  [ -f "$ini" ] && cargs=(-c "$ini")
  if [ -n "$tier" ]; then
    tiers="$( [ -f "$ini" ] && awk '/^markers/{f=1;next} f&&/^[^[:space:]]/{f=0} f{sub(/:.*/,""); print $1}' "$ini" || true)"
    if [ -n "$tiers" ] && ! _base_in_list "$tier" $tiers; then err "unknown tier '$tier' — this suite has: $(echo $tiers)"; BASE_TEST_RESULT="unknown tier"; BASE_TEST_RC=2; return 0; fi
  fi
  # the venv's pytest, else uv with its own; never pip-installed into an interpreter that is not our venv;
  # and never "skipped" with exit 0 when nothing ran
  local tbpy=""
  if [ -x "$BASE_HOME/bin/uv" ]; then export PATH="$BASE_HOME/bin:$PATH"; hash -r; fi   # the base's own uv on the volume (3.0.0: $BASE_HOME/bin): the install path had it on PATH, `test` on its own did not (2.0.5)
  if [ -n "${BASE_NODE_SRC:-}" ]; then for tbpy in "$BASE_NODE_SRC"/.venv*/bin/python; do [ -x "$tbpy" ] && break || tbpy=""; done; fi
  if [ -x "${PY:-}" ] && "$PY" -c "import pytest" 2>/dev/null; then runner=("$PY" -m pytest)
  elif [ -x "${PY:-}" ] && [ -z "$BASE_FAKE_ROOT" ] && _base_venv_pytest; then runner=("$PY" -m pytest)       # 2.0.10: into OUR venv, not a fallback interpreter that cannot see the packs
  elif [ -n "$tbpy" ] && "$tbpy" -c "import pytest" 2>/dev/null; then note "running the suite with the testbed venv ($tbpy) — it can import ComfyUI"; runner=("$tbpy" -m pytest)
  elif command -v uv >/dev/null 2>&1; then note "pytest not importable by ${PY:-the venv}: running the suite through uv, on the newest Python it has"; runner=(uv run --quiet --no-project --python "$(env $(env | sed -n 's/^\(BASE_[A-Za-z0-9_]*\)=.*/-u \1/p') uv python list --only-installed 2>/dev/null | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV | head -1 || true)" --with pytest python -m pytest)   # the lookup sees no BASE_* either
  else miss "the suite did NOT run: no pytest in the venv and no uv (not on PATH, not in $BASE_HOME/bin)"; BASE_TEST_RESULT="NOT RUN (no pytest, no uv)"; BASE_WARN+=("suite not run: no pytest and no uv"); BASE_TEST_RC=1; return 0; fi
  if [ -z "$BASE_FAKE_ROOT" ] && _base_on_pod && [ -d "${CN:-/nonexistent}" ]; then on_pod=1; fi
  _base_tmp; out="$BASE_TMPD/pytest.out"
  # The suite gets the documented hand-over (BASE_WF … BASE_ON_POD, set below) and NO other BASE_* the run exported: the
  # rehearsal's fake step one leaked BASE_NO_NET / BASE_FAKE_ROOT into tests that build their own fake pods (2.0.16).
  # 3.6.0: BASE_WORKSPACE passes through when set: it is the consumer root py/pkgdirs.py walks in the workspace shape.
  local _scrub=() _v
  for _v in $(env | sed -n 's/^\(BASE_[A-Za-z0-9_]*\)=.*/\1/p' | sort -u); do
    case "$_v" in BASE_WF|BASE_SCRIPT|BASE_PKG_DIR|BASE_LIB|BASE_COMFY|BASE_VENV|BASE_STATE|BASE_MODELS_DIR|BASE_NODE_SRC|BASE_SERVER|BASE_ON_POD|BASE_WORKSPACE) ;; *) _scrub+=(-u "$_v");; esac
  done
  local _errexit=0; case $- in *e*) _errexit=1;; esac; set +e      # pytest's OWN status, whatever the shell's options (2.0.14: tee's 0 once read as green)
  PYTHONPATH="$BASE_DIR/py${PYTHONPATH:+:$PYTHONPATH}" BASE_WF="${PKG_DIR:-}/${WF_NAME:-}" BASE_SCRIPT="${PKG_SCRIPT:-}" BASE_PKG_DIR="${PKG_DIR:-}" BASE_LIB="$BASE_DIR" \
     BASE_COMFY="${COMFY:-}" BASE_VENV="${VENV:-}" BASE_STATE="${BASE_STATE:-}" BASE_MODELS_DIR="${M:-}" BASE_NODE_SRC="${BASE_NODE_SRC:-}" BASE_SERVER="${BASE_SERVER:-}" BASE_ON_POD="$on_pod" \
     env ${_scrub[@]+"${_scrub[@]}"} -u HF_TOKEN -u HF_HUB_TOKEN -u HUGGING_FACE_HUB_TOKEN -u HUGGINGFACE_API_KEY \
     "${runner[@]}" -q -rs -p no:cacheprovider -p basetest ${cargs[@]+"${cargs[@]}"} "$suite" ${tier:+-m "$tier"} 2>&1 | tee "$out"; rc="${PIPESTATUS[0]}"
  [ "$_errexit" = 1 ] && set -e; true
  BASE_TEST_TABLE="$(awk '/= test tiers =/{f=1;next} f&&/^=+/{exit} f' "$out" || true)"
  BASE_TEST_RESULT="$(grep -E '^[0-9]+ (passed|failed)|no tests ran|error' "$out" | tail -1 || true)"; BASE_TEST_RESULT="${BASE_TEST_RESULT:-exit $rc}"
  if [ "$rc" -ne 0 ]; then miss "suite: $BASE_TEST_RESULT (exit $rc)"; BASE_FAILED+=("suite: $BASE_TEST_RESULT"); else ok "suite: $BASE_TEST_RESULT"; fi   # a red suite is a failed run (2.0.15: it had been a warning, STEP RC=0)
  BASE_TEST_RC=$rc
  return 0
}

_base_pkg_command(){ # <name> [args] — a package's extra subcommand (PKG_COMMANDS rows: name|function|help)
  local want="$1" row name fn help; shift
  for row in ${PKG_COMMANDS[@]+"${PKG_COMMANDS[@]}"}; do
    IFS='|' read -r name fn help <<< "$row"
    if [ "$name" = "$want" ] && declare -F "$fn" >/dev/null; then
      base_env_setup; base_discover quiet || true; _base_tmp
      "$fn" "$@"; exit $?
    fi
  done
  return 1
}
_base_usage(){ echo "usage: bash \"$(basename "${PKG_SCRIPT:-$0}")\" [--check | preflight | --latest | test [tier] | rescue | help$(_base_pkg_command_names)]" >&2; }
_base_pkg_command_names(){ local row; for row in ${PKG_COMMANDS[@]+"${PKG_COMMANDS[@]}"}; do printf ' | %s' "${row%%|*}"; done; }
base_help(){
  local s; s="$(basename "${PKG_SCRIPT:-$0}")"
  local npacks nmodels; npacks="$(( ${#BASE_PACKS[@]} + ${#PACKS[@]} ))"; nmodels="${#MODELS[@]}"
  cat <<HLP
${PKG_NAME:-ComfyUI Base} V${PKG_VERSION:-$BASE_VERSION}  ·  runs on ComfyUI Base $BASE_VERSION

  bash "$s"                ONE COMMAND: toolchain via the base, $npacks node pack(s), $nmodels model row(s), workflow paths,
                             import check, hygiene, then the exact launch line. Prompts only for tokens and deletion.
  bash "$s" --check        dry run: the same report, nothing changed
  bash "$s" preflight      seconds, nothing changed: only what would stop the install before it changes anything
  bash "$s" --latest       the same as a plain run (3.0.0: every run takes everything to its newest)
  bash "$s" test [tier]    run the package's suite (tiers: its pytest.ini markers); auto-detects the repo testbed
  bash "$s" rescue         repair a venv that cannot boot after a pod restart (never runs the interpreter it repairs)
  bash "$s" help
HLP
  local row name fn help
  for row in ${PKG_COMMANDS[@]+"${PKG_COMMANDS[@]}"}; do IFS='|' read -r name fn help <<< "$row"; printf '  bash "%s" %-14s %s\n' "$s" "$name" "$help"; done
  cat <<HLP

  env: BASE_RESTART=1 (actually restart ComfyUI; default prints the launch line) · BASE_YES=1 (unattended: yes to deletion)
       HF_TOKEN (else asked once, stored 0600) · COMFY_DIR (only if discovery fails) · BASE_NODE_SRC / BASE_SERVER (tests)
  base: <volume>/comfy-base — state in state/, ledger in state/packages/, logs in state/logs/. \`bash base.sh status\` lists every package.
HLP
}

_base_list(){ local x; for x in "$@"; do echo "      • $x"; done; }
base_summary(){ # one screen; exits 0, or 1 when anything is in BASE_FAILED. Always the last step.
  local tag=""; [ "$BASE_DRY" = "1" ] && tag=" (dry run — nothing was changed)"
  hdr "SUMMARY · ${PKG_NAME:-ComfyUI Base} V${PKG_VERSION:-$BASE_VERSION} · base $BASE_VERSION$tag"
  echo "  venv       ${VENV:-?} → $BASE_VENV_RESULT"
  if [ "$BASE_TORCH_INFO" != "?" ]; then echo "             $BASE_TORCH_INFO"; fi
  if [ -n "$BASE_VENV_BACKUP" ]; then echo "             previous venv kept at $BASE_VENV_BACKUP — the rollback for this one. Once the pod has been STOPPED, STARTED and rendered:  rm -rf '$BASE_VENV_BACKUP'"; fi
  case "$BASE_VENV_RESULT" in built|rebuilt)
    local m hooks script pkg
    for m in "${BASE_STATE:-/nonexistent}"/packages/*.manifest; do
      [ -f "$m" ] || continue
      hooks="$(sed -n '2p' "$m" | tr '\t' '\n' | sed -n 's/^hooks=//p')"; script="$(sed -n '2p' "$m" | tr '\t' '\n' | sed -n 's/^script=//p')"; pkg="$(basename "$m" .manifest)"
      case ",$hooks," in *,pkg_post_venv,*) [ "$pkg" != "${PKG_ID:-base}" ] && echo -e "${YEL}             the venv was rebuilt: $pkg has a post-venv hook whose work is gone — re-run:  bash \"$script\"${NC}" || true;; esac
    done;;
  esac
  echo "  ComfyUI    $COMFY_OLD → $COMFY_NEW   (${COMFY_REF_INFO:+$COMFY_REF_INFO · }${COMFY:-?})"
  _base_versions_block
  if [ -n "${BASE_LIBRARY:-}" ]; then echo "  library    shared: $BASE_LIBRARY (models, output, input): every machine on this volume sees the same files"; fi
  echo "  packs      present ${#PACK_PRESENT[@]} · updated ${#PACK_UPDATED[@]} · cloned ${#PACK_CLONED[@]} · local edits ${#PACK_DIRTY[@]}"
  [ "${#PACK_UPDATED[@]}" -gt 0 ] && _base_list "${PACK_UPDATED[@]}" || true
  [ "${#PACK_CLONED[@]}" -gt 0 ] && _base_list "${PACK_CLONED[@]}" || true
  echo "  models     ok ${#MODEL_OK[@]} (${MODEL_OK_GB} GB) · moved ${#MODEL_MOVED[@]} (${MODEL_MOVED_GB} GB) · downloaded ${#MODEL_DL[@]} (${MODEL_DL_GB} GB) · partial re-fetched ${#MODEL_PARTIAL[@]} · failed ${#MODEL_FAIL[@]} (${MODEL_FAIL_GB} GB)"
  if [ "${#MODEL_STANDIN[@]}" -gt 0 ]; then echo "  later      ${#MODEL_STANDIN[@]} file(s) stand in until they arrive${LATER_PENDING:+: $LATER_PENDING downloading behind the server} (state/progress.json)"; fi
  if _base_runtime_on; then echo "  runtime    ${BASE_RUNTIME_NOTE:-$BASE_RUNTIME} (no OS, git, pip or uv step ran)"; fi
  # the average is the number that says whether Xet actually engaged: plain HTTPS lands an order of magnitude lower
  if [ "${BASE_DL_TIME:-0}" -gt 0 ]; then echo "             average $(_base_rate "$BASE_DL_BYTES" "$BASE_DL_TIME") across ${#MODEL_DL[@]} file(s)"; fi
  [ "${#MODEL_MOVED[@]}" -gt 0 ] && _base_list "${MODEL_MOVED[@]}" || true
  [ "${#MODEL_DL[@]}" -gt 0 ] && _base_list "${MODEL_DL[@]/#/downloaded: }" || true
  if [ "${#MODEL_FAIL[@]}" -gt 0 ]; then echo -e "${RED}             MISSING:${NC}"; _base_list "${MODEL_FAIL[@]}"; fi
  [ "${#UNKNOWN_FILES[@]}" -gt 0 ] && echo "             unknown files left alone in old-layout folders: ${#UNKNOWN_FILES[@]}" || true
  if [ "${#UNCLAIMED_FILES[@]}" -gt 0 ]; then echo "             unclaimed ${#UNCLAIMED_FILES[@]} — model files no package declares (an image's own, or yours), left where they are:"; _base_list "${UNCLAIMED_FILES[@]}"; fi
  [ "${#DEL_FILES[@]}" -gt 0 ] && echo "             $( [ "$BASE_DRY" = "1" ] && echo "reclaimable" || echo "kept (deletion declined)"): ${#DEL_FILES[@]} file(s), $(_base_bytes_to_gb "$DEL_BYTES") GB" || true
  echo "  workflow   $BASE_SYNC_RESULT"
  echo "  hygiene    ${#BASE_CHANGED[@]} change(s)"; [ "${#BASE_CHANGED[@]}" -gt 0 ] && _base_list "${BASE_CHANGED[@]}" || true
  echo "  boot       ${BASE_BOOT_RESULT:-not installed}"
  echo "  import     $BASE_IMPORT_RESULT"
  echo "  restart    $BASE_RESTART_RESULT"
  echo "  smoke      $BASE_SMOKE_RESULT"
  echo "  combos     $BASE_COMBO_RESULT"
  echo "  suite      $BASE_TEST_RESULT"; [ -n "${BASE_TEST_TABLE:-}" ] && echo "$BASE_TEST_TABLE" | sed 's/^/           /' || true
  _base_timings_block; _base_timings_write      # 3.2.0: where this run spent its time (the SUMMARY header closed the last stage)
  echo "  log        ${BASE_LOG:-—}"
  [ -n "$BASE_STASH_CMD" ] && echo -e "${YEL}  stash      local ComfyUI edits were stashed — restore with: $BASE_STASH_CMD${NC}" || true
  if [ "${#BASE_WARN[@]}" -gt 0 ]; then echo -e "${YEL}  warnings   ${#BASE_WARN[@]}${NC}"; _base_list "${BASE_WARN[@]}"; fi
  if [ "${#BASE_FAILED[@]}" -gt 0 ]; then echo -e "${RED}  FAILED     ${#BASE_FAILED[@]} — this run is not complete:${NC}"; _base_list "${BASE_FAILED[@]}"; fi
  if [ "${#BASE_FAILED[@]}" -eq 0 ] && [ "$BASE_RESTART_NEEDED" = "1" ] && [ "$BASE_DRY" != "1" ]; then
    echo; echo -e "${GREEN}  ══ EVERYTHING IS INSTALLED — ONE STEP LEFT ══${NC}"
    echo "  Start (or restart) ComfyUI so it sees this run. Nothing has been stopped for you:"
    echo; echo "      cd $COMFY && nohup $(_base_start_cmd) >> $COMFY_LOG 2>&1 &"; echo
    echo "  Then: bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
  fi
  if declare -F pkg_summary >/dev/null; then pkg_summary || true; fi
  BASE_SUMMARY_DONE=1
  # 3.1.0: the verdict, kept, so `runtime-capture` can refuse a machine whose last install was not green
  if [ "$BASE_DRY" != "1" ] && [ -n "${BASE_STATE:-}" ] && [ -n "${PKG_ID:-}" ]; then
    mkdir -p "$BASE_STATE/last-run" 2>/dev/null && { if [ "${#BASE_FAILED[@]}" -eq 0 ]; then echo "green $(_base_ts) $PKG_VERSION"; else echo "red $(_base_ts) $PKG_VERSION"; fi > "$BASE_STATE/last-run/$PKG_ID"; } 2>/dev/null || true
  fi
  # exit here, explicitly: a failing last command would trip the ERR trap and turn the contractual exit 1 into 4
  if [ "${#BASE_FAILED[@]}" -eq 0 ]; then exit 0; fi
  exit 1
}

_base_versions_block(){ # 3.0.0: what this install put in place, newest everything, said once in one place
  echo "  VERSIONS   base $BASE_VERSION · ComfyUI ${COMFY_NEW:-?}${COMFY_REF_INFO:+ ($COMFY_REF_INFO)}"
  local py tinfo uvv
  py="$("$PY" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || true)"
  tinfo="$("$PY" -c 'import torch; print("torch %s" % torch.__version__)' 2>/dev/null || true)"
  uvv="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
  echo "             Python ${py:-?} · ${tinfo:-torch ?}${BASE_TORCH_BACKEND:+ ($BASE_TORCH_BACKEND)} · uv ${uvv:-?}"
  [ -n "${BASE_FRONTEND_INFO:-}" ] && echo "             $BASE_FRONTEND_INFO" || true
  echo "             driver ${BASE_DRIVER_AFTER:-${DRIVER:-?}}${BASE_KERNEL_INFO:+ · kernel $BASE_KERNEL_INFO} · system: ${BASE_SYSTEM_RESULT:-not run}   (this machine's)"
  [ "${#BASE_SYSTEM_ROWS[@]}" -gt 0 ] && _base_list "${BASE_SYSTEM_ROWS[@]}" || true
  echo "             newest: ${BASE_LIFT_RESULT:-not run}"
  [ "${#BASE_LIFT_ROWS[@]}" -gt 0 ] && _base_list "${BASE_LIFT_ROWS[@]}" || true
  if [ "${#BASE_UPSTREAM[@]}" -gt 0 ]; then echo -e "${YEL}  UPSTREAM   ${#BASE_UPSTREAM[@]}: needs upgrading upstream (named, not blocking):${NC}"; _base_list "${BASE_UPSTREAM[@]}"; fi
  _base_machines_behind
  if [ "$BASE_DRY" != "1" ] && [ -d "${BASE_STATE:-/nonexistent}" ]; then   # the ledger's copy: what the store holds now
    { echo "base=$BASE_VERSION"; echo "comfyui=${COMFY_NEW:-?}"; echo "comfyui_ref=${COMFY_REF_INFO:-}"; echo "python=${py:-}"
      echo "torch=${tinfo#torch }"; echo "backend=${BASE_TORCH_BACKEND:-}"; echo "frontend=${BASE_FRONTEND_INFO:-}"; echo "uv=${uvv:-}"
      echo "by=${PKG_ID:-base}"; echo "host=$(hostname -s 2>/dev/null)"; echo "ts=$(_base_ts)"; } > "$BASE_STATE/versions.env" 2>/dev/null || true
  fi
  if [ "${BASE_VOLUME_SHARED:-0}" = "1" ]; then
    echo -e "${YEL}  STORE      shared: the venv, ComfyUI and the packs above changed for EVERY machine on this store. Code already${NC}"
    echo -e "${YEL}             loaded keeps running until its restart; anything imported later sees the new files.${NC}"
  fi
}
_base_machines_behind(){ # the store's per-machine records (lib/15-system.sh): machines whose driver cannot run this venv's CUDA major
  local d="${BASE_STATE:-/nonexistent}/machines" f h cuda vmaj="" me
  [ -d "$d" ] || return 0
  case "${BASE_TORCH_BACKEND:-}" in cu*) vmaj="${BASE_TORCH_BACKEND#cu}"; vmaj="${vmaj%?}";; *) return 0;; esac
  me="$(hostname -s 2>/dev/null || echo machine)"
  for f in "$d"/*.env; do
    [ -f "$f" ] || continue
    h="$(sed -n 's/^host=//p' "$f")"; cuda="$(sed -n 's/^cuda=//p' "$f")"
    [ "$h" = "$me" ] && continue
    if [ -n "$cuda" ] && [ "${cuda%%.*}" -lt "$vmaj" ] 2>/dev/null; then
      echo -e "${YEL}  MACHINES   $h runs a driver for CUDA $cuda; this venv needs CUDA $vmaj: run its install (driver upgrade, an approved restart) before it renders${NC}"
      BASE_WARN+=("machine $h must run its own install before it renders (driver CUDA $cuda < the venv's CUDA $vmaj)")
    fi
  done
}
base_main(){ # the six forms every package shares, plus the package's own PKG_COMMANDS
  if [ -z "${PKG_SCRIPT:-}" ] && [ -f "$0" ]; then PKG_SCRIPT="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"; fi
  PKG_SCRIPT="${PKG_SCRIPT:-}"
  if [ -z "${PKG_DIR:-}" ]; then if [ -n "$PKG_SCRIPT" ]; then PKG_DIR="$(dirname "$PKG_SCRIPT")"; else PKG_DIR="$PWD"; fi; fi
  if [ "${BASE_DECLARE_ONLY:-0}" = "1" ]; then _base_declare_dump; return $?; fi
  _base_install_traps
  case "${1:-}" in
    "")        base_install ;;
    --check)   BASE_DRY=1; base_install ;;
    preflight) base_preflight ;;        # 3.2.0: podctl's pre-pass, in seconds: only what would stop the real run first
    --latest)  base_install ;;             # 3.0.0: every run takes everything to its newest; kept so older callers still work
    test)      shift; base_env_setup; base_discover quiet || true; _base_use_testbed; base_test "${1:-}"; exit "$BASE_TEST_RC" ;;
    rescue)    base_env_setup; _base_logging; if ! base_discover; then exit 3; fi; base_rescue; exit "${BASE_RESCUE_RC:-0}" ;;
    help|-h|--help) base_help ;;
    runtime-apply)   shift; base_runtime_apply "$@"; exit $? ;;                # 3.1.0: a buyer's machine
    runtime-capture) shift; base_runtime_capture "$@"; exit $? ;;
    fetch-later)     base_fetch_later; exit $? ;;
    *)         if ! _base_pkg_command "$@"; then _base_usage; exit 2; fi ;;
  esac
}
base_cli(){ # the developer's entry (`bash base.sh test …`): a package script sets pipefail before sourcing the base; this entry must too (2.0.14: tee's 0 once read as green)
  set -o pipefail # bash base.sh <command>: the base acting for itself
  PKG_ID="${PKG_ID:-base}"; PKG_NAME="${PKG_NAME:-ComfyUI Base}"; PKG_VERSION="${PKG_VERSION:-$BASE_VERSION}"; BASE_MIN="${BASE_MIN:-$BASE_VERSION}"
  WF_NAME="${WF_NAME:-}"; COMFY_MIN="${COMFY_MIN:-0.34.0}"; PACKS=(${PACKS[@]+"${PACKS[@]}"}); MODELS=(${MODELS[@]+"${MODELS[@]}"})
  PKG_DIR="${PKG_DIR:-$BASE_DIR}"; PKG_SCRIPT="${PKG_SCRIPT:-$BASE_DIR/base.sh}"
  case "${1:-}" in
    version)      echo "$BASE_VERSION" ;;
    list-packs)   shift; base_list_packs "$@" ;;
    gen-models)   shift; base_env_setup; base_gen_models "$@" ;;
    gen-hashes)   shift; base_env_setup; base_gen_hashes "$@" ;;                                 # 3.4.0: models.sha256, from the Hub
    stamp-models) shift; "$SYS_PY" "$BASE_DIR/py/stamp_models.py" "$@" ;;                    # 2.1.0: the workflow's own `models` array, from the rows
    status)       base_env_setup; base_status ;;
    latest)       base_env_setup; base_latest ;;
    install-self) shift; base_install_self "$@" ;;
    runtime-apply)   shift; trap _base_on_exit EXIT; base_runtime_apply "$@" ;;     # 3.1.0: a buyer's machine
    runtime-capture) shift; trap _base_on_exit EXIT; base_runtime_capture "$@" ;;
    fetch-later)     trap _base_on_exit EXIT; base_fetch_later ;;
    test|rescue|help|-h|--help) base_main "$@" ;;
    *) echo "base.sh: unknown command '${1:-}' (version | install-self | status | latest | list-packs <dir>... | gen-models <dir> | gen-hashes <dir> [--check] | stamp-models <dir> [--check] | runtime-apply <manifest> | runtime-capture <dir> | fetch-later | test [tier] | rescue | help)" >&2; exit 2 ;;
  esac
}
base_install_self(){ # copy this base to $BASE_HOME (the volume) unless it already runs from there; then re-exec the installed copy
  [ "${BASE_DECLARE_ONLY:-0}" = "1" ] && return 0
  base_env_setup
  local dest="$BASE_HOME" here="$BASE_DIR" have="" f
  if [ "$dest" = "$here" ]; then return 0; fi                                     # off-pod with no volume: nothing to install to
  mkdir -p "$dest" 2>/dev/null || { err "cannot create $dest"; exit 3; }
  if [ "$(cd "$dest" && pwd -P)" = "$(cd "$here" && pwd -P)" ]; then return 0; fi   # already the installed copy
  [ -f "$dest/VERSION" ] && have="$(tr -d '[:space:]' < "$dest/VERSION")"
  # 3.0.0: always the newest; an older base never replaces a newer one (BASE_FORCE_SELF is retired)
  if [ -n "$have" ] && ! _base_vge "$BASE_VERSION" "$have"; then
    err "a newer base ($have) is already installed at $dest; this copy is $BASE_VERSION: use the newer one"; exit 3
  fi
  if [ "$have" = "$BASE_VERSION" ] && [ -f "$dest/MANIFEST.sha256" ] && cmp -s "$here/MANIFEST.sha256" "$dest/MANIFEST.sha256" \
     && (cd "$dest" && _base_sha256 -c MANIFEST.sha256 >/dev/null 2>&1); then   # 2.0.30: same version AND the same manifest — an amended zip under one version refreshes
    note "base $BASE_VERSION already installed at $dest (same manifest)"
  else
    hdr "INSTALL BASE $BASE_VERSION → $dest"
    for f in base.sh lib py suite hosts pytest.ini VERSION MANIFEST.sha256 "comfyui-base-script.sh" "comfyui-base-handbook.md" LICENSE; do
      [ -e "$here/$f" ] || continue
      rm -rf "$dest/$f"; cp -R "$here/$f" "$dest/$f"
    done                                                                          # state/, python/, dead-venvs/ are never touched
    chmod 700 "$dest/base.sh" "$dest/comfyui-base-script.sh" 2>/dev/null || true
    ok "installed ${have:+(replacing $have) }→ $dest"
  fi
  # the boot the pod runs ($dest/boot.sh, what podctl's wrapper execs) follows the installed base on EVERY command, not
  # only the install flow that first wrote it (2.0.7: `test` upgraded the library and left a three-versions-old boot.sh)
  if [ -f "$dest/boot.sh" ] && ! cmp -s "$here/lib/boot.sh" "$dest/boot.sh"; then
    cp "$here/lib/boot.sh" "$dest/boot.sh" && chmod 700 "$dest/boot.sh" && ok "refreshed $dest/boot.sh (the boot follows the installed base)"
  fi
  if [ "${BASE_REEXEC:-0}" != "1" ]; then BASE_REEXEC=1 exec bash "$dest/comfyui-base-script.sh" "$@"; fi
}
