# 70-hygiene.sh — the base-owned comfyui_args.txt (state/) edited by flag AND value, ComfyUI-Manager's security level,
# extra_model_paths.yaml registrations. The base owns the preview flags and the API-node switch; packages add their own
# through HYGIENE_ARGS and drop an image's through HYGIENE_ARGS_REMOVE. An image's own args file is imported once.

_base_args_import(){ # the base file is created once — from the image's own args file when there is one, else empty
  [ -f "$ARGS_FILE" ] && return 0
  local hdr="# comfy-base launch args — the only source of ComfyUI's flags on this pod; edit here, never an image's file"
  if [ "$BASE_DRY" = "1" ]; then
    if [ -n "${ARGS_IMPORT:-}" ]; then would "create $ARGS_FILE by importing ${ARGS_IMPORT} (once; that file is never read again)"
    else would "create $ARGS_FILE"; fi
    return 0
  fi
  mkdir -p "$(dirname "$ARGS_FILE")"
  if [ -n "${ARGS_IMPORT:-}" ]; then
    { echo "$hdr"; echo "# imported from $ARGS_IMPORT on $(_base_ts)"; grep -v '^#' "$ARGS_IMPORT" || true; } > "$ARGS_FILE"
    BASE_CHANGED+=("args: imported $ARGS_IMPORT"); ok "args: imported $ARGS_IMPORT into $ARGS_FILE (once; that file is never read again)"
  else
    echo "$hdr" > "$ARGS_FILE"; BASE_CHANGED+=("args: created $ARGS_FILE"); ok "args: created $ARGS_FILE"
  fi
}

_base_args_ensure(){ # <--flag> [value] — present with that value, or made so; idempotent
  local name="$1" val="${2:-}" cur flag="$1${2:+ $2}"
  if [ ! -f "$ARGS_FILE" ]; then
    if [ "$BASE_DRY" = "1" ]; then would "create $ARGS_FILE with '$flag'"; return 0; fi
    mkdir -p "$(dirname "$ARGS_FILE")"; printf '%s\n' "$flag" > "$ARGS_FILE"; BASE_CHANGED+=("created $ARGS_FILE with $flag"); ok "args: created with $flag"; return 0
  fi
  if [ -z "$val" ]; then
    if grep -qE -- "(^|[[:space:]])${name}([[:space:]]|$)" "$ARGS_FILE"; then ok "args: $name present"
    elif [ "$BASE_DRY" = "1" ]; then would "append '$flag' to comfyui_args.txt"
    else printf '%s\n' "$flag" >> "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: + $flag"); ok "args: + $flag"; fi
    return 0
  fi
  # a flag is its name AND its value: `--preview-method latent2rgb` is not "--preview-method present"
  cur="$(sed -nE "s#.*${name}[[:space:]]+([^[:space:]]+).*#\\1#p" "$ARGS_FILE" | head -1 || true)"
  if [ "$cur" = "$val" ]; then ok "args: $flag present"
  elif [ -n "$cur" ]; then
    if [ "$BASE_DRY" = "1" ]; then would "change $name from '$cur' to '$val' in comfyui_args.txt"
    else sed -E "s#(${name})[[:space:]]+[^[:space:]]+#\\1 ${val}#" "$ARGS_FILE" | _base_replace_file "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: $name $cur -> $val"); ok "args: $name corrected from '$cur' to '$val'"; fi
  elif [ "$BASE_DRY" = "1" ]; then would "append '$flag' to comfyui_args.txt"
  else printf '%s\n' "$flag" >> "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: + $flag"); ok "args: + $flag"; fi
}
_base_args_remove(){ # <--flag> [why]
  local name="$1" why="${2:-}"
  [ -f "$ARGS_FILE" ] || return 0
  grep -qE -- "(^|[[:space:]])${name}([[:space:]]|$)" "$ARGS_FILE" || return 0
  if [ "$BASE_DRY" = "1" ]; then would "remove $name from comfyui_args.txt${why:+ ($why)}"; return 0; fi
  sed -E "s/(^|[[:space:]])${name}([[:space:]]|$)/\1\2/g" "$ARGS_FILE" | awk 'NF || /^#/' | _base_replace_file "$ARGS_FILE"
  BASE_CHANGED+=("comfyui_args.txt: removed $name"); ok "args: removed $name${why:+ ($why)}"
}
_base_args_value(){ # <--flag> → the value the args file gives it, or nothing
  [ -f "$ARGS_FILE" ] || return 0
  sed -nE "s#(^|.*[[:space:]])${1}[[:space:]]+([^[:space:]]+).*#\\2#p" "$ARGS_FILE" | head -1
}
_base_args_remove_valued(){ # <--flag> [why]: the flag AND its value (a bare _base_args_remove would leave the value as a stray word)
  local name="$1" why="${2:-}" cur
  cur="$(_base_args_value "$name")"; [ -n "$cur" ] || return 0
  if [ "$BASE_DRY" = "1" ]; then would "remove $name $cur from comfyui_args.txt${why:+ ($why)}"; return 0; fi
  sed -E "s#(^|[[:space:]])${name}[[:space:]]+[^[:space:]]+#\\1#g" "$ARGS_FILE" | awk 'NF || /^#/' | _base_replace_file "$ARGS_FILE"
  BASE_CHANGED+=("comfyui_args.txt: removed $name $cur"); ok "args: removed $name $cur${why:+ ($why)}"
}
# 3.28.0: HYGIENE_ARGS+=( "--cache-ram 10 32" ) in a package script puts `--cache-ram 10 32` in the args file: the headroom,
# in GB, of ComfyUI's RAM-pressure cache, the active cache's first and the inactive cache's second (comfy/cli_args.py: at
# most two values). The one row that carries two values (_base_args_ensure keeps one, so it would land as `--cache-ram 10`);
# with no such row the base writes nothing. One or two values, each a number of GB. It is never written beside
# --cache-classic, --cache-lru or --cache-none (one argparse group: ComfyUI would refuse to start), nor for a ComfyUI that
# has no --cache-ram; HYGIENE_ARGS_REMOVE takes it out with both its values.
_base_cache_ram_ok(){ [[ "${1:-}" =~ ^[0-9]+(\.[0-9]+)?([[:space:]]+[0-9]+(\.[0-9]+)?)?$ ]]; }
_base_args_cache_ram_value(){ # → the values --cache-ram has in the args file, space-separated, or nothing
  [ -f "$ARGS_FILE" ] || return 0
  sed -nE 's#(^|.*[[:space:]])--cache-ram(([[:space:]]+[0-9.]+){0,2}).*#\2#p' "$ARGS_FILE" | head -1 | awk '{$1=$1; print}'
}
_base_args_cache_ram_drop(){ # the args file without --cache-ram and its values, on stdout
  sed -E -e 's#(^|[[:space:]])--cache-ram([[:space:]]+[0-9.]+){0,2}#\1#g' -e 's#[[:space:]]+$##' "$ARGS_FILE" | awk 'NF || /^#/'
}
_base_args_cache_ram(){ # <one or two numbers of GB>: --cache-ram with them, or made so; idempotent
  local want cur other
  want="$(printf '%s' "${1:-}" | awk '{$1=$1; print}')"
  if ! _base_cache_ram_ok "$want"; then warn "args: '--cache-ram ${1:-}' is not one or two numbers of GB: left off"; return 0; fi
  if ! grep -q -- '"--cache-ram"' "$COMFY/comfy/cli_args.py" 2>/dev/null; then note "args: --cache-ram $want left off: this ComfyUI has no --cache-ram"; return 0; fi
  for other in --cache-classic --cache-lru --cache-none; do
    if [ -f "$ARGS_FILE" ] && grep -qE -- "(^|[[:space:]])${other}([[:space:]]|$)" "$ARGS_FILE"; then
      warn "args: --cache-ram $want left off: the args file sets $other, and ComfyUI takes one cache mode"; return 0
    fi
  done
  cur="$(_base_args_cache_ram_value)"
  if grep -qE -- '(^|[[:space:]])--cache-ram([[:space:]]|$)' "$ARGS_FILE" 2>/dev/null && [ "$cur" = "$want" ]; then ok "args: --cache-ram $want present"; return 0; fi
  if [ "$BASE_DRY" = "1" ]; then would "set --cache-ram $want in comfyui_args.txt"; return 0; fi
  if [ -f "$ARGS_FILE" ]; then _base_args_cache_ram_drop | _base_replace_file "$ARGS_FILE"; else mkdir -p "$(dirname "$ARGS_FILE")"; fi
  printf '%s\n' "--cache-ram $want" >> "$ARGS_FILE"
  BASE_CHANGED+=("comfyui_args.txt: --cache-ram ${cur:-(none)} -> $want"); ok "args: --cache-ram $want"
}
_base_args_cache_ram_remove(){ # [why]: --cache-ram AND its values (a bare _base_args_remove would leave them as stray words)
  local why="${1:-}" cur
  grep -qE -- '(^|[[:space:]])--cache-ram([[:space:]]|$)' "$ARGS_FILE" 2>/dev/null || return 0
  cur="$(_base_args_cache_ram_value)"
  if [ "$BASE_DRY" = "1" ]; then would "remove --cache-ram $cur from comfyui_args.txt${why:+ ($why)}"; return 0; fi
  _base_args_cache_ram_drop | _base_replace_file "$ARGS_FILE"
  BASE_CHANGED+=("comfyui_args.txt: removed --cache-ram $cur"); ok "args: removed --cache-ram $cur${why:+ ($why)}"
}
_base_args_package(){ # the package's own rows: HYGIENE_ARGS made so, then HYGIENE_ARGS_REMOVE taken out
  local row name val
  for row in ${HYGIENE_ARGS[@]+"${HYGIENE_ARGS[@]}"}; do
    name="${row%% *}"; val=""; [ "$row" != "$name" ] && val="${row#* }"
    case "$name" in --cache-ram) _base_args_cache_ram "$val"; continue ;; esac   # 3.28.0: its one or two values
    _base_args_ensure "$name" $val
  done
  for name in ${HYGIENE_ARGS_REMOVE[@]+"${HYGIENE_ARGS_REMOVE[@]}"}; do
    case "$name" in --cache-ram) _base_args_cache_ram_remove "removed by ${PKG_NAME:-the package}"; continue ;; esac
    _base_args_remove "$name" "removed by ${PKG_NAME:-the package}"
  done
}
_base_args_store_dirs(){ # 3.0.3: with no shared library, output/ and input/ are ComfyUI's own, inside the tree, on the store
  # MEASURED on a Verda machine on a shared store: the args file kept --output-directory and --input-directory from the
  # days its library was a second mount. 2.5.2 stopped using that library under a shared root but only ever ENSURED the
  # flags and never took them back, so ComfyUI made the old mount point on the OS disk and wrote every render and upload
  # there, off the store. A value off the store is removed; a package that wants one elsewhere says so in HYGIENE_ARGS,
  # which is applied after this.
  local store="${BASE_FAKE_ROOT:-$BASE_VOLUME}" name cur row
  for name in --output-directory --input-directory; do
    for row in ${HYGIENE_ARGS[@]+"${HYGIENE_ARGS[@]}"}; do [ "${row%% *}" = "$name" ] && continue 2; done   # the package's own choice
    cur="$(_base_args_value "$name")"; [ -n "$cur" ] || continue
    case "${cur%/}" in "$store"|"$store"/*) ok "args: $name $cur is on the store" ;;
      *) _base_args_remove_valued "$name" "not on the store $store: ComfyUI writes inside its tree instead" ;; esac
  done
}
_base_manager_security(){ # security_level = normal; network_mode and everything else untouched
  local ini="" c
  for c in "$(_base_user_dir)/default/ComfyUI-Manager/config.ini" "$COMFY/user/default/ComfyUI-Manager/config.ini" "$CN/ComfyUI-Manager/config.ini"; do if [ -f "$c" ]; then ini="$c"; break; fi; done
  if [ -z "$ini" ]; then note "ComfyUI-Manager config.ini not found yet (Manager writes it on first start) — security_level is set on the next run"
  elif grep -qE '^security_level[[:space:]]*=[[:space:]]*normal[[:space:]]*$' "$ini"; then ok "Manager security_level = normal ($ini)"
  elif [ "$BASE_DRY" = "1" ]; then would "set security_level = normal in $ini"
  else
    if grep -q '^security_level' "$ini"; then sed -E 's/^security_level[[:space:]]*=.*/security_level = normal/' "$ini" | _base_replace_file "$ini"
    else awk '{print} /^\[default\]/ && !done {print "security_level = normal"; done=1}' "$ini" | _base_replace_file "$ini"; fi
    BASE_CHANGED+=("ComfyUI-Manager config.ini: security_level = normal"); ok "Manager security_level = normal"
  fi
}
# 3.17.1 (error log row 81): frontend 1.55.x opens a full-screen "Welcome to Apps" tour over the App Mode form on a user's
# first visit, and every package opens in App Mode. "Comfy.OnboardingCoachmarks.Seen" gains "appMode" (the list kept, every
# other key untouched; py/comfy_settings.py has the first-party sources) in the settings file the SERVER reads:
# _base_user_dir, the same folder the workflow lands in, so wherever there is a workflow to open there is a seed beside it.
_base_settings_seed(){
  local f; f="$(_base_user_dir)/default/comfy.settings.json"
  local py="${SYS_PY:-}"; [ -n "$py" ] && command -v "$py" >/dev/null 2>&1 || py="$(command -v python3 || true)"
  if [ -z "$py" ]; then warn "comfy.settings.json: no python to seed the App Mode tour as seen ($f)"; return 0; fi
  local out rc=0 dry=(); [ "$BASE_DRY" = "1" ] && dry=(--dry)
  out="$("$py" "$BASE_DIR/py/comfy_settings.py" "$f" ${dry[@]+"${dry[@]}"} 2>&1)" || rc=$?
  case "$rc:$out" in
    0:seeded*)     BASE_CHANGED+=("comfy.settings.json: the App Mode tour marked seen"); ok "comfy.settings.json: the App Mode tour marked seen ($f)";;
    0:already*)    ok "comfy.settings.json: the App Mode tour is marked seen";;
    0:would*)      would "mark the App Mode tour seen in $f";;
    *)             warn "comfy.settings.json: ${out:-exit $rc} (the App Mode tour may open over the form)";;
  esac
  return 0
}
_base_yaml_register(){ # <section> <key> <path> — register a folder in extra_model_paths.yaml once; a foreign entry is left alone
  local section="$1" key="$2" path="$3" y="${EXTRA_YAML:-$COMFY/extra_model_paths.yaml}"
  if [ -f "$y" ]; then
    if grep -qE "^[[:space:]]+${key}:" "$y"; then
      if grep -qF -- "$path" "$y"; then ok "extra_model_paths.yaml registers $key"
      else warn "extra_model_paths.yaml already has a $key entry pointing elsewhere — left alone"; fi
    elif [ "$BASE_DRY" = "1" ]; then would "append a $section: section with $key: $path to $y"
    else
      [ -n "$(tail -c1 "$y")" ] && printf '\n' >> "$y"
      printf '\n%s:\n    %s: %s\n' "$section" "$key" "$path" >> "$y"
      BASE_CHANGED+=("extra_model_paths.yaml: + $section.$key"); ok "extra_model_paths.yaml: + $key"
    fi
  elif [ "$BASE_DRY" = "1" ]; then would "create $y with a $section: section ($key)"
  else printf '%s:\n    %s: %s\n' "$section" "$key" "$path" > "$y"; BASE_CHANGED+=("created extra_model_paths.yaml ($section.$key)"); ok "created $y"; fi
}

_base_yaml_library(){ # 2.4.0: the shared library as ComfyUI's FIRST extra base_path: the fallback when models/ cannot be a link
  local y="${EXTRA_YAML:-$COMFY/extra_model_paths.yaml}" lib="$BASE_LIBRARY/models" cat_keys tmp
  if [ -f "$y" ] && grep -q '^comfy-library:' "$y"; then
    if grep -qF "base_path: $lib" "$y"; then ok "extra_model_paths.yaml: comfy-library -> $lib"; return 0; fi
    warn "extra_model_paths.yaml has a comfy-library section pointing elsewhere, left alone"; return 0
  fi
  if [ "$BASE_DRY" = "1" ]; then would "register the shared library $lib as the first base_path in $y"; return 0; fi
  # FIRST in the file, because base_discover reads the first base_path it finds. is_default sends ComfyUI's own
  # downloads here too. The freeform categories (LLM, RMBG, SEEDVR2, sams, grounding-dino, depthanything,
  # insightface) are listed as well: packs that ask folder_paths for them find the shared copy.
  tmp="$(mktemp)"
  {
    printf 'comfy-library:\n    base_path: %s\n    is_default: true\n' "$lib"
    for cat_keys in checkpoints diffusion_models text_encoders clip clip_vision vae loras controlnet embeddings \
                    upscale_models latent_upscale_models sams depthanything grounding-dino insightface \
                    LLM RMBG SEEDVR2 style_models diffusers vae_approx; do
      printf '    %s: %s\n' "$cat_keys" "$cat_keys"
    done
    if [ -f "$y" ]; then printf '\n'; cat "$y"; fi
  } > "$tmp"
  mv "$tmp" "$y"
  BASE_CHANGED+=("extra_model_paths.yaml: + comfy-library -> $lib")
  ok "extra_model_paths.yaml: comfy-library -> $lib (first base_path)"
}

_base_yaml_library_drop(){ # the link and a search path to the SAME place would list every model twice
  # Reachable on the migration this very function tells people to run: a first run finds real models in
  # ComfyUI's tree, refuses to link and registers the library as a search path; the operator moves the models and
  # runs again; the second run links. Without this, both are now true and every dropdown is doubled.
  local y="${EXTRA_YAML:-$COMFY/extra_model_paths.yaml}"
  [ -f "$y" ] || return 0
  grep -q '^comfy-library:' "$y" || return 0
  if [ "$BASE_DRY" = "1" ]; then would "remove the now-redundant comfy-library section from $y"; return 0; fi
  awk '/^comfy-library:/{skip=1; next} skip && /^[^[:space:]]/{skip=0} !skip{print}' "$y" | _base_replace_file "$y"
  grep -q '[^[:space:]]' "$y" || rm -f "$y"        # a file holding nothing but blank lines is noise
  BASE_CHANGED+=("extra_model_paths.yaml: - comfy-library (models/ IS the library now)")
  ok "extra_model_paths.yaml: comfy-library removed, models/ IS the library"
}

_base_library_link(){ # 2.4.0: make ComfyUI's own models/ BE the shared library
  # Why a link and not extra_model_paths alone: several packs this base installs never ask folder_paths where
  # their weights are: they join folder_paths.models_dir with their own folder name (RMBG, SeedVR2,
  # DepthAnythingV2, insightface's FaceAnalysis root). An extra search path is invisible to those, and they would
  # re-download 21 GB onto each machine's own disk. A link moves models_dir itself, so every consumer agrees.
  [ -n "$BASE_LIBRARY" ] || return 0
  local local_models="$COMFY/models" lib="$BASE_LIBRARY/models" n
  mkdir -p "$lib" 2>/dev/null || true
  if [ -L "$local_models" ]; then
    if [ "$(readlink "$local_models")" = "$lib" ]; then ok "models/ is the shared library ($lib)"; _base_yaml_library_drop; return 0; fi
    warn "$local_models is a symlink to $(readlink "$local_models"), not the library, left alone"
    _base_yaml_library; return 0
  fi
  if [ -d "$local_models" ]; then
    # "holds files" is NOT "holds models": ComfyUI's own tree ships a placeholder in every category folder
    # (put_checkpoints_here and friends), so a freshly materialised models/ is never empty. Counting those as
    # content made the refusal below fire on exactly the fresh machine this is for, which would have sent every
    # machine back to its own copy of the library, silently and looking like it worked.
    # ComfyUI's own tree ships more than the placeholders: models/configs/ carries v1-inference*.yaml and friends.
    # Counting those as content made the refusal fire on every fresh machine and print advice nobody can act on,
    # because the files it objects to are ComfyUI's, not the operator's. Measured on a live machine (2.5.1).
    n="$(find "$local_models" -mindepth 1 -type f ! -name 'put_*_here' ! -name '.gitkeep' ! -name '.DS_Store' \
         ! -name '*.yaml' ! -name '*.yml' ! -name 'README*' ! -name '*.md' 2>/dev/null | head -1)"
    if [ -n "$n" ]; then
      # never move a machine's models without being asked: say exactly what to run, and register the library as a
      # search path meanwhile so the standard categories at least resolve
      warn "$local_models still holds files of its own (e.g. $(basename "$n")), so it was not replaced by a link to the shared library."
      warn "  move them once, then re-run this script:  rsync -a --remove-source-files '$local_models/' '$lib/'"
      _base_yaml_library
      return 0
    fi
    if [ "$BASE_DRY" = "1" ]; then would "replace $local_models (ComfyUI's own scaffolding only) with a symlink to $lib"; return 0; fi
    # Everything left here is ComfyUI's own: the placeholders and models/configs/*.yaml. COPY it onto the library
    # first, without clobbering, so the shared tree ends up looking exactly like a ComfyUI models/ should and the
    # configs are shared like everything else. Only then take the local tree down: the placeholders by name, and
    # the directories bottom up while they are empty. Never `rm -rf` a path built from a variable.
    cp -Rn "$local_models/." "$lib/" 2>/dev/null || true
    find "$local_models" -type f \( -name 'put_*_here' -o -name '.gitkeep' -o -name '.DS_Store' \
         -o -name '*.yaml' -o -name '*.yml' -o -name 'README*' -o -name '*.md' \) -delete 2>/dev/null
    find "$local_models" -depth -type d -empty -delete 2>/dev/null
    if [ -e "$local_models" ]; then
      warn "could not clear $local_models (something is still in it) - the shared library is a search path instead"
      _base_yaml_library; return 0
    fi
  fi
  if [ "$BASE_DRY" = "1" ]; then would "link $local_models -> $lib"; return 0; fi
  if ln -s "$lib" "$local_models" 2>/dev/null; then
    BASE_CHANGED+=("models/ -> $lib (shared library)"); ok "models/ -> $lib (shared library)"
    _base_yaml_library_drop
  else
    warn "could not link $local_models -> $lib"; _base_yaml_library
  fi
  return 0
}

base_hygiene(){
  hdr "HYGIENE · comfyui_args.txt · ComfyUI-Manager"
  local before=${#BASE_CHANGED[@]} row name val
  _base_args_import
  # previews on, as high as they go: auto uses the family's TAESD decoder when one is installed and falls back
  # to latent2rgb on its own; no API nodes — nothing on this pod calls a third-party inference API
  _base_args_ensure --preview-method auto
  _base_args_ensure --preview-size "${BASE_PREVIEW_SIZE:-1024}"
  _base_args_ensure --disable-api-nodes
  # 2.4.0: with a shared library, ComfyUI's output/ and input/ live on it too, so a still one machine renders is
  # there for the machine that animates it. user/ and temp/ stay local on purpose: user/ holds the saved
  # workflows App Mode opens and each machine has only its own package, and temp/ is churn nobody shares.
  if [ -n "$BASE_LIBRARY" ]; then
    _base_args_ensure --output-directory "$BASE_LIBRARY/output"
    _base_args_ensure --input-directory "$BASE_LIBRARY/input"
    if [ "$BASE_DRY" != "1" ]; then mkdir -p "$BASE_LIBRARY/output" "$BASE_LIBRARY/input" 2>/dev/null || true; fi
  else
    _base_args_store_dirs
  fi
  # 2.5.0: with a SHARED workspace, ComfyUI's user/ and temp/ must NOT be shared. user/ holds the saved workflows
  # the App view opens and the frontend's settings, which are per machine and per package; temp/ is scratch two
  # servers would overwrite. Everything else on the root is meant to be shared, which is the point.
  if [ "$BASE_VOLUME_SHARED" = "1" ]; then
    _base_args_ensure --user-directory "$BASE_LOCAL_STATE/user"
    _base_args_ensure --temp-directory "$BASE_LOCAL_STATE/temp"
    if [ "$BASE_DRY" != "1" ]; then mkdir -p "$BASE_LOCAL_STATE/user/default/workflows" "$BASE_LOCAL_STATE/temp" 2>/dev/null || true; fi
  fi
  _base_library_link
  _base_args_package                   # HYGIENE_ARGS, then HYGIENE_ARGS_REMOVE (3.28.0: a --cache-ram row keeps both values)
  # the template ships --use-sage-attention; with no sageattention in the venv ComfyUI dies at start (and some
  # models render noise with it). Keep it only when a package's hook installed the module into this venv.
  # 3.7.5: and only when the last proof did not see sageattn() (the flag's own call) fail on this GPU: ComfyUI catches that
  # error and falls back to PyTorch attention on every call, so the flag buys nothing there but an error per step.
  if [ -x "$PY" ] && "$PY" -c "import sageattention" >/dev/null 2>&1; then
    local sv; sv="$(_base_sage_cached | sed -n 's/^SAGE_PROOF verdict=\([a-z]*\).*/\1/p')" || true
    if [ "$sv" = "only" ] || [ "$sv" = "none" ]; then _base_args_remove --use-sage-attention "sageattn() fails on ${BASE_GPU_SM:-this GPU} (bash <script> sage)"
    elif [ -f "$ARGS_FILE" ] && grep -q -- '--use-sage-attention' "$ARGS_FILE"; then ok "args: --use-sage-attention kept (sageattention imports in the venv)"; fi
  else _base_args_remove --use-sage-attention "sageattention is not importable in $VENV"; fi
  _base_manager_security
  _base_settings_seed
  if declare -F pkg_hygiene >/dev/null; then pkg_hygiene; fi
  if [ "${#BASE_CHANGED[@]}" -gt "$before" ]; then echo "  changed:"; printf '    • %s\n' "${BASE_CHANGED[@]:$before}"; fi
  return 0
}
