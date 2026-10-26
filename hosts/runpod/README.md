# hosts/runpod : RunPod, the proven host

A RunPod pod is a container. Its start command is what runs at boot, its persistent storage is a network volume mounted
at `/workspace`, and it is reached through a public IP with a mapped ssh port and through RunPod's HTTP proxy. This
folder's `provider.py` is the RunPod half of the driver, moved here verbatim in base 2.2.0.

## What you need on the Mac

- **The API key.** `RUNPOD_API_KEY` in the environment, else (3.19.0) the macOS login Keychain item whose account is
  `RUNPOD_API_KEY` (any service name; `BASE_KEYCHAIN=0` skips the lookup), else `~/.runpod/config.toml` as written by
  `runpodctl config --apiKey=<key>`. It is sent as a bearer header and is never printed, logged or put in an argument.
- **Tokens for the pod (optional).** Account Secrets named exactly `HF_TOKEN` and `CIVITAI_TOKEN` (console: Secrets).
  `podctl create` lists the account's Secret names (REST v2 `GET /v2/account/secrets`, never a value) and gives the new
  pod `HF_TOKEN={{ RUNPOD_SECRET_HF_TOKEN }}` and the same for `CIVITAI_TOKEN` when the secret exists; RunPod puts the
  value in at start. A missing secret is never referenced, and a failed listing is a warning, not a failed create.
- **An ssh key.** `~/.ssh/id_ed25519` and its `.pub`; `--pubkey` names another.
- `uv` (the driver is a `uv run` script with no dependencies).

## What the pod needs

- **`PUBLIC_KEY`** in the pod's env, set to the Mac's public key. `podctl ensure` sets it (the pod resets); the base's
  `boot.sh` writes it to `/root/.ssh/authorized_keys` and starts sshd on 22. No key, no sshd: the base never starts a
  listener nobody can log in to.
- **A network volume at `/workspace`** (`volumeMountPath`). The base installs itself to `/workspace/comfy-base` and the
  packages to `/workspace/packages`, so a stop, start or redeploy keeps everything. `podctl install` records the volume's
  size in `state/volume.env` because `df` on the pooled volume reports the pool, not the quota.
  A **pod volume** (`volumeInGb`, the console's "Volume Disk" with no network volume chosen) also mounts at `/workspace`
  and survives stop/start, but it belongs to that machine: terminating the pod deletes it, and `deploy --like` cannot
  share it. `status` says which kind a pod has (3.7.3); before that a pod volume printed as "volume NONE".
- **Network volumes exist only in Secure Cloud and pin a data centre**: the pod lands where its volume is, so the GPU must
  be in stock THERE. `podctl create` checks that first.
- **22/tcp exposed.** `ensure` adds it when missing (the pod resets).
- **Several GPUs (3.20.0).** `podctl create --gpus N` (1 to 8): the boot runs one ComfyUI per GPU, 8188 for GPU 0 (the
  links) and `127.0.0.1:18188+i` for the others, which a package's runner reaches on the pod (the handbook's §6 Servers).
  The stock check asks for N per data centre (`gpuTypes.lowestPrice(gpuCount, dataCenterId)`): a data centre with one
  free GPU of the type is not one with four. RunPod sizes RAM and vCPUs with the count (B200, 2026-10-17: x1 188 GB and
  24 vCPU, x2 502 GB and 48 vCPU), so each server has at least what a 1-GPU pod gives it. Every create prints the pod's
  price and refuses when the projected balance covers under one hour of it plus today's spend.

## Which GPU

The video packages are tuned for **B200** (`NVIDIA B200`, `sm_100`, 180 GB) first and **B300** (`NVIDIA B300 SXM6 AC`,
`sm_103`, 288 GB) second; a package declares that with `GPU_PREFER="B200 B300"` and the base warns, never stops, on
anything below (`py/gpu_fit.py`, the handbook's §1.1). `podctl create` asks for them in that order when no `--gpu` is given,
then (3.18.0) `NVIDIA H200`, `NVIDIA H100 80GB HBM3` and `NVIDIA RTX PRO 6000 Blackwell Server Edition`, the
first GPU with stock winning: on the
reusable network volume where it is free, else as a fresh pod on a pod volume (`--pod-gb`, default 400) wherever it is
(models download again; `--no-fresh` waits for the volume instead). On a B300 its note says to load fp8 weights: INT8 is
about 1/24 of a B200's there (NVIDIA's HGX table). On
RunPod, as read on 2026-10-15/16 (stock moves; the create reads it live):

| | B200 | B300 |
|---|---|---|
| type id | `NVIDIA B200` | `NVIDIA B300 SXM6 AC` (not `… MIG 1g.34gb`, a 34 GB slice) |
| cloud | Secure | Secure (EUR-IS-1) |
| datacenters with stock | EU-RO-1, US-CA-2, US-NC-2, US-NE-1, US-TX-6 | EUR-IS-1 (the one that also takes a network volume) |
| CUDA floor | 13.0 (3.29.0: checked on the placed pod, never sent as a list) | 13.0 |
| NVENC | none (NVIDIA's matrix: 0 encoders, 7 decoders) | none |

A network volume pins its datacenter, so create the volume where the GPU is. With no NVENC, video is encoded on the CPU
(`libx264`); `h264_nvenc` fails on both.

## How the boot is handed over

`podctl ensure <pod>` wraps the image's own start command with the boot guard:

    bash -c "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi; <sshd bootstrap>; exec <the image's own entry>"

Before the base is installed that boots the image as it was, plus sshd. Afterwards the base's `boot.sh` is PID 1:
sshd, JupyterLab (with the pod's `JUPYTER_TOKEN` or `JUPYTER_PASSWORD`, else a token the boot generates: never
tokenless), the saved tokens, provisioning, ComfyUI with the base's launch line, the app ports, then `sleep infinity`. The image's entry is read from Docker Hub's registry; pass
`--entry '<its start command>'` for an image the registry cannot describe. `ensure` is idempotent: a pod that already
carries the guard is not patched and its image is never fetched. RunPod has no systemd, so this wrap is the whole
mechanism; `BASE_HOST` is detected on the pod itself from PID 1's `RUNPOD_POD_ID`.

Every patch above resets the pod, so (3.10.1) `ensure` never resets one in the middle of its image's FIRST boot (an
image like `runpod/comfyui` copies ComfyUI and its venv onto the volume then; a reset mid-copy left no venv and a crash
loop): while the base's boot is not PID 1 yet, it waits until an image that ships ComfyUI answers through the proxy on
8188 (any status below 500; the proxy says 502 while nothing listens), and for any other image (`runpod/base` serves
nothing there) until three minutes after the pod started, at most 15 minutes, then resets. After a reset it waits for the pod's `lastStartedAt` to move (right after the
PATCH the API still answers with the old pod) and looks for sshd on the port the pod has NOW, re-read on every try:
RunPod may map 22/tcp to a new port on a reset (20424 became 36944 on 2026-10-16, and `ensure` polled the old one).

`create`'s stock check (3.10.1) offers only data centres a pod can be created in: those in REST v1 `POST /pods`'
`dataCenterIds` enum (read from `https://rest.runpod.io/v1/openapi.json` once per run; 30 ids on 2026-10-16, without
US-NE-1, US-NC-2 or US-PA-1, which the stock view names) and, when a network volume is wanted, those whose GraphQL
`dataCenters.storageSupport` is true. Each one left out is printed with why.

## The addresses

- **ssh: `<public ip>:<mapped port>`**, user `root`. The mapped port changes on every start, which is why `start --wait`,
  `restart --wait` and `deploy --wait` rewrite the `Host runpod` block in `~/.ssh/config` (HostName and Port only; every
  other byte of the file stays). `ssh-config <pod>` rewrites it by hand.
- **HTTP: RunPod's proxy**, `https://<pod id>-<port>.proxy.runpod.net`: 8188 for ComfyUI, 8888 for JupyterLab, and
  one port per customer app (below). The proxy routes by port only (no paths), reaches the container from outside, so
  everything it serves binds `0.0.0.0` on this host: `BASE_LISTEN=0.0.0.0`, the one host where it is not loopback.
  RunPod's limits: at most 10 HTTP ports, a 100 s timeout per request (524), and the URL is public. JupyterLab on a proxy
  URL is root on the box, which is why it never starts without a token.
- **The tunnel**, `podctl tunnel <pod>`: the ONE ssh session that forwards 8188, 8888 and the machine's app ports to
  `localhost` (`--port` replaces the list), after printing them as named links. Use it for the canvas tools and the
  browser when the proxy is not wanted, and for a `--private` pod, which has no proxy URL; command sessions carry no
  forwards, so nothing else fights the tunnel for the ports.

## Named ports (3.9.0)

The Connect tab lists each HTTP port by name: **ComfyUI** (8188), **Jupyter Terminal** (8888), and one per customer app,
e.g. **Video Replacer App** (8189). How, from first-party sources (read 2026-10-16):

- RunPod names a port only on a **template**: GraphQL `PodTemplate.portsConfig: [PortConfig {port: String!, name:
  String!}]`, written by `mutation saveTemplate(input: SaveTemplateInput)` (graphql-spec.runpod.io; runpodctl v3.0.1,
  `internal/api/template_port_labels.go`, "a named port shown in the Runpod dashboard"; `runpodctl template update <id>
  --port-labels '{"8188":"ComfyUI"}'`). REST v1 and v2 have `ports: ["8188/http", …]` and no label field; a pod has no
  `portsConfig`, and one without a template shows "Port 8188 → HTTP Service". A label keys on the bare port number,
  must be one of the template's ports, and may not be blank.
- `saveTemplate` replaces the WHOLE template, so the names are written as runpodctl writes them: read the template back
  (`myself { podTemplates { … } }`, retried while a fresh one is not visible yet), re-send every field with `portsConfig`.
- `podctl create` therefore makes, once per set of names, a private template `comfy-base ports <sig>` (REST
  `POST /templates`: the ports, no env, no start command, no volume, then the names through `saveTemplate`), and creates
  the pod with its `templateId` (REST v1, which keeps the link; REST v2 drops it after create). Every other field of the
  pod is the create body's own. `--no-names` skips the template. `deploy --like` keeps a names template and drops any
  other (the reason `deploy` never took the source's template: an image's template re-applies its own env and start).
- The names come from the base (8188, 8888) and from each package's `APP_PORTS=("<port>|<slug>|<name>")` (handbook
  §4), read from the tree's package scripts; two packages claiming one port is an error naming both.
- **On the pod**, ComfyUI serves an app at `/apps/<slug>/`; the proxy cannot link a path, so `py/app_proxy.py` serves
  each app at the root of its own port (`:8189/` → `127.0.0.1:8188/apps/<slug>/`, ComfyUI's `ws upload view history
  queue interrupt` passed through; the websocket included). The BOOT stage writes `state/apps.d/<package>.list` and
  restarts it (`boot.sh --apps`); the boot starts it after ComfyUI. While ComfyUI is not up yet, the app's port says so.
- **The terminal's token, one click (Paul's order: delight, then secure, then private).** `create` puts a fresh
  `JUPYTER_TOKEN` in the pod's env and prints the ready links at once: `ComfyUI`, `Jupyter Terminal`
  (`…-8888.proxy.runpod.net/lab?token=<token>`) and each app. A `--private` pod keeps
  the token out of RunPod's env (stored in the clear): once ssh answers, `create` hands it to the pod volume through
  ssh's stdin and prints the same names as the localhost links `podctl tunnel` opens. Never tokenless: the URL is public
  and the terminal is root.
- **Open by default (3.19.1).** Since 3.19.1 the links are OPEN by default: one click on the Connect tab opens ComfyUI, each app and the Jupyter Terminal (no key, no token), because RunPod's proxy passes nothing that names the pod's owner (its docs: the service "becomes publicly accessible"; "implement proper authentication in your application"), so any login is a step. The key below, and JupyterLab's token on the public bind, are the opt-in: `BASE_LINK_LOCK=1` in the pod's env (`podctl create --lock`).
- **With `--lock`, every other link is locked by the pod's key (3.14.0).** `create --lock` also puts a fresh
  `BASE_LINK_TOKEN` in the env, and every link it prints but the terminal's carries it once: `…-8188.proxy.runpod.net/?token=<key>`.
  On the pod, `py/app_proxy.py` stands in front of ComfyUI's 8188 (on the container's own addresses; ComfyUI itself
  listens on 127.0.0.1) and of every app port. The key in the query is traded for a cookie (`base_link`: HttpOnly,
  Secure, SameSite=Lax, no Domain, so no other pod under `proxy.runpod.net` ever receives it) and a 303 to the same
  URL without it; from then on the Connect tab's plain links open in that browser. Neither key nor cookie: a plain 401
  page that says where the full link is. The compare is constant-time, the key is never logged, and the cookie is
  taken out of the request before ComfyUI sees it. A connection from the machine itself (an ssh tunnel, the install's
  own checks) passes unasked. A pod without the key in its env (a public template's) gets one generated by the boot,
  saved to `state/tokens.env` (0600), and its links named on the pod's Logs tab, never in the boot log on the volume.
  The Connect tab cannot carry a key (RunPod links a port's bare root), so the first visit in a new browser needs
  the printed link once.
  `status <pod>` prints every link, the Jupyter Terminal's as `…-8888.proxy.runpod.net/lab?token=<token>` (the env's, else
  the one the machine saved, read over ssh); `jupyter <pod>` prints it too. A pod without one in its env (a public
  template's) gets one generated by the boot, saved to `state/tokens.env` (0600) and named on ONE line of the pod's
  Logs tab (`Jupyter Terminal token: …`, the start command's stdout, the pod owner's), never in the boot log on the volume.

## The commands

    uv run "_build/pod/podctl.py" pods                          # every pod: id, name, image, status, public ip, port 22
    uv run "_build/pod/podctl.py" create --name <n> --image <img> [--data-gb 500] [--gpu <type>] [--location <dc>] [--volume <id> | --volume-name <n> | --private] [--pod-gb N | --no-fresh]
                                                                # stock check (B200, B300, H200, H100, RTX PRO 6000 unless --gpu); a PUBLIC Secure Cloud pod on the network volume
                                                                # where that GPU is free, else (3.18.0) a fresh pod on a pod volume where it is;
                                                                # comfy-base-data (reused where the GPU is free, else made of --data-gb), named links locked by
                                                                # the pod's key; --private: a pod volume, ssh only, ComfyUI through podctl tunnel
    uv run "_build/pod/podctl.py" balance [--lag-min 30]        # 3.18.0: the balance, the spend per hour, the balance projected past RunPod's lag
    uv run "_build/pod/podctl.py" guard --floor 5 [--yes]       # 3.18.0: stop every running pod once the projection is at the floor (exit 3 there)
    uv run "_build/pod/podctl.py" status <pod>                  # facts; env NAMES, never values; the named links (each with its key or token); the GPU verdict
    uv run "_build/pod/podctl.py" ensure <pod>                  # 22/tcp, PUBLIC_KEY, the boot guard, Host runpod
    uv run "_build/pod/podctl.py" install <pod> --base --pkg <dir>...   # upload, extract, --check, run (everything newest), poll, logs, pack records
                                                                # --no-suite (BASE_NO_SUITE=1 on the machine), --env KEY=VALUE (repeatable)
    uv run "_build/pod/podctl.py" stop|start|restart <pod> --wait
    uv run "_build/pod/podctl.py" deploy --like <pod> --wait    # a NEW pod cloned onto the same volume (a machine whose GPU is not ours)
    uv run "_build/pod/podctl.py" lease <pod> [--take|--release] [--minutes N] [--purpose ...]   # who holds the GPU; advisory, expires
    uv run "_build/pod/podctl.py" tunnel <pod>                  # 8188, 8888 and the app ports to localhost, printed as named links, until killed
    uv run "_build/pod/podctl.py" jupyter <pod>                 # the Jupyter Terminal's link with its token (proxy and tunnel)
    uv run "_build/pod/podctl.py" prune <pod> [--yes]           # the old-layout folders under /workspace/packages

## Creating a pod from nothing (3.7.3)

`podctl create` is the first-pod path on a fresh account (before it, only `deploy --like <pod>` existed, so a first pod
needed the console or runpodctl). In order: the stock check (`stock` in `provider.py`: RunPod's GraphQL `dataCenters {
gpuAvailability { gpuTypeId stockStatus available } }`, the same view `runpodctl datacenter list` prints; REST v1 has no
stock call), which prints every data centre where the GPU is free now and stops when the chosen one (`--location`, or
the `--volume`'s) has none; `POST /networkvolumes` (`--data-gb` in that data centre) unless `--volume` names an existing
one or (3.14.0) a network volume called `--volume-name`, default `comfy-base-data`, already sits in a data centre with the
GPU free (`GET /networkvolumes`; such a data centre is chosen first, so models download once); `POST /pods` with `cloudType SECURE`, `dataCenterIds` [that one], the network volume at `/workspace`, ports
`8188/http 8888/http`, one `/http` port per declared app, `22/tcp` (3.9.0: named through the port-names template above),
`env.PUBLIC_KEY` (the Mac key), `env.JUPYTER_TOKEN` and (3.14.0) `env.BASE_LINK_TOKEN` (each fresh per pod), `containerDiskInGb` (`--os-gb`, default 100),
and `supportPublicIp`. 3.29.0: never `allowedCudaVersions`. RunPod matches that list exactly, REST v1's enum stops at
13.0, and every B200, B300 and RTX PRO 6000 host with stock reported CUDA 13.2 on 2026-10-26, so `["13.0"]` could never
place one. No list is any CUDA. The floor (`--min-cuda`, else the image tag's, `…-cuda13.0`, `…-cu130`, never below
13.0) is used twice instead: the stock check asks REST v2 `GET /catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE&minCudaVersion=<floor>`
(GraphQL's stock view has no CUDA input), and once the pod exists its host's `cudaVersion` is read from REST v2
`GET /pods/{id}` (asked again for about 60 s while it is null). Below the floor, or never named, the pod is STOPPED
(never deleted), a `STOP:` line says the version and the floor, and podctl exits 1. The helpers are importable:
`pod_cuda(api, pod_id)`, `wait_pod_cuda`, `check_pod_cuda(api, pod_id, floor)`, `catalog_stock(api, gpu, floor)`,
`cuda_at_least(version, floor)`. Without `--gpu` the stock
check asks for `NVIDIA B200`, then `NVIDIA B300 SXM6 AC`, and the first with stock where the pod must go is created. No templateId but the port-names one.
3.14.0: the pod is created with its start command already the boot guard plus sshd and NOTHING of its image's own
(`create_start`: `dockerEntrypoint` = `bash -c "<guard>; <sshd bootstrap>; exec sleep infinity"`). Before, it started on
the image's entry until `ensure` patched it, and an image like `runpod/comfyui` served its own ComfyUI on the public 8188
link with no key for the whole install (MEASURED 2026-10-16). Now nothing answers there until the install starts ComfyUI
on 127.0.0.1 behind the key's front, the base installs ComfyUI itself (as on `runpod/base`), `ensure` resets nothing,
and the install's `boot.sh --apps` starts JupyterLab with the pod's token, so the Jupyter Terminal link works at once. A refused pod (sold out between the check and the create) deletes the volume
the same call made, so nothing is left billing. `create` is a billable verb: a consumer's guard hook prompts for it.

`create --private` (3.7.4) is the shape for work that must not be reachable from a URL. It was the default from 3.8.1;
since 3.14.0 it is opt-in again (Paul: delight first, and the public links are now locked by the pod's key). It makes a POD volume
(`volumeInGb` = `--data-gb`) instead of a network volume and refuses `--volume`; exposes `22/tcp`
only; sets `BASE_LISTEN=127.0.0.1` in the pod's env (boot reads it; `install` passes it to its runs) so ComfyUI listens on
loopback, and with it JupyterLab and the app ports; sets no Jupyter token in the env (RunPod stores env in the clear), so
the boot generates one on the pod volume. No http port, so no port names and no template: `podctl tunnel <pod>`
forwards 8188, 8888 and the app ports and prints them as named localhost links, the terminal's with its token.
The volume is NOT encrypted, and `create` says so before the pod exists (3.9.1).
RunPod's storage docs: "Encryption applies only to volume disk. Container disk and network volumes cannot be encrypted",
and the one way they name is the console's "Encrypt volume" box at create time. `volumeEncrypted` is on REST v1's Pod
read-back ("can only be set when creating a Pod") but REST v1 `POST /pods` refuses it ("Extra input keys"; it is not in
`PodCreateInput`), REST v2 has no encryption field, runpodctl 3.0.1 no flag, and GraphQL's `podFindAndDeployOnDemand`
input has only an undocumented `volumeKey` string (not sent: unverified, and RunPod says bring-your-own-key is not
supported). The summary prints what the read-back says. A pod that must be encrypted is made in the console. The trade:
the data lives on that machine (stop/start keeps it, terminate deletes it, and a start can wait for that machine's GPU).
And money: RunPod's billing docs say that when the balance reaches $0 "those with a network volume are preserved, while
those without one are terminated and their data cannot be recovered", so `create` says that up front too; keep the
balance topped up (auto-pay or a low-balance alert) while a private pod holds work.

## The two shapes of a pod's volume

REST v1 has answered with a top-level `networkVolumeId` (pods read on 2026-09-05) and, in its current openapi, names
only the `networkVolume` object, which comes back when the read asks `includeNetworkVolume=true` (pods made by runpodctl
v3 or the console, read on 2026-10-15). Every pod read asks for it (and for `includeMachine`), and `pod_volume_id`
accepts either shape; `deploy` takes the data centre from `networkVolume.dataCenterId`. A pod with neither but
`volumeInGb` > 0 has a pod volume, and its size goes to `state/volume.env` as `pod-volume:<pod id>`.

`--provider runpod` is the default when the repository's `brand.toml` names no other host. The handbook's section 1
("Step zero") and section 7 ("State on the pod") carry the details this page leaves out.
