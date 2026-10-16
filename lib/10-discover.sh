# 10-discover.sh — find the pod's ComfyUI, its library, its venv, its launch args; the banner; the driver gate.

_base_logging(){ # every run leaves a log in the base's state dir (the volume on the pod, the fake root in tests)
  local ts; ts="$(_base_ts)"
  BASE_LOG="$BASE_STATE/logs/${PKG_ID:-base}_$ts.log"
  mkdir -p "$(dirname "$BASE_LOG")" 2>/dev/null || true
  exec > >(tee -a "$BASE_LOG") 2>&1
  BASE_LOGGING=1
}

_base_comfy_version(){ # <comfy dir> → x.y.z from comfyui_version.py, else pyproject.toml, else 0.0.0 loudly
  local d="$1" v=""
  if [ -f "$d/comfyui_version.py" ]; then
    v="$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']([0-9.]+)["'"'"'].*/\1/p' "$d/comfyui_version.py" | head -1)"
  fi
  if [ -z "$v" ] && [ -f "$d/pyproject.toml" ]; then
    v="$(sed -nE 's/^version[[:space:]]*=[[:space:]]*["'"'"']([0-9.]+)["'"'"'].*/\1/p' "$d/pyproject.toml" | head -1)"
  fi
  if [ -z "$v" ]; then
    echo "    !! ComfyUI version unreadable in $d (no comfyui_version.py, no pyproject.toml) — reporting 0.0.0" >&2
    v="0.0.0"
  fi
  echo "$v"
}

_base_gpu_headroom(){ # the GPU must be ours: memory no visible process holds belongs to another container or a leaked host context
  # (2.0.26: 93.5 GB held outside pod fakepod0000003, 0 % util — every check passed and the first render died at its first node)
  local line="${BASE_GPU_LINE:-verdict=no-gpu}" v total used ours foreign
  v="$(printf '%s' "$line" | sed -n 's/.*verdict=\([A-Za-z-]*\).*/\1/p')"
  case "$v" in
    ours)
      total="$(printf '%s' "$line" | sed -n 's/.*total=\([0-9]*\).*/\1/p')"; used="$(printf '%s' "$line" | sed -n 's/.*used=\([0-9]*\).*/\1/p')"
      ours="$(printf '%s' "$line" | sed -n 's/.*ours=\([0-9]*\).*/\1/p')"
      echo "  gpu memory   $((total / 1024)) GiB total · $((used / 1024)) GiB in use · $((ours / 1024)) GiB ours — the GPU is ours";;
    NOT-OURS)
      total="$(printf '%s' "$line" | sed -n 's/.*total=\([0-9]*\).*/\1/p')"; ours="$(printf '%s' "$line" | sed -n 's/.*ours=\([0-9]*\).*/\1/p')"
      foreign="$(printf '%s' "$line" | sed -n 's/.*foreign=\([0-9]*\).*/\1/p')"
      err "gpu memory   $((foreign / 1024)) GiB of $((total / 1024)) GiB are held by processes OUTSIDE this container (ours: $((ours / 1024)) GiB) — the GPU is not ours; every render will die at its first node with the card idle"
      echo "               fix: stop and start the pod (a fresh container — the leak may live on this machine), else redeploy the pod on the volume (a new machine), else the host's support with the machine id; nothing in this pod can free it"
      BASE_FAILED+=("gpu: $((foreign / 1024)) GiB of VRAM held outside this container — stop/start or redeploy the pod");;
    *) note "gpu memory   not verifiable here (no nvidia-smi answer)";;
  esac
  return 0
}

_base_gpu_fit(){ # 3.8.0: GPU_PREFER="B200 B300" names the GPUs a package is tuned for; a GPU below them is a warning, never a stop
  [ -n "${GPU_PREFER:-}" ] || return 0
  local line fit gpu sm gb want
  if [ -n "${BASE_FAKE_GPU:-}" ]; then          # the suite's GPU: "name|compute capability|MiB"
    IFS='|' read -r gpu sm gb <<< "$BASE_FAKE_GPU"
    line="$("$SYS_PY" "$BASE_DIR/py/gpu_fit.py" --prefer "$GPU_PREFER" --name "$gpu" --cc "$sm" --mib "$gb" 2>/dev/null || true)"
  elif [ -z "$BASE_FAKE_ROOT" ] && command -v nvidia-smi >/dev/null 2>&1; then
    line="$("$SYS_PY" "$BASE_DIR/py/gpu_fit.py" --prefer "$GPU_PREFER" 2>/dev/null || true)"
  else return 0; fi
  IFS=$'\t' read -r fit gpu sm gb want <<< "$line"            # tab-separated key=value (py/gpu_fit.py): names have spaces
  fit="${fit#fit=}"; gpu="${gpu#gpu=}"; sm="${sm#sm=}"; gb="${gb#gb=}"; want="${want#want=}"
  case "$fit" in
    ok)    echo "  gpu fit      $gpu is what ${PKG_NAME:-this package} is tuned for ($want)";;
    below) warn "GPU $gpu ($gb GB, $sm) is below what ${PKG_NAME:-this package} is tuned for: $want. It runs, slower or with offloading; deploy one of those for the speed and quality the handbook states";;
    *)     note "gpu fit      not verifiable here (no nvidia-smi answer); ${PKG_NAME:-this package} is tuned for $want";;
  esac
  return 0
}

_base_vol(){ # the ONE filesystem a stop/start keeps: BASE_VOLUME on a machine, the fake root in tests, COMFY_DIR's grandparent off-pod
  if [ -n "$BASE_FAKE_ROOT" ]; then echo "$BASE_FAKE_ROOT"
  elif [ -d "$BASE_VOLUME" ]; then echo "$BASE_VOLUME"
  elif [ -n "${COMFY_DIR:-}" ]; then dirname "$(dirname "$COMFY_DIR")"
  else echo "${HOME:-/root}"; fi
}
_base_volume_ok(){ # on a pod BASE_VOLUME must be a mount (the volume), never a directory on the container disk; on an owned box (BASE_VOLUME_KIND=dir) a directory is the volume
  if [ -n "$BASE_FAKE_ROOT" ]; then [ "$BASE_FAKE_NO_VOLUME" != "1" ]; return $?; fi
  if [ "${BASE_VOLUME_KIND:-mount}" = dir ]; then [ -d "$BASE_VOLUME" ]; return $?; fi
  if _base_on_pod; then
    [ -d "$BASE_VOLUME" ] && awk -v v="$BASE_VOLUME" '$2==v{f=1} END{exit !f}' /proc/mounts 2>/dev/null; return $?
  fi
  return 0
}
_base_recorded_tree(){ # the canonical tree an earlier install recorded (state/boot.env): it wins over any other tree on the volume
  local f="$BASE_STATE/boot.env" t
  [ -f "$f" ] || return 0
  t="$(sed -n 's/^COMFY=//p' "$f" | head -1)"
  [ -n "$t" ] && [ -f "$t/main.py" ] && echo "$t" || true
}
base_discover(){ # base_discover [quiet] → 3 when the pod has no network volume, 4 when the volume holds no ComfyUI tree
  # ComfyUI is found by main.py, never by models/ — a stray tree has a models/ folder too. Only trees ON THE VOLUME
  # count: the container disk (/ComfyUI, /opt/ComfyUI, $HOME/ComfyUI) is restored from the image at every start.
  local quiet="${1:-}"
  COMFY=""; RUN_PID=""; RUN_PY=""; BASE_NO_TREE=0; BASE_OTHER_TREES=""
  VOL="$(_base_vol)"
  if ! _base_volume_ok; then
    [ -n "$quiet" ] || err "REFUSED: $VOL is not a mounted volume (a network volume on a pod, a block volume on a VM) — nothing installed here would survive a stop/start. Attach one at $BASE_VOLUME (BASE_VOLUME) and deploy again; on an owned box set BASE_HOST=local (hosts/local/README.md)."
    return 3
  fi
  local cands=() c rec
  rec="$(_base_recorded_tree)"
  if [ -n "$BASE_FAKE_ROOT" ]; then
    # COMFY_DIR is exported by suite runners; inherited into a fake run it pointed a whole script at the
    # real tree once. Inside the fake root only — an outside COMFY_DIR is refused, not honoured.
    local fake_cd=""
    case "${COMFY_DIR:-}/" in "$BASE_FAKE_ROOT"/*) fake_cd="${COMFY_DIR:-}";; esac
    if [ -n "${COMFY_DIR:-}" ] && [ -z "$fake_cd" ]; then
      echo "  ○ BASE_FAKE_ROOT set: ignoring COMFY_DIR=$COMFY_DIR (outside the fake root)" >&2
    fi
    cands=("$fake_cd" "$rec" "$BASE_FAKE_ROOT/ComfyUI" "$BASE_FAKE_ROOT/runpod-slim/ComfyUI")
  else
    cands=("${COMFY_DIR:-}" "$rec")
    if [ -n "${COMFY_DIR:-}" ]; then case "${COMFY_DIR}/" in "$VOL"/*) ;; *) warn "COMFY_DIR=$COMFY_DIR is not under $VOL — whatever is installed there is lost at the next stop/start";; esac; fi
    if [ -d /proc ] && command -v pgrep >/dev/null 2>&1; then
      local pid cwd
      for pid in $(pgrep -f '[p]ython[0-9.]* .*main\.py' 2>/dev/null || true); do
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
        if [ -n "$cwd" ] && [ -f "$cwd/main.py" ]; then
          RUN_PID="$pid"
          # /proc/PID/exe RESOLVES symlinks onto the base interpreter a venv was built from; Python finds
          # its venv beside sys.executable, so that binary cannot see the venv's packages. argv[0] keeps it.
          RUN_PY="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1 || true)"
          case "$RUN_PY" in
            /*)  ;;                            # absolute — use it
            */*) RUN_PY="$cwd/$RUN_PY" ;;      # relative — resolve against the process cwd
            *)   RUN_PY="" ;;                  # bare name ("python3") — carries no venv
          esac
          if [ -z "$RUN_PY" ] || [ ! -x "$RUN_PY" ]; then RUN_PY="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"; fi
          case "$cwd/" in "$VOL"/*) cands+=("$cwd");; esac      # a process running the image's own tree is not a candidate
          break
        fi
      done
    fi
    cands+=("$VOL/runpod-slim/ComfyUI" "$VOL/ComfyUI" "$VOL/madapps/ComfyUI")
    for c in "$VOL"/*/ComfyUI; do [ -d "$c" ] && cands+=("$c") || true; done
  fi
  local seen=" " real
  for c in "${cands[@]}"; do
    [ -n "$c" ] && [ -f "$c/main.py" ] || continue
    real="$(cd "$c" && pwd -P)"
    case "$seen" in *" $real "*) continue;; esac
    seen="$seen$real "
    if [ -z "$COMFY" ]; then COMFY="$real"; else BASE_OTHER_TREES="$BASE_OTHER_TREES$real"$'\n'; fi
  done
  if [ -z "$COMFY" ]; then
    BASE_NO_TREE=1
    [ -n "$quiet" ] || note "no ComfyUI tree on $VOL (looked in: ${cands[*]}) — the install materialises one at $VOL/ComfyUI"
    return 4
  fi
  CN="$COMFY/custom_nodes"
  # The base owns ComfyUI's flags: state/comfyui_args.txt, on every image. An image's own args file (the official
  # template's runpod-slim/comfyui_args.txt) is imported ONCE, on the first real run, and never read again — the
  # boot is the base's, so the image's file has no say afterwards.
  ARGS_FILE="$BASE_STATE/comfyui_args.txt"; ARGS_IMPORT=""
  if [ ! -f "$ARGS_FILE" ]; then
    local t
    for t in "$VOL/runpod-slim/comfyui_args.txt" "$COMFY/../comfyui_args.txt"; do
      if [ -f "$t" ]; then ARGS_IMPORT="$(cd "$(dirname "$t")" && pwd -P)/$(basename "$t")"; break; fi
    done
  fi
  COMFY_LOG="$BASE_STATE/logs/comfyui.log"

  # the library: the SHARED library when the machine has one (2.4.0), else extra_model_paths.yaml's first
  # base_path if it IS a library (holds category folders), else its models/ child, else $COMFY/models.
  # BASE_LIBRARY wins over the yaml on purpose: it is true before the yaml exists, on the very first install of
  # a fresh machine, which is exactly when a download to the wrong place would cost a second copy of 125 GB.
  EXTRA_YAML="$COMFY/extra_model_paths.yaml"
  # 2.5.13: a models/ symlink left pointing OUTSIDE the volume, on a root that is now the whole store.
  #
  # `_base_library_link` (70-hygiene.sh) made `$COMFY/models` a symlink to `$BASE_LIBRARY/models` back when a
  # machine had a separate library mounted at /mnt/comfy-library. That symlink lives ON THE STORE, so it outlives
  # the machine that made it and every machine that mounts the store inherits it. Since 2.5.2 `BASE_LIBRARY` is
  # correctly ignored when the whole root IS the store, which means the function that created the link now returns
  # at its first line and can never repair it. While the store happened to be mounted twice the link still
  # resolved and nobody saw it; the moment that second mount stopped being made, it dangled.
  #
  # MEASURED 2026-09-19 on a fresh boot: `/workspace/ComfyUI/models -> /mnt/comfy-library/models`, target absent,
  # `df` on it fails, and the disk gate therefore reported 0.00 GB free on a store with 272 GB free and refused
  # every install. ComfyUI's own models directory did not resolve at all. Repaired here rather than in hygiene
  # because hygiene runs AFTER the model step, which is the step this breaks.
  if [ "$BASE_VOLUME_SHARED" = "1" ] && [ -L "$COMFY/models" ]; then
    local _ml_t _ml_vol
    _ml_t="$(readlink "$COMFY/models")"
    _ml_vol="${BASE_FAKE_ROOT:-$BASE_VOLUME}"        # the suite runs a whole pod under a temp root
    case "$_ml_t" in
      "$_ml_vol"/*) : ;;                             # already inside the store: leave it exactly as it is
      *)
        if [ "$BASE_DRY" = "1" ]; then
          would "repoint $COMFY/models ($_ml_t is outside $_ml_vol) at $_ml_vol/models"
        else
          mkdir -p "$_ml_vol/models" 2>/dev/null || true
          if ln -sfn "$_ml_vol/models" "$COMFY/models" 2>/dev/null; then
            BASE_CHANGED+=("models/ repointed from $_ml_t to $_ml_vol/models")
            note "models/ pointed outside the store at $_ml_t — repointed at $_ml_vol/models"
          else
            warn "models/ points outside the store at $_ml_t and could not be repointed"
          fi
        fi ;;
    esac
  fi
  M="$COMFY/models"; M_SRC="default"
  if [ -n "$BASE_LIBRARY" ] && [ -d "$BASE_LIBRARY" ]; then
    M="$BASE_LIBRARY/models"; M_SRC="BASE_LIBRARY (shared)"
    mkdir -p "$M" 2>/dev/null || true
  elif [ -f "$EXTRA_YAML" ]; then
    local base
    base="$(grep -m1 -E '^[[:space:]]+base_path:' "$EXTRA_YAML" | sed -E 's/^[[:space:]]+base_path:[[:space:]]*//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' || true)"
    base="${base%/}"
    if [ -n "$base" ] && [ -d "$base" ]; then
      if [ -d "$base/diffusion_models" ] || [ -d "$base/loras" ] || [ -d "$base/checkpoints" ] || [ -d "$base/vae" ] || [ -d "$base/text_encoders" ]; then
        M="$base"; M_SRC="extra_model_paths.yaml"
      elif [ -d "$base/models" ]; then M="$base/models"; M_SRC="extra_model_paths.yaml (base_path/models)"; fi
    fi
  fi
  # staging sits on the library's own filesystem (never /tmp, which is container disk, and never a second device:
  # a finished download is renamed into place, not copied). On a SHARED library several machines stage into the
  # same tree, so each gets its own subdirectory and two concurrent installs can never write one another's
  # half-file. BASE_PRUNE drops .comfy-base-staging by NAME, so every machine's subdirectory stays out of the index.
  STAGING_ROOT="$M/.comfy-base-staging"
  if [ -n "$BASE_LIBRARY" ]; then STAGING="$STAGING_ROOT/$(hostname -s 2>/dev/null || echo machine)"; else STAGING="$STAGING_ROOT"; fi

  # the venv: $COMFY/.venv* first (start.sh activates exactly that path), then the usual places
  VENV=""; local v first_exec=""
  for v in "$COMFY"/.venv*; do
    [ -d "$v" ] || continue
    # Our own backup is "$VENV.pre-<who>-<ts>", which also matches .venv*. A backup may only ever be a
    # restore source, never the venv we build into.
    case "$v" in *.pre-*) continue;; esac
    if [ -f "$v/.comfy-base-venv" ]; then VENV="$v"; break; fi
    if [ -z "$first_exec" ] && [ -x "$v/bin/python" ]; then first_exec="$v"; fi
  done
  [ -z "$VENV" ] && VENV="$first_exec" || true
  if [ -z "$VENV" ]; then
    # /venv and /opt/venv are CONTAINER DISK; building into one is another route to the same brick
    # a fake pod (BASE_FAKE_ROOT) consults container-disk paths only through BASE_FAKE_IMAGE_ROOT — never the real
    # /venv or /opt/venv (2.0.24: the qwen template's own /opt/venv got a fake stamp from a dry run's suite)
    local _img="" _vol; _vol="$(_base_vol)"
    if [ -n "$BASE_FAKE_ROOT" ]; then _img="${BASE_FAKE_IMAGE_ROOT:-/nonexistent/fake-image}"; fi
    for v in "$COMFY/venv" "$_vol/venv" "$_img/venv" "$_img/opt/venv"; do
      [ -x "$v/bin/python" ] || continue
      if [ -n "$BASE_PERSIST_ROOT" ]; then case "$v/" in "$BASE_PERSIST_ROOT"/*) ;; *) continue;; esac; fi
      VENV="$v"; break
    done
  fi
  VENV_PREEXISTING=1
  if [ -z "$VENV" ]; then VENV="$COMFY/.venv-cu130"; VENV_PREEXISTING=0; fi   # the template's own name
  PY="$VENV/bin/python"
  if [ -z "$RUN_PY" ] && [ -x "$PY" ]; then RUN_PY="$PY"; fi
  [ -z "$RUN_PY" ] && RUN_PY="$(command -v python3 || true)" || true
  SYS_PY="$(command -v python3 || true)"

  # GPU / driver / the interpreter that runs today
  GPU_NAME="?"; DRIVER="?"; TORCH_NOW="?"; BASE_GPU_SM=""
  if [ -z "$BASE_FAKE_ROOT" ] && command -v nvidia-smi >/dev/null 2>&1; then
    local q cc
    q="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
    GPU_NAME="${q%%,*}"; DRIVER="${q##*, }"
    cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)"
    [[ "$cc" =~ ^[0-9]+\.[0-9]+$ ]] && BASE_GPU_SM="sm_${cc%%.*}${cc##*.}" || true     # 12.0 → sm_120
    BASE_GPU_LINE="$("$SYS_PY" "$BASE_DIR/py/gpu_facts.py" 2>/dev/null || echo "verdict=no-gpu")"
    _base_gpu_headroom
  fi
  if [ -z "$BASE_FAKE_ROOT" ] && [ -x "$RUN_PY" ]; then
    TORCH_NOW="$(_base_timeout 120 "$RUN_PY" -c 'import torch; print("torch %s · CUDA %s · %s" % (torch.__version__, torch.version.cuda, ",".join(a for a in torch.cuda.get_arch_list() if a.startswith("sm_1")) or "no Blackwell arch (sm_1xx)"))' 2>/dev/null || echo "torch not importable")"
  elif [ -n "$BASE_FAKE_ROOT" ]; then TORCH_NOW="(fake root: not probed)"; fi
  PORT=8188
  local pf="" p
  if [ -f "$ARGS_FILE" ]; then pf="$ARGS_FILE"; elif [ -n "$ARGS_IMPORT" ]; then pf="$ARGS_IMPORT"; fi
  if [ -n "$pf" ]; then
    p="$(grep -v '^#' "$pf" | tr ' ' '\n' | grep -A1 -x -- '--port' | tail -1 || true)"
    [[ "$p" =~ ^[0-9]+$ ]] && PORT="$p" || true
  elif [ -n "$RUN_PID" ] && [ -r "/proc/$RUN_PID/cmdline" ]; then          # no file yet: the running process knows its port
    p="$(tr '\0' '\n' < "/proc/$RUN_PID/cmdline" 2>/dev/null | grep -A1 -x -- '--port' | tail -1 || true)"
    [[ "$p" =~ ^[0-9]+$ ]] && PORT="$p" || true
  fi
  HOSTPORT="127.0.0.1:$PORT"
}

base_banner(){
  local free_vol free_root mode="run"
  [ "$BASE_DRY" = "1" ] && mode="--check (dry run: nothing is changed)"
  [ "$BASE_NO_NET" = "1" ] && mode="$mode · BASE_NO_NET (offline, fake downloads)"
  [ -n "$BASE_FAKE_ROOT" ] && mode="$mode · BASE_FAKE_ROOT=$BASE_FAKE_ROOT"
  free_vol="$(( $(_base_free_kb "$M") / 1000000 ))"; free_root="$(( $(_base_free_kb /) / 1000000 ))"
  hdr "${PKG_NAME:-ComfyUI Base} V${PKG_VERSION:-$BASE_VERSION} · base $BASE_VERSION · $mode"
  echo "  ComfyUI      $COMFY $( [ -n "$RUN_PID" ] && echo "(running, pid $RUN_PID)" || echo "(not running)") · $(_base_comfy_version "$COMFY" 2>/dev/null)"
  local ot; while IFS= read -r ot; do [ -n "$ot" ] && echo "  other tree   $ot (its models are consolidated into the canonical tree; nothing there is deleted)"; done <<< "${BASE_OTHER_TREES:-}"
  echo "  models       $M  [$M_SRC]"
  local stamp=""; [ -f "$VENV/.comfy-base-venv" ] && stamp=" · ours: $(cat "$VENV/.comfy-base-venv")"
  echo "  venv         $VENV $( [ "$VENV_PREEXISTING" = "1" ] && echo "(present)" || echo "(none found — will be created)")$stamp"
  echo "  interpreter  ${RUN_PY:-?}$( [ -n "$RUN_PID" ] && [ "$RUN_PY" != "$PY" ] && [ -x "$PY" ] && echo " (via $VENV/bin/python)") · $TORCH_NOW"
  # The line above names the venv, which is on the volume. The line below names what that venv actually
  # BOOTS from, which is the path a pod restart can take away without warning.
  if [ -n "$BASE_PERSIST_ROOT" ] && [ -d "${VENV:-/nonexistent}" ] && declare -F _base_venv_boot_line >/dev/null; then
    _base_venv_boot_line "$VENV"                                   # 2.0.8: the home DIR was handed to a predicate expecting a venv
  fi
  echo "  GPU          $GPU_NAME · driver $DRIVER${BASE_GPU_SM:+ · $BASE_GPU_SM}"
  _base_gpu_fit
  # A RunPod volume is backed by a shared pool and df reports the POOL (702026 GB was a real reading).
  # Printed as "free" it reads like headroom you own; name it instead of quietly trusting it.
  local vol_note=""
  if _base_fs_is_pool "$M" 2>/dev/null; then vol_note="  ← shared pool, not your quota; the disk gate cannot verify headroom"; fi
  echo "  disk         volume ($M): ${free_vol} GB free · container disk (/): ${free_root} GB free${vol_note}"
  echo "  args file    $ARGS_FILE $( [ -f "$ARGS_FILE" ] || echo "(absent${ARGS_IMPORT:+ — imports $ARGS_IMPORT on the first run})") · port $PORT"
  echo "  state        $BASE_STATE"
  echo "  log          ${BASE_LOG:-—}"
}

_base_driver_gate(){ # a real gate, run AFTER base_system (3.0.0) so an upgrade can lift the driver first: CUDA wheels need driver >= DRIVER_MIN
  local want="${DRIVER_MIN:-580}" d="${BASE_DRIVER_AFTER:-$DRIVER}" maj
  case "$d" in [0-9]*) DRIVER="$d";; esac; maj="${DRIVER%%.*}"
  if [ -z "$BASE_FAKE_ROOT" ] && [[ "$maj" =~ ^[0-9]+$ ]] && [ "$maj" -lt "$want" ]; then
    err "driver $DRIVER is older than $want — CUDA 13 wheels (the only stable sm_120 wheels) need >= $want."
    echo "      Nothing was downloaded and the venv is untouched; the running server is unaffected."
    BASE_FAILED+=("driver $DRIVER < $want — stopped before any download")
    return 1
  fi
  return 0
}
