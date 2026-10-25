# hosts/ : the per-host subsets of ComfyUI Base

The core of the base (`lib/`, `py/`, `suite/`) is host-neutral: it runs on any NVIDIA GPU on any Linux that has one
persistent volume root. What differs between hosts is how a machine is created, how it is reached, and how its boot is
handed to the base. That lives here, one folder per host, behind one interface (`hosts/__init__.py`, class `Provider`).

The core reads four knobs and nothing else about its host:

| knob | meaning | how it is set |
|---|---|---|
| `BASE_HOST` | `runpod`, `verda`, `crusoe`, `local` or `vm` | detected from PID 1's `RUNPOD_POD_ID` on RunPod; else read from `<volume>/comfy-base/state/host.env`, which the driver writes; else `vm` |
| `BASE_VOLUME` | the persistent root | default `/workspace` |
| `BASE_LISTEN` | the address ComfyUI and JupyterLab bind | `0.0.0.0` only on RunPod, whose HTTP proxy needs it; `127.0.0.1` everywhere else, reached through the driver's ssh tunnel |
| `BASE_VOLUME_KIND` | `mount` or `dir` | `mount` on every cloud host; `dir` on a local box whose volume root is a plain directory |

On a VM host the base installs a systemd unit, `comfy-base-boot.service`, that runs `<volume>/comfy-base/boot.sh` at
every boot. RunPod has no systemd and keeps its start-command wrap. The Mac-side driver is
`_build/pod/podctl.py --provider <host> <command>`; each provider lives in `hosts/<host>/provider.py`.

## The matrix

| | runpod | verda | crusoe | local |
|---|---|---|---|---|
| machine created by | RunPod REST | Verda REST | Crusoe REST or CLI | by hand |
| the base's boot handed over by | start command wrap | first-boot startup script + systemd unit | every-boot startup script + systemd unit | the unit, or by hand |
| ssh user and port | root, mapped port | root, 22 | ubuntu, 22 | yours |
| cloud firewall | RunPod proxy | none, loopback only | default-deny with 22 open | your LAN |
| the volume | network volume at `/workspace` | block volume you mkfs and mount at `/workspace` | persistent disk you mkfs and mount at `/workspace` | a directory |
| `BASE_HOST` | `runpod` | `verda` | `crusoe` | `local` |
| `BASE_VOLUME` | `/workspace` | `/workspace` | `/workspace` | the directory (default `$HOME/comfy`) |
| `BASE_LISTEN` | `0.0.0.0` | `127.0.0.1` | `127.0.0.1` | `127.0.0.1` |
| `BASE_VOLUME_KIND` | `mount` | `mount` | `mount` | `dir` |
| status | proven on live pods | EXPERIMENTAL until the first live account proves it | interface only, not implemented | recipe |

## How the driver picks a host

`podctl` binds one provider per run: `--provider <host>` (before or after the command), else `$PODCTL_PROVIDER`, else
the one `host = "..."` of the `brand.toml` files in the repository this base sits in (two brands on two hosts make
`--provider` required), else `runpod`. An experimental provider says so once per run on stderr.

`PODCTL_TUNNEL_OFFSET` (default 0) shifts every local port the driver forwards: ComfyUI 8188, JupyterLab 8888,
9199 and each app port all move together. With one GPU per workflow every machine serves 8188, so the second machine of
a fleet needs an offset or its tunnel silently reaches the first one. It is a fact about your fleet, not about
the base or the host, so it lives beside `PODCTL_HOST` in your environment: `export PODCTL_HOST=gpu-two` and
`export PODCTL_TUNNEL_OFFSET=1`. A value that is not a whole number in 0-1000 is refused rather than read as 0,
because 0 is the collision it was set to avoid.

`PODCTL_TUNNEL_BASE` (unset by default) is for a workstation that keeps its local listeners in a block of its own
and the vendors' numbers (8188, 8888, 9199) empty, which no offset can reach. Set it to the first port of a ten-port
run inside your block and every local side moves there; the remote ports stay where they are. Machine N of the fleet
(`PODCTL_TUNNEL_OFFSET=N`) takes `base+10N` to `base+10N+9`:

| local | remote | what |
|---|---|---|
| `base+10N` | 8188 | ComfyUI |
| `base+10N+1` | 8888 | JupyterLab |
| `base+10N+2` | 9199 | a package's metrics exporter, if it runs one |
| `base+10N+3` … `+9` | every other port, lowest first | the machine's apps, or the ports `--port` names |

Pick a base that leaves room for every machine: each machine takes 10 ports (base+10N … base+10N+9), so the whole
fleet must fit inside your account's block, e.g. base 3150 for up to 5 machines in 3100–3199. A base like 3180 with a
third machine would run into the next account's block. `export PODCTL_TUNNEL_BASE=3150` puts the first machine at
3150-3159 and the second at 3160-3169. The ssh-config
block, `podctl tunnel`, `jupyter`, `mcp --over tunnel` and `create`'s hint all print the same numbers. A value that
is not a whole number, or that would put a machine's ten ports outside 1024-65535, is refused rather than ignored,
and so is an eleventh port. `--port LOCAL:REMOTE` still picks a local side by hand. 11434 (Ollama's) has not been
forwarded since 3.23.0, because no package runs an inference server on the machine; `--port 11434` forwards it, into
the block, if one does. `PODCTL_HOST` still
gives a session its own `Host <alias>` block; without it the alias is the provider's own name, so each host keeps its
own block in `~/.ssh/config`.

## Where each host is documented

- `hosts/runpod/README.md`: the RunPod recipe as it works today (the API key, `PUBLIC_KEY`, the proxy, the commands).
- `hosts/verda/README.md`: Verda, and what the live runs of 2026-09-17 and 2026-09-18 answered.
- `hosts/crusoe/README.md`: Crusoe, the recipe and what a first live run should report; `hosts/crusoe/provider.py`
  is a signed REST client with the whole interface behind it, exercised against a fake API but not yet against a
  live account; `hosts/crusoe/startup.sh` is the VM's every-boot startup script.
- `hosts/local/README.md`: an owned NVIDIA box; `hosts/local/install-local.sh` is the one-command install.

The handbook (`comfyui-base-handbook.md`) carries the rest: the install sequence, the lease, the pins, the state files.
