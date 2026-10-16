# 30-venv.sh — the venv start.sh activates: who owns it, whether it survives a restart, rebuild with rollback, rescue.
#
# Stamp: $VENV/.comfy-base-venv holds "python=<x.y.z> torch=<ver> base=<ver> ts=<ts> by=<pkg id>".
# An unstamped venv that passes the probe is adopted and stamped.

_base_dir_bytes(){ # <dir> → its size in bytes. 3.5.0: remembered by path and ctime in state/sizes.tsv, because a backup venv
  # is never written after it is made and `du` over it on a network volume took up to a minute on every run
  local d="$1" c k hit b f
  c="$( { stat -c %Z "$d" || stat -f %c "$d"; } 2>/dev/null | head -1)"
  f="${BASE_STATE:-}/sizes.tsv"
  if [ -n "$c" ] && [ -n "${BASE_STATE:-}" ]; then
    k="$d"$'\t'"$c"
    hit="$(awk -F'\t' -v p="$d" -v c="$c" '$1==p && $2==c {print $3; exit}' "$f" 2>/dev/null)"
    if [ -n "$hit" ]; then echo "$hit"; return 0; fi
  fi
  b="$(du -sk "$d" 2>/dev/null | awk '{print $1*1024}')"; b="${b:-0}"
  if [ -n "${k:-}" ] && [ "$b" != "0" ]; then mkdir -p "$BASE_STATE" 2>/dev/null && printf '%s\t%s\n' "$k" "$b" >> "$f" 2>/dev/null || true; fi
  echo "$b"
}
_base_pip(){ "$PY" -m pip "$@"; }      # never bare pip: pip on PATH belongs to whatever interpreter start.sh did not use
_base_uvpip(){ uv pip install --python "$PY" "$@"; }   # uv is faster and hardlinks out of a same-filesystem cache; the interpreter is named, never inferred
# Every requirements file the venv must satisfy. 40-packs.sh replaces this with the full list (ComfyUI,
# baked packs, this package's packs, every ledger-installed package's packs); the stub keeps 30 self-contained.
_base_all_reqfiles(){ if [ -f "$COMFY/requirements.txt" ]; then echo "$COMFY/requirements.txt"; fi; }

_base_bootstrap_venv(){ # $BASE_HOME/tools-bootstrap, made with the system python's own venv module, only to fetch uv from PyPI.
  # The system interpreter is never written to (PEP 668 refuses that on Ubuntu 24.04 images, and it is container disk
  # anyway); no installer script is piped into a shell: PyPI is the one source.
  local t="$BASE_HOME/tools-bootstrap"
  [ -x "$t/bin/python" ] && "$t/bin/python" -m pip --version >/dev/null 2>&1 && return 0
  [ -n "$SYS_PY" ] || return 1
  if [ "$BASE_DRY" = "1" ]; then would "create $t with $SYS_PY -m venv (only to fetch uv)"; return 0; fi
  if ! "$SYS_PY" -m venv "$t" 2>&1 | tail -2; then err "$SYS_PY -m venv $t failed — the image lacks the venv module"; return 1; fi
  "$t/bin/python" -m pip --version >/dev/null 2>&1 || { err "$t has no pip after python -m venv (ensurepip missing on this image)"; return 1; }
  return 0
}
_base_uv_refresh(){ # 3.0.0: uv at its newest on every run, as a standalone copy at $BASE_HOME/bin/uv. It is copied out of the
  # bootstrap venv so that nothing uv builds (the tools venv, the main venv) is ever the thing uv itself lives in.
  local t="$BASE_HOME/tools-bootstrap" b="$BASE_HOME/bin"
  _base_bootstrap_venv || return 1
  "$t/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip uv 2>&1 | tail -2 || true
  [ -x "$t/bin/uv" ] || return 1
  mkdir -p "$b" && cp "$t/bin/uv" "$b/uv.new" && chmod 755 "$b/uv.new" && mv -f "$b/uv.new" "$b/uv"
  if [ -x "$t/bin/uvx" ]; then cp "$t/bin/uvx" "$b/uvx.new" && chmod 755 "$b/uvx.new" && mv -f "$b/uvx.new" "$b/uvx"; fi
  return 0
}
_base_ensure_uv(){ # uv is how the newest Python, torch and every package arrive: $BASE_HOME/bin/uv, refreshed once per run
  local b="$BASE_HOME/bin"
  if [ "$BASE_DRY" = "1" ]; then
    if [ -x "$b/uv" ]; then export PATH="$b:$PATH"; hash -r; else command -v uv >/dev/null 2>&1 || would "install uv at $b/uv (from PyPI, through $BASE_HOME/tools-bootstrap)"; fi
    return 0
  fi
  if [ -x "$b/uv" ] && { [ -n "$BASE_FAKE_ROOT" ] || [ "$BASE_NO_NET" = "1" ]; }; then _BASE_UV_REFRESHED=1; fi   # a fake run never reaches PyPI
  if [ -z "${_BASE_UV_REFRESHED:-}" ]; then
    _BASE_UV_REFRESHED=1
    local was=""; [ -x "$b/uv" ] && was="$("$b/uv" --version 2>/dev/null | awk '{print $2}')"
    if _base_uv_refresh; then
      local now; now="$("$b/uv" --version 2>/dev/null | awk '{print $2}')"
      if [ -n "$was" ] && [ "$was" != "$now" ]; then BASE_CHANGED+=("uv $was → $now"); fi
    elif [ ! -x "$b/uv" ]; then return 1
    else warn "uv could not be refreshed from PyPI this run: using $("$b/uv" --version 2>/dev/null)"; BASE_WARN+=("uv not refreshed this run"); fi
  fi
  export PATH="$b:$PATH"; hash -r
  [ -x "$b/uv" ]
}
_base_newest_python_minor(){ # → the newest stable CPython minor uv can provide (no pre-release, no free-threaded build)
  _base_python_minors | head -1 || true
}
_base_python_minors(){ # → every stable CPython minor uv can provide, newest first
  uv python list 2>/dev/null | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV || true
}

# ---------------------------------------------------------------- picks: genuine latest, no fallback
_base_python_pick(){ # → newest CPython minor whose uv pip compile resolves the derived set (every upstream pin lifted) + torch
  # The resolve output must go to a real file: `uv pip compile -o /dev/null` fails because uv writes its
  # temp file beside the output path. `|| true` on the greps: an empty grep exits 1 and pipefail would
  # fire the ERR trap inside the substitution.
  local mm tmp list cands f reqargs=() errf backend="${BASE_TORCH_BACKEND:-auto}"
  _base_tmp; tmp="$BASE_TMPD/resolve.txt"; errf="$BASE_TMPD/resolve.err"
  # 3.0.0: the DERIVED set, so no upstream == pin caps the Python either. Requirement files are POSITIONAL for
  # `uv pip compile` (uv 0.12 rejects `-r`: "unexpected argument", 2026-09-05).
  if _base_reqlift >/dev/null 2>"$errf" && [ -s "$BASE_LIFT_DIR/derived.txt" ]; then reqargs=("$BASE_LIFT_DIR/derived.txt")
  else while IFS= read -r f; do if [ -n "$f" ]; then reqargs+=("$f"); fi; done < <(_base_all_reqfiles); fi
  list="$(uv python list 2>/dev/null || true)"
  cands="$(printf '%s\n' "$list" | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV || true)"   # -rV, not tac: no tac on macOS
  if [ -z "$cands" ]; then
    err "uv listed no CPython versions — cannot pick a Python (uv $(uv --version 2>/dev/null || echo missing))"
    BASE_FAILED+=("venv: uv python list returned nothing"); return 1
  fi
  # 3.6.1: no shortcut when there is nothing else to resolve against. The torch family alone decides then, and it is
  # what ruled out the newest minor on 2026-10-15: uv listed CPython 3.15 before torch shipped a cp315 wheel, and
  # "take the newest" handed 3.15 to the torch pick, which failed every fresh venv's dry run.
  for mm in $cands; do
    printf '    trying Python %s ... ' "$mm" >&2
    if printf 'torch\ntorchvision\ntorchaudio\n' | uv pip compile --quiet --python-version "$mm" --python-platform "$(_base_uv_platform)" \
         --torch-backend "$backend" --extra-index-url https://pypi.nvidia.com --index-strategy unsafe-best-match \
         - ${reqargs[@]+"${reqargs[@]}"} -o "$tmp" >/dev/null 2>"$errf"; then
      rm -f "$tmp"; echo "resolves" >&2; echo "$mm"; return 0
    fi
    echo "no (the torch family${reqargs[0]:+ and the requirement set} does not resolve)" >&2
    # uv's own words, or the failure is undiagnosable from the log (a whole run said "no" seven times over an argument error)
    { grep -v '^\s*$' "$errf" | tail -3 | sed 's/^/        /' || true; } >&2
  done
  err "no CPython minor resolves ComfyUI + pack requirements (tried: $(printf '%s ' $cands))"
  return 1
}
_base_uv_platform(){ case "$(uname -m)" in aarch64|arm64) echo aarch64-unknown-linux-gnu;; *) echo x86_64-unknown-linux-gnu;; esac; }
_base_torch_pick(){ "$SYS_PY" "$BASE_DIR/py/torch_pick.py" "$1" "${2:-}"; }   # <x.y> [venv backend] → "<torch version> <backend>" (uv --torch-backend=auto)

# ---------------------------------------------------------------- ownership and stamps
_base_venv_owner(){ # → base | none
  [ -f "$VENV/.comfy-base-venv" ] && { echo base; return 0; }
  echo none
}
_base_venv_stamp_write(){ # <python x.y.z> <torch> [backend]
  echo "python=$1 torch=$2 backend=${3:-${BASE_TORCH_BACKEND:-?}} base=$BASE_VERSION ts=$(_base_ts) by=${PKG_ID:-base}" > "$VENV/.comfy-base-venv"
}
_base_venv_probe(){ # [python-mm] [torch] → 0 reusable, 1 rebuild (py/venv_probe.py)
  BASE_WANT_PY="${1:-}" BASE_WANT_TORCH="${2:-}" BASE_WANT_BACKEND="${BASE_TORCH_BACKEND:-}" BASE_WANT_PATCH="${BASE_WANT_PATCH:-}" BASE_WANT_SM="${BASE_GPU_SM:-}" BASE_PERSIST_ROOT="$BASE_PERSIST_ROOT" VENV="$VENV" \
    "$PY" "$BASE_DIR/py/venv_probe.py" 2>/dev/null
}

# ---------------------------------------------------------------- what survives a pod restart
_base_path_is_persistent(){ # <path> → 0 when a pod stop/start keeps it
  # TWO kinds of path survive a restart: the volume, and anything the IMAGE provides (a start restores the
  # container disk FROM the image, so /usr/bin/python3.12 is back every time). What does not come back is
  # anything WRITTEN AT RUNTIME onto the container disk: $HOME, /root, /home, /tmp. That is exactly where uv
  # puts a managed interpreter. Getting this wrong the other way quarantines the template's own venv.
  local p="${1:-}"
  [ -n "$BASE_PERSIST_ROOT" ] || return 0                       # not the pod layout: nothing to prove
  [ -n "$p" ] || return 1
  case "$p/" in
    "$BASE_PERSIST_ROOT"/*) return 0;;
    "${HOME:-/root}"/*|/root/*|/home/*|/tmp/*|/var/tmp/*) return 1;;
    *) return 0;;                                               # /usr, /opt, ...: the image restores it
  esac
}
_base_venv_base_home(){ # <venv> → the base interpreter dir from pyvenv.cfg; readable even when bin/python dangles
  awk '/^[[:space:]]*home[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/,""); print; exit}' "$1/pyvenv.cfg" 2>/dev/null || true
}
_base_venv_base_is_persistent(){ # [venv] → 0 when the venv's interpreter survives a pod stop/start
  local v="${1:-${VENV:-}}" home=""
  [ -n "$BASE_PERSIST_ROOT" ] || return 0
  case "$v/" in "$BASE_PERSIST_ROOT"/*) ;; *) return 0;; esac     # a venv off the volume is ephemeral anyway
  home="$(_base_venv_base_home "$v")"
  [ -n "$home" ] || return 1                                    # no pyvenv.cfg: not provable, so not trusted
  _base_path_is_persistent "$home"
}
_base_venv_base_verdict(){ # [venv] → one phrase
  local v="${1:-${VENV:-}}" home; home="$(_base_venv_base_home "$v")"
  if [ -z "$BASE_PERSIST_ROOT" ]; then echo "not the pod layout — nothing to prove"; return 0; fi
  if [ -z "$home" ]; then echo "no pyvenv.cfg — cannot prove it survives, so treated as if it does not"; return 0; fi
  case "$home/" in "$BASE_PERSIST_ROOT"/*) echo "on the volume — survives a pod restart"; return 0;; esac
  if _base_path_is_persistent "$home"; then echo "provided by the image — restored at every pod start"
  else echo "CONTAINER DISK, written at runtime — the next pod start wipes it"; fi
}
_base_venv_boot_line(){ # [venv] → the banner's "boots from" line, judged by the SAME predicate the venv stage uses (2.0.8)
  local v="${1:-${VENV:-}}" bh bv
  bh="$(_base_venv_base_home "$v")"; bv="$(_base_venv_base_verdict "$v")"
  if _base_venv_base_is_persistent "$v"; then echo "  boots from   ${bh:-?} · $bv"
  else echo -e "  boots from   ${bh:-?} · ${RED}$bv; this run rebuilds the venv${NC}"; fi
}

# ---------------------------------------------------------------- backups, swap-aside, rollback
_base_pick_pristine_backup(){ # → the newest .pre-* whose python actually runs, or nothing (ts sorts oldest→newest)
  local b last=""
  for b in "$VENV".pre-*; do
    [ -d "$b" ] || continue
    [ -x "$b/bin/python" ] || continue
    "$b/bin/python" -c 'import sys' >/dev/null 2>&1 || continue
    last="$b"
  done
  printf '%s' "$last"
}
_base_drop_stale_venv_backups(){ # only the run that replaces a venv may remove the backup it is replacing
  # You can roll back exactly one step, so a second backup is dead weight — but only provably dead at the
  # moment a new one is about to be written. Deleting it earlier throws away a rollback the user was told to keep.
  local old
  for old in "$VENV".pre-*; do
    [ -d "$old" ] || continue
    note "superseded venv backup removed: $old ($(_base_bytes_to_gb "$(_base_dir_bytes "$old")") GB)"
    rm -rf "$old"
  done
  return 0
}
_base_venv_rollback(){
  if [ -z "$BASE_VENV_BACKUP" ] || [ ! -d "$BASE_VENV_BACKUP" ]; then err "no venv backup to restore"; return 1; fi
  rm -rf "$VENV" && mv "$BASE_VENV_BACKUP" "$VENV" && ok "venv rolled back: $BASE_VENV_BACKUP → $VENV"
  BASE_VENV_BACKUP=""; BASE_VENV_RESULT="rolled-back"
}
_base_venv_swap_aside(){ # <ts> — move $VENV out of the way and arm the rollback
  local ts="$1" pristine="" dead=""
  [ -d "$VENV" ] || return 0
  # healthy: back it up in place, dropping the superseded backup
  if [ -x "$PY" ] && _base_venv_base_is_persistent; then
    _base_drop_stale_venv_backups; BASE_VENV_BACKUP="$VENV.pre-comfy-base-$ts"
    mv "$VENV" "$BASE_VENV_BACKUP"; note "old venv → $BASE_VENV_BACKUP"
    return 0
  fi
  # This venv cannot boot (interpreter on the container disk, or bin/python already dangles). Backing IT up
  # is worthless, and dropping the existing .pre-* would delete the only proven-bootable venv on the volume.
  # Keep that as the rollback and quarantine the corpse — OUT of $COMFY, so nothing matching .venv* is left.
  pristine="$(_base_pick_pristine_backup)"
  dead="$BASE_HOME/dead-venvs"; mkdir -p "$dead" 2>/dev/null || true
  if [ -d "$dead" ] && mv "$VENV" "$dead/$(basename "$VENV")-$ts" 2>/dev/null; then
    note "unbootable venv quarantined → $dead/$(basename "$VENV")-$ts"
  else
    BASE_VENV_BACKUP="$VENV.pre-comfy-base-$ts"; mv "$VENV" "$BASE_VENV_BACKUP"
    note "old venv → $BASE_VENV_BACKUP (quarantine unavailable; the pristine backup is kept too)"
    return 0
  fi
  if [ -n "$pristine" ]; then BASE_VENV_BACKUP="$pristine"; note "rollback target kept: $pristine"
  else BASE_VENV_BACKUP=""; miss "no bootable venv to roll back to — if this build fails the path is left empty, which start.sh rebuilds on the next boot"; fi
}
_base_venv_verify(){ # [python-mm] — prints the facts, non-zero on any miss (py/venv_verify.py)
  BASE_TORCH_INFO="$(VENV="$VENV" BASE_PERSIST_ROOT="$BASE_PERSIST_ROOT" BASE_WANT_PY="${1:-}" BASE_WANT_BACKEND="${BASE_TORCH_BACKEND:-}" BASE_WANT_SM="${BASE_GPU_SM:-}" \
    BASE_EXTRA_IMPORTS="${PKG_IMPORT_CHECK:-}" "$PY" "$BASE_DIR/py/venv_verify.py" 2>&1)"; local rc=$?
  echo "$BASE_TORCH_INFO" | sed 's/^/  /'
  return $rc
}

# ---------------------------------------------------------------- build
_base_venv_pip_extra(){ # the package's PIP_EXTRA into the venv — an OPTIONAL declaration: guarded, or `set -u` kills the run
  # (the base's own step one died seven minutes into a build on 2026-09-05 over ${#PIP_EXTRA[@]} with none declared)
  # ${NAME+x} (element 0) is the test bash 3.2 AND 5 accept for an array that may not be declared at all; ${NAME[@]+x} is not
  [ -n "${PIP_EXTRA+x}" ] && [ "${#PIP_EXTRA[@]}" -gt 0 ] || return 0
  _base_pip install -q --upgrade -c "$CONSTRAINTS" --extra-index-url https://pypi.nvidia.com "${PIP_EXTRA[@]}" 2>&1 | tail -2 || { miss "PIP_EXTRA install failed"; return 1; }
  return 0
}
_base_venv_pytest(){ # pytest into OUR venv, proven by import — so base_test's first choice ($PY -m pytest) is taken and every
  # package's comfyui/gpu tier imports the packs it tests (2.0.10: a package suite ran under uv's throwaway 3.12 on the pod and
  # could not see the packs' modules; the old one-line "pytest + huggingface_hub" install had left pytest out, unseen)
  local c="${CONSTRAINTS:-$BASE_STATE/constraints-torch.txt}" v
  _base_pip install -q --upgrade ${c:+-c "$c"} pytest 2>&1 | tail -2 || { miss "pytest install failed"; return 1; }
  v="$("$PY" -c 'import pytest; print(pytest.__version__)' 2>/dev/null)" || { miss "pytest does not import from $PY after the install"; return 1; }
  ok "pytest $v in the venv — the suite runs inside it"
  return 0
}
_base_venv_ensure_pytest(){ # every run, reuse included: a venv without pytest is a venv the suite cannot run in
  "$PY" -c 'import pytest' >/dev/null 2>&1 && return 0
  if [ "$BASE_DRY" = "1" ]; then would "install pytest into the venv (the suite runs inside it)"; return 0; fi
  _base_venv_pytest
}
_base_venv_build(){ # <python-mm> <torch> → sets BASE_VENV_RESULT to built | rebuilt | failed-* | rolled-back
  local pymm="$1" tv="$2" ts rv step_fail=0 had=0 backend="${BASE_TORCH_BACKEND:?the torch backend is picked first}"
  ts="$(_base_ts)"
  [ -d "$VENV" ] && had=1 || true
  CONSTRAINTS="$BASE_STATE/constraints-torch.txt"
  _base_reqlift >/dev/null 2>&1 || true
  # (0) prove the whole set resolves BEFORE the old venv is touched — a throwaway venv in a temp dir
  rv="$(_base_mktemp_d)/resolve"
  echo "  resolvability dry-run for Python $pymm, torch $tv on $backend, the derived requirement set (every upstream pin lifted)…"
  if ! uv venv -q --python "$pymm" "$rv" 2>&1 | tail -3; then
    err "uv cannot provide Python $pymm"; BASE_FAILED+=("venv: uv venv --python $pymm failed"); BASE_VENV_RESULT="failed-python"; return 1
  fi
  if ! uv pip install --python "$rv/bin/python" --dry-run -q --torch-backend "$backend" \
        --extra-index-url https://pypi.nvidia.com --index-strategy unsafe-best-match \
        "torch==$tv" torchvision torchaudio -r "$BASE_LIFT_DIR/derived.txt" pytest "huggingface_hub[hf-xet]" 2>&1 | tail -15; then
    err "the requirement set does NOT resolve for Python $pymm — nothing was changed (the offending name is above)"
    BASE_FAILED+=("venv: resolvability dry-run failed for $pymm"); BASE_VENV_RESULT="failed-resolve"; return 1
  fi
  ok "every requirement resolves for Python $pymm"
  # (1) swap the venv at the SAME path: start.sh only creates one when the path is missing
  _base_venv_swap_aside "$ts"
  if ! uv venv -q --python "$pymm" --seed "$VENV" 2>&1 | tail -3; then
    err "uv venv failed"; BASE_FAILED+=("venv: uv venv failed"); BASE_VENV_RESULT="failed-uv-venv"; _base_venv_rollback || true; return 1
  fi
  # (a) the torch family FIRST, on the picked CUDA backend (requirements first once pulled a cu12 torch)
  if ! _base_run_watched "torch $tv+$backend · torchvision · torchaudio" \
         _base_uvpip "torch==$tv" torchvision torchaudio --torch-backend "$backend"; then
    err "torch install failed"; BASE_FAILED+=("venv: torch $tv+$backend install failed"); BASE_VENV_RESULT="failed-torch"; _base_venv_rollback || true; return 1
  fi
  # (b) the torch family and its whole dependency closure held, then everything else newest in one resolve
  base_torch_constraints
  if [ ! -s "$CONSTRAINTS" ]; then err "no torch in the venv to hold"; BASE_FAILED+=("venv: torch missing after install"); BASE_VENV_RESULT="failed-torch"; _base_venv_rollback || true; return 1; fi
  echo "  constraints: $(wc -l < "$CONSTRAINTS" | tr -d ' ') package(s) of torch's closure held ($(grep -E '^torch==' "$CONSTRAINTS" | head -1))"
  _base_run_watched "requirements (the derived set, one resolve)" _base_derived_install || { miss "the derived requirement set failed"; step_fail=1; }
  # (d) verify inside the venv → stamp, else roll back
  if ! _base_venv_verify "$pymm" || [ "$step_fail" = "1" ]; then
    err "venv verify failed — rolling back"
    BASE_FAILED+=("venv: verify failed ($BASE_TORCH_INFO)"); BASE_VENV_RESULT="failed-verify"
    _base_venv_rollback || true
    return 1
  fi
  _base_xet_check_soft
  local t0 t1; t0=$(date +%s); _base_pip --version >/dev/null 2>&1 || true; t1=$(date +%s)
  echo "  warm-up: python -m pip --version $((t1 - t0)) s (ComfyUI-Manager allows 5 s)"
  if [ -n "$BASE_PERSIST_ROOT" ]; then note "venv interpreter: $(_base_venv_base_home "$VENV") ($(_base_venv_base_verdict "$VENV"))"; fi
  _base_venv_stamp_write "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" "$tv" "$backend"
  if [ "$had" = "1" ]; then BASE_VENV_RESULT="rebuilt"; BASE_CHANGED+=("venv rebuilt on Python $pymm / torch $tv+$backend (previous at ${BASE_VENV_BACKUP:-quarantine})")
  else BASE_VENV_RESULT="built"; BASE_CHANGED+=("venv built on Python $pymm / torch $tv+$backend"); fi
  ok "venv ready: $BASE_TORCH_INFO"
}
_base_torch_family_upgrade(){ # <torch> <backend>: on a reused venv: torchvision, torchaudio and triton to the builds that match torch
  local tv="$1" backend="$2" out have
  # 3.3.0: never backwards. A venv whose torch is already newer than the pick (a newer build than the index resolves
  # today) keeps it; installing torch==<pick> there would have moved it down.
  have="$("$PY" -c 'import importlib.metadata as m; print(m.version("torch").split("+")[0])' 2>/dev/null || true)"
  if [ -n "$have" ] && [ "$have" != "$tv" ] && _base_vge "$have" "$tv"; then note "torch $have is newer than the pick ($tv): kept, never moved backwards"; tv="$have"; fi
  if out="$(_base_uvpip -q --upgrade --torch-backend "$backend" "torch==$tv" torchvision torchaudio 2>&1)"; then
    base_torch_constraints; return 0
  fi
  warn "the torch family could not be brought to the newest builds for torch $tv on $backend: $(printf '%s' "$out" | tail -1)"
  BASE_WARN+=("torch family: torchvision/torchaudio not moved with torch $tv ($backend)")
  return 0
}
_base_xet_check_soft(){ # a silent fall back to plain HTTPS is several times slower and reports no error at all
  local xv; xv="$("$PY" -c 'import hf_xet; print(getattr(hf_xet, "__version__", "present"))' 2>/dev/null || true)"
  if [ -n "$xv" ]; then ok "hf_xet $xv — chunked Xet transfer, ${HF_XET_NUM_CONCURRENT_RANGE_GETS:-32} concurrent range gets"
  else warn "hf_xet is NOT importable — downloads fall back to plain HTTPS and will be far slower"; fi
}

base_venv(){ # the stage: reuse when the probe passes, otherwise rebuild with rollback
  hdr "VENV · $VENV"
  CONSTRAINTS="$BASE_STATE/constraints-torch.txt"
  local ts owner pymm tv cp; ts="$(_base_ts)"
  if [ -n "${BASE_PINNED:-}" ] && [ "${BASE_PINNED}" != "0" ]; then note "BASE_PINNED is retired in 3.0.0 and ignored: the venv takes the newest Python and torch like every run"; fi
  # ---- fake / offline: a venv-shaped directory so every later step exercises its real code path
  if [ "$BASE_NO_NET" = "1" ]; then
    if [ -f "$VENV/.comfy-base-venv" ] && _base_venv_base_is_persistent; then ok "reusing our venv (stamp: $(cat "$VENV/.comfy-base-venv"))"; BASE_VENV_RESULT="reused"; BASE_TORCH_INFO="(fake venv)"
    elif [ -f "$VENV/.comfy-base-venv" ] && [ "$BASE_DRY" != "1" ]; then
      miss "our venv boots from $(_base_venv_base_home "$VENV") — $(_base_venv_base_verdict "$VENV"); rebuilding (fake)"
      _base_venv_swap_aside "$ts"
      local real_py; real_py="$("$SYS_PY" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$SYS_PY")"
      mkdir -p "$VENV/bin"; ln -sfn "$real_py" "$VENV/bin/python"; _base_venv_stamp_write "fake" "fake"
      ok "fake venv rebuilt"; BASE_VENV_RESULT="rebuilt"
    elif [ "$BASE_DRY" = "1" ]; then would "build a fake venv at $VENV (BASE_NO_NET)"; BASE_VENV_RESULT="would-build"
    else
      _base_venv_swap_aside "$ts"
      local real_py; real_py="$("$SYS_PY" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$SYS_PY")"
      mkdir -p "$VENV/bin"; ln -sfn "$real_py" "$VENV/bin/python"
      _base_venv_stamp_write "fake" "fake"
      ok "fake venv created (bin/python → $real_py, stamp written); pip skipped (BASE_NO_NET)"; BASE_VENV_RESULT="fake"
    fi
    if [ "$BASE_DRY" != "1" ]; then [ -f "$CONSTRAINTS" ] || : > "$CONSTRAINTS"; fi
    BASE_TORCH_INFO="(fake venv: $("$PY" --version 2>&1 || echo 'no python at the venv path'))"; return 0
  fi
  # ---- uv first: the picks need it
  if ! _base_ensure_uv; then err "uv could not be installed from PyPI"; BASE_FAILED+=("venv: uv missing"); BASE_VENV_RESULT="failed-uv"; return 1; fi
  note "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  # 2.0.43: a dry run must not write, so _base_ensure_uv only PROMISES uv when the tools venv is absent. The picks
  # below then have no uv to run, and the step failed — over a venv that does not exist yet to be stale. A dry run
  # predicts; this it cannot predict, so it says so and lets the rest of the check run. Found on a brand-new pod
  # (no network volume): every earlier pod already carried tools/bin/uv from an install on the shared volume.
  if [ "$BASE_DRY" = "1" ] && ! command -v uv >/dev/null 2>&1; then
    would "uv pick the newest Python that resolves the requirement set, then the newest torch for it"
    note "picks not computed — uv arrives with the real run"
    BASE_VENV_RESULT="$([ -d "$VENV" ] && echo would-rebuild || echo would-build)"
    BASE_TORCH_INFO="(not computed — the dry run has no uv yet)"
    return 0
  fi
  # ---- the picks: the torch backend (uv, from this machine's driver; never backwards on a shared venv), the newest
  # Python that resolves the derived set on it, then the newest torch for that Python. No fallback.
  local have_backend="" pick why
  have_backend="$( [ -x "$PY" ] && _base_venv_backend )"
  case "$have_backend" in cu*) ;; *) have_backend="";; esac
  BASE_TORCH_BACKEND="$have_backend"
  if [ -z "$BASE_TORCH_BACKEND" ]; then
    # 3.6.1: the newest minor torch has a build for, not the newest uv lists (3.15 arrived before its torch wheels)
    local mm1; for mm1 in $(_base_python_minors); do
      if pick="$(_base_torch_pick "$mm1" "" 2>/dev/null)"; then BASE_TORCH_BACKEND="${pick#* }"; break; fi
    done
  fi
  # the pick runs in a command substitution: anything it appends to BASE_FAILED is lost — the caller records the failure
  if ! pymm="$(_base_python_pick)"; then BASE_FAILED+=("venv: no Python resolves the requirement set (uv's reason is above)"); BASE_VENV_RESULT="failed-pick"; return 1; fi
  _base_tmp
  if ! pick="$(_base_torch_pick "$pymm" "$have_backend" 2>"$BASE_TMPD/torch_pick.why")"; then
    err "torch pick failed: $(cat "$BASE_TMPD/torch_pick.why")"; BASE_FAILED+=("venv: torch pick failed ($(tail -1 "$BASE_TMPD/torch_pick.why"))"); BASE_VENV_RESULT="failed-pick"; return 1
  fi
  tv="${pick%% *}"; BASE_TORCH_BACKEND="${pick#* }"; why="$(tail -1 "$BASE_TMPD/torch_pick.why" 2>/dev/null)"
  echo "  target: Python $pymm · torch $tv+$BASE_TORCH_BACKEND (${why:-uv --torch-backend=auto})${BASE_GPU_SM:+ · $BASE_GPU_SM}"
  case "$why" in *"OLDER CUDA major"*) BASE_WARN+=("torch: $why");; *"newer CUDA major"*) BASE_CHANGED+=("torch: $why");;
    *"held by the torch family"*) BASE_UPSTREAM+=("CUDA: $why");; esac
  # the newest patch of the chosen minor: uv keeps the old patch (a venv elsewhere may still use it) and moves venvs
  # created through its minor-version directory to the new one without a rebuild
  if [ "$BASE_DRY" != "1" ]; then uv python upgrade "$pymm" >/dev/null 2>&1 || uv python install "$pymm" >/dev/null 2>&1 || true; fi
  owner="$(_base_venv_owner)"
  # ---- reuse when the probe passes (the probe is HARD on a runtime-written interpreter, so a poisoned venv never reuses)
  if [ -x "$PY" ] && _base_venv_probe "$pymm" "$tv"; then
    case "$owner" in
      base)   BASE_VENV_RESULT="reused"; ok "reusing our venv (stamp: $(cat "$VENV/.comfy-base-venv"))";;
      none)   BASE_VENV_RESULT="adopted"; ok "adopting the existing venv — it passes every check"; BASE_CHANGED+=("venv adopted");;
    esac
    _base_venv_ensure_pytest || warn "pytest could not be installed into $VENV — the suite cannot run inside it"
    if [ "$BASE_DRY" = "1" ]; then would "bring torchvision/torchaudio/triton to the builds for torch $tv on $BASE_TORCH_BACKEND; refresh the venv stamp"      # 2.0.18: a --check restamped the pod's venv
    else
      _base_torch_family_upgrade "$tv" "$BASE_TORCH_BACKEND"
      _base_venv_stamp_write "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" "$tv" "$BASE_TORCH_BACKEND"
    fi
    BASE_TORCH_INFO="$("$PY" -c 'import torch; print("torch %s · CUDA %s" % (torch.__version__, torch.version.cuda))' 2>/dev/null || echo "?")"
    return 0
  fi
  if [ -x "$PY" ]; then miss "the venv at $VENV does not pass the probe (details above) — rebuilding"; else note "no venv at $VENV — building"; fi
  if [ "$BASE_DRY" = "1" ]; then
    would "uv dry-run the derived requirement set for Python $pymm ($(_base_all_reqfiles | wc -l | tr -d ' ') requirement file(s), every upstream pin lifted, + the torch family on $BASE_TORCH_BACKEND)"
    [ -d "$VENV" ] && would "mv $VENV → $VENV.pre-comfy-base-<ts> (or quarantine it if it cannot boot)"
    would "uv venv --python $pymm --seed $VENV; install torch==$tv on $BASE_TORCH_BACKEND, hold its closure, the derived set in one resolve; verify; stamp"
    BASE_VENV_RESULT="$([ -d "$VENV" ] && echo would-rebuild || echo would-build)"; return 0
  fi
  _base_venv_build "$pymm" "$tv" || return 1
}

# ---------------------------------------------------------------- rescue: the only stage that must work when $PY is dead
_base_venv_probe_ro(){ # <dir> — describe one .venv* directory without executing it; 0 when it could boot
  local v="$1" kind="live" stamp="none" link="(not a symlink)" tgt="present" home verdict rc=0
  case "$v" in *.pre-*) kind="backup (restore source only)";; esac
  [ -f "$v/.comfy-base-venv" ] && stamp="$(cat "$v/.comfy-base-venv" 2>/dev/null || echo present)"
  [ -L "$v/bin/python" ] && link="$(readlink "$v/bin/python" 2>/dev/null || echo "?")" || true
  if [ ! -x "$v/bin/python" ]; then tgt="MISSING — dangling symlink"; rc=1; fi
  home="$(_base_venv_base_home "$v")"; [ -n "$home" ] || home="none (no pyvenv.cfg)"
  verdict="$(_base_venv_base_verdict "$v")"
  _base_venv_base_is_persistent "$v" || rc=1
  echo "  $v   ($(_base_bytes_to_gb "$(_base_dir_bytes "$v")") GB)"
  echo "      kind          $kind"
  echo "      stamp         $stamp"
  echo "      bin/python    $link   [$tgt]"
  echo "      pyvenv home   $home"
  echo "      verdict       $verdict"
  return $rc
}
base_rescue(){ # diagnosis is test/readlink/cat, repair is mv; the only interpreter ever executed is a candidate BACKUP's
  hdr "RESCUE · $COMFY"
  local facts="$BASE_STATE/boot-facts.txt" v live_ok=0 found=0 ts; ts="$(_base_ts)"
  BASE_RESCUE_RC=0
  echo "Every venv-shaped directory beside ComfyUI:"
  for v in "$COMFY"/.venv*; do [ -d "$v" ] || continue; found=1; _base_venv_probe_ro "$v" || true; done
  [ "$found" = "1" ] || note "no .venv* directory at all — start.sh rebuilds one on the next boot"
  echo
  note "the run would use: ${VENV:-<none>}"
  case "${VENV:-}" in *.pre-*) miss "that is a BACKUP, not the venv start.sh activates — do not build into it";; esac
  # the template boot command is the one fact only a live pod can give: it says where start.sh lives
  mkdir -p "$BASE_STATE" 2>/dev/null || true
  { echo "captured $ts"
    echo "PID 1 cmdline: $(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null || echo unavailable)"
    echo "PID 1 exe:     $(readlink /proc/1/exe 2>/dev/null || echo unavailable)"
    echo "COMFY:         $COMFY"
    echo "VENV selected: ${VENV:-<none>}"
    echo "persist root:  ${BASE_PERSIST_ROOT:-<none>}"; } > "$facts" 2>/dev/null || true
  [ -s "$facts" ] && note "boot facts written: $facts" || true
  if [ -n "${VENV:-}" ] && [ -d "$VENV" ] && _base_venv_probe_ro "$VENV" >/dev/null 2>&1; then live_ok=1; fi
  if [ "$live_ok" = "1" ]; then ok "healthy — the venv start.sh activates can boot; nothing changed"; return 0; fi
  err "the venv at the template path cannot boot — this is why the pod stops seconds after starting"
  echo "  the models on the volume are untouched by any of this; nothing below re-downloads them."
  if [ "$BASE_DRY" = "1" ]; then would "restore a bootable venv at $VENV"; BASE_RESCUE_RC=1; return 0; fi
  local pristine dead; pristine="$(_base_pick_pristine_backup)"; dead="$BASE_HOME/dead-venvs"
  if [ -n "$pristine" ] && _base_confirm_yes "Restore the previous venv from $pristine? [Y/n] "; then
    mkdir -p "$dead" 2>/dev/null || true
    if [ -d "$VENV" ]; then mv "$VENV" "$dead/$(basename "$VENV")-$ts" || { err "could not move the dead venv aside"; BASE_RESCUE_RC=1; return 0; }; fi
    if mv "$pristine" "$VENV" && "$VENV/bin/python" -V >/dev/null 2>&1; then
      ok "restored: $(basename "$pristine") → $VENV ($("$VENV/bin/python" -V 2>&1))"
      note "quarantined: $dead/$(basename "$VENV")-$ts   (delete it once the pod boots)"
      note "now start the pod normally, then re-run any package to rebuild the venv properly"
      return 0
    fi
    err "the restore did not produce a working python — leaving the path empty so start.sh rebuilds it"
    BASE_RESCUE_RC=1; return 0
  fi
  if _base_confirm_yes "Move the dead venv aside so start.sh rebuilds a clean one on the next boot? [Y/n] "; then
    mkdir -p "$dead" 2>/dev/null || true
    if [ -d "$VENV" ] && mv "$VENV" "$dead/$(basename "$VENV")-$ts"; then
      ok "quarantined → $dead/$(basename "$VENV")-$ts"
      note "start the pod: the template sees a missing path and builds its own venv"
      return 0
    fi
    err "could not move $VENV aside"; BASE_RESCUE_RC=1; return 0
  fi
  note "nothing changed. To do it by hand:"
  echo "      mkdir -p '$dead' && mv '$VENV' '$dead/$(basename "$VENV")-$ts'"
  [ -n "$pristine" ] && echo "      mv '$pristine' '$VENV'      # or leave it missing and let start.sh rebuild" || true
  BASE_RESCUE_RC=1; return 0
}
