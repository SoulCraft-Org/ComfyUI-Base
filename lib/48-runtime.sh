# 48-runtime.sh: the proven runtime (3.1.0): a BUYER's machine installs what its maintainer's own sweep proved,
# instead of building it. Paul's decision of 2026-09-25: a newbie's first run must never meet an untested upstream
# change, so on that one kind of machine the base installs the newest runtime that passed, and changes nothing else.
# Every other run stays "everything newest" (lib/47-latest.sh); nothing here runs unless BASE_RUNTIME is set or one
# of the two verbs is called.
#
#   runtime-capture <outdir> [--url-base <prefix>] [--part-bytes <n>]   on a machine after a green run: independent zstd
#                                                                       parts and runtime.json (py/runtime.py build)
#   runtime-apply <manifest path or https URL> [--parts <links.json>] [--jobs <n>]
#                                                                       on a buyer's machine: fetch <n> parts at once (8),
#                                                                       verify each, unpack each as it lands
#   BASE_RUNTIME=<state/runtime.json> <package script>                  install from it: no OS, git, pip or uv step
#
# The versions live ONLY in runtime.json. This file and py/runtime.py read them; neither writes one down.
#
# 3.29.4: the models do not wait for the unpack. While `runtime-apply` runs it holds $BASE_STATE/runtime.applying
# ("<pid> <manifest sha256>"; a dead pid is no lock) and has already put the manifest at state/runtime.json; an install
# with BASE_RUNTIME set then finds its manifest "pending", starts the package's model downloads at once where the tree
# will be, and waits for the stamp only where it first needs the tree, the venv or a pack. Pod to App ready becomes
# max(unpack, first models) plus one ComfyUI start.

_base_runtime_on(){ [ -n "${BASE_RUNTIME:-}" ]; }
_base_runtime_field(){ "$SYS_PY" "$BASE_DIR/py/runtime.py" field "$BASE_RUNTIME" "$1" 2>/dev/null || true; }
_base_runtime_skip(){ note "$1: runtime mode, from the proven runtime (${BASE_RUNTIME_NOTE:-runtime.json}); nothing is fetched or built"; }
_base_runtime_root(){ dirname "$BASE_HOME"; }            # /workspace on a machine, the fake root in the suite

BASE_RUNTIME_PENDING=0; M_PRE=""
_base_runtime_applying(){ # [manifest sha256] → 0 when a runtime-apply is running now (and, when a sha256 is given, is unpacking that manifest)
  local f="$BASE_STATE/runtime.applying" pid="" sum=""
  [ -f "$f" ] || return 1
  read -r pid sum < "$f" || true
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  [ -z "${1:-}" ] || [ "$sum" = "?" ] || [ "$sum" = "$1" ]
}
_base_runtime_pending_check(){ # → 0 when the manifest BASE_RUNTIME names is being unpacked right now (the install need not wait for it yet)
  local i want stamp
  if [ ! -f "$BASE_RUNTIME" ] && _base_runtime_applying; then      # the apply fetches its manifest first: a second or two
    note "BASE_RUNTIME: the runtime-apply that writes it is running; waiting for the manifest"
    for i in $(seq 1 60); do [ -f "$BASE_RUNTIME" ] && break; sleep 1; done
  fi
  [ -f "$BASE_RUNTIME" ] || return 1
  want="$(_base_sha256 "$BASE_RUNTIME" | awk '{print $1}')"; stamp="$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)"
  [ "$stamp" != "$want" ] || return 1
  _base_runtime_applying "$want" || return 1
  # the stamp may have landed between the two reads: a finished apply is not pending
  [ "$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)" != "$want" ]
}
_base_runtime_init(){ # base_init, when BASE_RUNTIME is set: the manifest must be the one runtime-apply unpacked (or is unpacking)
  local stamp want
  if [ ! -f "$BASE_RUNTIME" ] && ! _base_runtime_pending_check; then err "BASE_RUNTIME=$BASE_RUNTIME does not exist: run 'runtime-apply <manifest>' first"; exit 2; fi
  stamp="$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)"
  want="$(_base_sha256 "$BASE_RUNTIME" | awk '{print $1}')"
  BASE_RUNTIME_PENDING=0
  if [ "$stamp" != "$want" ]; then
    if _base_runtime_pending_check; then BASE_RUNTIME_PENDING=1
    elif [ "$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)" != "$want" ]; then
      err "BASE_RUNTIME names a manifest this machine has not applied (stamp ${stamp:-none}): run 'runtime-apply' with it first"; exit 2
    fi
  fi
  BASE_RUNTIME_NOTE="created $(_base_runtime_field created), base $(_base_runtime_field base)"
  local dm; dm="$(_base_runtime_field driver_min)"
  if [[ "$dm" =~ ^[0-9]+$ ]] && { ! [[ "${DRIVER_MIN:-}" =~ ^[0-9]+$ ]] || [ "$dm" -gt "$DRIVER_MIN" ]; }; then DRIVER_MIN="$dm"; fi
  if [ "$BASE_RUNTIME_PENDING" = 1 ]; then ok "runtime mode: $BASE_RUNTIME_NOTE; driver floor ${DRIVER_MIN:-580}; the runtime is still unpacking (the models start now, the tree is waited for where it is first needed)"
  else ok "runtime mode: $BASE_RUNTIME_NOTE; driver floor ${DRIVER_MIN:-580}"; fi
}
_base_runtime_apt_warn(){ # the apply could not install some of the runtime's apt packages: said in this run's summary, never silent
  local bad; bad="$("$SYS_PY" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("apt_failed", [])))' "$BASE_STATE/runtime-apply.json" 2>/dev/null || true)"
  [ -z "$bad" ] || warn "the runtime-apply could not install: $bad (the OS stage is skipped in runtime mode; install them with apt-get)"
}
_base_runtime_pin_tree(){ # COMFY_DIR names the runtime's tree, so no other tree on the volume (RunPod's runpod-slim, a leftover) is the one read
  [ -n "${COMFY_DIR:-}" ] && return 0
  local d; d="$(_base_runtime_field comfyui.dir)"
  [ -n "$d" ] || return 0
  COMFY_DIR="$(_base_runtime_root)/$d"; export COMFY_DIR
}
_base_runtime_pretree(){ # the tree is not there yet: name where it WILL be, so the model downloads have a place to land
  COMFY="$(_base_runtime_root)/$(_base_runtime_field comfyui.dir)"; CN="$COMFY/custom_nodes"; VENV=""; PACK_DIRS=()
  M="$COMFY/models"; M_SRC="default (the runtime's tree, still unpacking)"; M_PRE="$M"
  STAGING_ROOT="$M/.comfy-base-staging"; STAGING="$STAGING_ROOT"
  mkdir -p "$M" 2>/dev/null || true
  # the facts base_discover reads only once a tree exists, and the driver gate and the banner need before it
  GPU_NAME="?"; DRIVER="?"; TORCH_NOW="?"; BASE_GPU_SM=""
  if [ -z "$BASE_FAKE_ROOT" ] && command -v nvidia-smi >/dev/null 2>&1; then
    local q; q="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
    GPU_NAME="${q%%,*}"; DRIVER="${q##*, }"
  fi
}
_base_runtime_early_ok(){ # → 0 when the model downloads may start before (or beside) the unpack: a library of its own, or a shared store, decides the models' place only once the tree is read
  [ -z "${BASE_LIBRARY:-}" ] && [ "${BASE_VOLUME_SHARED:-0}" != "1" ] || return 1
}
_base_runtime_wait_apply(){ # → 0 once the stamp names this manifest and the tree is read as usual; 1 when the apply ended without it
  local want t0="$SECONDS" limit="${BASE_RUNTIME_WAIT:-3600}" res="$BASE_STATE/runtime-apply.json" line
  hdr "RUNTIME · waiting for the unpack"                    # its seconds are how long the unpack outlasted everything started before it
  want="$(_base_sha256 "$BASE_RUNTIME" | awk '{print $1}')"
  while [ "$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)" != "$want" ]; do
    if ! _base_runtime_applying "$want" && [ "$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)" != "$want" ]; then
      err "the runtime-apply ended without finishing (no stamp): re-run 'runtime-apply' with the manifest"
      BASE_FAILED+=("runtime: the apply did not finish"); return 1
    fi
    if [ $(( SECONDS - t0 )) -ge "$limit" ]; then err "the runtime was still unpacking after ${limit}s (BASE_RUNTIME_WAIT)"; BASE_FAILED+=("runtime: the apply took over ${limit}s"); return 1; fi
    sleep 1
  done
  ok "the runtime is unpacked (waited $(( SECONDS - t0 )) s)"
  # the apply ran beside this install: its seconds join this run's ledger as a background stage, as the downloads' do
  if [ -s "$res" ]; then
    line="$("$SYS_PY" "$BASE_DIR/py/runtime.py" ledger-line "$res" --label "RUNTIME (in the background, beside the models)" 2>/dev/null || true)"
    if [ -n "$line" ]; then local r0 r1 rs rw; IFS=$'\t' read -r r0 r1 rs rw <<< "$line"; BASE_STAGE_TIMES+=("$rs|$rw"); _base_span "$(( r0 - BASE_EPOCH0 ))" "$(( r1 - BASE_EPOCH0 ))" "$rw"; fi
  fi
  _base_runtime_apt_warn
  # now the tree exists: read it as base_init reads any tree
  _base_runtime_pin_tree
  local drc=0; base_discover quiet || drc=$?
  if [ "$drc" != 0 ]; then base_discover || true; err "the runtime is unpacked but no ComfyUI tree was found"; BASE_FAILED+=("runtime: no tree after the unpack"); return 1; fi
  BASE_RUNTIME_PENDING=0
  if [ -n "$M_PRE" ] && [ "$M" != "$M_PRE" ]; then
    warn "the model library resolved to $M, not $M_PRE where the early downloads went: whatever is missing there is fetched again"
  fi
  _base_library_link; base_banner || true
  return 0
}
_base_fetch_tool_pip(){ # the downloader of a runtime-mode install, on the system Python: uv and the runtime's own Python may be unpacking right now
  local v="$BASE_HOME/fetch" out w=0
  # python3-venv is one of the apply's first apt packages: while it runs, wait for it (up to 90 s) instead of giving the Hub rows up
  while ! "$SYS_PY" -c 'import ensurepip, venv' >/dev/null 2>&1 && _base_runtime_applying "" && [ "$w" -lt 90 ]; do
    [ "$w" != 0 ] || note "fetch venv: waiting for the runtime-apply to install python3-venv"
    sleep 1; w=$((w + 1))
  done
  if ! "$SYS_PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then note "fetch venv: the system Python has no venv/ensurepip"; return 1; fi
  if [ ! -x "$v/bin/python" ]; then
    if ! out="$("$SYS_PY" -m venv "$v" 2>&1)"; then printf '%s\n' "$out" | tail -3; note "fetch venv: python -m venv failed"; rm -rf "$v"; return 1; fi
  fi
  if ! out="$("$v/bin/python" -m pip install -q --disable-pip-version-check --upgrade "huggingface_hub[hf-xet]" 2>&1)"; then
    printf '%s\n' "$out" | tail -3; note "fetch venv: huggingface_hub[hf-xet] could not be installed"
  fi
  if [ ! -x "$v/bin/hf" ]; then return 1; fi
  ok "fetch venv: huggingface_hub $("$v/bin/python" -c 'import importlib.metadata as m; print(m.version("huggingface_hub"))' 2>/dev/null) on the system Python ($v)"
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

_base_runtime_zstd(){ # the parts are zstd tars: 0 when the tool is here; the apt job below installs it where it is not
  command -v zstd >/dev/null 2>&1
}
_base_runtime_apt_job(){ # <dir> <pkg>... → in the background beside the unpack: zstd and python3-venv first (the unpack and an install's
  # downloader wait on them), then the rest; only what dpkg reports missing is installed. Writes <dir>/result: "<secs>|<installed>|<failed>"
  local dir="$1" t0="$SECONDS" first=() rest=() p out rc inst="" bad="" a b; shift
  for p in zstd "$@"; do
    case " ${first[*]-} ${rest[*]-} " in *" $p "*) continue ;; esac
    case "$p" in zstd|python3-venv) first+=("$p") ;; *) rest+=("$p") ;; esac
  done
  out="$(_base_apt_ensure "${first[@]}")" && rc=0 || rc=$?
  [ -z "$out" ] || inst="${out#installed: }"
  if [ "$rc" != 0 ] && [ "$rc" != 3 ]; then bad="${first[*]}"; fi
  command -v zstd >/dev/null 2>&1 || touch "$dir/zstd.failed"
  touch "$dir/first.done"
  if [ "${#rest[@]}" -gt 0 ]; then
    out="$(_base_apt_ensure "${rest[@]}")" && rc=0 || rc=$?
    [ -z "$out" ] || inst="${inst:+$inst }${out#installed: }"
    if [ "$rc" != 0 ] && [ "$rc" != 3 ]; then bad="${bad:+$bad }${rest[*]}"; fi
  fi
  printf '%s|%s|%s\n' "$(( SECONDS - t0 ))" "$inst" "$bad" > "$dir/result"
}
_base_runtime_ledger(){ # <result json> → the RUNTIME row of the run's timing ledger: wall seconds, bytes, GB/s, apt seconds (3.29.3)
  local res="$1" t0 t1 secs line
  [ -s "$res" ] || return 0
  line="$("$SYS_PY" "$BASE_DIR/py/runtime.py" ledger-line "$res" 2>/dev/null || true)"
  [ -n "$line" ] || return 0
  IFS=$'\t' read -r t0 t1 secs line <<< "$line"
  BASE_STAGE_NAME=""                                 # the header's open stage is replaced by the measured one
  BASE_STAGE_TIMES+=("$secs|$line"); _base_span "$(( t0 - BASE_EPOCH0 ))" "$(( t1 - BASE_EPOCH0 ))" "$line"
  PKG_ID="runtime-apply" _base_timings_write
}

base_runtime_apply(){ # the apply, holding state/runtime.applying while it runs (3.29.4): an install may start beside it
  local rc=0
  _base_runtime_apply_run "$@" || rc=$?
  [ -z "${BASE_STATE:-}" ] || rm -f "$BASE_STATE/runtime.applying" 2>/dev/null || true
  return $rc
}
_base_runtime_apply_run(){ # <manifest path or https URL> [--parts <links.json>] [--jobs <n>] → the runtime unpacked beside comfy-base, stamped; idempotent
  local src="" parts_file="" jobs="${BASE_RUNTIME_JOBS:-8}" root work man sum have name from rc=0 old="" ts
  while [ $# -gt 0 ]; do
    case "$1" in
      --parts) parts_file="${2:-}"; shift 2 ;;
      --jobs) jobs="${2:-}"; shift 2 ;;
      *) src="$1"; shift ;;
    esac
  done
  if [ -z "$src" ]; then err "runtime-apply needs a manifest: runtime-apply <path or https URL of runtime.json> [--parts <links.json>] [--jobs <n>]"; return 2; fi
  if ! [[ "$jobs" =~ ^[1-9][0-9]*$ ]]; then err "runtime-apply: --jobs takes a number of parts to fetch at once (got '$jobs')"; return 2; fi
  if [ -n "$parts_file" ] && [ ! -f "$parts_file" ]; then err "runtime-apply: no link list at $parts_file"; return 2; fi
  base_env_setup
  local drc=0; base_discover quiet || drc=$?
  if [ "$drc" = 3 ]; then base_discover || true; return 3; fi
  if [ "${BASE_VOLUME_SHARED:-0}" = "1" ]; then note "runtime-apply: a shared store is the operator's shape, which builds everything newest; a runtime is for a buyer's own machine"; return 0; fi
  echo "$$ ?" > "$BASE_STATE/runtime.applying"                  # "<pid> <manifest sha256>"; the sha256 follows the manifest fetch below
  hdr "RUNTIME · $src"
  root="$(_base_runtime_root)"; work="$root/.comfy-base-runtime"; mkdir -p "$work"; man="$work/runtime.json"
  case "$src" in
    https://*|http://127.0.0.1:*|http://localhost:*) if ! _base_curl_dl "$man" "$src"; then err "could not fetch the manifest"; return 1; fi ;;
    *) if ! cp "$src" "$man"; then err "no manifest at $src"; return 1; fi ;;
  esac
  sum="$(_base_sha256 "$man" | awk '{print $1}')"; have="$(cat "$BASE_STATE/runtime.stamp" 2>/dev/null || true)"
  if [ "$sum" = "$have" ] && [ -d "$root/$("$SYS_PY" "$BASE_DIR/py/runtime.py" field "$man" comfyui.dir)" ]; then
    cp "$man" "$BASE_STATE/runtime.json"; rm -rf "$work"
    ok "this runtime is already applied: install with BASE_RUNTIME=$BASE_STATE/runtime.json"; return 0
  fi
  echo "$$ $sum" > "$BASE_STATE/runtime.applying"
  cp "$man" "$BASE_STATE/runtime.json.new" && mv -f "$BASE_STATE/runtime.json.new" "$BASE_STATE/runtime.json"     # the manifest an install names; its stamp follows the unpack
  # an Update replaces the code wholesale: the old venv and packs go aside first, and come back if the unpack fails
  local cdir; cdir="$root/$("$SYS_PY" "$BASE_DIR/py/runtime.py" field "$man" comfyui.dir)"; ts="$(_base_ts)"
  if [ -n "$have" ] && [ -d "$cdir" ]; then
    old="$root/.comfy-base-runtime-old.$ts"; mkdir -p "$old"
    local d; for d in "$cdir"/.venv* "$cdir/custom_nodes"; do [ -e "$d" ] && mv "$d" "$old/" || true; done
  fi
  # 3.29.3: every part fetched, verified and unpacked on its own, $jobs at a time (py/runtime.py apply); the result file
  # carries the seconds, bytes and GB/s for the ledger
  # 3.29.5: and the apt packages the runtime needs (runtime mode skips the OS stage), beside it: zstd and python3-venv first, the rest
  # after, only what dpkg reports missing. The unpack waits for zstd where it is not installed yet; the stamp waits for all of it.
  local res="$work/apply-result.json" aptdir="$work/apt" apt_pid="" zwait=0 pkgs=()
  mkdir -p "$aptdir"; rm -f "$aptdir"/*
  while IFS= read -r name; do [ -n "$name" ] && pkgs+=("$name"); done < <("$SYS_PY" "$BASE_DIR/py/runtime.py" apt "$man")
  if [ "${#pkgs[@]}" -gt 0 ] || ! _base_runtime_zstd; then
    ( trap - ERR EXIT; set +e; _base_runtime_apt_job "$aptdir" ${pkgs[@]+"${pkgs[@]}"} ) > "$aptdir/out" 2>&1 &
    apt_pid=$!
    _base_runtime_zstd || zwait=180
  fi
  "$SYS_PY" "$BASE_DIR/py/runtime.py" apply "$man" "$src" --root "$root" --work "$work/parts" --jobs "$jobs" --result "$res" \
    --zstd-wait "$zwait" --zstd-gate "$aptdir/zstd.failed" ${parts_file:+--parts "$parts_file"} || rc=$?
  if [ -n "$apt_pid" ]; then
    wait "$apt_pid" 2>/dev/null || true
    local asecs ainst abad
    IFS='|' read -r asecs ainst abad < "$aptdir/result" 2>/dev/null || true
    if [ -s "$aptdir/out" ]; then sed 's/^/  /' "$aptdir/out"; fi
    if [ -n "${asecs:-}" ] && [ -s "$res" ]; then
      "$SYS_PY" "$BASE_DIR/py/runtime.py" result-add "$res" apt_seconds "$asecs"
      "$SYS_PY" "$BASE_DIR/py/runtime.py" result-add "$res" apt_installed "$("$SYS_PY" -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$ainst")"
      "$SYS_PY" "$BASE_DIR/py/runtime.py" result-add "$res" apt_failed "$("$SYS_PY" -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$abad")"
      if [ -n "$ainst" ]; then ok "apt: installed $ainst (${asecs} s, beside the unpack)"; else note "apt: nothing missing (${asecs} s)"; fi
      if [ -n "$abad" ]; then warn "apt: could not install $abad: the OS stage is skipped in runtime mode, install them with apt-get"; fi
    fi
  fi
  if [ "$rc" != 0 ]; then
    err "the runtime did not unpack (rc $rc): nothing is stamped, so no install will run from it; re-run runtime-apply (a verified part is simply fetched again)"
    if [ -n "$old" ]; then local d; for d in "$old"/.venv* "$old/custom_nodes"; do [ -e "$d" ] && { rm -rf "$cdir/$(basename "$d")"; mv "$d" "$cdir/"; } || true; done; note "the previous runtime's venv and packs are back"; fi
    return 1
  fi
  [ -n "$old" ] && rm -rf "$old"
  cp "$man" "$BASE_STATE/runtime.json"; echo "$sum" > "$BASE_STATE/runtime.stamp"
  cp "$res" "$BASE_STATE/runtime-apply.json" 2>/dev/null || true
  _base_runtime_ledger "$res" || true
  rm -rf "$work"
  ok "runtime unpacked into $root; install with BASE_RUNTIME=$BASE_STATE/runtime.json"
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
  # the apt packages this green machine runs on, from the base's own list: a machine that skips the OS stage installs the missing ones
  local c apt=""
  for c in zstd $(_base_apt_core) openssh-server nfs-common; do
    dpkg-query -W -f '${db:Status-Abbrev}' "$c" 2>/dev/null | grep -q '^ii' && apt="${apt:+$apt,}$c" || true
  done
  "$SYS_PY" "$BASE_DIR/py/runtime.py" build --apt "$apt" --root "$(_base_runtime_root)" --comfy "$COMFY" --out "$out" \
    ${part:+--part-bytes "$part"} ${url_base:+--url-base "$url_base"} --torch-cuda "$cuda" --base-version "$BASE_VERSION" \
    --python "$("$PY" -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
    --sage-key "$(cat "$VENV/.comfy-base-sageattention" 2>/dev/null)"
}
