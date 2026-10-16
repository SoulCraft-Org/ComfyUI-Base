# hosts/verda: ComfyUI Base on a Verda (DataCrunch) VM

**PROVEN on a live account**, 2026-09-17 and 2026-09-18. What the live runs answered, in the order this file used
to ask:

1. **The ssh user is `root`** on port 22 of the public ip, as the docs said.
2. **The startup script does re-run** on a redeploy from an OS volume, and `/etc/fstab` survives on that volume, so
   a machine started from it re-mounts `/workspace` and any shared store by itself with no `ensure` needed.
3. **`ip` populates within about 30 s** of `POST /instances`, which answers the new id as a bare JSON string.

Exercised since: a shared filesystem created, attached to two instances at once and mounted as the whole
workspace; a 150 GB migration at 509 MB/s; a stop that keeps every volume and a start that redeploys from the OS
volume; and six volumes restored from the trash after a zero balance, byte for byte.

Everything below is the recipe for a person. The Mac side is `_build/pod/podctl.py --provider verda ...`; the
machine side is `hosts/verda/startup.sh` (mount the data volume, install the boot unit), and the base itself is
the same on every host.

## 1. Account and credentials

Verda authenticates with a client id and a client secret (OAuth2 client credentials, exchanged for a bearer
token at `POST /oauth2/token`). Make the pair in the console under API keys, then put it in ONE of these places:

- the environment: `export VERDA_CLIENT_ID=... VERDA_CLIENT_SECRET=...`
- (3.19.0, on a Mac) the login Keychain: two items whose accounts are `VERDA_CLIENT_ID` and `VERDA_CLIENT_SECRET` (any
  service name; `BASE_KEYCHAIN=0` skips the lookup)
- the file `~/.verda/credentials`, mode 600:

      client_id = your-client-id
      client_secret = "your-client-secret"

  Quoted or unquoted values are both read; `#` lines are comments. They are tried in that order, and a pair counts
  only when both halves come from the same place. The pair is never printed, logged or placed in
  an argument or a URL: it travels once, in the JSON body of the token request, and the token travels as
  `Authorization: Bearer`.

The base URL is `https://api.verda.com/v1`; `VERDA_API_URL` overrides it (the suite points it at a fake).

## 2. The ssh key

The provider's key is `~/.ssh/verda_comfyui`. Make it once:

    ssh-keygen -t ed25519 -f ~/.ssh/verda_comfyui -C comfyui-mac

`podctl ensure` registers the `.pub` with Verda (`POST /sshkeys`, name `comfyui-mac`) when no registered key
equals it, and every `start` and `deploy` passes the matching key ids. A machine deployed from the console
before the key was registered has no way to receive it after the fact except the console: add it there, or
redeploy.

## 3. Instance type, image, location, and the data volume

- **Instance type:** an RTX PRO 6000 class card, 1x. `GET /instance-types` lists the exact names; put the one
  you use in `VERDA_INSTANCE_TYPE` (the provider needs it when it redeploys from an OS volume, because a volume
  does not remember the machine it was on). `GET /instance-availability/<type>` says whether one is free.
- **Image:** `ubuntu-24.04-cuda-13.0-open-docker` (Ubuntu 24.04, CUDA 13.0, the open kernel module, Docker).
  `GET /images` lists the rest. The base needs nothing from the image but a driver and Python 3.
- **Location:** one of `FIN-01`, `FIN-02`, `FIN-03`, `ICE-01` (`GET /locations`). The instance and every volume it
  uses must be in the same location; pick one and stay in it.
- **The data volume:** an NVMe volume in that location, sized for the models you will keep (500 GB is a
  reasonable first size), named with the machine's name plus `-data`: `comfy-base-data` beside an instance
  `comfy-base`. The name matters: when the machine is stopped, `podctl start <os volume id>` attaches every
  detached data volume whose name shares the OS volume's prefix. `startup.sh` finds it as the one unmounted
  `/dev/vd[b-z]`, formats it ext4 (label `comfy-volume`) when blank, and mounts it at `/workspace` by UUID with
  `nofail`. A disk that already carries a filesystem is kept, never reformatted. Attach exactly ONE data disk;
  with two the script refuses to choose (set `COMFY_DATA_DISK=/dev/vdX` and run `--ensure` if you must).

## 3b. The shared library (2.4.0)

Several machines can draw models from ONE store instead of each keeping a copy. On Verda that store is a volume of
a SHARED type: `GET /volume-types` marks `NVMe_Shared`, `HDD_Shared` and `NVMe_Shared_Cluster` with
`is_shared_fs: true`, and unlike a plain block volume (one instance at a time) a shared one attaches to several at
once. `POST /volumes` takes `instance_ids`, and `PUT /volumes {"action": "attach", "instance_ids": [...]}` adds
more later. NVMe_Shared costs the same per GiB as plain NVMe.

- **Create it** in the console or with `POST /volumes` (`name`, `size`, `type: NVMe_Shared`, `location_code`), in
  the SAME location as every machine that will use it. A location cannot be crossed, so the library's location is
  the location all its machines live in.
- **Mount it** by giving the endpoint to `ensure`: `podctl ensure <id> --library nfs.<dc>.verda.com:/<pseudo>`
  (or `VERDA_LIBRARY` in the environment). `startup.sh` writes one fstab line with `nconnect=16,nofail,_netdev`,
  mounts it at `/mnt/comfy-library`, and creates `models/`, `output/` and `input/` under it. A later plain
  `podctl ensure <id>` keeps the library: the endpoint is recorded in `host.env` and the script recalls it.
  `--no-library` drops one.
- **What moves and what does not.** `models/` becomes ComfyUI's own `models/` (a symlink, so packs that join
  `folder_paths.models_dir` with their own folder name find the shared copy instead of downloading 21 GB again),
  and `--output-directory` / `--input-directory` put renders and inputs on it too. `user/` stays local, because it
  holds the saved workflows App Mode opens and each machine carries only its own package; `temp/` stays local
  because it is churn nobody shares. The venv, ComfyUI itself and the custom nodes stay on the machine's own data
  volume: they are tens of thousands of small files, which is what NFS is worst at.
- **No library, no server.** With one configured the boot unit gains `RequiresMountsFor=/mnt/comfy-library`, so a
  machine whose library is missing does not start ComfyUI. An empty model dropdown looks like a broken package;
  a unit that refuses to start says what is wrong in one line of the log.
- **The disk gate.** `_base_fs_is_pool` treats every `host:/path` device as a pool whose `df` cannot be trusted
  (it was written for MooseFS), so `podctl ensure` records the library volume's size in `state/volume.env` and the
  gate measures against that. Without the record it would print "free space is not verifiable here" and never fire.
- **Starting and cloning.** `podctl start <os volume>` attaches the library by TYPE and LOCATION, never by name:
  one library serves machines called `comfy-base`, `comfy-cc` and `comfy-mm`, whose name stems all differ, so the
  stem rule that finds a machine's own data volume cannot find it and must not. `podctl deploy --like` carries a
  shared volume to the clone WITHOUT detaching it, because detaching would take the library from every other
  machine.

**Measured on a live account, 2026-09-17** (a 1 GB `NVMe_Shared` probe volume attached to a running instance):

- **A shared volume is NOT a block device.** `lsblk` is byte for byte identical before and after the attach, no new
  `/dev/vd*`, nothing in `dmesg`. So `startup.sh`'s "the one unmounted `/dev/vd[b-z]`" rule is not endangered and
  `COMFY_DATA_DISK` is not needed: a machine with a library is still a two-disk machine plus an NFS mount.
- **The record carries the whole recipe**, so nothing has to be typed. `target` is the endpoint
  (`nfs.fin-03.datacrunch.io:/comfy-library-<id>`), beside `pseudo_path`, `create_directory_command`,
  `mount_command` and `filesystem_to_fstab_command`. The driver reads `target`; `--library` is an override.
  Note `target` is overloaded: on a plain block volume it is the device name, `vda`, so the `host:/export` SHAPE
  is what the driver tests, never the key alone.
- **The mount is NFS 4.2**, `nconnect=16`, `rsize/wsize=1048576`, `hard`, `proto=tcp`, over the instance's PRIVATE
  address. `df` and `stat -f` report the volume's real size and usage, not a pool's.
- **Attach is `PUT /volumes {"id": ..., "action": "attach", "instance_id": ...}`**, which answers `[null]`; detach
  takes the same shape.
- **Cross-location is refused** with `HTTP 400 invalid_request`: "Volume location (FIN-02) does not match instance
  location (FIN-03)". One library serves one location, and that decides where its machines live.
- **The status vocabulary differs from a block volume's.** A fresh shared volume is `created`; an ATTACHED one is
  `exported`, not `attached`; only after a detach does it read `detached`. `pods()` therefore lists shared volumes
  whatever their status, because `_detached()` would hide the library exactly while it is in use.
- **Price** confirmed at `monthly_price: 0.2` per GB, the same as plain NVMe.

### The shared store as the WHOLE workspace (2.5.0)

The section above shares the models. This shares everything, and it is the RunPod shape: on RunPod a pod is a
container with no disk of its own, one network volume mounts at `/workspace`, and it holds `comfy-base/`, ComfyUI,
the venv, `packages/` and the models. The pod is disposable; the volume is the asset. A Verda instance is a virtual
machine and must boot from a block device, so it keeps one OS volume, but nothing above the OS has to live there.

    podctl ensure <id> --workspace-shared

That mounts the attached shared volume at `/workspace` instead of hunting for a data disk, so **the machine needs
no data volume at all**. The endpoint is the same `target` the library uses, resolved the same way, so it is never
typed twice; with no shared volume attached the command refuses rather than leaving `startup.sh` to wait 600 s for
a disk that will never appear. `fstab` is the only record of it, because `host.env` lives on the share it names.

What the base does differently once `BASE_VOLUME_SHARED=1` is recorded:

- **`user/` and `temp/` move off the store**, to `BASE_LOCAL_STATE` (`/var/lib/comfy-base-machine`). `user/` holds
  the saved workflows the App view opens and the frontend's settings, which are per machine; `temp/` is scratch.
  Everything else on the root is meant to be shared, which is the whole point.
- **An install takes a lock** (`state/install.lock`, a directory, because mkdir is atomic over NFSv4). Two machines
  writing one venv corrupts it: pip and uv write in place and neither expects a second writer. A lock whose owner
  died is honoured for two hours and then broken, with the machine that left it named. **Running takes no lock**,
  because running only reads, and that is what makes several GPUs on one store safe.

So three workflows on three GPUs share one store: one base, one ComfyUI, one venv, one model library, and each
workflow adds its own packs and rows to it the first time it is installed.

## 4. The first deploy

Either way, the sequence ends with `ensure` and `install`.

**From the console:** create the instance with the type, image and location above, your ssh key, and the data
volume as an existing volume. If the startup script `comfy-base-startup` already exists in your account (a
previous `ensure` registered it), pick it: the first boot then mounts `/workspace` and installs the boot unit
by itself. If it does not exist yet, deploy without one; `ensure` does the same work over ssh a minute later.

**From podctl**, once one OS volume exists in the account (that is, after one console deploy and one stop):

    podctl --provider verda pods                    # instances, and the detached volumes of stopped machines
    podctl --provider verda start <os volume id>    # a new instance from that volume, data volumes attached

Then, for a running instance:

    podctl --provider verda ensure <instance id>    # key, script, ssh block, mount + unit over ssh
    podctl --provider verda install <instance id> --base
    podctl --provider verda tunnel <instance id>    # 8188 and 8888 on localhost

`ensure` is idempotent: the second run registers nothing, rewrites only the `Host verda` block, and `startup.sh
--ensure` reports the mount and the unit as unchanged. It prints the script's READY line, which names the disk,
the mount, the unit state and whether `/workspace/comfy-base/boot.sh` is present yet. Before the base is
installed the unit sits in `sleep infinity`; after `install --base`, `systemctl restart comfy-base-boot` on the
machine (or a `podctl restart`) boots the base.

## 5. Stop and start: delete keeping the volumes

A shut-down Verda instance still bills the GPU. So `podctl stop` does NOT shut down: it sends
`PUT /instances {"action": "delete", "volume_ids": []}`, which deletes the instance and keeps every volume,
and it prints the ids it kept:

    stopped <id>: instance deleted, volumes kept: OS volume <os-id>, data volumes <data-id>. podctl start <os-id> ...

From then on `podctl pods` shows the two detached volumes instead of the instance. `podctl start <os-id>`
deploys a new instance with `image=<os-id>` (the OS volume is the image next time: the disk, the packages,
the unit, the fstab line all come back), attaches the data volumes that share its name, and waits for
`running` plus an ip plus an ssh banner. Two things change on every start:

- **the public ip.** The provider rewrites the `Host verda` block, so `ssh verda` keeps working. OpenSSH
  remembers host keys by ip, and a reused ip with a different key is refused; `ssh-keygen -R <ip>` clears it.
- **the instance id.** The stopped id is gone; the OS volume id is the stable handle of a machine.

`podctl restart` is the API pair `shutdown` then `start` on the same instance (never `reboot` inside the
guest: Verda's confidential-computing instances forbid it, and the API path is the same for every type). The
ip may change here too; the block is rewritten.

## 6. The balance

Verda bills hourly from a prepaid balance. **At zero balance every instance is discontinued and every volume
goes to the trash, with 96 hours to restore them.** Watch the balance before a long run; the provider cannot
(there is no balance endpoint it reads) and a `discontinued` status in `podctl status` is the symptom.

## 7. No cloud firewall

Verda instances have no firewall in front of them: every port a process binds on `0.0.0.0` is on the
internet, on the public ip, immediately. On this host the base binds ComfyUI to loopback: `lib/00-env.sh`
reads `BASE_HOST=verda` from `state/host.env` and sets `BASE_LISTEN=127.0.0.1` for every host but RunPod, and
`lib/85-launch.sh` passes it as `--listen`. The way in is `podctl tunnel`, which forwards 8188 and 8888 over ssh.

JupyterLab follows the same setting: `lib/boot.sh` starts it on `BASE_LISTEN`, so on Verda it too listens on
loopback only and is reached through the tunnel on 8888.

Nothing else may listen on `0.0.0.0` on a Verda machine.

## 8. What the provider does, per command

| command | Verda calls |
|---|---|
| `pods` | `GET /instances` plus `GET /volumes` (the detached ones) |
| `status <id>` | `GET /instances/{id}`, falling back to `GET /volumes/{id}` for a stopped machine |
| `ensure <id>` | `GET/POST /sshkeys`, `GET/POST /scripts`, poll `GET /instances/{id}`, ssh: `startup.sh --ensure` |
| `ssh-config <id>` | `GET /instances/{id}`, then the `Host verda` block |
| `stop <id>` | `PUT /instances {action: delete, volume_ids: []}` |
| `start <id>` | instance: `PUT {action: start}`; OS volume: `POST /instances {image: <volume id>, existing_volumes: [...]}` |
| `restart <id>` | `PUT {action: shutdown}`, poll offline, `PUT {action: start}` |
| `deploy --like <id>` | source must be offline; `PUT /volumes {action: detach}` per data volume, then `POST /instances` with them |
| `install`, `upload`, `tunnel`, `lease` | generic: ssh and scp through `Host verda` |

`podctl image` has nothing to show on this host (a VM boots from its own image, not a container's), and
`record_volume` writes nothing: the data volume is a real block device, so `df` on `/workspace` is honest.

## 9. On the machine

- `/var/log/comfy-base-startup.log`: every run of `startup.sh`, first boot and `--ensure`, with a READY line.
- `/etc/systemd/system/comfy-base-boot.service`: `RequiresMountsFor=/workspace`; runs `boot.sh` or sleeps.
- `/etc/fstab`: one `UUID=... /workspace ext4 defaults,nofail,x-systemd.device-timeout=30 0 2` line.
- `/workspace/comfy-base/state/host.env`: `BASE_HOST=verda`, `BASE_VOLUME=/workspace`.
- `/root/comfy-base-startup.sh`: the copy `ensure` ran; `bash /root/comfy-base-startup.sh --ensure` repeats it.

## A buyer's machine (3.1.0)

Everything above is a maintainer's machine. A buyer runs on their own account, and the driver makes that machine
from nothing:

    podctl --provider verda balance
    podctl --provider verda availability --gpu 1RTXPRO6000.30V          # where it can be deployed right now
    podctl --provider verda create --name <n> --gpu 1RTXPRO6000.30V --data-gb 200 [--location FIN-03]
    podctl --provider verda ensure <instance id>
    podctl --provider verda buyer-install <instance id> --runtime <runtime.json URL> --base-zip comfyui-base.zip <package zips>
    podctl --provider verda progress <instance id>                       # where the later files are
    podctl --provider verda pause <instance id>                          # only the disks bill
    podctl --provider verda put-away <instance id> --to <folder> --yes   # images to the Mac, the disks to the trash

`create` sends `POST /volumes {"type": "NVMe", ...}` for the data disk, then `POST /instances` with `os_volume`
(`{"name": "<n>-os", "size": 100}`), that disk in `existing_volumes`, and the image TYPE (`24.04.cuda13.2.docker`,
or `VERDA_IMAGE`): the display name in §3 is what the console shows, not what the API takes. It never sends a
`startup_script_id`, because a fresh OS volume has no fstab line and the registered script would wait ten minutes
for one; `ensure` configures the machine over ssh instead. When the instance is refused (`503 Not enough resources`,
a sold-out location) the new data disk is deleted at once, so nothing is left billing. `balance` reads
`GET /balance` and `availability` reads `GET /instance-availability`, both now called by the driver.

