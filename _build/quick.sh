#!/bin/bash
# Tier 0: what takes seconds, for any session on any CPU (this Mac, a cloud session, a runner), before every commit.
#
#   bash "$COMFY_BASE/_build/quick.sh" <pkg dir>...      # these packages (a path, or one relative to the consumer root)
#   bash "$COMFY_BASE/_build/quick.sh"                   # the base and every package of the consumer root
#
# Four steps per item, cheapest first, and every step runs, so one run hands back the whole list (root CLAUDE.md §2):
#   1. the zip matches the tree                          package.py --check
#   2. the suite's pure tiers                            <name>-script.sh test quick: every tier the suite declares but
#                                                        those that need a live machine (BASE_HEAVY_TIERS, lib/95-summary.sh)
#   3. the API prompts against the cached /object_info   objinfo.py check (classes, required inputs, links, ranges, combos)
#   4. the brand's zip guard, when its brand.toml names one: zip_guard = "<command>", run in the brand's folder with the
#      zip as its last argument (SoulCraft: the private cast guard)
#
# Tier 1 is _build/verify.sh (the suite from the repo and from the extracted zip, with the testbed), once per release on
# a free runner. Tier 2 is the pod: a real render, and `podctl runner` makes it a self-hosted runner while it is up.
set -uo pipefail
BASE="$(cd "$(dirname "$0")/.." && pwd)"
CROOT="$(python3 "$BASE/py/pkgdirs.py" --root)" || { echo "── py/pkgdirs.py could not resolve the consumer root (above): NOT green"; exit 1; }
items=(); rc=0; t0=$(date +%s)
if [ "$#" -gt 0 ]; then
  for a in "$@"; do
    if [ -d "$a" ]; then items+=("$(cd "$a" && pwd)"); elif [ -n "$CROOT" ] && [ -d "$CROOT/$a" ]; then items+=("$CROOT/$a")
    else echo "── no such package: $a"; rc=1; fi
  done
else
  items+=("$BASE")
  if [ -n "$CROOT" ]; then
    pkgs="$(python3 "$BASE/py/pkgdirs.py" "$CROOT")" || { echo "── py/pkgdirs.py could not list $CROOT (above): NOT green"; exit 1; }
    while IFS= read -r d; do [ -n "$d" ] && items+=("$d"); done <<< "$pkgs"
  fi
fi
log="$(mktemp)"; trap 'rm -f "$log"' EXIT
step(){ # <label> <cmd...>: run it, print one line with its seconds, and the tail of its output when it fails
  local label="$1" s e; shift; s=$(date +%s)
  "$@" > "$log" 2>&1; e=$?
  printf '  %-7s %3ss  %s\n' "$label" "$(( $(date +%s) - s ))" "$(grep -vE '^\s*$' "$log" | tail -1 | sed 's/^ *//' | cut -c1-150)"
  if [ $e -ne 0 ]; then grep -E 'FAILED|ERROR|Error|error:|stale|problem|not one of|missing|STALE|failed' "$log" | head -40 | sed 's/^/          /'; rc=1; fi
}
for d in ${items[@]+"${items[@]}"}; do
  name="$(basename "$d")"; echo "══ $name (tier 0)"
  if [ "$d" = "$BASE" ]; then
    step zip python3 "$BASE/_build/package.py" --base --check
    step suite bash -c 'cd "$1" && bash base.sh test quick' _ "$BASE"
    continue
  fi
  host="$(python3 "$BASE/_build/package.py" --host "$d")" || { echo "  package.py --host failed for $d"; rc=1; continue; }
  zp="$d/$name-$host.zip"
  step zip python3 "$BASE/_build/package.py" "$d" --check
  if [ -f "$d/$name-script.sh" ] && grep -qE '^PKG_NO_SUITE=1' "$d/$name-script.sh" && [ ! -f "$d/suite.py" ]; then
    echo "  suite     -   no suite, declared (PKG_NO_SUITE=1)"
  else
    step suite bash -c 'cd "$1" && COMFY_BASE="$2" bash "$3-script.sh" test quick' _ "$d" "$BASE" "$name"
  fi
  step prompts python3 "$BASE/_build/objinfo.py" check "$d"
  bdir="$(BASE_PY="$BASE/py" python3 -c 'import os, sys; sys.path.insert(0, os.environ["BASE_PY"]); import brand; d = brand.brand_dir_of(sys.argv[1]); print(d or "")' "$d")"
  guard=""; [ -n "$bdir" ] && guard="$(BASE_PY="$BASE/py" python3 -c 'import os, sys; sys.path.insert(0, os.environ["BASE_PY"]); import brand; print(brand.parse_brand_toml(open(sys.argv[1], encoding="utf-8").read()).get("zip_guard", ""))' "$bdir/brand.toml")"
  if [ -n "$guard" ]; then step guard bash -c 'cd "$1" && eval "$2 \"\$3\""' _ "$bdir" "$guard" "$zp"
  else echo "  guard     -   none declared (brand.toml zip_guard)"; fi
done
echo "── tier 0 $( [ $rc -eq 0 ] && echo green || echo 'NOT green' ) in $(( $(date +%s) - t0 ))s (Tier 1, verify.sh, runs once per release on a free runner)"
exit $rc
