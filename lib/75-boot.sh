# 75-boot.sh: the base owns the machine's boot: boot.sh + the launch lib on the volume, the tools venv (JupyterLab, newest),
# state/boot.env (what boot.sh starts), state/launch.d/ (3.10.0: each package's LAUNCH_ENV). On RunPod podctl points the pod's start command at <volume>/comfy-base/boot.sh;
# on a VM host (2.2.0) the base installs comfy-base-boot.service, which runs it at every boot. From then on every boot is
# the base's: sshd → JupyterLab → ComfyUI from the base's venv → the app ports (3.9.0), and the script never exits.

_base_jupyter_auth(){ # which variable protects JupyterLab at boot (the value is never shown)
  local v
  for v in JUPYTER_TOKEN JUPYTER_PASSWORD; do
    if [ -n "${!v:-}" ] || [ -n "$(_base_pid1_env "$v")" ]; then echo "$v"; return 0; fi
  done
  echo "generated at boot (state/tokens.env; podctl status or jupyter prints it)"
}
_base_app_rows(){ # 3.9.0: this package's APP_PORTS, validated, one `port|slug|name` per line; a bad row is an error, named on stderr (the caller captures stdout)
  local r p s n rc=0
  for r in ${APP_PORTS[@]+"${APP_PORTS[@]}"}; do
    IFS='|' read -r p s n <<< "$r"
    if ! [[ "$p" =~ ^[0-9]+$ ]] || [ "$p" -lt 1024 ] || [ "$p" -gt 65535 ]; then err "APP_PORTS row '$r': the port must be 1024-65535" >&2; rc=1; continue; fi
    case " 22 8188 8888 9199 11434 " in *" $p "*) err "APP_PORTS row '$r': $p is the base's own (sshd, ComfyUI, JupyterLab, the tunnel's)" >&2; rc=1; continue;; esac
    if ! [[ "$s" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then err "APP_PORTS row '$r': the slug must be lowercase kebab-case (its /apps/<slug>/ folder)" >&2; rc=1; continue; fi
    if [ -z "$n" ] || [[ "$n" == *"|"* ]]; then err "APP_PORTS row '$r': port|slug|name, the name non-empty" >&2; rc=1; continue; fi
    printf '%s|%s|%s\n' "$p" "$s" "$n"
  done
  return $rc
}
base_boot_apps(){ # 3.9.0: the package's customer apps get a port each: state/apps.d/<package>.list, then the forwarder restarts
  # The declaration is the package's (APP_PORTS=("8189|<slug>|<name>")); the base knows no app. podctl create and a
  # template read the same rows from the script, so the console's names and the machine's ports cannot drift apart.
  local pkg rows f d="$BASE_STATE/apps.d"
  if [ -z "${PKG_DIR:-}" ] || [ "$PKG_DIR" = "$BASE_DIR" ]; then return 0; fi      # the base's own run declares no app
  pkg="$(basename "$PKG_DIR")"; f="$d/$pkg.list"
  if ! rows="$(_base_app_rows)"; then BASE_FAILED+=("app ports: a bad APP_PORTS row (above)"); return 0; fi
  if [ "$BASE_DRY" = "1" ]; then
    if [ -n "$rows" ]; then would "app ports for $pkg: $(printf '%s' "$rows" | tr '\n' ' ')"; fi
    return 0
  fi
  if [ -z "$rows" ]; then
    [ -f "$f" ] || return 0
    rm -f "$f"; ok "app ports: $pkg declares none now; $f removed"
  else
    mkdir -p "$d"
    if [ -f "$f" ] && [ "$(cat "$f")" = "$rows" ]; then ok "app ports current: $(printf '%s' "$rows" | cut -d'|' -f1,3 | tr '\n' ' ')"
    else printf '%s\n' "$rows" > "$f"; ok "app ports: $(printf '%s' "$rows" | cut -d'|' -f1,3 | tr '\n' ' ')"; BASE_CHANGED+=("app ports: $pkg"); fi
  fi
  if [ -n "$BASE_FAKE_ROOT" ]; then note "app ports: not (re)started in a fake tree"; return 0; fi
  bash "$BASE_HOME/boot.sh" --apps 2>&1 | sed 's/^/  /'
  return 0
}
base_boot_env_write(){ # the facts boot.sh needs; written by every real run, single-quoted so boot.sh can source it
  local f="$BASE_STATE/boot.env"
  [ "$BASE_DRY" = "1" ] && return 0
  mkdir -p "$BASE_STATE"
  { echo "# comfy-base boot facts — written by every install; boot.sh sources this"
    printf "COMFY='%s'\n" "$COMFY"; printf "VENV='%s'\n" "$VENV"; printf "PORT='%s'\n" "$PORT"
    printf "ARGS_FILE='%s'\n" "$ARGS_FILE"; printf "PERSIST='%s'\n" "${BASE_PERSIST_ROOT:-}"; printf "BASE_HOME='%s'\n" "$BASE_HOME"
    printf "PREVIEW_SIZE='%s'\n" "${BASE_PREVIEW_SIZE:-1024}"; printf "TS='%s'\n" "$(_base_ts)"; } > "$f"
  ok "boot facts written: $f"
}
base_launch_env_write(){ # 3.10.0: the package's LAUNCH_ENV → state/launch.d/<PKG_ID>.env, read at every launch (85-launch.sh)
  # One file per package, because one store serves several packages and each install knows only its own declaration.
  # A package that stops declaring one has its file removed, so a variable never outlives the package line that set it.
  local f rows
  f="$BASE_STATE/launch.d/${PKG_ID:-base}.env"
  rows="$(_base_launch_env_rows 2>/dev/null || true)"
  if [ "$BASE_DRY" = "1" ]; then
    if [ -n "$rows" ]; then would "write $f ($(echo $rows))"; elif [ -f "$f" ]; then would "remove $f (${PKG_ID:-base} declares no LAUNCH_ENV now)"; fi
    return 0
  fi
  if [ -z "$rows" ]; then
    if [ -f "$f" ]; then rm -f "$f"; ok "launch env: ${PKG_ID:-base} declares none now, $f removed"; BASE_CHANGED+=("launch env: $f removed"); fi
    return 0
  fi
  mkdir -p "$BASE_STATE/launch.d"
  if [ -f "$f" ] && [ "$(grep -v '^#' "$f")" = "$rows" ]; then ok "launch env current: $(echo $rows)"; return 0; fi
  { echo "# ${PKG_NAME:-ComfyUI Base} V${PKG_VERSION:-$BASE_VERSION}: its LAUNCH_ENV; every ComfyUI launch reads this folder (lib/85-launch.sh)"
    printf '%s\n' "$rows"; } > "$f"
  ok "launch env: $(echo $rows) ($f)"; BASE_CHANGED+=("launch env: $(echo $rows)")   # a change the running server cannot see: BASE_CHANGED is a restart reason
  return 0
}
base_boot_tools(){ # JupyterLab in the base's tools venv, on the newest Python, at its newest (3.0.0: every run, never "present")
  # $BASE_HOME/tools (the path boot.sh starts JupyterLab from) is a SYMLINK to tools.<python minor>.<ts>. A new Python
  # minor builds a new venv beside the old and swaps the link in one rename, so a JupyterLab another machine on the
  # store is running keeps its files; otherwise JupyterLab is upgraded in place. The newest two are kept.
  local link="$BASE_HOME/tools" mm cur_mm target ts stamp
  if [ "$BASE_DRY" = "1" ]; then would "JupyterLab at its newest in the tools venv $link, on the newest Python uv provides"; BASE_TOOLS_RESULT="would-upgrade"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then
    stamp="$link/.comfy-base-tools"
    if [ -x "$link/bin/jupyter-lab" ] && [ -f "$stamp" ]; then ok "tools venv present (fake)"; BASE_TOOLS_RESULT="present (fake)"; return 0; fi
    mkdir -p "$link/bin"; printf '#!/bin/bash\necho "fake jupyter-lab $*"\n' > "$link/bin/jupyter-lab"; chmod +x "$link/bin/jupyter-lab"
    echo "fake ts=$(_base_ts)" > "$stamp"; ok "fake tools venv (BASE_NO_NET)"; BASE_TOOLS_RESULT="built (fake)"; BASE_CHANGED+=("tools venv built (fake)"); return 0
  fi
  if _base_runtime_on; then                           # 3.1.0: JupyterLab came with the proven runtime
    if [ -x "$link/bin/jupyter-lab" ]; then _base_runtime_skip "JupyterLab"; BASE_TOOLS_RESULT="runtime"; return 0; fi
    warn "the runtime carries no JupyterLab at $link: the file browser will not start (ComfyUI is unaffected)"; BASE_TOOLS_RESULT="missing (runtime)"; return 0
  fi
  command -v uv >/dev/null 2>&1 || _base_ensure_uv || { warn "tools venv: no uv: the boot starts without JupyterLab"; BASE_TOOLS_RESULT="failed (no uv)"; return 0; }
  mm="$(_base_newest_python_minor)"
  [ -n "$mm" ] || { warn "tools venv: uv lists no Python: JupyterLab not upgraded"; BASE_TOOLS_RESULT="failed (no python)"; return 0; }
  cur_mm="$("$link/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ -L "$link" ] && [ "$cur_mm" = "$mm" ] && [ -x "$link/bin/jupyter-lab" ]; then
    if uv pip install -q --python "$link/bin/python" --upgrade jupyterlab 2>&1 | tail -2; then
      echo "python=$mm jupyterlab=$("$link/bin/python" -c 'import importlib.metadata as m; print(m.version("jupyterlab"))' 2>/dev/null) ts=$(_base_ts)" > "$link/.comfy-base-tools"
      ok "tools venv: $(cat "$link/.comfy-base-tools")"; BASE_TOOLS_RESULT="newest"
    else warn "tools venv: JupyterLab could not be upgraded"; BASE_TOOLS_RESULT="failed (upgrade)"; fi
    return 0
  fi
  ts="$(_base_ts)"; target="$BASE_HOME/tools.$mm.$ts"
  uv python install "$mm" >/dev/null 2>&1 || true
  if ! uv venv -q --python "$mm" --seed "$target" 2>&1 | tail -2 || ! uv pip install -q --python "$target/bin/python" jupyterlab 2>&1 | tail -2 || [ ! -x "$target/bin/jupyter-lab" ]; then
    rm -rf "$target"; warn "tools venv: could not build JupyterLab on Python $mm: the previous one stays"; BASE_TOOLS_RESULT="failed (build)"; return 0
  fi
  echo "python=$mm jupyterlab=$("$target/bin/python" -c 'import importlib.metadata as m; print(m.version("jupyterlab"))' 2>/dev/null) ts=$ts" > "$target/.comfy-base-tools"
  if [ -d "$link" ] && [ ! -L "$link" ]; then mv "$link" "$BASE_HOME/tools.pre-$ts" || true; fi    # the 2.x directory, kept for a running JupyterLab
  # one rename replaces the link (a plain `mv` onto a symlink to a directory would move INTO the directory)
  ln -sfn "$(basename "$target")" "$link.new" && "${SYS_PY:-python3}" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$link.new" "$link"
  ok "tools venv: $(cat "$link/.comfy-base-tools") at $target"; BASE_CHANGED+=("tools venv: JupyterLab on Python $mm"); BASE_TOOLS_RESULT="built (Python $mm)"
  # keep the newest two (the one the link names, and the one before it)
  ls -dt "$BASE_HOME"/tools.[0-9]* "$BASE_HOME"/tools.pre-* 2>/dev/null | sed -n '3,$p' | while IFS= read -r old; do rm -rf "$old"; done
  return 0
}
_base_systemd_live(){ # systemd is this machine's init and the base may drive it: never RunPod (a container) nor a fake root
  [ "$BASE_HOST" != runpod ] && [ -z "$BASE_FAKE_ROOT" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}
_base_boot_unit_enabled(){ # 3.0.3: comfy-base-boot is installed and enabled here, so the unit is what owns ComfyUI
  _base_systemd_live && systemctl is-enabled --quiet comfy-base-boot.service 2>/dev/null
}
_base_in_boot_unit(){ # 3.0.3: this shell runs INSIDE the unit (a JupyterLab terminal, a boot extension), so stopping the unit stops it
  grep -q 'comfy-base-boot\.service' "/proc/$$/cgroup" 2>/dev/null
}
_base_boot_unit_owns(){ # 3.0.3: a restart must go through the unit: it owns ComfyUI here and stopping it does not stop this run
  _base_boot_unit_enabled && ! _base_in_boot_unit
}
_base_boot_unit_text(){ # the systemd unit that runs the base's boot at every boot of a VM host (Verda, Crusoe, an owned box)
  local mounts=""
  [ "${BASE_VOLUME_KIND:-mount}" = mount ] && mounts="RequiresMountsFor=$BASE_VOLUME"$'\n'
  printf '%s' "[Unit]
Description=ComfyUI Base boot (sshd, JupyterLab, ComfyUI) from $BASE_HOME
After=network-online.target local-fs.target
Wants=network-online.target
${mounts}
[Service]
Type=simple
Environment=BOOT_HOME=$BASE_HOME
Environment=BASE_HOST=$BASE_HOST
Environment=BASE_LISTEN=$BASE_LISTEN
ExecStart=/bin/bash -c 'test -x $BASE_HOME/boot.sh && exec bash $BASE_HOME/boot.sh || exec sleep infinity'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"
}
base_boot_unit(){ # 2.2.0: the unit's text always lands beside boot.sh (what a person or a startup script installs); on a live
  # systemd host that is not RunPod it is installed and enabled too. RunPod (a container, no systemd) keeps its start command.
  local text unit="$BASE_HOME/comfy-base-boot.service" sys="/etc/systemd/system/comfy-base-boot.service" changed=0
  text="$(_base_boot_unit_text)"
  if [ "$BASE_DRY" = "1" ]; then would "write $unit (BASE_HOST=$BASE_HOST)"; return 0; fi
  if ! { [ -f "$unit" ] && [ "$(cat "$unit")" = "$text" ]; }; then printf '%s\n' "$text" > "$unit"; changed=1; fi
  if ! _base_systemd_live; then
    [ "$changed" = 1 ] && ok "boot unit written: $unit (installed by the host's startup script or by hand)" || ok "boot unit current: $unit"
    return 0
  fi
  if [ "$(id -u 2>/dev/null)" != "0" ]; then warn "boot unit: not root, so $sys was not installed (sudo cp $unit $sys && sudo systemctl enable comfy-base-boot)"; return 0; fi
  if ! { [ -f "$sys" ] && cmp -s "$unit" "$sys"; }; then
    cp "$unit" "$sys" && systemctl daemon-reload && systemctl enable comfy-base-boot >/dev/null 2>&1 && ok "boot unit installed and enabled: $sys" && BASE_CHANGED+=("boot: unit $sys")
  else ok "boot unit installed: $sys"; fi
  return 0
}
base_boot_install(){ # boot.sh + lib/85-launch.sh on the volume (current copies), the boot unit on a VM host, the tools venv, state/boot.env
  hdr "BOOT · the base boots this pod (sshd → JupyterLab → ComfyUI, never exits)"
  local src="$BASE_DIR/lib/boot.sh" dst="$BASE_HOME/boot.sh" lsrc="$BASE_DIR/lib/85-launch.sh" ldst="$BASE_HOME/lib/85-launch.sh" changed=0
  if [ "$BASE_DRY" = "1" ]; then
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then ok "boot.sh current at $dst"; else would "install $dst (PID 1 once podctl points the pod's start command at it)"; fi
    base_boot_tools; would "write $BASE_STATE/boot.env (COMFY, VENV, PORT, ARGS_FILE)"
    base_boot_apps
    base_launch_env_write
    BASE_BOOT_RESULT="would install · tools: $BASE_TOOLS_RESULT · auth: $(_base_jupyter_auth)"; return 0
  fi
  mkdir -p "$BASE_HOME/lib"
  if ! { [ -f "$dst" ] && cmp -s "$src" "$dst"; }; then cp "$src" "$dst" && chmod 755 "$dst" && changed=1; fi
  if ! { [ -f "$ldst" ] && cmp -s "$lsrc" "$ldst"; }; then cp "$lsrc" "$ldst" && changed=1; fi
  # VERSION rides beside boot.sh too (on a pod BASE_DIR is BASE_HOME and this is a no-op; in the suite the fake pod's
  # comfy-base/ gets exactly what the boot needs and nothing else)
  if [ -f "$BASE_DIR/VERSION" ] && ! { [ -f "$BASE_HOME/VERSION" ] && cmp -s "$BASE_DIR/VERSION" "$BASE_HOME/VERSION"; }; then cp "$BASE_DIR/VERSION" "$BASE_HOME/VERSION" && changed=1; fi
  # 3.9.0: the forwarder rides beside boot.sh (on a pod BASE_DIR is BASE_HOME and this is a no-op)
  local psrc="$BASE_DIR/py/app_proxy.py" pdst="$BASE_HOME/py/app_proxy.py"
  if [ "$psrc" != "$pdst" ] && ! { [ -f "$pdst" ] && cmp -s "$psrc" "$pdst"; }; then mkdir -p "$BASE_HOME/py" && cp "$psrc" "$pdst" && changed=1; fi
  if [ "$changed" = "1" ]; then ok "installed $dst (+ lib/85-launch.sh)"; BASE_CHANGED+=("boot: installed $dst"); else ok "boot.sh current at $dst"; fi
  base_boot_unit
  base_boot_tools
  base_boot_env_write
  base_boot_apps
  base_launch_env_write
  BASE_BOOT_RESULT="$dst · tools: $BASE_TOOLS_RESULT · JupyterLab auth: $(_base_jupyter_auth)"
  return 0
}
