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
# BASE_SERVER only when nothing set it: the runner detects the testbed itself (lib/95-summary.sh), and an
# unconditional export here overrode that detection. The testbed's port file wins over the 8199 default.
if [ -z "${BASE_SERVER:-}" ]; then
  if [ -f "$TB_ROOT/.testbed-server.port" ]; then BASE_SERVER="127.0.0.1:$(tr -d '[:space:]' < "$TB_ROOT/.testbed-server.port")"
  else BASE_SERVER="127.0.0.1:8199"; fi
  export BASE_SERVER
fi
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
    [ $s -eq 0 ] || { echo "  FROM THE REPO: FAILED, the failing test(s):"; grep -E -A2 "_{5,} (ERROR at [a-z]+ of )?test_|^E " /tmp/verify-$$.log | grep -v '^--$' | head -16 | sed 's/^/    /'; rc=1; }   # an ERROR (setup/teardown) is named too
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
    [ $z -eq 0 ] || { echo "  FROM THE ZIP: FAILED, this is what the pod would have said"; awk '/= (ERRORS|FAILURES) =/{p=1} p' /tmp/verifyz-$$.log | head -24; rc=1; }   # errors (setup/teardown) too
    rm -rf "$t"
  fi
done
rm -f /tmp/verify-$$.log /tmp/verifyz-$$.log
[ $rc -eq 0 ] && echo "── everything that can be checked without a pod is green" || echo "── NOT green: fix here, not on the pod"
exit $rc
