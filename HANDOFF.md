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

## Requests

### 1. To the MiniMax H3 Video Replacer: declare the app's port
One line in `video-replacer-minimax-h3-script.sh`, beside `WF_NAME`, once the workspace pins base 3.9.0 (`BASE_MIN`
may stay: a base before 3.9.0 never reads the array): `APP_PORTS=("8189|video-replacer|Video Replacer App")`. Then `podctl create`, `status`, `tunnel`
and the template name it, and the machine serves the app at that port's root. The app needs no change: it already
reaches ComfyUI through `new URL("../../", HERE)` and names no top-level file `ws upload view history queue interrupt`.
