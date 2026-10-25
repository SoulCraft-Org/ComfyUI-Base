# podctl — the Claude-driven pod path (Mac side)

    uv run "base/comfyui-base/_build/pod/podctl.py" pods
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> [--data-gb 500] [--gpu <type>] [--location EU-RO-1] [--volume <id> | --private]   # RunPod: public, links locked by the pod's key; B200 then B300
    uv run "base/comfyui-base/_build/pod/podctl.py" status <pod>        # facts; env NAMES and placeholder verdicts, never values
    uv run "base/comfyui-base/_build/pod/podctl.py" image <pod>         # the image's entrypoint/cmd and how ensure wraps them
    uv run "base/comfyui-base/_build/pod/podctl.py" ensure <pod>        # TCP 22 · PUBLIC_KEY · the base's boot as start command · Host runpod
    uv run "base/comfyui-base/_build/pod/podctl.py" upload <file>...    # to /workspace/packages, size + sha256 checked both sides; a file already there is not sent
    uv run "base/comfyui-base/_build/pod/podctl.py" stop|start|restart <pod> [--wait]

The API key comes from `RUNPOD_API_KEY`, else (3.19.0, on a Mac) the login Keychain item whose account is
`RUNPOD_API_KEY`, else runpodctl's `~/.runpod/config.toml` (`runpodctl config --apiKey=…`, run once by the user). Verda's
and Crusoe's pairs follow the same order, each pair whole from one place. `BASE_KEYCHAIN=0` skips the Keychain. A key
rides a bearer header and is never printed or placed in an argument. `create` on RunPod references the account's
Secrets `HF_TOKEN` and `CIVITAI_TOKEN` in the new pod's env when they exist (names only; RunPod puts the values in).

`ensure` is idempotent. It exposes `22/tcp` when missing, sets `PUBLIC_KEY` to `~/.ssh/id_ed25519.pub`, waits for the
pod and its port mapping, probes for an ssh banner (a mapped port proves nothing on images that run no sshd), and when
nothing answers wraps the image's own entry in a start command: `boot.sh` when the base has been installed on the
volume, else the sshd bootstrap from `lib/boot.sh --print-sshd-bootstrap` followed by the image's original command.
Then it writes only the `HostName`/`Port` lines of the `Host runpod` block and runs `ssh runpod hostname`.

Tests: `base/comfyui-base/suite/test_podctl.py` (unit tier) against a fake RunPod REST server, a fake ssh banner and stubbed
`sshd`/`ssh-keygen`/`apt-get`/`scp`/`ssh`. Nothing in the suite touches the network or the user's ssh config.

## install — the one command (2.0.14)

    uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg "<brand>/packages/<name>" [--pkg …] [--every 30] [--timeout 10800] [--comfy-ref <ref>] [--no-save-pins] [--no-suite] [--env KEY=VALUE …]

First every zip: `package.py --check` locally (a stale zip stops before anything is uploaded), then the volume size
recorded for the disk gate and ONE upload of every zip (3.24.0: a probe, one scp, one size + sha256 check; a zip already
on the machine with the same size and sha256 is not sent again). Then for each item in order: extract, `--check` on the pod (non-zero stops), the real run detached (3.0.0:
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

    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> --data-gb 500 [--location EU-RO-1]          # public (the default): comfy-base-data reused, else made
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img>                                             # public, only where comfy-base-data already is
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> --volume <network volume id>                # public, on that volume
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --image <img> --data-gb 500 --private                      # opt-in: ssh only, a pod volume
    uv run "base/comfyui-base/_build/pod/podctl.py" create --name <n> --gpu "NVIDIA H200" --image <img> --data-gb 500   # one named type instead

Without `--gpu` (3.8.0) RunPod asks for `NVIDIA B200`, then `NVIDIA B300 SXM6 AC`, the order the video packages'
`GPU_PREFER` names, and creates the first with stock where the pod must go (the `--location`, the `--volume`'s data
centre, else the first data centre that has it); a host with no default order (Verda) still needs `--gpu`.

The stock check runs first and prints every data centre where the GPU is free now (RunPod's GraphQL `dataCenters`;
REST v1 has no stock call). A `--location` (or the `--volume`'s own data centre) without stock stops there, naming the
ones that have it, before anything is created. Then a network volume of `--data-gb` in that data centre (or the existing
`--volume`), then a Secure Cloud pod on it: the image, ports `8188/http 8888/http 22/tcp`, `PUBLIC_KEY` from the Mac key,
`--os-gb` of container disk (default 100), and `allowedCudaVersions` from `--min-cuda`, else the image tag (`cuda13.0`,
`cu130` → 13.0 and newer) never below 13.0 (3.8.0: the base installs a cu130 torch whatever the image was built for). A refused pod deletes the volume that same call created; an existing volume is never deleted.
`--wait` is the default (until sshd answers, then `Host runpod` → the new pod); then `podctl ensure <pod>`. `create` bills:
a consumer's guard hook prompts for it like `deploy`.

**Public is the default again (3.14.0)**, on RunPod. Paul's order (2026-10-16) for everything a person touches is
delight first (easy, simple: named links in the Connect tab), then secure, then private, and security may not cost a
click. So a plain `create` makes the network-volume pod above, with ComfyUI, the Jupyter Terminal and each app named in
the Connect tab, and every one of those links locked:

- **Open by default (3.19.1).** Since 3.19.1 the links are OPEN by default: one click on the Connect tab opens ComfyUI, each app and the Jupyter Terminal (no key, no token), because RunPod's proxy passes nothing that names the pod's owner (its docs: the service "becomes publicly accessible"; "implement proper authentication in your application"), so any login is a step. The key below, and JupyterLab's token on the public bind, are the opt-in: `BASE_LINK_LOCK=1` in the pod's env (`podctl create --lock`).
- **The key (opt-in, `--lock`).** `create --lock` puts a fresh per-pod `BASE_LINK_TOKEN` in the pod's env beside `JUPYTER_TOKEN`, and prints
  every link ready to click: ComfyUI and each app as `https://<pod>-<port>.proxy.runpod.net/?token=<key>`, the terminal
  as `…-8888…/lab?token=<jupyter token>`. On the machine `py/app_proxy.py` answers the key with a cookie (HttpOnly,
  Secure, SameSite=Lax, that host only) and a redirect to the same URL without it, so after that one click the
  Connect tab's own links open straight away in that browser. A request with neither gets a plain 401 page. ComfyUI
  itself listens on 127.0.0.1 behind it; JupyterLab keeps its own token. `status <pod>` prints the links again.
- **Models download once.** With no `--volume`, a public pod goes onto the network volume called `--volume-name`
  (default `comfy-base-data`) in a data centre that has it AND has the GPU free; a data centre without one gets a new one
  of `--data-gb` under that name, which the next `create` then reuses. With no `--data-gb` the pod is made only where
  the volume already is, and `create` stops, naming both ways out, when none of those data centres has the GPU.

`--private` is the opt-in it was before 3.8.1 made it the default; `--public` is still accepted and changes nothing;
both together are refused. A host whose create has no private mode (Verda) is unchanged; `--private` there is refused
by name.

`--private` (3.7.4): a **pod volume** of `--data-gb` at `/workspace` (`volumeInGb`; `--volume` is refused), `22/tcp` the
only port (no proxy URL, no JupyterLab), and `BASE_LISTEN=127.0.0.1` in the pod's env so ComfyUI binds loopback;
`podctl tunnel <pod>` is the way in (http://localhost:8188), and `install` carries that `BASE_LISTEN` into its runs. The
volume is the machine's: kept across stop/start, deleted with the pod.

**The volume is not encrypted (3.9.1).** RunPod encrypts a pod's volume disk only when the console's "Encrypt volume"
box is ticked at create time. No create input RunPod documents can ask for it: REST v1 `POST /pods` refuses
`volumeEncrypted` ("Extra input keys", it is on the Pod read-back only), REST v2 and runpodctl 3.0.1 have no such field,
and GraphQL's deploy inputs carry only an undocumented `volumeKey` string. So `create` says so before the pod exists,
and its summary prints what the read-back says (`ENCRYPTED pod volume`, `pod volume … (not encrypted)`, or
`(encryption not reported)`). An encrypted pod is made in the console; podctl then works on it like any other pod.

## tunnel

    uv run "base/comfyui-base/_build/pod/podctl.py" tunnel <pod> [--port 8188] [--port 8888]

The one ssh session that forwards ports (keepalives on, a taken port fails loudly). Every other podctl ssh/scp call
clears forwards, so a running tunnel never makes them print "bind: Address already in use".

3.24.0: those other calls share ONE connection per machine (ControlMaster under `~/.cache/podctl/ssh/`, kept 120 s after
the last command; `stop` and `restart` close it first). A handshake to a pod is about 11 round trips; a multiplexed
command is 2. The master is opened with ClearAllForwardings, so it never binds the Host block's forwards; the tunnel
never uses it. `PODCTL_SSH_MUX=0` turns it off.


## runner — the machine as a free GitHub self-hosted runner while it is up (3.16.0, opt-in)

    uv run "base/comfyui-base/_build/pod/podctl.py" runner <pod> [--scope <org> | <owner>/<repo>] [--labels gpu-pod] [--group <name>]
    uv run "base/comfyui-base/_build/pod/podctl.py" runner <pod> --remove

Self-hosted minutes cost nothing, so a machine that is up for real work can also take a repository's GPU job
(`runs-on: [self-hosted, gpu-pod]`). `runner` registers it as an **ephemeral** runner named `gpu-pod-<pod id>`: it takes
one job, then GitHub removes it; run it again for the next. The scope is `--scope`, else `$PODCTL_RUNNER_SCOPE`, else the
GitHub owner of the consumer root's `origin` (an organisation-wide runner; the organisation's runner group decides
which repositories may use it, and a group that refuses public repositories keeps forks away).

- **The token.** This Mac's `gh` mints a one-hour registration token (`POST …/actions/runners/registration-token`; the
  login must administer the scope). It is held in memory, written to ssh's stdin, read by the machine into the
  runner's environment (`ACTIONS_RUNNER_INPUT_TOKEN`, which `config.sh` reads in place of `--token`) and unset after
  configuration. Never in argv, a log, a file or the output.
- **The machine.** actions/runner 2.338.0 (x86_64), the tarball cached under `/workspace/.cache/gh-runner` and checked
  against its published SHA-256 on every use; the runner itself in `/opt/gh-runner` on the container disk, so a stopped
  machine keeps nothing. It runs as root (`RUNNER_ALLOW_RUNASROOT=1`), with no self-update.
- **Removal.** `stop`, `pause` and `put-away` first remove the runner through GitHub's API when this Mac registered one
  (a mark in `~/.cache/podctl/runners/`, holding the scope's API path only); a machine that never ran one stops as
  before, with no call to GitHub. `--remove` does it on demand.
- **The GPU.** A job on the machine reaches the GPU only through the machine's own ComfyUI (`127.0.0.1:8188`, and on a
  multi-GPU pod each server in `state/servers.list`, 3.20.0), never as a second GPU process beside one; `runner` refuses while another session holds the GPU lease (`--force-lease`).
