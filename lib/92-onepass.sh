# 92-onepass.sh (3.2.0): an install does each piece of work once, and says what each stage cost.
# The stage clock itself is hdr's (lib/00-env.sh); this file turns it into the summary's block and the store's record.

_base_timings_block(){ # BASE_STAGE_TIMES → "time N s in K stages" then one line per stage that took a second or more, slowest first
  local total=0 n=0 row secs
  for row in ${BASE_STAGE_TIMES[@]+"${BASE_STAGE_TIMES[@]}"}; do total=$(( total + ${row%%|*} )); n=$(( n + 1 )); done
  echo "  time       $total s in $n stages, slowest first:"
  for row in ${BASE_STAGE_TIMES[@]+"${BASE_STAGE_TIMES[@]}"}; do echo "$row"; done | sort -t'|' -k1,1nr | while IFS='|' read -r secs row; do
    if [ "$secs" -gt 0 ]; then printf '             %4d s  %s\n' "$secs" "$row"; fi
  done
  return 0
}
_base_run_suite(){ # the pipeline's suite stage: a dry run skips it (it would test the server the real run is about to replace)
  if [ "$BASE_DRY" = "1" ]; then hdr "TEST SUITE"; note "skipped in --check: the real run tests the server it restarts"; BASE_TEST_RESULT="skipped (--check)"; BASE_TEST_RC=0; return 0; fi
  base_test ""
}
base_preflight(){ # `<script> preflight`: what podctl runs before an install in place of a whole --check pipeline.
  # It stops on exactly what would stop the real run before that run changes anything: invalid declarations or a
  # BASE_MIN this base does not meet (base_init, exit 2 or 3), no volume (exit 3), a workflow whose version is not
  # the script's (exit 1). The driver and the tokens are reported, never judged: the real run upgrades the driver
  # before its gate, and no token is required. No suite, no uv, no filesystem walk: those are the real run's.
  BASE_DRY=1
  base_init
  hdr "PREFLIGHT · what would stop the install before it changes anything"
  base_require_workflow
  note "driver ${DRIVER:-?} (the install upgrades it first, then gates at ${DRIVER_MIN:-580})"
  local row name
  for row in ${TOKENS[@]+"${TOKENS[@]}"}; do
    name="${row%%|*}"; [ -n "$name" ] || continue
    if [ -n "${!name:-}" ] || grep -qs "^$name=" "$BASE_STATE/tokens.env"; then ok "$name present"; else note "no $name yet (the install asks for it, or continues without it)"; fi
  done
  if [ "${#BASE_FAILED[@]}" -gt 0 ]; then err "the install would stop: ${BASE_FAILED[*]}"; exit 1; fi
  ok "nothing stops the install"
  BASE_SUMMARY_DONE=1
  exit 0
}
_base_timings_write(){ # the run's stage times beside its log: state/logs/<pkg>_<ts>.timings.tsv (seconds TAB stage), which
  # podctl fetches back to the Mac with the console after every install; never in a dry run
  [ "$BASE_DRY" = "1" ] && return 0
  [ -n "${BASE_STATE:-}" ] && [ "${#BASE_STAGE_TIMES[@]}" -gt 0 ] || return 0
  local d="$BASE_STATE/logs" row
  mkdir -p "$d" 2>/dev/null || return 0
  for row in "${BASE_STAGE_TIMES[@]}"; do printf '%s\t%s\n' "${row%%|*}" "${row#*|}"; done > "$d/${PKG_ID:-base}_$(_base_ts).timings.tsv" 2>/dev/null || true
  return 0
}

# ---------------------------------------------------------------- 3.11.0: a volume that already holds this exact install
# Paul (2026-10-16): "make it so pods are installed as fast as possible"; a later pod on a volume must be usable in under
# three minutes. When this package's last run on this store was GREEN, for this package version, on this base version and
# on a GPU of the same arch, and every file it declares is still in place, the install has nothing to do: the run starts
# ComfyUI if nothing is serving (BASE_RESTART=1) and stops there, in seconds. Like BASE_RUNTIME (§0.3), it is a named
# exception to "every run takes everything to its newest": BASE_FULL=1 runs the whole pipeline, and so does any change of
# package, base or GPU arch, a missing file, a dry run, a seed build or a buyer's machine.
_base_green_fp(){ # → "pkgfp=<sha> basefp=<sha>": every file the package folder ships (its script, workflow, own packs) and the base's
  # manifest, so a re-upload under the same version (a development round) is never mistaken for what is installed
  local p b
  p="$(cd "${PKG_DIR:-/nonexistent}" 2>/dev/null && find . -type f ! -path '*/.git/*' ! -path '*/__pycache__/*' ! -name '*.pyc' ! -path './_build/*' 2>/dev/null \
       | LC_ALL=C sort | while IFS= read -r x; do _base_sha256 "$x"; done | _base_sha256 | cut -c1-16)" || true
  b="$(_base_sha256 < "$BASE_DIR/MANIFEST.sha256" 2>/dev/null | cut -c1-16)" || true
  echo "pkgfp=${p:-?} basefp=${b:-?}"
}
_base_already_green(){ # → 0 when this exact install is already in place on this store (the reason it is not: BASE_GREEN_WHY)
  BASE_GREEN_WHY=""
  if [ "$BASE_DRY" = "1" ] || [ "${BASE_FULL:-0}" = "1" ] || [ "${BASE_SEED:-0}" = "1" ] || _base_runtime_on; then BASE_GREEN_WHY="a full run was asked for"; return 1; fi
  local f="${BASE_STATE:-/nonexistent}/last-run/${PKG_ID:-}" w ts ver rest base="" sm="" fp="" x cat fam purp file url bytes mnote alts rel dest
  if [ ! -f "$f" ]; then BASE_GREEN_WHY="no earlier run of ${PKG_ID:-this package} on this store"; return 1; fi
  read -r w ts ver rest < "$f" || true
  for x in $rest; do case "$x" in base=*) base="${x#base=}";; sm=*) sm="${x#sm=}";; pkgfp=*|basefp=*) fp="$fp${fp:+ }$x";; esac; done
  if [ "$w" != green ]; then BASE_GREEN_WHY="the last run here was not green"; return 1; fi
  if [ "$ver" != "$PKG_VERSION" ]; then BASE_GREEN_WHY="the last green run here installed ${ver:-?}, this is $PKG_VERSION"; return 1; fi
  if [ "$base" != "$BASE_VERSION" ]; then BASE_GREEN_WHY="the last green run here was on base ${base:-before 3.11.0}, this is $BASE_VERSION"; return 1; fi
  if [ "$fp" != "$(_base_green_fp)" ]; then BASE_GREEN_WHY="the package's files or the base's differ from the last green run's"; return 1; fi
  if [ -n "${BASE_GPU_SM:-}" ] && [ "$sm" != "$BASE_GPU_SM" ]; then BASE_GREEN_WHY="the last green run here was on ${sm:-another GPU}, this GPU is $BASE_GPU_SM"; return 1; fi
  if [ ! -x "${PY:-/nonexistent}" ] || [ ! -f "$BASE_STATE/boot.env" ] || [ ! -f "$BASE_STATE/packages/$PKG_ID.manifest" ]; then BASE_GREEN_WHY="the venv, boot.env or this package's ledger entry is missing"; return 1; fi
  while IFS='|' read -r cat fam purp file url bytes mnote alts; do
    [ -n "$cat" ] || continue
    if [ "${BASE_STAGED:-0}" = "1" ] && _base_is_later "$file"; then continue; fi
    rel="$(_base_dest_rel "$cat|$fam|$purp|$file|$url|$bytes|$mnote|$alts")"; dest="$(_base_dest_path "$rel")"
    if [[ "$file" == */ ]]; then [ -d "$dest" ] && continue
    elif [ -f "$dest" ] && _base_size_ok "$(_base_fsize "$dest")" "$bytes"; then continue; fi
    BASE_GREEN_WHY="$rel is no longer in place"; return 1
  done <<< "$(_base_model_rows 2>/dev/null || true)"
  return 0
}
_base_green_run(){ # the whole run when _base_already_green: the workflow checked, ComfyUI started if nothing serves, a short summary, exit 0
  local f="$BASE_STATE/last-run/$PKG_ID"
  hdr "ALREADY INSTALLED · ${PKG_NAME:-this package} V$PKG_VERSION on base $BASE_VERSION, green here since $(awk '{print $2}' "$f" 2>/dev/null)"
  note "nothing to install: same package, same base, same GPU arch, every declared file in place (BASE_FULL=1 runs everything)"
  base_require_workflow
  base_restart || true
  hdr "SUMMARY · ${PKG_NAME:-ComfyUI Base} V${PKG_VERSION:-$BASE_VERSION} · base $BASE_VERSION · already installed"
  echo "  install    skipped: this store already holds it, green ($(cat "$f" 2>/dev/null))"
  echo "  restart    $BASE_RESTART_RESULT"
  _base_timings_block; _base_timings_write
  echo "  log        ${BASE_LOG:-none}"
  BASE_SUMMARY_DONE=1
  if [ "${#BASE_FAILED[@]}" -gt 0 ]; then echo -e "${RED}  FAILED     ${#BASE_FAILED[@]}:${NC}"; _base_list "${BASE_FAILED[@]}"; exit 1; fi
  exit 0
}
