# ComfyUI Base: how work is done in this repository

The shared toolchain for ComfyUI workflow packages on a rented or owned NVIDIA GPU. MIT, maintained by SoulCraft. The
current version is whatever `VERSION` holds; never restate it here, it goes stale. `README.md` says what this is;
`comfyui-base-handbook.md` is the design reference (§0 design rules, §1 hosts, §4 the package contract, §8 tests). This
file is only the working agreement.

## Who consumes it

A consumer keeps this repository either as a git submodule at `base/comfyui-base`, pinned to its own commit, or checked
out at a tag (SoulCraft's packages repository pins a `base-v<VERSION>` tag in its `repos.txt` and `bootstrap.sh` checks
it out at `$COMFY_BASE`). Several consumers can sit on different versions at the same time. Nothing here knows a
brand's name: what is brand-specific lives in the brand repository and plugs in through `hosts/`, `ext/*.sh`,
`brand.toml`, `LOADER_CATS`. Keep it that way.

SoulCraft's house rules (the session cycle, the list rule, staging by path, defects are issues) live in the private
repository `SoulCraft-Org/workspace`, in `AGENTS.md`; `gh repo clone SoulCraft-Org/workspace` fetches it. A session that
also needs a package beside the base runs `bash bootstrap.sh --only <package>` there, which puts the base in place too.

## Layout

    base.sh                     entry point, sourced by package scripts or run directly (version | install-self | status | test [tier] | rescue)
    lib/00-env.sh … 95-summary.sh, boot.sh   the install and boot stages, in order
    py/                         the Python side (brand.py, canvas.py, ledger.py, torch_pick.py, …)
    suite/                      the base's own pytest suite (test_unit … test_verda)
    hosts/{runpod,verda,crusoe,local}/  per-host providers and recipes; hosts/README.md is the matrix
    _build/package.py           the one packager: package zips and the base zip (writes MANIFEST.sha256 first)
    _build/verify.sh            the gate: zip freshness, suite from the repo, suite from the extracted zip
    _build/pod/podctl.py        the Mac-side driver (`podctl --provider <host> …`)
    comfyui-base.zip, MANIFEST.sha256   built artefacts, committed; an installed copy must match the manifest

## The gate

`bash _build/verify.sh` is the check before any commit. It runs `package.py --check`, then the suite from the
repository, then the suite from the extracted zip, because a test that passes at step 2 and fails at step 3 is what
breaks a pod ten minutes and one upload later. `bash base.sh test [tier]` runs the suite alone; tiers are `unit`,
`install`, `comfyui` (needs the shared testbed beside the base), `runpod`, `gpu`, `bare`. On the SoulCraft owner's Mac
no suite runs (their rule): push the change once, when it is whole, and read the GitHub CI (`verify.yml`); everywhere
else run the gate before the commit.

`pytest.ini` sets `filterwarnings = error`. Warnings are fixed, never filtered, and that line is not negotiable.

## Releasing a base version

1. Change the code, keep `verify.sh` green.
2. Bump `VERSION`, then `python3 _build/package.py --base` (rewrites `MANIFEST.sha256`, then `comfyui-base.zip`).
3. Run `bash _build/verify.sh` again; it fails if the zip is stale.
4. Commit as `ComfyUI Base X.Y.Z: <what changed>` (the log's existing convention) and tag `base-vX.Y.Z`.
5. In each consumer repository, move its pin in a base-only commit (`git add base/comfyui-base` for a submodule, the
   `base` line of `repos.txt` for SoulCraft's packages repository).

Never edit `MANIFEST.sha256` or the zip by hand, and never edit this code from inside a consumer's checkout of it: fix
it here, release, then move the pin there.

## Hosts

`hosts/README.md` is the matrix of what each host provider has been proven on. Say which host a change was actually
exercised on when you describe it.

## Defects

Every defect found is a GitHub issue in the repository whose code is at fault, the moment it is found; the fixing pull
request says `Fixes #N`.
