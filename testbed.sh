#!/usr/bin/env bash
# ============================================================================
#  ComfyUI testbed — a shared, real ComfyUI + node-pack tree for every project
#  folder in this directory to test against.
#
#      bash testbed.sh            # provision/update source only  (fast, ~500 MB)
#      bash testbed.sh --server   # also build a CPU venv and start ComfyUI
#      COMFY_REF=master bash testbed.sh --server   # ComfyUI at a ref NEWER than the newest release (default: the newest release)
#  3.0.0: always the newest. ComfyUI at its newest release, every pack at its remote's HEAD, the newest Python uv
#  provides, torch upgraded, and every requirement installed from one pin-free set (py/reqlift.py), so ComfyUI's
#  own == pins (its frontend packages) never hold anything below its newest. --latest is accepted and changes nothing.
#      bash testbed.sh --packs    # the node packs alone, each to its tip (3.6.1); ComfyUI left as it is
#      bash testbed.sh --stop     # stop the server
#      bash testbed.sh --status   # what is here and what is running
#
#  Then, from any package folder that runs on ComfyUI Base:
#      bash "<name>-script.sh" test             # auto-detects base/testbed and the server on the port --server took
#  Overrides: BASE_NODE_SRC="<this dir>/testbed"  BASE_SERVER=127.0.0.1:8199
#  Which packages: $BASE_WORKSPACE, else <repo> of <repo>/base/comfyui-base (3.6.0)
#  (every package with a suite finds it the same way: BASE_NODE_SRC / BASE_SERVER, auto-detected by `test`;
#   --server writes the port it chose to .testbed-server.port beside this script, which the base's runner
#   and its _build/verify.sh read; --stop removes it)
#
#  WHICH PACKS
#  The base derives them: `base.sh list-packs <every package py/pkgdirs.py finds>` merges the four
#  shared base packs with every converted package's PACKS table, pinned to the
#  commits the pod installs. EXTRA below would hold the packs of packages NOT yet
#  converted onto the base; each conversion deletes its rows here.
#
#  WHY IT LIVES HERE, NOT AT ~/ComfyUI
#  Pod discovery scans $HOME/ComfyUI, /workspace/* and /ComfyUI. A tree at
#  ~/ComfyUI would be found by a plain `bash <name>-script.sh` run, which would
#  then rebuild its venv and restart it. This path is invisible to that scan.
#
#  WHAT THIS CAN AND CANNOT PROVE
#  ✔ source: every node type, widget order and input name the workflow uses,
#    read from each pack's real source — the checks that catch a pod install
#    failing to load the graph.
#  ✔ server (--server): the pure-Python packs import and register under a
#    current Python, and /object_info answers.
#  ✘ CUDA: this host has no NVIDIA GPU. SolAttn (Triton), the Nvidia RTX nodes
#    and the int8-ConvRot loader (comfy-kitchen) cannot import here.
#  ✘ rendering: needs the weights and an sm_120 GPU. That is the pod.
# ============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFY="$HERE/testbed"
CN="$COMFY/custom_nodes"
VENV="$COMFY/.venv-testbed"
PORT="${TESTBED_PORT:-8199}"
LOG="$HERE/.testbed-server.log"
PIDF="$HERE/.testbed-server.pid"
PORTF="$HERE/.testbed-server.port"   # the port a running server took; read by the base's runner and verify.sh (base 2.2.0)
COMFY_REF="${COMFY_REF:-}"          # empty = the newest release tag; else a branch, a newer tag or a full commit (3.0.0: COMFY_TAG is retired)
# The base is either this script's own directory (testbed.sh ships in the ComfyUI Base repository) or a
# comfyui-base/ beside it (a brand repository keeps the base as a submodule at base/comfyui-base).
if [ -f "$HERE/base.sh" ]; then BASE_SH="$HERE/base.sh"; else BASE_SH="$HERE/comfyui-base/base.sh"; fi
REQLIFT="$(dirname "$BASE_SH")/py/reqlift.py"
LATEST=0

GREEN='\033[0;32m'; YEL='\033[1;33m'; CYA='\033[0;36m'; NC='\033[0m'
[ -t 1 ] || { GREEN=''; YEL=''; CYA=''; NC=''; }
ok(){ echo -e "${GREEN}  ✔ $*${NC}"; }
note(){ echo "  ○ $*"; }
warn(){ echo -e "${YEL}  ✖ $*${NC}"; }
hdr(){ echo -e "\n${CYA}══ $* ══${NC}"; }

# dir | git url — packs of packages NOT yet converted onto ComfyUI Base (unpinned, pulled --ff-only).
# The four shared base packs are never listed here: list-packs supplies them, pinned.
EXTRA=(
  # empty since 2026-09-04: every package that declares packs runs on the base, and the Replacer, Continuer and
  # Klein installers will declare theirs the same way — nothing is hand-kept here any more
)

# A package's own node pack: <package dir>/<Pack>/__init__.py. Derived, never hand-kept — nothing
# here knows a brand's name, and the list used to go stale every time a package was added or moved. In a
# standalone base checkout the glob matches nothing and no links are made, which is correct: there are no
# packages to vendor. Absolute paths, so the link loop does not re-anchor them.
VENDORED=()

# Where the packages are (3.6.0): py/pkgdirs.py, the rule every walker in the base shares. The consumer root is
# $BASE_WORKSPACE when set (a workspace of one repository per package, <name>/ side by
# side, with the base read from $COMFY_BASE outside it), else <repo> when the base is <repo>/base/comfyui-base (a
# brand repository's submodule, packages at <brand>/packages/<name>/). A standalone base has no root and no packages,
# which is correct: list-packs still supplies the base's own rows.
# Fail closed: an empty answer is "no consumer root"; a missing helper, a crash or a root that does not exist stops here,
# never a testbed that quietly provisions the base's packs and none of the packages' (the 2.6.0 fault, status() below).
die(){ warn "$*"; exit 1; }
REPO_ROOT=""; PKGS=()
PKGDIRS_PY="$(dirname "$BASE_SH")/py/pkgdirs.py"
if [ -f "$BASE_SH" ]; then
  [ -f "$PKGDIRS_PY" ] || die "no $PKGDIRS_PY: the base beside this script predates 3.6.0 and cannot say where the packages are"
  REPO_ROOT="$(python3 "$PKGDIRS_PY" --root)" || die "py/pkgdirs.py could not resolve the consumer root (above)"
  if [ -n "$REPO_ROOT" ]; then
    _pkgs="$(python3 "$PKGDIRS_PY" "$REPO_ROOT")" || die "py/pkgdirs.py could not list $REPO_ROOT (above)"
    while IFS= read -r _d; do [ -n "$_d" ] && PKGS+=("$_d"); done <<< "$_pkgs"
  fi
fi

derive_vendored(){
  local p d
  for p in ${PKGS[@]+"${PKGS[@]}"}; do
    for d in "$p"/*/; do
      [ -d "$d" ] || continue
      case "$(basename "$d")" in _*) continue;; esac      # _build/ and friends are not node packs
      [ -f "$d/__init__.py" ] || continue
      VENDORED+=("${d%/}")
    done
  done
}
derive_vendored

# PACKS rows are dir|url|sha|branch (sha empty = unpinned; branch empty = the remote's HEAD, else the branch a base or
# package row names to track, 3.6.1). Derived first, EXTRA appended for dirs not yet covered.
PACKS=()
_has_pack(){ local r; for r in ${PACKS[@]+"${PACKS[@]}"}; do [ "${r%%|*}" = "$1" ] && return 0; done; return 1; }
derive_packs() {
  local dir url sha branch row
  if [ -f "$BASE_SH" ]; then
    while IFS='|' read -r dir url sha branch; do
      if [ -n "$dir" ] && ! _has_pack "$dir"; then PACKS+=("$dir|$url|$sha|$branch"); fi
    done < <(bash "$BASE_SH" list-packs ${PKGS[@]+"${PKGS[@]}"} 2>/dev/null || true)
  else
    warn "no ComfyUI Base beside this script — only EXTRA packs are provisioned"
  fi
  for row in ${EXTRA[@]+"${EXTRA[@]}"}; do      # EXTRA may be empty (bash 3.2 + set -u)
    dir="${row%%|*}"
    if ! _has_pack "$dir"; then PACKS+=("$row||"); fi
  done
}
derive_packs

_latest_tag() {
  git ls-remote --tags --refs https://github.com/comfyanonymous/ComfyUI 2>/dev/null \
    | awk -F/ '{print $NF}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1
}

provision_packs() { # every derived pack cloned, or moved to the tip of its row's branch, else its remote's HEAD
  mkdir -p "$CN"
  local row dir url sha branch
  for row in ${PACKS[@]+"${PACKS[@]}"}; do          # PACKS may be empty (bash 3.2 + set -u)
    IFS='|' read -r dir url sha branch <<< "$row"
    if [ ! -d "$CN/$dir/.git" ]; then
      if git clone -q --depth 1 ${branch:+--branch "$branch"} "$url" "$CN/$dir" 2>/dev/null; then ok "$dir (cloned)"; else warn "$dir — clone failed ($url)"; continue; fi
    fi
    # 3.0.0: what the machines install, the remote's HEAD (the row's sha is only the last-tested record); 3.6.1: the tip
    # of the row's branch when it names one, so a testbed cloned from a stale HEAD moves across too
    if git -C "$CN/$dir" fetch -q --depth 1 origin "${branch:-HEAD}" >/dev/null 2>&1 && git -C "$CN/$dir" checkout -q --detach FETCH_HEAD 2>/dev/null; then
      ok "$dir @ $(git -C "$CN/$dir" rev-parse --short HEAD) (${branch:-HEAD})"
    else warn "$dir: could not move to ${branch:+the tip of $branch, }its remote's HEAD"; fi
  done
}

provision_source() {
  hdr "SOURCE"
  local tag; tag="$(_latest_tag)"
  [ -n "$tag" ] || { warn "could not reach github to read ComfyUI's tags"; return 1; }
  if [ -n "${COMFY_TAG:-}" ]; then note "COMFY_TAG is retired (3.0.0): the newest release, or COMFY_REF for something newer"; fi
  if [ -n "$COMFY_REF" ]; then                        # a ref newer than the release: fetched and checked out detached
    if [ ! -d "$COMFY/.git" ]; then
      note "cloning ComfyUI at $COMFY_REF …"
      git clone -q --depth 1 --branch "$COMFY_REF" https://github.com/comfyanonymous/ComfyUI "$COMFY" \
        || { git init -q "$COMFY" && git -C "$COMFY" remote add origin https://github.com/comfyanonymous/ComfyUI \
             && git -C "$COMFY" fetch -q --depth 1 origin "$COMFY_REF" && git -C "$COMFY" checkout -q --detach FETCH_HEAD; } \
        || { warn "could not clone ComfyUI at $COMFY_REF"; return 1; }
    elif git -C "$COMFY" fetch -q --depth 1 origin "$COMFY_REF" 2>/dev/null; then
      git -C "$COMFY" checkout -q --detach FETCH_HEAD
    else
      warn "could not fetch ComfyUI $COMFY_REF; the tree is left as it was"
    fi
    ok "ComfyUI → $COMFY_REF ($(git -C "$COMFY" rev-parse --short HEAD)); the newest release is $tag"
  elif [ -d "$COMFY/.git" ]; then
    git -C "$COMFY" fetch --depth 1 origin "refs/tags/$tag:refs/tags/$tag" >/dev/null 2>&1 || true
    git -C "$COMFY" checkout -q "$tag" 2>/dev/null || true
    ok "ComfyUI → $tag (the newest release)"
  else
    note "cloning ComfyUI $tag …"
    git clone -q --depth 1 --branch "$tag" https://github.com/comfyanonymous/ComfyUI "$COMFY"
    ok "ComfyUI $tag"
  fi
  provision_packs
  # Packages' own vendored packs (no git URL; the pod copies them from the package, the testbed links them
  # so an edit is live on the next server restart). Keep this list in step with each package's VENDORED_PACKS.
  local v
  for v in ${VENDORED[@]+"${VENDORED[@]}"}; do
    local d="$CN/$(basename "$v")"
    if [ ! -f "$v/__init__.py" ]; then warn "vendored pack missing: $v"
    elif [ -d "$d" ] && [ ! -L "$d" ]; then note "$(basename "$v") is a real copy in custom_nodes — left alone (replace it with a link by hand if you want edits live)"
    else ln -sfn "$v" "$d"; ok "$(basename "$v") (vendored, linked)"; fi
  done
  echo
  note "source tree: $COMFY  ($(du -sh "$COMFY" 2>/dev/null | cut -f1)) · ${#PACKS[@]} packs + ${#VENDORED[@]} vendored"
  note "use it with:  BASE_NODE_SRC=\"$COMFY\"   (converted packages find it on their own)"
}

provision_server() {
  hdr "VENV + SERVER (CPU)"
  command -v uv >/dev/null || { warn "uv is not installed — needed to build the venv"; return 1; }
  local mm have cands c
  # 3.7.1: the newest CPython minor the torch family resolves on, the rule _base_python_pick follows since 3.6.1. "The
  # newest uv lists" handed 3.15 to the venv before torch shipped a cp315 wheel, and every fresh testbed (and every
  # package's CI, which provisions one) failed at "installing torch". The same index the install below uses (PyPI).
  cands="$(uv python list 2>/dev/null | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV)"
  mm=""
  for c in $cands; do
    if printf 'torch\ntorchvision\ntorchaudio\n' | uv pip compile --quiet --python-version "$c" - >/dev/null 2>&1; then mm="$c"; break; fi
  done
  [ -n "$mm" ] || mm="$(printf '%s\n' $cands | head -1)"      # nothing resolved (offline?): the newest, as before
  have="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ -x "$VENV/bin/python" ] && [ -n "$mm" ] && [ "$have" != "$mm" ]; then note "the venv is Python $have, the newest torch resolves on is $mm: rebuilding it"; rm -rf "$VENV"; fi
  if [ ! -x "$VENV/bin/python" ]; then
    note "creating a Python ${mm:-3} venv …"
    uv venv --python "${mm:-3}" "$VENV" >/dev/null
  fi
  uv python upgrade "${mm:-}" >/dev/null 2>&1 || true
  note "installing torch (CPU) + every requirement at its newest: this is the slow part …"
  uv pip install -q --upgrade --python "$VENV/bin/python" torch torchvision torchaudio >/dev/null
  # one pin-free set: ComfyUI's own == pins (its frontend packages) and every pack's are lifted by the base's reqlift
  local row dir files=("ComfyUI=$COMFY/requirements.txt") derived="$COMFY/.testbed-derived.txt"   # inside the ignored testbed tree
  for row in "${PACKS[@]}"; do
    dir="${row%%|*}"
    [ -f "$CN/$dir/requirements.txt" ] && files+=("$dir=$CN/$dir/requirements.txt")
  done
  if [ -f "$REQLIFT" ] && uv run --no-project --quiet --with packaging python "$REQLIFT" --out "$derived" --map "$derived.map" "${files[@]}" >/dev/null; then
    if uv pip install -q --upgrade --python "$VENV/bin/python" --index-strategy unsafe-best-match -r "$derived" pytest >/dev/null 2>&1; then
      ok "every requirement at its newest ($(grep -c . "$derived") lines, one resolve)"
    else
      # best effort on a Mac: several packs are CUDA-only and will not resolve here; install what does, pack by pack
      warn "the full set does not resolve on this host (expected for CUDA-only packs): installing it pack by pack"
      uv pip install -q --upgrade --python "$VENV/bin/python" -r "$COMFY/requirements.txt" pytest >/dev/null 2>&1 || true
      local f
      for f in "${files[@]}"; do
        uv run --no-project --quiet --with packaging python "$REQLIFT" --out "$derived.one" --map /dev/null "$f" >/dev/null 2>&1 || continue
        uv pip install -q --upgrade --python "$VENV/bin/python" --index-strategy unsafe-best-match -r "$derived.one" >/dev/null 2>&1 \
          && ok "reqs: ${f%%=*}" || warn "reqs: ${f%%=*}: some deps do not resolve on this host (expected for CUDA-only packs)"
      done
      rm -f "$derived.one"
    fi
  else
    warn "no reqlift beside the base: installing ComfyUI's requirements as written (its == pins hold the frontend back)"
    uv pip install -q --upgrade --python "$VENV/bin/python" -r "$COMFY/requirements.txt" pytest >/dev/null
  fi
  "$VENV/bin/python" -c 'import importlib.metadata as m; print("  frontend " + m.version("comfyui-frontend-package"))' 2>/dev/null || true
  stop_server
  note "starting ComfyUI --cpu on port $PORT …"
  ( cd "$COMFY" && nohup "$VENV/bin/python" main.py --cpu --port "$PORT" --disable-auto-launch \
      >"$LOG" 2>&1 & echo $! >"$PIDF" )
  echo "$PORT" >"$PORTF"     # the base's runner (lib/95-summary.sh) and _build/verify.sh read this to find the server
  local i
  for i in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then
      ok "server up at http://127.0.0.1:$PORT (pid $(cat "$PIDF"), port recorded in $PORTF)"; return 0
    fi
    sleep 2
  done
  rm -f "$PORTF"
  warn "server did not answer within 240 s — see $LOG"; tail -30 "$LOG" || true; return 1
}

stop_server() {
  # $! inside the subshell is the SUBSHELL, not python — nohup's child ends up one pid along.
  # Kill whatever is actually listening on the port, then fall back to the recorded pid.
  # NB: lsof exits 1 when nothing matches, and `set -o pipefail` propagates that, so every
  # lookup here needs `|| true` or `set -e` kills the caller before the server ever starts.
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)"
  fi
  if [ -z "$pids" ] && [ -f "$PIDF" ]; then pids="$(cat "$PIDF" 2>/dev/null || true)"; fi
  local p
  for p in $pids; do
    kill -0 "$p" 2>/dev/null || continue
    kill "$p" 2>/dev/null || true
    ok "stopped pid $p"
  done
  if [ -n "$pids" ]; then
    sleep 2
    for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
  fi
  rm -f "$PIDF" "$PORTF"
}

status() {
  hdr "STATUS"
  [ -d "$COMFY/.git" ] && ok "ComfyUI $(git -C "$COMFY" describe --tags 2>/dev/null || echo '?') · $(ls -1 "$CN" 2>/dev/null | wc -l | tr -d ' ') packs" || note "no source tree yet"
  [ -x "$VENV/bin/python" ] && ok "venv $($VENV/bin/python -V 2>&1)" || note "no venv"
  if curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then ok "server answering on $PORT"; else note "server not running"; fi
  [ -f "$PORTF" ] && note "port file: $PORTF ($(tr -d '[:space:]' < "$PORTF"))"
  # what a provision WOULD install, and where the packages were found. Silent derivation is how 2.6.0 shipped
  # a testbed that quietly provisioned the base's packs and none of a consumer repository's.
  note "packs: ${#PACKS[@]} derived${REPO_ROOT:+ · packages under $REPO_ROOT (${#PKGS[@]})}${REPO_ROOT:+ · ${#VENDORED[@]} vendored}"
  echo; note "BASE_NODE_SRC=\"$COMFY\"   BASE_SERVER=127.0.0.1:$PORT"
}

case "${1:-}" in
  "")        provision_source; status;;
  --latest)  LATEST=1; provision_source; status;;
  --server)  provision_source; provision_server; status;;
  --packs)   provision_packs; status;;   # 3.6.1: the node packs alone (each to its tip), ComfyUI left as it is
  --stop)    stop_server;;
  --status)  status;;
  *) echo "usage: bash testbed.sh [--server | --latest | --packs | --stop | --status]"; exit 2;;
esac
