# ComfyUI Base — Handbook

**Version 3.13.0.** The shared toolchain every workflow package on a machine sources, on **any host with an NVIDIA
GPU** (RunPod, Verda, Crusoe, an owned box) and on any image or OS that gives it a driver and Python 3. One command,
run once per machine as **step one**; then each workflow package is **step two**, still one command. From a Mac,
`podctl` reaches the machine and hands its boot to the base (§1); after that every boot is the base's.

## 0. Design rules

1. **The base owns the machine — its boot included; a package owns only what is unique to it.** Toolchain (uv,
   Python, torch, ComfyUI, the shared node packs, launch-arg hygiene), the model library layout, discovery and
   relocation on the volume, the boot (sshd, JupyterLab, ComfyUI), restart survival and shared state are the base's.
   A package declares its workflow, its models, its extra packs and its own steps (hooks), nothing more.
2. **One canonical ComfyUI tree on the volume, one venv beside it, one stamp.** The tree is the one recorded in
   `state/boot.env`, else an existing volume tree (`/workspace/runpod-slim/ComfyUI`, `/workspace/ComfyUI`,
   `/workspace/*/ComfyUI`), else the base materialises `/workspace/ComfyUI` from GitHub at the newest release tag (or a
   newer `COMFY_REF`)  - 
   beside whatever `models/ user/ output/ input/ custom_nodes/` an image already keeps there. The container disk
   (`/ComfyUI`, `/opt/ComfyUI`, `$HOME/ComfyUI`) is never adopted and never scanned: it is restored from the image at
   every start. The venv is `$COMFY/.venv-cu130` (a name kept for every existing volume; its CUDA build is whatever
   §0.3 picks), stamped `.comfy-base-venv` (Python, torch, backend, base, the package that last stamped it); an unstamped venv that passes the probe is adopted and stamped; a rebuild happens at the same
   path with a backup and automatic rollback. A second tree on the volume is reported (`other tree`), its models are
   consolidated into the canonical one, nothing is deleted.
3. **Always the newest, for everything the base installs (3.0.0), and no fallbacks.** Every run, on every host:
   ComfyUI is its newest `vX.Y.Z` release on branch `comfy-base-stable` (`COMFY_REF` may name something NEWER, such as
   master, for one run; an older ref is refused). Every node pack, declared or not, is at its remote's HEAD, never
   moved backwards (local edits stashed by name first). Every Python package is at its newest: ComfyUI's and every
   pack's requirement files are never installed as written, because their `==` pins would put packages back down;
   `py/reqlift.py` merges them into one set with every hold lifted, uv resolves it, and whatever another package's
   cap still holds back is forced to its newest with uv's overrides. A pack whose own newest code cannot import with
   a newest dependency is named ("needs upgrading upstream") and the install goes on; ComfyUI's own startup failing is
   the one thing rolled back (only the packages that run changed). torch is the newest release on the newest CUDA
   build the machine's driver runs (`uv --torch-backend=auto`), never moved backwards on a shared venv. Python is the
   newest CPython minor that resolves that set, its newest patch, among the minors the torch family (torch,
   torchvision, torchaudio) has wheels for (one small compile each, 3.9.2). uv, JupyterLab, SageAttention (a published wheel or a
   build, whenever main or torch moves), comfy-cli, pytest and huggingface_hub (in the venv, and in the downloader's own `fetch/`, 3.11.0) are upgraded every run. The OS packages and the NVIDIA driver
   (NVIDIA's `nvidia-open`) are upgraded first (§0.15). A pick that cannot be made stops the run. A pack row's sha is
   the last-tested record, printed every run and written back by `podctl` after a green install; never a target.
   **One exception, a buyer's machine** (3.1.0, the maintainer's decision of 2026-09-25): with `BASE_RUNTIME` set, a
   machine installs the newest runtime that passed its maintainer's own sweep (§0.12) and changes nothing else, so a
   newcomer's first run never meets an untested upstream change. Its freshness comes from runtimes being rebuilt and
   swept continuously, and an Update that applies a newer proven one. Every other run is everything newest.
   **A second exception, a store that already holds the install** (3.11.0, Paul's "as fast as possible" of 2026-10-16):
   when this package's last run on the store was green with the same package files, the same base and the same GPU arch,
   and every file it declares is in place, the run starts ComfyUI if nothing serves and stops (§5); `BASE_FULL=1`, or
   any change, runs everything newest.
4. **Everything the venv boots from lives on the volume, and the boot cannot brick the machine.**
   `UV_PYTHON_INSTALL_DIR=<volume>/comfy-base/python`, `UV_MANAGED_PYTHON=1`; the probe is HARD on an interpreter
   written to the container disk, so a poisoned venv is never reused. `boot.sh` starts sshd first and never exits,
   so a venv that cannot boot leaves a reachable machine with the reason in the boot log; `rescue` repairs it without
   executing the interpreter. A volume root that is not a mountpoint is refused outright, unless
   `BASE_VOLUME_KIND=dir` says the machine is an owned box (§1).
5. **The library is `models/<category>/<Family>[/<Purpose>]/<upstream filename>`.** A Family is a plain folder name
   (letters, digits, `. _ -`); the base checks the shape, and a brand keeps one spelling per family across its
   packages in `<brand>/families.txt` (§4). Two packages that want the same file declare the same row and it is
   downloaded once. `vae_approx/` is flat; `LLM`, `custom_nodes/<Pack>`, `embeddings`, `sams`, `depthanything`,
   `SEEDVR2`, `RMBG`, `grounding-dino`, `latent_upscale_models` and `insightface` are freeform (flat by their
   consumers' design). Model files in a flat category folder that no row declares (an image's own downloads, the
   user's files) are reported as `unclaimed` and left where they are.
6. **No alternate or optional models.** A package's rows cover every loader that is ACTIVE in its shipped workflow;
   bypassed loaders are reported by name, not downloaded. A `LOCAL` row must already be on the volume or the run
   fails (upload it with `podctl upload`; the next run's scan claims it by name).
7. **The base boots the machine; a run restarts nothing by itself.** `boot.sh` (PID 1 once podctl has pointed a
   RunPod pod's start command at it; the process `comfy-base-boot.service` runs on a VM host) runs sshd → JupyterLab →
   ComfyUI from the base's venv with the base's launch line, then sleeps forever. An install prints the exact launch
   line; `BASE_RESTART=1` stops the running server (found by what it runs, never PID 1) and starts that line, the same
   function boot.sh uses (`lib/85-launch.sh`). One ComfyUI per machine: where `comfy-base-boot.service` is enabled the
   restart is the unit's (`systemctl stop`, then `start`), so the server is always the unit's, and the boot stops any
   ComfyUI it did not start before starting its own (3.0.3).
8. **One deletion prompt, default No.** Duplicates, superseded files (only inside the package's own family
   folders) and partials, never a file another installed package claims (the ledger). `BASE_YES=1` approves.
9. **Privacy and secrets.** Outbound hosts are huggingface.co, github.com, pypi.org, pypi.nvidia.com,
   download.pytorch.org, the distribution's apt mirrors and developer.download.nvidia.com (NVIDIA's CUDA repository); a `MODELS` row may name no other host. Nothing is uploaded. `--disable-api-nodes` is
   launch-arg hygiene. Tokens come from the shell env, else the machine's own environment (PID 1's: `HF_TOKEN` or its
   aliases `HUGGING_FACE_HUB_TOKEN` / `HF_HUB_TOKEN`), else `state/tokens.env` (0600), else one prompt. Template
   placeholders (`token_here`, `replace_with_ids`…) are named and ignored; a token its service rejects fails the run
   before anything downloads. No token ever appears in a log or on the process list: `hf` reads it from its
   environment, every `curl` to the Hub reads it from a 0600 header file (`-H @file`, 3.12.0), and the suite runs with
   every token name stripped. The install only reads, so a token whose role is `write` is accepted and named as more
   than it needs (a warning: a read-only or fine-grained read token is enough); only the role is read out of the
   Hub's answer, never the account it names (3.12.0). JupyterLab's token rides `JUPYTER_TOKEN` (or
   `JUPYTER_PASSWORD`) in its environment; without one the boot does not start it and says so (2.1.0). Since 2.5.6,
   where `BASE_LISTEN` is loopback (every host but RunPod, so the terminal is reachable only through an ssh tunnel)
   the boot GENERATES a token instead of leaving the terminal dark, and writes it owner-only to `state/tokens.env`.
   It is still never printed, for the same reason no other token is: with a shared store the boot log is a file every
   machine mounts. `podctl jupyter <machine>` reads it over ssh and prints the URL with the token already in it.
   3.9.0: JupyterLab is the pod's named "Jupyter Terminal" link, so the boot generates its token on EVERY bind; 2.1.0's
   public-bind refusal is gone because nothing listens tokenless and the token now has owner-only places to be read:
   `podctl create --public` puts a fresh per-pod `JUPYTER_TOKEN` in a RunPod pod's env and prints the ready link with it
   (`deploy --like` gives the clone its own; the private default hands it to the pod volume over ssh stdin instead),
   `podctl status`, `jupyter` and `tunnel` print the link with it, and on RunPod's public bind a token the boot made
   (never one the env set) is written as ONE line to the container log, the start command's own stdout (RunPod's Logs
   tab), which is how a pod from a public template with no ssh key gets its terminal. Never to the boot log.
10. **The base owns ComfyUI's launch flags.** `state/comfyui_args.txt` is the only source. An image's own args file
    (RunPod's `runpod-slim/comfyui_args.txt`) is imported once, on the first real run, and never read again; nothing
    creates a phantom file on images that have none. `HYGIENE_ARGS` entries are ensured present, `HYGIENE_ARGS_REMOVE`
    entries absent (the sage flag stays only when `sageattention` imports in the venv).
11. **The GPU must be ours.** `nvidia-smi` inside a container reports the whole card's memory but lists only the
    container's own processes; memory in use that no visible process holds belongs to another container or a leaked
    context on the host, and every render dies at its first node with the card idle. `py/gpu_facts.py` measures it
    (foreign = used − ours; more than 4 GiB is `NOT-OURS`), discovery prints the numbers and fails the run on
    `NOT-OURS`, `boot.sh` logs them at every start, and podctl asks the same question over ssh BEFORE it uploads
    anything (`install`), after every `start`/`restart --wait`, and in `status`. Nothing inside the pod can free that
    memory: the remedy is a fresh container (stop and start; the leak may live on the machine), else a redeploy of the
    pod on the volume — `podctl deploy --like <pod> --wait`, a NEW pod cloned onto the same volume (image, GPU type,
    disk, ports, env, the base's boot as its start command; the volume's datacenter; never a templateId) that lands
    wherever the datacenter has the GPU free — else the host's support with the machine id.
12. **An unattended machine is a mode, not a fork; nothing is ever frozen** (2.1.0, 3.0.0). `BASE_NONINTERACTIVE=1`
    makes every prompt take its default. A baked image may carry the base built at the volume's own paths by a
    project's image generator (`BASE_SEED=1`: toolchain and packs, no models, built where no GPU is, so its venv check
    asks for no device and its import check runs ComfyUI with `--cpu`, 3.9.1 and 3.10.0), parked on the container disk; a boot
    extension (`ext/`, §9) copies it onto an empty volume once, and the first install then takes everything to its
    newest like any machine. 3.0.0 retired the frozen mode (`BASE_PINNED`) and `COMFY_TAG`: they held a machine below
    its newest, which §0.3 forbids. The base ships neither the generator nor the extension.
    **A proven runtime is not a freeze** (3.1.0): `runtime-capture` packs a green machine's code (the tree minus its
    data, the uv Python, the venv, the git-checkout packs, JupyterLab, the wheel cache; never a package's vendored
    packs, which its zip brings) into parts under 2 GiB with `runtime.json`,
    the only place its versions are written; `runtime-apply <manifest>` unpacks it on a buyer's machine, and
    `BASE_RUNTIME=<state/runtime.json>` installs from it with no OS, git, pip or uv step, each skip noted, the driver
    gate held at the manifest's floor. A package that needs a pack the runtime lacks fails by name ("needs a newer
    runtime"). It is the §0.3 exception, and nothing else runs this way.
13. **The model library may be SHARED by several machines** (2.4.0). `BASE_LIBRARY` names a store that more than
    one machine mounts at once, and with it set `models/` and ComfyUI's `output/` and `input/` live there instead of
    on the machine's own volume: a model is downloaded once for all of them rather than once each, and a file one
    machine writes is there for the next with no copy. ComfyUI's own `models/` becomes a LINK to it, not an extra
    search path, because several packs never ask `folder_paths` where their weights are and join
    `folder_paths.models_dir` with their own folder name instead. `user/` and `temp/` stay on the machine: `user/`
    holds the saved workflows the App view opens, and each machine carries only its own package. With no library set
    nothing changes and every path in this handbook reads as it always did. On Verda the store is an `NVMe_Shared`
    volume over NFS at `/mnt/comfy-library`, and a configured library is a hard requirement of the boot unit, so a
    missing mount means no server rather than a panel of empty dropdowns.

14. **The WHOLE root may be the shared store, and then a machine is just a GPU** (2.5.0). This is the RunPod shape
    carried to a host whose machines are virtual machines rather than containers. On RunPod a pod has no disk of its
    own: one network volume mounts at `/workspace` and holds `comfy-base/`, ComfyUI, the venv, `packages/` and the
    models, so the pod is disposable and the volume is the asset. With `BASE_VOLUME_SHARED=1` the same is true here,
    and a machine carries only the disk it boots from. Two things follow, and the base does both for you: ComfyUI's
    `user/` and `temp/` move to `BASE_LOCAL_STATE` on the machine itself, because saved workflows, frontend settings
    and scratch are per machine and two servers writing one `user/` tread on each other; and an install takes a
    lock on the store, because pip and uv write a venv in place and neither expects a second writer. RUNNING takes
    no lock at all, which is what makes several GPUs on one store safe: running only reads.
15. **The OS and the NVIDIA driver are at their newest too, and the base never reboots a machine itself** (3.0.0).
    The first stage of every run (`lib/15-system.sh`, before the store's lock and before the driver gate): `apt-get
    full-upgrade`, non-interactive, config files kept, needrestart told only to list (Ubuntu 24.04 would otherwise
    restart the boot unit mid-install). The driver is NVIDIA's own newest-tracking `nvidia-open` with `cuda-toolkit`,
    from NVIDIA's CUDA repository (added from its newest `cuda-keyring` when missing). A machine on another family
    (Verda's images ship Ubuntu's `nvidia-driver-580-server-open`) switches once, in ONE apt transaction that purges
    the old family's packages and prebuilt kernel modules, after stopping the boot unit (that machine's renders end
    there, as on any install's restart). After any kernel or driver install the nvidia module must exist for the newest
    kernel, or the run fails and advises no restart. A driver that changed stops the run before anything uses the GPU:
    "restart required: A -> B", and `podctl` prints the restart a person approves (Verda forbids an in-guest reboot).
    RunPod upgrades the container's packages only (the driver is the host's; `libnvidia-*` and `cuda-compat*` held). A
    host the base cannot upgrade (not apt, no root and no passwordless sudo) is warned about and the run goes on. A new
    Ubuntu release comes from deploying the provider's newest image, never an in-place release upgrade. Each machine
    writes `state/machines/<host>.env` (driver, CUDA, kernel) to the store, so an install that moves the shared venv
    to a new CUDA major names the machines that must run their own install before they render.
## 1. Hosts

The core of the base (`lib/`, `py/`, `suite/`) runs on any NVIDIA GPU on any Linux with one persistent volume root.
What differs between hosts is how a machine is created, reached and booted, and that lives in `hosts/<host>/`: the
host's provider (Mac-side, loaded by `podctl`), its startup script and its README. `hosts/README.md` is the matrix.

| Host | The machine | Volume root | The boot | Listen | Status |
|---|---|---|---|---|---|
| `runpod` | a container behind RunPod REST v1, sshd on a mapped port | `/workspace`, a network volume (`mount`) | the start command `podctl ensure` wraps around `boot.sh` | `0.0.0.0` (RunPod's HTTP proxy needs it) | proven |
| `verda` | a VM behind Verda's (DataCrunch's) REST API, `root@<ip>:22`; an OS volume that IS the image next time | `/workspace`, a block volume `startup.sh` formats once and mounts | `comfy-base-boot.service` | `127.0.0.1`, reached through `podctl tunnel` | proven (live account, 2026-09-17 and 2026-09-18) |
| `crusoe` | a VM with an every-boot startup script and a default-deny firewall, `ubuntu@<ip>:22` | `/workspace` (`mount`) | `comfy-base-boot.service` | `127.0.0.1`, through the tunnel | implemented, not yet run on a live account |
| `local` | an owned NVIDIA box, no API | a directory of your choosing (`BASE_VOLUME_KIND=dir`) | `comfy-base-boot.service`, installed by hand | `127.0.0.1` | a recipe |

**The four knobs** the core reads (`lib/00-env.sh`; `_base_host_resolve` settles them once every lib is loaded):

- `BASE_HOST`: `runpod | verda | crusoe | local | vm`. Explicit, else `runpod` when PID 1's environment carries
  `RUNPOD_POD_ID`, else read from `<volume>/comfy-base/state/host.env` (the driver writes it on `ensure` and
  `install`), else `vm` (a VM whose host is unknown). `boot.sh` answers the same question the same way (`boot_host`).
- `BASE_VOLUME`: the persistent root, `/workspace` by default. Every path the base keeps across a restart derives
  from it (`<volume>/comfy-base`, `<volume>/packages`, `<volume>/ComfyUI` when the tree is materialised).
- `BASE_VOLUME_KIND`: `mount` (the default: the root must be a mountpoint, never a directory on the container disk)
  or `dir` (the default when `BASE_HOST=local`: a directory on an owned box is the volume).
- `BASE_LISTEN`: the address ComfyUI and JupyterLab bind. `0.0.0.0` on RunPod because its HTTP proxy needs it,
  `127.0.0.1` everywhere else (Verda has no cloud firewall; the driver's ssh tunnel is the way in).

**How the driver picks a host.** `podctl [--provider <host>] <command>`, the flag before or after the verb; without
it `$PODCTL_PROVIDER`, else the one `brand.toml` host of the repository this base sits in (two brands on two hosts
make the flag required), else `runpod`. The provider answers where the machine is and how it starts, stops, clones
and hands its boot to the base; ssh, scp, upload, install, the lease, the tunnel and the GPU gate are the driver's and
the same on every host. The `Host <alias>` block the driver writes is the provider's name (`runpod`, `verda`,
`crusoe`) unless `PODCTL_HOST` overrides it, and the key is the provider's (`~/.ssh/id_ed25519` on RunPod,
`~/.ssh/verda_comfyui` on Verda). `ensure` and `install` write `state/host.env` on the machine (`BASE_HOST`,
`BASE_VOLUME`). An experimental provider says so once per run, on stderr. Credentials (RunPod's key, Verda's client
secret) are read by the provider from the environment or its tool's own config file and are never printed, logged or
placed in an argument. A host whose provider is not implemented (Crusoe today) is refused by name: `hosts/<host>/`
holds the interface and a recipe.

### 1.1 RunPod (proven)

**The one command (2.0.14).** Once a pod is reachable (`podctl ensure`), the whole documented sequence is
`uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg "<package dir>"…`. For each item in order,
the local zip is checked current, the volume size is recorded for the disk gate, the zip is uploaded and extracted,
`--check` runs on the pod (non-zero stops everything), the real run goes detached (`BASE_RESTART=1` for a package;
`--comfy-ref <ref>` for a newer ComfyUI, that install only) and is polled until `STEP RC=`, the pod's `state/logs` are
copied to `<item>/_build/podruns/<date>/pod-logs/`, the summary block is printed, and a green run writes the pack records
it printed (each pack's HEAD) into the local script and rebuilds its zip. A run that upgraded the NVIDIA driver stops with
"restart required" and prints the `podctl restart` a person approves; the same install then continues. A red run stops with the console's path: the log is the deliverable, the fix loop
starts there. `podctl tunnel <pod>` is the one ssh session that forwards ports (8188, 8888, each app port the machine
serves (3.9.0), and 9199 and 11434 beside them:
the base runs no metrics exporter and no inference server, but a package may run one on the machine and reach
it on loopback, which is the shape a package takes when its point is that nothing leaves the box) for the canvas tools and
the browser; it prints them first as named localhost links (`ComfyUI http://localhost:8188`, `Jupyter Terminal
http://localhost:8888/lab?token=…`, one per app), which is how a `--private` pod's ports are named. `podctl prune <pod>` lists the old-layout folders an earlier layout left under `/workspace/packages`
(`<Display Name>/` with a `<Display Name> Script.sh`, and `ComfyUI Base/`) and their workflow copies in ComfyUI's
browser; `--yes` removes exactly those, after the re-install has put the `<name>/` folders beside them. `--pkg` takes
the repository-relative package path (`<brand>/packages/<name>`) or a bare `<name>` that one brand has.

The REST key comes from `RUNPOD_API_KEY`, else `~/.runpod/config.toml` (written by `runpodctl config --apiKey=…`);
it rides a bearer header and is never printed.

```bash
uv run "base/comfyui-base/_build/pod/podctl.py" status <pod>     # facts; env NAMES and placeholder verdicts, never values; the GPU verdict
uv run "base/comfyui-base/_build/pod/podctl.py" deploy --like <pod> --wait   # a NEW pod cloned onto the same volume (rule 11's redeploy rung)
uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> --data-gb 500   # a first pod: stock check (B200, then B300), PRIVATE: pod volume (not encrypted), 22/tcp only, ComfyUI via podctl tunnel
uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> --data-gb 500 --public   # not private: a network volume, ComfyUI and JupyterLab on proxy URLs
uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg <dir> --no-suite --env KEY=VALUE   # BASE_NO_SUITE=1 and KEY on every run on the machine
uv run "base/comfyui-base/_build/pod/podctl.py" ensure <pod>     # 22/tcp · PUBLIC_KEY = the Mac key · the start command · Host runpod · state/host.env
uv run "base/comfyui-base/_build/pod/podctl.py" upload <zip>...  # to /workspace/packages, size + sha256 checked both sides
uv run "base/comfyui-base/_build/pod/podctl.py" stop|start|restart <pod> --wait
```

`ensure` sets the pod's start command to `bash -c "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash …; fi;
<sshd bootstrap>; exec <the image's own entry>"` (the image's entry read from Docker Hub's registry; `--entry` for
private images). Before the base is installed that boots the image as it was plus sshd; afterwards it boots the base.
`stop`/`start --wait` rewrite the `Host runpod` block, because RunPod maps a new public port on every start. ComfyUI
(8188) and JupyterLab (8888) are also on RunPod's HTTP proxy ports, which is why this host binds `0.0.0.0`.

**Named ports (3.9.0).** RunPod's Connect tab links ports, not paths, and names a port only from the pod's TEMPLATE:
GraphQL `PodTemplate.portsConfig: [{port, name}]`, written by `saveTemplate` (REST v1 and v2 have no label field, and a
pod has no names of its own; a pod with no template shows "Port 8188 → HTTP Service"). So `podctl create` exposes
`8188/http` "ComfyUI", `8888/http` "Jupyter Terminal" and one `/http` port per customer app the tree's packages declare
(§4 `APP_PORTS`), then `22/tcp`, and puts the pod on a private template of the account, `comfy-base ports <sig>`, made
once per set of names (REST `POST /templates`, then the names re-sent with every other field through `saveTemplate`, the
way runpodctl v3 does). That template carries the names and nothing else: no env, no start command, no volume.
`--no-names` skips it. A failure to write the names is said and never costs the pod. `status` prints the links
(`https://<pod>-<port>.proxy.runpod.net`, the terminal's with its token). On the machine, `py/app_proxy.py` serves each
app at its own port's root (§6). Fallback
when the API is not available: the JupyterLab terminal on port 8888, where `bash lib/boot.sh --print-sshd-bootstrap`
prints the sshd snippet to paste.

**Which GPU (3.8.0).** The video packages are tuned for NVIDIA's datacenter Blackwell: **B200** (RunPod type
`NVIDIA B200`, compute capability 10.0 = `sm_100`, 180 GB HBM3e) first, then **B300** (`NVIDIA B300 SXM6 AC`, 10.3 =
`sm_103`, 288 GB). A package says so with `GPU_PREFER="B200 B300"` (§4), and `py/gpu_fit.py` is the one table and rule
behind it: the pod's own run warns in its banner and SUMMARY, and `podctl install` warns before it uploads, when the
GPU is below (another architecture with less memory: an H200 at 141 GB, an RTX PRO 6000 at 96 GB). Never a stop.
`podctl create` takes the same order when no `--gpu` is given, and a CUDA floor of 13.0. What holds on RunPod, read 2026-10-15/16 (stock moves; the
create reads it live):

- B200 is Secure Cloud, with stock in EU-RO-1, US-CA-2, US-NC-2, US-NE-1 and US-TX-6; B300 in EUR-IS-1, the
  one datacenter with B300 that also takes a network volume. `NVIDIA B300 SXM6 AC MIG 1g.34gb` is a 34 GB slice, not
  a B300, and `gpu_fit.py` never counts it as one.
- A network volume pins its datacenter: create it where the GPU is, and a pod on it can only ever be that DC's GPU.
- The CUDA floor is 13.0 (REST `allowedCudaVersions: ["13.0"]`; GraphQL calls it `minCudaVersion`): the base's cu130
  torch and the Blackwell kernels need a 580+ driver, and a host on CUDA 12.x answers with an older one.
- No NVENC. NVIDIA's encode/decode matrix gives the HGX B200 and B300 0 NVENC engines (7 NVDEC), like the H100, so an
  `h264_nvenc` save fails there; encode on the CPU (`libx264`). "RTX" features (RTX Video Super Resolution) name an
  RTX GPU, which these are not.
- Attention (3.7.5): SageAttention's `auto` (KJ's default, and `--use-sage-attention`) has no `sm_100` dispatch, so on a
  B200 it fails at the first step. The base runs every KJ mode on a tiny tensor and keeps the answer per GPU
  (`$VENV/.comfy-base-sage/<sm>.json`, `base_sage_mode`, `bash base.sh sage`); a package uses the mode it names, and
  SageAttention's Triton kernel (`sageattn_qk_int8_pv_fp16_triton`) is the safe choice on both chips.

### 1.2 Verda

Proven on a live account, 2026-09-17 and 2026-09-18 (2.5.3); the driver no longer warns. What those runs answered,
and the recipe in full, is `hosts/verda/README.md`; the shape:

- **Credentials:** a client id and secret (OAuth2 client credentials, exchanged for a bearer token), from
  `VERDA_CLIENT_ID` + `VERDA_CLIENT_SECRET` or `~/.verda/credentials` (mode 600). The key is `~/.ssh/verda_comfyui`;
  `ensure` registers its `.pub` with Verda when no registered key equals it.
- **The machine:** an RTX PRO 6000 class instance type (`VERDA_INSTANCE_TYPE`), the `ubuntu-24.04-cuda-13.0-open-docker`
  image, one location for the instance and every volume, and ONE data volume named `<machine>-data` beside it.
  `hosts/verda/startup.sh` (registered as `comfy-base-startup`, and run again by `ensure` over ssh) finds it as the one
  unmounted `/dev/vd[b-z]`, formats it ext4 only when blank, mounts it at `/workspace` by UUID with `nofail`, and
  installs `comfy-base-boot.service`.
- **The sequence:** `podctl --provider verda pods`, `start <os volume id>` (a new instance from that volume, data
  volumes attached), then `ensure <instance id>`, `install <instance id> --base`, `tunnel <instance id>`.
- **Stop and start are delete and redeploy.** A shut-down Verda instance still bills the GPU, so `stop` deletes the
  instance keeping every volume, and `start <os volume id>` redeploys from the OS volume (the disk, the packages, the
  unit and the fstab line all come back). The public ip and the instance id both change on every start; the provider
  rewrites the `Host verda` block, and the OS volume id is the stable handle of a machine.
- **No cloud firewall:** every port bound on `0.0.0.0` is on the internet at once, so the base binds loopback here and
  the tunnel is the way in. The balance is prepaid and at zero every volume goes to the trash, with 96 hours to restore.

The first live pass must settle the ssh user, whether the startup script re-runs on a redeploy, and when `ip`
populates (the README's numbered list); the EXPERIMENTAL notice comes off only when all three are known.

### 1.3 Crusoe (implemented, not yet run live)

A VM with an every-boot startup script and a default-deny firewall, `ubuntu` as the login user, `/workspace` as a
mounted block volume and the base's systemd unit as the boot. `hosts/crusoe/provider.py` is a signed REST client
carrying the whole Provider interface, and `suite/test_crusoe.py` drives it against a fake API that verifies every
signature; no call has reached the real service yet, so the driver still says EXPERIMENTAL. Three things differ
from Verda and shape the code: `stop` is a real stop (a stopped VM bills its disks only), the public ip is dynamic
and changes on every start, and the startup script is a create-time field that cannot be changed through the API,
so `ensure` ships the current one over ssh. The recipe, and what a first live run should report, is
`hosts/crusoe/README.md`.

### 1.4 local (a recipe)

An owned NVIDIA box: no API, no driver. `BASE_HOST=local` makes `BASE_VOLUME_KIND=dir`, so `BASE_VOLUME` is a
directory of your choosing; the base binds loopback; `base_boot_install` writes `comfy-base-boot.service` beside
`boot.sh` and installs it itself when it runs as root on a live systemd host, else it prints the `cp` and
`systemctl enable` to run. The recipe is `hosts/local/README.md`.

## 2. Step one on a fresh machine

```bash
cd <volume>/packages
python3 -m zipfile -e "comfyui-base.zip" .      # slim images have no unzip
bash "comfyui-base/comfyui-base-script.sh"
```

The base's zip is `comfyui-base.zip`, one artefact for every host. The script copies itself to `<volume>/comfy-base/`
and re-executes from there, then: resolves the host (§1), refuses a volume root that is not a mountpoint (unless
`BASE_VOLUME_KIND=dir`), upgrades the OS and the NVIDIA driver (§0.15), finds or materialises the canonical tree
(§0.2), reads the tokens (§0.9), moves ComfyUI to its newest release, gates on the version floor, moves the six shared
packs (rgthree, KJNodes, VideoHelperSuite, cg-use-everywhere, ComfyUI-Manager, ComfyUI-advanced-model-manager) to their
HEAD, builds or adopts the venv with every present pack's requirements at their newest (§0.3), sets the launch-arg hygiene, installs `boot.sh` + the boot unit on a VM host + the tools venv
(JupyterLab) + `state/boot.env`, writes its ledger entry, and prints the launch line. `--check` is the dry run (on a
volume with no tree it stops after "would materialise"). `bash base.sh status` lists every package installed on the
machine and, when the driver wrote one, prints `state/host.env`.

On a machine from a baked image step one has already happened inside the image: a boot extension (`ext/*.sh`, §9)
copies the seed onto the empty volume on the first boot, and the project's packages install themselves from their
zips with `BASE_YES=1 BASE_NONINTERACTIVE=1 BASE_NO_SUITE=1`, taking everything to its newest. The base ships no such
extension.

## 3. Step two: a workflow package

```bash
cd <volume>/packages && mkdir -p "<name>" && python3 -m zipfile -e "<name>-<host>.zip" "<name>" && BASE_RESTART=1 bash "<name>/<name>-script.sh"
```

A package zip is `<name>-<host>.zip`, the host being its brand's `brand.toml` (§4; `runpod` for a package with no
brand above it). It is flat and extracts into its own folder (`podctl install` does this): two packages both ship
`suite.py` and `pytest.ini`, and flat extraction let the second overwrite the first's (2.0.17).
Same six forms everywhere: install · `--check` · `--latest` (3.0.0: the same as a plain install; kept for old callers) ·
`test [tier]` · `rescue` · `sage` (3.7.5: does SageAttention RUN here; it answers before a package's own `sage` row) ·
`help`, plus the package's own `PKG_COMMANDS`. `BASE_RESTART=1` makes the summary's smoke,
combos and suite run against the server this run installed.

## 4. The package contract

A thin `<name>-script.sh` (60–200 lines) declares, then sources the base and calls `base_main "$@"`:

- Required: `PKG_NAME PKG_ID PKG_VERSION BASE_MIN WF_NAME COMFY_MIN PACKS MODELS`. A package whose `BASE_MIN` is newer
  than the installed base is refused, both versions named. `WF_NAME=""` is allowed (2.2.0): a package with no workflow
  yet; the packager ships no workflow member, and the sync, smoke and combo stages report "no workflow" and pass.
- The brand: a package belongs to the brand whose `brand.toml` sits above it (`<brand>/brand.toml`, with `name` and
  `host = runpod | verda | crusoe | local`). `py/brand.py` reads it; the packager names the zip `<name>-<host>.zip`
  by it and the driver takes its default provider from it. A tree with no `brand.toml` above it (an extracted zip, a
  flat checkout) is host `runpod` by default.
- Where the packages are (3.6.0; 3.7.0 for `podctl`): a package directory is any `d` holding `d/<basename(d)>-script.sh`, in one of two
  shapes under a consumer root. The submodule shape keeps the base at `<root>/base/comfyui-base` and the packages at
  `<root>/<brand>/packages/<name>/`. The workspace shape clones one repository per package side by side as
  `<root>/<name>/` (the folder is the package's name, so `<name>-script.sh` inside it passes the rule), keeps the
  brand's `brand.toml` and `families.txt` at `<root>`, and reads the base from `$COMFY_BASE`, a shared checkout outside
  the workspace; the workspace names itself with `$BASE_WORKSPACE`. The root is an explicit `--root <dir>` (the Python tools), else
  `$BASE_WORKSPACE` (the only override `verify.sh` and `testbed.sh` take), else `<root>` of the submodule shape, else
  none, and then only what the caller named is walked. A named root that does not exist is an error, and every walker
  fails on it rather than walking zero packages; so do `verify.sh` and `testbed.sh` when the helper itself fails.
  `py/pkgdirs.py` is that rule, once: `package.py --all`, `verify.sh`, `testbed.sh`, podctl's pin scan (which also
  reads the base's own `lib/40-packs.sh`, beside the driver) and the unit tier's cross-package checks all ask it.
  Dot-directories, a `comfyui-base/` checkout and the caller's own base are never packages.
- The brand's families: `<brand>/families.txt` beside `brand.toml`, one family spelling per line (`#` comments), is
  the brand's list of `Family` folders. The base's unit tier, run from the brand repository, checks every package's
  rows against it (`test_unit_every_package_family_is_in_its_brands_families_file`); on the machine a Family is
  checked for shape only, so no zip carries the list. A brand without the file skips the check.
- Optional: `WF_VERSION_KEY` (the `extra` key carrying the workflow's version; default `package_version`),
  `DRIVER_MIN SUPERSEDED LEGACY_DIRS DROPPED_PACKS TOKENS PIP_EXTRA HYGIENE_ARGS HYGIENE_ARGS_REMOVE LOADER_CATS
  PKG_COMMANDS PKG_IMPORT_CHECK PKG_NO_SUITE ZIP_EXTRA VENDORED_PACKS GPU_PREFER`. `GPU_PREFER="B200 B300"` (3.8.0) names
  the GPU classes the package is tuned for, best first, from `py/gpu_fit.py`'s table (an unknown class is an invalid
  declaration); a GPU below them is a warning in the run's banner and SUMMARY and in `podctl install`, never a stop
  (§1.1). `PKG_NO_SUITE=1` is how a package that ships no `suite.py`
  says so: the run's test stage notes it, and `verify.sh` passes the package as "no suite, declared". `LOADER_CATS`
  rows (`NodeType|library category`) extend the base's map of loader node types to library folders (`lib/60-sync.sh`
  covers ComfyUI's core loaders and the shared packs'); a package whose own pack loads models declares its loaders here.
- Optional, 3.9.0: `APP_PORTS=("<port>|<slug>|<name>" …)`, one row per customer app the package ships, which ComfyUI
  serves at `/apps/<slug>/` (a pack the package carries; the base serves none). The port is 1024-65535 and never one
  of the base's (22, 8188, 8888, 9199, 11434), the slug is the app's folder, the name is what the host's console and
  the tunnel call it (`APP_PORTS=("8189|video-replacer|Video Replacer App")`). The BOOT stage writes the rows to
  `state/apps.d/<package>.list` and restarts the forwarder; `podctl create`, `status`, `tunnel` and a brand's
  template read the same rows from the script. A port two packages declare fails `podctl`, naming both. The app must
  reach ComfyUI's own routes relative to its own URL (`new URL("../../", here)`), so it works at `/apps/<slug>/` and at
  `/` on its port alike, and may not name a top-level file after a route the forwarder passes through (`ws upload view
  history queue interrupt`).
- `VENDORED_PACKS=( dir ... )` (3.2.0): the package's own node packs, folders beside the script that ship in its zip
  (their files listed in `ZIP_EXTRA`). The base mirrors each into `custom_nodes` on every install, right after
  `pkg_post_venv` and before the requirements resolve: the installed folder becomes exactly what the zip carries (a file
  the pack dropped is gone), and a pack whose content did not change is not touched. Each installed copy carries a stamp,
  `.comfy-base-own` (package, version, content digest). On a shared store two packages may ship the same pack: the newest
  `pyproject.toml` version wins and is never moved backwards, and the same version with different content is installed
  and named (one of the two missed a version bump). An own pack that fails the import check is put back to its previous
  copy (kept in `state/own/prev/`) and the restart is blocked, so the live server keeps the code that worked. The ledger
  records `own <dir> <version> <digest>`; `BASE_DECLARE_ONLY=1` prints `OWNROW dir|version|digest`. A package no longer
  needs its own copy loop in a hook; one that still has it is harmless (the base saves the live copy before the hooks and
  mirrors after them).
- `LAUNCH_ENV=( "KEY=value" ... )` (3.10.0, optional): variables ComfyUI is started with. A package cannot reach the
  pod's start command or the boot unit, so this is its only way to set ComfyUI's environment. The install writes the
  validated list to `state/launch.d/<PKG_ID>.env` (removed when the package stops declaring one) and every launch, the
  install's restart and every boot alike, reads the whole folder (`lib/85-launch.sh`, `_base_launch_env`). Allowed keys:
  `OMP_NUM_THREADS MKL_NUM_THREADS OPENBLAS_NUM_THREADS NUMEXPR_NUM_THREADS VECLIB_MAXIMUM_THREADS
  TOKENIZERS_PARALLELISM PYTORCH_CUDA_ALLOC_CONF PYTORCH_ALLOC_CONF CUDA_MODULE_LOADING` and CTranslate2's `CT2_*`;
  a value is one word of `[A-Za-z0-9_.,:+-]`. Anything else (a path, a library, a token, a variable the base owns such
  as `HF_HOME`) refuses the package's declarations before anything downloads, and a key set twice does too.
  Precedence: the machine's own environment (a variable set on the pod, in the unit) wins; then the packages' files in
  name order, the first naming a key winning and a different later value named in the launch's output; then the
  base's one default, `OMP_NUM_THREADS` from the container's CPU quota. `BASE_DECLARE_ONLY=1` prints
  `LAUNCHENV KEY=value`. A base before 3.10.0 ignores the array, so a package that depends on it sets `BASE_MIN`.
- The CPU quota (3.10.0): when nothing sets `OMP_NUM_THREADS`, the base sets it to the CPUs the container's cgroup
  quota allows (`cpu.max`, v2, or `cpu.cfs_quota_us` / `cpu.cfs_period_us`, v1; rounded up; never more than the CPUs
  online), and leaves it unset when there is no quota. A RunPod container is a slice of a large host: `nproc` and torch
  both see the host's CPUs while the CFS quota lets the container run only its own. Its effect on torch: the intra-op
  pool (`torch.get_num_threads()`, every CPU op: preprocessing, CPU offload, a CPU-side VAE or encoder) is sized by
  `OMP_NUM_THREADS` when set, else by the CPU count torch sees, so without it a container with (say) a 16-CPU quota on
  a host torch counts as 112 cores runs 112 threads on 16 CPUs' worth of time and is throttled. The same variable sizes
  OpenBLAS's pool (numpy) and CTranslate2's: faster-whisper on CPU uses 4 threads unless `cpu_threads` or
  `OMP_NUM_THREADS` says otherwise (its `WhisperModel` docstring), and a node that never passes `cpu_threads` gets the
  quota's count. Inter-op threads, the GPU and CUDA kernels are unaffected. On a machine with no quota torch keeps its
  own default (the physical cores it sees), the right number there.
- Hooks, called if defined, dry-run aware via `$BASE_DRY`: `pkg_pre_venv pkg_post_venv pkg_pre_models
  pkg_post_models pkg_hygiene pkg_import_check pkg_post_install pkg_summary`, and `pkg_cmd_<name>` per
  `PKG_COMMANDS` row `name|function|help`. A failing hook fails the run. A package whose `pkg_post_venv` compiles
  something (SageAttention) is listed for re-run after any venv rebuild.
- `PACKS` rows: `dir|url|sha|cnr_id|why[|branch]`: `sha` is 40 hex, the last-tested record (every run takes the pack to
  its remote's HEAD); `why` is free text without a `|`; a base pack may not be redeclared; two packages naming one pack
  from two URLs (or two branches) is an error naming both, and two different records for one pack is a note.
  `branch` (3.6.1, optional, last so every five-field reader is unchanged) names the branch to track when the remote's
  HEAD is the wrong one: rgthree publishes a 2023 `dev` as HEAD while its work is on `main`, so its base row ends in
  `|main`. A row with a branch is cloned at that branch, an existing checkout on any other branch (a pod cloned from the
  stale HEAD, shallow or not) moves to it and is wired to track it, the fast path asks that branch's tip, and the
  testbed fetches it; never backwards, edits stashed, exactly as for HEAD. It is a branch, never a pin: a commit, a
  name with spaces (the tail of a `why` that held a `|`) or a seventh field is refused when the tables are validated.
  Leave it off unless the remote's HEAD is genuinely stale.
- `MODELS` rows: `category|Family|Purpose|file|url|bytes|note|alts` — `bytes` exact (`base.sh gen-models <dir>`
  fills it from the Hub); `url` is a Hugging Face `resolve` URL, a GitHub URL (a release asset), `LOCAL`, or
  `hf://owner/repo` with `file` ending in `/` for a repo snapshot; no other host is accepted; `alts` are legacy
  basenames the index may adopt. Pin a row to a commit (3.12.0): `resolve/<40-hex commit>/<path>` for a file,
  `hf://owner/repo@<40-hex commit>` for a snapshot (a branch after the `@` is refused). The revision a row names is
  the one downloaded, and every Hub download is checked with `hf cache verify` against the sha256 (LFS) or git hash the
  Hub states for that revision before it moves into place; a file that fails is deleted, never resumed. A file is
  hashed once, when it downloads (inside its own fetch job, so beside the other downloads), and its commit and etag go
  to `state/verified.tsv` from the metadata `hf download` writes anyway; a later run checks size and that record and
  hashes nothing, unless the row's pin moved to a commit whose file differs (one HEAD tells). An unpinned
  row follows `main`, so its bytes break the day the repository changes. `base.sh gen-models <pkg dir> --pin` prints
  the rows pinned to the commits they are at now. A Hub error retrying cannot fix (a gated repository, a refused
  token, a missing repository, file or revision) is said once in one plain line and not retried. `base.sh gen-hashes <pkg dir> [--check]` (3.4.0) writes `models.sha256` beside the
  script (`<sha256>  <file>`, the `sha256sum` form): each Hugging Face file's sha256 as the Hub states it on a HEAD
  (`x-linked-etag`), so nothing is downloaded or hashed; the packager ships it, `BASE_DECLARE_ONLY=1` prints
  `MODELHASH file|sha256`, and stamp-models adds `hash` + `hash_type` to those entries. `base.sh stamp-models <pkg dir>` writes ComfyUI's own `models` array into the
  shipped workflow from these rows (`--check` compares).
- `MODELS_LATER=( <file> ... )` (optional, 3.1.0): the MODELS rows the package's FIRST flow does not need, by their
  `file`. Only a staged run (`BASE_STAGED=1`, a buyer's machine) acts on it: each gets a stand-in (a 0-byte file at its
  dest, listed in `state/standins.list`), because ComfyUI validates every active loader's filename before a run, even
  on branches the run never takes; the server starts on the first flow's files; a detached `base.sh fetch-later`
  downloads the rest, and `state/progress.json` (§7) says where each is. A LOCAL row and a snapshot row cannot be
  later. The package keeps a buyer off a flow whose files are still stand-ins (its own page reads `progress.json`
  and refreshes its dropdowns as files land). Every other run ignores the list, and treats a stand-in it meets as
  absent.
- The workflow beside the script must carry `extra.<WF_VERSION_KEY> == PKG_VERSION`; the run refuses a mismatch.
- Helpers a hook may use: `ok miss err note warn hdr would todo`, `base_mkdir`, `base_run <cmd> <args>…` (dry-run aware: runs the command, or prints it under `--check`; 3.7.2: the
  install pipeline is `base_install`, so a hook that calls `base_run` no longer re-enters the whole install),
  `_base_yaml_register <section> <key> <path>`, `base_build_sageattention` (SageAttention 2 from source for this GPU's sm,
  dry-run aware; `SAGE_REF` names a branch or tag, default main; rebuilt whenever its full cache key moves; 2.0.22, 3.0.0;
  3.7.5: then PROVEN, `py/sage_probe.py` calling every KJ Patch Sage Attention mode on a tiny tensor against PyTorch
  attention, kept beside the build's stamp; "Sage: OK" means `auto` runs. On sm_100 upstream's `sageattn()` has no
  dispatch (it raises `Unsupported CUDA architecture: sm100`; SM100 support was withdrawn in 2025-12, thu-ml/SageAttention#322,
  and SageAttention3 refuses sm_100, #346), so the run warns and names the mode that runs, and hygiene drops
  `--use-sage-attention`. Upstream's arch table has no 10.3 (B300 got no qattn kernel); the build adds it as
  sm_103a. The proof is kept per GPU, as data: `base_sage_mode` prints the KJ `sage_attention` value for this GPU
  (`auto`, the named mode, or `disabled`), and `$VENV/.comfy-base-sage/<sm>.json` (`sys.prefix` inside ComfyUI) holds
  `{"sm", "build", "verdict", "mode", "runs", "fails", "version"}`. 3.13.0: before any build, the wheel this
  volume already holds for the key, then the one published for it (release `sage-<commit12>-torch<ver>-<cuda>-<cpXY>`,
  asset `sm_<arch>.<wheel>`, checked against `sm_<arch>.sha256` beside it), so a fresh volume downloads in seconds what
  it built in 10-20 minutes; with neither, a GPU whose mode is Triton builds `_fused` alone, about a minute), `$COMFY $CN $M $VENV $PY $PKG_DIR $BASE_STATE`.
  A hook that changed something the running server must reload appends its reason to `PKG_RESTART_WHY` (an array);
  the run's restart hand-off then restarts the server (`BASE_RESTART=1`) or prints the launch line with those reasons,
  never a green summary on a stale server.

Suites ship as `suite.py` + `pytest.ini` beside the script and load the base's plugin (`-p basetest`), which gives
`load_package`, `active_loaders`, `manifest_covers_active_loaders`, `brand_families`, `fake_pod` (layouts `official`,
`community`, `bare`, `volume`), `run_script`, `boot_stubs`, `boot_fake_pod`, `tree_hash`, `code_only`, `zip_extra` and the
per-tier table. `fake_pod` puts in `<root>/pkg` what the package's zip unpacks to: its top-level files, its vendored
packs and every path its script's `ZIP_EXTRA` lists, read as `package.py` reads it (3.9.2: a shipped directory that is
not a node pack, such as an app's `app/`, was missing; a listed file that is not there is an error). `bash "<name>-script.sh" test` auto-detects the testbed and its server on 8199, or on the port a running testbed
recorded in `.testbed-server.port` beside `testbed.sh` (2.2.0: `_base_use_testbed` and `verify.sh` both read it, so a
brand whose testbed runs elsewhere needs no env var). `--server` and `--stop` end only the server this checkout started,
the pid in `.testbed-server.pid` while it is still a ComfyUI `main.py` (3.9.2); a port another process holds is never
killed: `--server` stops, naming the holder's pid and a free `TESTBED_PORT`, and `--stop` says it left it running. `BASE_NODE_SRC` / `BASE_SERVER` override; the runner hands the
suite `BASE_COMFY` and `BASE_VENV`, and `BASE_WORKSPACE` when set (3.6.0: the consumer root `py/pkgdirs.py` walks in the
workspace shape, so a suite's cross-package checks see the workspace's packages); every other `BASE_*` is scrubbed.

Since 2.6.0 the testbed lives in **two possible places**, checked in this order: `<base>/testbed`, provisioned by the
`testbed.sh` that ships in this repository, and `base/testbed` beside `base/comfyui-base` in a brand repository. A root
is taken whole — the port file is read from the same root that supplied the tree, never from the other one. Before
2.6.0 only the second existed, so a clone of this repository on its own could never run the `comfyui` tier; it skipped
by name forever, which is a poor thing to hand someone who forked the repository to work on it. `bash testbed.sh
--server` provisions an upstream ComfyUI at its newest release tag (or `COMFY_REF`) plus the base's own packs at their
HEAD (derived from `list-packs`, never hand-listed), every requirement at its newest through `py/reqlift.py`, on the
newest Python torch ships for, and starts it on CPU. It is several GB and gitignored. 3.7.1: uv lists a CPython minor
before torch has wheels for it (3.15, October 2026), so the minors are tried newest first and the first one the torch
family resolves on wins, the pod's rule (`_base_python_pick`) on a smaller set; a venv whose python does not run (a
build that died, an interpreter uv removed) is replaced instead of blocking the next run.

## 5. Pipeline (install)

init (env · log · volume check · discover, or materialise the tree · declarations + `BASE_MIN` gate · banner) → system
(the OS and the NVIDIA driver at their newest; 3.5.0: started in the background and joined before the venv snapshot,
where the driver gate and a new driver's "restart required" stop apply; `BASE_SYSTEM_FOREGROUND=1` keeps the old order)
→ the store's install lock → require workflow → tokens (validated; a rejected one stops here) → update ComfyUI → version gate →
consolidate strays (the volume only) → packs git (every pack to its HEAD; 3.5.0: every remote's HEAD asked at once,
and a pack already on it is not fetched) → [join the OS stage · driver gate] → [start the model downloads in the
background (3.11.0, below)] → the venv snapshot → `pkg_pre_venv` → venv →
`pkg_post_venv` → own packs (VENDORED_PACKS mirrored from the zip, 3.2.0) → packs pip (the derived, pin-free set) → Comfy
MCP → newest (every package still behind forced to its newest; 3.3.0: each force is carried as a floor, `state/lift/forced.txt`,
so the next run's first resolve keeps it; a force a package refused is remembered in `refused.tsv` and tried again only when
either version moves) → `pkg_pre_models` → [join the model downloads] →
models (one index over the volume; a staged run stands in for the `MODELS_LATER` rows; `BASE_FETCH_JOBS` files at once; move on the same device else copy-verify-delete; hf-xet for the Hub, curl for
GitHub; disk gate first) → prune (one prompt; `unclaimed` reported) → sync workflow paths → `pkg_post_models` →
import check → hygiene → boot (boot.sh, tools venv, boot.env) → ledger → restart hand-off (or `BASE_RESTART=1`) →
smoke → combos → the later stage handed to a detached `fetch-later` (a staged run) → the package's suite →
`pkg_post_install` → summary (exit 0, or 1 when anything failed; the verdict kept in `state/last-run/<id>`).

**The downloads start beside the venv build** (3.11.0, `lib/52-models-early.sh`). Neither needs the other, so once the
packs are on disk and the OS stage is joined (it installs `python3-venv`, which uv's bootstrap needs), every download the
model stage would start runs in the background: a URL row whose dest does not exist and that no file on the machine
supplies by name and size (never a LOCAL row, a staged run's later rows, a partial or a stand-in), through the same
fetch, size and Hub checks. It overlaps the venv, the toolkit probe, `pkg_post_venv` (a SageAttention build), own packs,
packs pip, Comfy MCP and the newest pass. The join, just before the model stage, shows its output and folds what it got
into the run (counted once, as downloaded); the model stage then walks every row as before: a row the early pass left
alone is fetched there, and a file the early pass could not get fails the run by name with no second round of attempts
(its three were made, and their output is shown at the join). Its downloader is hf from
`$BASE_HOME/fetch`, a venv of its own with `huggingface_hub[hf-xet]` at its newest, because the main venv is rebuilt or
upgraded meanwhile; every download uses it once it exists (the model stage and `fetch-later` too), the main venv's hf
being the fallback, and it is left as it is while a `fetch-later` still runs from it. A run that stops before the join
stops its downloads too (they resume from staging next run). The clock shows two rows: `MODELS (in the background,
beside the venv build)`, the downloads' own seconds, and `MODELS · joining …`, how long they outlasted the venv.
`BASE_MODELS_FOREGROUND=1` keeps the 3.10 order; a dry run, a seed build and a buyer's runtime machine never start them.

**What waits for what** (3.11.0; Paul, 2026-10-16: "Parallelization of everything. Speed for everything!"). Every step
that does not need another's output runs beside it, and a join stands only where one truly needs the other:

    init (discover · tree · declarations · banner)
      ├─ OS stage (apt) ............................ background ─┐ joined before the venv snapshot (it brings python3-venv)
      └─ lock → tokens → ComfyUI update → gate → consolidate     │
           → packs git: every remote's HEAD asked at once, then every clone and fetch at once (BASE_PACK_JOBS)
           ◄────────────────────────────────────────────────────┘
           ├─ model downloads (BASE_FETCH_JOBS at once, largest first) ... background ─┐ joined before the model stage
           ├─ JupyterLab's tools venv .................................... background ─┼─┐ joined at BOOT
           └─ venv snapshot → venv → toolkit → pkg_post_venv (Sage) → own packs        │ │
                → packs pip (skipped when the venv build installed this exact set)     │ │
                → Comfy MCP → newest → [local-runtime copy, when lib/84-run-local.sh is in] │
                → pkg_pre_models ◄─────────────────────────────────────────────────────┘ │
                → models (every row judged) → prune → sync → pkg_post_models → import     │
                → hygiene → BOOT ◄───────────────────────────────────────────────────────┘
                → ledger → restart → smoke → combos → later stage → suite → summary

The serial spine is what really depends on its predecessor: the venv needs uv and Python (the OS stage's python3-venv);
packs pip needs the venv and every pack on disk; the newest pass needs the installed set; the model stage needs the packs
(a `custom_nodes/<Pack>` dest is inside its pack) and judges what the downloads left; the import check needs packs, venv
and models; the restart needs the import check. Not overlapped, and why: consolidate moves stray packs into
`custom_nodes`, so the pack pass waits for it; the ComfyUI update beside the packs would save about a second on a fresh
tree. A store that already holds this exact install green (same package files, same base, same GPU arch, every file in
place) runs none of it: `ALREADY INSTALLED`, ComfyUI started if nothing serves, done in seconds (`BASE_FULL=1` runs it all).

**One pass, and its clock** (3.2.0). Every stage header closes the stage before it with its seconds; the summary prints
them slowest first and `state/logs/<pkg>_<ts>.timings.tsv` keeps them, which podctl fetches with the console. When the
only change a run makes is the package's own packs (ComfyUI, every other pack and the venv's package list unchanged), the
import check loads ComfyUI with just those packs (`--disable-all-custom-nodes --whitelist-custom-nodes`); anything short
of a pass there is judged again by the full check. `<script> preflight` is what podctl runs before an install: seconds,
nothing changed, and it stops only on what would stop the real run before that run changes anything (declarations,
`BASE_MIN`, the volume, a workflow whose version is not the script's). `--check` stays the full dry report, without the
suite, which would only test the server the real run is about to replace. `podctl install --base --pkg <dir>` installs
the base and runs ONE pipeline, the package's (it already covers every stage the base's own run would); the base's suite
runs on a machine once per base version (`state/base-suite.ok`). A retry of the same zip against the same base skips the
Mac gate, and podctl polls every 5 s.

**A buyer's machine** (`BASE_RUNTIME`, 3.1.0): the same pipeline with the system step, the ComfyUI update, the pack
moves, the venv build, packs pip, Comfy MCP and the newest pass each replaced by a note; the tree and every pack are
checked against `runtime.json` instead, the venv must import torch, SageAttention must be the runtime's own build,
and a failed import check is a failure, never a rollback.

## 6. Any host: the contract

| Fact | On every host |
|---|---|
| The volume | the volume root, `BASE_VOLUME` (`/workspace` on the three clouds; your directory on an owned box): the only filesystem a stop/start keeps; a root that is not a mountpoint is refused unless `BASE_VOLUME_KIND=dir` |
| The tree | recorded in `state/boot.env`, else an existing volume tree, else materialised at `<volume>/ComfyUI` (§0.2) |
| The venv | `$COMFY/.venv-cu130`, interpreter under `<volume>/comfy-base/python` |
| Packs | the base's six + each package's, cloned into `$COMFY/custom_nodes`; other directories there are left alone but their requirements go into the venv |
| Models | scanned and consolidated on the volume only; the container disk is never read (it is gone at the next start) |
| Launch flags | `state/comfyui_args.txt` (§0.10) |
| Tokens | the machine's own environment, then `state/tokens.env` (§0.9) |
| The host | `BASE_HOST`, `BASE_VOLUME`, `BASE_VOLUME_KIND`, `BASE_LISTEN` (§1); `state/host.env` when the driver wrote it |
| The boot | `boot.sh`: PID 1 via podctl's start command on RunPod, the process of `comfy-base-boot.service` on a VM host: sshd (keys only) → JupyterLab (base tools venv, `JUPYTER_TOKEN`/`JUPYTER_PASSWORD`, else generated: never tokenless) → boot extensions (§9) → ComfyUI (base venv, base line, bound to `BASE_LISTEN`) → the app ports (3.9.0) → sleep forever |
| App ports | `py/app_proxy.py` (standard library, any python3, never the GPU): one process for every row of `state/apps.d/*.list`, bound to `BASE_LISTEN`. `:<port>/<path>` → `127.0.0.1:8188/apps/<slug>/<path>`, except `ws upload view history queue interrupt`, which pass unchanged; one request per connection (`Connection: close` both ways), then bytes piped both ways, so bodies, streams and the websocket's 101 pass unparsed; `Host` and `Origin` untouched; a page that says ComfyUI is starting while it does not answer. `boot.sh --apps` restarts it (the install runs it) |
| Restart | `_base_pids_from_table`: a python running `main.py` from the tree, or naming a `…/ComfyUI/main.py`; never PID 1 |
| Uploads | `<volume>/packages/` (`podctl upload`); each package extracted into its own folder there |
| A baked image | a project's own (the base ships no generator): the base built at these very paths and parked on the container disk; a boot extension copies it onto an empty volume once and installs the project's packages, non-interactive, everything newest (§0.12) |
| A buyer's machine | one OS disk and one NVMe data disk, created from nothing by `podctl create`; `runtime-apply` then `BASE_RUNTIME` + `BASE_STAGED=1` per package (`podctl buyer-install`); `pause` keeps the disks, `put-away` copies `output/` and `input/` to the Mac and deletes them (§0.3, §0.12) |

**RunPod, three image families:** RunPod's ComfyUI (tree `/workspace/runpod-slim/ComfyUI`,
`runpod-slim/comfyui_args.txt` imported once, sshd native); community templates that keep the code on the container
disk (`/ComfyUI`) with the persist dirs at `/workspace/ComfyUI`, so the base materialises the code beside them (no sshd;
their own JupyterLab, if any, unauthenticated unless `JUPYTER_TOKEN`; the base's never is); a bare GPU image (everything materialised). `/workspace` is a
network filesystem: `df` reports the shared pool, so the disk gate says when it cannot verify headroom. Slim images
have no `unzip`; `python3 -m zipfile -e` does the job.

**A VM host (Verda, Crusoe):** a fresh OS image with a driver and Python 3, and an empty block volume the host's
startup script formats (ext4, only when blank) and mounts at `/workspace` by UUID; the base's `comfy-base-boot.service`
runs `boot.sh` at every boot (`RequiresMountsFor=/workspace`, and it sleeps rather than fails while the base is not
installed yet). Everything else is the bare-image case: the tree, the venv, the packs are all materialised on the
volume, and the machine is reached through the driver's tunnel because the base binds loopback.

**An owned box (local):** `BASE_HOST=local BASE_VOLUME=<dir>`; the directory is the volume (`BASE_VOLUME_KIND=dir`),
the unit is installed by hand or by the base when it runs as root, sshd is whatever the box already runs, and nothing
listens beyond loopback.

## 7. State on the machine

`<volume>/comfy-base/` holds the library (`base.sh lib/ py/ suite/ hosts/`, verified against `MANIFEST.sha256` on
every source), `boot.sh`, `comfy-base-boot.service` (the unit's text, always written; installed under
`/etc/systemd/system` on a VM host), `ext/` when a project's image put extensions there (§9), `state/` (`boot.env`,
`host.env` (`BASE_HOST`, `BASE_VOLUME`, written by the driver), `comfyui_args.txt`, `tokens.env` 0600, `apps.d/<package>.list`
(3.9.0: the package's `APP_PORTS` rows), `launch.d/<id>.env` (3.10.0: each package's `LAUNCH_ENV`, §4),
`constraints-torch.txt`, `boot-facts.txt`, `volume.env` on RunPod, `gpu.lease`, `packages/<id>.manifest`, `logs/`: install
logs, `boot_<ts>.log`, `comfyui.log`, `jupyter.log`, `app_proxy.log`, `fetch-later_<ts>.log`; 3.1.0: `last-run/<id>` (the last run's
verdict), `runtime.json` and `runtime.stamp` (the applied runtime), and for a staged install `standins.list`,
`later.queue`, `later.failed`, `fetch-later.pid` and `progress.json`:
`{"format": 1, "updated", "stage": "first|later|done", "files": [{"file", "rel", "bytes", "state":
"present|standin|downloading|failed"}], "later": {"files", "bytes", "done_files", "done_bytes"}}`, every state read
from the disk and the file replaced whole, never written in place; ComfyUI's environment names the state dir as
`COMFY_BASE_STATE`, so a package reads `$COMFY_BASE_STATE/progress.json` and treats its absence as "nothing staged"),
`python/` (the uv-managed interpreter), `tools/` (JupyterLab), `fetch/` (3.11.0: hf, the downloader, in a venv of its own) and
`dead-venvs/` (quarantined venvs). A package manifest records its packs, model claims, superseded names and hooks;
`bash base.sh status` renders them and prints `state/host.env` when there is one; `bash base.sh latest` asks every
pinned pack's remote what HEAD is now.

## 8. Tests

Tiers: `unit` (source contracts on the library, podctl and the RunPod provider against a fake RunPod REST server,
boot.sh with stubs, the host layer in `suite/test_hosts.py` (the four knobs, the boot's host detection and extension
seam, the systemd unit, brand.toml, the driver's provider binding), the Verda provider against a fake REST server in
`suite/test_verda.py`, the derive kit in `suite/test_derive.py`, the canvas kit on a synthetic workflow in
`suite/test_canvas.py`), `install` (the stub package under `_build/stub` run end to end against fake pods of every
layout, offline), `comfyui` (the repo testbed), `runpod` / `gpu` (pod only: the venv the boot activates, the running
server on it, PID 1 = boot.sh with sshd and JupyterLab answering; the last two skip until the stop/start hands the boot
over), `bare` (the shell sources without a machine).

```bash
cd base/comfyui-base && bash base.sh test                                                        # every tier
uv run --no-project --with pytest python base/comfyui-base/_build/rehearse.py <brand>/packages/<name> --layout official|community|bare|volume
bash base/comfyui-base/_build/verify.sh [<brand>/packages/<name>...]                            # zip current, suite from the repo, suite from the zip
```

The rehearsal unpacks the shipped zips, runs the two steps on a fake pod of that layout, then boots it through
`comfy-base/boot.sh` with stubs; ALL OK means the zips are ready to upload. Standalone (this base as its own
repository), `rehearse.py stub --layout volume` uses the fixture package.

### 8.1 The canvas kit — what every package's canvas is tested with

The same shape as the installer: the base owns the machinery, a package owns its tables. Each piece was written
against the real frontend and a real defect.

| What | Where | Package side |
|---|---|---|
| graph helpers (`byid` through subgraphs, `links`, the frontend's bypass resolver `resolve_frontend`), geometry (`nbound`, rgthree's `membership`), the size floor, `structure_sha`, `note_height`, `Budgets`, `CanvasSpec` | `py/canvas.py` | `from canvas import ...` in suite.py |
| 41 generic tests: the `uiux` budgets (collisions, spacing, grid, notes and labels fit, LOD cliff, fill, bloat, flow, palette, titles, reading order), the `snapshot` tier (the real frontend), the shape-only `config` guards (zip = shipped pair, versions agree, tier gates, handbook numbers, packs pinned) | `py/canvastest.py` | `CANVAS = CanvasSpec(...)` then `from canvastest import *`; markers `uiux`, `snapshot`, `config` in pytest.ini |
| `snapshot.py` (photograph + `sizes.json`), `render.py` (offline review, `--review`), `page.py` (before/after), `panels.py` (every rgthree row clicked in the real frontend), `probe.py` (`graphToPrompt` per state), `canvaslayout.py` (the layout engine) | `_build/canvas/` (repo tooling, not in the zip) | every tool takes `--pkg <package dir>`; `<pkg>/_build/layout.py` = tables + `run(globals())` |

Adopting a package: (1) put its constants in suite.py as literals and declare `CANVAS = CanvasSpec(user_facing=...,
markers=..., cuda_only=..., start_card=..., display="1512x982@2", nest=..., partial=..., tier_gates=...)`, then
`from canvastest import *` before the suite's marker loop; (2) `uv run "base/comfyui-base/_build/canvas/snapshot.py" --pkg <dir>`
once, with the testbed server up, for its `_build/sizes.json`; (3) `python3 "base/comfyui-base/_build/canvas/render.py" --pkg <dir>
--review` and fix what the budgets find -- for a canvas that never had them, expect off-grid boxes, frames too big for
what they hold and a saved view under the LOD cliff; `canvaslayout.py` with a tables file is how a package relays
itself out.

## 9. Boot extensions

`boot.sh` runs sshd, JupyterLab and the saved tokens, then sources every `ext/*.sh` it finds, in name order, before it
starts ComfyUI (`boot_extensions`, 2.2.0). It looks in two places and takes the first that has any: `$BOOT_HOME/ext`
(that is `<volume>/comfy-base/ext`, the installed base on the volume), else `ext/` beside `boot.sh` itself. The second
place exists for a baked image's FIRST boot: the volume is still empty then, so the only copy that can run is the
seed's own, beside the seed's `boot.sh` on the container disk, and copying the seed onto the volume is exactly what
such an extension does. An extension is a bash file sourced into the boot's own shell, so it sees `BOOT_HOME`,
`BOOT_VOLUME`, `BASE_HOST` and `BASE_LISTEN` and must not `exit`; the boot logs each file it sourced, or "no
extensions" when neither place has one.

The base ships no extension. A project's image adds its own stages this way (copying its seed onto an empty volume,
provisioning, a status page), in the project's own repository.

## 9.1 Comfy MCP (the agent's way in)

`comfy-mcp` is a stdio MCP server wrapping `comfy-cli`. Since 2.7.0 the base installs it into the run's venv
(`lib/45-mcp.sh`), beside ComfyUI itself, and `podctl mcp <machine>` writes the client configuration.
`BASE_MCP=0` skips the install; the stage never fails a run.

There are two transports and the difference is not cosmetic:

| | where comfy-mcp runs | what works |
|---|---|---|
| `--over ssh` (default) | on the machine | everything: `install_node`, `search_models`, `get_logs`, `fetch_outputs`, `launch_comfyui`, and the run tools |
| `--over tunnel` | on your Mac, reaching `COMFYUI_URL` through `podctl tunnel` | the run tools only |

The lifecycle and discovery tools need the tree, and the tree is on the machine — which is why the server is
installed there and ssh is the default. The ssh transport passes `ClearAllForwardings=yes`: the `Host` block
`podctl ssh-config` writes carries four `LocalForward` lines, and a second session binding them while a tunnel
holds them prints warnings onto the very stdio channel MCP is speaking. The tunnel transport takes the alias's
own local port (`tunnel_locals`), so two machines never point at one tunnel.

**What this repository does NOT ship.** No `.mcp.json` is committed, and no settings file approves one
(`enabledMcpjsonServers`, `enableAllProjectMcpServers`). This is a public toolchain many people clone: Claude Code
starts a project's servers once a person trusts the folder (and a headless `claude -p` without asking), and a
committed approval folds the per-server prompt, the one that shows the exact command, into that single trust click,
which is the pattern the MCP specification's security guidance warns against. The entries would also be one person's
setup (a port, a path). The local case is the person's own: `claude mcp add ... -- comfy-mcp` at user or local scope,
as Comfy-Org's docs show (the README has the line). Corrected in 3.5.0: `comfy skills install` writes Comfy-Org's
agent skills, not MCP client configuration; their `comfy-skills` marketplace plugin connects the hosted Comfy Cloud
server; and comfy-mcp 0.9.0 and 0.10.0 came two days apart, not two months. What stays here is the case upstream does
not cover: a machine the base provisioned on a rented GPU. `podctl mcp` writes that file beside you, and `.gitignore`
keeps it out of the tree because it names one machine and one operator's paths.

**`COMFY_BIN`.** comfy-mcp is a wrapper: its discovery and lifecycle tools shell out to comfy-cli, which it
finds through `COMFY_BIN` or `PATH`. An MCP client is usually launched by a GUI, whose `PATH` is not your
shell's, so `PATH` is the thing not to rely on. `podctl mcp` therefore names the binary: over ssh it is the
`comfy` beside the machine's `comfy-mcp`, and over the tunnel it is this machine's, when one can be found (it
says so when none can). MEASURED 2026-09-19 against a live ComfyUI: without it, `server_info` answers "Error
executing tool server_info"; with it, it answers with the interpreter, the config path and the bound workspace.

**Telemetry.** `comfy-cli` depends on `mixpanel` and `posthog`, and rule 9 says the outbound hosts are an
allowlist and nothing is uploaded. That wheel is not our source, so the allowlist test cannot see it. Three
things are done instead, in order of how much they can be relied on: `DO_NOT_TRACK` and `COMFY_NO_TELEMETRY`
are exported before anything from the wheel runs — comfy-cli honours the `DO_NOT_TRACK` convention by never
importing its telemetry extension at all, so this is the code path not loading rather than a request to be
quiet; the same two go into the boot environment, because a machine that reboots must come back as quiet as it
was installed; and `comfy tracking disable` writes the config file (`~/.config/comfy-cli/config.ini` on Linux)
for any invocation that somehow arrives without the environment. The suite asserts the first two.

## 10. Record

- 3.13.0: SageAttention is downloaded, not built, on a fresh volume (Paul, 2026-10-16: pods install as fast as
  possible). With the model downloads in parallel (3.11.0) the source build (10-20 min) was a pod's biggest step. A pod's key
  (upstream commit, Python, torch and its CUDA build, the GPU's sm) is now built ONCE per arch by
  `.github/workflows/sage-wheels.yml` (daily and by hand: `_build/sage/resolve.py` asks what a pod would pick today,
  torch through `py/torch_pick.py`; `_build/sage/build-wheel.sh` builds it in the matching `nvidia/cuda` devel container
  with the base's own source patch, `_base_sage_prepare_src`, and checks the wheel carries its kernels and imports)
  and published as a prerelease `sage-<commit12>-torch<ver>-<cuda>-<cpXY>`: per arch (9.0, 10.0, 10.3, 12.0)
  `sm_<arch>.<wheel>` then `sm_<arch>.sha256`, and `LICENSE-SageAttention.txt` (Apache-2.0). Assets are write-once (the
  workflow refuses to replace one; the organisation's tag ruleset keeps `sage-*` tags from moving). `_base_sage_install`
  tries, in order: this volume's wheel cache; that release (the sha256 checked before pip sees the file, a mismatch
  refused by name); a `_fused`-only build where the GPU's mode is Triton (this volume's kept proof says so, or with no
  proof yet upstream's `sageattn()` has no branch for the sm: sm_100 and sm_103 today); the full build. `_fused` is the
  one extension `core.py` imports unconditionally, and the Triton mode calls no qattn kernel, so that build takes about
  a minute; if its proof then finds Triton does not run, every kernel is built and proven again. The proof's mode is now
  also kept on the volume (`state/sage/<sm>.mode`), so it outlives a venv rebuild. The cuBLAS probe runs only before a
  build. `SAGE_WHEELS=0` skips the download, `SAGE_FUSED_ONLY=0` the `_fused`-only build, `SAGE_WHEELS_URL` names
  another releases root. The Sage step needs no new join with the early downloads: it runs in `pkg_post_venv`, inside
  their window. Exercised on this Mac against a file:// release mirror and upstream's real setup.py and core.py; the
  wheels are built by CI; the pod measurement is in the PR.
- 3.12.0: the Hub, pinned and checked (Paul, 2026-10-16: Hugging Face best practices). (1) A row's revision is what
  downloads: the file leg parsed `resolve/<revision>/` and then dropped it, so a commit-pinned URL silently fetched
  `main`; it now passes `--revision`. Snapshots take `hf://owner/repo@<commit>` (the 40-hex form only), which the
  base could not pin before. (2) Every Hub download is checked with `hf cache verify` against the hashes the Hub states
  for that revision (sha256 for LFS files) before it moves into place; size alone had been the check. A file that fails
  is deleted rather than kept for resume; a snapshot that fails is kept, since `hf download` resumes it file by file.
  An `hf` without `cache verify` checks size only and says so. (3) The size and sha256 HEADs put `Authorization:
  Bearer <token>` in curl's argv since 2.0, on the process list against §0.9; they now read a 0600 header file. `gen-models` and `gen-hashes` now install the
  exit trap, so that file (and every temp dir) is gone when the verb ends. (4)
  A token whose role is `write` is accepted with a warning that a read-only or fine-grained read token is enough;
  the whoami answer is read for its role only and deleted. (5) Gated, 401, not-found and bad-revision errors are said
  once, in one line that names the fix, and not retried; transient errors keep three attempts, and the last no longer
  sleeps. (6) `base.sh gen-models <dir> --pin` prints the rows pinned to the commits they are at now (the first hop's
  `x-repo-commit`, the API's `sha` for a snapshot); stamp-models links a pinned snapshot to its `/tree/<commit>`. The
  hf_xet fast path (`HF_XET_HIGH_PERFORMANCE`, 32 range gets, no chunk cache) and resumable downloads were already in
  place and are unchanged. Speed first (Paul, 2026-10-16): a file is hashed ONCE, in its own fetch job beside the
  other downloads, never in a pass afterwards; `state/verified.tsv` records its commit and etag from hf's own
  `--local-dir` metadata (no request), and a file already in place costs nothing on any later run (size + record; one
  HEAD only when its row's pin moved). Measured: `hf cache verify`'s hash runs at 2.6 GB/s on an M4, so the 174 GB
  Video Replacer set is about 70 s of hashing in total on a fresh volume, spread over the parallel downloads, and 0 s
  on a volume that has it. 3.11.0's downloads beside the venv build take the same path (pins, hash, record) and
  now also take a file whose row's pin moved, so it no longer waits for the model stage. Exercised on this Mac with a stubbed `hf` and `curl` (suite/test_hub.py) and against the
  real Hub for `hf cache verify` (a flipped byte fails it, exit 1); not on a pod.
- 3.11.0: the model downloads start beside the venv build (`lib/52-models-early.sh`, §5). Once the packs are on disk and
  the OS stage is joined, every download the model stage would start runs in the background through hf from the
  downloader's own venv (`$BASE_HOME/fetch`), and is joined just before the model stage, which still judges every row:
  an early file is counted once (as downloaded), a row the early pass left alone is fetched there, and a file it could
  not get fails the run by name with no second round of attempts. A run that stops before the join stops its
  downloads (they resume from staging). `BASE_MODELS_FOREGROUND=1` keeps the old order. Measured on RunPod, two fresh
  pods made side by side (H100 80GB HBM3, EU-FR-1, `runpod/base:1.4.0-cuda1300-ubuntu2404`, a network volume, the MiniMax
  H3 Video Continuer 1.0.5 with its 41.1 GB of models, `--no-gate --no-suite`), wall clock from `podctl create` to the
  workflow listed through the pod's public proxy: 3.9.2 1610 s, 3.11.0 1471 s (139 s less; 167 s less from the pod's
  first ssh to the end of the install, against a 181 s download stage in 3.9.2). The early downloads ran 605 s at 70 MB/s
  beside the venv, packs pip and newest pass (304 MB/s alone in 3.9.2), and the join waited under a second. Where 3.9.2
  spent its 1532 s: venv 589, newest 346, downloads 181, packs pip 112, import check 108, restart 61, boot 45, packs git
  36, consolidate and tree 19, the OS stage 25 in the background. Both runs ended red on the same smoke miss
  (`LoadImageCrop` not registered), a package matter.
- 3.10.1: `podctl ensure` on a fresh RunPod pod, and one clearer STOP (three defects from the Video Swapper's pod install,
  2026-10-16). (1) `ensure` reset a fresh pod while its image was still in its own first boot (`runpod/comfyui` copies
  ComfyUI and its venv onto the volume then): no venv, a crash loop, ssh never up (pod wibxqf22v26shv). It now waits,
  before its first reset of a pod the base's boot does not own yet, until an image that ships ComfyUI answers
  through the proxy on 8188 (any status below 500), or, for any other image (`runpod/base`), three minutes after the
  pod started; at most 15 minutes. (2) After its own reset it kept polling the OLD ssh port: the API answers with the pod as it was for a while
  after the PATCH, and RunPod had moved 22/tcp (20424 to 36944). It now waits for `lastStartedAt` to move and looks for
  sshd on the port the pod has now, re-read on every try. (3) `podctl install` on a package folder not named after its
  package (`vr` for `video-replacer-minimax-h3`) stopped on "vr has no zip (vr-runpod.zip)"; it now says the folder's
  name must be the package's and names the one to rename it to. (4) `podctl create` offered data centres a pod cannot be
  created in: the stock view names more than REST v1 `POST /pods` takes (its `dataCenterIds` enum has 30 ids and lacks
  US-NE-1, a B200 location: a schema 400), and it picked ones that take no network volume (US-NC-1, US-MO-2, EUR-IS-2:
  `POST /networkvolumes` 500). The candidates are now those in the live enum (REST v1's own `openapi.json`, read once per
  run; the 30 ids read on 2026-10-16 when it cannot be read) and, when a network volume is wanted, those whose GraphQL
  `storageSupport` is true; each one left out is named. Exercised on this Mac against the suite's fake RunPod, which now
  answers stale reads after a reset and moves the ssh port; (2) also on a live pod (16174 became 19093, followed).
- 3.10.0: a package can set ComfyUI's launch environment, and ComfyUI starts sized to the container's CPU quota
  (2026-10-16). `LAUNCH_ENV=( "KEY=value" ... )` (§4): an allowlist of thread, tokenizer, allocator and CTranslate2
  (`CT2_*`) variables, each value one word, so no path or secret can reach a file or a log; anything else, or a key set
  twice, refuses the package's declarations before anything downloads. The install writes `state/launch.d/<id>.env`
  (removed when a package drops the array; a change restarts ComfyUI) and every launch, install restart and boot alike,
  reads the folder: the machine's own environment first, then the packages in name order (a conflicting later value is
  named), then the base's one default, `OMP_NUM_THREADS` from cgroup `cpu.max` (v2) or `cpu.cfs_quota_us` (v1), rounded
  up, capped at the CPUs online, unset with no quota. Exported only in ComfyUI's own shell and printed on the launch
  line. Why: a RunPod container is a quota on a large host, and torch's intra-op pool, OpenBLAS and CTranslate2 sized
  themselves to the host's CPU count and were throttled; faster-whisper on CPU, which ran 4 threads unless told
  otherwise, now takes the quota's count with no package change (the Video Replacer only raises `BASE_MIN`). Checked
  on this Mac: torch honours `OMP_NUM_THREADS` (`get_num_threads()`); owed on a pod: the boot log's `launch env:` line.
  And `_build/canvas/snapshot.py` names a console error it used to leave unexplained: ComfyUI-Manager's
  `loadGraphData` wrapper reading `undefined.extra` when comfyui-obvpm 0.2.16's wrapper turns the frontend's
  zero-argument startup call into a one-argument one (the startup default graph then does not load). Matched only with
  Manager's frame on top; the one-line fixes belong upstream in either pack. And a seed build gets past its import
  check: with `BASE_SEED=1` (the template image, built where no GPU is) `main.py --quick-test-for-ci` died "Found no
  NVIDIA driver" before importing a pack, the next wall after 3.9.1's venv verify (runpod-template's image job,
  2026-10-16). Both the full and the scoped check now pass `--cpu` for a seed build alone (`_base_quick_cpu`): every
  pack is still imported and judged, and a real machine's check still starts on its GPU. Unit tests on fakes (cgroup
  root, stub python, the bash 3.2 boot path, the import check's arguments) and the classifier test.
- 3.9.2: three harness fixes (2026-10-16). Two were found porting the Video Replacer 5.1.0 into its own repository.
  `basetest.fake_pod` copies every path a package's `ZIP_EXTRA` ships (new helper `zip_extra`), so a shipped
  directory that is not a node pack reaches the fake machine (the Replacer's `app/`, which its install test had copied by
  hand). `testbed.sh` stops only the server it started: it used to kill whatever listened on `$PORT`, and on a Mac shared
  by several sessions that was another checkout's ComfyUI on 8199. Ours is the pid `--server` recorded (now python's own
  pid, not the subshell's), or its child for a pid file written before, and only while its command line is a ComfyUI
  `main.py`; `--server` on a held port exits 1 naming the holder and a free `TESTBED_PORT`; `--status` says whose server
  answers. The third came from the Video Swapper's pod install: a fresh venv lost minutes on CPython 3.15, which uv
  lists before torchaudio ships a cp315 wheel (the backend pre-pick ran `torch_pick.py` for it, stepping down through
  every CUDA build, then `_base_python_pick` ran the full derived-set resolve for it, before both fell back to 3.14).
  `_base_family_minors` now asks uv for the torch family alone, per minor, newest first, and both picks start from
  the newest minor that has it (the testbed has done so since 3.7.1). Unit tests for all three.
- 3.9.1: `podctl create --private` (the default since 3.8.1) works again, and says plainly that its volume is not
  encrypted. It sent `volumeEncrypted: true`, which REST v1 `POST /pods` refuses ("Extra input keys": the field is on the
  Pod read-back only), so every private create failed before anything was made; and its summary printed "ENCRYPTED pod
  volume" without asking. Read 2026-10-16 from RunPod's own sources: REST v1 `PodCreateInput`, REST v2 and runpodctl
  3.0.1 have no encryption input; GraphQL's deploy inputs carry an undocumented `volumeKey` string (not used: unverified,
  and the docs say bring-your-own-key is not supported); the storage docs name one way, the console's "Encrypt volume"
  box. So the body no longer carries the field, a `note` line says "NOT encrypted" before the pod is created, the summary
  prints what the read-back says, and the seconds-old-pod deletion on `false` is gone (false is now the expected answer).
  The suite's fake RunPod refuses any `POST /pods` key outside the live `PodCreateInput`, so a field the API does not
  take fails on the Mac first. Exercised on this Mac against the fake; not against the live API.
  And a seed build works again: `BASE_SEED=1` (the template image, built where no GPU is) failed its venv verify on
  "torch.cuda.is_available() is False" and rolled the venv back, so `base-image.yml` had never pushed an image (two runs
  on 2026-10-16, both red at step one). The install now passes `BASE_NO_GPU=1` to `py/venv_verify.py` for a seed build
  alone, which drops that one question (the CUDA build, the archs and the closure are still checked); a machine with no
  visible device still fails. Exercised on this Mac against a fake torch; the image build is CI's to show.
- 3.9.0: named ports. On RunPod's Connect tab every link has a name: `8188` "ComfyUI", `8888` "Jupyter Terminal", and
  one port per customer app a package declares (`APP_PORTS`, §4), through a private port-names template `podctl
  create` makes once per set of names (§1.1; RunPod names ports only on templates, GraphQL `portsConfig`). The machine
  serves each app at its port's root with `py/app_proxy.py` (§6), restarted by the install and started by the boot
  after ComfyUI. JupyterLab is never tokenless and never dark for want of a token: a per-pod `JUPYTER_TOKEN` from
  `create` (a fresh one for a `deploy --like` clone), else generated by the boot on any bind, read back by `status`,
  `jupyter` and `tunnel`, and on RunPod's public bind named once on the container log, never the boot log (rule 9).
  `podctl tunnel` forwards the machine's app ports too and prints the three kinds of link as named localhost URLs.
  Exercised on this Mac against fakes (the suite's fake RunPod REST and GraphQL, a raw-socket ComfyUI, fake pods);
  the console showing the names and the forwarder in front of a real ComfyUI are owed on a real pod.
- 3.8.2: the canvas size cache is compared only on the OS that photographed it. The frontend measures text with the
  system's fonts, so the package gate's Linux runner computed other sizes than the Mac that made `_build/sizes.json`
  (2026-10-16: 22 of the Character Creator's nodes off on Linux, 1 on the Mac, both on frontend 1.55.17), and
  `test_snapshot_sizes_cache_is_current` was red in CI for every package with a cache. `snapshot.py` now writes
  `platform` into the cache; the test skips, naming both, where it differs. A cache without the field predates 3.8.2
  and counts as the Mac's, where the layout tool runs. Exercised on this Mac; the Linux skip is CI's to show.
- 3.8.1: a new pod is PRIVATE unless `--public` (Paul, 2026-10-16: everything a person touches is as simple, then as
  secure, then as private as possible). `podctl create` on RunPod with no flag makes 3.7.4's private pod (an encrypted pod
  volume, `22/tcp` only, ComfyUI on loopback through `podctl tunnel`) on Secure Cloud, B200 first; `--public` is the one way
  to a network volume and proxy URLs, and `--volume` (a network volume, which RunPod cannot encrypt) is refused without it,
  naming both ways out, never silently made public. `--private` is still accepted and means the default; `--public
  --private` is refused. A host whose create has no private mode (Verda) is unchanged, and an explicit `--private` there is
  refused by name. The GPU settings need no flag either: B200/B300 by default, `GPU_PREFER` per package, the Sage mode per
  GPU (3.7.5). Exercised on this Mac against the suite's fake RunPod; not against the live API.
- 3.8.0: B200 and B300 are the defaults (Paul's decision of 2026-10-16: the video packages run on NVIDIA's datacenter
  Blackwell, optimised for it). `py/gpu_fit.py` is the one table (B200: compute capability 10.0, 180 GB; B300: 10.3,
  288 GB) and the one rule: a package declares `GPU_PREFER="B200 B300"` (§4; an unknown class is an invalid
  declaration), and a GPU of another architecture with less than 90 % of the smallest preferred memory is BELOW. The
  pod's run says so in its banner and its SUMMARY warnings (preflight and the real run share `base_init`), and
  `podctl install` asks the pod the same three numbers before it uploads and prints a WARNING; neither ever stops. A
  RunPod MIG slice (`NVIDIA B300 SXM6 AC MIG 1g.34gb`, 34 GB) is never a B300. `podctl create` on RunPod without
  `--gpu` asks the stock check for `NVIDIA B200`, then `NVIDIA B300 SXM6 AC`, and creates the first with stock where
  the pod must go (the `--location`, the `--volume`'s data centre, else the first that has it), naming each one's data
  centres when none fits; `allowedCudaVersions` is never below 13.0 without `--min-cuda` (the base installs a cu130
  torch whatever the image's tag says). The banner's torch line names every Blackwell arch the wheel carries (`sm_1xx`),
  not only `sm_12x`, so a B200's `sm_100` shows. The handbook (§1.1 "Which GPU") and `hosts/runpod/README.md` carry the
  RunPod facts read 2026-10-15/16: B200 stock in EU-RO-1, US-CA-2, US-NC-2, US-NE-1 and US-TX-6; B300 in EUR-IS-1, the
  one data centre with B300 that takes a network volume; a volume pins its data centre; the CUDA floor; and NVIDIA's
  encode matrix: 0 NVENC on HGX B200 and B300, so `h264_nvenc` fails there. Exercised on this Mac against the suite's
  fakes (fake RunPod, fake nvidia-smi, a fake pod); not on a B200 or a B300.
- 3.7.5: SageAttention is proven by RUNNING it. A B200 (sm_100, 2026-10-16) imported a correct sm_100a build and died
  at the first sampling step: upstream's `sageattn()` (KJ's "auto", ComfyUI's `--use-sage-attention`) dispatches
  sm80/86/89/90/120/121 only. Upstream withdrew SM100 in 2025-12 (#322) and SageAttention3 refuses sm_100 (#346).
  `py/sage_probe.py` calls every KJ mode on a tiny tensor against PyTorch attention; "Sage: OK" means `auto` runs,
  otherwise the run warns and names the mode that does (Triton first), and no mode at all fails the run. The proof
  is kept per GPU as `$VENV/.comfy-base-sage/<sm>.json` (and `base_sage_mode`), `sage` is the base's own command,
  hygiene drops `--use-sage-attention` where `auto` fails, and 10.3 (B300) builds the qattn kernels as sm_103a.
  Exercised on this Mac against fakes and a fake probe; not run on a B200 or B300.
- 3.7.4: `podctl create --private` (RunPod), for work that must not be reachable from a URL: an encrypted pod volume of
  `--data-gb` (RunPod encrypts only a pod's volume disk, never a network volume, so `--volume` is refused), `22/tcp` the
  only port (no proxy URL, no JupyterLab), and `BASE_LISTEN=127.0.0.1` in the pod's env so ComfyUI binds loopback;
  `podctl tunnel` is the way in, and `install` carries the pod's `BASE_LISTEN` into its runs (an ssh shell does not see
  the pod's env, so an install's restart would have bound 0.0.0.0). `volumeEncrypted` is in no documented create input,
  so the pod is read back: unencrypted is deleted at once and fails, an answer without the field is kept and fails.
  `status` says "encrypted pod volume", and "jupyter not exposed" for a pod without 8888/http. Exercised on this Mac
  against the suite's fake RunPod; the encryption field has not yet been sent to the live API.
- 3.7.3: `podctl create` on RunPod, the volume a pod really has, and `install --no-suite` / `--env`. A first pod on a
  fresh account needed the console or runpodctl, because RunPod had only `deploy --like <pod>`; `create` now checks
  stock first (GraphQL `dataCenters`, the only stock view RunPod has) and names every data centre where the GPU is free,
  stops before anything is created when the chosen one (or the `--volume`'s) has none, makes a network volume of
  `--data-gb` there or reuses `--volume`, and puts a Secure Cloud pod on it (the image, `8188/http 8888/http 22/tcp`,
  `PUBLIC_KEY`, container disk, `allowedCudaVersions` from `--min-cuda` or the image tag); a refused pod takes the
  volume it made with it. `status` printed "volume NONE" for a pod whose `/workspace` is a pod volume
  (`volumeInGb`), and every pod read now asks `includeNetworkVolume=true`, because the live openapi names only the
  `networkVolume` object and not the `networkVolumeId` older answers carried: either shape is read, a pod volume is
  named as one (and recorded for the disk gate), and `deploy` says why a pod volume cannot be cloned. `install
  --no-suite` sets `BASE_NO_SUITE=1` for the base step and every package run (it had to be done by hand over ssh), and
  `--env KEY=VALUE` passes any variable to every run on the machine, names printed, values never. Exercised on this
  Mac against the suite's fake RunPod; the stock query and both volume shapes were read from the live API (read-only).
- 3.7.2: no shell function is defined twice. `base.sh` sources `lib/[0-9][0-9]-*.sh` in order into one shell, so a name
  defined in two files is silently the later one: 00-env's §4 hook helper `base_run` was shadowed by 95-summary's
  install pipeline of the same name, and a hook that called it (`base_run cp …`, as §4 says) re-entered the whole
  install without end. The pipeline is now `base_install`; `base_run` is the dry-run aware helper §4 always described,
  so a hook written to the handbook is correct as written. 30-venv's one-line `_base_all_reqfiles`, always replaced
  by 40-packs's full one, is gone. The unit tier now fails on any name defined twice in one shell (`base.sh` + the
  numbered lib files; `boot.sh` alone), and checks that `base_run` runs its command, or prints it under `--check`,
  after the whole library is sourced. Exercised on this Mac (unit tier and `verify.sh`); not on a pod.
- 3.7.1: the testbed's Python is the newest torch ships for. uv listed CPython 3.15 on 2026-10-15 before torch had a
  cp315 wheel, and `testbed.sh --server` took the newest listed minor and died in the torch install ("No solution
  found"). The minors are now tried newest first and the first one the torch family resolves on wins (a cached
  `uv pip compile`, no install); offline, the venv's own Python is kept. A venv whose python does not run (a build
  that died, an interpreter uv removed, a dangling `bin/python` that `-x` could not see) is removed before the
  rebuild instead of making `uv venv` refuse with "A virtual environment already exists". The pod side already had
  this rule since 3.6.1 (`_base_python_pick`). Exercised on this Mac against the real uv (3.15 rejected, 3.14 picked)
  and on temp trees with a uv stand-in; not on a pod (the pod path is unchanged).
- 3.7.0: `podctl` reads the workspace shape too. 3.6.0 taught every package walker the workspace and left two readers
  on the submodule shape, as its record said: `podctl install --pkg <name>` from a workspace stopped with "not inside a
  brand repository", and the default provider ignored the workspace's `brand.toml` and fell back to runpod. Both now ask
  `py/pkgdirs.py` for the consumer root (`$BASE_WORKSPACE`, then the submodule shape): `--pkg <name>` resolves to
  `<workspace>/<name>`, and the provider comes from the brand at the workspace root (`brands_at`: the root's own
  `brand.toml`, else every `<root>/<brand>/brand.toml`). A `$BASE_WORKSPACE` that does not exist is an error, as in every
  other walker. The workspace folder is `<name>/`, not `<brand>-<name>/` as 3.6.0's prose said: the rule needs the
  folder's name to be the script's stem, and the first consumer (SoulCraft, one repository per workflow under
  SoulCraft-Org) clones each package as `<workspace>/<name>/`. New: `test_unit_podctl_reads_the_workspace_shape_for_pkg_and_provider`.
  Exercised on temp trees only. Also: the install tier's "a second run is a no-op" checks were red on `main` since
  3.5.0, because 3.5.0's size cache (`state/sizes.tsv`, a directory's size by path and ctime) may gain a row on the
  second run; like `logs/` it is a runtime record, not the store, so every no-op check now skips it (`basetest.tree_hash`).
- 3.6.1: a pack row may name the branch to track. rgthree/rgthree-comfy's GitHub default branch now points at a stale
  `dev` (5f940dd, 2023-11-20) while its work is on `main` (2026-10-15 finding, exporting the Video Replacer). Every
  install follows the remote's HEAD, so a fresh pod or testbed got 2023 rgthree, which does not import on ComfyUI
  v0.41.x (`execution` has no `recursive_output_delete_if_changed`) and has no Fast Groups Bypasser: every package
  using rgthree broke. An optional sixth field, `dir|url|sha|cnr_id|why|branch`, names the branch instead (the
  `url#branch` form was rejected: the consumer's comfybuild reads `url` as a GitHub repository and dockerize compares
  it as one). The rgthree base row tracks `main`. `_base_pack_sync` widens a single-branch clone's refspec to the branch
  and moves a checkout on another branch across; `py/pack_heads.py` answers `url#branch` keys with that branch's tip;
  the clone, the dry-run plan, the printed records (podctl writes the row back whole), `base.sh latest`, `list-packs`
  (now `dir|url|sha|branch`) and `testbed.sh` (new `--packs`: the node packs alone) honour it. Unit tests run against
  local remotes whose HEAD is a stale `dev`; the install tier runs it through `base_main`. Not yet run on a pod.
  Also, because the gate was red on 3.6.0 the same day: uv started listing CPython 3.15 before torch had a cp315 wheel,
  and with nothing else to resolve against `_base_python_pick` took the newest minor uv listed, so the torch pick (and
  every dry run on a new pod) failed; the backend pre-pick asked 3.15 only. Both now take the newest minor the torch
  family resolves on, the rule the pick already followed when there were requirements to resolve. `testbed.sh
  --server` still picks the newest minor uv lists (a follow-up).

- 3.6.0: packages are found in the workspace shape too. Every cross-package walker assumed a consumer with the base as
  a submodule at `base/comfyui-base` and its packages under `<brand>/packages/<name>/`; the consumers are moving to one
  repository per package, cloned side by side as `<brand>-<name>/` under a workspace, with the base read from
  `$COMFY_BASE`. `py/pkgdirs.py` holds the one rule (a directory holding `<its name>-script.sh`, in either shape,
  deduplicated by resolved path) and the one root resolution (`--root`, then `$BASE_WORKSPACE`, then the
  submodule shape, then none); `package.py --all` (new `--root`), podctl's pin scan, `verify.sh`, `testbed.sh` and
  `basetest.converted_packages` use it instead of five `*/packages/*` globs with five guards. The pin scan now reads
  the base's own `lib/40-packs.sh` beside the driver, not `<root>/base/comfyui-base/lib/`, which a workspace does not
  have, so a standalone base (no consumer root) now measures its own rows where it used to report nothing: intended,
  those rows are the base's. `base_test` hands `BASE_WORKSPACE` to the suite when set, and the unit tier's
  cross-package checks walk the consumer root it resolves (skipping when there is none, the 2.2.0 standalone guard). A
  named root that does not exist is an error everywhere, and `verify.sh` / `testbed.sh` stop when `py/pkgdirs.py` is
  missing or fails instead of going green over zero packages. `podctl --pkg` resolution and the provider default
  still read the submodule shape. Exercised on temp trees only.

- 3.5.0: every probe once, and in parallel where it can be. The pack HEADs are asked of every remote at once
  (`py/pack_heads.py`, one `git ls-remote --symref` each, sixteen at a time), and a checkout already on its remote's
  HEAD (same commit, on that branch, wired to it, not shallow, no edits) is not fetched; anything else takes the full
  path, never backwards. ComfyUI asks the remote for its newest release tag first and skips the fetch of every branch
  when HEAD is already on it. apt runs in the background beside those network checks and is joined before the venv
  snapshot, where the driver gate and "restart required" stop as before; its output is shown whole at the join. The two
  filesystem walks skip the uv, pip, Hugging Face and xet caches (`.comfy-base-staging` stays pruned), a directory's
  name is read without a `basename` process per line, a backup venv is sized once per path and ctime (`state/sizes.tsv`),
  the venv's torch backend comes from torch's metadata without importing it (a torch with no local tag is still asked
  directly), and the cuBLAS probe compiles once per toolkit on a machine. Runtime mode keeps the OS step skipped and its
  torch import. The README and §9.1 now say what Comfy-Org's tooling actually does (skills, not MCP client
  configuration; the marketplace plugin is Comfy Cloud's) and why the repository still ships no `.mcp.json` and no
  approval for one. Exercised on fake pods only.

- 3.4.0: each model file's sha256, from the Hub, never hashed here (Paul, 2026-09-25, asked for by the ComfyUI tooling
  session so a Comfy Build spec needs no second pass over 100+ GB). `base.sh gen-hashes <pkg dir>` reads the sha256 the Hub
  publishes for every LFS file on a HEAD and writes `models.sha256` beside the script; `--check` compares. The MODELS rows
  are unchanged (no new column for every package to carry): a package opts in by adding the file. It ships in the zip,
  the declarations dump prints `MODELHASH` rows, and the workflow's `models` array gains `hash` + `hash_type` for ComfyUI's
  Missing Models dialog. Rows the Hub does not serve (LOCAL, a repo snapshot, a GitHub asset) are left out and said.

- 3.3.0: every Python resolve once, and a force is never undone. Traced on 3.2.1: `base_packs pip` installed the derived
  set WITHOUT the overrides, so each package the newest pass had forced past a cap (protobuf 7 over its <6 caps) went back
  down under it on the next run and was forced up again: two whole resolves and a reinstall, every run; a force its holder
  refused (huggingface-hub 2 under transformers) was installed, probed and taken out again every run; and the newest pass
  resolved the very set the pip half had just installed. Now forces are carried as floors (`name>=version`, never a pin),
  every resolve applies them (a round's own force wins its name; floors that no longer resolve are dropped and the install
  runs without them), a refusal is remembered until either version moves, an identical set is not resolved twice in one run,
  and the outdated scan runs again only after something was installed. Forward repairs: Python packages that moved are a
  reason to restart (the live server had kept the old ones loaded); a pack failing on a module nothing declared gets it at
  its newest from a reviewed map (`py/modmap.py`, never a name taken from an error) and is judged again; a reused venv never
  moves torch backwards; comfy-mcp is not installed a second time; SageAttention's cache key names torch's CUDA backend,
  and a build stamped under the old key for the same venv is adopted rather than rebuilt. A runtime machine is untouched
  (no pip, no restart reason). Exercised on fake pods only.

- 3.2.1: a package may write into its own pack, and the mirror keeps what it writes. Qwen 2.1 fetches its rewriter
  prompts into its own pack in `pkg_post_models` (they cannot ship in the zip); they survive because "unchanged" compares
  the zip's copy with the stamp, never with the live folder, and because the mirror runs before the models hooks. Found
  by the Qwen 2.1 session reading 3.2.0; now said in `lib/62-own-packs.sh` and held by two tests. No behaviour change.

- 3.2.0: an install does each piece of work once, and says what each stage cost. Paul, 2026-09-25: a two-file change to
  a package's own node pack took a full install (about 10 minutes), and he asked for installs that always take everything
  to its newest while doing only the work that changed. Traced from the code and the consoles of 2026-09-24/25, nothing
  was ever reinstalled: the minutes were work done twice. `podctl install --base --pkg` ran two whole pipelines, the
  base's step one (with its restart and a 75 to 82 s suite) and then the package's, which repeats every stage of it; now
  the base is installed and the package's run is the one pipeline, with the base suite once per base version. podctl's
  full `--check` pass before each run is now `preflight`, seconds long (a machine whose base predates it gets `--check`
  as before), and a dry run no longer runs the suite. A package's own node packs (`VENDORED_PACKS`) are the base's to
  install: mirrored from the zip, untouched when unchanged, newest version first on a shared store, and put back when
  they fail to import; the import check loads only them when nothing else changed. Every stage is timed. The buyer flow
  takes the same preflight and single pipeline (no base suite there, `BASE_NO_SUITE=1`). Where own packs live was
  decided with Paul the same day: in the zip (buyers' machines have no git and no GitHub credentials, the published
  runtime must never carry them, and Comfy's own build tool takes a local folder as a pack's source). Exercised on fake
  pods only; no live host yet.

- 3.1.1: a runtime never carries a package's vendored packs. `runtime-capture` archived everything under
  `custom_nodes/`, which on a machine holds each package's own plain-folder packs beside the git checkouts; a runtime
  meant for publishing would have carried that code. Only a git checkout is runtime now (the manifest's `packs` were
  already exactly those); a vendored pack arrives with its package's zip on every install, which is also why an
  Update (a newer `runtime-apply`) is followed by the packages' installs.

- 3.1.0: a buyer's machine, opt-in end to end, with every plain run unchanged. A proven runtime: `runtime-capture` on
  a green machine (parts under GitHub's 2 GiB and `runtime.json`, `py/runtime.py`), `runtime-apply` on a buyer's, and
  `BASE_RUNTIME` installing from it with no OS, git, pip or uv step (§0.3's one exception, §0.12). The first flow
  first: `MODELS_LATER` and `BASE_STAGED=1` stand in for the rest, which a detached `base.sh fetch-later` brings behind
  the started server while `state/progress.json` reports each file. `BASE_FETCH_JOBS` fetches several files at once
  (default 1, the old serial loop, same output). Verda: `balance`, `availability`, `create_machine` (the data disk,
  then the instance with its own OS disk, no startup script; a refused instance takes its new disk with it),
  `machine_volumes`, `delete_volumes`. podctl: `balance`, `availability`, `create`, `pause`, `put-away`,
  `buyer-install` (zips alone: no packager check, no Mac gate, no pack records written), `progress`,
  `runtime-capture`; `install()` takes a run environment and zip-born items. Each run keeps its verdict in
  `state/last-run/<id>`, which is what capture reads.
  ComfyUI's environment carries `COMFY_BASE_STATE` (the base's state dir), so a package's own route finds
  `progress.json` without guessing. Exercised by the suite only (macOS, Python 3.12 and 3.14); not yet on a live
  machine.

- 3.0.3: renders stay on the store, and a machine runs one ComfyUI. Both measured on a Verda machine on a shared store
  (`BASE_VOLUME_SHARED=1`). First, `comfyui_args.txt` still named `--output-directory` and `--input-directory` on the
  old library mount: 2.5.2 stopped using a library under a shared root, but the hygiene only ever ensured those flags
  and never took them back, so ComfyUI made the old mount point on the OS disk and wrote every render and upload there.
  Without a library the hygiene now removes a value that is not on the store (a package's own `HYGIENE_ARGS` choice
  stands), and the launch line, which the boot also uses, leaves off one that sits on the OS disk while the store is a
  mount of its own, and never creates it. Second, `BASE_RESTART=1` started ComfyUI detached (ppid 1), outside
  `comfy-base-boot.service`'s cgroup, so a later `systemctl restart comfy-base-boot` started a second one beside it (the
  old one kept :8188 and 34 GB of VRAM). `_base_restart_comfy` is now the one way to restart: through the unit where it
  is enabled and the caller is not inside it, directly elsewhere; and `boot_comfy` stops any ComfyUI it did not start
  before starting its own. The process matcher moved to `lib/85-launch.sh`, which gives the boot's wait the liveness
  check it lacked (before, it declared a healthy start dead after 2 s). Exercised by the suite only (macOS and Python
  3.14); not yet on a live machine.

- 3.0.2: every `HTTPError` the host providers and `py/smoke.py` read is closed. 3.0.0's test runner takes the newest
  Python uv has instead of 3.12, and on Python 3.14 an HTTPError that is read but never closed is a `ResourceWarning`
  when it is collected; with warnings as errors, the repository's CI went red on 3.0.0 (13 failures, all this one
  cause, each reported against whichever test was running when it was collected). The suite passes under 3.14 now.

- 3.0.1: a SageAttention build for a list of architectures serves each GPU in the list. An image built with no GPU
  stamps its build for `SAGE_ARCHS="9.0;12.0"`, and the machine it serves computed its key from `sm_120`, so the two
  never matched and every first install rebuilt SageAttention (10-20 minutes) from the very source, Python and torch the
  image already carried. The stamp, and a cached wheel, now count as current when they are the same commit, Python and
  torch and their list holds this GPU's sm; a GPU the list does not hold still builds. Measured by the session that
  adapts a baked-image consumer to 3.0.0.

- 3.0.0: always the newest, for everything the base installs, on every run and every host (§0.3). ComfyUI stays on its
  newest release, with `COMFY_REF` (`podctl install --comfy-ref`) as the opt-in for something newer; a pack is at its
  remote's HEAD on every run (a row's sha is the last-tested record); every Python package is at its newest through
  `py/reqlift.py` and uv's overrides (measured on 2026-09-25: ComfyUI v0.37.2 plus 27 pack requirement files resolve on
  Python 3.14, the frontend lifted from 1.52.7 to 1.54.7, protobuf forced from 5.29.6 to 7.36.2 past a cap that
  `google-generativeai` 0.8.6 still imports under); torch follows the newest CUDA build the driver runs through uv's
  `--torch-backend=auto`; the OS and the NVIDIA driver (NVIDIA's `nvidia-open`, a one-time switch from another family in
  a single transaction) are upgraded first, and a new driver stops the run before the GPU is used, asking for a restart a
  person approves. The Verda startup script registered on an account is replaced when it differs (`ensure`) and never
  attached when stale. RETIRED, each because it held something below its newest: `BASE_PINNED`, `COMFY_TAG`,
  `BASE_LATEST` (`--latest` is a plain run), `SAGE_WHEEL`, a 40-hex `SAGE_REF`, `BASE_FORCE_SELF`, `podctl --no-latest`.
  A baked-image consumer that set `BASE_PINNED`/`COMFY_TAG` now gets a machine that upgrades at its first install.
  `podctl install` no longer rewrites a shared-root machine's `host.env` as a library machine (it read an attached
  shared volume as a library and dropped `BASE_VOLUME_SHARED=1`): the Verda provider asks the machine what `/workspace` is.
  The rehearsal's fake machine now runs the package's own `COMFY_MIN` (it was always 0.34.7) and carries the package's
  vendored node packs (it copied only top-level files), both measured by a consumer whose suite failed only there.
  2.12.3's `_base_venv_reconcile` is superseded: nothing installs an upstream file one at a time any more (the overshoot
  it repaired cannot happen in one resolve), and re-resolving `pip check` conflicts would undo a deliberate force; a
  force its holder refuses at import (transformers with huggingface-hub 2.0.0) is taken back by `py/holders.py` instead.

- 2.12.3: the venv's requirements are made to agree after every pip half, at their newest. On a Verda machine with a
  reused venv, `--latest` ended at the import check: `transformers` 5.17.0, the newest release, requires
  `huggingface-hub<2.0`, and the per-pack loop had taken the hub to 2.0.0. `pip install --upgrade -r <file>` always
  takes a requirement the file names to its newest, several packs name a bare `huggingface_hub`, and pip does not
  resolve against what is installed outside the request; it only warns. `_base_venv_reconcile` now runs after the
  per-pack loop (every run, reuse included) and after the build's installs: `pip check`, then both sides of every
  conflict in ONE `pip install --upgrade` under the torch constraint, so the resolver lands on the newest set that
  agrees (that night, transformers 5.17.0 with huggingface-hub 1.33.0). No pin is written. A conflict pip cannot
  resolve is reported and left to the import check, which stays the gate. Four unit tests feed it canned
  `pip check` text.
- 2.12.2: `comfyui-base.zip` carries `LICENSE`. It never did: the packager's member list named five files and the
  licence was not one of them, so every copy of the zip went out without the notice MIT asks to travel with it.
  The zip is exactly the copy a project built on the base hands to its own users (a workflow package's buyers
  install it as step one), so the gap reached every one of them. `suite/test_unit.py` asserts the notice is in
  the extracted zip, beside the manifest check that already proves every shipped file is listed. Step one's
  copy list (`lib/95-summary.sh`) had to learn the file too, or the install failed its own manifest check; a new
  test holds the packager's members and the installer's list together, since they are kept by hand in two places.
  Also: `suite/test_podctl.py`'s fake-RunPod CLI call inherited the session's `PODCTL_*`, so a workspace exporting
  `PODCTL_PROVIDER=verda` sent it, authenticated, to the LIVE Verda API. It strips them now; a test reaches only
  its own fake server.

- 2.12.1: a security review of 2.12.0, and it found two real things, both in the same eight lines. The Jupyter
  token was interpolated into the ssh COMMAND, and sshd runs a non-login remote command as `bash -c '<cmd>'`, so
  it landed in the machine's `/proc/<pid>/cmdline`, world-readable on Linux, and in `ps` on the Mac. The code
  then wrote the file 0600, which protects the value at rest while having published it in the process table on
  the way there. `lib/boot.sh` states the rule for this exact secret and `suite/test_unit.py` asserts it: the
  token rides the environment, never argv. It goes on stdin now, and `ssh_run` grew an `input` parameter to make
  that possible for any secret.
  The same line also passed the drop-in body as printf's FORMAT string. MEASURED: a token of `ab%sc` was written
  as `abc`, `100%done` as `1000one`, and `tok%` truncated the file. printf failing inside a pipeline leaves the
  pipeline's status as tee's, so the `&&` chain carried on and `ensure` reported the token written while the
  machine held a different one. There is no attacker here, the token is the operator's own, but the failure was
  silent and that is worse than loud.
  Also hardened while in there: a public address from the API is parsed with `ipaddress.ip_address()` before it
  reaches the operator's `~/.ssh/config`, where a newline would have become an ssh directive. That needs the API
  to lie, which is a high bar, but it is one line.
  And the signing test stopped being circular before the review reached it: it recomputed the expected signature
  with the same formula the code uses, which proves only that the code agrees with itself. Two golden vectors are
  pinned as literals now, with an assertion that at least one of them contains a `-` or `_`, because standard and
  url-safe base64 differ in exactly two characters and a signature containing neither cannot tell them apart.

- 2.12.0: Crusoe has a driver. `hosts/crusoe/provider.py` was 144 lines in which every method raised, so of the
  four hosts it was the only one actually broken: the machine side has been complete for a while, but nothing on
  a Mac could find, reach or move a VM. It is a signed REST client now with the whole Provider interface behind
  it. The signing was the part worth getting right and the part with no live 200 to check against: the prose
  docs' own worked example does not reproduce under any reading, so it is built from two of Crusoe's own
  implementations instead, which do agree (client-go auth/v1/auth.go and the Python in
  crusoe-registry-token-rotator), and the suite asserts it against that formula rather than against a constant.
  The fake API verifies every signature, so a provider that stops signing fails here rather than in production.
  Three things are not Verda and shape the code. `stop` is a real stop, because a stopped Crusoe VM keeps its
  disks and its id and bills the disks only, where a shut-down Verda instance still bills the GPU and is
  therefore deleted. The public ip is dynamic, so every lifecycle call rewrites the Host block rather than only
  the first. And the startup script is a create-time field that cannot be changed through the API and runs on
  every boot, so `ensure` ships the current `hosts/crusoe/startup.sh` over ssh and re-runs it, which is the only
  way to move an existing machine forward. `deploy` detaches the disk before creating the clone and passes it in
  the create body rather than attaching afterwards, because a VM that boots without its disk sends the startup
  script into a 15 minute wait for one. `ensure` also writes the JUPYTER_TOKEN systemd drop-in, without which
  `podctl jupyter` reports no token on this host forever and reads as a broken command.
  Two defects in the stub went with it: `record_volume` took no `env` keyword while `PodIO.upload` passes one,
  which was a TypeError escaping the PodctlError handler as a traceback rather than a diagnosis; and a lifecycle
  call that answered FAILED immediately was polled 120 times anyway instead of failing at once.
  `experimental` stays True. Implementing the methods does not earn its removal, a live account does, and this
  is exactly where Verda sat before its first live run corrected three things the documentation implied.

- 2.11.1: the repository names no brand but its own maintainer. The runtime was already clean and a foreign
  brand already built and gated green, but `CLAUDE.md` named two consumer repositories, three Record entries
  named a foreign brand, two machine aliases and a workflow product, and two test comments named a consumer's
  layout and a package. None of it was load-bearing and all of it told a reader this was somebody's private
  tool. Only the attribution survives, in `LICENSE` and the README. The brand-guard test changed shape too: it
  used to forbid a list of two organisation names, which meant citing the very organisations the rule exists to
  keep out, and would have gone stale as consumers change. It asserts the invariant now - every `packages/` glob
  in `testbed.sh` is anchored on a variable, never on a literal directory - which is what the original leak
  violated, catches any future one, and names nobody. Verified against the 2.6.0 leak verbatim and a synthetic
  new brand: both caught; variable-anchored globs and comments: clean.
  The README also says what this is for someone who will never write a package: ComfyUI at its newest release
  tag plus six pinned packs including ComfyUI-Manager, no model weights, bring any workflow. That is what most
  people want from it, and the front page led with the package contract instead.

- 2.11.0: the driver stops knowing one brand's machines. `TUNNEL_LOCAL` was a hard-coded map of three
  `verda-*` aliases belonging to the repository's own maintainer, and `tunnel_locals` read a machine's port
  offset out of it. Everybody else's fleet fell through to 0, so their second machine silently shared 8188
  with their first, and the only way to fix it was to edit the base - the one thing a consumer repository must
  never do. The offset is a fact about somebody's fleet, not about the base or the host, so it comes from
  `$PODCTL_TUNNEL_OFFSET` now, beside the `PODCTL_HOST` that already names the alias, and defaults to 0 so a
  single machine needs nothing. A value that is not a whole number in 0-1000 is refused rather than read as 0,
  because 0 is exactly the collision it was set to avoid. Found by building a foreign brand from scratch, with
  its own `brand.toml` shipping to a different host, and checking what the base still assumed: everything else
  passed, including the runtime in `lib/` and `py/`, which carries no brand name at all.
  **Migration:** a fleet that relied on the old map must now export the offset per machine. The alias that was
  the second machine takes `PODCTL_TUNNEL_OFFSET=1`, the third `2`; everything else was already 0.

- 2.10.1: the front page stops calling a proven host experimental. 2.5.3 promoted Verda after live runs on
  2026-09-17 and 2026-09-18: it set `experimental = False`, rewrote `hosts/verda/README.md` around what those
  runs answered, and recorded the change here. It did not touch the four other places that still said
  otherwise - this handbook's own host table, its 1.2 heading and opening line, `hosts/README.md`, and the
  top-level README in three spots, including the status table a visitor reads first and the sentence naming
  Verda an open item. So for a day the repository advertised a working host as unproven, which is the one
  direction a status claim should never be wrong in: overselling gets found out, underselling just loses
  people. All corrected against the provider flag, which was right the whole time.

- 2.10.0: the MCP client configuration stops being this repository's to own. 2.7.0 committed a `.mcp.json` at
  the root, which Claude Code and Cursor read automatically: every person who cloned the repository was
  prompted to enable a `comfy` server that could not work until they had comfy-mcp installed and a tunnel up.
  Researched rather than guessed, and the answer was upstream's: `comfy skills install` writes six skills
  across Claude Code, Cursor and `AGENTS.md` at user scope, with install / uninstall / status / validate, and
  `Comfy-Org/comfy-skills` describes itself as the single source of truth for the installer and the MCP
  server. comfy-mcp shipped 0.9.0 and 0.10.0 within two months. Competing with that would have drifted within
  weeks. The local case is upstream's now and the README says so; what stays is the case their tooling does
  not cover, because its local flow assumes ComfyUI is on your machine and its cloud flow assumes Comfy Cloud:
  a machine the base provisioned on a rented GPU. `podctl mcp` still writes the file, and it is gitignored,
  because it names one machine and one operator's paths. Also, two more places still resolved the testbed at
  the pre-2.6.0 path only: `suite/test_comfyui.py`, whose fallback made the whole tier skip under a bare
  `pytest suite/` in a standalone clone, and `gate()` in podctl. Both look in the base first and beside it
  second now, as `_base_use_testbed` does. That is the third instance of the same 2.6.0 oversight; the moral
  is that moving a file one level up means auditing every path expressed relative to it, not just the ones a
  test happens to cover.

- 2.9.2: the testbed finds a consumer repository's packages again. 2.6.0 moved `testbed.sh` into the base,
  which changed `$HERE` by one level and silently broke the glob that locates the packages:
  `$HERE/../*/packages/*/` from `<repo>/base/comfyui-base` resolves to `<repo>/base/*/packages/*/` and matches
  nothing. MEASURED on a reconstructed consumer layout: zero matches, so the testbed would have provisioned the
  base's own packs and none of the package's, and the `comfyui` tier would have run against a tree missing the
  very packs it exists to exercise - green, and meaningless. Exactly the failure `BASE_SH` was fixed for in
  2.6.0; its sibling went unnoticed because a standalone base has no packages either way, and that is the only
  shape the suite could see. The root is searched for now, one level up then two, and a candidate counts only
  when it holds a `<name>/<name>-script.sh` - two levels up from a standalone checkout is an unrelated
  directory. `--status` also prints what it derived and where from, because silent derivation is how this
  shipped. Proven on all three layouts: base-as-submodule, testbed.sh-beside-the-base, and standalone.

- 2.9.1: the forwarded ports say why they are forwarded. `TUNNEL_SERVICES` carries 9199 and 11434 next to
  ComfyUI's 8188 and JupyterLab's 8888, and the comment beside them explained only the per-alias offset (a
  collision measured on two live machines), never why those two ports were in the list at all. Read cold they
  look like leftovers, and this session came close to deleting 11434 as one. They are not: the base runs no
  metrics exporter and no inference server, but a PACKAGE may run one on the machine and talk to it on
  loopback, which is the shape a package takes when its whole point is that nothing leaves the box. Removing
  the forward would break such a package silently, from the Mac side, with nothing in the log to say why.
  Documented in both places, so the next reader does not have to rediscover it.

- 2.9.0: SAGE_REF can finally do what it was documented to do. It is described as the way to pin
  SageAttention, but the clone used `--depth 1 --branch "$SAGE_REF"`, and `--branch` takes a branch or a tag
  and refuses a commit: MEASURED, `git clone --depth 1 --branch <40-hex>` answers "Remote branch <sha> not
  found in upstream origin", so passing a commit failed at the clone for as long as the knob has existed. A
  commit is fetched by object name now, and it also short-circuits the `ls-remote` that resolves the cache
  key, which lists refs and would have resolved a commit to nothing. The DEFAULT stays `main`, deliberately:
  the wheel cache is keyed on the resolved upstream commit so that tracking upstream costs nothing, and a
  version-keyed cache would be exactly the stale pin the comment above that key refuses. Reproducibility is
  now available to anyone who wants it without making everyone else carry a pin. Also: the RunPod fixture in
  `suite/test_podctl.py` had a real-looking `machineId` and `dataCenterId` among its obviously-fake
  `fakevol001` and `faketmpl02`; both are fake now.

- 2.8.1: the MCP configuration names the `comfy` binary. comfy-mcp is a wrapper whose discovery and lifecycle
  tools shell out to comfy-cli, found through `COMFY_BIN` or `PATH`, and an MCP client is usually launched by a
  GUI whose `PATH` is not a shell's. 2.7.0 shipped neither transport with `COMFY_BIN` set, which left the ssh
  form giving an absolute path to `comfy-mcp` and then hoping the machine's `PATH` had `comfy`. MEASURED
  2026-09-19, driving the real stdio protocol against the testbed's ComfyUI: initialize returned protocol
  2025-06-18 and 39 tools listed either way, but `server_info` answered "Error executing tool server_info"
  without `COMFY_BIN` and answered properly with it. The unit tests could not have found this; they assert the
  shape of a config, and the shape was fine. Over ssh the binary beside `comfy-mcp` is named; over the tunnel
  this machine's is, when `comfy` can be found, and the command says so when it cannot rather than writing a
  config that will fail later.

- 2.8.0: the owned-box recipe says what a Blackwell card actually needs. `hosts/local/README.md` now sets out the
  chain the whole host hangs on — compute capability 12.0 becomes `sm_120`, `sm_120` means the CUDA 13 wheels are
  the only stable ones, CUDA 13 means driver >= 580, and torch is therefore the newest `+cu130` wheel for the
  interpreter's tag rather than a pinned number — with the file each link lives in, so a refusal can be read
  instead of guessed at. It also says why SageAttention is built from source here (no PyPI wheel carries
  `sm_100`/`sm_120` kernels) and what to pass when the card is not visible at build time. The MCP shape for a box
  with no pod and no ssh alias is written out, since 2.7.0's `podctl mcp` has nothing to point at locally. The row
  stays honest: still a recipe, and the host-report template is there for the first person to run it end to end.

- 2.7.0: the machine is drivable by an agent. `lib/45-mcp.sh` installs `comfy-cli` and `comfy-mcp` into the
  run's venv and binds comfy-cli to the discovered tree; `podctl mcp <machine>` writes the `.mcp.json`, over
  ssh by default and over the tunnel on request. The transport matters more than it looks: comfy-mcp's
  lifecycle and discovery tools only work where the tree is, so a tunnel-only client can run a workflow and
  little else, which is rarely what anyone wanted. §9.1 has the table. Telemetry was the one real obstacle —
  comfy-cli depends on mixpanel and posthog, rule 9 says nothing is uploaded, and a wheel's behaviour is
  invisible to the outbound-host test because it is not our source; both kill switches are exported in the
  stage and in the boot environment, `comfy tracking disable` writes the config too, and the suite asserts it
  so it cannot regress quietly. MEASURED 2026-09-19, the stage run against a real venv: comfy-cli 1.20.0 and
  comfy-mcp 0.10.0 installed, the workspace bound, and torch 2.14.0 / torchvision 0.29.0 / torchaudio 2.11.0
  unchanged afterwards — none of comfy-cli's 29 dependencies is torch, torchvision, torchaudio or numpy, and
  the constraints file is passed anyway. That run also settled how to install: `$PY -m pip` works on a pod
  because 30-venv.sh seeds the venv, but a venv built without `--seed` has no pip at all, and uv built the
  venv in the first place — so the stage uses uv and stops depending on a detail it does not control.
  `BASE_MCP=0` skips the whole stage, which never fails a run.

- 2.6.0: the testbed the base tests itself with now lives in the base. `testbed.sh` was carried by the brand
  repositories, so `suite/test_unit.py` skipped the `comfyui` tier by name — "this base is its own repository
  (2.2.0); the brand repositories carry one" — and a clone of THIS repository could never run it at all. That is a
  poor thing to hand someone who forked the repository in order to work on it, and it is the whole tier that
  proves a pack actually imports. `testbed.sh` ships here now; `_base_use_testbed` and `verify.sh` check
  `<base>/testbed` first and `base/testbed` second, taking one root whole so the port file always comes from the
  same place as the tree. Two things had to be fixed on the way in. `BASE_SH` was hard-coded to
  `$HERE/comfyui-base/base.sh`, which from the repository root resolves to nothing: MEASURED, the script then
  printed "no ComfyUI Base beside this script — only EXTRA packs are provisioned" and, since EXTRA has been empty
  since 2026-09-04, would have provisioned a ComfyUI with no custom nodes at all, silently. And `VENDORED` was
  three hard-coded `../<brand>/packages/...` paths — a brand's name in a repository whose first design rule is
  that nothing here knows one, and a list that had already gone stale. Both are derived now, and a test asserts no
  brand is named. `testbed.sh` is deliberately NOT in `BASE_MEMBERS`: the zip is what goes to a pod, and a CPU
  testbed has no business there.

- 2.5.14: the suite runs without a RunPod credential. The three `test_podio_fetch_*` tests drive `PodIO.fetch`
  against a fake ssh and never reach the API, but `PodIO(None, …)` builds a `RunPodProvider`, whose constructor
  loads the key eagerly (`Api(load_api_key())`), and `load_api_key` falls back to `~/.runpod/config.toml`. On a
  maintainer's machine that file exists, so the tests passed; anywhere without a RunPod account they raised
  before the first assertion. MEASURED 2026-09-19 on a GitHub Actions ubuntu-24.04 runner: 3 failed, 293 passed,
  15 skipped — the first run of the new CI job, which is exactly the class of bug a gate that only ever runs on
  the author's machine cannot find. The tests now put a placeholder key in their own environment. Nothing about
  auth is asserted by them, so nothing is being pretended; the credential is scaffolding for a constructor.

- 2.5.13: a `models/` symlink left pointing outside the volume is repointed at it. `_base_library_link` wrote
  `$COMFY/models -> $BASE_LIBRARY/models` when a machine had a separate library mounted; that symlink lives ON THE
  STORE, so every later machine inherits it, and since 2.5.2 `BASE_LIBRARY` is correctly ignored when the whole
  root IS the store, which means the function that created it returns at its first line and can never repair it.
  A second mount of the same store hid this until the release that stopped making one. MEASURED 2026-09-19 on a
  fresh boot: the link dangled, `df` on it failed, the disk gate read 0.00 GB free on a store with 272 GB free and
  refused every install, and ComfyUI's own models directory did not resolve. Repaired in `base_discover`, before
  `M` is chosen, rather than in hygiene, because hygiene runs after the model step this breaks.
- 2.5.12: the deletion is staged and reversible. Every guard before it is an INFERENCE about identity, and 2.5.11
  fixed one that was wrong only after it had offered 63.52 GB of live models; an inference can be wrong again in a
  shape nobody has met, and the consequence is a model that exists nowhere while the ledger still calls it
  installed. So `base_prune` now RENAMES each candidate aside in its own directory (atomic, never across a
  filesystem), re-stats every destination the MODELS table declares, and removes nothing unless all of them are
  still present at their declared size. If one vanished or changed size, every rename is undone, the step fails and
  says which model it was. It needs to know nothing about mounts, inodes or symlinks to be safe, because it checks
  the thing that actually matters rather than inferring it.
- 2.5.11: a file reached by two paths is not a duplicate of itself. `base_prune` offered 63.52 GB of LIVE models
  for deletion on a three-machine store, measured 2026-09-19: the store was mounted at two points and
  `ComfyUI/models` symlinked into the second, so every file was indexed under two path strings, while the
  predicate is basename plus size and the identity check compared strings that `cd && pwd -P` had only collapsed
  SYMLINKS in. `rm -f` unlinks the one directory entry, so the model would have gone from both paths. Now a `-ef`
  test (dev:ino) against the destination and between candidates, so a second mountpoint or a hardlink can never
  reach the deletion list or inflate "reclaimable"; and under `BASE_VOLUME_SHARED` the duplicate sweep is skipped
  outright, because a run that cannot see the other machines' ledgers cannot decide what is surplus on a store
  they share. `BASE_YES=1` removes the prompt rather than answering it, which is what made this a silent
  unattended deletion in a baked image rather than a footgun.
- 2.5.10: the Mac gate works again. 2.5.7's `test_unit_each_workflow_gets_its_own_local_port` loaded
  `_build/pod/podctl.py` with its own inline import instead of the `_load()` helper beside it, and that helper
  is where the "not a repo checkout" skip lives. The shipped zip contains no `_build/`, so the test raised
  FileNotFoundError in every run from the extracted zip, which is exactly the run `podctl install` performs
  before it uploads anything. Every session's install stopped at the gate, for a reason that had nothing to do
  with what it was installing. Found while installing a package 2026-09-19.
- 2.5.9: a video package's last node no longer dies on the encoder it asked for. VideoHelperSuite prefers its
  own bundled imageio_ffmpeg binary, and that build carries NO nvenc encoders, so a format asking for
  `h264_nvenc` failed at `VHS_VideoCombine` with "Unknown encoder" AFTER the whole sample had been computed and
  paid for. Measured on a GPU on 2026-09-19, on a render that had already cleared its policy gate and produced
  frames. The Verda host now installs `ffmpeg` beside `nfs-common` and `python3-venv`, and the launch line sets
  `VHS_FORCE_FFMPEG_PATH` when a system ffmpeg is present AND has `h264_nvenc`. The nvenc condition is the point:
  forcing a system build without it would trade one silently wrong encoder for another, and the bundled one at
  least works for the stock formats. Two tests, one per half.
- 2.5.8: and so are the SETTINGS, which is the same bug one path away. 2.5.7 moved `workflows/` to the
  directory the server reads and scoped its guard to that path, so an identical split in
  `comfy.settings.json` survived untouched. This package sets `pysssss.ImageFeed.Location=hidden` as hygiene,
  with the comment "it covers App Mode's Run button", and wrote it into ComfyUI's own tree while the server read
  the machine's. The setting never took effect, the image feed sat over the Run button, and every Playwright
  click landed on a `DIV.pysssss-image-feed-menu`: no `POST /prompt` was ever made, so a whole GPU sweep
  reported "did not queue" for every case with no reason attached. `force=True` does not help, because it skips
  the actionability check and still dispatches at the point where the overlay is. Measured 2026-09-19 with
  `document.elementFromPoint` on the Run button's own centre. `lib/80-server.sh` and `lib/70-hygiene.sh` now ask
  `_base_user_dir`, and the static guard is widened from `workflows` to the whole of `$COMFY/user` so the next
  instance cannot hide one path away again.
- 2.5.7: the workflow is written where the SERVER actually reads it. 2.5.0 moved the server's
  `--user-directory` to `BASE_LOCAL_STATE` (`lib/70-hygiene.sh`), because two machines sharing one store cannot
  share one `user/`, and left every WRITER pointing at `$COMFY/user`. So on a shared-root machine the installer
  put each package's workflow in ComfyUI's own tree, the server read the machine's, and
  `GET /api/userdata?dir=workflows` answered `[]`. No package could be opened from the server's own workflow
  list by name, which is exactly what an App Mode pass is required to do. Measured on three machines on
  2026-09-18, one per brand, each holding workflows in a directory nothing served. `_base_user_dir` and
  `_base_workflows_dir` now answer it once and `lib/60-sync.sh`, `lib/80-server.sh` and `lib/90-ledger.sh` all
  ask, so the halves cannot drift apart again; `lib/20-comfyui.sh`'s scaffold still lays out ComfyUI's own
  `user/default/workflows`, which should exist whether or not this machine serves from it.
- 2.5.6: the terminal a customer needs, and four things that had to be right before it could exist. JupyterLab
  now GENERATES a token where `BASE_LISTEN` is loopback, so the machine is not dark until somebody invents a
  credential; on a public bind with no credential 2.1.0's refusal is unchanged, because a generated token there is
  still a service on a proxy URL that nobody asked to start. The token is written owner-only to `state/tokens.env`
  and is NEVER printed: the boot log is a file on the volume, and with a shared store every machine mounts it, so
  the rule that has forbidden logging a supplied token since 2.1.0 applies to a generated one too. `podctl jupyter
  <machine>` reads it over ssh and prints the URL. The IP in that hint is read from the machine's own NIC
  (`hostname -I`); an earlier draft asked api.ipify.org, which the outbound-host allowlist caught — the base asks
  the network nothing about itself. `--port LOCAL:REMOTE` and a per-alias offset (`tunnel_locals`) because with one
  GPU per workflow every machine serves 8188 and one number can reach only one of them; a plain `8188` is
  unchanged. `_base_local_dirs` creates the machine-local directories the launch line names, which is why ComfyUI
  refused to start on a shared root: `--user-directory` pointed at a path no machine had made yet. And
  `recall_library` no longer feeds the shared workspace back to itself as a separate library, which left a stale
  fstab line naming a mount that no longer exists.
- 2.5.5: the GPU lease is the MACHINE's, so it moved off the volume to `BASE_LOCAL_STATE`. It was written to
  `$BASE_VOLUME/comfy-base/state/gpu.lease`, which is per machine only while the volume is. Under the shared root
  (2.5.0) `/workspace` IS one store that every machine mounts, so that one file became a global mutex: three
  machines, three GPUs, one lease, and `lease_blocks` refusing whoever asked second. That is the exact inverse of
  the shape's own claim, which is that installs serialise on the mkdir lock and RUNNING does not, precisely so
  several GPUs can render at once. A pre-2.5.5 lease still on the volume is REPORTED by `podctl lease` and not
  obeyed, because obeying it would reinstate the mutex and it may belong to another machine entirely.
  The rule, and the one to apply to anything added here: anything about the MACHINE is per machine, anything
  about the WORK is shared. Deliberately not moved with it, after checking every `VOLUME_ROOT`-derived path
  rather than the one that bit: `pkgs_dir()` and `upload --to` are the work's; `state/logs/` is the machine's but
  the SHELL side writes there too (`lib/10-discover.sh:5`, `lib/boot.sh`), so moving only the driver's half would
  split an install's logs across two directories, which is worse than one interleaved directory and wants a
  two-sided change. `state/host.env` is the machine's too and is worse than stale: it PROPAGATES, because a
  machine that ensures correctly has `recall_library` read another machine's `BASE_LIBRARY` back out of the
  shared file and mount the store twice. Measured on two machines 2026-09-18; it is owed its own release.
- 2.5.4: a dry run is a report, not a gate, where a report is all it can honestly be. `--check` failed any
  `LOCAL` model row whose file was not on disk, although a `LOCAL` row is placed by the package's own
  `pkg_pre_models`, which does nothing under `BASE_DRY`. So the file was absent BY DESIGN at check time and its
  absence carried no information, and `podctl install --pkg` was gated on a condition the gate itself created:
  every package with a `LOCAL` row stopped at "nothing was run" before it could place the file. Both `LOCAL`
  branches, the file one and the folder-snapshot one, now report the row as unjudgeable **when the package
  declares `pkg_pre_models`**, and are unchanged otherwise: with no such hook a missing file really is missing
  and saying so before the run is the useful answer. A real run still fails either way. Measured on
  a package whose engine is CivitAI only and has no Hub mirror. Four cases in one test hold it,
  including the positive control that a `LOCAL` file which IS present still reports ok, so the guard cannot
  quietly widen to every row. Also: a `SUPERSEDED` entry the sweep can never match (a path, or a dotfile) is
  still skipped, because that sweep matches basenames only and a path pattern would widen it across a store
  several machines share, but it now says so once per pattern instead of silently doing nothing.
- 2.5.3: the Verda host is no longer EXPERIMENTAL. The three questions its README asked are answered by live
  runs: the ssh user is root, the startup script does re-run on a redeploy and fstab survives on the OS volume, and
  `ip` populates within about 30 s. The driver no longer prints the warning on every command.
- 2.5.2: proven on a live machine, and two things it found. `ensure --workspace-shared` could not swap a mount
  under a running system, so the script reported success while the machine was still on its own disk; it now says
  REBOOT and fails the step, because a report line that lies is worse than an error. And `BASE_VOLUME_SHARED` with
  `BASE_LIBRARY` are alternatives rather than layers: together they mount one store twice and the run warns about
  its own tree seen through the second path, so a shared root now ignores the library and says so. Measured with
  the whole root on the store: a cold `import torch` costs 9.28 s against 2.15 s local, warm 1.55 s.
- 2.5.1: `_base_auth_file` read its own argument. `local a="$1" b="...$a"` expands every word before `local`
  binds any, so the second read the CALLER's scope; it worked only because every in-tree caller held `name` set to
  the same token name, and a caller holding a different one sent a bearer token for the wrong name with no error.
  The library link also ignores everything ComfyUI itself ships, `models/configs/*.yaml` as well as the
  placeholders, and copies it onto the library rather than discarding it.
- 2.5.0: `BASE_VOLUME_SHARED`, the whole root on the shared store (§0.14), which is what makes a machine
  disposable rather than only its models shared. `user/` and `temp/` move to `BASE_LOCAL_STATE`; installs take a
  mkdir lock on the store, honoured for two hours and then broken with the owner named; running is unlocked. On
  Verda `podctl ensure --workspace-shared` mounts the shared volume AS `/workspace`, so the machine needs no data
  volume and `startup.sh` never hunts for one.
- 2.4.0: `BASE_LIBRARY`, a model library several machines mount at once (§0.13). Discovery prefers it over
  `extra_model_paths.yaml`, which is not yet written on a machine's first install; staging is per machine so two
  installs cannot collide on one half-file; the disk gate measures the library rather than the volume. On Verda:
  a shared volume is resolved from the account and attached by type and location, never by name stem.
- 2.3.0: the first public release. Models come from Hugging Face and GitHub only; model families are a brand's list,
  not the base's; the loader map covers ComfyUI's core loaders and the shared packs', packages extend it with
  `LOADER_CATS`; the fake-pod layouts are `official | community | bare | volume`.
