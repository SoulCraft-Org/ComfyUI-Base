#!/usr/bin/env bash
# ============================================================================
#  ComfyUI testbed — a shared, real ComfyUI + node-pack tree for every project
#  folder in this directory to test against.
#
#      bash testbed.sh            # provision/update source only  (fast, ~500 MB)
#      bash testbed.sh --server   # also build a CPU venv and start ComfyUI
#      COMFY_REF=master bash testbed.sh --server   # ComfyUI at a ref NEWER than the newest release (default: the newest release)
#  3.0.0: always the newest. ComfyUI at its newest release, every pack at its remote's HEAD, the newest Python uv
#  provides that torch ships for (3.7.1), torch upgraded, and every requirement installed from one pin-free set (py/reqlift.py), so ComfyUI's
#  own == pins (its frontend packages) never hold anything below its newest. --latest is accepted and changes nothing.
#      bash testbed.sh --packs    # the node packs alone, each to its tip (3.6.1); ComfyUI left as it is
#      bash testbed.sh --start    # the server alone, from the venv as it is (3.16.0; verify.sh starts each run's own)
#      bash testbed.sh --stop     # stop the server
#      bash testbed.sh --status   # what is here and what is running
#      bash testbed.sh --owned    # 127.0.0.1:<port> of the server THIS testbed started, else exit 1 with the reason (3.16.0)
#
#  Then, from any package folder that runs on ComfyUI Base:
#      bash "<name>-script.sh" test             # auto-detects base/testbed and the server on the port --server took
#  Overrides: BASE_NODE_SRC="<this dir>/testbed"  BASE_SERVER=127.0.0.1:<port>
#  Which packages: $BASE_WORKSPACE, else <repo> of <repo>/base/comfyui-base (3.6.0)
#  (every package with a suite finds it the same way: BASE_NODE_SRC / BASE_SERVER, auto-detected by `test`;
#   --server writes the port it chose to .testbed-server.port beside this script, and --stop removes it. 3.16.0: nothing
#   trusts that file alone any more: the runner, verify.sh and podctl ask --owned, which answers only when the pid this
#   testbed recorded is what listens on that port. TESTBED_STATE=<dir> keeps the pid, port and log files there instead,
#   which is how verify.sh gives every run a server of its own)
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
#    current Python, and /object_info answers. On Linux triton sits beside the
#    CPU torch (3.22.1), so packs that import it at module level register too.
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
# 3.16.0: where this server's pid, port and log files live. Beside this script by default; verify.sh hands each run a
# folder of its own (TESTBED_STATE), so two runs, or a run and a session's --server, never share a server (error log row 84)
STATE="${TESTBED_STATE:-$HERE}"
[ -d "$STATE" ] || mkdir -p "$STATE"
LOG="$STATE/.testbed-server.log"
PIDF="$STATE/.testbed-server.pid"
PORTF="$STATE/.testbed-server.port"   # the port a running server took (base 2.2.0); only --owned turns it into an address
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

# 3.9.2: the testbed stops only the server it started. It used to kill whatever listened on $PORT, and on a Mac shared
# by several sessions that was another checkout's ComfyUI on 8199. Ours is the pid --server recorded in $PIDF, or a
# child of it (a pid file written before 3.9.2 holds the subshell nohup's python ran under), and only while its command
# line is still a ComfyUI main.py: a pid the system has since handed to something else is never ours.
own_pids() {
  [ -f "$PIDF" ] || return 0
  local rec p
  rec="$(tr -dc '0-9' < "$PIDF" 2>/dev/null || true)"
  [ -n "$rec" ] && kill -0 "$rec" 2>/dev/null || return 0
  for p in "$rec" $(pgrep -P "$rec" 2>/dev/null || true); do
    case "$(ps -o command= -p "$p" 2>/dev/null || true)" in *main.py*--cpu*) echo "$p";; esac
  done
}

# Who listens on a port (pids), or "?" when something accepts a connection there and nothing on this host can say who.
# lsof first; then, on Linux, /proc (3.20.1: a container such as the soulcraft-hf runner has no lsof, and every holder
# read "?"). NB: lsof exits 1 when nothing matches, and `set -o pipefail` propagates that, so every lookup needs `|| true`.
port_holders() { # [port] (default $PORT)
  local port="${1:-$PORT}" pids=""
  command -v lsof >/dev/null 2>&1 && pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)"
  [ -n "$pids" ] || pids="$(proc_port_holders "$port")"
  if [ -n "$pids" ]; then echo "$pids"
  elif (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then echo "?"
  fi
}

# Linux without lsof: the LISTEN rows (st 0A) of /proc/net/tcp and tcp6 for the port give socket inodes, and the
# /proc/<pid>/fd links that name those inodes give the pids. Sees every process of this user; prints nothing elsewhere.
proc_port_holders() { # port
  local hex inodes
  [ -r /proc/net/tcp ] || return 0
  hex="$(printf '%04X' "$1")"
  inodes="$(awk -v h="$hex" 'FNR>1 && $4=="0A" && $10!="0" {n=split($2, a, ":"); if (a[n]==h) printf " %s", $10}' \
    /proc/net/tcp /proc/net/tcp6 2>/dev/null || true)"
  [ -n "$inodes" ] || return 0
  ls -l /proc/[0-9]*/fd 2>/dev/null | awk -v want="$inodes " '
    /^\/proc\/[0-9]+\/fd:$/ {split($0, a, "/"); pid = a[3]; next}
    / -> socket:\[[0-9]+\]$/ {i = $NF; gsub(/[^0-9]/, "", i); if (index(want, " " i " ")) print pid}' | sort -u || true
}

# 3.16.0: the one answer to "is there a server this testbed started, and where is it". Ours only when the recorded pid (or
# its child, see own_pids) is a live ComfyUI main.py AND is what listens on the recorded port. The port file alone proved
# nothing: verify.sh and the runner used whatever answered there, else 8199, and on a Mac where several sessions each run
# a testbed that was another checkout's server (error log row 84: the snapshot tier measured its frontend, and a server
# test lost it mid-run when its session stopped it). A foreign holder is named, never used.
owned_server() { # -> 127.0.0.1:<port> on stdout and exit 0; else the reason on stderr and exit 1
  local port pids held p
  [ -f "$PORTF" ] || { echo "no server recorded in $STATE (no .testbed-server.port)" >&2; return 1; }
  port="$(tr -dc '0-9' < "$PORTF" 2>/dev/null || true)"
  [ -n "$port" ] || { echo "$PORTF holds no port" >&2; return 1; }
  pids="$(own_pids | tr '\n' ' ')"; pids="${pids% }"
  held="$(port_holders "$port" | tr '\n' ' ')"; held="${held% }"
  if [ -z "$pids" ]; then
    if [ -n "$held" ]; then echo "port $port (recorded in $STATE) is held by pid $held, which this testbed did not start: its own server is gone" >&2
    else echo "the server recorded in $STATE is gone (stale pid and port files)" >&2; fi
    return 1
  fi
  [ -n "$held" ] || { echo "this testbed's server (pid $pids) is not listening on port $port" >&2; return 1; }
  if [ "$held" = "?" ]; then echo "127.0.0.1:$port"; return 0; fi   # no lsof: our live main.py, and its port answers
  for p in $held; do case " $pids " in *" $p "*) echo "127.0.0.1:$port"; return 0;; esac; done
  echo "port $port is held by pid $held, not by this testbed's server (pid $pids)" >&2; return 1
}

# --owned answers before anything is derived: the runner, verify.sh and podctl ask it on every run (no python, no git)
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ "${1:-}" = "--owned" ]; then owned_server; exit $?; fi

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
# Two packages may vendor the same pack (one folder name, one set of node classes), and only one copy can be linked.
# 3.20.2: the newest version, the copy a pod keeps (lib/62-own-packs.sh), never whichever package is walked last: the
# workspace's integration run linked the Video Replacer's Content Gate 1.5.0 over FLUX 3 Klein's, by name order alone.
if [ "${#VENDORED[@]}" -gt 1 ]; then
  _own="$(python3 "$(dirname "$BASE_SH")/py/own_packs.py" newest "${VENDORED[@]}")" || die "py/own_packs.py could not choose among the vendored packs (above)"
  VENDORED=(); while IFS= read -r _d; do [ -n "$_d" ] && VENDORED+=("$_d"); done <<< "$_own"
fi

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

_venv_minor(){ "$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }   # empty: no venv, or one whose python cannot run

# 3.7.1: the newest Python torch actually ships for. uv can list a CPython minor (3.15 on 2026-10-15) weeks before
# torch publishes wheels for it, and the newest-listed pick then died in the torch install ("No solution found").
# Newest first, each minor asked whether the torch family resolves on it (a cached resolve, no install), the first
# yes wins: still "always the newest", only the newest that works. The pod does the same in lib/30-venv.sh
# (_base_python_pick, against the whole derived set, since 3.6.1). Stable minors only: the pattern skips 3.16.0a1 and +freethreaded.
_testbed_python_pick(){ # → x.y on stdout; why on stderr
  local cands mm tmp="${TMPDIR:-/tmp}/testbed-pick.$$.txt" have
  cands="$(uv python list 2>/dev/null | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV || true)"
  [ -n "$cands" ] || { warn "uv lists no CPython: cannot pick a Python" >&2; return 1; }
  for mm in $cands; do
    if printf 'torch\ntorchvision\ntorchaudio\n' | uv pip compile --quiet --python-version "$mm" - -o "$tmp" >/dev/null 2>&1; then
      rm -f "$tmp"; echo "$mm"; return 0
    fi
    note "Python $mm: torch has no build for it yet, trying the next" >&2
  done
  rm -f "$tmp"
  # nothing resolves (offline, or the index is down): keep a venv that runs rather than tear it down for a guess
  have="$(_venv_minor)"
  local keep="trying the newest"; [ -z "$have" ] || keep="keeping the venv's Python $have"
  warn "torch resolves on none of: $(echo $cands) (offline?): $keep" >&2
  if [ -n "$have" ]; then echo "$have"; else printf '%s\n' $cands | head -1; fi
}

# A venv is reused only when its python runs and is the picked minor. Anything else at $VENV is removed first: an
# older minor, and a half-made one (3.7.1) whose bin/python is gone or dangling (its interpreter was removed, or the
# first build died), which `-x` could not see and `uv venv` then refused with "A virtual environment already exists".
_testbed_venv(){ # <x.y>
  local mm="$1" have; have="$(_venv_minor)"
  if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    if [ -z "$have" ]; then note "the venv at $VENV has no working python (a half-made build): replacing it"; rm -rf "$VENV"
    elif [ "$have" != "$mm" ]; then note "the venv is Python $have, the newest torch supports is $mm: rebuilding it"; rm -rf "$VENV"; fi
  fi
  if [ ! -e "$VENV" ]; then
    note "creating a Python $mm venv …"
    uv venv --python "$mm" "$VENV" >/dev/null || { rm -rf "$VENV"; warn "uv could not create a Python $mm venv"; return 1; }
  fi
}

# 3.22.1: triton beside the CPU torch, on Linux. A pod's CUDA torch carries it (lib/30-venv.sh keeps it in the torch
# family) and the CPU build does not, while packs import it at module level: ComfyUI-RMBG's HEAD (229529e, 2026-10-17)
# bundles a sam3 whose edt.py does `import triton`, the pack's loader then raises a NameError in its own except branch
# and none of its nodes register, and ComfyUI-Easy-Sam3 stops on "No module named 'triton'". Importing it needs no GPU
# (on the soulcraft-hf runner triton 3.8.0 imported and RMBG registered its 45 nodes). The newest triton, resolved
# against a constraints file of the torch family as installed, so adding it never moves torch. A miss is a warning: the
# packs that need it then fail to register on their own, and every other check still runs. macOS has no triton wheels.
_testbed_triton(){
  local cons="$COMFY/.testbed-torch.txt" v="" tv   # inside the ignored testbed tree
  uv pip freeze --python "$VENV/bin/python" 2>/dev/null | grep -E '^(torch|torchvision|torchaudio)==' >"$cons" || true
  tv="$(sed -n 's/^torch==//p' "$cons")"
  if uv pip install -q --upgrade --python "$VENV/bin/python" -c "$cons" triton >/dev/null 2>&1 \
     && v="$("$VENV/bin/python" -c 'import triton; print(triton.__version__)' 2>/dev/null)" && [ -n "$v" ]; then
    ok "triton $v beside torch ${tv:-?} (packs that import it at module level register)"
  else
    warn "triton did not install or import beside the CPU torch: packs that import it at module level (ComfyUI-RMBG, ComfyUI-Easy-Sam3) will not register"
  fi
}

provision_server() {
  hdr "VENV + SERVER (CPU)"
  command -v uv >/dev/null || { warn "uv is not installed — needed to build the venv"; return 1; }
  local mm
  mm="$(_testbed_python_pick)" || return 1
  _testbed_venv "$mm" || return 1
  uv python upgrade "$mm" >/dev/null 2>&1 || true
  # 3.18.2: on Linux PyPI's torch IS the CUDA build (2.14.1+cu130 plus every nvidia-* wheel, a 6.7-7.2 GB venv on x64 and
  # arm64 alike), which every CI gate downloaded again on runners that keep no cache (error log row 108). The testbed
  # never sees a GPU, so on Linux every uv install below takes torch from the CPU index. macOS wheels are CPU already,
  # and the pod's install (lib/30-venv.sh) chooses its own backend: neither changes.
  if [ "$(uname -s)" = Linux ]; then export UV_TORCH_BACKEND=cpu; fi
  note "installing torch (CPU${UV_TORCH_BACKEND:+, uv --torch-backend $UV_TORCH_BACKEND}) + every requirement at its newest: this is the slow part …"
  uv pip install -q --upgrade --python "$VENV/bin/python" torch torchvision torchaudio >/dev/null
  if [ "$(uname -s)" = Linux ]; then _testbed_triton; fi
  # one pin-free set: ComfyUI's own == pins (its frontend packages) and every pack's are lifted by the base's reqlift
  local row dir files=("ComfyUI=$COMFY/requirements.txt") derived="$COMFY/.testbed-derived.txt"   # inside the ignored testbed tree
  for row in ${PACKS[@]+"${PACKS[@]}"}; do          # PACKS may be empty (bash 3.2 + set -u)
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
  start_server
}

# The server alone, from the venv as it is: --server's last step, and --start (3.16.0), which verify.sh runs with a
# TESTBED_STATE and a free TESTBED_PORT of its own. Exit 3 when there is no venv to start from (verify.sh: no server).
start_server() {
  [ -x "$VENV/bin/python" ] || { warn "no venv at $VENV: bash \"$HERE/testbed.sh\" --server builds it"; return 3; }
  stop_server
  refuse_foreign_port
  # a server with its own state folder gets its own temp folder too: ComfyUI empties its temp directory when it starts,
  # and two servers on one tree would empty each other's
  local tmpd=(); [ "$STATE" = "$HERE" ] || tmpd=(--temp-directory "$STATE")
  # 3.17.1: the settings a customer machine has (the App Mode tour marked seen), so the stand-in serves the same form
  local seed; seed="$(dirname "$BASE_SH")/py/comfy_settings.py"
  if [ -f "$seed" ]; then note "$(python3 "$seed" "$COMFY/user/default/comfy.settings.json" 2>&1 || true) (comfy.settings.json)"; fi
  note "starting ComfyUI --cpu on port $PORT …"
  # nohup alone in the background, so $! is the pid python runs as (nohup execs it): the pid file names the server
  ( cd "$COMFY" || exit 1; nohup "$VENV/bin/python" main.py --cpu --port "$PORT" --disable-auto-launch ${tmpd[@]+"${tmpd[@]}"} \
      >"$LOG" 2>&1 & echo $! >"$PIDF" )
  echo "$PORT" >"$PORTF"
  local i
  for i in $(seq 1 120); do
    # up only when OUR pid answers: a server that takes the port between the check above and python's bind is not ours
    if owned_server >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then
      ok "server up at http://127.0.0.1:$PORT (pid $(cat "$PIDF"), port recorded in $PORTF)"; return 0
    fi
    [ -n "$(own_pids)" ] || break               # it exited (a port taken under it, an import error): no point waiting
    sleep 2
  done
  rm -f "$PORTF"
  warn "this testbed's server did not come up — see $LOG"; tail -30 "$LOG" || true; return 1
}

stop_server() {
  local pids p i alive
  pids="$(own_pids)"
  for p in $pids; do kill "$p" 2>/dev/null && ok "stopped pid $p" || true; done
  if [ -n "$pids" ]; then
    for i in 1 2 3 4 5 6 7 8 9 10; do          # up to 10 s for a clean exit, then the hard way
      alive=0; for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
      [ "$alive" = 0 ] && break; sleep 1
    done
    for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
  fi
  rm -f "$PIDF" "$PORTF"
  local held; held="$(port_holders | tr '\n' ' ')"
  [ -n "${held// /}" ] && note "port $PORT is held by pid ${held% } (not this testbed's server): left running"
  return 0
}

# Before --server starts: a port someone else holds is a stop, never a kill. Name the holder and the way out.
refuse_foreign_port() {
  local held p cmd=""
  held="$(port_holders | tr '\n' ' ')"; held="${held% }"
  [ -n "$held" ] || return 0
  p="${held%% *}"; [ "$p" = "?" ] || cmd="$(ps -o command= -p "$p" 2>/dev/null | cut -c1-120 || true)"
  warn "port $PORT is already in use by pid $held${cmd:+ ($cmd)}, which this testbed did not start; it is left running"
  local free; free="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])' 2>/dev/null || echo 8237)"
  note "another checkout's server, most likely. Pick a free port for this one: TESTBED_PORT=$free bash \"$HERE/testbed.sh\" --server"
  exit 1
}

status() {
  hdr "STATUS"
  [ -d "$COMFY/.git" ] && ok "ComfyUI $(git -C "$COMFY" describe --tags 2>/dev/null || echo '?') · $(ls -1 "$CN" 2>/dev/null | wc -l | tr -d ' ') packs" || note "no source tree yet"
  [ -x "$VENV/bin/python" ] && ok "venv $($VENV/bin/python -V 2>&1)" || note "no venv"
  if [ -x "$VENV/bin/python" ] && [ "$(uname -s)" = Linux ]; then   # 3.22.1: _testbed_triton; macOS has none to show
    local tri; tri="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("triton"))' 2>/dev/null || true)"
    if [ -n "$tri" ]; then ok "triton $tri"; else warn "no triton in the venv: packs that import it at module level do not register (--server installs it)"; fi
  fi
  local mine
  if mine="$(owned_server 2>/dev/null)"; then ok "server answering at $mine (this testbed's, pid $(own_pids | head -1))"
  elif curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then note "a server answers on $PORT, but not this testbed's (TESTBED_PORT picks another)"
  else note "server not running"; fi
  [ -f "$PORTF" ] && note "port file: $PORTF ($(tr -d '[:space:]' < "$PORTF"))"
  # what a provision WOULD install, and where the packages were found. Silent derivation is how 2.6.0 shipped
  # a testbed that quietly provisioned the base's packs and none of a consumer repository's.
  note "packs: ${#PACKS[@]} derived${REPO_ROOT:+ · packages under $REPO_ROOT (${#PKGS[@]})}${REPO_ROOT:+ · ${#VENDORED[@]} vendored}"
  local v
  for v in ${VENDORED[@]+"${VENDORED[@]}"}; do      # which copy of each vendored pack gets linked, and from where
    note "  vendored $(basename "$v") $(python3 "$(dirname "$BASE_SH")/py/own_packs.py" version "$v" 2>/dev/null || echo '?') from $(basename "$(dirname "$v")")"
  done
  echo; note "BASE_NODE_SRC=\"$COMFY\"   BASE_SERVER=127.0.0.1:$PORT"
}

# sourced (the suite does, to test the picks above): define, never run
if [ "${BASH_SOURCE[0]}" != "$0" ]; then return 0; fi

case "${1:-}" in
  "")        provision_source; status;;
  --latest)  LATEST=1; provision_source; status;;
  --server)  provision_source; provision_server; status;;
  --packs)   provision_packs; status;;   # 3.6.1: the node packs alone (each to its tip), ComfyUI left as it is
  --start)   start_server; status;;   # 3.16.0: the server alone (exit 3: no venv to start it from)
  --stop)    stop_server;;
  --status)  status;;
  *) echo "usage: bash testbed.sh [--server | --start | --latest | --packs | --stop | --status | --owned]"; exit 2;;
esac
