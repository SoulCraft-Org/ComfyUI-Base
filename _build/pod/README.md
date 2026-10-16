# podctl — the Claude-driven pod path (Mac side)

    uv run "base/comfyui-base/_build/pod/podctl.py" pods
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --gpu "NVIDIA B200" --image <img> --data-gb 500 [--location EU-RO-1 | --volume <id>]
    uv run "base/comfyui-base/_build/pod/podctl.py" status <pod>        # facts; env NAMES and placeholder verdicts, never values
    uv run "base/comfyui-base/_build/pod/podctl.py" image <pod>         # the image's entrypoint/cmd and how ensure wraps them
    uv run "base/comfyui-base/_build/pod/podctl.py" ensure <pod>        # TCP 22 · PUBLIC_KEY · the base's boot as start command · Host runpod
    uv run "base/comfyui-base/_build/pod/podctl.py" upload <file>...    # scp to /workspace/packages, size + sha256 checked both sides
    uv run "base/comfyui-base/_build/pod/podctl.py" stop|start|restart <pod> [--wait]

The API key comes from `RUNPOD_API_KEY`, else runpodctl's `~/.runpod/config.toml` (`runpodctl config --apiKey=…`,
run once by the user). It rides a bearer header and is never printed or placed in an argument.

`ensure` is idempotent. It exposes `22/tcp` when missing, sets `PUBLIC_KEY` to `~/.ssh/id_ed25519.pub`, waits for the
pod and its port mapping, probes for an ssh banner (a mapped port proves nothing on images that run no sshd), and when
nothing answers wraps the image's own entry in a start command: `boot.sh` when the base has been installed on the
volume, else the sshd bootstrap from `lib/boot.sh --print-sshd-bootstrap` followed by the image's original command.
Then it writes only the `HostName`/`Port` lines of the `Host runpod` block and runs `ssh runpod hostname`.

Tests: `base/comfyui-base/suite/test_podctl.py` (unit tier) against a fake RunPod REST server, a fake ssh banner and stubbed
`sshd`/`ssh-keygen`/`apt-get`/`scp`/`ssh`. Nothing in the suite touches the network or the user's ssh config.

## install — the one command (2.0.14)

    uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg "<brand>/packages/<name>" [--pkg …] [--every 30] [--timeout 10800] [--comfy-ref <ref>] [--no-save-pins] [--no-suite] [--env KEY=VALUE …]

For each item in order: `package.py --check` locally (a stale zip stops before anything is uploaded), the volume size
recorded for the disk gate, upload, extract, `--check` on the pod (non-zero stops), the real run detached (3.0.0:
everything to its newest; `BASE_RESTART=1` for a package; `COMFY_REF=<ref>` with `--comfy-ref`, that install only), polled
until `STEP RC=` (progress = the run's section headers), the pod's `state/logs` copied to
`<item>/_build/podruns/<date>/pod-logs/`, the summary block printed. Green → the pack records it printed (each pack's HEAD)
are saved into the local script and its zip rebuilt (commit both). "restart required" (a new NVIDIA driver) → stop with
the `podctl restart` to approve, then run the same install again. Red → stop with the console's path. Exit 0 only when
every item is green. `--no-latest` is retired and refused.

3.7.3: `--no-suite` sets `BASE_NO_SUITE=1` for the base step (no base suite on the machine) and for every package run
(no package suite at the end); the Mac gate still runs (`--no-gate` is the separate switch for that). `--env KEY=VALUE`
(repeatable) prefixes every run on the machine with that variable, `install-self` and the preflight included; the names
are printed, the values never.

## create — a pod from nothing (3.7.3, RunPod; Verda since 3.1.0)

    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --gpu "NVIDIA B200" --image <img> --data-gb 500 [--location EU-RO-1]
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --gpu "NVIDIA B200" --image <img> --volume <network volume id>

The stock check runs first and prints every data centre where the GPU is free now (RunPod's GraphQL `dataCenters`;
REST v1 has no stock call). A `--location` (or the `--volume`'s own data centre) without stock stops there, naming the
ones that have it, before anything is created. Then a network volume of `--data-gb` in that data centre (or the existing
`--volume`), then a Secure Cloud pod on it: the image, ports `8188/http 8888/http 22/tcp`, `PUBLIC_KEY` from the Mac key,
`--os-gb` of container disk (default 100), and `allowedCudaVersions` from `--min-cuda` or the image tag (`cuda13.0`,
`cu130` → 13.0 and newer). A refused pod deletes the volume that same call created; an existing volume is never deleted.
`--wait` is the default (until sshd answers, then `Host runpod` → the new pod); then `podctl ensure <pod>`. `create` bills:
a consumer's guard hook prompts for it like `deploy`.

    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --gpu "NVIDIA B200" --image <img> --data-gb 500 --private

`--private` (3.7.4): an **encrypted pod volume** of `--data-gb` at `/workspace` (`volumeInGb` + `volumeEncrypted`; RunPod
cannot encrypt a network volume or a container disk, so `--volume` is refused), `22/tcp` the only port (no proxy URL, no
JupyterLab), and `BASE_LISTEN=127.0.0.1` in the pod's env so ComfyUI binds loopback; `podctl tunnel <pod>` is the way in
(http://localhost:8188), and `install` carries that `BASE_LISTEN` into its runs. `volumeEncrypted` is in no documented
create input, so the pod is read back: one that came back unencrypted is deleted at once and the run fails; one whose
answer does not say is kept and named. The volume is the machine's: kept across stop/start, deleted with the pod.

## tunnel

    uv run "base/comfyui-base/_build/pod/podctl.py" tunnel <pod> [--port 8188] [--port 8888]

The one ssh session that forwards ports (keepalives on, a taken port fails loudly). Every other podctl ssh/scp call
clears forwards, so a running tunnel never makes them print "bind: Address already in use".

