# ComfyUI Base

[![verify](https://github.com/Soul-Craft/ComfyUI-Base/actions/workflows/verify.yml/badge.svg)](https://github.com/Soul-Craft/ComfyUI-Base/actions/workflows/verify.yml)

A current ComfyUI on any NVIDIA GPU you can get hold of, rented or owned: one installer that finds the machine's
ComfyUI, pins its node packs to commits, owns the launch line and the boot, and brings the machine up the same way
on RunPod, Verda, Crusoe Cloud or a box under your desk. MIT, maintained by SoulCraft.

## Two ways to use it

**Just the base.** `comfyui-base-script.sh` on its own installs ComfyUI at its newest release plus six node packs at
their newest, among them **ComfyUI-Manager** and **ComfyUI-advanced-model-manager**, and keeps everything it installs
at its newest on every run: Python, torch on the newest CUDA your driver runs, every Python package (upstream pins
lifted), the tools, and the OS packages and NVIDIA driver. It ships no model
weights: bring your own, or add them with the managers it just installed. For most people that is the whole of
it, a current ComfyUI you did not have to assemble, on a GPU you rented an hour ago. Run whatever workflow you
like on it: your own, Comfy's templates, something from CivitAI or Hugging Face.

**The base plus a package.** A package is the optional layer on top: a thin `<name>-script.sh` that declares the
node packs, the models and the tokens one workflow needs, so that workflow comes back byte for byte on a machine
that has never seen it. The package contract is handbook §4. Nothing in the base knows a brand's name, so anyone
can publish packages for it from their own repository.

## What it is

- **The core** (`lib/`, `py/`, `suite/`) runs on any Linux host with an NVIDIA GPU and one persistent volume root.
  Four settings say what differs between hosts: `BASE_HOST`, `BASE_VOLUME`, `BASE_VOLUME_KIND`, `BASE_LISTEN`
  (`lib/00-env.sh` documents them; the handbook §1 explains them).
- **The hosts** (`hosts/`) are the per-host subsets: RunPod and Verda (both proven on live accounts), Crusoe Cloud
  (implemented, awaiting its first live run) and an owned box (`hosts/local`). `hosts/README.md` is the matrix.
- **The driver** (`_build/pod/podctl.py`) runs on your Mac: `podctl --provider <host> pods | status | ensure | install |
  stop | start | restart | deploy | upload | lease | tunnel`. Everything it does over ssh is the same on every host;
  the provider answers where the machine is and how it boots.
- **The package contract** (handbook §4): a thin `<name>-script.sh` declares its packs, models and tokens and calls
  the base; `package.py` builds its zip; `verify.sh` runs its suite from the repository and from the extracted zip.

## Quick start

On a fresh machine, step one is the base. Step two is only for those who want a package:

```bash
python3 -m zipfile -e comfyui-base.zip . && bash comfyui-base/comfyui-base-script.sh
cd <volume>/packages && mkdir -p "<name>" && python3 -m zipfile -e "<name>-<host>.zip" "<name>" && BASE_RESTART=1 bash "<name>/<name>-script.sh"
```

From a Mac, with the driver, the same two steps are `podctl ensure <machine>` then `podctl install <machine> --base --pkg <dir>`.
The handbook (`comfyui-base-handbook.md`) has the design rules, the per-host recipes and the package contract.

## Using it from your own repository

A brand repository keeps this base as a git submodule at `base/comfyui-base`, declares itself once in `<brand>/brand.toml`
(`name = "..."`, `host = "runpod" | "verda" | "crusoe" | "local"`) and puts its packages at `<brand>/packages/<name>/`.
A `<brand>/families.txt` beside it lists the model families its packages file under, one spelling each. Every tool
then finds them: `package.py --all`, `verify.sh`, the driver's `--pkg`, the shared testbed. Nothing in the base knows a
brand's name; what is specific to a project (its baked image, its boot extension, its own download tooling) lives in
that project's repository and plugs in through the seams the base leaves for it (`hosts/`, `ext/*.sh`, `brand.toml`,
`LOADER_CATS`). Models come from Hugging Face or GitHub; a package needing a file from anywhere else declares it
`LOCAL` and puts it on the volume itself.

A consumer can instead keep one repository per package, cloned side by side under a workspace as `<name>/` (the brand's `brand.toml` and `families.txt` at the workspace root),
and read the base from a shared checkout named by `$COMFY_BASE`, outside the workspace. Set `$BASE_WORKSPACE` to the
workspace (`package.py` also takes `--root <dir>`) and the same tools, `verify.sh` and `testbed.sh` included,
walk it. Both shapes are one rule, in `py/pkgdirs.py`: a package is a directory holding `<its name>-script.sh`.

## Tests

`bash base.sh test` runs the base's own suite (tiers: unit, install, comfyui, runpod, gpu, bare); from the extracted zip
it runs the same way. `bash _build/verify.sh` does both, for the base and for every package of the brand repository
around it, and is what CI runs on every push.

The `comfyui` tier needs a local ComfyUI to test against. `bash testbed.sh --server` provisions one — upstream at its
newest release, plus the base's own packs at their HEAD (or the branch a row names, 3.6.1) and every requirement at its newest, on the newest Python torch ships for (3.7.1): and starts it on CPU, so a clone of this repository can run
that tier on its own. It is several GB and gitignored. Tiers that need a GPU or a pod skip by name.

## Comfy MCP

Every machine the base installs comes up drivable by an agent: `comfy-mcp` (the official Comfy-Org MCP server)
goes into the run's venv, and the driver points a client at it.

**For a ComfyUI on your own machine** (Comfy Desktop, a local install, this repository's testbed), add comfy-mcp
to your own client at user or local scope, the way Comfy-Org's docs show. Nothing in this repository changes:

```bash
claude mcp add comfy-local --scope user -e COMFY_WHERE=local -e COMFY_LOCAL_URL=http://127.0.0.1:8188 \
  -e DO_NOT_TRACK=1 -e COMFY_NO_TELEMETRY=1 -- comfy-mcp
```

Add `-e COMFY_BIN=$(command -v comfy)` only when `comfy` is not on your PATH. Comfy-Org's `comfy skills install`
adds their agent skills (instructions, not an MCP server), and their `Comfy-Org/comfy-skills` marketplace plugin
connects Comfy Cloud's hosted server. This repository ships no `.mcp.json` and no settings that approve one: in a
repository many people clone, a committed server entry starts a command on each of their machines once they trust
the folder, and an entry that approves itself skips the per-server prompt that shows them that command.

**For a machine this base provisioned, use the driver.** That case is the one upstream's tooling does not
cover: its local flow assumes ComfyUI is on your machine, and its cloud flow assumes Comfy Cloud, while this
is a GPU you rented and installed with the base.

```bash
podctl --provider runpod mcp <machine>               # comfy-mcp runs ON the machine, over ssh: every tool
podctl --provider runpod mcp <machine> --over tunnel # it runs here, through `podctl tunnel`: the run tools only
```

It writes a `.mcp.json` beside you (gitignored: it names one machine and your paths). The ssh transport is the
default because `install_node`, `search_models`, `get_logs` and `fetch_outputs` need the ComfyUI tree, and the
tree is on the machine. `BASE_MCP=0` skips the install. comfy-cli's telemetry is disabled in the stage, in the
boot environment and in its config file — handbook §9.1 says why that is three places and not one.

## Contributing

`CONTRIBUTING.md` has the gate, the house rules the suite enforces and the release flow. The short version: one
command, `bash _build/verify.sh`, has to be green before any commit, and CI runs the same one on every push and
pull request. You need `uv` on PATH.

## Status

| Host | Status |
|---|---|
| RunPod | proven on live pods |
| Verda | proven on a live account, 2026-09-17 and 2026-09-18 (`hosts/verda/README.md` records what the runs answered) |
| Crusoe Cloud | implemented and tested against a fake API, not yet run on a live account (`hosts/crusoe/README.md` says what a first run should report) |
| an owned NVIDIA box | a recipe with a quickstart (`hosts/local/README.md`, RTX Pro 6000 / Blackwell), not yet reported end to end |

A live Crusoe run and a report from an owned box are the open items; issues and pull requests welcome.
