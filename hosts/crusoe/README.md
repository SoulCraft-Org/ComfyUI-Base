# hosts/crusoe : Crusoe Cloud

**Status: implemented, not yet run on a live account.** `provider.py` is a signed REST client with the whole
Provider interface behind it, and `suite/test_crusoe.py` drives all of it against a fake Crusoe API that verifies
every signature. What has NOT happened is a single call to the real thing.

That distinction matters, because it is exactly where the Verda host sat before 2.5.3, and its first live run
corrected three things the documentation had implied: the ssh user, whether the startup script re-runs on a
redeploy, and how long an address takes to appear. Expect this host to have three of its own.

**If you are the first to run it**, the useful things to report are: whether the signature is accepted (it is
built from Crusoe's own published client, but no live 200 has confirmed it), what `GET /organizations/projects`
answers for a single-project key, the exact state strings the API uses (the normaliser treats anything it does not
know as transitional), how long a started VM takes to show a public ip, and whether the disk attaches before the
startup script's 15 minute wait expires. Open a host report; the issue template asks for these.

## The shape of it

One VM, one persistent disk, one startup script. The VM's image carries the NVIDIA driver; the startup script runs as
root on EVERY boot and cannot be changed through the API afterwards (edit it over ssh under `/usr/local/bin/crusoe/`,
or recreate the VM). The disk is zonal, formatted once and mounted at `/workspace` by UUID from fstab. The login user
is `ubuntu`, on port 22, with the key you gave at create time. Crusoe's firewall is default-deny inbound with 22 open,
so ComfyUI is reached through an ssh tunnel and binds loopback (`BASE_LISTEN=127.0.0.1`).

| | |
|---|---|
| instance types | `a100-80gb.1x` (80 GB, no NVENC, not RTX), `l40s-48gb.1x` (48 GB, RTX with NVENC), `h100-80gb-sxm-ib.8x` |
| image | `ubuntu22.04-nvidia-pcie-docker:latest` |
| disk | a persistent disk sized for the models your packages declare (a large video package is 60 to 130 GB), ext4 at `/workspace` |
| billing | per second while running; a stopped VM bills its disks only |
| knobs | `BASE_HOST=crusoe`, `BASE_VOLUME=/workspace`, `BASE_LISTEN=127.0.0.1`, `BASE_VOLUME_KIND=mount` |

## The recipe, by hand, with the `crusoe` CLI

Install the CLI (`brew install crusoecloud/cli/crusoe`) and log in with an access key id and secret. Then:

    # 1. the disk, in the zone the VM will use
    crusoe storage disks create --name comfy-workspace --size 400GiB --location us-east1-a --block-size 4096

    # 2. the VM, with this folder's startup script as its create-time startup script
    crusoe compute vms create --name comfy-1 --type a100-80gb.1x --location us-east1-a \
        --image ubuntu22.04-nvidia-pcie-docker:latest --keyfile ~/.ssh/id_ed25519.pub \
        --startup-script hosts/crusoe/startup.sh

    # 3. attach the disk (the startup script waits up to 15 minutes for it)
    crusoe compute vms attach-disks comfy-1 --disk name=comfy-workspace,mode=read-write

    # 4. the address
    crusoe compute vms get comfy-1

The startup script must stay plain ASCII and under 64 KB (Crusoe's limits on the field). On the first boot it formats
the disk, checks the driver (580 or newer; installs it from NVIDIA's repository and reboots once when older), installs
`build-essential` and the CUDA 13 toolkit, installs and enables `comfy-base-boot.service` (the base's own unit text
once the base is installed; a matching bootstrap copy before that, which sleeps until `boot.sh` exists), and writes
`/workspace/comfy-base/state/host.env`. On every later boot it mounts from fstab and confirms all of that in seconds;
after the first `podctl install --base` it restarts the unit so `boot.sh` replaces the pre-install sleep.
Watch it with `ssh ubuntu@<ip> sudo tail -f /var/log/comfy-base-startup.log`; its last status line is also in
`/workspace/comfy-base/state/crusoe-startup.status`. To run the stages again by hand: `sudo bash startup.sh --ensure`.

The public IP is dynamic and changes on every stop/start unless the VM was created with `--public-ip-type static`.

## The install and the tunnel

The base and the packages are installed over ssh from the Mac, like on every other host:

    uv run "_build/pod/podctl.py" --provider crusoe install comfy-1 --base --pkg <package dir>

Until the provider is implemented that command fails with its "not implemented yet" line; the by-hand equivalent is
to scp the zips to `/workspace/packages/` as `ubuntu` (the startup script gives that user the volume) and run each
`<name>-script.sh` there. `boot.sh` then runs under the unit at every boot: no `PUBLIC_KEY` is needed, because
Crusoe's own sshd and your console key are the way in, and `boot.sh` leaves a running sshd alone.

Port 8188 is closed by the firewall, on purpose (ComfyUI has no login), so the browser goes through the tunnel:

    uv run "_build/pod/podctl.py" --provider crusoe tunnel comfy-1      # once implemented
    ssh -N -L 8188:localhost:8188 -L 8888:localhost:8888 ubuntu@<public ip>   # today

then `http://localhost:8188`. JupyterLab starts only when `JUPYTER_TOKEN` reaches `boot.sh`; on this host that means a
systemd drop-in for the unit, which nothing writes yet.

## What the driver will do once implemented

- credentials: `CRUSOE_ACCESS_KEY_ID` + `CRUSOE_SECRET_KEY` in the environment, else (3.19.0, on a Mac) the login
  Keychain items whose accounts are those two names (`BASE_KEYCHAIN=0` skips the lookup), else `~/.crusoe/config`
  (`access_key_id`, `secret_key`, `default_project`). A pair counts only when both halves come from the same place;
  the project is `CRUSOE_PROJECT`, else the file's.
- `pods`, `status`: list and read VMs through the signed REST client (or `crusoe ... --json`), normalising the state
  case-insensitively (RUNNING; STOPPED or SHUTOFF; anything else transitional) and reading the public IP from
  `network_interfaces[0].ips[0].public_ipv4.address`.
- `ensure`: no API step; check that sshd answers on `<ip>:22` as `ubuntu`, write the `Host crusoe` block, confirm
  `state/host.env`.
- `start`, `stop`, `restart`: `PATCH .../instances/{id}` with `START`, `STOP` or `RESET`, polling the returned
  operation until its state is no longer `IN_PROGRESS`; `start` rewrites the Host block because the IP changed.
- `deploy --like`: a new VM with the source's type, image, zone, key and startup script; the disk moves with
  `detach-disks` then `attach-disks` (one read-write attacher, same zone).
- `install`, `upload`, `lease`, `tunnel`, `prune`: the driver's generic code, unchanged.

Stop the VM when you are done: `crusoe compute vms stop comfy-1`. The disk keeps everything for the next start.
