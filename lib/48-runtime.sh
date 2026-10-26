# 48-runtime.sh: the proven runtime (3.1.0): a BUYER's machine installs what its maintainer's own sweep proved,
# instead of building it. Paul's decision of 2026-09-25: a newbie's first run must never meet an untested upstream
# change, so on that one kind of machine the base installs the newest runtime that passed, and changes nothing else.
# Every other run stays "everything newest" (lib/47-latest.sh); nothing here runs unless BASE_RUNTIME is set or one
# of the two verbs is called.
#
#   runtime-capture <outdir> [--url-base <prefix>] [--part-bytes <n>]   on a machine after a green run: the parts and
#                                                                       runtime.json (py/runtime.py build)
#   runtime-apply <manifest path or https URL>                          on a buyer's machine: fetch, verify, unpack
#   BASE_RUNTIME=<state/runtime.json> <package script>                  install from it: no OS, git, pip or uv step
#
# The versions live ONLY in runtime.json. This file and py/runtime.py read them; neither writes one down.

_base_runtime_on(){ [ -n "${BASE_RUNTIME:-}" ]; }
_base_runtime_field(){ "$SYS_PY" "$BASE_DIR/py/runtime.py" field "$BASE_RUNTIME" "$1" 2>/dev/null || true; }
_base_runtime_skip(){ note "$1: runtime mode, from the proven runtime (${BASE_RUNTIME_NOTE:-runtime.json}); nothing is fetched or built"; }
_base_runtime_root(){ dirname "$BASE_HOME"; }            # /workspace on a machine, the fake root in the suite

_base_runtime_init(){ # base_init, when BASE_RUNTIME is set: the manifest must be the one runtime-apply unpacked
  local stamp want
  if [ ! -f "$BASE_RUNTIME" ]; then err "BASE_RUNTIME=$BASE_RUNTIME does not exist: run 'runtime-apply <manifest>' first"; exit 2; fi
  stamp="$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)"
  want="$(_base_sha256 "$BASE_RUNTIME" | awk '{print $1}')"
  if [ "$stamp" != "$want" ]; then err "BASE_RUNTIME names a manifest this machine has not applied (stamp ${stamp:-none}): run 'runtime-apply' with it first"; exit 2; fi
  BASE_RUNTIME_NOTE="created $(_base_runtime_field created), base $(_base_runtime_field base)"
  local dm; dm="$(_base_runtime_field driver_min)"
  if [[ "$dm" =~ ^[0-9]+$ ]] && { ! [[ "${DRIVER_MIN:-}" =~ ^[0-9]+$ ]] || [ "$dm" -gt "$DRIVER_MIN" ]; }; then DRIVER_MIN="$dm"; fi
  ok "runtime mode: $BASE_RUNTIME_NOTE; driver floor ${DRIVER_MIN:-580}"
}
_base_runtime_comfy(){ # in place of base_update_comfyui: the tree must be exactly the runtime's, packs included
  hdr "COMFYUI · the proven runtime"
  COMFY_OLD="$(_base_comfy_version "$COMFY" 2>/dev/null)"; COMFY_NEW="$COMFY_OLD"
  echo "  current      v$COMFY_OLD (runtime: $(_base_runtime_field comfyui.version))"
  local out rc=0
  out="$("$SYS_PY" "$BASE_DIR/py/runtime.py" check-tree "$BASE_RUNTIME" --comfy "$COMFY" 2>&1)" || rc=$?
  if [ "$rc" != 0 ]; then
    printf '%s\n' "$out" | sed 's/^/  /'
    err "the tree is not the runtime it was unpacked from: re-run 'runtime-apply' to restore it"
    BASE_FAILED+=("runtime: the tree differs from its manifest"); return 1
  fi
  ok "ComfyUI and every pack at the runtime's commits"
  local urls=() name url rest
  while IFS='|' read -r name url rest; do [ -n "$url" ] && urls+=("$url"); done <<< "$(_base_pack_rows 2>/dev/null || true)"
  if [ "${#urls[@]}" -gt 0 ] && ! out="$("$SYS_PY" "$BASE_DIR/py/runtime.py" covers "$BASE_RUNTIME" "${urls[@]}" 2>&1)"; then
    printf '%s\n' "$out" | sed 's/^/  /'
    err "${PKG_NAME:-this package} needs packs this runtime does not carry: it needs a newer runtime"
    BASE_FAILED+=("runtime: missing packs for ${PKG_ID:-the package}"); return 1
  fi
  return 0
}
_base_runtime_venv(){ # in place of base_venv: the runtime's venv must import torch; nothing is resolved or installed
  hdr "VENV · $VENV (the proven runtime)"
  local tv
  if [ ! -x "$PY" ]; then err "no interpreter at $PY: the runtime is incomplete, re-run 'runtime-apply'"; BASE_VENV_RESULT="failed (runtime)"; BASE_FAILED+=("runtime: no venv interpreter"); return 1; fi
  if ! tv="$("$PY" -c 'import torch; print(torch.__version__, torch.version.cuda)' 2>/dev/null)"; then
    err "the runtime's venv cannot import torch: re-run 'runtime-apply'"; BASE_VENV_RESULT="failed (runtime)"; BASE_FAILED+=("runtime: torch does not import"); return 1
  fi
  BASE_TORCH_INFO="$tv"; BASE_VENV_RESULT="runtime"; ok "venv from the runtime · torch $tv"
  return 0
}

base_runtime_apply(){ # <manifest path or https URL> → the runtime unpacked beside comfy-base, stamped; idempotent
  local src="${1:-}" root work man sum have name from rc=0 old="" ts
  if [ -z "$src" ]; then err "runtime-apply needs a manifest: runtime-apply <path or https URL of runtime.json>"; return 2; fi
  base_env_setup
  local drc=0; base_discover quiet || drc=$?
  if [ "$drc" = 3 ]; then base_discover || true; return 3; fi
  if [ "${BASE_VOLUME_SHARED:-0}" = "1" ]; then note "runtime-apply: a shared store is the operator's shape, which builds everything newest; a runtime is for a buyer's own machine"; return 0; fi
  hdr "RUNTIME · $src"
  root="$(_base_runtime_root)"; work="$root/.comfy-base-runtime"; mkdir -p "$work"; man="$work/runtime.json"
  case "$src" in
    https://*) if ! _base_curl_dl "$man" "$src"; then err "could not fetch the manifest $src"; return 1; fi ;;
    *) if ! cp "$src" "$man"; then err "no manifest at $src"; return 1; fi ;;
  esac
  sum="$(_base_sha256 "$man" | awk '{print $1}')"; have="$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)"
  if [ "$sum" = "$have" ] && [ -d "$root/$("$SYS_PY" "$BASE_DIR/py/runtime.py" field "$man" comfyui.dir)" ]; then
    cp "$man" "$BASE_STATE/runtime.json"; rm -rf "$work"
    ok "this runtime is already applied: install with BASE_RUNTIME=$BASE_STATE/runtime.json"; return 0
  fi
  local want_b
  while IFS=$'\t' read -r name from want_b; do
    [ -n "$name" ] || continue
    if [ -f "$work/$name" ] && [ "$(_base_fsize "$work/$name")" = "$want_b" ]; then continue; fi   # a part fetched before: verified below with the rest
    echo "  → $name"
    case "$from" in
      https://*) local attempt; for attempt in 1 2 3; do _base_curl_dl "$work/$name" "$from" && break; note "attempt $attempt failed (the transfer resumes)"; sleep 5; done ;;
      *) cp "$from" "$work/$name" || true ;;
    esac
  done < <("$SYS_PY" "$BASE_DIR/py/runtime.py" sources "$man" "$src")
  if ! "$SYS_PY" "$BASE_DIR/py/runtime.py" verify-parts "$man" "$work"; then err "the runtime's parts do not match its manifest: nothing was unpacked (re-run to resume)"; return 1; fi
  # an Update replaces the code wholesale: the old venv and packs go aside first, and come back if the unpack fails
  local cdir; cdir="$root/$("$SYS_PY" "$BASE_DIR/py/runtime.py" field "$man" comfyui.dir)"; ts="$(_base_ts)"
  if [ -n "$have" ] && [ -d "$cdir" ]; then
    old="$root/.comfy-base-runtime-old.$ts"; mkdir -p "$old"
    local d; for d in "$cdir"/.venv* "$cdir/custom_nodes"; do [ -e "$d" ] && mv "$d" "$old/" || true; done
  fi
  local parts=() total=0; while IFS=$'\t' read -r name from want_b; do if [ -n "$name" ]; then parts+=("$work/$name"); total=$((total + want_b)); fi; done < <("$SYS_PY" "$BASE_DIR/py/runtime.py" sources "$man" "$src")
  cat "${parts[@]}" | tar -xzf - -C "$root" || rc=$?
  if [ "$rc" != 0 ]; then
    err "unpacking the runtime failed (tar exit $rc)"
    if [ -n "$old" ]; then local d; for d in "$old"/.venv* "$old/custom_nodes"; do [ -e "$d" ] && { rm -rf "$cdir/$(basename "$d")"; mv "$d" "$cdir/"; } || true; done; note "the previous runtime's venv and packs are back"; fi
    return 1
  fi
  [ -n "$old" ] && rm -rf "$old"
  cp "$man" "$BASE_STATE/runtime.json"; echo "$sum" > "$BASE_STATE/runtime.stamp"
  rm -rf "$work"
  ok "runtime unpacked into $root ($(_base_bytes_to_gb "$total") GB packed); install with BASE_RUNTIME=$BASE_STATE/runtime.json"
  return 0
}

base_runtime_capture(){ # <outdir> [--url-base <prefix>] [--part-bytes <n>] → the runtime of this green machine, as parts + runtime.json
  local out="" url_base="" part="" f bad="" cuda
  while [ $# -gt 0 ]; do
    case "$1" in
      --url-base) url_base="${2:-}"; shift 2 ;;
      --part-bytes) part="${2:-}"; shift 2 ;;
      *) out="$1"; shift ;;
    esac
  done
  if [ -z "$out" ]; then err "runtime-capture needs an output directory"; return 2; fi
  base_env_setup
  if ! base_discover quiet; then err "runtime-capture: no ComfyUI tree to capture"; return 3; fi
  hdr "RUNTIME CAPTURE · $COMFY"
  # only a machine whose every package's last run was green is a proven runtime
  if ! ls "$BASE_STATE"/last-run/* >/dev/null 2>&1; then err "no install has run here: nothing is proven yet"; return 1; fi
  for f in "$BASE_STATE"/last-run/*; do if ! grep -q '^green' "$f"; then bad="$bad $(basename "$f")"; fi; done
  if [ -n "$bad" ]; then err "the last run was not green for:$bad; capture a machine whose installs all passed"; return 1; fi
  # 3.29.2: the driver floor is what the runtime's torch build needs (py/runtime.py driver_floor), never this machine's driver
  cuda="$("$PY" -c 'import torch; print(torch.version.cuda or "")' 2>/dev/null || true)"
  if [ -z "$cuda" ]; then err "runtime-capture: $PY has no CUDA torch: the driver floor cannot be derived"; return 1; fi
  "$SYS_PY" "$BASE_DIR/py/runtime.py" build --root "$(_base_runtime_root)" --comfy "$COMFY" --out "$out" \
    ${part:+--part-bytes "$part"} ${url_base:+--url-base "$url_base"} --torch-cuda "$cuda" --base-version "$BASE_VERSION" \
    --python "$("$PY" -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
    --sage-key "$(cat "$VENV/.comfy-base-sageattention" 2>/dev/null)"
}
