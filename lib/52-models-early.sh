# 52-models-early.sh: 3.13.0: the package's downloads start beside the venv build, not after it.
#
# A fresh install built the venv (uv, Python, torch, every pack's requirements, the newest pass, a package's
# SageAttention build) and only THEN started on tens of GB of models, though neither needs the other. Now, once the
# packs are on disk (a custom_nodes/<Pack> row's dest is inside its pack) and the OS stage is joined (it installs
# python3-venv, which uv's bootstrap needs), the run starts in the background every download the model stage WOULD
# start, and joins it just before that stage. The model stage itself is unchanged and
# stays the judge: it walks every row again, finds the early files in place (counted once, as downloaded), fetches any
# row the early pass left alone exactly as before, and fails by name every file the early pass could not get (its three
# attempts were the attempts: their output is shown at the join, and there is no fourth).
#
#   what goes early  a URL row (Hub or GitHub, a file or a snapshot) whose dest does not exist at all and that no file
#                    on the machine supplies by name and size (moving it is the model stage's, and takes seconds). Never
#                    a LOCAL row, a MODELS_LATER row of a staged run, a partial file or a stand-in.
#   the downloader   hf from $BASE_HOME/fetch, a small venv of its own, huggingface_hub[hf-xet] at its newest every run:
#                    the main venv is rebuilt or upgraded while the downloads run, and the hf it is replacing must not be
#                    the one downloading. Every download uses it once it exists (the model stage, fetch-later), with the
#                    main venv's hf as the fallback. It is left as it is while a fetch-later still runs from it.
#   stopping         an install that stops before the join (a failed venv, a restart required, an ERR) stops its early
#                    downloads too; hf and curl resume from the staging folder on the next run.
#   off              BASE_MODELS_FOREGROUND=1 (the 3.12 order), a dry run, a seed build (no models), a buyer's machine
#                    (BASE_RUNTIME: no uv step may run there, and its venv is not built anyway).

_BASE_EARLY_PID=""; _BASE_EARLY_DIR=""; _BASE_EARLY_DESTS=(); _BASE_EARLY_FAILED=()

_base_hf_bin(){ # → the hf every download runs: the fetch venv's, else the main venv's, else PATH's; empty when there is none
  if [ -x "$BASE_HOME/fetch/bin/hf" ]; then echo "$BASE_HOME/fetch/bin/hf"
  elif [ -x "${VENV:-/nonexistent}/bin/hf" ]; then echo "$VENV/bin/hf"
  else command -v hf || true; fi
}
_base_fetch_tool(){ # $BASE_HOME/fetch: huggingface_hub[hf-xet] at its newest, on the newest Python uv provides → 0 when hf is there
  local v="$BASE_HOME/fetch" mm cur out other
  other="$(cat "$BASE_STATE/fetch-later.pid" 2>/dev/null || true)"
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null && [ -x "$v/bin/hf" ]; then
    note "fetch venv: a fetch-later (pid $other) is running from it, so it is used as it is this run"; return 0
  fi
  mm="$(_base_newest_python_minor)"
  if [ -z "$mm" ]; then note "fetch venv: uv lists no Python"; return 1; fi
  cur="$("$v/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ "$cur" != "$mm" ]; then
    rm -rf "$v"
    if ! out="$(uv venv -q --python "$mm" "$v" 2>&1)"; then printf '%s\n' "$out" | tail -3; note "fetch venv: uv venv --python $mm failed"; rm -rf "$v"; return 1; fi
  fi
  if ! out="$(uv pip install -q --python "$v/bin/python" --upgrade "huggingface_hub[hf-xet]" 2>&1)"; then
    printf '%s\n' "$out" | tail -3; note "fetch venv: huggingface_hub[hf-xet] could not be installed"
  fi
  if [ ! -x "$v/bin/hf" ]; then return 1; fi
  ok "fetch venv: huggingface_hub $("$v/bin/python" -c 'import importlib.metadata as m; print(m.version("huggingface_hub"))' 2>/dev/null) on Python $mm ($v)"
}

_base_models_early_specs(){ # → one KIND|rel|url|bytes|dest line per download the model stage would start now (needs IDX)
  local rows cat fam purp file url bytes mnote alts rel dest size path hit
  rows="$(_base_model_rows 2>/dev/null)" || return 0
  while IFS='|' read -r cat fam purp file url bytes mnote alts; do
    [ -n "$cat" ] && [ "$url" != "LOCAL" ] || continue
    rel="$(_base_dest_rel "$cat|$fam|$purp|$file|$url|$bytes|$mnote|$alts")"; dest="$(_base_dest_path "$rel")"
    [ ! -e "$dest" ] || continue                      # in place, partial or a stand-in: each is the model stage's to judge
    if [[ "$file" == */ ]]; then echo "SNAP|$rel|$url|$bytes|$dest"; continue; fi
    if [ "${BASE_STAGED:-0}" = "1" ] && _base_is_later "$file"; then continue; fi
    hit=0
    # shellcheck disable=SC2086
    while IFS=$'\t' read -r size path; do           # the same test the model stage moves by: exact name (or alternate), exact size
      if [ -z "$path" ] || [ ! -f "$path" ]; then continue; fi
      case "$path" in "$STAGING_ROOT"/*|*.partial) continue;; esac
      if _base_size_ok "$size" "$bytes"; then hit=1; break; fi
    done < <(_base_idx_find "$(basename "$file")" $alts)
    [ "$hit" = 1 ] || echo "FILE|$rel|$url|$bytes|$dest"
  done <<< "$rows"
}
_base_models_early(){ # the background half: what goes early, then those downloads; its output is shown at the join
  hdr "MODELS · started beside the venv build"
  local -a specs=() DL_DESTS=()
  local spec need=0 b
  _base_build_index
  while IFS= read -r spec; do
    if [ -n "$spec" ]; then specs+=("$spec"); IFS='|' read -r _ _ _ b _ <<< "$spec"; need=$((need + b)); fi
  done < <(_base_models_early_specs)
  if [ "${#specs[@]}" -eq 0 ]; then note "nothing to download early: every row is in place, LOCAL, later, or a move away"; return 0; fi
  echo "  ${#specs[@]} item(s), $(_base_bytes_to_gb "$need") GB"
  if ! _base_disk_gate "$need"; then note "nothing downloaded early: the model stage judges the disk again and stops the run there"; return 0; fi
  if [ "$BASE_NO_NET" != "1" ] && ! _base_fetch_tool; then note "no downloader of its own: the model stage downloads everything, as before"; return 0; fi
  mkdir -p "$STAGING"; [ "$BASE_NO_NET" = "1" ] || _base_xet_check
  _base_fetch_queue "${specs[@]}"
  rmdir "$STAGING" 2>/dev/null || true
  [ "$STAGING" = "$STAGING_ROOT" ] || rmdir "$STAGING_ROOT" 2>/dev/null || true
  _BASE_EARLY_GOT=(${DL_DESTS[@]+"${DL_DESTS[@]}"})
  for spec in "${specs[@]}"; do _base_in_list "${spec##*|}" ${DL_DESTS[@]+"${DL_DESTS[@]}"} || _BASE_EARLY_BAD+=("${spec##*|}"); done
}

_base_models_early_start(){ # → 0 when the downloads now run in the background; 1 when they are off or there is nothing to start
  if [ "${BASE_MODELS_FOREGROUND:-0}" = "1" ] || [ "$BASE_DRY" = "1" ] || [ "${BASE_SEED:-0}" = "1" ] || _base_runtime_on; then return 1; fi
  [ "${#MODELS[@]}" -gt 0 ] || return 1
  # uv is refreshed once per run: here, in the foreground, so the venv step's own call finds it done instead of racing
  # this one into the same bootstrap venv
  if [ "$BASE_NO_NET" != "1" ] && ! _base_ensure_uv; then return 1; fi
  _base_tmp; _BASE_EARLY_DIR="$BASE_TMPD/models-early"; mkdir -p "$_BASE_EARLY_DIR"
  local dir="$_BASE_EARLY_DIR" nw="${#BASE_WARN[@]}"
  (
    trap - ERR EXIT; set +e
    export TMPDIR="$dir"                               # every temp dir it makes is inside the run's own, removed with it
    MODEL_DL=(); MODEL_FAIL=(); MODEL_DL_GB=0; BASE_DL_BYTES=0; BASE_DL_TIME=0; _BASE_EARLY_GOT=(); _BASE_EARLY_BAD=()
    t0="$SECONDS"; _base_models_early
    {
      printf '_e_dl=('; for x in ${MODEL_DL[@]+"${MODEL_DL[@]}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_e_dests=('; for x in ${_BASE_EARLY_GOT[@]+"${_BASE_EARLY_GOT[@]}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_e_bad=('; for x in ${_BASE_EARLY_BAD[@]+"${_BASE_EARLY_BAD[@]}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_e_warn=('; for x in ${BASE_WARN[@]+"${BASE_WARN[@]:$nw}"}; do printf ' %q' "$x"; done; printf ' )\n'
      printf '_e_bytes=%q\n_e_time=%q\n_e_gb=%q\n_e_secs=%q\n' "$BASE_DL_BYTES" "$BASE_DL_TIME" "$MODEL_DL_GB" "$(( SECONDS - t0 ))"
    } > "$dir/vars.sh"
  ) > "$dir/out" 2>&1 &
  _BASE_EARLY_PID=$!
  note "the model downloads start in the background, beside the venv build; the run joins them before the model stage"
  return 0
}
_base_models_early_join(){ # before the model stage: wait for the early downloads, show their output, and fold what they got in
  [ -n "$_BASE_EARLY_PID" ] || return 0
  hdr "MODELS · joining the downloads started beside the venv build"     # its seconds are how long the downloads outlasted the venv
  local _e_dl=() _e_dests=() _e_bad=() _e_warn=() _e_bytes=0 _e_time=0 _e_gb=0 _e_secs=0 x
  if kill -0 "$_BASE_EARLY_PID" 2>/dev/null; then note "the venv is done; waiting for the downloads"; fi
  wait "$_BASE_EARLY_PID" 2>/dev/null || true
  _BASE_EARLY_PID=""
  cat "$_BASE_EARLY_DIR/out" 2>/dev/null || true
  if [ ! -f "$_BASE_EARLY_DIR/vars.sh" ]; then note "the early downloads ended without a result: the model stage fetches whatever is missing"; return 0; fi
  . "$_BASE_EARLY_DIR/vars.sh"
  for x in ${_e_dl[@]+"${_e_dl[@]}"}; do MODEL_DL+=("$x"); done
  for x in ${_e_dests[@]+"${_e_dests[@]}"}; do _BASE_EARLY_DESTS+=("$x"); done
  for x in ${_e_bad[@]+"${_e_bad[@]}"}; do _BASE_EARLY_FAILED+=("$x"); done
  for x in ${_e_warn[@]+"${_e_warn[@]}"}; do BASE_WARN+=("$x"); done
  BASE_DL_BYTES=$(( BASE_DL_BYTES + _e_bytes )); BASE_DL_TIME=$(( BASE_DL_TIME + _e_time )); MODEL_DL_GB="$(_base_add_gb "$MODEL_DL_GB" "$_e_gb")"
  BASE_STAGE_TIMES+=("$_e_secs|MODELS (in the background, beside the venv build)")
  if [ "${#_e_bad[@]}" -gt 0 ]; then note "${#_e_bad[@]} early download(s) failed after their attempts (above): the model stage fails the run by their names"; fi
  return 0
}
_base_early_got(){ _base_in_list "$1" ${_BASE_EARLY_DESTS[@]+"${_BASE_EARLY_DESTS[@]}"}; }   # <dest> → 0 when the early pass downloaded it
_base_early_settle(){ # inside base_models (its TODO and NEED, by bash's dynamic scope, as _base_fetch_fold uses DL_DESTS):
  # each spec the early pass already failed is folded as failed, by name, and leaves the queue; its attempts were made and
  # shown at the join, and a fourth only adds the wait
  [ "${#_BASE_EARLY_FAILED[@]}" -gt 0 ] && [ "${#TODO[@]}" -gt 0 ] || return 0
  local keep=() s b
  for s in "${TODO[@]}"; do
    if _base_in_list "${s##*|}" "${_BASE_EARLY_FAILED[@]}"; then IFS='|' read -r _ _ _ b _ <<< "$s"; NEED=$((NEED - b)); _base_fetch_fold 1 0 "$s"
    else keep+=("$s"); fi
  done
  TODO=(${keep[@]+"${keep[@]}"})
}

_base_children(){ # <pid> → its child pids (ps where it exists; /proc on a slim image without procps)
  if command -v ps >/dev/null 2>&1; then ps -A -o pid= -o ppid= 2>/dev/null | awk -v p="$1" '$2 == p {print $1}'
  elif [ -d /proc ]; then awk -v p="$1" '{ sub(/.*\) /, ""); if ($2 == p) { f = FILENAME; sub(/^\/proc\//, "", f); sub(/\/stat$/, "", f); print f } }' /proc/[0-9]*/stat 2>/dev/null; fi
  return 0
}
_base_kill_tree(){ # <pid> → TERM to it and every process under it; stopped first, so it starts nothing new meanwhile
  local p="$1" k
  kill -STOP "$p" 2>/dev/null || return 0
  for k in $(_base_children "$p"); do _base_kill_tree "$k"; done
  kill -TERM "$p" 2>/dev/null || true; kill -CONT "$p" 2>/dev/null || true
}
_base_models_early_stop(){ # the run ends before the join: its early downloads end with it (they resume next run, from staging)
  [ -n "${_BASE_EARLY_PID:-}" ] || return 0
  _base_kill_tree "$_BASE_EARLY_PID"; _BASE_EARLY_PID=""
}
