# 40-packs.sh: node packs: the shared six the base owns, the extras each package declares; locate, clone, move
# every one to its remote HEAD (3.0.0: always the newest, never backwards), requirements into the venv, an advisory
# import scan; strays consolidated.
#
# Row format (BASE_PACKS and every package's PACKS): dir|url|sha|cnr_id|why[|branch]
#   dir     the directory name under custom_nodes (also how the pack is located)
#   sha     the 40-hex LAST-TESTED RECORD: the commit a green run last measured. Printed on every run and written back
#           by podctl after a green install; never a target. Every run moves the pack to its remote HEAD.
#   cnr_id  the Comfy Registry id when it differs from dir (empty otherwise)
#   why     free text, no "|"
#   branch  (3.6.1, optional) the branch to track when the remote's HEAD is the wrong one: every run then moves the pack
#           to the newest commit of THIS branch instead (clone, existing checkouts on another branch, the testbed). Only
#           for a remote whose published HEAD is stale (rgthree's HEAD is a 2023 `dev`, its work is on `main`); a pin
#           is still never written here. Left off, the remote's HEAD is tracked, as before. Last on purpose: every
#           reader that takes the first five fields (podctl's pin rows, comfybuild, dockerize) reads the row unchanged.

# The packs most workflows share, declared by the base. A package must not redeclare one of these.
BASE_PACKS=(
 "rgthree-comfy|https://github.com/rgthree/rgthree-comfy|2c5342a8cb0eaecaabf61435a5f37dd594c510ba||switches, bypassers, any-switch, fast group bypassers/muters, radio panels, Power Lora Loader|main"
 "ComfyUI-KJNodes|https://github.com/kijai/ComfyUI-KJNodes|57105374f47d0fbb49c9c3926fb981702e0a4b5c|comfyui-kjnodes|SetNode/GetNode routing, resize, patches, video helpers"
 "ComfyUI-VideoHelperSuite|https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite|4d907bee61e92c2e65af3bd6383a4e4d356126d1|comfyui-videohelpersuite|video load/combine, SelectEveryNth, custom formats"
 "cg-use-everywhere|https://github.com/chrisgoringe/cg-use-everywhere|50ae9f8c5d8b9538589663c90a15d4067a02969c||Anything Everywhere broadcast links"
 "ComfyUI-Manager|https://github.com/Comfy-Org/ComfyUI-Manager|f82970b7cb63ad44928308f980a1d38fda103cbb|comfyui-manager|the node manager (hygiene sets its security level); RunPod's image bakes it, other images do not"
 "ComfyUI-advanced-model-manager|https://github.com/BISAM20/ComfyUI-advanced-model-manager|232997501a9ae6f9fa83f3594d16ca5341ecdbb2|comfyui-advanced-model-manager|the Hugging Face browser (2.1.0): Hub search and downloads into the library's folders via extra_model_paths.yaml, HF_TOKEN from the environment and sent to huggingface.co only (read once at this pin)"
)
# There is no image-owned pack list any more: the tree on the volume is the base's. A declared row and an undeclared
# git checkout both move to their remote HEAD (a row's branch, when it names one); an undeclared directory with no git source is reported and left alone.
# Their requirements all go into the venv.
DEL_DIRS=()
BASE_PACKS_LATEST_ROWS=()

_base_pack_rows(){ # prints BASE_PACKS then PACKS; validates shape; exit 1 on any violation (message on stderr)
  local row dir url sha cnr why branch bad=0 seen=" " base_seen=" "
  for row in ${BASE_PACKS[@]+"${BASE_PACKS[@]}"}; do IFS='|' read -r dir url sha cnr why branch <<< "$row"; base_seen="$base_seen$dir "; done
  for row in ${BASE_PACKS[@]+"${BASE_PACKS[@]}"} ${PACKS[@]+"${PACKS[@]}"}; do
    IFS='|' read -r dir url sha cnr why branch <<< "$row"
    if [ -z "$dir" ] || [ -z "$url" ] || ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
      echo "  !! bad PACKS row (want dir|url|sha|cnr_id|why[|branch] with a 40-hex sha): $row" >&2; bad=1; continue
    fi
    # a branch name, never a commit (a pin) and never the tail of a why that held a "|"
    if [ -n "$branch" ] && { ! _base_pack_branch_ok "$branch" || [[ "$branch" =~ ^[0-9a-f]{7,40}$ ]]; }; then
      echo "  !! bad PACKS row: '$branch' is not a branch name (the sixth field names the branch to track; why may not hold a '|'): $row" >&2; bad=1; continue
    fi
    case "$seen" in *" $dir "*)
      case "$base_seen" in *" $dir "*) echo "  !! $dir is a base pack and may not be redeclared by ${PKG_NAME:-the package}" >&2;;
        *) echo "  !! $dir is declared twice" >&2;; esac
      bad=1; continue;;
    esac
    seen="$seen$dir "; echo "$row"
  done
  return $bad
}

_base_pack_branch_ok(){ # <name> → 0 when it is a plausible git branch name (what a sixth field may hold)
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] && [[ "$1" != *..* ]] && [[ "$1" != */ ]] && [[ "$1" != *.lock ]]
}
_base_pack_key(){ # <url> [branch] → the key py/pack_heads.py answers under: the url, or url#branch for a tracked branch
  printf '%s%s' "$1" "${2:+#$2}"
}

_base_pack_locate(){ # <dir> <url> → prints the checkout dir or nothing
  local name="$1" url="$2" found=""
  if [ -d "$CN/$name" ]; then echo "$CN/$name"; return 0; fi
  found="$(find "$CN" -mindepth 1 -maxdepth 1 -type d -iname "$name" 2>/dev/null | head -1 || true)"
  [ -z "$found" ] && found="$(grep -lis "url = ${url%.git}\(\.git\)\?\$" "$CN"/*/.git/config 2>/dev/null | head -1 | sed 's#/\.git/config$##' || true)"
  [ -n "$found" ] && echo "$found"; return 0
}
_base_fake_clone(){ # BASE_NO_NET: the shape of a clone, no network
  local url="$1" dir="$2"
  mkdir -p "$dir/.git"; printf '[remote "origin"]\n\turl = %s\n' "$url" > "$dir/.git/config"; : > "$dir/requirements.txt"
}
_base_pack_sync(){ # <dir> <url> [branch] → prints git's complaint if the pack could not move, nothing if it did (every run, 3.0.0)
  # Nothing here reads the repo's existing tracking config: baked clones sit on a branch with no upstream or
  # detached after `clone --branch <tag>`, so the remote, the branch and the upstream are re-established from
  # the canonical URL and the update is a fast-forward onto the branch the remote publishes as HEAD, or (3.6.1) onto
  # the row's branch when it names one: a checkout on the stale HEAD branch moves across to it.
  local dir="$1" url="$2" track="${3:-}" rmt="" br="" cur="" out="" fix=""
  rmt="$(_base_git_remote "$dir")" || true
  if [ -z "$rmt" ]; then rmt=origin; _base_git -C "$dir" remote add origin "$url" >/dev/null 2>&1 || true; fix="no remote was configured — origin set to $url"; fi
  if [ -n "$track" ]; then
    # a `clone --depth 1` fetches its one branch only (the stale HEAD's): widen the fetch to the tracked one, or the
    # fetch below never brings it and `--set-upstream-to` refuses a ref no refspec maps
    if ! _base_git -C "$dir" config --get-all "remote.$rmt.fetch" 2>/dev/null | grep -qE "^\+?refs/heads/(\*|$track):refs/remotes/$rmt/(\*|$track)\$"; then
      _base_git -C "$dir" remote set-branches --add "$rmt" "$track" >/dev/null 2>&1 || true
    fi
  fi
  if [ "$(_base_git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]; then
    _base_git -C "$dir" fetch -q --unshallow --tags --force --prune "$rmt" >/dev/null 2>&1 || true
  fi
  out="$(_base_git -C "$dir" fetch -q --tags --force --prune "$rmt" 2>&1)" || { printf '%s\n' "$out"; return 0; }
  br="$track"
  if [ -n "$br" ] && ! _base_git -C "$dir" rev-parse -q --verify "refs/remotes/$rmt/$br" >/dev/null 2>&1; then
    echo "$rmt has no branch '$br' (the row's sixth field)"; return 0
  fi
  [ -z "$br" ] && br="$(_base_git -C "$dir" symbolic-ref -q --short "refs/remotes/$rmt/HEAD" 2>/dev/null | sed "s#^$rmt/##" || true)"
  if [ -z "$br" ]; then
    _base_git -C "$dir" remote set-head "$rmt" -a >/dev/null 2>&1 || true
    br="$(_base_git -C "$dir" symbolic-ref -q --short "refs/remotes/$rmt/HEAD" 2>/dev/null | sed "s#^$rmt/##" || true)"
  fi
  [ -z "$br" ] && br="$(_base_git -C "$dir" ls-remote --symref "$rmt" HEAD 2>/dev/null | sed -n 's#^ref:[[:space:]]*refs/heads/\([^[:space:]]*\).*#\1#p' | sed -n '1p' || true)"
  if [ -z "$br" ]; then echo "cannot tell which branch $rmt publishes as HEAD"; return 0; fi
  cur="$(_base_git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  if [ "$cur" != "$br" ]; then
    _base_git -C "$dir" checkout -q "$br" >/dev/null 2>&1 || _base_git -C "$dir" checkout -q -b "$br" "$rmt/$br" >/dev/null 2>&1 || true
    fix="${fix:+$fix; }was on '$cur' — now on $br tracking $rmt/$br${track:+ (the branch its row names)}"
  elif ! _base_git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    fix="${fix:+$fix; }branch '$br' had no upstream — wired to $rmt/$br"
  fi
  [ -n "$fix" ] && note "$(basename "$dir"): $fix" >&2 || true
  _base_git -C "$dir" branch -q --set-upstream-to="$rmt/$br" "$br" >/dev/null 2>&1 || true
  # never backwards: a checkout already AHEAD of the remote's HEAD (a descendant of it) stays where it is
  if _base_git -C "$dir" merge-base --is-ancestor "$rmt/$br" HEAD >/dev/null 2>&1; then return 0; fi
  if ! out="$(_base_git -C "$dir" merge -q --ff-only "$rmt/$br" 2>&1)"; then
    # diverged (local commits the remote does not have): the newest is the remote's HEAD. Detach there; the local
    # branch keeps the local commits, so nothing is lost and nothing is rewritten.
    if _base_git -C "$dir" checkout -q --detach "$rmt/$br" >/dev/null 2>&1; then
      note "$(basename "$dir"): diverged from $rmt/$br: detached at the remote HEAD; the local commits stay on branch '$br'" >&2
    else printf '%s\n' "$out"; fi
  fi
  return 0
}
_BASE_PACK_HEADS=""   # 3.5.0: the table py/pack_heads.py wrote this run (key, branch, sha per remote that answered)
_base_pack_heads_probe(){ # <url | url#branch>... → every remote's HEAD (or that branch's tip), asked at once; nothing asked in a dry, fake or runtime run
  _BASE_PACK_HEADS=""
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ] || _base_runtime_on; then return 0; fi
  [ "$#" -gt 0 ] || return 0
  _base_tmp; _BASE_PACK_HEADS="$BASE_TMPD/pack-heads.tsv"
  printf '%s\n' "$@" | "${SYS_PY:-python3}" "$BASE_DIR/py/pack_heads.py" > "$_BASE_PACK_HEADS" 2>/dev/null || : > "$_BASE_PACK_HEADS"
  return 0
}
_base_pack_current(){ # <dir> <url> [branch] → 0 when the checkout sits exactly on what that remote publishes as HEAD (or on
  # the tip of the row's branch): same commit, on that branch, wired to it, not shallow, no local edits. Then there is
  # nothing to fetch. Anything else takes the full path (_base_pack_move), which fetches, never moves backwards, stashes
  # edits and says what git says.
  local dir="$1" url="$2" track="${3:-}" line br sha rmt
  [ -n "$_BASE_PACK_HEADS" ] && [ -s "$_BASE_PACK_HEADS" ] || return 1
  line="$(awk -F'\t' -v u="$(_base_pack_key "$url" "$track")" '$1==u {print; exit}' "$_BASE_PACK_HEADS")"
  [ -n "$line" ] || return 1
  br="$(printf '%s' "$line" | cut -f2)"; sha="$(printf '%s' "$line" | cut -f3)"
  [ "$(_base_git -C "$dir" rev-parse HEAD 2>/dev/null)" = "$sha" ] || return 1
  [ "$(_base_git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "$br" ] || return 1
  [ "$(_base_git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null)" = "false" ] || return 1
  [ -z "$(_base_git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ] || return 1
  rmt="$(_base_git_remote "$dir")"; [ -n "$rmt" ] || return 1
  [ "$(_base_git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)" = "$rmt/$br" ] || return 1
  [ "$(_base_git -C "$dir" remote get-url "$rmt" 2>/dev/null)" = "$url" ] || return 1      # the remote it fetches is the one asked
  return 0
}
_base_pack_move_aside(){ # <dir> → moves a pack directory aside under custom_nodes/comfy-base-aside.disabled, prints the new path
  local d="$1" aside="$CN/comfy-base-aside.disabled" to   # ".disabled": ComfyUI skips it when it loads custom_nodes
  mkdir -p "$aside" 2>/dev/null || return 1
  to="$aside/$(basename "$d")-$(_base_ts)"
  mv "$d" "$to" 2>/dev/null && printf '%s\n' "$to"
}
_base_pack_stash(){ # <dir> <name> → stashes local edits under a named, restorable entry; 0 on success
  local d="$1" name="$2" ts; ts="$(_base_ts)"
  if _base_git -C "$d" stash push -q -m "comfy-base $ts" >/dev/null 2>&1; then
    note "$name: local edits stashed as 'comfy-base $ts' (restore: git -C \"$d\" stash pop), then moved to HEAD"
    PACK_DIRTY+=("$name (stashed 'comfy-base $ts')"); return 0
  fi
  return 1
}
_base_pack_move(){ # <name> <dir> <url> [branch] → moves one git checkout to its remote HEAD (or the branch's tip); records the result
  local name="$1" dir="$2" url="$3" track="${4:-}" before after out
  if [ -n "$(_base_git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ] && ! _base_pack_stash "$dir" "$name"; then
    miss "$name: local edits could not be stashed: not moved"; PACK_DIRTY+=("$name"); BASE_FAILED+=("pack $name: local edits could not be stashed, not at its newest"); return 0
  fi
  before="$(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo '?')"
  out="$(_base_pack_sync "$dir" "$url" "$track")"
  after="$(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo '?')"
  if [ -n "$out" ]; then
    PACK_PRESENT+=("$name @ $after"); err "$name not at its newest: $(_base_git_diag "$out")"; BASE_FAILED+=("pack $name: could not move to its remote HEAD ($(_base_git_diag "$out"))")
  elif [ "$before" != "$after" ]; then PACK_UPDATED+=("$name $before→$after"); ok "$name $before → $after"
  else PACK_PRESENT+=("$name @ $after"); ok "$name @ $after (${track:+the tip of $track, }its remote HEAD)"; fi
  return 0
}
_base_pack_reqfile(){ # <dir> → the requirements file a pack wants installed (Frame-Interpolation ships a no-cupy variant)
  local d="$1"
  if [ -f "$d/requirements-no-cupy.txt" ]; then echo "$d/requirements-no-cupy.txt"
  elif [ -f "$d/requirements.txt" ]; then echo "$d/requirements.txt"; fi
}
_base_all_reqfiles(){ # ComfyUI's + every pack directory PRESENT in the tree (managed or not) + every pack located this run + the ledger's
  # `if`, not `[ … ] && …`: an AND-list at the END OF A LOOP BODY trips errexit and the ERR trap exits.
  local f d name path m seen=" "
  if [ -f "$COMFY/requirements.txt" ]; then echo "$COMFY/requirements.txt"; fi
  for d in "${CN:-/nonexistent}"/*/; do
    d="${d%/}"
    if [ -d "$d" ]; then f="$(_base_pack_reqfile "$d")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi; fi
  done
  for d in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
    d="${d#*|}"; f="$(_base_pack_reqfile "$d")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi
  done
  for m in "${BASE_STATE:-/nonexistent}"/packages/*.manifest; do
    [ -f "$m" ] || continue
    while IFS=$'\t' read -r name d _ path; do
      if [ "$name" = "pack" ] && [ -d "$path" ]; then f="$(_base_pack_reqfile "$path")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi; fi
    done < "$m"
  done
}
_base_pack_imports_report(){ # <name|dir>… → for packs with no requirements file: what they import, and whether the venv has it. Advisory.
  [ "$#" -gt 0 ] || return 0
  if [ "$BASE_DRY" = "1" ]; then would "check every requirements-less pack's imports against $VENV"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "pack import check skipped (BASE_NO_NET: the fake venv has no packages)"; return 0; fi
  local nm req missing
  while IFS=$'\t' read -r nm req missing; do
    [ -n "$nm" ] || continue
    if [ -n "$missing" ]; then
      # a warning, not a stop: the scan cannot know whether the importing module is ever loaded. A pack that
      # fails to import outright is caught, and blocks the restart advice, in base_import_check.
      # 2.0.50: this used to assert "a node using it renders red". On the cold pod it said exactly that about the
      # gguf pack while /object_info showed LoaderGGUF and AILab_QwenVL_GGUF_PromptEnhancer both REGISTERED — the
      # scan sees imports the pack never reaches at load time. base_import_check is the one that actually knows.
      warn "$nm: no requirements file, and the venv cannot import ${missing//,/, } — a node that reaches one of those at run time fails (the import check below says whether the pack registered at all)"
    elif [ -n "$req" ]; then note "$nm: no requirements file — imports ${req//,/, }; all already provided"
    else note "$nm: no requirements file — imports nothing third-party"; fi
  done < <("$PY" "$BASE_DIR/py/import_scan.py" "$@" 2>/dev/null || true)
  return 0
}

base_packs(){ # base_packs git | pip
  local phase="${1:-git}" row name url sha cnr why branch dir out
  if [ "$phase" = "git" ]; then
    hdr "NODE PACKS · locate / clone / pin"
    [ "$BASE_DRY" = "1" ] || mkdir -p "$CN" 2>/dev/null || true
    PACK_DIRS=(); BASE_PACKS_LATEST_ROWS=()
    local rows; rows="$(_base_pack_rows)" || { err "the pack tables are invalid (see above)"; BASE_FAILED+=("packs: invalid rows"); return 1; }
    local declared=" " purls=() pu pd
    # 3.5.0: every remote's HEAD, asked at once (declared rows and every checkout already in custom_nodes)
    while IFS='|' read -r name url sha cnr why branch; do if [ -n "$url" ]; then purls+=("$(_base_pack_key "$url" "$branch")"); fi; done <<< "$rows"
    for pd in "${CN:-/nonexistent}"/*/; do
      if [ -d "$pd/.git" ]; then pu="$(_base_git -C "$pd" remote get-url "$(_base_git_remote "$pd")" 2>/dev/null || true)"; if [ -n "$pu" ]; then purls+=("$pu"); fi; fi
    done
    _base_pack_heads_probe ${purls[@]+"${purls[@]}"}
    while IFS='|' read -r name url sha cnr why branch; do
      [ -n "$name" ] || continue
      dir="$(_base_pack_locate "$name" "$url")"
      # a declared pack that is not a git checkout (a registry install) has no source to move: it goes aside and is cloned
      if [ -n "$dir" ] && ! { [ -d "$dir/.git" ] && _base_git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; }; then
        if [ "$BASE_DRY" = "1" ]; then would "move $dir aside (not a git checkout) and clone $url at ${branch:+the tip of $branch, }its HEAD"; PACK_DIRS+=("$name|$dir"); continue
        elif [ "$BASE_NO_NET" = "1" ]; then PACK_PRESENT+=("$name (not a git checkout; no-net: not moved)"); PACK_DIRS+=("$name|$dir"); continue
        elif _base_runtime_on; then PACK_PRESENT+=("$name (runtime)"); PACK_DIRS+=("$name|$dir"); continue; fi
        local aside; if aside="$(_base_pack_move_aside "$dir")"; then note "$name: not a git checkout: moved aside to $aside so it can be cloned at its newest"; dir=""
        else err "$name: not a git checkout and could not be moved aside"; BASE_FAILED+=("pack $name: not a git checkout, not at its newest"); PACK_DIRS+=("$name|$dir"); continue; fi
      fi
      if [ -z "$dir" ] && _base_runtime_on; then     # 3.1.0: a buyer's machine clones nothing; the runtime must carry it
        err "$name is not in the proven runtime (${BASE_RUNTIME_NOTE:-runtime}): this package needs a newer runtime"
        BASE_FAILED+=("pack $name: not in the runtime"); continue
      fi
      if [ -z "$dir" ]; then
        todo "$name — $why"
        if [ "$BASE_DRY" = "1" ]; then would "git clone --depth 1 ${branch:+--branch $branch }$url (${branch:+the tip of $branch, }its HEAD)"; PACK_CLONED+=("$name (would clone)"); continue; fi
        if [ "$BASE_NO_NET" = "1" ]; then _base_fake_clone "$url" "$CN/$name"; dir="$CN/$name"; PACK_CLONED+=("$name (fake clone)")
        else
          # git's own exit status is the judge. Piping the clone through `grep -v | tail` made an EMPTY output (a quiet
          # clone on an image that prints no warnings) look like a failure under pipefail — every pack "failed" on a
          # pod where every directory was in place (2026-09-05).
          local cout="" crc=0
          cout="$(_base_git clone -q --depth 1 ${branch:+--branch "$branch"} "$url" "$CN/$name" 2>&1)" || crc=$?
          [ -n "$cout" ] && { printf '%s\n' "$cout" | grep -v 'depth is ignored' | tail -2 || true; }
          if [ "$crc" = "0" ]; then dir="$CN/$name"; PACK_CLONED+=("$name @ $(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null)"); ok "$name cloned at ${branch:+the tip of $branch, }its HEAD"
          else err "clone failed: $name (git exit $crc)"; BASE_FAILED+=("pack: $name clone failed"); continue; fi
        fi
      elif [ "$BASE_DRY" = "1" ]; then
        would "fetch $name and move it to ${branch:+the tip of its branch $branch, }the HEAD its remote publishes (never backwards; local edits stashed first)"; PACK_PRESENT+=("$name")
      elif [ "$BASE_NO_NET" = "1" ]; then PACK_PRESENT+=("$name (no-net: not moved)")
      elif _base_runtime_on; then PACK_PRESENT+=("$name (runtime)")
      elif _base_pack_current "$dir" "$url" "$branch"; then
        PACK_PRESENT+=("$name @ $(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null)"); ok "$name @ $(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null) (its remote HEAD; nothing to fetch)"
      else _base_pack_move "$name" "$dir" "$url" "$branch"; fi
      declared="$declared$(basename "${dir:-$name}") "
      PACK_DIRS+=("$name|$dir")
      if [ -n "$dir" ] && [ "$BASE_DRY" != "1" ]; then
        BASE_PACKS_LATEST_ROWS+=("$name|$url|$(_base_git -C "$dir" rev-parse HEAD 2>/dev/null || echo "$sha")|$cnr|$why${branch:+|$branch}")
      fi
    done <<< "$rows"
    # undeclared git checkouts are software on this machine too: they go to their upstream HEAD the same way
    local ud un uurl
    for ud in "${CN:-/nonexistent}"/*/; do
      ud="${ud%/}"; un="$(basename "$ud")"
      [ -d "$ud" ] || continue
      case "$un" in .*|__pycache__|*.disabled) continue;; esac
      case "$declared" in *" $un "*) continue;; esac
      if [ -d "$ud/.git" ] && _base_git -C "$ud" rev-parse --git-dir >/dev/null 2>&1; then
        uurl="$(_base_git -C "$ud" remote get-url "$(_base_git_remote "$ud")" 2>/dev/null || true)"
        if [ -z "$uurl" ]; then note "$un: undeclared git checkout with no remote: nothing to move it to"; continue; fi
        if [ "$BASE_DRY" = "1" ]; then would "move undeclared $un to its remote HEAD"
        elif [ "$BASE_NO_NET" != "1" ] && ! _base_runtime_on; then
          if _base_pack_current "$ud" "$uurl"; then PACK_PRESENT+=("$un @ $(_base_git -C "$ud" rev-parse --short HEAD 2>/dev/null)"); ok "$un @ $(_base_git -C "$ud" rev-parse --short HEAD 2>/dev/null) (its remote HEAD; nothing to fetch)"
          else _base_pack_move "$un" "$ud" "$uurl"; fi
        fi
      elif [ -f "$ud/__init__.py" ]; then note "$un: undeclared and not a git checkout: no source to upgrade it from, left alone"; fi
    done
    # the last-tested records: printed on EVERY real run, so podctl can write them back after a green install
    if [ "$BASE_DRY" != "1" ] && [ "${#BASE_PACKS_LATEST_ROWS[@]}" -gt 0 ]; then
      note "pack records (each pack's HEAD this run; podctl writes them back after a green install):"
      printf '  "%s"\n' "${BASE_PACKS_LATEST_ROWS[@]}"
    fi
    # packs a package used to install and no longer needs: reported only when no installed package claims them
    for name in ${DROPPED_PACKS:-}; do
      if [ -d "$CN/$name" ] && ! _base_ledger_pack_claimed "$name"; then note "$name is no longer used by ${PKG_NAME:-this package} — still installed, remove by hand if unwanted"; fi
    done
    return 0
  fi
  # ---- pip half (3.0.0): ONE install of the derived, pin-free set, never an upstream file as written. ComfyUI's own
  # requirements go in every run, not only when the venv is built: --latest once moved ComfyUI 0.34.6 -> 0.35.0 on a
  # REUSED venv, its four new pins were never installed, and main.py died on `No module named
  # 'comfy_aimdo.malloc_graph'`. Installing each upstream file as written, though, put every == pin back down on every
  # run, which is exactly what "always the newest" forbids. py/reqlift.py merges ComfyUI's, every pack's and PIP_EXTRA
  # into one file with the holds removed (lib/47-latest.sh), and uv resolves it under the torch constraint.
  hdr "NODE PACKS · requirements into $VENV (one resolve, every upstream pin lifted)"
  local d rf entry probe=() out
  CONSTRAINTS="${CONSTRAINTS:-$BASE_STATE/constraints-torch.txt}"
  for entry in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
    name="${entry%%|*}"; d="${entry#*|}"; rf="$(_base_pack_reqfile "$d")"
    if [ -z "$rf" ]; then probe+=("$name|$d"); fi
  done
  if [ "$BASE_DRY" = "1" ]; then would "derive one pin-free requirement set from ComfyUI's, $(_base_all_reqfiles | wc -l | tr -d ' ') file(s) and PIP_EXTRA, and install it in one uv resolve under the torch constraint"
  elif [ "$BASE_NO_NET" = "1" ]; then note "requirements: pip skipped (BASE_NO_NET)"
  elif ! command -v uv >/dev/null 2>&1 || [ ! -x "${PY:-/nonexistent}" ]; then miss "no uv or no venv interpreter: requirements not installed"; BASE_FAILED+=("requirements: no uv or no venv")
  else
    base_torch_constraints
    if ! out="$(_base_reqlift 2>&1)"; then
      miss "py/reqlift.py failed: $(printf '%s' "$out" | tail -2)"; BASE_FAILED+=("requirements: could not derive the requirement set")
    else
      printf '%s\n' "$out" | awk -F'\t' '$1=="summary"{printf "  %s\n", $2}'
      if _base_run_watched "requirements (one resolve)" _base_derived_install; then ok "ComfyUI + every pack's requirements + PIP_EXTRA installed, newest"
        BASE_DERIVED_DONE="$(_base_derived_fp)"      # 3.3.0: the newest pass does not resolve this same set again
      else miss "the derived requirement set failed to install (uv's reason is above)"; BASE_FAILED+=("requirements: the derived set did not install"); fi
    fi
  fi
  # 2.12.3: one file at a time, `--upgrade` takes whatever a file names to its newest even past a cap another installed
  # package declares (huggingface-hub 2.0.0 under transformers' <2.0); re-resolve the conflicts together, at their newest
  # A pack with no requirements file is only fine if what it imports is already in the venv. Writing one
  # into the checkout is not the fix (it dirties the tree and the pack is never updated again).
  _base_pack_imports_report ${probe[@]+"${probe[@]}"}
}

_base_sage_ver(){ # SageAttention 2 sets no __version__ attribute — the success line printed blank on the cold pod
  "$PY" -c 'import importlib.metadata as m; print(m.version("sageattention"))' 2>/dev/null || echo "(installed, no version metadata)"
}
BASE_CUDA_BUILD_OK=""      # "" not probed · 1 the toolkit can link cuBLAS · 0 it cannot
base_cuda_toolchain(){ # CAN THIS TOOLKIT LINK WHAT PACKAGES BUILD? "nvcc exists" is not the same question.
  # runpod/comfyui:1.4.7-cuda13.0 ships nvcc + cudart ONLY — no cuBLAS lib and no headers. llama-cpp-python died at
  # CMake ("Target ggml-cuda links to CUDA::cublas but the target was not found") and SageAttention died at nvcc
  # exit 255, seven minutes in — and because both builders return 0 on failure the run then downloaded 131 GB and
  # reported the wreck at the end. A two-second test compile answers the real question before any of that. The
  # older pod images all carried a full toolkit, which is the only reason this never showed up. (cold pod, 2026-09-08)
  [ -n "$BASE_CUDA_BUILD_OK" ] && return 0
  local nvcc="${CUDACXX:-/usr/local/cuda/bin/nvcc}"
  [ -x "$nvcc" ] || nvcc="$(command -v nvcc 2>/dev/null || true)"
  if [ -z "$nvcc" ] || [ ! -x "$nvcc" ]; then BASE_CUDA_BUILD_OK=0; return 0; fi   # no toolkit: the builders say so
  _base_tmp
  local src="$BASE_TMPD/cublas_probe.cu" bin="$BASE_TMPD/cublas_probe" ver pkg key stamp root
  # 3.5.0: a toolkit that linked cuBLAS on this machine and has not changed since is not compiled against again. The key
  # is nvcc's path and version and the identity of the cuBLAS library and header; per machine, since the store is shared
  # and each machine has its own toolkit (a restarted container loses an apt-installed library: the key changes).
  root="$(cd "$(dirname "$nvcc")/.." 2>/dev/null && pwd -P)"
  key="$( { echo "$nvcc"; "$nvcc" --version 2>/dev/null | tail -1; ls -liL "$root"/lib64/libcublas.so* "$root"/lib/libcublas.so* "$root"/include/cublas_v2.h /usr/include/cublas_v2.h 2>/dev/null; } | cksum)"
  stamp="${BASE_STATE:-/nonexistent}/machines/$(hostname -s 2>/dev/null || echo machine).cublas-probe"
  if [ "$(cat "$stamp" 2>/dev/null)" = "$key" ]; then BASE_CUDA_BUILD_OK=1; ok "CUDA toolchain links cuBLAS (this toolkit was probed on this machine before)"; return 0; fi
  printf '%s\n' '#include <cublas_v2.h>' 'int main(){ cublasHandle_t h; cublasCreate(&h); cublasDestroy(h); return 0; }' > "$src"
  if "$nvcc" -o "$bin" "$src" -lcublas >/dev/null 2>&1; then
    BASE_CUDA_BUILD_OK=1; ok "CUDA toolchain links cuBLAS — source builds can proceed"
    if [ -n "${BASE_STATE:-}" ] && [ "$BASE_DRY" != "1" ]; then mkdir -p "$(dirname "$stamp")" 2>/dev/null && printf '%s\n' "$key" > "$stamp" 2>/dev/null || true; fi
    return 0
  fi
  ver="$("$nvcc" --version 2>/dev/null | sed -nE 's/.*release ([0-9]+)\.([0-9]+).*/\1-\2/p' | head -1)"
  pkg="cuda-libraries-dev${ver:+-$ver}"          # the installed toolkit's own; with none readable, NVIDIA's newest-tracking metapackage
  miss "the CUDA toolkit cannot link cuBLAS — nvcc is present but the math libraries are not"
  if [ "$BASE_DRY" = "1" ]; then
    would "apt-get install $pkg, then re-probe (source builds need cuBLAS: llama-cpp-python, SageAttention)"
    BASE_CUDA_BUILD_OK=0; return 0
  fi
  if command -v apt-get >/dev/null 2>&1 && _base_can_root; then
    note "installing $pkg (a source build links cuBLAS; the image shipped nvcc + cudart only)"
    _base_apt install "$pkg" >/dev/null 2>&1 || { _base_apt update >/dev/null 2>&1 && _base_apt install "$pkg" >/dev/null 2>&1; } || true
    if "$nvcc" -o "$bin" "$src" -lcublas >/dev/null 2>&1; then
      BASE_CUDA_BUILD_OK=1; ok "$pkg installed — the toolchain links cuBLAS now"; BASE_CHANGED+=("$pkg installed (the image shipped no cuBLAS)"); return 0
    fi
  fi
  BASE_CUDA_BUILD_OK=0
  BASE_WARN+=("CUDA toolkit incomplete: nvcc cannot link cuBLAS. Source builds (llama-cpp-python, SageAttention) will be skipped. Remedy: apt-get install -y $pkg")
  warn "cuBLAS still missing after trying $pkg — CUDA source builds will be SKIPPED, not attempted"
  return 0
}

_base_sage_covers(){ # <key> <prefix> <sm token, e.g. 12-0> → 0 when <key> is <prefix>archs-<list> and the list holds this GPU
  # 3.0.1: an image built with no GPU stamps SageAttention for SAGE_ARCHS="9.0;12.0". On the machine it serves, the same
  # source, Python and torch built for a list that holds this GPU's sm IS current; before, the keys never matched and
  # every first install rebuilt it (10-20 min). Measured by the SoulCraft image session on base 3.0.0.
  local key="$1" prefix="$2" tok="$3" list
  case "$key" in "${prefix}archs-"*) ;; *) return 1;; esac
  list="${key#"${prefix}archs-"}"
  case "_${list}_" in *"_${tok}_"*) return 0;; esac
  return 1
}
base_build_sageattention(){ # for a package's pkg_post_venv: SageAttention 2 built from source for THIS GPU's sm (no PyPI wheel has
  # sm_100/sm_120 kernels; 1.0.6 crashes there). SAGE_REF names a BRANCH or tag (default main); 3.0.0 retired SAGE_WHEEL and a
  # 40-hex SAGE_REF, both of which held it below its newest. It is rebuilt whenever the full cache key (the resolved commit,
  # Python, torch, sm) differs from the one stamped beside the install, so a reused venv follows main like a fresh one.
  # A failed build FAILS the run (2.0.22: a missing build died at the first render with `No module named 'sageattention'`).
  if [ "$BASE_DRY" = "1" ]; then would "build SageAttention from source for ${BASE_GPU_SM:-the GPU} into $VENV unless the installed build matches main's newest commit"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "SageAttention: skipped (fake venv — BASE_NO_NET)"; return 0; fi
  if _base_runtime_on; then                           # 3.1.0: the runtime's own build, by its cache key, or nothing
    local want stamped; want="$(_base_runtime_field sageattention.key)"; stamped="$(cat "$VENV/.comfy-base-sageattention" 2>/dev/null || true)"
    if [ -n "$want" ] && [ "$stamped" = "$want" ] && "$PY" -c "import sageattention" >/dev/null 2>&1; then ok "sageattention from the proven runtime ($want)"; return 0; fi
    err "SageAttention is not the runtime's build (runtime ${want:-none}, venv ${stamped:-none}): a runtime machine builds nothing; this GPU may need a runtime captured on its own architecture"
    BASE_FAILED+=("SageAttention: not the runtime's build"); return 0
  fi
  if [ -n "${SAGE_WHEEL:-}" ]; then note "SAGE_WHEEL is retired in 3.0.0 and ignored: SageAttention is built from its newest source"; fi
  if [[ "${SAGE_REF:-}" =~ ^[0-9a-f]{7,40}$ ]]; then note "SAGE_REF=$SAGE_REF is a commit, retired in 3.0.0 (a commit is a pin): main is used"; SAGE_REF=main; fi
  {
    # ---- the cache key is the RESOLVED UPSTREAM COMMIT, not a version. A cache keyed on a version would be a pin:
    # SAGE_REF defaults to main, main moves, and a stale wheel would be served forever: the "stale pin is the same
    # debt with a delay" this repo refuses. Keyed on the commit (plus the venv's python, torch and the GPU's sm, all
    # of which change the artefact) the cache can only ever skip rebuilding byte-identical source. ls-remote resolves
    # the ref in about a second, so a hit costs no clone at all. A 10-20 minute build becomes a ~20 second install.
    local cache="$BASE_STATE/wheels" sha key hit pytag tv
    local ref="${SAGE_REF:-main}" stampf="$VENV/.comfy-base-sageattention"
    sha="$(git ls-remote https://github.com/thu-ml/SageAttention "$ref" 2>/dev/null | awk 'NR==1{print substr($1,1,12)}')"
    pytag="$("$PY" -c 'import sys; print("cp%d%d" % sys.version_info[:2])' 2>/dev/null)"
    # 3.3.0: the key names torch's CUDA backend too (2.14.0 exists for cu130 and cu132 alike, and a build for one is
    # not a build for the other); the public version alone is what keys before 3.3.0 carried
    local tvpub tvfull
    tvfull="$("$PY" -c 'import torch; print(torch.__version__.replace("+", "-"))' 2>/dev/null)"; tvpub="${tvfull%%-*}"; tv="$tvfull"
    key="sageattention-${sha:-unresolved}-${pytag:-cp}-torch${tv:-0}-${SAGE_ARCHS:+archs-}$(printf '%s' "${SAGE_ARCHS:-${BASE_GPU_SM:-sm}}" | tr ';.' '_-')"
    # already built from exactly this source for exactly this venv: nothing to do. Anything else (main moved, a new
    # Python, a new torch, another GPU) rebuilds or reinstalls from the cache, so it is never left behind.
    local prefix="sageattention-${sha:-unresolved}-${pytag:-cp}-torch${tv:-0}-" smtok="${BASE_GPU_SM#sm_}" stamped
    smtok="${smtok%?}-${smtok: -1}"                                  # sm_120 → 12-0, the form SAGE_ARCHS takes in a key
    stamped="$(cat "$stampf" 2>/dev/null)"
    if [ -n "$sha" ] && { [ "$stamped" = "$key" ] || { [ -z "${SAGE_ARCHS:-}" ] && [ -n "${BASE_GPU_SM:-}" ] && _base_sage_covers "$stamped" "$prefix" "$smtok"; }; } \
       && "$PY" -c "import sageattention" >/dev/null 2>&1; then
      ok "sageattention $(_base_sage_ver) already built from SageAttention $sha for this venv (its newest)"; return 0
    fi
    # a build stamped with a pre-3.3.0 key for this very venv (same source, Python, torch, GPU; it imports with this
    # torch) is this venv's build: adopt it under the new key rather than spend 10 to 20 minutes rebuilding it
    if [ -n "$sha" ] && [ "$tvpub" != "$tvfull" ]; then
      local okey="sageattention-${sha}-${pytag:-cp}-torch${tvpub:-0}-${SAGE_ARCHS:+archs-}$(printf '%s' "${SAGE_ARCHS:-${BASE_GPU_SM:-sm}}" | tr ';.' '_-')"
      local oprefix="sageattention-${sha}-${pytag:-cp}-torch${tvpub:-0}-"
      if { [ "$stamped" = "$okey" ] || { [ -z "${SAGE_ARCHS:-}" ] && [ -n "${BASE_GPU_SM:-}" ] && _base_sage_covers "$stamped" "$oprefix" "$smtok"; }; } \
         && "$PY" -c "import sageattention" >/dev/null 2>&1; then
        echo "${prefix}${stamped#"$oprefix"}" > "$stampf"
        ok "sageattention $(_base_sage_ver) already built from SageAttention $sha for this venv (its newest; the key now names ${tvfull#*-})"; return 0
      fi
    fi
    local cc="${BASE_GPU_SM:-}"; cc="${cc#sm_}"
    if [ -n "${SAGE_ARCHS:-}" ]; then cc="$SAGE_ARCHS"             # 2.1.0: the image build has no GPU and wants several ("9.0;12.0")
    else
      if [ -z "$cc" ]; then err "no CUDA device visible: SageAttention needs the GPU's compute capability to build (or SAGE_ARCHS=<list>)"; BASE_FAILED+=("SageAttention: no GPU visible, not built"); return 0; fi
      cc="${cc%?}.${cc: -1}"                                        # sm_120 → 12.0 · sm_100 → 10.0 · sm_89 → 8.9
    fi
    base_cuda_toolchain
    if [ "$BASE_CUDA_BUILD_OK" != "1" ]; then                       # 7 minutes of nvcc to reach a link error we can predict
      err "SageAttention links cuBLAS and this toolkit cannot — not starting a build that cannot finish"
      BASE_FAILED+=("SageAttention: the CUDA toolkit cannot link cuBLAS (the toolchain line above names the fix)"); return 0
    fi
    hit=""; [ -n "$sha" ] && hit="$(ls "$cache/$key"/*.whl 2>/dev/null | head -1)"
    if [ -z "$hit" ] && [ -n "$sha" ] && [ -z "${SAGE_ARCHS:-}" ] && [ -n "${BASE_GPU_SM:-}" ]; then   # 3.0.1: a wheel built for a list holding this GPU
      local cd_
      for cd_ in "$cache/${prefix}archs-"*; do
        [ -d "$cd_" ] || continue
        if _base_sage_covers "$(basename "$cd_")" "$prefix" "$smtok" && ls "$cd_"/*.whl >/dev/null 2>&1; then
          key="$(basename "$cd_")"; hit="$(ls "$cd_"/*.whl | head -1)"; break
        fi
      done
    fi
    if [ -n "$hit" ]; then
      ok "SageAttention: a cached wheel matches this exact source and venv — installing instead of a 10-20 min build"
      note "$(basename "$hit")  ($key)"
      _base_pip install -q --force-reinstall --no-deps "$hit" 2>&1 | tail -2 || true
      if "$PY" -c "import sageattention" >/dev/null 2>&1; then
        echo "$key" > "$stampf"; BASE_CHANGED+=("SageAttention installed from the cached wheel ($sha)"); return 0
      fi
      warn "the cached wheel did not import — rebuilding from source and replacing it"; rm -rf "$cache/$key"
    fi
    _base_pip install -q --upgrade ninja 2>&1 | tail -1 || true       # the build's generator, at its newest
    # torch 2.14's headers demand C++20 and upstream's setup.py hardcodes -std=c++17: every kernel failed at the first
    # include on the pod (2.0.25). Clone the ref, patch the flag, build from the checkout — pip sees the patched tree.
    local src="$BASE_STATE/sageattention-src"; rm -rf "$src"
    # `--branch` takes a branch or a tag and refuses a commit ("Remote branch <sha> not found in upstream
    # origin"), so SAGE_REF=<sha> failed here for as long as the knob has existed. A commit is fetched by object
    # name instead. The DEFAULT stays main on purpose: the cache is keyed on the resolved commit precisely so
    # that tracking upstream costs nothing, and a version-keyed cache would be the stale pin this repo refuses.
    local got=0
    if git clone -q --depth 1 --branch "$ref" https://github.com/thu-ml/SageAttention "$src" 2>&1 | tail -2; then got=1; fi
    if [ "$got" != "1" ]; then
      err "could not clone SageAttention ($ref)"; BASE_FAILED+=("SageAttention: clone failed"); return 0
    fi
    sed -i.bak 's/-std=c++17/-std=c++20/g' "$src/setup.py" && rm -f "$src/setup.py.bak"
    note "SageAttention $(git -C "$src" rev-parse --short HEAD 2>/dev/null) — setup.py patched: -std=c++17 → -std=c++20 (torch ≥ 2.14 headers)"
    echo "  building SageAttention from source for ${SAGE_ARCHS:-$BASE_GPU_SM} (TORCH_CUDA_ARCH_LIST=$cc, MAX_JOBS=${MAX_JOBS:-32}; 10–20 min, a heartbeat prints while it runs)"
    # built as a WHEEL, not installed straight in: the wheel is the artefact the next venv rebuild reuses
    mkdir -p "$cache/$key"
    if ! TORCH_CUDA_ARCH_LIST="$cc" MAX_JOBS="${MAX_JOBS:-32}" CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}" PATH="/usr/local/cuda/bin:$PATH" \
         _base_run_watched "SageAttention build" _base_pip wheel --no-build-isolation --no-deps -w "$cache/$key" "$src"; then
      err "the SageAttention build failed (its last lines are above)"; rm -rf "$cache/$key"
    else
      hit="$(ls "$cache/$key"/*.whl 2>/dev/null | head -1)"
      if [ -n "$hit" ]; then _base_pip install -q --force-reinstall --no-deps "$hit" 2>&1 | tail -2 || true
        echo "$key" > "$stampf"; ok "wheel cached at $cache/$key: the next venv rebuild on this pod skips the build"
      else err "the build produced no wheel"; rm -rf "$cache/$key"; fi
    fi
    rm -rf "$src"
  }
  if "$PY" -c "import sageattention" >/dev/null 2>&1; then ok "sageattention $(_base_sage_ver) — built for $BASE_GPU_SM; a ⚡ Sage patch may go ON"
  else err "sageattention does not import in $VENV: a Sage patch node would fail at queue time"; BASE_FAILED+=("SageAttention not installed"); fi
  return 0
}

base_consolidate(){ # what is already installed elsewhere on this pod: strays of ours moved in, the rest reported
  hdr "CONSOLIDATE · what is already installed on this pod"
  local VOLROOT="${BASE_PERSIST_ROOT:-$BASE_VOLUME}"; [ -n "$BASE_FAKE_ROOT" ] && VOLROOT="$BASE_FAKE_ROOT"
  local d name url sha cnr why branch other rows
  # ---- other ComfyUI trees: reported, never touched
  local others=()
  while IFS= read -r d; do
    if [ -n "$d" ] && [ -f "$d/main.py" ] && [ -d "$d/comfy" ] && [ "$d" != "$COMFY" ]; then others+=("$d"); fi
  done < <(_base_find_dirs 'ComfyUI' 'ComfyUI-*' 'comfyui')
  if [ "${#others[@]}" -gt 0 ]; then
    for other in "${others[@]}"; do warn "another ComfyUI tree: $other ($(_base_bytes_to_gb "$(_base_dir_bytes "$other")") GB) — this run uses $COMFY; models may be split across two libraries"; done
  else ok "no second ComfyUI tree"; fi
  # ---- stray node packs: a pack of ours cloned somewhere other than custom_nodes is invisible to ComfyUI
  local moved=0 dupe=0
  rows="$(_base_pack_rows 2>/dev/null || true)"
  while IFS='|' read -r name url sha cnr why branch; do
    [ -n "$name" ] || continue
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      case "$d/" in "$CN"/*) continue;; esac
      if [ ! -f "$d/__init__.py" ] && [ ! -d "$d/.git" ]; then continue; fi
      if [ -e "$CN/$name" ]; then
        case "$d/" in "$VOLROOT"/*) ;; *) note "$name also at $d — the image's own copy, outside the volume (left alone; ComfyUI loads $CN only)"; continue;; esac
        miss "$name also at $d — duplicate of $CN/$name"
        if [ "$BASE_DRY" = "1" ]; then would "queue $d for deletion"; else DEL_DIRS+=("$d"); DEL_BYTES=$((DEL_BYTES + $(_base_dir_bytes "$d"))); dupe=$((dupe + 1)); fi
      elif [ "$BASE_DRY" = "1" ]; then would "move $d → $CN/$name"
      else
        mkdir -p "$CN"
        if mv "$d" "$CN/$name" 2>/dev/null; then ok "$name moved into custom_nodes ($d → $CN/$name)"; moved=$((moved + 1))
        else warn "stray pack not moved: $d"; fi
      fi
    done < <(_base_find_dirs "$name")
  done <<< "$rows"
  if [ "$moved" = "0" ] && [ "$dupe" = "0" ]; then ok "no stray node packs outside custom_nodes"; fi
  # ---- orphaned venvs and backups: reported with sizes, never removed here
  local vfound=0 vb
  while IFS= read -r d; do
    if [ -z "$d" ] || [ "$d" = "$VENV" ]; then continue; fi
    vb="$(_base_dir_bytes "$d")"; vfound=$((vfound + 1))
    case "$d" in
      *.pre-*) note "venv backup: $d ($(_base_bytes_to_gb "$vb") GB) — kept as the rollback for $VENV; rm -rf '$d' to reclaim it";;
      *)       warn "other venv: $d ($(_base_bytes_to_gb "$vb") GB) — not touched; remove it by hand if it is dead";;
    esac
  done < <(_base_find_dirs '.venv-*' '.venv' '*.pre-*')
  if [ "$vfound" = "0" ]; then ok "no orphaned venvs or old backups"; fi
  return 0
}

base_list_packs(){ # <pkg dir>... → deduplicated dir|url|sha|branch rows (base packs + every package's; branch empty unless the row
  # names one, 3.6.1); exit 1 when one pack names two URLs or two branches.
  # 3.0.0: a row's sha is a last-tested RECORD and every run takes the pack to its HEAD, so two packages whose records
  # were written on different days is a note, never a conflict. Two URLs (or two tracked branches) for one directory is
  # still a real conflict: the machine can hold one checkout.
  local d s out rc=0
  out="$(printf '%s\n' "${BASE_PACKS[@]}")"
  for d in "$@"; do
    d="${d%/}"; s="$(ls "$d"/*"-script.sh" 2>/dev/null | head -1 || true)"
    if [ -z "$s" ]; then echo "  !! no '*-script.sh' in $d" >&2; rc=1; continue; fi
    if [ "$(cd "$d" && pwd -P)" = "$BASE_DIR" ]; then continue; fi                       # the base's own entry script is not a package
    if ! grep -q 'base_main' "$s"; then echo "  ○ $(basename "$s") does not source the base yet — its packs are not derived" >&2; continue; fi
    local got; got="$(BASE_DECLARE_ONLY=1 bash "$s" | grep '^PACKROW ' | cut -c9- || true)"
    if [ -n "$got" ]; then out="$out"$'\n'"$got"; fi
  done
  printf '%s\n' "$out" | awk -F'|' 'function u(x){ sub(/\.git$/,"",x); sub(/\/$/,"",x); return tolower(x) } NF>=3 && $1!="" { b=(NF>=6 ? $6 : ""); if ($1 in url) { if (u(url[$1])!=u($2)) { print "  !! " $1 " comes from two URLs: " url[$1] " vs " $2 > "/dev/stderr"; bad=1 } else if (br[$1]!=b) { print "  !! " $1 " tracks two branches: " (br[$1]=="" ? "its HEAD" : br[$1]) " vs " (b=="" ? "its HEAD" : b) > "/dev/stderr"; bad=1 } else if (sha[$1]!=$3) { print "  ~ " $1 ": records differ (" substr(sha[$1],1,12) " vs " substr($3,1,12) "); every run takes it to its HEAD" > "/dev/stderr" } } else { url[$1]=$2; sha[$1]=$3; br[$1]=b; print $1 "|" $2 "|" $3 "|" b } } END { exit bad }' || rc=1
  return $rc
}
