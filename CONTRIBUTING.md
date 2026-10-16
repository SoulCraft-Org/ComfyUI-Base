# Contributing to ComfyUI Base

Issues and pull requests are welcome. The open items are in the README's status table: a Verda run on a live
account, the Crusoe provider, and a report from an owned NVIDIA box.

## The gate

One command has to be green before any commit:

```bash
bash _build/verify.sh
```

It does three things per item, cheapest failure first:

1. `comfyui-base.zip` matches the working tree (`package.py --check`)
2. the suite, from the repository
3. the suite, from the **extracted zip**

Step 3 is the one that keeps being learned. A test that reads `_build/`, an installed pack, or a path that only
exists beside the repository passes at step 2 and fails on the pod ten minutes and one upload later. CI runs the
same command on every push and pull request.

You need `uv` on PATH. A fresh clone has no venv, so the suite reaches pytest through
`uv run --no-project --python <the newest uv has> --with pytest`; without `uv` it reports "no pytest and no uv" and exits 1.

## Tests

```bash
bash base.sh test          # every tier
bash base.sh test unit     # one tier
```

Tiers are `unit`, `install`, `comfyui`, `runpod`, `gpu`, `bare`. Anything needing hardware or a provisioned
testbed skips itself by name, so `unit` and `install` run anywhere. `comfyui` needs the testbed:

```bash
bash testbed.sh --server   # provisions a CPU ComfyUI beside the base and starts it
```

`bash _build/verify.sh` starts a server of its own run beside that tree (and stops it at the end), so it never talks
to another checkout's; `VERIFY_SERVER=reuse` uses the one `--server` started instead, once `testbed.sh --owned` proves it is this
checkout's.

`pytest.ini` sets `filterwarnings = error`. Warnings get fixed, never filtered. That line is not negotiable.

## What goes where

    base.sh                     the entry point: version | install-self | status | test [tier] | rescue
    lib/00-env.sh … 95-summary.sh, boot.sh   the install and boot stages, in order
    py/                         the Python side (brand.py, canvas.py, ledger.py, torch_pick.py, …)
    suite/                      the base's own pytest suite
    hosts/{runpod,verda,crusoe,local}/       per-host providers and recipes; hosts/README.md is the matrix
    _build/package.py           the packager: package zips and the base zip (writes MANIFEST.sha256 first)
    _build/verify.sh            the gate
    _build/pod/podctl.py        the Mac-side driver
    testbed.sh                  provisions a local CPU ComfyUI for the comfyui tier
    comfyui-base.zip, MANIFEST.sha256        built artefacts, committed

Never edit `MANIFEST.sha256` or the zip by hand — `python3 _build/package.py --base` writes both.

## House rules the suite enforces

These are unit tests, so you will find out immediately, but knowing them first saves a round trip:

- **Outbound hosts are an allowlist.** `huggingface.co`, `github.com`, `pypi.org`, `pypi.nvidia.com`,
  `download.pytorch.org`, and the loopback names. No URL outside it may appear in `lib/` or `py/`. Nothing is
  uploaded — no `requests.post`, no `curl -X POST`.
- **`pip` is never bare.** Only `uv pip install --python "$PY"` and `"$PY" -m pip install` name their
  interpreter; a bare `pip` belongs to whichever interpreter the run did not use.
- **Fallbacks are loud.** Anything returning a `*_FALLBACK` value prints a `!!` line to stderr first.
- **Nothing here knows a brand's name.** What is specific to a project lives in that project's repository and
  plugs in through the seams the base leaves for it: `hosts/`, `ext/*.sh`, `brand.toml`, `LOADER_CATS`.

## Pull requests

Say which host the change was actually exercised on. RunPod is proven on live pods; Verda is experimental;
Crusoe is an interface stub; `hosts/local` is a recipe. "Not run on hardware" is a fine answer — an untested
claim that reads as tested is not.

## Releasing (maintainers)

1. Change the code, keep `verify.sh` green.
2. Bump `VERSION`, then `python3 _build/package.py --base` (rewrites `MANIFEST.sha256`, then the zip).
3. Run `bash _build/verify.sh` again — it fails if the zip is stale.
4. Commit as `ComfyUI Base X.Y.Z: <what changed>` and tag `base-vX.Y.Z`.
5. In each consumer repository, bump the submodule pin in a base-only commit.

## Licence

MIT. By contributing you agree your work ships under it.
