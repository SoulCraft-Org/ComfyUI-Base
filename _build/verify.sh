#!/bin/bash
# Everything that can be checked WITHOUT a pod, in one command — because the failures of 2026-09-08 were
# not missing tests, they were checks that existed and were skipped while moving fast.
#
#   bash base/comfyui-base/_build/verify.sh                                        # the base and every package of the consumer root
#   bash base/comfyui-base/_build/verify.sh <brand>/packages/<name>                  # just these (paths relative to that root)
#   BASE_WORKSPACE=<ws> bash "$COMFY_BASE/_build/verify.sh"                         # the base and every <name>/ of a workspace
#   bash _build/verify.sh                                                          # a standalone base checkout: the base alone
#
# The consumer root (3.6.0, py/pkgdirs.py): $BASE_WORKSPACE, else <root> when this base is
# <root>/base/comfyui-base. Its packages are <brand>/packages/<name>/ and <name>/, found by the same rule
# every other walker uses.
#
# Three things per item, in the order that fails cheapest first:
#   1. the built zip matches the working tree            (package.py --check)
#   2. the suite, from the repo                          (<name>-script.sh test / base.sh test)
#   3. the suite, from the EXTRACTED ZIP                 (what the pod runs, and what podctl's gate runs)
#
# Step 3 is the one that keeps being learned: a test that reads _build/, an installed pack, or a path that
# only exists beside the repo passes at step 2 and fails on the pod ten minutes and one upload later. It
# also caught a test that CRASHED from the zip and took the gate down with it.
#
# A package whose script declares PKG_NO_SUITE=1 and ships no suite.py skips steps 2 and 3 as "declared":
# the zip check still runs over it.
#
# The server the suites talk to (3.16.0) is one this run started, or none:
#   VERIFY_SERVER=own    (the default) a ComfyUI of this run's own, from the testbed's venv: a free port, its pid, port and
#                        log files in a folder of the run's own, stopped when the run ends, however it ends
#   VERIFY_SERVER=reuse  this checkout's testbed server (testbed.sh --server), only when `testbed.sh --owned` proves that
#                        its pid file names the process on that port; anything else stops the gate with the reason
#   VERIFY_SERVER=none   no server: the live checks skip
#   BASE_SERVER=<h:p>    set by the caller: the caller's own choice, used as given
set -uo pipefail
BASE="$(cd "$(dirname "$0")/.." && pwd)"; UP="$(cd "$BASE/../.." && pwd)"
# Fail closed: an empty answer is "no consumer root" (documented, printed below); a helper that is missing, crashes, or
# names a root that does not exist stops the gate, because a green gate over zero packages is the 2.6.0 fault again.
CROOT="$(python3 "$BASE/py/pkgdirs.py" --root)" || { echo "── py/pkgdirs.py could not resolve the consumer root (above): NOT green"; exit 1; }
ROOT="${CROOT:-$UP}"
# The testbed sits either IN the base (since 2.6.0 testbed.sh ships here) or at base/testbed of a brand tree.
# One root is chosen and used whole, so the port file always comes from the same place as the tree.
if [ -f "$BASE/testbed.sh" ]; then TB_ROOT="$BASE"; else TB_ROOT="$UP/base"; fi
export BASE_NODE_SRC="${BASE_NODE_SRC:-$TB_ROOT/testbed}"
items=(); rc=0
if [ "$#" -gt 0 ]; then for a in "$@"; do items+=("$ROOT/$a"); done
else
  items+=("$BASE")
  if [ -n "$CROOT" ]; then
    pkgs="$(python3 "$BASE/py/pkgdirs.py" "$CROOT")" || { echo "── py/pkgdirs.py could not list $CROOT (above): NOT green"; exit 1; }
    while IFS= read -r d; do [ -n "$d" ] && items+=("$d"); done <<< "$pkgs"
  else
    echo "── no consumer root (no \$BASE_WORKSPACE, not at <root>/base/comfyui-base): verifying the base alone"
  fi
fi
# 3.16.0: which server (error log row 84). This used to be whatever answered on the port in .testbed-server.port, else
# 8199. On a Mac where several sessions each run a testbed, that was another checkout's server: the snapshot tier measured
# ITS frontend (other LoadImage sizes), a server test lost it mid-run when its session stopped it, and a clean PR went red.
# Now a run starts its own (two runs at once never share one) or proves the one it reuses. 127.0.0.1:0 is "no server"
# said out loud: an empty BASE_SERVER lets a suite fall back to its own default (8199, 8188), where a stranger's ComfyUI
# may well be listening.
NO_SERVER="127.0.0.1:0"; RUN_TB=""
_verify_stop_server(){ # the run's own server, stopped and its folder gone, on every way out
  [ -n "$RUN_TB" ] || return 0
  TESTBED_STATE="$RUN_TB" bash "$TB_ROOT/testbed.sh" --stop >/dev/null 2>&1 || true
  rm -rf "$RUN_TB"; RUN_TB=""
}
trap _verify_stop_server EXIT; trap 'exit 130' INT; trap 'exit 143' TERM
if [ -n "${BASE_SERVER:-}" ]; then
  echo "── server: $BASE_SERVER (BASE_SERVER, set by the caller)"
else
  case "${VERIFY_SERVER:-own}" in
    none) BASE_SERVER="$NO_SERVER"; echo "── server: none (VERIFY_SERVER=none): the live checks skip";;
    reuse)
      [ -f "$TB_ROOT/testbed.sh" ] || { echo "── VERIFY_SERVER=reuse, but there is no testbed.sh at $TB_ROOT: NOT green"; exit 1; }
      if BASE_SERVER="$(bash "$TB_ROOT/testbed.sh" --owned 2>/tmp/verifys-$$.log)"; then
        rm -f /tmp/verifys-$$.log; echo "── server: $BASE_SERVER (this checkout's testbed server; its pid file names the process on that port)"
      else
        echo "── VERIFY_SERVER=reuse refused: $(head -1 /tmp/verifys-$$.log)"; rm -f /tmp/verifys-$$.log
        echo "   start this checkout's own (bash \"$TB_ROOT/testbed.sh\" --server), or leave VERIFY_SERVER unset and the run starts one: NOT green"
        exit 1
      fi;;
    own)
      if [ -f "$TB_ROOT/testbed.sh" ]; then
        RUN_TB="$(mktemp -d "${TMPDIR:-/tmp}/verify-server.XXXXXX")"
        port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
        TESTBED_STATE="$RUN_TB" TESTBED_PORT="$port" bash "$TB_ROOT/testbed.sh" --start > "$RUN_TB/start.out" 2>&1; s=$?
        if [ $s -eq 0 ] && BASE_SERVER="$(TESTBED_STATE="$RUN_TB" bash "$TB_ROOT/testbed.sh" --owned 2>>"$RUN_TB/start.out")"; then
          echo "── server: $BASE_SERVER (this run's own, stopped when it ends)"
        elif [ $s -eq 3 ]; then
          BASE_SERVER="$NO_SERVER"; _verify_stop_server; echo "── server: none (the testbed has no venv: bash testbed.sh --server builds one): the live checks skip"
        else
          echo "── this run's own testbed server did not start:"; tail -25 "$RUN_TB/start.out" | sed 's/^/    /'
          echo "── NOT green: the testbed is broken (VERIFY_SERVER=none runs the gate without a server)"; exit 1
        fi
      else
        BASE_SERVER="$NO_SERVER"; echo "── server: none (no testbed.sh at $TB_ROOT): the live checks skip"
      fi;;
    *) echo "── VERIFY_SERVER=${VERIFY_SERVER}: it is own, reuse or none. NOT green"; exit 1;;
  esac
fi
export BASE_SERVER
# 3.15.3: a failing suite's report, whole enough to diagnose from CI: pytest's ERRORS and FAILURES sections (each test's
# header, its > lines and its whole E block) capped at 200 lines, then the short summary naming every failing test.
# It used to keep 12-24 lines, and a long assertion lost its cause (2026-10-16, a Video Replacer failure).
_verify_failures(){ # <log>
  awk '/= (ERRORS|FAILURES) =/{p=1} /= short test summary info =/{p=0} p' "$1" | head -200 | sed 's/^/    /'
  awk '/= short test summary info =/{p=1} p' "$1" | head -40 | sed 's/^/    /'
}
for d in "${items[@]}"; do
  name="$(basename "$d")"; echo "══ $name"
  if [ "$d" = "$BASE" ]; then zp="$BASE/comfyui-base.zip"
  else host="$(python3 "$BASE/_build/package.py" --host "$d")" || { echo "  package.py --host failed for $d"; rc=1; continue; }; zp="$d/$name-$host.zip"; fi
  # a package with no suite, by declaration: steps 2 and 3 are not a run, they are a stated fact
  nosuite=0
  if [ "$d" != "$BASE" ] && [ -f "$d/$name-script.sh" ] && grep -qE '^PKG_NO_SUITE=1' "$d/$name-script.sh" && [ ! -f "$d/suite.py" ]; then nosuite=1; fi
  # 1. the zip matches the tree
  if [ "$d" = "$BASE" ]; then
    python3 "$BASE/_build/package.py" --base --check 2>&1 | grep -E "stale|current|no zip" | sed 's/^/  zip   /'
    python3 "$BASE/_build/package.py" --base --check >/dev/null 2>&1 || { echo "  ZIP STALE: rebuild with package.py --base"; rc=1; continue; }
  else
    python3 "$BASE/_build/package.py" "$d" --check 2>&1 | grep -E "stale|current|no zip" | sed 's/^/  zip   /'
    python3 "$BASE/_build/package.py" "$d" --check >/dev/null 2>&1 || { echo "  ZIP STALE: rebuild with package.py ${d#"$ROOT/"}"; rc=1; continue; }
  fi
  # 2. from the repo
  if [ "$nosuite" = 1 ]; then
    echo "  suite no suite, declared (PKG_NO_SUITE=1)"
  else
    if [ "$d" = "$BASE" ]; then ( cd "$BASE" && bash base.sh test ) > /tmp/verify-$$.log 2>&1
    else ( cd "$d" && COMFY_BASE="$BASE" bash "$name-script.sh" test ) > /tmp/verify-$$.log 2>&1; fi
    s=$?; echo "  suite $(grep -E 'passed|failed' /tmp/verify-$$.log | tail -1 | sed 's/^ *//')"
    [ $s -eq 0 ] || { echo "  FROM THE REPO: FAILED, the failing test(s):"; _verify_failures /tmp/verify-$$.log; rc=1; }   # every failing or errored test, whole
  fi
  # 3. from the zip — exactly what the pod and podctl's gate run
  if [ "$nosuite" = 1 ]; then
    echo "  zip   no suite, declared (PKG_NO_SUITE=1)"
  else
    t="$(mktemp -d)"
    if [ "$d" = "$BASE" ]; then
      unzip -q "$zp" -d "$t" && ( cd "$t/comfyui-base" && bash base.sh test ) > /tmp/verifyz-$$.log 2>&1
    else
      mkdir -p "$t/$name" && unzip -q "$zp" -d "$t/$name" \
        && ( cd "$t/$name" && COMFY_BASE="$BASE" bash "$name-script.sh" test ) > /tmp/verifyz-$$.log 2>&1
    fi
    z=$?; echo "  zip   $(grep -E 'passed|failed' /tmp/verifyz-$$.log | tail -1 | sed 's/^ *//')"
    [ $z -eq 0 ] || { echo "  FROM THE ZIP: FAILED, this is what the pod would have said"; _verify_failures /tmp/verifyz-$$.log; rc=1; }   # every failing or errored test, whole
    rm -rf "$t"
  fi
done
rm -f /tmp/verify-$$.log /tmp/verifyz-$$.log
[ $rc -eq 0 ] && echo "── everything that can be checked without a pod is green" || echo "── NOT green: fix here, not on the pod"
exit $rc
