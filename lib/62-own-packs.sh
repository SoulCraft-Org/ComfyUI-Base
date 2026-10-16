# 62-own-packs.sh (3.2.0): a package's own node packs, VENDORED_PACKS=( dir ... ): folders beside the script that ship
# in its zip (ZIP_EXTRA). The base mirrors each into custom_nodes on every install: the folder becomes exactly what the
# zip carries (a file the pack dropped is gone), and a pack whose content did not change is not touched at all.
#
# Each installed copy carries a stamp, custom_nodes/<dir>/.comfy-base-own (pkg=, version=, digest=), so the next run
# knows what is there without trusting a record. On a shared store two packages may ship the same pack: the newest
# version wins and is never moved backwards; the same version with different bytes is installed and named, because one
# of the two missed a version bump. The copy is staged under state/own/, never inside custom_nodes (ComfyUI imports
# every folder there that is not __pycache__ or *.disabled). The previous folder waits in state/own/prev/<pkg>/ until
# the import check has judged the new one: a pack of the package's own that fails to import is put back and the
# restart is blocked, so the live server keeps the code that worked.
#
# Two steps around the hooks. A package written before 3.2.0 still copies its packs itself in pkg_post_venv, over
# whatever is there: `_base_own_packs_save` keeps the live folder BEFORE the hooks, and `base_own_packs` mirrors AFTER
# them (still before the requirements resolve), so the rollback copy is the real previous one and a newer copy kept
# from another package is put back if a hook wrote over it.
#
# Keep two things as they are (3.2.1, found by the Qwen 2.1 session): a package may WRITE INTO its own pack after the
# mirror (Qwen fetches its rewriter prompts into its pack in pkg_post_models, because they cannot ship in the zip), so
# (1) "unchanged" compares the zip's copy with the stamp, never with the live folder's content, and (2) the mirror runs
# before pkg_pre_models / pkg_post_models, so a changed pack is emptied first and the hook refills it. Judging by the
# live folder, or mirroring after those hooks, would silently drop such files until the next run.

OWN_CHANGED=()     # the own packs this run mirrored (the import check judges exactly these)
OWN_SAVED=()       # the own packs whose live folder _base_own_packs_save kept before the hooks

_base_own_py(){ "${SYS_PY:-python3}" "$BASE_DIR/py/own_packs.py" "$@"; }
_base_own_stamp(){ sed -n "s/^$2=//p" "$1/.comfy-base-own" 2>/dev/null | head -1; }   # <installed dir> <key> → value

_base_own_rows(){ # VENDORED_PACKS → one "dir|version|digest" per pack; exit 1 naming each entry that is not a node pack beside the script
  local d src bad=0
  for d in ${VENDORED_PACKS[@]+"${VENDORED_PACKS[@]}"}; do
    if ! [[ "$d" =~ ^[A-Za-z0-9._-]+$ ]]; then echo "  !! VENDORED_PACKS entry '$d' is not a plain folder name" >&2; bad=1; continue; fi
    src="${PKG_DIR:-.}/$d"
    if [ ! -f "$src/__init__.py" ]; then echo "  !! VENDORED_PACKS: $d is not a node pack beside the script (no $src/__init__.py)" >&2; bad=1; continue; fi
    printf '%s|%s|%s\n' "$d" "$(_base_own_py version "$src")" "$(_base_own_py digest "$src")"
  done
  return $bad
}

_base_own_packs_save(){ # before the hooks: keep each own pack's live folder when this run is going to change it
  OWN_SAVED=()
  [ "$BASE_DRY" = "1" ] && return 0
  local rows d ver dig dst keep
  rows="$(_base_own_rows 2>/dev/null)" || return 0
  while IFS='|' read -r d ver dig; do
    [ -n "$d" ] || continue
    dst="$CN/$d"
    if [ -d "$dst" ] && [ "$(_base_own_stamp "$dst" digest)" != "$dig" ]; then
      keep="$BASE_STATE/own/prev/${PKG_ID:-base}/$d"
      rm -rf "$keep"; mkdir -p "$(dirname "$keep")"
      if cp -R "$dst" "$keep" 2>/dev/null; then OWN_SAVED+=("$d"); fi
    fi
  done <<< "$rows"
  return 0
}

base_own_packs(){ # after the hooks, before the requirements: mirror every own pack whose content changed; newest wins on a shared store
  OWN_CHANGED=()
  local rows d ver dig dst have_d have_v have_p prev hooked saved
  rows="$(_base_own_rows)" || { err "VENDORED_PACKS is invalid (see above)"; BASE_FAILED+=("own packs: invalid VENDORED_PACKS"); return 0; }
  [ -n "$rows" ] || return 0
  hdr "OWN PACKS · ${PKG_NAME:-this package}'s own node packs → custom_nodes (mirrored from the zip)"
  while IFS='|' read -r d ver dig; do
    [ -n "$d" ] || continue
    dst="$CN/$d"; prev="$BASE_STATE/own/prev/${PKG_ID:-base}/$d"
    saved=0; case " ${OWN_SAVED[*]-} " in *" $d "*) saved=1;; esac
    # what was installed BEFORE this run: the saved copy's stamp when the hooks may have written over the live one
    if [ "$saved" = 1 ]; then have_d="$(_base_own_stamp "$prev" digest)"; have_v="$(_base_own_stamp "$prev" version)"; have_p="$(_base_own_stamp "$prev" pkg)"
    else have_d="$(_base_own_stamp "$dst" digest)"; have_v="$(_base_own_stamp "$dst" version)"; have_p="$(_base_own_stamp "$dst" pkg)"; fi
    if [ "$have_d" = "$dig" ] && [ "$saved" = 0 ]; then ok "$d $ver unchanged"; continue; fi
    if [ -n "$have_p" ] && [ "$have_p" != "${PKG_ID:-base}" ]; then
      if [ "$have_v" != "-" ] && [ "$ver" != "-" ] && [ -n "$have_v" ] && _base_own_py newer "$have_v" "$ver"; then
        warn "$d: $have_p installed $have_v, newer than ${PKG_ID:-this package}'s $ver: kept, never moved backwards (${PKG_NAME:-this package} needs a release carrying $have_v)"
        if [ "$saved" = 1 ] && [ "$BASE_DRY" != "1" ]; then       # a hook of this package wrote its older copy over it: put the newer one back
          hooked="$BASE_STATE/own/hooked/${PKG_ID:-base}"; rm -rf "$hooked"; mkdir -p "$hooked"
          mv "$dst" "$hooked/$d" 2>/dev/null || true; mv "$prev" "$dst"; rm -rf "$hooked"
        fi
        continue
      fi
      if [ "$have_v" = "$ver" ]; then warn "$d: $have_p and ${PKG_ID:-this package} both ship version $ver with different content: ${PKG_ID:-this package}'s copy goes in; one of them missed a version bump"; fi
    fi
    if [ "$BASE_DRY" = "1" ]; then would "mirror $d $ver into $dst${have_v:+ (now $have_v)}"; continue; fi
    if [ "$saved" = 1 ]; then       # the real previous copy is already kept: the live folder is what a hook wrote, discard it
      hooked="$BASE_STATE/own/hooked/${PKG_ID:-base}/$d"
      if ! _base_own_py mirror "$PKG_DIR/$d" "$dst" "$BASE_STATE/own/new/${PKG_ID:-base}" "$hooked"; then err "$d: could not be mirrored into $dst"; BASE_FAILED+=("own pack $d: mirror failed"); continue; fi
      rm -rf "$BASE_STATE/own/hooked/${PKG_ID:-base}"
    elif ! _base_own_py mirror "$PKG_DIR/$d" "$dst" "$BASE_STATE/own/new/${PKG_ID:-base}" "$prev"; then
      err "$d: could not be mirrored into $dst"; BASE_FAILED+=("own pack $d: mirror failed"); continue
    fi
    printf 'pkg=%s\nversion=%s\ndigest=%s\n' "${PKG_ID:-base}" "$ver" "$dig" > "$dst/.comfy-base-own"
    OWN_CHANGED+=("$d"); PKG_RESTART_WHY+=("own pack $d changed (${have_v:-new} -> $ver)")
    ok "$d $ver mirrored into custom_nodes${have_v:+ (was $have_v)}"
  done <<< "$rows"
  return 0
}

_base_own_packs_judge(){ # <import check log>: an own pack this run changed that fails to import is put back; the restart is blocked
  local log="$1" d dst prev bad=() kept=() u
  [ "${#OWN_CHANGED[@]}" -gt 0 ] || return 0
  # 3.15.0: the launch is the import check this run (lib/80-server.sh); judged from its log there. Judged now, with no
  # log at all, every changed pack would pass and its previous copy would be deleted before anything imported it.
  [ "${BASE_IMPORT_DEFERRED:-0}" != "1" ] || return 0
  for d in "${OWN_CHANGED[@]}"; do
    dst="$CN/$d"; prev="$BASE_STATE/own/prev/${PKG_ID:-base}/$d"
    if [ -f "$log" ] && grep -E '\(IMPORT FAILED\):' "$log" 2>/dev/null | grep -qE "/$d/?\$"; then
      bad+=("$d")
      if [ -d "$prev" ]; then rm -rf "$dst"; mv "$prev" "$dst"; err "$d does not import: its previous copy is back in custom_nodes"
      else rm -rf "$dst"; err "$d does not import and there was no previous copy: removed from custom_nodes"; fi
    else
      rm -rf "$prev"
    fi
  done
  if [ "${#bad[@]}" -gt 0 ]; then
    # the package's own code is not "upstream": it is this package's to fix, and this run fails
    for u in ${BASE_UPSTREAM[@]+"${BASE_UPSTREAM[@]}"}; do
      case " ${bad[*]} " in *" $(printf '%s' "$u" | sed -nE 's/^pack ([^ ]+) .*/\1/p') "*) ;; *) kept+=("$u");; esac
    done
    BASE_UPSTREAM=(${kept[@]+"${kept[@]}"})
    BASE_FAILED+=("own pack(s) do not import: ${bad[*]}: previous copies restored, ComfyUI not restarted")
    BASE_BLOCK_RESTART=1; BASE_IMPORT_RESULT="FAILED (own pack: ${bad[*]})"
  fi
  return 0
}

_base_import_scope(){ # → the own packs to load alone, one per line, when they are the ONLY thing this run changed; nothing means the full check
  [ "${#OWN_CHANGED[@]}" -gt 0 ] || return 0
  [ "${COMFY_OLD:-?}" = "${COMFY_NEW:-!}" ] || return 0
  [ "${#PACK_UPDATED[@]}" -eq 0 ] && [ "${#PACK_CLONED[@]}" -eq 0 ] || return 0
  [ "${BASE_VENV_RESULT:-}" = "reused" ] || return 0
  [ -n "${BASE_VENV_SNAPSHOT:-}" ] && [ -f "$BASE_VENV_SNAPSHOT" ] || return 0      # runtime mode takes no snapshot: the full check
  local now; now="$(_base_uv pip freeze --python "$PY" 2>/dev/null || true)"
  [ -n "$now" ] && [ "$now" = "$(cat "$BASE_VENV_SNAPSHOT")" ] || return 0            # a Python package moved: everything is judged
  printf '%s\n' "${OWN_CHANGED[@]}"
}
_base_quick_test_scoped(){ # <log>: the import check. Only own packs changed: ComfyUI loads just those; anything short of a pass is judged by the full check
  local only rc=0
  only="$(_base_import_scope | tr '\n' ' ')"
  if [ -z "${only// /}" ]; then _base_quick_test "$1"; return $?; fi
  note "only this package's own packs changed: ComfyUI loads just those (${only% })"
  _base_quick_here                                   # 3.15.0: from the copy on the machine's disk (lib/80-server.sh)
  (cd "$RUN_HERE_DIR" && _base_timeout 900 "$RUN_HERE_PY" main.py --quick-test-for-ci --disable-auto-launch $(_base_quick_cpu) --disable-all-custom-nodes --whitelist-custom-nodes $only >"$1" 2>&1) || rc=$?
  if [ "$rc" -eq 0 ] && ! grep -q "IMPORT FAILED" "$1"; then return 0; fi
  note "the scoped check did not pass: the full check judges (an own pack may import another pack when it loads)"
  _base_quick_test "$1"
}
