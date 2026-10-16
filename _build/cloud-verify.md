# verify.sh in a Claude Code cloud session (Tier 1, by hand, at no cost)

A brief a cloud session follows to run one package's `_build/verify.sh` and report its totals. The VM costs nothing beyond
the plan's usage (4 vCPU, 16 GB, 30 GB disk, Ubuntu 24.04). Use it once per release, or whenever the free self-hosted
runner a consumer's CI uses is down. Tier 0 (`_build/quick.sh`, seconds) runs wherever the change was made; Tier 2 is the
pod (`podctl runner`, README.md beside podctl).

## What the session can and cannot do (measured 2026-10-16)

| | |
|---|---|
| Clone the package, this base and the consumer's other repositories | yes, through the session's GitHub proxy (the Claude GitHub App must be installed on the owner, for those repositories) |
| `testbed.sh --server` (ComfyUI, the packs, a CPU torch venv) | only when the cloud environment's network access is **Custom** and allows `download.pytorch.org`, `huggingface.co` with its LFS/Xet hosts, and the Playwright CDN; the default "Trusted" list blocks all three |
| A check run, a commit status or a deployment on the commit | **no**: the proxy answers 403 "Write access to this GitHub API path is not permitted through this proxy" for all three; the app installs with `statuses: read` too |
| A pull request comment | try it once (step 5); if it is refused, the final message is the report |

So the session reports; a maintainer's own `gh` turns the report into a commit status where a status is wanted (step 6).

## The brief (paste it, with the four values filled in)

```
Run Tier 1 for one package and report the totals. Do nothing else: no edits, no commits, no pushes.
  OWNER=<github owner>  REPO=<package repository>  REF=<branch, tag or full sha>  PR=<number, or none>
  WORKSPACE=<the owner's workspace repository holding brand.toml, or none>   BASE_REF=<a ComfyUI-Base tag or branch, or newest>

1. The workspace shape (ComfyUI Base py/pkgdirs.py): the package inside the workspace, the base beside it.
     mkdir -p /tmp/t1 && cd /tmp/t1
     if [ "$WORKSPACE" != none ]; then git clone -q https://github.com/$OWNER/$WORKSPACE ws; else mkdir ws; fi
     git clone -q https://github.com/$OWNER/$REPO ws/$REPO && git -C ws/$REPO -c advice.detachedHead=false checkout -q $REF
     ref=$BASE_REF; [ "$ref" != newest ] || ref=$(git ls-remote --tags --refs https://github.com/$OWNER/ComfyUI-Base 'base-v*' | sed 's|.*refs/tags/||' | sort -V | tail -1)
     git clone -q --depth 1 --branch "$ref" https://github.com/$OWNER/ComfyUI-Base
     export COMFY_BASE=/tmp/t1/ComfyUI-Base BASE_WORKSPACE=/tmp/t1/ws && cd /tmp/t1/ws
2. Tier 0 first, it takes seconds: bash $COMFY_BASE/_build/quick.sh $REPO
3. The testbed, in the background (commands stop after 10 minutes in the foreground):
     (bash $COMFY_BASE/testbed.sh --server > /tmp/t1/testbed.log 2>&1; echo "exit $?" >> /tmp/t1/testbed.log) &
   and wait for its "exit" line. A download that fails on download.pytorch.org, huggingface.co or a Playwright host is
   the environment's network setting: report it as BLOCKED with the host, do not work around it.
   Each playwright version the package's _build pins needs its browser: 
     for v in $(grep -rhoE 'playwright==[0-9.]+' $REPO/_build | sort -u | cut -d= -f3); do uvx --from playwright==$v playwright install --with-deps chromium; done
4. While the server is up, check the prompts against it (the testbed has no weights, so model lists are "unjudged"):
     python3 $COMFY_BASE/_build/objinfo.py save $REPO --server 127.0.0.1:$(cat $COMFY_BASE/.testbed-server.port)
     python3 $COMFY_BASE/_build/objinfo.py check $REPO
   (save writes $REPO/_build/object_info.json in this clone only; it is never committed from here.)
   Then verify.sh, in the background the same way: bash $COMFY_BASE/_build/verify.sh $REPO > /tmp/t1/verify.log 2>&1
   then bash $COMFY_BASE/testbed.sh --stop
5. If PR is a number, try once: gh pr comment $PR -R $OWNER/$REPO --body-file <the report below>. A 403 is expected
   on some setups; say which answer you got.
6. End with this report and nothing after it:
     TIER1 <OWNER>/<REPO> @ <the full sha checked out>  base <ref>
     tier0: <quick.sh's last line>
     prompts: <objinfo.py check's last line, and each problem it named>
     verify: <PASS|FAIL|BLOCKED>  repo: <its "suite" line>  zip: <its "zip" line>
     failing tests: <each failing test id, or none>
     pr comment: <posted | refused: the HTTP answer | not asked>
```

## Step 6, on the maintainer's machine (only where a commit status is wanted)

The report names the sha. From a checkout whose `gh` may write statuses on the repository:

    gh api -X POST repos/<owner>/<repo>/statuses/<sha> -f state=success -f context=verify/cloud \
      -f description="verify.sh in a Claude cloud session: <the suite line>"

(`state=failure` for FAIL; nothing for BLOCKED). A ruleset that requires `verify/cloud` is then satisfied only by a
maintainer's credential, which is why a consumer with a free runner keeps requiring its runner's check instead.
