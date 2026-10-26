#!/usr/bin/env bash
# hosts/verda/startup.sh: the Verda first-boot script for a ComfyUI Base machine, and its --ensure pass.
#
#   as a Verda startup script   runs ONCE, as root, on the first boot of a fresh image (registered by
#                               `podctl --provider verda ensure` under the name comfy-base-startup)
#   bash startup.sh --ensure    the same work, idempotently, on a machine that is already running (what
#                               `podctl ensure` does over ssh: an instance deployed without the script, or
#                               redeployed from an OS volume, ends up equal to one that had it)
#   bash startup.sh --ensure --library nfs.fin-02.verda.com:/pseudo
#                               the same, and mount that Verda shared filesystem at /mnt/comfy-library
#
# What it does, in order:
#   0. installs what the image lacks and the base needs: nfs-common (the shared library) and python3-venv
#      (the ubuntu-24.04-cuda-13.0-open-docker image ships no ensurepip, so `python3 -m venv` cannot run);
#   1. finds the one data disk (/dev/vd[b-z], unmounted, not the OS disk), waits up to 10 minutes for it,
#      refuses when there is more than one candidate, makes an ext4 filesystem labelled comfy-volume when
#      the disk is blank, adds an fstab line by UUID at /workspace (nofail, 30 s device timeout), mounts it;
#   2. mounts the SHARED LIBRARY when one is configured: a Verda NVMe_Shared volume, reached over NFS, one
#      fstab line at /mnt/comfy-library (nconnect=16, nofail, _netdev), holding models/, output/ and input/
#      for every machine at once. The endpoint comes from --library, else COMFY_LIBRARY, else the value a
#      previous run recorded in host.env, so a plain `--ensure` keeps the library it already had;
#   3. writes and enables the comfy-base-boot systemd unit, which runs /workspace/comfy-base/boot.sh when
#      the base is installed there and sleeps otherwise (so the unit is always healthy, and a base install
#      followed by `systemctl restart comfy-base-boot` or a machine restart boots the base). With a library
#      configured the unit also RequiresMountsFor it: ComfyUI must not come up with empty model dropdowns,
#      so no library means no server, said plainly in the log, rather than a silently empty panel;
#   4. writes /workspace/comfy-base/state/host.env (BASE_HOST, BASE_VOLUME, and the library when there is one);
#   5. prints one READY line.
#
# Never `set -e`: a partial first boot must still reach the READY line and the log. Pure ASCII.
# Log: /var/log/comfy-base-startup.log (also stderr, which Verda keeps in the instance console).

LOG=/var/log/comfy-base-startup.log
MOUNT=/workspace
LABEL=comfy-volume
UNIT=comfy-base-boot
UNIT_FILE=/etc/systemd/system/$UNIT.service
BOOT_HOME=$MOUNT/comfy-base
DISK_WAIT=600
LIB_MOUNT=/mnt/comfy-library
LIB_OPTS=nconnect=16,nofail,_netdev
MODE=first-boot
LIBRARY="${COMFY_LIBRARY:-}"          # host:/export of the Verda shared filesystem, or empty for no library
# 2.5.0: the shared filesystem AS the workspace, which is the RunPod shape carried over. On RunPod a pod is a
# container with no disk of its own and one network volume at /workspace holding the base, ComfyUI, the venv, the
# packages and the models, so the pod is disposable and the volume is the asset. A Verda VM must still boot from a
# block device, but nothing above the OS has to live there: with this set, /workspace IS the share and the machine
# carries no data volume at all.
WORKSPACE_SRC="${COMFY_WORKSPACE:-}"  # host:/export to mount at /workspace instead of hunting for a data disk
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ensure)  MODE=ensure ;;
    --library) LIBRARY="${2:-}"; shift ;;
    --library=*) LIBRARY="${1#--library=}" ;;
    --workspace) WORKSPACE_SRC="${2:-}"; shift ;;
    --workspace=*) WORKSPACE_SRC="${1#--workspace=}" ;;
    --no-library) LIBRARY=""; NO_LIBRARY=1 ;;
    *) ;;
  esac
  shift
done
NO_LIBRARY="${NO_LIBRARY:-0}"

# stderr, not stdout: several functions below are called as $(...) and their stdout IS their answer
log() { printf '%s comfy-base-startup[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*" | tee -a "$LOG" >&2; }

if [ "$(id -u)" != "0" ]; then
  echo "comfy-base-startup: must run as root" >&2
  exit 1
fi
touch "$LOG" 2>/dev/null

# ---------------------------------------------------------------- 0. what the image lacks

ensure_packages() {
  # nfs-common: the shared library is an NFS mount. mount.nfs was present on one machine that had already had
  # apt run on it, so the stock image may well carry it; the check costs nothing and the install only runs when
  # it is genuinely missing.
  # python3-venv: the image ships python3 without ensurepip, so `python3 -m venv` fails and the base's
  # venv step dies on a machine that looks fine. Both are installed once and persist on the OS volume.
  # ffmpeg: the image ships none, and VideoHelperSuite's bundled binary has no nvenc encoders, so a video
  # package's h264_nvenc format fails at the last node after the whole sample is paid for. Ubuntu's build has
  # h264_nvenc; lib/85-launch.sh finds it and sets VHS_FORCE_FFMPEG_PATH.
  # libegl1, libgles2: libEGL.so.1 and libGLESv2.so.2, which MediaPipe's libmediapipe.so needs even on CPU (3.29.1; the
  # base's own OS stage, lib/15-system.sh, installs them too).
  # 3.0.0: install OR UPGRADE, every time: `apt-get install` of a present package moves it to its newest. The whole
  # OS (and the NVIDIA driver) goes to its newest in the base's own first stage (lib/15-system.sh) at every install.
  local need=(nfs-common python3-venv ffmpeg libegl1 libgles2) out
  log "installing or upgrading ${need[*]}"
  export DEBIAN_FRONTEND=noninteractive
  # -o DPkg::Lock::Timeout: unattended-upgrades often holds the dpkg lock on a first boot
  if ! out=$(apt-get -o DPkg::Lock::Timeout=300 update 2>&1); then
    printf '%s\n' "$out" | tail -5 >>"$LOG"
    log "apt-get update failed (see $LOG)"
  fi
  if apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "${need[@]}" >>"$LOG" 2>&1; then
    PACKAGES="installed ${need[*]}"
    log "installed ${need[*]}"
    return 0
  fi
  PACKAGES="FAILED ${need[*]}"
  log "could not install ${need[*]} (see $LOG)"
  return 1
}

# ---------------------------------------------------------------- 1. the data disk

os_disk() {
  # the whole disk the root filesystem lives on (pkname of /'s source, or the source itself when it is a whole disk)
  local src
  src=$(findmnt -n -o SOURCE / 2>/dev/null | head -n1)
  [ -n "$src" ] || return 0
  local pk
  pk=$(lsblk -n -o PKNAME "$src" 2>/dev/null | head -n1)
  if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
}

disk_is_busy() {
  # anything on this disk (or one of its partitions) mounted or in use as swap
  local dev=$1
  lsblk -n -o MOUNTPOINT "$dev" 2>/dev/null | grep -q . && return 0
  lsblk -n -o TYPE "$dev" 2>/dev/null | grep -qx 'part' && return 0
  return 1
}

candidates() {
  # every /dev/vd[b-z] whole disk that is not the OS disk and is not in use
  local osd d
  osd=$(os_disk)
  for d in /dev/vd[b-z]; do
    [ -b "$d" ] || continue
    [ "$d" = "$osd" ] && continue
    disk_is_busy "$d" && continue
    echo "$d"
  done
}

find_data_disk() {
  # prints the device; an explicit COMFY_DATA_DISK wins; otherwise the single candidate, or the single
  # candidate already labelled comfy-volume when several disks are present; waits up to DISK_WAIT seconds
  local waited=0 list n labelled
  if [ -n "${COMFY_DATA_DISK:-}" ]; then
    if [ -b "$COMFY_DATA_DISK" ]; then echo "$COMFY_DATA_DISK"; return 0; fi
    log "COMFY_DATA_DISK=$COMFY_DATA_DISK is not a block device"
    return 1
  fi
  while :; do
    list=$(candidates)
    n=$(printf '%s\n' "$list" | grep -c .)
    if [ "$n" = "1" ]; then echo "$list"; return 0; fi
    if [ "$n" -gt 1 ]; then
      labelled=$(for d in $list; do [ "$(blkid -s LABEL -o value "$d" 2>/dev/null)" = "$LABEL" ] && echo "$d"; done)
      if [ "$(printf '%s\n' "$labelled" | grep -c .)" = "1" ]; then echo "$labelled"; return 0; fi
      log "refusing to choose: $n unmounted data disks ($(echo $list)) and not exactly one labelled $LABEL; set COMFY_DATA_DISK=/dev/vdX and run --ensure"
      return 1
    fi
    # 2.5.6: a machine with NO data volume is the normal case now, and on one the old code sat here for ten
    # minutes waiting for a disk nobody attached, because the registered startup script recalls its workspace from
    # an fstab a fresh OS volume does not have. Waiting is right when a disk is ATTACHED and slow to appear; it is
    # pointless when the instance has none at all. So: give it DISK_PROBE seconds to show up, and if nothing has
    # EVER appeared by then, say what to do and finish rather than block the boot.
    if [ "$waited" -ge "${DISK_PROBE:-30}" ] && [ -z "$(candidates)" ]; then
      log "no data disk on this machine after ${waited}s. If it should have one, attach it and run --ensure."
      log "  If it is meant to use the shared store instead, run: podctl ensure <id> --workspace-shared"
      return 1
    fi
    if [ "$waited" -ge "$DISK_WAIT" ]; then
      log "no data disk appeared within ${DISK_WAIT}s (no unmounted /dev/vd[b-z]): attach a volume in the console and run --ensure"
      return 1
    fi
    [ "$waited" = "0" ] && log "waiting for the data disk to appear (up to ${DISK_WAIT}s)"
    sleep 5
    waited=$((waited + 5))
  done
}

mount_workspace_share() {
  # /workspace from the shared filesystem: no data disk is looked for at all, and every machine that mounts the
  # same export sees the same base, the same ComfyUI, the same venv and the same models.
  local line src
  DISK="$WORKSPACE_SRC"
  case "$WORKSPACE_SRC" in
    *:/*) ;;
    *) log "workspace share '$WORKSPACE_SRC' is not host:/export, ignored"; WORKSPACE_SRC=""; return 1 ;;
  esac
  if ! command -v mount.nfs >/dev/null 2>&1; then
    log "workspace share: no mount.nfs on this machine (nfs-common did not install)"
    return 1
  fi
  mkdir -p "$MOUNT"
  line="$WORKSPACE_SRC $MOUNT nfs defaults,$LIB_OPTS 0 0"
  if grep -qxF "$line" /etc/fstab 2>/dev/null; then
    log "workspace fstab line present"
  else
    if awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { found = 1 } END { exit !found }' /etc/fstab 2>/dev/null; then
      cp /etc/fstab /etc/fstab.comfy-base.bak
      awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { next } { print }' /etc/fstab.comfy-base.bak >/etc/fstab
      log "replaced the old fstab line for $MOUNT (backup /etc/fstab.comfy-base.bak)"
    fi
    printf '%s\n' "$line" >>/etc/fstab
    log "fstab: $line"
  fi
  # the OS volume carries fstab across a full delete and recreate, so a machine converted from the 2.4.0 library
  # shape keeps mounting the store a SECOND time at /mnt/comfy-library forever unless the line is removed here.
  if awk -v m="$LIB_MOUNT" '$1 !~ /^#/ && $2 == m { found = 1 } END { exit !found }' /etc/fstab 2>/dev/null; then
    cp /etc/fstab /etc/fstab.comfy-base.bak
    awk -v m="$LIB_MOUNT" '$1 !~ /^#/ && $2 == m { next } { print }' /etc/fstab.comfy-base.bak >/etc/fstab
    log "removed the separate-library fstab line for $LIB_MOUNT (the workspace IS the store now)"
    umount "$LIB_MOUNT" 2>/dev/null && log "unmounted the second copy at $LIB_MOUNT"
  fi
  systemctl daemon-reload 2>/dev/null
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    src=$(findmnt -n -o SOURCE "$MOUNT")
    if [ "$src" = "$WORKSPACE_SRC" ]; then
      log "$MOUNT already mounted from $src"
      return 0
    fi
    # MEASURED on a live machine: the fstab line is rewritten but the mount is NOT, because $MOUNT is already
    # occupied by the block device and mount(8) will not replace it under a running system. Reporting success here
    # made READY say mounted=yes while the machine was still on its own disk, which is the one lie a report line
    # must never tell. The fstab is correct, so a reboot completes it; say exactly that and fail the step.
    log "$MOUNT is mounted from $src, NOT the share. The fstab line is written, so REBOOT to complete the switch"
    log "  (nothing can remount $MOUNT under a running system while the base and ComfyUI live on it)"
    DISK="$src"
    return 1
  fi
  local tries=0
  while [ "$tries" -lt 6 ]; do
    mount "$MOUNT" >>"$LOG" 2>&1 && break
    tries=$((tries + 1)); [ "$tries" = "1" ] && log "workspace not mounting yet, retrying for 30s"
    sleep 5
  done
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    log "$MOUNT mounted from $WORKSPACE_SRC (shared)"
    return 0
  fi
  log "$MOUNT is NOT mounted from $WORKSPACE_SRC (see $LOG)"
  return 1
}

mount_data_disk() {
  local dev uuid fs line
  if [ -n "$WORKSPACE_SRC" ]; then
    mount_workspace_share
    return $?
  fi
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    dev=$(findmnt -n -o SOURCE "$MOUNT")
    log "$MOUNT already mounted from $dev"
    DISK=$dev
    return 0
  fi
  dev=$(find_data_disk) || return 1
  DISK=$dev
  fs=$(blkid -s TYPE -o value "$dev" 2>/dev/null)
  if [ -z "$fs" ]; then
    log "$dev is blank: mkfs.ext4 -L $LABEL"
    if ! mkfs.ext4 -q -F -L "$LABEL" "$dev" >>"$LOG" 2>&1; then
      log "mkfs.ext4 failed on $dev (see $LOG)"
      return 1
    fi
  else
    log "$dev already carries $fs (kept)"
  fi
  uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null)
  if [ -z "$uuid" ]; then
    log "no UUID on $dev after mkfs; not mounting"
    return 1
  fi
  line="UUID=$uuid $MOUNT ext4 defaults,nofail,x-systemd.device-timeout=30 0 2"
  if grep -qF "$line" /etc/fstab 2>/dev/null; then
    log "fstab line present"
  else
    # drop any older line for the mount point, then add ours
    if awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { found = 1 } END { exit !found }' /etc/fstab 2>/dev/null; then
      cp /etc/fstab /etc/fstab.comfy-base.bak
      awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { next } { print }' /etc/fstab.comfy-base.bak >/etc/fstab
      log "replaced the old fstab line for $MOUNT (backup /etc/fstab.comfy-base.bak)"
    fi
    printf '%s\n' "$line" >>/etc/fstab
    log "fstab: $line"
  fi
  mkdir -p "$MOUNT"
  systemctl daemon-reload 2>/dev/null
  if ! mount -a 2>>"$LOG"; then
    log "mount -a reported an error (see $LOG)"
  fi
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    log "$MOUNT mounted from $dev"
    return 0
  fi
  log "$MOUNT is NOT mounted after mount -a"
  return 1
}

# ---------------------------------------------------------------- 1b. the shared library

recall_library() {
  # A plain `--ensure` must keep the library the machine already had: the endpoint was recorded in host.env
  # by the run that set it up. An explicit --no-library is the only way to drop one.
  #
  # 2.5.6, and this is a FEEDBACK LOOP rather than a stale field. host.env lives on the workspace, so once the
  # workspace IS the shared store that file is shared: this function then read ANOTHER machine's
  # COMFY_LIBRARY_SRC back out of it, mounted the store a second time at /mnt/comfy-library, and wrote that back
  # over the host.env the same run had just written correctly. Both live machines showed the double mount despite
  # being built completely differently, which is what gave it away. A shared workspace and a separate library are
  # ALTERNATIVES, exactly as lib/00-env.sh has said since 2.5.2; the script now agrees with the base.
  local f="$BOOT_HOME/state/host.env"
  if [ -n "$WORKSPACE_SRC" ]; then
    [ -z "$LIBRARY" ] || log "the whole workspace is the shared store, so the separate library $LIBRARY is ignored"
    LIBRARY=""
    return 0
  fi
  [ -z "$LIBRARY" ] || return 0
  [ "$NO_LIBRARY" = "1" ] && return 0
  [ -f "$f" ] || return 0
  LIBRARY="$(sed -n 's/^COMFY_LIBRARY_SRC=//p' "$f" | head -1)"
  [ -n "$LIBRARY" ] && log "library $LIBRARY recalled from host.env"
  return 0
}

recall_workspace() {
  # the workspace share cannot be recalled from host.env, because host.env LIVES on it. fstab is the record: a
  # machine that mounted it once has the line, and `--ensure` with no argument must not undo that.
  [ -z "$WORKSPACE_SRC" ] || return 0
  WORKSPACE_SRC="$(awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m && $3 == "nfs" { print $1; exit }' /etc/fstab 2>/dev/null)"
  [ -n "$WORKSPACE_SRC" ] && log "workspace share $WORKSPACE_SRC recalled from fstab"
  return 0
}

mount_library() {
  # The Verda shared filesystem (an NVMe_Shared volume) reached over NFS. One fstab line, nconnect=16 as
  # Verda's own documentation gives it, nofail so a boot never hangs on it, _netdev so it waits for the network.
  local line src tries=0
  LIBRARY_STATE=none
  if [ -z "$LIBRARY" ]; then
    log "no shared library configured (--library host:/export, or COMFY_LIBRARY)"
    return 0
  fi
  case "$LIBRARY" in
    *:/*) ;;
    *) log "library '$LIBRARY' is not host:/export, ignored"; LIBRARY=""; LIBRARY_STATE=invalid; return 1 ;;
  esac
  if ! command -v mount.nfs >/dev/null 2>&1; then
    log "library $LIBRARY: no mount.nfs on this machine (nfs-common did not install), not mounted"
    LIBRARY_STATE=no-nfs-client
    return 1
  fi
  mkdir -p "$LIB_MOUNT"
  line="$LIBRARY $LIB_MOUNT nfs defaults,$LIB_OPTS 0 0"
  if grep -qF "$line" /etc/fstab 2>/dev/null; then
    log "library fstab line present"
  else
    if awk -v m="$LIB_MOUNT" '$1 !~ /^#/ && $2 == m { found = 1 } END { exit !found }' /etc/fstab 2>/dev/null; then
      cp /etc/fstab /etc/fstab.comfy-base.bak
      awk -v m="$LIB_MOUNT" '$1 !~ /^#/ && $2 == m { next } { print }' /etc/fstab.comfy-base.bak >/etc/fstab
      log "replaced the old fstab line for $LIB_MOUNT (backup /etc/fstab.comfy-base.bak)"
    fi
    printf '%s\n' "$line" >>/etc/fstab
    log "fstab: $line"
  fi
  systemctl daemon-reload 2>/dev/null
  if findmnt -n "$LIB_MOUNT" >/dev/null 2>&1; then
    src=$(findmnt -n -o SOURCE "$LIB_MOUNT")
    if [ "$src" != "$LIBRARY" ]; then
      log "WARNING $LIB_MOUNT is already mounted from $src, not $LIBRARY, left alone; unmount it and re-run --ensure to change it"
    fi
    LIBRARY_STATE=mounted
  else
    # the network may not be up yet on a first boot; the fstab line covers every boot after this one
    while [ "$tries" -lt 6 ]; do
      mount "$LIB_MOUNT" >>"$LOG" 2>&1 && break
      tries=$((tries + 1))
      [ "$tries" = "1" ] && log "library not mounting yet, retrying for 30s"
      sleep 5
    done
    if findmnt -n "$LIB_MOUNT" >/dev/null 2>&1; then
      LIBRARY_STATE=mounted
      log "$LIB_MOUNT mounted from $LIBRARY"
    else
      LIBRARY_STATE=NOT-MOUNTED
      log "$LIB_MOUNT is NOT mounted from $LIBRARY (see $LOG); the fstab line stays, so a reboot retries"
      return 1
    fi
  fi
  # the three trees every machine shares; ComfyUI is pointed at them by the base, never by this script
  if ! mkdir -p "$LIB_MOUNT/models" "$LIB_MOUNT/output" "$LIB_MOUNT/input" 2>>"$LOG"; then
    log "could not create models/ output/ input/ under $LIB_MOUNT"
    return 1
  fi
  log "library ready at $LIB_MOUNT (models, output, input)"
  return 0
}

# ---------------------------------------------------------------- 2. the boot unit

write_unit() {
  local tmp req="/workspace"
  # a configured library is a hard requirement of the server: ComfyUI must never come up with empty model
  # dropdowns because an NFS mount was late or gone. No library, no server, and the log says which.
  [ -n "$LIBRARY" ] && req="/workspace $LIB_MOUNT"
  tmp=$(mktemp)
  cat >"$tmp" <<UNIT_EOF
[Unit]
Description=ComfyUI Base boot (sshd, JupyterLab, ComfyUI) from $BOOT_HOME
After=network-online.target local-fs.target
Wants=network-online.target
RequiresMountsFor=$req

[Service]
Type=simple
Environment=BOOT_HOME=$BOOT_HOME
ExecStart=/bin/bash -c 'test -x $BOOT_HOME/boot.sh && exec bash $BOOT_HOME/boot.sh || exec sleep infinity'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT_EOF
  if [ -f "$UNIT_FILE" ] && cmp -s "$tmp" "$UNIT_FILE"; then
    rm -f "$tmp"
    log "$UNIT_FILE unchanged"
  else
    install -m 0644 "$tmp" "$UNIT_FILE"
    rm -f "$tmp"
    log "wrote $UNIT_FILE"
  fi
  systemctl daemon-reload 2>>"$LOG"
  if ! systemctl enable --now "$UNIT" >>"$LOG" 2>&1; then
    log "systemctl enable --now $UNIT failed (see $LOG)"
    return 1
  fi
  UNIT_STATE=$(systemctl is-active "$UNIT" 2>/dev/null || true)
  log "$UNIT is $UNIT_STATE ($(systemctl is-enabled "$UNIT" 2>/dev/null || echo unknown))"
  return 0
}

# ---------------------------------------------------------------- 3. host.env

write_host_env() {
  local dir="$BOOT_HOME/state" want
  HOST_ENV=skipped
  if ! findmnt -n "$MOUNT" >/dev/null 2>&1; then
    log "host.env skipped: $MOUNT is not a mount point (never written to the OS disk)"
    return 1
  fi
  if ! mkdir -p "$dir" 2>>"$LOG"; then
    log "host.env skipped: cannot make $dir"
    return 1
  fi
  want=$(printf 'BASE_HOST=verda\nBASE_VOLUME=/workspace\n')
  # a shared workspace is recorded so the base knows this root is network storage several machines write to
  if [ -n "$WORKSPACE_SRC" ]; then
    want=$(printf '%s\nBASE_VOLUME_SHARED=1\n' "$want")
  fi
  # BASE_LIBRARY is the mount point the base reads; COMFY_LIBRARY_SRC is the endpoint this script remounts from
  if [ -n "$LIBRARY" ]; then
    want=$(printf '%s\nBASE_LIBRARY=%s\nCOMFY_LIBRARY_SRC=%s\n' "$want" "$LIB_MOUNT" "$LIBRARY")
  fi
  if [ -f "$dir/host.env" ] && [ "$(cat "$dir/host.env")" = "$want" ]; then
    HOST_ENV=kept
  else
    printf '%s\n' "$want" >"$dir/host.env"
    HOST_ENV=written
  fi
  log "host.env $HOST_ENV ($dir/host.env)"
  return 0
}

# ---------------------------------------------------------------- run

DISK=none
UNIT_STATE=absent
HOST_ENV=skipped
PACKAGES=unknown
LIBRARY_STATE=none
RC=0

log "start ($(hostname), kernel $(uname -r))"
ensure_packages || RC=1
recall_workspace
mount_data_disk || RC=1
recall_library
mount_library || RC=1
write_unit || RC=1
write_host_env || RC=1

MOUNTED=no
findmnt -n "$MOUNT" >/dev/null 2>&1 && MOUNTED=yes
BOOT=absent
[ -x "$BOOT_HOME/boot.sh" ] && BOOT=present
if [ "$RC" = "0" ]; then
  log "READY disk=$DISK mounted=$MOUNTED library=$LIBRARY_STATE unit=$UNIT_STATE boot.sh=$BOOT host.env=$HOST_ENV packages=$PACKAGES"
else
  log "READY-WITH-ERRORS disk=$DISK mounted=$MOUNTED library=$LIBRARY_STATE unit=$UNIT_STATE boot.sh=$BOOT host.env=$HOST_ENV packages=$PACKAGES (read $LOG)"
fi
exit $RC
