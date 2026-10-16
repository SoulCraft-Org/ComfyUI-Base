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

## Requests

### 1. To the MiniMax H3 Video Replacer: declare the app's port
One line in `video-replacer-minimax-h3-script.sh`, beside `WF_NAME`, once the workspace pins base 3.9.0 (`BASE_MIN`
may stay: a base before 3.9.0 never reads the array): `APP_PORTS=("8189|video-replacer|Video Replacer App")`. Then `podctl create`, `status`, `tunnel`
and the template name it, and the machine serves the app at that port's root. The app needs no change: it already
reaches ComfyUI through `new URL("../../", HERE)` and names no top-level file `ws upload view history queue interrupt`.
