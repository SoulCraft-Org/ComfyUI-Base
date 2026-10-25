# ComfyUI Base: session hand-overs

Append; never rewrite an earlier entry. The long record is the handbook's §10.

## 1. Named ports: ComfyUI, Jupyter Terminal and each app as named links (3.9.0)   (2026-10-16, no pod used)

**Proven.** On this Mac, against fakes only: `bash _build/verify.sh` from a clone outside `~/Developer` (suite from the
repo and from the extracted zip; the counts are in the release commit's PR, SoulCraft-Org/ComfyUI-Base#8). New `suite/test_ports.py`:
- `py/app_proxy.py` in front of a raw-socket ComfyUI stand-in: `/app.js` -> `/apps/<slug>/app.js` with Host and
  Origin untouched; `/api/status` -> the app's api; a 300 000-byte `POST /upload/image` passed byte for byte;
  `/view?…` unchanged; a websocket `GET /ws` gets its 101 and echoes both ways; with no ComfyUI, a 503 "ComfyUI is
  starting" page that reloads itself.
- `boot.sh --apps` starts one forwarder for every `state/apps.d/*.list` row, a second call replaces it (one pid),
  no rows leaves none; the install's BOOT stage writes `apps.d/<package>.list` from `APP_PORTS`, refuses a base port
  or a bad slug, removes the file when the package declares none.
- `podctl create` against the fake RunPod REST + GraphQL: ports `8188/http 8888/http <app>/http 22/tcp`; a private
  template `comfy-base ports <sig>` made once (REST), its names written by `saveTemplate` with every field re-sent
  and read back; the pod's `templateId` is it; a fresh 48-hex `JUPYTER_TOKEN` per pod, printed once as the ready "Jupyter Terminal" link (the private default hands it to the volume over ssh stdin and prints the tunnel's localhost links); `--no-names`
  and `--private` take no template; a refused name write is said and the pod is still made. `deploy --like` keeps a
  names template only, and gives the clone its own token. `status` prints the named links (the terminal's with its
  token); `tunnel` forwards the machine's app ports and prints the named localhost links.
- JupyterLab on a public bind now starts with a generated token; the token is never in the boot log, and on RunPod
  one line names it on fd 3 (the container log); a token the pod's env set is never repeated.

**How.** From this checkout: `python3 _build/package.py --base && bash _build/verify.sh`;
`python3 -m pytest suite/test_ports.py -q` alone.

**Owed (needs a real pod; nothing here ran on one).**
- The Connect tab showing "ComfyUI", "Jupyter Terminal", "<app name>" for a pod made by `podctl create`. The
  inference that a pod shows its TEMPLATE's `portsConfig` is from runpodctl's source and the API shapes, not from a
  console seen. If it does not, the fallback is `--no-names` (same ports, generic names).
- That a stop/start of a pod on the names template keeps its env (PUBLIC_KEY, JUPYTER_TOKEN) and the start command
  `ensure` set. The template has none of its own, so nothing should be re-applied; not yet seen.
- `py/app_proxy.py` in front of a real ComfyUI with the App-Front pack: the Video Replacer app at
  `https://<pod>-8189.proxy.runpod.net/` uploading, running and streaming progress over `/ws`, through RunPod's
  proxy (its 100 s request timeout applies to every request, websocket included as far as RunPod documents).
- The Logs tab carrying the `Jupyter Terminal token:` line for a pod from the public template.

**Found on the way.**
- `suite/test_podctl.py::test_unit_repo_root…` fails on any checkout under `~/Developer` on this Mac, because
  `~/Developer/base/comfyui-base` exists (repo_root walks up to it). Environmental, not a code defect; not changed.
- The workspace's `bootstrap.sh` dies on macOS's bash 3.2 without `--depth` (`DEPTH[@]: unbound variable`, line 41).
- runpod-template's image test reports `Dockerfile, seed-script.sh` stale against any base newer than its last
  regeneration; it moves with the `repos.txt` pin, not with this change.

## 2. Model downloads beside the venv build (3.11.0)   (2026-10-16, two RunPod pods, both deleted)

**Proven.** On RunPod, two fresh pods side by side (H100 80GB HBM3, EU-FR-1, bare `runpod/base` CUDA 13 image, network
volume, MiniMax H3 Video Continuer 1.0.5, 41.1 GB of models, `podctl install --base --pkg … --no-gate --no-suite`):
create to the workflow served through the public proxy, 3.9.2 1610 s, this branch 1471 s. The early pass downloaded all
six files beside the venv (605 s at 70 MB/s; 304 MB/s alone), counted them once, and the join waited under a second.
On this Mac and in CI: `suite/test_models_early.py` (the overlap really happens, what goes early, the count, an early
failure fails by name with three attempts not six, the disk gate, the downloader's order, kill on exit, the pipeline's
order, the fake-pod install both ways); the full suite green in CI.

**How.** `podctl create` twice at once, `podctl install <pod> --base --pkg <dir> --no-gate --no-suite`, then the
summary's clock (`state/logs/<pkg>_<ts>.timings.tsv`); `BASE_MODELS_FOREGROUND=1` for the old order on one machine.

**Owed.** A package with more models than its venv takes to build (Krea 2, 124 GB; Video Replacer, 112 GB): the join
then waits, and the gain is what downloaded during the venv. A B200 run. The early pass with the Hub session's
parallel downloads (3.12.0) once both are in.

**Found on the way** (sent to the platform orchestrator).
- `podctl create --public` takes a data centre from the stock check that REST `POST /pods` refuses (its
  `dataCenterIds` enum lacks US-NE-1, US-NC-2, US-PA-1), and one that takes no network volume (US-NC-1, US-MO-2,
  EUR-IS-2). Both stop before billing.
- The combo check's `cp -p` into `/tmp` printed "preserving permissions … Operation not supported" on the bare image.
- Other serial steps that could overlap: the tools venv (JupyterLab, 45 s of BOOT) could build beside the main venv;
  apt could overlap the downloads if the downloader did not need uv's bootstrap (python3-venv comes from apt); the
  model index walk runs twice (early pass, model stage).

## 3. ComfyUI runs from a copy on the machine's own disk; a first install starts it once (3.15.0)   (2026-10-16, no pod used)

**Proven.** On this Mac and in the PR's CI, against fakes and real local venvs only. `suite/test_run_local.py`: a real
venv copied to a generation, relocated (bin/ links, `pyvenv.cfg`, shebangs, `*.pth`) and run from the copy with
`sys.prefix` inside it; a uv Python root mapped, a `ComfyUI-old` path and a `models/` path left alone; `models/ input/
output/ user/`, every `.git` and a pack's weight file are symlinks to the volume's; a venv backup never copied; a second
sync with nothing changed is `fresh` without a walk; a bumped `run.gen` makes a new generation that hardlinks what did not
change and never writes the old one; a new pack re-walks; no room and a failed import check fall back to the volume and
leave no generation; `--auto` copies only from a network filesystem (mountinfo parsing, fuse vs fuseblk); the warm read;
the boot's head start before sshd; the pid matcher; the deferred import check (a named pack from this start's log slice
only, a clean start, a start that never answers judged the long way and started again, the own-pack judge held back).

**How.** `python3 -m pytest suite/test_run_local.py -q -p basetest` (PYTHONPATH=py); the full gate in CI.

**Owed (needs a real pod; nothing here ran on one: RunPod had no credit on 2026-10-16).**
- One fresh pod on a NEW network volume (the Video Replacer's models do not fit 60 GB, so the base's own install):
  3.13.0 then this branch, timing the import check, the restart and a stop/start boot to 8188 up; the copy's own line
  (`copied from the volume in N s`) and `state/logs/run-local.log`. Before, measured by the platform orchestrator on
  kmgqu8yf0pscts: 1.72 ms per stat, `import torch` 56 s, import check 7.6 min, restart 3.5 min.
- On the same pod: one `tar.zst` of the venv unpacked from the volume against the parallel copy, and a venv built on the
  container disk against one built on the volume. These decide the next step: the install building the venv on local
  disk and writing it back (packed or in parallel), which is where a first install's minutes of FUSE writes go.
- That RunPod's FUSE mount reports a `fuse*` type in `/proc/self/mountinfo` (auto mode keys on it; `BASE_LOCAL_RUN=1`
  forces it if not).

## 4. 3.11.0 measured on RunPod; the first install's packs pip skip fixed (3.15.2)   (2026-10-16, pods and volumes deleted)

**Proven.** Two fresh pods side by side (H100 80GB HBM3, EU-FR-1, bare `runpod/base` CUDA 13 image, a network volume,
MiniMax H3 Video Continuer 1.0.5, 41.1 GB, `podctl install <pod> --base --pkg … --no-gate --no-suite`), create to the
workflow served through the public proxy: 3.9.2 2113 s, 3.11.0 1716 s. The downloads (127 s) and the tools venv (112 s)
ran hidden beside the venv chain; the join waited under a second; BOOT went from 83 s to 1 s. The run also showed the
packs pip resolve 3.11.0 meant to skip (138 s): fixed here, held by `suite/test_parallel_install.py`.

**Owed (needs pods).**
- The packs pip skip on a pod (about 130 s expected on a first install).
- A second pod on a volume already installed GREEN, create to App usable (the "under 3 minutes" target): needs a package
  whose install ends green (the Continuer 1.0.5 fails its smoke on every base: `LoadImageCrop` not registered).
- A B300 run showing its venv kept (`sm_103` covered by `sm_100`).

**Found on the way.** The combo check's `cp -p` into `/tmp` prints "preserving permissions … Operation not supported" on
the bare image. The next serial step worth cutting is the newest pass (467 s here: two forcing rounds, each a full
re-resolve and install, plus the holders' import probe).

## 5. Testing that costs nothing: tier 0, the cloud brief, the pod as a runner (3.17.0)   (2026-10-16, no pod used)

**Proven.** Offline only, by the unit tier: `_build/objinfo.py` refuses what ComfyUI's validator refuses on a synthetic
schema (a missing required input, a link to no node or past a node's outputs or of the wrong type, a value below min or
above max or not a number, a combo value not in the list, with a "did it become" hint when one option shares its head)
and leaves upload combos, empty option lists and V3 dynamic inputs "unjudged", never passed; it judges only what the
outputs reach. `test quick` hands pytest one `-m` expression of the light tiers. `podctl runner` puts the token on ssh's
stdin and nowhere else, and `stop` asks GitHub only when this Mac registered a runner. `_build/quick.sh` was timed on
the Video Replacer (the PR has the numbers).

**Owed.**
- `podctl runner` on a live pod: config.sh with `ACTIONS_RUNNER_INPUT_TOKEN` on the RunPod image, one job taken from a
  `runs-on: [self-hosted, gpu-pod]` workflow, the runner gone from GitHub after it and after `podctl stop`.
- `_build/cloud-verify.md` run once in a cloud session (the orchestrator launches it): whether the environment's network
  lets `testbed.sh` provision, and whether the proxy lets `gh pr comment` through.
- A package's first `objinfo.py save` from the pod, so its prompts are checked against the pod's own lists.

## 7. One ComfyUI per GPU, podctl create --gpus N (3.20.0)   (2026-10-17, no pod used)

**Why.** A multi-GPU pod ran one ComfyUI on GPU 0 and left the rest idle. The Video Replacer (5.9.0) splits a clip at
its cuts into independent shots; N servers render N shots at once, each exactly as one GPU would.

**What changed.** `lib/85-launch.sh`: `_base_server_count` (GPUs, capped by `BASE_SERVERS`), `_base_spawn_server` (one per
GPU: `CUDA_VISIBLE_DEVICES`, port 8188 or `127.0.0.1:18188+i`, its own temp folder and database, `COMFY_BASE_SERVERS` and
`COMFY_BASE_SERVER_INDEX`), `state/servers.list`, the quota's `OMP_NUM_THREADS` split by N. `lib/boot.sh` and the install's
restart start them all through `_base_start_comfy`. `hosts/runpod/provider.py`: `create_body(gpu_count=)`,
`stock_count` / `count_rows` (lowestPrice with the count, one aliased query), `price()`. `podctl create --gpus N`, the
money guard (`money_guard`: price, RAM and vCPUs, refused under one hour of balance), `podctl status` names the servers.
`py/gpu_facts.py` and podctl's `gpu_probe`: each card judged by its own processes (`verdict_many`, by uuid).
`balance()`'s spend is never below the running pods' `pod_rate` (the larger of `costPerHr` and gpuCount x
`securePrice`), so `podctl balance`, `guard` and create's guard project N GPUs at N x the price. engine-akatz's three
commits: `ultralytics/bbox`, `ultralytics/segm`, `Wan`, `DreamID-V`, `MiniMax-Remover` are flat model categories.

**Proven, and how.** The suite on this Mac against fakes: a 4-GPU `nvidia-smi` stub through the launch functions, the
start and the fake pod's boot; the fake RunPod for `--gpus 2`, the count query, the price line and the STOP. CI on the PR.

**Owed (the orchestrator's GPU proof, 2x B200: x4 and x8 had no Secure stock on 2026-10-17).**
- The boot log's `2 servers, one per GPU`, both servers' `/system_stats` naming their own GPU, and each package's warm-up
  on each.
- One shot rendered on server 1 is byte-identical in content to the same shot on server 0 (same seed).
- Host RAM with both servers loaded (the 4/8 rows of `lowestPrice` once they have stock).

## 8. The cuBLAS probe beside the venv build; the install's timeline (3.22.0)   (2026-10-17, no pod used)

**Why.** Paul's priority 3, install speed. The last Video Replacer pod (yswh3tkypxhxev, 1x B200, base 3.20.0, every row:
42 files, 295.5 GB) took about 5:17 from create to the App on its fresh attempt (its 16:55 includes a failed first
attempt and the pod stopped for about 11 minutes). The long pole was the 66.3 GB B300-only file (147 s with its sha256),
with VENV (116 s) beside it, and VENV held an apt install of `cuda-libraries-dev-13-0` (2,251 MB, 33 packages) on an
install that built nothing. The stage clock had seconds only, so what ran beside what was inferred, not read.

**What changed.** `lib/40-packs.sh`: `_base_cuda_toolchain_start` / `_join` / `_stop`; `lib/95-summary.sh` starts the
probe with the venv and joins it before `pkg_post_venv`. `lib/00-env.sh`: `_base_span`, `BASE_SPANS`, `BASE_FILE_SPANS`,
`BASE_EPOCH0`, `BASE_CUDA_FOREGROUND`; `lib/92-onepass.sh`: `_base_timeline_write` (`<pkg>_<ts>.timeline.tsv`);
`lib/50-models.sh`: each download's `BASE_VERIFY_SECS` and spans; the background joins (system, models, tools) add
theirs. `_build/pod/podctl.py`: `PODCTL_TIMES=1`.

**Proven, and how.** `suite/test_critical_path.py` in CI (nothing ran on this Mac but `bash -n` and the packager).

**Owed (the next pod).** The VENV stage without the apt, the `CUDA TOOLCHAIN (in the background…)` row's seconds, and the
timeline's critical path; the measurement plan is in the PR. The ranked levers and the sources are in the orchestrator's
notes (`install-speed-ranked.md`, `install-speed-sources.md`).

## Requests

### 1. To the MiniMax H3 Video Replacer: declare the app's port
One line in `video-replacer-minimax-h3-script.sh`, beside `WF_NAME`, once the workspace pins base 3.9.0 (`BASE_MIN`
may stay: a base before 3.9.0 never reads the array): `APP_PORTS=("8189|video-replacer|Video Replacer App")`. Then `podctl create`, `status`, `tunnel`
and the template name it, and the machine serves the app at that port's root. The app needs no change: it already
reaches ComfyUI through `new URL("../../", HERE)` and names no top-level file `ws upload view history queue interrupt`.

## 6. The GPU order past B200 and B300, no volume needed, and the balance projected (3.18.0)   (2026-10-16, no pod used)

**Why.** Paul, 2026-10-16: rendering speed first. A B200 and a B300 sold out, or the B200 only where the network volume
is not (on 2026-10-16 the volume was in EU-RO-1, which then had only RTX PRO 6000s and smaller), left `podctl create`
with no pod. And RunPod's balance trails its spend: a $5 stop rule let it reach -$1.24 and RunPod removed the pods.

**What changed.**
- `hosts/runpod/provider.py`: `DEFAULT_GPUS` is B200, B300, H200, H100 80GB HBM3, RTX PRO 6000 Blackwell Server
  Edition (Paul's order, 2026-10-16; RunPod's ids from GraphQL `gpuTypes`). `GPU_NOTES` says what a pick means before anything bills (B300: INT8 is about 1/24 of a B200's, NVIDIA's HGX
  table, so load fp8 weights; H200: nvfp4 is emulated). `create_body(pod_gb=)` / `create(pod_gb=)`: a PUBLIC pod on a
  pod volume. `balance()`: GraphQL `myself { clientBalance currentSpendPerHr spendLimit }`.
- `podctl create`: per GPU in order, the reusable volume where it is free, else a fresh pod on a `--pod-gb` (default
  400) pod volume in the first data centre with it (any data centre, since none needs to take a network volume);
  `--no-fresh` is 3.14.0's rule. `--location`, `--volume`, `--data-gb` and `--private` are unchanged.
- `podctl balance [--lag-min 30]` adds the spend per hour and the projection; `podctl guard --floor 5 [--yes]` stops every
  running pod once the projection is at the floor (exit 3 there; without `--yes` it names them). Run it from a
  scheduler on the Mac; the base schedules nothing.
- `LAUNCH_ENV` allows `RENDER_SPEED_ATTENTION` (the Render Speed pack's startup order: off, cudnn, fa4).

**Proven, and how.** Nothing ran here beyond `python3 -m py_compile` and `package.py --base`: the session was tier 0
only (no suite on the Mac). The suite's new and changed tests (`test_podctl.py`: the order and notes, the fresh pod
against the fake RunPod, balance and guard; `test_ports.py`: reuse, now fresh by default and STOP with `--no-fresh`;
`test_unit.py`: the launch key) run in this repository's CI on the PR.

**Owed (needs RunPod credit).**
- A real `podctl create` with no `--gpu` that goes fresh: the pod volume's size and mount, and the install's download
  time with no volume (six at a time, pinned).
- `podctl balance` against the live account while a pod runs: how far `clientBalance` actually trails, to set
  `--lag-min` from a measurement instead of 30.

## 9. The Python side of a fresh install: the image's wheels, one torch pick (3.26.0)   (2026-10-25, no pod used)

**Why.** Paul's priority 3, install speed (target 8 minutes from pod create to App ready). The 5.9.0 install's VENV took
116 s on `runpod/comfyui:1.4.7-cuda13.0`. The picks are a few seconds of it (about 14 uv compiles, measured on the Mac at
about 1 s cold and near 0 warm); the rest is 4.1 GB of wheels fetched and unpacked (3.0 GB of them torch's closure), the
verify, and the cuBLAS apt 3.22.0 moved beside it. The template image could carry all of that, but a podctl pod never
copied its seed, and its build deleted uv's cache.

**What changed.** `lib/30-venv.sh`: `_base_venv_image_cache` (a venv being built reads `$BASE_IMAGE_SEED/.cache/uv`,
`UV_LINK_MODE=copy` across filesystems), `_base_image_python`, `_base_image_twin`, `_base_in_image_seed`, and
`_base_torch_pick` answering once per run. `lib/40-packs.sh`: CONSOLIDATE skips the seed's tree and venv.
`lib/47-latest.sh`: `_base_outdated --exclude` the torch closure. `lib/15-system.sh`: `--force-unsafe-io` on RunPod.
`lib/85-launch.sh`: the readiness polls every second. The image's half is runpod-template's PR of the same day (the seed
keeps uv's cache, hardlinked with its venv; ffmpeg and the OS upgrade baked in; the seed copy keeps hardlinks).

**Proven, and how.** The unit tier in CI (`suite/test_image_start.py`, and tests beside the pick, the scan, apt and the
poll). Render parity (Paul: an install change must never change the render): the image's index listings are dropped
before uv reads its cache. `test_unit_the_image_cache_resolves_the_same_versions_as_a_cold_cache_never_older` shows a
stale listing installing an older version without the guard, and an empty freeze diff against a cold cache with it. On this Mac only `bash -n`, the packager and the new functions run once against a scratch tree.

**Owed (the next pod, on the image).** The image has never been built: base-image.yml is skipped until `IMAGE_RUNNER`
names a machine with Docker. Then, on one fresh pod made with `--image ghcr.io/soulcraft-org/comfyui-base:<tag>`: the
pull (create to RUNNING, uncached and cached), VENV and its `uv cache: the image's` line, and the timeline's critical
path against the 5.9.0 pod's.
