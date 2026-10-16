# hosts/runpod : RunPod, the proven host

A RunPod pod is a container. Its start command is what runs at boot, its persistent storage is a network volume mounted
at `/workspace`, and it is reached through a public IP with a mapped ssh port and through RunPod's HTTP proxy. This
folder's `provider.py` is the RunPod half of the driver, moved here verbatim in base 2.2.0.

## What you need on the Mac

- **The API key.** `RUNPOD_API_KEY` in the environment, else `~/.runpod/config.toml` as written by
  `runpodctl config --apiKey=<key>`. It is sent as a bearer header and is never printed, logged or put in an argument.
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

## How the boot is handed over

`podctl ensure <pod>` wraps the image's own start command with the boot guard:

    bash -c "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi; <sshd bootstrap>; exec <the image's own entry>"

Before the base is installed that boots the image as it was, plus sshd. Afterwards the base's `boot.sh` is PID 1:
sshd, JupyterLab (only with `JUPYTER_TOKEN` or `JUPYTER_PASSWORD` set), the saved tokens, provisioning, ComfyUI with
the base's launch line, then `sleep infinity`. The image's entry is read from Docker Hub's registry; pass
`--entry '<its start command>'` for an image the registry cannot describe. `ensure` is idempotent: a pod that already
carries the guard is not patched and its image is never fetched. RunPod has no systemd, so this wrap is the whole
mechanism; `BASE_HOST` is detected on the pod itself from PID 1's `RUNPOD_POD_ID`.

## The addresses

- **ssh: `<public ip>:<mapped port>`**, user `root`. The mapped port changes on every start, which is why `start --wait`,
  `restart --wait` and `deploy --wait` rewrite the `Host runpod` block in `~/.ssh/config` (HostName and Port only; every
  other byte of the file stays). `ssh-config <pod>` rewrites it by hand.
- **HTTP: RunPod's proxy**, `https://<pod id>-8188.proxy.runpod.net` for ComfyUI and `-8888` for JupyterLab. The proxy
  reaches the container from outside, so ComfyUI and JupyterLab must bind `0.0.0.0` on this host: `BASE_LISTEN=0.0.0.0`,
  the one host where it is not loopback. JupyterLab on a proxy URL is root on the box; it starts only with a token.
- **The tunnel**, `podctl tunnel <pod>`: the ONE ssh session that forwards 8188 and 8888 to `localhost` (`--port` adds
  more). Use it for the canvas tools and the browser when the proxy is not wanted; command sessions carry no forwards,
  so nothing else fights the tunnel for the ports.

## The commands

    uv run "_build/pod/podctl.py" pods                          # every pod: id, name, image, status, public ip, port 22
    uv run "_build/pod/podctl.py" create --name <n> --gpu "NVIDIA B200" --image <img> --data-gb 500 [--location <dc> | --volume <id>]
                                                                # stock check (names the data centres with the GPU free now), network volume, Secure Cloud pod
    uv run "_build/pod/podctl.py" status <pod>                  # facts; env NAMES and placeholder verdicts, never values; the GPU verdict
    uv run "_build/pod/podctl.py" ensure <pod>                  # 22/tcp, PUBLIC_KEY, the boot guard, Host runpod
    uv run "_build/pod/podctl.py" install <pod> --base --pkg <dir>...   # upload, extract, --check, run (everything newest), poll, logs, pack records
                                                                # --no-suite (BASE_NO_SUITE=1 on the machine), --env KEY=VALUE (repeatable)
    uv run "_build/pod/podctl.py" stop|start|restart <pod> --wait
    uv run "_build/pod/podctl.py" deploy --like <pod> --wait    # a NEW pod cloned onto the same volume (a machine whose GPU is not ours)
    uv run "_build/pod/podctl.py" lease <pod> [--take|--release] [--minutes N] [--purpose ...]   # who holds the GPU; advisory, expires
    uv run "_build/pod/podctl.py" tunnel <pod>                  # 8188 and 8888 to localhost, until killed
    uv run "_build/pod/podctl.py" prune <pod> [--yes]           # the old-layout folders under /workspace/packages

## Creating a pod from nothing (3.7.3)

`podctl create` is the first-pod path on a fresh account (before it, only `deploy --like <pod>` existed, so a first pod
needed the console or runpodctl). In order: the stock check (`stock` in `provider.py`: RunPod's GraphQL `dataCenters {
gpuAvailability { gpuTypeId stockStatus available } }`, the same view `runpodctl datacenter list` prints; REST v1 has no
stock call), which prints every data centre where the GPU is free now and stops when the chosen one (`--location`, or
the `--volume`'s) has none; `POST /networkvolumes` (`--data-gb` in that data centre) unless `--volume` names an existing
one; `POST /pods` with `cloudType SECURE`, `dataCenterIds` [that one], the network volume at `/workspace`, ports
`8188/http 8888/http 22/tcp`, `env.PUBLIC_KEY` (the Mac key), `containerDiskInGb` (`--os-gb`, default 100),
`supportPublicIp`, and `allowedCudaVersions`: REST v1's field (GraphQL's is `minCudaVersion`), every version from
`--min-cuda`, else from the image tag (`…-cuda13.0`, `…-cu130`), upward. No templateId and no start command: `ensure`
installs the boot guard as for every pod. A refused pod (sold out between the check and the create) deletes the volume
the same call made, so nothing is left billing. `create` is a billable verb: a consumer's guard hook prompts for it.

`create --private` (3.7.4) is the shape for work that must not be reachable from a URL. RunPod's storage docs: "Encryption
applies only to volume disk. Container disk and network volumes cannot be encrypted." So it makes an encrypted POD volume
(`volumeInGb` = `--data-gb`, `volumeEncrypted: true`) instead of a network volume and refuses `--volume`; exposes `22/tcp`
only; sets `BASE_LISTEN=127.0.0.1` in the pod's env (boot reads it; `install` passes it to its runs) so ComfyUI listens on
loopback; sets no Jupyter token, so the base starts no JupyterLab. `podctl tunnel <pod>` forwards 8188 to
`127.0.0.1:8188` on the pod. `volumeEncrypted` appears on REST v1's Pod ("can only be set when creating a Pod") but in
neither documented create input (REST `PodCreateInput`, GraphQL `podFindAndDeployOnDemand`; runpodctl v3 has no flag
for it), so `create` reads the pod back: `false` deletes the seconds-old pod and fails, an answer without the field is
kept, named, and fails. The trade: the data lives on that machine (stop/start keeps it, terminate deletes it, and a
start can wait for that machine's GPU).

## The two shapes of a pod's volume

REST v1 has answered with a top-level `networkVolumeId` (pods read on 2026-09-05) and, in its current openapi, names
only the `networkVolume` object, which comes back when the read asks `includeNetworkVolume=true` (pods made by runpodctl
v3 or the console, read on 2026-10-15). Every pod read asks for it (and for `includeMachine`), and `pod_volume_id`
accepts either shape; `deploy` takes the data centre from `networkVolume.dataCenterId`. A pod with neither but
`volumeInGb` > 0 has a pod volume, and its size goes to `state/volume.env` as `pod-volume:<pod id>`.

`--provider runpod` is the default when the repository's `brand.toml` names no other host. The handbook's section 1
("Step zero") and section 7 ("State on the pod") carry the details this page leaves out.
