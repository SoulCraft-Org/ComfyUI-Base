#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""podctl: the Mac-side pod driver. One host provider per run (hosts/<host>/provider.py); everything else is generic.

    uv run "base/comfyui-base/_build/pod/podctl.py" [--provider runpod|verda|crusoe] <command> ...

The provider is --provider (before or after the command), else $PODCTL_PROVIDER, else the one brand.toml host of the
repository this base sits in, else runpod. It answers where the machine is and how it starts, stops, clones and hands
its boot to the base; the driver does ssh, scp, upload, install, the lease, the tunnel and the GPU gate the same way
on every host. Credentials (RunPod's key, Verda's client secret) are read by the provider from the environment or its
tool's config file and are never printed, logged or placed in an argument.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import posixpath
import shutil
import re
import zipfile
import sys
import types
import urllib.error
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]


def _load_by_path(name, path, package_dir=None):
    """Import a base module by file path under a fixed sys.modules name, once (the driver is a script, not a package)."""
    import importlib.util
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(package_dir)] if package_dir else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Self(types.ModuleType):
    """This module when it was loaded by path without being registered (the suite's _load): attribute reads go to its globals."""
    def __getattr__(self, name):
        try:
            return _GLOBALS[name]
        except KeyError:
            raise AttributeError(name)


_GLOBALS = globals()
sys.modules["comfyui_podctl"] = sys.modules[__name__] if __name__ in sys.modules else _Self("comfyui_podctl")   # providers reach back through this
_hosts = _load_by_path("comfyui_hosts", BASE_DIR / "hosts" / "__init__.py", BASE_DIR / "hosts")
_brand = _load_by_path("comfyui_brand", BASE_DIR / "py" / "brand.py")
_pkgdirs = _load_by_path("comfyui_pkgdirs", BASE_DIR / "py" / "pkgdirs.py")     # 3.6.0: the one package-discovery rule
_gpu_fit = _load_by_path("comfyui_gpu_fit", BASE_DIR / "py" / "gpu_fit.py")     # 3.8.0: the one GPU-preference rule (GPU_PREFER)
_appproxy = _load_by_path("comfyui_app_proxy", BASE_DIR / "py" / "app_proxy.py")   # 3.9.0: the one APP_PORTS row rule
PodctlError = _hosts.PodctlError
Provider = _hosts.Provider

# The RunPod half lives in hosts/runpod/provider.py since 2.2.0; its functions stay reachable here for the suite and for
# any caller that grew up with them.
_runpod = _hosts.load_provider("runpod", BASE_DIR)
RunPodProvider = _runpod.RunPodProvider
for _n in ("DEFAULT_API_URL", "PLACEHOLDERS", "SECRET_NAMES", "load_api_key", "Api", "pod_image", "pod_volume_id", "env_verdicts", "pod_facts",
           "BOOT_GUARD", "image_config", "wrap_entry", "sshd_bootstrap_text", "wait_for", "ensure", "stop", "start", "restart", "deploy_body",
           "deploy", "volume_size_gb", "record_volume", "POD_READ", "pod_volume", "volume_line", "stock", "cuda_floor", "create_floor", "DEFAULT_GPUS", "DEFAULT_MIN_CUDA", "allowed_cuda",
           "create_body", "create",
           "save_port_names", "names_template", "template_state", "http_ports", "jupyter_token", "proxy_url"):   # 3.9.0
    _GLOBALS[_n] = getattr(_runpod, _n)
del _n


# ---------------------------------------------------------------- the host the run addresses (set once per run by set_provider)

# ONE alias, one Mac, several sessions, and the block is a single pointer. On 2026-09-08 a third session ran `ssh-config`
# for its own pod and every other session's ssh, scp, install and lease silently began addressing that pod instead.
# `PODCTL_HOST` gives a session its own block (export PODCTL_HOST=runpod-<podid>) so nothing it does moves anyone else's
# pointer. Without it the alias is the provider's own name (runpod, verda, crusoe), so each host keeps its own block.
PROVIDER = None
SSH_ALIAS = (os.environ.get("PODCTL_HOST") or "").strip() or "runpod"
SSH_USER = "root"
KEY_PATH = "~/.ssh/id_ed25519"
VOLUME_ROOT = "/workspace"                              # the persistent root on the machine: the base's BASE_VOLUME there
# 2.5.6: the ComfyUI forward is per ALIAS, because with one GPU per workflow every machine serves on 8188 and
# identical LocalForward lines mean only whichever `ssh <alias>` ran first gets the port; the rest fail or, worse,
# the operator reads one product's panel believing it is another's. The offset is per DEPLOYMENT, not per
# provider: it says "this machine is my second one", which is a fact about somebody's fleet and not about the
# base. It therefore comes from the environment, beside the PODCTL_HOST that already names the alias, and
# defaults to 0 so a single-machine setup needs nothing.
# Every service is offset, not just ComfyUI. The first machine's session took 9199 and 11434 as well, and
# because ExitOnForwardFailure is set the SECOND machine's session then died on those and took its own ComfyUI
# forward down with it, so a product could not be opened even though its own port was free. Measured on two live
# machines.
# 2.11.0: this was a hard-coded map of one brand's three machine aliases, which meant anybody else's fleet
# silently shared 8188 and could only fix it by editing the base - the one thing a consumer must never do.
TUNNEL_OFFSET_ENV = "PODCTL_TUNNEL_OFFSET"
# 11434 is Ollama's. The base installs no inference server and never will: the port is forwarded because a
# PACKAGE may run one on the machine and talk to it on loopback, which is the shape a package uses when its
# whole point is that nothing leaves the box. Forwarding it costs nothing when no package does, and its
# absence would be silent and confusing. It is not a leftover; do not remove it without checking the
# packages of the repository around the base.
TUNNEL_SERVICES = [("comfy", 8188), ("jupyter", 8888), ("metrics", 9199), ("ollama", 11434)]
SSH_BLOCK = ("Host {alias}\n  HostName {host}\n  Port {port}\n  User {user}\n  IdentityFile {key}\n  StrictHostKeyChecking accept-new\n"
             "  LocalForward {comfy} localhost:8188\n  LocalForward {jupyter} localhost:8888\n"
             "  LocalForward {metrics} localhost:9199\n  LocalForward {ollama} localhost:11434\n")


def tunnel_offset(env=None):
    """How far this machine's local ports sit from the defaults. $PODCTL_TUNNEL_OFFSET, else 0. A bad value is
    refused rather than silently treated as 0, because 0 is exactly the collision it was set to avoid."""
    raw = ((env or os.environ).get(TUNNEL_OFFSET_ENV) or "").strip()
    if not raw:
        return 0
    try:
        off = int(raw)
    except ValueError:
        raise PodctlError("%s=%r is not a whole number" % (TUNNEL_OFFSET_ENV, raw))
    if off < 0 or off > 1000:
        raise PodctlError("%s=%d is out of range (0-1000)" % (TUNNEL_OFFSET_ENV, off))
    return off


def tunnel_locals(alias=None, env=None):
    """The local port for each service on this machine: the remote port plus this deployment's offset, so two
    machines never contend for one number. `alias` is accepted and ignored; it is kept so the call sites and
    the suite read the same as before 2.11.0."""
    off = tunnel_offset(env)
    return {name: remote + off for name, remote in TUNNEL_SERVICES}


def _block_for(alias, user, key):
    """The block as it would be written for this run, host and port left as {host} {port} (the name RUNPOD_BLOCK is kept for
    the suite and for anyone who read it before 2.2.0; the provider fills the rest)."""
    blk = SSH_BLOCK.replace("{alias}", alias).replace("{user}", user).replace("{key}", key)
    for name, local in tunnel_locals(alias).items():
        blk = blk.replace("{%s}" % name, str(local))
    return blk


RUNPOD_BLOCK = _block_for(SSH_ALIAS, SSH_USER, KEY_PATH)


def set_provider(prov, env=None):
    """Bind the run to one host: the alias, user, key and volume root every generic function reads from here on."""
    global PROVIDER, SSH_ALIAS, SSH_USER, KEY_PATH, VOLUME_ROOT, LEASE_PATH, LEGACY_LEASE_PATH, RUNPOD_BLOCK
    env = os.environ if env is None else env
    PROVIDER = prov
    SSH_ALIAS = (env.get("PODCTL_HOST") or "").strip() or prov.ssh_alias
    SSH_USER = prov.ssh_user
    KEY_PATH = str(prov.key_path)
    VOLUME_ROOT = prov.volume_root.rstrip("/") or "/"
    LEASE_PATH = (env.get("BASE_LOCAL_STATE", "").strip() or LOCAL_STATE).rstrip("/") + "/gpu.lease"
    LEGACY_LEASE_PATH = VOLUME_ROOT + "/comfy-base/state/gpu.lease"
    RUNPOD_BLOCK = _block_for(SSH_ALIAS, SSH_USER, KEY_PATH)
    return prov


def pkgs_dir():
    """Where zips are uploaded and extracted on the machine: <volume>/packages."""
    return VOLUME_ROOT + "/packages"


def logs_dir_remote():
    return VOLUME_ROOT + "/comfy-base/state/logs"


def provider_for(name=None, base_dir=None, env=None):
    """The provider for this run: --provider, else $PODCTL_PROVIDER, else the one brand.toml host above this base, else runpod.
    Two brands with two hosts in one repository make --provider required."""
    env = os.environ if env is None else env
    base = pathlib.Path(base_dir or BASE_DIR)
    name = (name or env.get("PODCTL_PROVIDER") or "").strip().lower()
    if not name:
        root = consumer_root(base, env)
        hosts = sorted({b["host"] for b in brands_at(root) if b.get("host")})
        if len(hosts) > 1:
            raise PodctlError("this repository has brands on %s: pass --provider <host>" % ", ".join(hosts))
        name = hosts[0] if hosts else "runpod"
    mod = _hosts.load_provider(name, base)
    cls = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Provider) and v is not Provider)
    return cls()


def write_host_env(prov, env=None):
    """state/host.env on the machine, so the base knows which host it runs on (BASE_HOST) and where its volume is (BASE_VOLUME)."""
    text = prov.host_env()
    r = ssh_run("mkdir -p %s && printf '%%s' %s > %s" % (_sq(VOLUME_ROOT + "/comfy-base/state"), _sq(text), _sq(VOLUME_ROOT + "/comfy-base/state/host.env")), env=env)
    if r.returncode != 0:
        raise PodctlError("could not write state/host.env on the machine: %s" % (r.stderr or r.stdout).strip()[-300:])
    return "host    %s (state/host.env: %s)" % (prov.name, " ".join(text.split()))


def write_ssh_config(path, host, port):
    """Point the run's `Host <alias>` block at host:port. Only the HostName and Port lines of that block change (plus, once, the
    `StrictHostKeyChecking accept-new` line of 2.0.28); every other byte of the file stays. A file without the block gets
    one appended (User, key, accept-new, the three forwards the base relies on)."""
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if re.match(r"^Host\s+%s\s*$" % re.escape(SSH_ALIAS), ln)), None)
    if start is None:
        sep = "" if text == "" or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        text = text + sep + SSH_BLOCK.format(alias=SSH_ALIAS, user=SSH_USER, key=KEY_PATH, host=host, port=port,
                                             **tunnel_locals(SSH_ALIAS))
    else:
        end = next((j for j in range(start + 1, len(lines)) if re.match(r"^Host\s", lines[j])), len(lines))
        for j in range(start + 1, end):
            if re.match(r"^\s*HostName\s", lines[j]):
                lines[j] = re.sub(r"(HostName\s+).*$", r"\g<1>" + host, lines[j])
            elif re.match(r"^\s*Port\s", lines[j]):
                lines[j] = re.sub(r"(Port\s+).*$", r"\g<1>%d" % int(port), lines[j])
        if not any(re.match(r"^\s*StrictHostKeyChecking\s", lines[j]) for j in range(start + 1, end)):
            # 2.0.28: a new pod is always an unknown host, and BatchMode refuses one under the default (ask) — accept-new, once
            port_at = next((j for j in range(start + 1, end) if re.match(r"^\s*Port\s", lines[j])), start)
            lines.insert(port_at + 1, "  StrictHostKeyChecking accept-new")
        text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- the image's own entry, and how the base wraps it


def ssh_banner(host, port, timeout=10):
    """True when something at host:port answers with an SSH banner. A mapped port alone proves nothing (RunPod
    allocates it for images that run no sshd at all)."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def mac_public_key(path=None):
    p = pathlib.Path(os.path.expanduser(str(path or (KEY_PATH + ".pub"))))
    if not p.exists():
        raise PodctlError("no public key at %s" % p)
    return p.read_text(encoding="utf-8").strip()


def _ssh_hostname():
    import subprocess
    r = subprocess.run(SSH_CMD + ["-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new", SSH_ALIAS, "hostname"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PodctlError("ssh %s hostname failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    return r.stdout.strip()


# ---------------------------------------------------------------- stop / start / restart, upload


SSH_CMD = ["ssh", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=accept-new"]
SCP_CMD = ["scp", "-q", "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=accept-new"]
TUNNEL_PORTS = [8188, 8888]      # ComfyUI, JupyterLab


def tunnel_port(v):
    """A --port value: a plain number stays an INT, as it always was, and LOCAL:REMOTE stays a string. Keeping the
    plain case an int means every caller and every test that predates the pair form is untouched (2.5.6)."""
    t = str(v)
    if ":" not in t:
        return int(t)
    local, _, remote = t.partition(":")
    int(local); int(remote)                       # both halves must be numbers, and say so here rather than in ssh
    return "%d:%d" % (int(local), int(remote))


def ssh_run(remote_cmd, env=None, timeout=None, input=None):
    """One command on the pod, no forwards, no TTY. Returns the CompletedProcess.

    `input` is fed to the remote command's stdin. That is the only way to hand a machine a SECRET: a value in
    remote_cmd becomes argv, and sshd runs a non-login command as `bash -c '<cmd>'`, so it lands in the remote
    /proc/<pid>/cmdline, which is world-readable on Linux, as well as in ps on this Mac. lib/boot.sh says the
    same thing about JUPYTER_TOKEN: it rides the environment, never argv."""
    import subprocess
    return subprocess.run(SSH_CMD + [SSH_ALIAS, remote_cmd], capture_output=True, text=True,
                          env=env or os.environ, timeout=timeout, input=input)


def tunnel_argv(ports=None, host=None, port=None, key=None, user=None):
    """The one ssh session that forwards: `podctl tunnel <pod>` runs this in the foreground (Claude backgrounds it).
    Explicit -L per port, keepalives, and a hard failure when a port is taken — never a silent half-tunnel.
    It ignores ~/.ssh/config (-F /dev/null) and addresses the pod by ip:port from the API: OpenSSH applies
    ClearAllForwardings after the whole command line, so the old `-o ClearAllForwardings=yes … -L …` cleared its own
    forwards and bound nothing (2.0.21), while the config's LocalForward lines would double-bind 8188."""
    argv = ["ssh", "-N", "-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes"]
    # 2.5.6: a port may be "LOCAL:REMOTE". With one GPU per workflow, every machine serves ComfyUI on 8188 and a
    # tunnel that binds 8188 locally can therefore only ever reach ONE of them: the second refuses with
    # ExitOnForwardFailure, which is honest but leaves the operator unable to see two products at once. A plain
    # "8188" still means 8188 both sides, so nothing that worked before changes.
    for p in (ports or TUNNEL_PORTS):
        local, _, remote = str(p).partition(":")
        remote = remote or local
        argv += ["-L", "%d:127.0.0.1:%d" % (int(local), int(remote))]
    return argv + ["-p", str(int(port)), "%s@%s" % (user or SSH_USER, host)]


def upload(files, to, env=None):
    """scp each file to <to>/ through the run's Host block, then compare size and sha256 on both sides. A mismatch fails."""
    import hashlib
    import subprocess
    env = env or os.environ
    files = [pathlib.Path(f) for f in files]
    for f in files:
        if not f.exists():
            raise PodctlError("no such file: %s" % f)
    r = ssh_run("mkdir -p %s" % _sq(to), env=env)
    if r.returncode != 0:
        raise PodctlError("ssh %s mkdir failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    r = subprocess.run(SCP_CMD + [str(f) for f in files] + ["%s:%s/" % (SSH_ALIAS, to)], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise PodctlError("scp failed: %s" % r.stderr.strip()[-300:])
    remote_cmd = "cd %s && for f in %s; do printf '%%s %%s  %%s\\n' \"$(stat -c %%s \"$f\")\" \"$(sha256sum \"$f\" | cut -d' ' -f1)\" \"$PWD/$f\"; done" % (
        _sq(to), " ".join(_sq(f.name) for f in files))
    r = ssh_run(remote_cmd, env=env)
    if r.returncode != 0:
        raise PodctlError("ssh %s sha256 failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    remote = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            remote[pathlib.Path(parts[2].strip()).name] = (int(parts[0]), parts[1])
    lines = []
    for f in files:
        size, digest = f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest()
        got = remote.get(f.name)
        if got != (size, digest):
            raise PodctlError("%s: size/sha256 differ after upload (local %d %s, remote %s)" % (f.name, size, digest[:12], got))
        lines.append("ok  %10d  %s  %s/%s" % (size, digest[:12], to, f.name))
    return "\n".join(lines)


def _sq(s):
    import shlex
    return shlex.quote(str(s))


# ---------------------------------------------------------------- install: the one-command Claude path (plan Part D1)
#
# The sequence proven by hand on 2026-09-05/06, for each item in order (the base first, then packages):
#   1. the local zip is current (package.py --check)      2. record the volume size, upload the zip
#   3. extract on the pod (/workspace/packages; a package into its own folder there, 2.0.17)
#   4. `--check` on the pod — non-zero stops everything
#   5. the real run, detached (BASE_RESTART=1 for a package; COMFY_REF=<ref> with --comfy-ref), its console under state/logs.
#      3.0.0: every run takes everything to its newest, so there is no --latest to pass and no --no-latest to hold it.
#   6. poll the console for "STEP RC=" (progress = the last section header)
#   7. copy the pod's state/logs beside the item (_build/podruns/<date>/pod-logs/), print the summary block
#   8. red → stop with the console's path; green → save the printed pin rows into the local script, rebuild its zip
# Every pod-side action goes through a PodIO so the whole sequence runs against a local stand-in in the suite.

PIN_ROW = re.compile(r'^\s*"([^|"\s]+)\|(https?://[^|"]+)\|([0-9a-f]{7,40})\|([^"]*)"\s*$', re.M)
STEP_RC = re.compile(r"^STEP RC=(\d+)\s*$", re.M)


def pin_rows(console_text):
    """The pin rows a run printed (its NODE PACKS section): (name, url, sha, rest), in order."""
    return [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in PIN_ROW.finditer(console_text)]


def save_pins(script_path, rows):
    """Rewrite the sha of every local PACKS row whose name AND url match a printed row ("latest, then tested, then saved").
    Rows the script does not declare are ignored. Returns [(name, old, new)] for what changed."""
    script_path = pathlib.Path(script_path)
    text = script_path.read_text(encoding="utf-8")
    changes = []
    for name, url, sha, _rest in rows:
        rx = re.compile(r'("%s\|%s\|)([0-9a-f]{7,40})(\|)' % (re.escape(name), re.escape(url)))
        m = rx.search(text)
        if m and m.group(2) != sha:
            text = text[:m.start(2)] + sha + text[m.end(2):]
            changes.append((name, m.group(2), sha))
    if changes:
        script_path.write_text(text, encoding="utf-8")
    return changes


def consumer_root(base_dir=None, env=None):
    """The consumer root this base serves (3.7.0): py/pkgdirs.py's one answer, both shapes. $BASE_WORKSPACE (the
    workspace shape: one repository per package side by side, this base read from $COMFY_BASE outside it), else
    <root> when this base is <root>/base/comfyui-base (the submodule shape), else None. A named root that does not
    exist is an error, said here rather than read as "no packages"."""
    try:
        return _pkgdirs.consumer_root(pathlib.Path(base_dir or BASE_DIR), None, os.environ if env is None else env)
    except _pkgdirs.NoSuchRoot as e:
        raise PodctlError(str(e))


# ---------------------------------------------------------------- 3.9.0: named ports
# A host whose console links ports (RunPod's Connect tab) shows a NAME for each one only if somebody gives it one. The base
# names its own two; every customer app a package ships gets a port of its own and the name the package declares in its
# script, APP_PORTS=("8189|<slug>|<name>"), which the machine's forwarder (py/app_proxy.py) serves at that port's root.
# podctl create, status, tunnel and a brand's template all read the same rows, so a console name, a port on the pod
# and a localhost link can never disagree. The base declares no app and knows none.
BASE_PORT_NAMES = [(8188, "ComfyUI"), (8888, "Jupyter Terminal")]


def app_rows_in(text):
    """The APP_PORTS rows of a package script's text, as written (`port|slug|name`), comments and blanks left out.
    Both shapes: APP_PORTS=("a" "b") on one line, and one row per line up to a line holding only `)`."""
    m = re.search(r'^APP_PORTS=\(([^\n]*)\)[ \t]*(?:#[^\n]*)?$', text, re.M) or re.search(r'^APP_PORTS=\((.*?)^\)', text, re.S | re.M)
    if not m:
        return []
    rows = []
    for ln in m.group(1).splitlines():
        s = ln.split("#", 1)[0] if not ln.strip().startswith('"') else ln
        rows.extend(re.findall(r'"([^"]*)"', s))
    return rows


def declared_apps(root=None, base_dir=None, env=None):
    """Every customer app the consumer root's packages declare: [(port, slug, name, package)], by port. A bad row, or a
    port two packages both claim, is an error naming both: a console would link one name to the other's app."""
    if root is None:
        root = consumer_root(base_dir, env)
    if root is None:
        return []
    out, seen = [], {}
    for d in _pkgdirs.package_dirs(pathlib.Path(root), pathlib.Path(base_dir or BASE_DIR)):
        script = d / ("%s-script.sh" % d.name)
        if not script.is_file():
            continue
        for raw in app_rows_in(script.read_text(encoding="utf-8")):
            try:
                port, slug, name = _appproxy.parse_row(raw)
            except ValueError as e:
                raise PodctlError("%s: APP_PORTS: %s" % (d.name, e))
            if port in seen:
                raise PodctlError("port %d is declared twice: %s (%s) and %s (%s); give one of them another port"
                                  % (port, seen[port][0], seen[port][1], d.name, slug))
            seen[port] = (d.name, slug)
            out.append((port, slug, name, d.name))
    return sorted(out)


def port_names(apps=()):
    """[(port, name)] for the base's two ports and each app, in port order after the base's own."""
    return list(BASE_PORT_NAMES) + [(int(a[0]), a[2]) for a in apps]


def apps_from_listing(text):
    """`app_proxy.py --list` (or an apps.d file) as read from a machine → [(port, slug, name)]; junk lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        try:
            r = _appproxy.parse_row(line)
        except ValueError:
            continue
        if r:
            out.append(r)
    return out


def links_lines(names, url_for, token=None, prefix="links"):
    """One line per named port: `<prefix>  <name padded>  <url>`, the terminal's with its token when there is one."""
    w = max([len(n) for _, n in names] + [0])
    lines = []
    for port, name in names:
        url = url_for(port)
        if url is None:
            continue
        if port == 8888:
            url = url.rstrip("/") + ("/lab?token=%s" % token if token else "/lab   (token: podctl jupyter <pod>)")
        lines.append("%-8s %-*s  %s" % (prefix, w, name, url))
    return lines


def brands_at(root):
    """The brands of a consumer root: the root itself when it holds brand.toml (the workspace shape keeps one brand
    at its root), else every <root>/<brand>/brand.toml (the submodule shape). None: no brands."""
    if root is None:
        return []
    root = pathlib.Path(root)
    if (root / "brand.toml").is_file():
        return [_brand.brand_of(root)]
    return _brand.brands_under(root)


def repo_root(path):
    """The repository root above a package or base path: the nearest ancestor holding base/comfyui-base/base.sh
    (the layout <repo>/<brand>/packages/<name>/ and <repo>/base/comfyui-base/). None when this base is its own
    repository with no brand tree above it."""
    p = pathlib.Path(path).resolve()
    for a in p.parents:
        if (a / "base" / "comfyui-base" / "base.sh").exists():
            return a
    return None                                            # 2.2.0: a base that is its own repository has no brand tree above it


def pins_elsewhere(script_path, rows, base_dir=None, env=None):
    """What a green install MEASURED for files the run does not own: every other package's script under the consumer
    root (py/pkgdirs.py: either shape, $BASE_WORKSPACE honoured) and the base's own pack list, base_dir/lib/40-packs.sh
    (this podctl's base by default) — WITHOUT writing any of them. Returns [(path, pack, old, new)].

    It used to write them. That is the fault behind three separate blockages on 2026-09-08: an install is a
    process whose job is to install, and it was mutating source files belonging to sessions that were not
    watching, leaving each one's zip stale against its tree — and `package.py --check` then refused the next
    upload by whoever touched that package next, for a pin they had never seen. A pin is a MEASUREMENT ("these
    commits were green on this pod at this time"), and a measurement belongs in an artefact that a human
    accepts, not in someone else's source file."""
    script_path = pathlib.Path(script_path).resolve()
    base_dir = pathlib.Path(base_dir or BASE_DIR)
    try:
        root = _pkgdirs.consumer_root(base_dir, None, os.environ if env is None else env)
    except _pkgdirs.NoSuchRoot as e:                    # a mistyped $BASE_WORKSPACE is said, never read as "no packages"
        raise PodctlError(str(e))
    targets = []
    for d in (_pkgdirs.package_dirs(root, base_dir) if root is not None else []):     # 3.6.0: both shapes, not */packages/* alone
        if d != script_path.parent:
            targets.append(d / ("%s-script.sh" % d.name))
    # 3.6.0: the base's OWN list, beside this driver. A workspace has no <root>/base/comfyui-base to read it from, and
    # a standalone base (no consumer root) still measures its own rows, where it used to report nothing at all.
    packs_sh = base_dir / "lib" / "40-packs.sh"
    if packs_sh.exists():
        targets.append(packs_sh)
    report = []
    for t in targets:
        text = t.read_text(encoding="utf-8")
        for name, url, sha, _rest in rows:
            m = re.search(r'("%s\|%s\|)([0-9a-f]{7,40})(\|)' % (re.escape(name), re.escape(url)), text)
            if m and m.group(2) != sha:
                report.append((t, name, m.group(2), sha))
    return report


def write_pins_artefact(base_dir, pod_id, slug, report, today=None):
    """The measurement, kept where it can be applied deliberately: who measured it, on which pod, when, and the
    one command that applies it. Under _build/, which is not in the shipped zip."""
    import datetime
    today = today or datetime.date.today().isoformat()
    out = pathlib.Path(base_dir) / "_build" / "pins" / ("%s-%s-%s.txt" % (today, pod_id, slug))
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# pack records measured by a green install: NOT applied: these files belong to other packages.",
             "# measured by %s on pod %s at %s" % (lease_me(), pod_id, datetime.datetime.now().isoformat(timespec="seconds")),
             "# apply deliberately, then run that package's suite and rebuild its zip:",
             "#     uv run \"base/comfyui-base/_build/pod/podctl.py\" pins %s --apply" % out.name, ""]
    for path, name, old, new in report:
        lines.append("%s|%s|%s|%s" % (path, name, old, new))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def apply_pins_artefact(path, base_dir):
    """Apply an artefact's rows to the files it names. Deliberate, by the session that owns them; it does NOT
    rebuild anything, because a zip rebuilt without its suite having run is what blocked an install today."""
    path = pathlib.Path(path)
    if not path.exists():
        path = pathlib.Path(base_dir) / "_build" / "pins" / path.name
    applied = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        target, name, old, new = line.rsplit("|", 3)
        t = pathlib.Path(target)
        if not t.exists():
            continue
        text = t.read_text(encoding="utf-8")
        if old in text:
            t.write_text(text.replace(old, new, 1), encoding="utf-8")
            applied.append((t.name, name, old, new))
    return applied


# ---------------------------------------------------------------- the GPU must be ours (2.0.26)
# nvidia-smi inside a container reports the whole card's memory but lists only that container's processes: memory used
# that nobody visible holds belongs to another container or a leaked host context, and every render dies at its first
# node with the card idle (93.5 GB held outside pod fakepod0000003, 2026-09-06 — every check had passed). The same rule
# as py/gpu_facts.py, asked over ssh BEFORE anything is uploaded; a suite test proves the two agree.
GPU_FOREIGN_LIMIT_MIB = 4096


def gpu_verdict(total, used, apps):
    ours = sum(m for _, m in apps)
    foreign = max(0, used - ours)
    return ours, foreign, ("ours" if foreign <= GPU_FOREIGN_LIMIT_MIB else "NOT-OURS")


def gpu_probe(io):
    """(line, verdict) from the pod: 'verdict=no-gpu' when nvidia-smi does not answer (a GPU-less pod, or the stand-in)."""
    rc, out, err = io.remote("nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits 2>/dev/null; echo ---; "
                             "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null")
    head, _, tail = (out or "").partition("---")
    nums = [x.strip() for x in head.strip().splitlines()[0].split(",")] if head.strip() else []
    if rc != 0 or len(nums) < 2 or not all(n.isdigit() for n in nums[:2]):
        return "verdict=no-gpu", "no-gpu"
    total, used = int(nums[0]), int(nums[1])
    apps = []
    for line in tail.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            apps.append((int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0))
    ours, foreign, v = gpu_verdict(total, used, apps)
    return "total=%d used=%d ours=%d foreign=%d procs=%d verdict=%s" % (total, used, ours, foreign, len(apps), v), v


def gpu_gate(io, say, uploading=True):
    """Print the verdict; True when the run may proceed. `uploading`: the caller was about to upload (install)."""
    line, v = gpu_probe(io)
    if v == "ours":
        say("gpu     %s — the GPU is ours" % line); return True
    if v == "no-gpu":
        say("gpu     no nvidia-smi answer on the pod — headroom not verified"); return True
    m = dict(kv.split("=") for kv in line.split())
    say("STOP: the pod's GPU is not ours — %d GiB of %d GiB are held by processes outside its container (ours: %d GiB, %s visible); "
        "every render would die at its first node.%s\n"
        "      fix: stop and start the pod (podctl stop --wait, then start --wait — a fresh container; the leak may live on this machine),\n"
        "      else redeploy the pod on the volume (a new machine), else RunPod support with the machine id (podctl status)."
        % (int(m["foreign"]) // 1024, int(m["total"]) // 1024, int(m["ours"]) // 1024, m["procs"], " Nothing was uploaded." if uploading else ""))
    return False


def gpu_prefer_of(script_path):
    """The GPU classes a package script declares (GPU_PREFER="B200 B300", 3.8.0), in its order; [] when it names none."""
    try:
        text = pathlib.Path(script_path).read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'^[^#\n]*?\bGPU_PREFER="([^"]*)"', text, re.M)        # the first assignment, never inside a comment
    return _gpu_fit.parse_prefer(m.group(1)) if m else []


def gpu_fit_note(io, items, say):
    """Before the upload, the pod's GPU against every package's GPU_PREFER: a GPU below what a package is tuned for is a
    WARNING with what it lacks, never a stop (the run is the person's call). Asked over ssh, the same three numbers the
    pod's own run reads (py/gpu_fit.py), so the Mac and the pod never disagree."""
    wants = [(it["name"], gpu_prefer_of(it["local_script"])) for it in items if it.get("kind") == "pkg" and it.get("local_script")]
    wants = [(n, p) for n, p in wants if p]
    if not wants:
        return
    rc, out, _ = io.remote("nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader,nounits 2>/dev/null")
    parts = [x.strip() for x in (out or "").strip().splitlines()[0].split(",")] if rc == 0 and (out or "").strip() else []
    if len(parts) < 3 or not parts[2].isdigit():
        say("gpu fit no nvidia-smi answer on the pod: %s not verified" % "; ".join("%s wants %s" % (n, _gpu_fit.want_text(p)) for n, p in wants))
        return
    name, cc, mib = parts[0], parts[1], int(parts[2])
    for n, p in wants:
        if _gpu_fit.fits(p, cc, mib, name) == "ok":
            say("gpu fit %s is what %s is tuned for (%s)" % (name, n, _gpu_fit.want_text(p)))
        else:
            say("WARNING: %s (%d GB, %s) is below what %s is tuned for: %s. The install goes on; it runs slower or with "
                "offloading. For the handbook's speed, deploy one of those (podctl create picks them by default on RunPod)."
                % (name, round(mib * 1024 * 1024 / 1e9), _gpu_fit.sm_of(cc), n, _gpu_fit.want_text(p)))


def item_for(directory):
    """What an item is on both sides: the base (its zip extracts to 'comfyui-base/…') or a package (a flat zip that
    extracts into '<Name>/…' — see below)."""
    d = pathlib.Path(directory).resolve()
    if (d / "base.sh").exists():
        return {"kind": "base", "dir": d, "name": "ComfyUI Base", "zip": d / "comfyui-base.zip",
                "script": "comfyui-base/comfyui-base-script.sh", "local_script": d / "comfyui-base-script.sh", "slug": "base"}
    name = d.name
    # a package zip is flat, so it extracts into its OWN folder: two packages both ship suite.py + pytest.ini, and flat
    # extraction let the second overwrite the first's. The base zip carries its own prefix.
    return {"kind": "pkg", "dir": d, "name": name, "zip": d / ("%s-%s.zip" % (name, _brand.host_of(d, "runpod"))), "script": "%s/%s-script.sh" % (name, name),
            "local_script": d / ("%s-script.sh" % name), "slug": re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()}


def item_for_zip(zip_path, logs=None):
    """An item from a ZIP alone, for a buyer who has no source tree (3.1.0): the base when the zip carries
    'comfyui-base/', else a package named by the one '<name>-script.sh' at its root. The zip is installed as it is:
    no packager check, no Mac gate, no pack records written back."""
    z = pathlib.Path(zip_path).resolve()
    if not z.is_file():
        raise PodctlError("%s: no such zip" % z)
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    logs = pathlib.Path(logs) if logs else z.parent / "podruns"
    if any(n.startswith("comfyui-base/") for n in names):
        return {"kind": "base", "dir": z.parent, "name": "ComfyUI Base", "zip": z, "script": "comfyui-base/comfyui-base-script.sh",
                "local_script": z.parent / ".no-local-script", "slug": "base", "buyer": True, "logs": logs}
    scripts = [n for n in names if "/" not in n and n.endswith("-script.sh")]
    if len(scripts) != 1:
        raise PodctlError("%s: not a package zip (want exactly one '<name>-script.sh' at its root, found %d)" % (z.name, len(scripts)))
    name = scripts[0][: -len("-script.sh")]
    return {"kind": "pkg", "dir": z.parent, "name": name, "zip": z, "script": "%s/%s" % (name, scripts[0]),
            "local_script": z.parent / ".no-local-script", "slug": re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower(),
            "buyer": True, "logs": logs}


class PodIO:
    """The real pod: ssh (no forwards), scp, the packager. `say` prints progress lines."""
    def __init__(self, api, pod, env=None, packager=None):
        self.api, self.pod, self.env = api, pod, env or os.environ
        self.provider = api if isinstance(api, Provider) else RunPodProvider(api)      # an Api is RunPod's; a provider is any host's
        self.packager = packager or (pathlib.Path(__file__).resolve().parents[1] / "package.py")
    def check_zip(self, d):
        import subprocess
        it = item_for(d)
        args = ["python3", str(self.packager)] + (["--base"] if it["kind"] == "base" else [str(it["dir"])]) + ["--check"]
        r = subprocess.run(args, capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise PodctlError("%s: the zip is not current — rebuild it (%s) and commit before installing:\n%s" % (it["name"], " ".join(args[:-1]), (r.stdout + r.stderr).strip()[-400:]))
        return it["zip"]
    def gate(self, d, say=None):
        """Rule: run the package's OWN suite from the EXTRACTED ZIP, on the Mac,
        BEFORE anything is uploaded — which is what the pod's `--check` does after the upload, ten minutes later.

        `check_zip` already proves the zip matches the working tree. It cannot prove the tree is GOOD: on
        2026-09-08 a base zip was rebuilt mid-iteration, matched the tree exactly, and carried a test that
        failed on the pod — another session picked it up in that window and its install died there instead of
        here. Extraction matters as much as the run: a test that reads `_build/` or an installed pack passes
        beside the repo and fails from the zip, which is two of the four uploads that day."""
        import shutil, subprocess, tempfile
        it = item_for(d)
        say = say or (lambda *_: None)
        # 3.2.0: a retry uploads the SAME zip; its suite already passed from that zip against the same base, so it is
        # not run again. The cache is the CLI's (PODCTL_GATE_CACHE, set by `podctl install`); only green enters it.
        mark = self._gate_mark(it)
        if mark is not None and mark.exists():
            say("gate    %s: this zip already passed its suite against this base (%s), not run again" % (it["name"], mark.name[:12]))
            return
        tmp = tempfile.mkdtemp(prefix="podctl-gate-")
        try:
            root = pathlib.Path(tmp)
            dest = root if it["kind"] == "base" else root / it["name"]
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(it["zip"]) as z:
                z.extractall(dest)
            # __file__ is <repo>/comfyui-base/_build/pod/podctl.py, so parents[2] IS the base directory —
            # not the repo root. Joining "ComfyUI Base" onto it again produced a COMFY_BASE that does not
            # exist and every package's own script correctly refused it (found when this gate blocked an
            # install). The base's own gate never noticed:
            # base.sh needs neither variable.
            base_dir = pathlib.Path(__file__).resolve().parents[2]
            script = (dest / "comfyui-base" / "base.sh") if it["kind"] == "base" else (dest / ("%s-script.sh" % it["name"]))
            if not script.exists():
                raise PodctlError("%s: the zip has no %s to run" % (it["name"], script.name))
            env = dict(self.env)
            # the testbed is in the base since 2.6.0, or beside it in a brand tree; first one that exists
            _tb = next((d for d in (base_dir / "testbed", base_dir.parent / "testbed") if (d / "main.py").exists()),
                       base_dir.parent / "testbed")
            env.setdefault("BASE_NODE_SRC", str(_tb))
            env.setdefault("BASE_SERVER", env.get("BASE_SERVER", "127.0.0.1:8199"))
            if it["kind"] != "base":
                env["COMFY_BASE"] = str(base_dir)
            for name in ("COMFY_BASE", "BASE_NODE_SRC"):     # a path that is not there makes the gate lie
                v = env.get(name)
                if v and not pathlib.Path(v).exists():
                    raise PodctlError("%s: the gate would run with %s=%s, which does not exist — the gate must "
                                      "not report a failure it caused itself" % (it["name"], name, v))
            r = subprocess.run(["bash", str(script), "test"], cwd=str(script.parent),
                               capture_output=True, text=True, env=env, timeout=1800)
            tail = (r.stdout + r.stderr).strip().splitlines()
            summary = next((l.strip() for l in reversed(tail) if "passed" in l or "failed" in l), "")
            if r.returncode != 0:
                raise PodctlError("%s: its own suite FAILS from the built zip — fix it here, not on the pod:\n%s"
                                  % (it["name"], "\n".join(tail[-25:])))
            say("gate    %s: %s" % (it["name"], summary or "suite green from the zip"))
            if mark is not None:
                mark.parent.mkdir(parents=True, exist_ok=True); mark.write_text(summary + "\n", encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _gate_mark(self, it):
        """The cache entry for this zip against this base: sha256(zip) + sha256(the base's MANIFEST.sha256), or None
        when no cache is configured."""
        import hashlib
        root = self.env.get("PODCTL_GATE_CACHE")
        if not root:
            return None
        base_mf = pathlib.Path(__file__).resolve().parents[2] / "MANIFEST.sha256"
        h = hashlib.sha256(pathlib.Path(it["zip"]).read_bytes())
        h.update(base_mf.read_bytes() if base_mf.exists() else b"")
        return pathlib.Path(root) / h.hexdigest()

    def upload(self, files, to):
        line = self.provider.record_volume(self.pod, env=self.env)
        if line:
            self.say(line)
        self.say(upload(files, to, env=self.env))
    def remote(self, cmd):
        r = ssh_run(cmd, env=self.env)
        return r.returncode, r.stdout, r.stderr
    def fetch(self, remote_dir, local_dir, newer_than=None):
        """The pod's logs as ONE gzipped tar stream over ssh. `scp <dir>/*` paid a round trip per file, and the directory
        keeps every run's log: 281 files on 2026-09-10, where each fetch took about five minutes of an install step (2.0.54).

        Two things 2.0.54 got wrong, both fixed here (2.0.55):

        * **The directory holds the LIVE comfyui.log.** GNU tar exits **1** ("file changed as we read it") when a file
          grows while it is being archived, and 2.0.54 read any non-zero exit as failure — so an install that finished
          green (STEP RC=0) could die on its own log, before the SUMMARY and before the pins were saved. The bytes are
          a valid archive either way, and the LOCAL extract is the judge of that: exit 1 with a clean extract is a pass.
        * **It copied every run's log every time**, so the cost grew with the history. `newer_than` is a path ON THE POD
          whose mtime is the cutoff — GNU tar's `--newer` takes a file name when the value starts with `/` or `.` — so
          the step's own console file selects exactly what that step wrote. Using the pod's own file, not a timestamp
          computed here, means a clock that differs between this Mac and the pod cannot silently drop the logs.
        """
        import subprocess
        local_dir = pathlib.Path(local_dir); local_dir.mkdir(parents=True, exist_ok=True)
        pick = ("--newer %s " % _sq(newer_than)) if newer_than else ""
        with subprocess.Popen(SSH_CMD + [SSH_ALIAS, "tar -C %s %s-czf - ." % (_sq(remote_dir), pick)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env) as src:
            dst = subprocess.run(["tar", "-xzf", "-", "-C", str(local_dir)], stdin=src.stdout, capture_output=True, env=self.env)
            src.stdout.close()
            err = src.stderr.read().decode(errors="replace")
        # GNU tar exits 1 for "some files differ" — here, a log that grew or rotated while it was being read, where
        # the archive is still whole. Anything else that exits 1 (a missing directory, a permission error) is a real
        # failure, and the exit code alone cannot tell them apart: an EMPTY stream extracts as an empty archive
        # without complaint (macOS bsdtar, measured), so a failed fetch would otherwise pass in silence. The stderr
        # text is what separates them.
        benign = ("file changed as we read it", "file removed before we read it", "socket ignored")
        notes = [l for l in err.strip().splitlines() if l.strip()]
        grew = src.returncode == 1 and bool(notes) and all(any(b in n for b in benign) for n in notes)
        if (src.returncode != 0 and not grew) or dst.returncode != 0:
            raise PodctlError("fetching the pod's logs failed: %s" % (err + dst.stderr.decode(errors="replace")).strip()[-300:])
        if grew:
            self.say("  (the pod kept writing while its logs were copied — that is expected)")
    def rebuild(self, d):
        import subprocess
        it = item_for(d)
        args = ["python3", str(self.packager)] + (["--base"] if it["kind"] == "base" else [str(it["dir"])])
        r = subprocess.run(args, capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise PodctlError("rebuild failed: %s" % (r.stdout + r.stderr).strip()[-300:])
    def sleep(self, s):
        import time
        time.sleep(s)
    def say(self, *a):
        print(*a, flush=True)


def _tail(text, n=25):
    lines = text.rstrip().splitlines()
    return "\n".join("    " + l for l in lines[-n:])


class _Flag:
    """A module-level switch for --no-gate: install()'s signature is part of the suite's contract."""
    value = False


args_no_gate = _Flag()


RESTART_REQUIRED = re.compile(r"restart required: ([^\n]+)")


def _env_prefix(run_env):
    """KEY=value pairs for a remote command line, each value quoted."""
    return "".join("%s=%s " % (k, _sq(str(v))) for k, v in (run_env or {}).items())


def _run_logged(io, name, slug, inner, every=30, timeout=3 * 3600):
    """One step on the machine, detached, polled through its console. Returns (rc, console text, console path);
    rc is None when the step could not start or outran the timeout (the reason is already said)."""
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    console = logs_dir_remote() + "/install_%s_%s.console" % (slug, ts)
    # braces: only the run goes to the background. `A && B && nohup X &` backgrounds the whole list in a subshell whose
    # stdout is the ssh channel, and that subshell waits for X — the launch ssh then lasted the whole step (2.0.19)
    rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && mkdir -p " + _sq(logs_dir_remote()) + " && { nohup setsid bash -c %s </dev/null > %s 2>&1 & }" % (_sq(inner), _sq(console)))
    if rc != 0:
        io.say("STOP: could not start %s on the pod (exit %d)\n%s" % (name, rc, _tail(out + err))); return None, "", console
    io.say("  running: %s\n  console: %s" % (inner, console))
    waited, last_hdr, text = 0, "", ""
    while True:
        rc, out, err = io.remote("cat %s 2>/dev/null" % _sq(console))
        text = out
        m = STEP_RC.search(text)
        if m:
            return int(m.group(1)), text, console
        hdrs = [l for l in text.splitlines() if l.startswith("══")]
        if hdrs and hdrs[-1] != last_hdr:
            last_hdr = hdrs[-1]; io.say("  %s" % last_hdr[:110])
        if waited >= timeout:
            io.say("STOP: %s did not finish within %d s — the run is still going on the pod; console: %s" % (name, timeout, console)); return None, text, console
        io.sleep(every); waited += every


def _preflight(io, env, script):
    """The pre-pass before a real run (3.2.0): `<script> preflight`, seconds, only what would stop the run before it
    changes anything. A machine whose installed base predates it answers with its usage line (exit 2): then the whole
    `--check`, as before. Returns (rc, out, err, the form that ran)."""
    rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && %sbash %s preflight" % (env, _sq(script)))
    if rc == 2 and "usage:" in (out + err):
        rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && %sbash %s --check" % (env, _sq(script)))
        return rc, out, err, "--check"
    return rc, out, err, "preflight"


def _install_base_only(io, it, every, timeout, today, suite=True, env=""):
    """Step one when a package install follows (3.2.0): install the base onto the volume, then its own suite only when
    this base version has not already passed on this machine (state/base-suite.ok: VERSION + the manifest's cksum).
    suite=False (a buyer's machine, BASE_NO_SUITE=1) installs only."""
    rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && %sBASE_REEXEC=1 bash comfyui-base/base.sh install-self" % env)
    if rc != 0:
        io.say("STOP: installing %s on the pod failed (exit %d)\n%s" % (it["name"], rc, _tail(out + err))); return False
    io.say("  installed on the volume (its packs, venv and restart are the package run's)")
    if not suite:
        io.say("  base suite: not run (BASE_NO_SUITE=1)"); return True
    home = VOLUME_ROOT + "/comfy-base"
    key = '"$(cat %s/VERSION) $(cksum < %s/MANIFEST.sha256)"' % (_sq(home), _sq(home))
    ok_file = _sq(home + "/state/base-suite.ok")
    inner = ('K=%s; if [ "$(cat %s 2>/dev/null)" = "$K" ]; then echo "  base suite already green on this machine for this base"; '
             'echo "STEP RC=0"; else %sbash %s test; rc=$?; if [ "$rc" = 0 ]; then printf "%%s\\n" "$K" > %s; fi; echo "STEP RC=$rc"; fi'
             % (key, ok_file, env, _sq(home + "/comfyui-base-script.sh"), ok_file))
    step_rc, text, console = _run_logged(io, it["name"] + " suite", it["slug"], inner, every=every, timeout=timeout)
    if step_rc is None:
        return False
    logs_dir = (it["logs"] / today / "pod-logs") if it.get("logs") else (it["dir"] / "_build" / "podruns" / today / "pod-logs")
    io.fetch(logs_dir_remote(), logs_dir, newer_than=console)
    io.say(_tail(text, 12))
    if step_rc != 0:
        io.say("STOP: the base's own suite is red on this machine: %s" % console); return False
    return True


def install(pod_id, dirs, io, every=5, timeout=3 * 3600, latest=True, save_pins_after=None, save_pins=True, today=None, comfy_ref=None, provider="verda",
            run_env=None, items=None):
    """Run the documented sequence for each directory in order. Returns 0 when every item ended green, 1 at the first
    failure (after printing what stopped it and where the console is). `io` is a PodIO or the suite's stand-in.
    3.1.0: `items` (from item_for_zip) installs zips a buyer was given, and `run_env` prefixes every remote run."""
    import datetime
    today = today or datetime.date.today().isoformat()
    items = items or [item_for(d) for d in dirs]
    for it in items:
        if not it["zip"].exists():
            io.say("STOP: %s has no zip (%s) — build it first" % (it["name"], it["zip"].name)); return 1
    if not gpu_gate(io, io.say):
        return 1
    gpu_fit_note(io, items, io.say)
    for it in items:
        io.say("══ %s ══" % it["name"])
        try:
            z = it["zip"] if it.get("buyer") else io.check_zip(it["dir"])
            if not it.get("buyer") and not getattr(args_no_gate, "value", False) and hasattr(io, "gate"):
                io.gate(it["dir"], io.say)
        except PodctlError as e:
            io.say("STOP: %s" % e); return 1
        io.upload([z], pkgs_dir())
        if it["kind"] == "base":
            rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && python3 -m zipfile -e %s ." % _sq(z.name))
        else:
            rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && mkdir -p %s && python3 -m zipfile -e %s %s" % (_sq(it["name"]), _sq(z.name), _sq(it["name"])))
        if rc != 0:
            io.say("STOP: extracting %s on the pod failed (exit %d)\n%s" % (z.name, rc, _tail(out + err))); return 1
        io.say("  extracted %s" % z.name)
        if it["kind"] == "base" and any(o["kind"] == "pkg" for o in items):
            # 3.2.0: a package follows, and its run does everything the base's own run would (the base's packs, the
            # venv, the newest pass, the restart): the base is installed, never run as a second whole pipeline. A buyer's
            # machine (BASE_NO_SUITE=1: no pytest there) skips the base's suite too.
            suite = (run_env or {}).get("BASE_NO_SUITE") != "1"
            if not _install_base_only(io, it, every, timeout, today, suite=suite, env=_env_prefix(run_env)):
                return 1
            io.say("  %s: green" % it["name"])
            continue
        env = (("COMFY_REF=%s " % _sq(comfy_ref)) if comfy_ref else "") + _env_prefix(run_env)
        rc, out, err, pre = _preflight(io, env, it["script"])     # carries run_env: a buyer's base_init checks the runtime too
        if rc != 0:
            io.say("STOP: %s %s exited %d on the pod, nothing was run:\n%s" % (it["name"], pre, rc, _tail(out + err))); return 1
        io.say("  %s ok" % pre)
        prefix = ("BASE_RESTART=1 " if it["kind"] == "pkg" else "") + (("COMFY_REF=%s " % comfy_ref) if comfy_ref else "") + _env_prefix(run_env)
        inner = '%sbash "%s"; echo "STEP RC=$?"' % (prefix, it["script"])      # double quotes: readable in the log, and _sq wraps the whole line
        step_rc, text, console = _run_logged(io, it["name"], it["slug"], inner, every=every, timeout=timeout)
        if step_rc is None:
            return 1
        logs_dir = (it["logs"] / today / "pod-logs") if it.get("logs") else (it["dir"] / "_build" / "podruns" / today / "pod-logs")
        io.fetch(logs_dir_remote(), logs_dir, newer_than=console)   # only what THIS step wrote (2.0.55)
        io.say("  logs → %s" % logs_dir)
        i = text.find("══ SUMMARY")
        io.say(text[i:] if i >= 0 else _tail(text, 40))
        if step_rc != 0:
            rr = RESTART_REQUIRED.search(text)
            if rr:
                io.say("STOP: %s needs the machine restarted before anything uses the GPU (%s)." % (it["name"], rr.group(1).strip()))
                io.say("      restart it (a person approves this):  podctl --provider %s restart %s --wait" % (provider, pod_id))
                io.say("      then run the same install again; the new driver is loaded and the run continues from there")
                return 1
            io.say("STOP: %s ended with STEP RC=%d — the log is the deliverable: %s" % (it["name"], step_rc, console)); return 1
        if save_pins and not it.get("buyer") and it["local_script"].exists():
            changes = save_pins_fn(it["local_script"], pin_rows(text))
            if changes:
                io.say("  pins saved into %s: %s" % (it["local_script"].name, ", ".join("%s %s→%s" % (n, o[:7], w[:7]) for n, o, w in changes)))
                io.rebuild(it["dir"]); io.say("  %s rebuilt — commit the script and the zip" % it["zip"].name)
            else:
                io.say("  pins unchanged (the printed rows equal the script's)")
            # A shared pack may not sit at two commits, so the same rows matter to every other package and to
            # the base's own list — but those files belong to other sessions. Report and record; never write.
            elsewhere = pins_elsewhere(it["local_script"], pin_rows(text))
            if elsewhere:
                art = write_pins_artefact(pathlib.Path(__file__).resolve().parents[2], pod_id, it["slug"], elsewhere, today)
                for path in sorted({c[0] for c in elsewhere}):
                    io.say("  pins MEASURED for %s (not written): %s" % (
                        path.name, ", ".join("%s %s→%s" % (c[1], c[2][:7], c[3][:7]) for c in elsewhere if c[0] == path)))
                io.say("  recorded in %s — apply it from that package's own session" % art)
        io.say("  %s: green" % it["name"])
    return 0


save_pins_fn = save_pins


# ---------------------------------------------------------------- 3.1.0: a buyer's machine
def runtime_src_remote():
    """Where a runtime handed over from this Mac lands on the machine (VOLUME_ROOT is the provider's, bound at run time)."""
    return VOLUME_ROOT + "/.comfy-base-runtime-src"


def runtime_manifest_remote():
    """The manifest runtime-apply keeps: what BASE_RUNTIME names on every install after it."""
    return VOLUME_ROOT + "/comfy-base/state/runtime.json"


def buyer_install(pod_id, base_zip, pkg_zips, runtime, io, jobs=1, every=5, timeout=3 * 3600, logs=None):
    """A buyer's install, one command: the base zip and the purchased package zips, from the proven runtime, the first
    flow first. `runtime` is the manifest: an https URL the machine fetches itself, or a local runtime.json whose parts
    sit beside it (uploaded; slow over a home line, meant for tests and a maintainer's own sweep). No packager check,
    no Mac gate, no pack records written anywhere: a buyer receives artefacts that already passed all three."""
    items = [item_for_zip(base_zip, logs)] + [item_for_zip(z, logs) for z in pkg_zips]
    if items[0]["kind"] != "base" or any(it["kind"] != "pkg" for it in items[1:]):
        raise PodctlError("buyer-install: --base-zip must be comfyui-base.zip and every other zip a package")
    if not gpu_gate(io, io.say):
        return 1
    base = items[0]
    io.say("══ runtime ══")
    io.upload([base["zip"]], pkgs_dir())
    rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && python3 -m zipfile -e %s ." % _sq(base["zip"].name))
    if rc != 0:
        io.say("STOP: extracting %s on the pod failed (exit %d)\n%s" % (base["zip"].name, rc, _tail(out + err))); return 1
    src = runtime
    if "://" not in runtime:
        man = pathlib.Path(runtime).resolve()
        if not man.is_file():
            raise PodctlError("buyer-install: no runtime manifest at %s" % man)
        parts = [man.parent / p["name"] for p in json.loads(man.read_text(encoding="utf-8")).get("parts", [])]
        missing = [str(p) for p in parts if not p.is_file()]
        if missing:
            raise PodctlError("buyer-install: the runtime's parts are not beside its manifest: %s" % ", ".join(missing[:3]))
        io.upload([man] + parts, runtime_src_remote())
        src = runtime_src_remote() + "/" + man.name
    inner = 'bash "%s" runtime-apply %s; echo "STEP RC=$?"' % (base["script"], _sq(src))
    step_rc, text, console = _run_logged(io, "runtime-apply", "runtime", inner, every=every, timeout=timeout)
    if step_rc is None:
        return 1
    if step_rc != 0:
        io.say(_tail(text, 30)); io.say("STOP: runtime-apply ended with STEP RC=%d; console: %s" % (step_rc, console)); return 1
    io.say("  runtime applied")
    run_env = {"BASE_RUNTIME": runtime_manifest_remote(), "BASE_STAGED": "1", "BASE_FETCH_JOBS": str(int(jobs)),
               "BASE_NO_SUITE": "1", "BASE_YES": "1", "BASE_NONINTERACTIVE": "1"}
    return install(pod_id, [], io, every=every, timeout=timeout, save_pins=False, items=items, run_env=run_env)


def buyer_comfy_dir(io):
    """The ComfyUI tree on the machine, as the base recorded it (state/boot.env), else the default."""
    rc, out, err = io.remote(". %s 2>/dev/null; echo \"${COMFY:-%s/ComfyUI}\"" % (_sq(VOLUME_ROOT + "/comfy-base/state/boot.env"), VOLUME_ROOT))
    return (out.strip().splitlines() or [VOLUME_ROOT + "/ComfyUI"])[-1]


def put_away(prov, pod_id, io, to, yes=False):
    """Copy the buyer's images (output/, input/) to this Mac, then delete the machine AND its disks (to Verda's trash,
    restorable for 96 hours): nothing bills while it is put away. Without `yes` it copies and stops there."""
    vols = prov.machine_volumes(pod_id)
    comfy = buyer_comfy_dir(io)
    to = pathlib.Path(to)
    for sub in ("output", "input"):
        rc, out, err = io.remote("test -d %s" % _sq(comfy + "/" + sub))
        if rc == 0:
            io.fetch(comfy + "/" + sub, to / sub)
            io.say("  %s/ -> %s" % (sub, to / sub))
    if not yes:
        io.say("copied. Nothing deleted: run again with --yes to delete the machine and its disks %s" % ", ".join(vols))
        return 2
    io.say(prov.stop(pod_id, wait=True))
    prov.delete_volumes(vols)
    io.say("put away: the machine and its disks (%s) are in Verda's trash; the images are in %s" % (", ".join(vols), to))
    return 0


# ---------------------------------------------------------------- CLI

def cmd_stop(prov, args):
    print(prov.stop(args.pod, wait=args.wait))


def cmd_start(prov, args):
    print(prov.start(args.pod, wait=args.wait, ssh_config=args.ssh_config))
    if args.wait:
        print(gpu_line_over_ssh())  # a fresh machine: the first thing to know is whether the card came back clean


def cmd_deploy(prov, args):
    pod_id = prov.deploy_like(args.like, name=args.name, gpu=args.gpu, wait=args.wait, ssh_config=args.ssh_config)
    if args.wait:
        print(gpu_line_over_ssh())
    print("next: podctl status %s · podctl stop %s --wait (the old machine; remove it in the console when the new one is proven) · podctl install %s --base" % (pod_id, args.like, pod_id))


def cmd_restart(prov, args):
    print(prov.restart(args.pod, wait=args.wait, ssh_config=args.ssh_config))
    if args.wait:
        print(gpu_line_over_ssh())


def cmd_upload(prov, args):
    line = prov.record_volume(prov.pod(args.pod))
    if line:
        print(line)
    print(upload(args.files, args.to or pkgs_dir()))


# ============================================================== the GPU lease: the pod is leased, not grabbed
# One pod, several sessions. One session's install once restarted ComfyUI in the middle of another's
# render, and two packages filed the same 288 MB model at two destinations because neither knew the other was
# working. The lease is the smallest thing that prevents the first: a file on the VOLUME (so it survives a pod
# restart and a new pod on the same volume) naming who holds the GPU, for what, and until when.
#
# It is ADVISORY and it EXPIRES. A crashed session must never lock the pod out, so an expired lease is not a
# blocker and never needs cleaning up; `--force-lease` overrides a live one when a human has decided.
#
# 2.5.5: the lease lives under BASE_LOCAL_STATE, NOT on the volume. It was written to
# $BASE_VOLUME/comfy-base/state/gpu.lease, which is per-machine only while the volume is. Under the shared root
# (2.5.0) /workspace IS one store that every machine mounts, so that single file became a GLOBAL MUTEX: three
# machines, three GPUs, one lease, and lease_blocks refusing whoever asked second. That is the exact opposite of
# the shape's whole claim, which is that installs serialise on the mkdir lock and RUNNING does not, so several
# GPUs can render at once. The rule it broke, and the one to apply to anything added here: anything about the
# MACHINE is per machine, anything about the WORK is shared. A lease is about a GPU, so it is the machine's.
#
# Deliberately NOT moved with it, having checked every VOLUME_ROOT-derived path rather than the one that bit:
# pkgs_dir() and upload --to are about the WORK and stay shared; state/logs/ is about the machine but the SHELL
# side writes there too (lib/10-discover.sh:5, lib/boot.sh), so moving only the driver's half would split every
# install's logs across two places, which is worse than one interleaved directory and wants a two-sided change.
LOCAL_STATE = "/var/lib/comfy-base-machine"      # lib/00-env.sh's BASE_LOCAL_STATE default
LEASE_PATH = LOCAL_STATE + "/gpu.lease"
LEGACY_LEASE_PATH = "/workspace/comfy-base/state/gpu.lease"
REMOTE_HOME = "/workspace/comfy-base"   # the base on the machine; the zip installs here


def lease_session_id(env=None):
    """The session id a blocked session needs in order to message the holder ($PODCTL_SESSION_ID), or ""."""
    env = env if env is not None else os.environ
    return env.get("PODCTL_SESSION_ID", "")


def lease_me(env=None):
    """This session's name in the lease: $PODCTL_SESSION, else user@host/pid — always something a person can ask."""
    env = env if env is not None else os.environ
    who = env.get("PODCTL_SESSION")
    if who:
        return who
    import getpass, socket
    try:
        return "%s@%s/%d" % (getpass.getuser(), socket.gethostname().split(".")[0], os.getpid())
    except Exception:                                        # pragma: no cover - defensive
        return "unknown/%d" % os.getpid()


def lease_encode(holder, purpose, minutes, now=None, session=None):
    """The file's one line. JSON so it can grow a field without breaking a reader that does not know it —
    `session` was added on 2026-09-08 at a waiting session's request: the holder's name says who has the GPU,
    the session id says whom to message."""
    import datetime, json as _json
    now = now or datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(minutes=max(1, int(minutes)))
    d = {"holder": holder, "purpose": purpose or "",
         "taken": now.isoformat(timespec="seconds"),
         "expires": exp.isoformat(timespec="seconds")}
    session = lease_session_id() if session is None else session
    if session:
        d["session"] = session
    return _json.dumps(d, sort_keys=True)


def lease_parse(text):
    """The lease, or None for empty/absent/corrupt — an unreadable lease is a free pod, never a stuck one."""
    import json as _json
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = _json.loads(text)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("holder") else None


def lease_expired(lease, now=None):
    import datetime
    if not lease:
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        exp = datetime.datetime.fromisoformat(lease["expires"])
    except (KeyError, ValueError, TypeError):
        return True                                          # no readable expiry: treat as expired, never as forever
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    return exp <= now


def lease_blocks(lease, me, now=None):
    """(blocked, why). Free, expired, or mine → not blocked. Someone else's live lease → blocked, and `why`
    is what to tell the person: who, doing what, until when."""
    if not lease or lease_expired(lease, now):
        return False, ""
    if lease.get("holder") == me:
        return False, ""
    return True, "the GPU is leased by %s%s until %s%s" % (
        lease["holder"], (" [%s]" % lease["session"]) if lease.get("session") else "",
        lease.get("expires", "?"),
        (" — %s" % lease["purpose"]) if lease.get("purpose") else "")


def lease_read(timeout=30):
    """The lease on the pod, or None. A pod that cannot be reached reads as free: this is advisory.

    Reads the machine's own lease. A pre-2.5.5 lease on the shared volume is reported by
    lease_legacy_note() rather than obeyed: obeying it would reinstate the global mutex this release
    exists to remove, and it may belong to a different machine entirely."""
    try:
        r = ssh_run("cat %s 2>/dev/null || true" % LEASE_PATH, timeout=timeout)
    except Exception:                                        # pragma: no cover - defensive
        return None
    return lease_parse(r.stdout if r.returncode == 0 else "")


def lease_legacy_note(timeout=30):
    """One line if a live pre-2.5.5 lease is still sitting on the shared volume, else "".

    Not a blocker. It is information: during the changeover a session on the old code may hold it,
    and a human deciding whether to wait should be told rather than left to wonder why a machine
    that reads as free has someone working on it."""
    try:
        r = ssh_run("cat %s 2>/dev/null || true" % LEGACY_LEASE_PATH, timeout=timeout)
    except Exception:                                        # pragma: no cover - defensive
        return ""
    old = lease_parse(r.stdout if r.returncode == 0 else "")
    if not old or lease_expired(old):
        return ""
    return ("note    a pre-2.5.5 lease is still on the SHARED volume, held by %s until %s%s. It is not obeyed "
            "(it may be another machine's); %s is this machine's." % (
                old.get("holder", "?"), old.get("expires", "?"),
                (" for %s" % old["purpose"]) if old.get("purpose") else "", LEASE_PATH))


def lease_minutes_keeping(current, holder, minutes, now=None):
    """Never SHORTEN a lease we already hold: a session that took 150 minutes for a long chain and then ran an
    install (which asks for its own, smaller window) had its own lease cut to 90 — measured 2026-09-08, and the
    waiting session saw a window that did not match what it had been told."""
    import datetime
    if not current or current.get("holder") != holder or lease_expired(current, now):
        return minutes
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        exp = datetime.datetime.fromisoformat(current["expires"])
    except (KeyError, ValueError, TypeError):
        return minutes
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    left = int((exp - now).total_seconds() // 60)
    return max(int(minutes), left)


def lease_take(holder, purpose, minutes, timeout=30):
    """Write the lease. The caller decides whether it is allowed to (lease_blocks)."""
    import shlex
    line = lease_encode(holder, purpose, minutes)
    r = ssh_run("mkdir -p %s && printf '%%s\\n' %s > %s" % (
        shlex.quote(os.path.dirname(LEASE_PATH)), shlex.quote(line), shlex.quote(LEASE_PATH)), timeout=timeout)
    if r.returncode != 0:
        raise PodctlError("could not write the lease: %s" % (r.stderr or r.stdout).strip()[:200])
    return lease_parse(line)


def lease_release(holder, timeout=30):
    """Drop the lease if it is ours (or already gone). Never removes someone else's."""
    cur = lease_read(timeout=timeout)
    if cur and cur.get("holder") != holder:
        return False
    ssh_run("rm -f %s" % LEASE_PATH, timeout=timeout)
    return True


def cmd_pins(api, args):
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    path = pathlib.Path(args.artefact)
    if not path.exists():
        path = base_dir / "_build" / "pins" / path.name
    if not path.exists():
        raise PodctlError("no such pins artefact: %s" % args.artefact)
    if not args.apply:
        print(path.read_text(encoding="utf-8").rstrip()); return 0
    applied = apply_pins_artefact(path, base_dir)
    for fname, name, old, new in applied:
        print("pins    %s: %s %s→%s" % (fname, name, old[:7], new[:7]))
    print("pins    %d row(s) applied — now run that package's suite and rebuild its zip" % len(applied))
    return 0


def cmd_lease(api, args):
    me = args.holder or lease_me()
    if args.release:
        ok = lease_release(me)
        print("lease   released" if ok else "lease   NOT released — it belongs to someone else")
        return 0 if ok else 3
    cur = lease_read()
    if not args.take:
        note = lease_legacy_note()
        if note:
            print(note)
        if not cur:
            print("lease   free")
        else:
            print("lease   %s%s%s · taken %s · expires %s%s" % (
                cur["holder"], (" [%s]" % cur["session"]) if cur.get("session") else "",
                " (EXPIRED)" if lease_expired(cur) else "",
                cur.get("taken", "?"), cur.get("expires", "?"),
                (" · %s" % cur["purpose"]) if cur.get("purpose") else ""))
        return 0
    blocked, why = lease_blocks(cur, me)
    if blocked and not args.force:
        print("lease   REFUSED — %s\n        wait, ask that session, or pass --force" % why)
        return 3
    got = lease_take(me, args.purpose, args.minutes)
    print("lease   held by %s until %s%s" % (got["holder"], got["expires"],
                                             (" · %s" % got["purpose"]) if got.get("purpose") else ""))
    return 0


def resolve_pkg_dir(root, d):
    """A --pkg value as a directory: absolute as given; else relative to the consumer root (<name> in the workspace
    shape, <brand>/packages/<name> in the submodule shape); else a bare <name> that exactly one */packages/<name>
    matches. Never the base folder's parent, which is what 2.0.55 joined it onto and which holds no packages."""
    p = pathlib.Path(d)
    if p.is_absolute():
        return p
    if root is None:
        raise PodctlError("--pkg %r: no consumer root (set $BASE_WORKSPACE to the workspace, or keep this base at <repo>/base/comfyui-base), so pass an absolute package path" % d)
    if (root / d).is_dir():
        return root / d
    hits = sorted(x for x in root.glob("*/packages/%s" % d) if x.is_dir())
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise PodctlError("--pkg %r is in more than one brand (%s): pass the <brand>/packages/<name> path" % (d, ", ".join(str(h.relative_to(root)) for h in hits)))
    raise PodctlError("--pkg %r is not a directory under %s (try <name> in a workspace, <brand>/packages/<name> in a brand repository)" % (d, root))


# The old-layout folders the 2026-09-12 rename left on the volume: a package used to be /workspace/packages/<Display Name>/
# with '<Display Name> Script.sh' inside, and the base extracted to 'ComfyUI Base/'. The new install lands beside them
# (/workspace/packages/<name>/ with <name>-script.sh), so a folder is stale exactly when it has no <name>-script.sh and
# either is 'ComfyUI Base' or holds a '* Script.sh'. Nothing else on the volume is touched, and the scan is read-only.
STALE_SCAN = (
    'cd /workspace/packages 2>/dev/null || exit 0; for d in */; do d="${d%/}"; [ -f "$d/$d-script.sh" ] && continue; '
    'if [ "$d" = "ComfyUI Base" ] || ls "$d"/*" Script.sh" >/dev/null 2>&1; then printf \'%s\\n\' "$d"; fi; done'
)


def stale_package_dirs(io):
    """The old-layout folder names under /workspace/packages (see STALE_SCAN), sorted; [] when there are none or no packages dir."""
    rc, out, err = io.remote(STALE_SCAN.replace("/workspace", VOLUME_ROOT))
    return sorted(l for l in (out or "").splitlines() if l.strip())


def stale_workflows_cmd(names):
    """The shell that lists the old shipped workflows those folders put into ComfyUI's browser ('<Display Name> Workflow.json'
    under any /workspace/*/user/default/workflows), by exact name, so a user's own file is never matched."""
    tests = " -o ".join("-name %s" % _sq("%s Workflow.json" % n) for n in names if n != "ComfyUI Base")
    if not tests:
        return "true"
    return "find " + VOLUME_ROOT + "/*/user/default/workflows -maxdepth 1 -type f \\( %s \\) 2>/dev/null" % tests


def prune(io, say, yes=False):
    """List (and with yes=True remove) the old-layout package folders and their workflow copies. Returns the exit code:
    0 with nothing to do or after a removal, 0 after a dry run that found something (the list is the result)."""
    stale = stale_package_dirs(io)
    if not stale:
        say("prune   nothing from the old layout under %s" % pkgs_dir()); return 0
    rc, out, err = io.remote(stale_workflows_cmd(stale))
    wfs = sorted(l for l in (out or "").splitlines() if l.strip())
    for d in stale:
        say("prune   old layout: %s/%s" % (pkgs_dir(), d))
    for w in wfs:
        say("prune   old workflow: %s" % w)
    if not yes:
        say("prune   dry run: pass --yes to remove them (the new <name>/ folders are untouched; a re-install puts a package back)"); return 0
    cmd = "cd " + _sq(pkgs_dir()) + " && rm -rf -- " + " ".join(_sq(d) for d in stale)
    if wfs:
        cmd += " && rm -f -- " + " ".join(_sq(w) for w in wfs)
    rc, out, err = io.remote(cmd)
    if rc != 0:
        raise PodctlError("prune failed (exit %s): %s" % (rc, (err or out).strip()[-400:]))
    say("prune   removed %d folder(s) and %d workflow copy(ies)" % (len(stale), len(wfs)))
    return 0


def cmd_prune(prov, args):
    prov.pod(args.pod)                                   # the machine must exist; the Host block must point at it (podctl ssh-config)
    return prune(_SshIO(), print, yes=args.yes)


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def env_pair(text):
    """argparse type for --env KEY=VALUE: (KEY, VALUE). The value may be empty and may hold '='; the name must be a shell name."""
    name, eq, value = text.partition("=")
    if not eq or not ENV_NAME.match(name):
        raise argparse.ArgumentTypeError("%r is not KEY=VALUE with KEY a shell variable name" % text)
    return (name, value)


def install_run_env(args, pod=None):
    """3.7.3: what `install` prefixes to every remote run: --env pairs in order (a later one wins), then --no-suite's
    BASE_NO_SUITE=1, which also stops the base's own suite step (install() reads it from run_env).
    3.7.4: the machine's own BASE_LISTEN (a `create --private` pod sets 127.0.0.1) rides along unless --env names one:
    an install restarts ComfyUI from an ssh shell, which does not see the pod's env, and would bind 0.0.0.0 there."""
    run_env = dict(getattr(args, "env", None) or [])
    listen = ((pod or {}).get("env") or {}).get("BASE_LISTEN") if isinstance(pod, dict) else None
    if listen and "BASE_LISTEN" not in run_env:
        run_env["BASE_LISTEN"] = listen
    if getattr(args, "no_suite", False):
        run_env["BASE_NO_SUITE"] = "1"
    return run_env


def cmd_install(prov, args):
    if getattr(args, "no_latest", False):
        print("STOP: --no-latest is retired in 3.0.0: every install takes ComfyUI, the packs and every package to their newest")
        return 2
    dirs = []
    here = pathlib.Path(__file__).resolve().parents[2]
    if args.base:
        dirs.append(here)
    root = consumer_root(here)
    for d in args.pkg or []:
        dirs.append(resolve_pkg_dir(root, d))
    if not dirs:
        raise PodctlError("nothing to install: --base and/or --pkg <dir>")
    pod = prov.pod(args.pod)
    # The lease before the upload: an install restarts ComfyUI, which kills whatever another session is
    # rendering or training. Advisory and expiring — see the lease block above.
    args_no_gate.value = bool(getattr(args, "no_gate", False))
    me = lease_me()
    blocked, why = lease_blocks(lease_read(), me)
    if blocked and not getattr(args, "force_lease", False):
        print("STOP: %s\n      wait for it, message that session, or pass --force-lease" % why)
        return 3
    minutes = max(20, int(args.timeout / 60) * len(dirs) // 4 or 20)
    minutes = lease_minutes_keeping(lease_read(), me, minutes)      # an install inside a longer chain keeps the chain's window
    try:
        got = lease_take(me, "install: " + ", ".join(d.name for d in dirs), minutes)
        print("lease   held by %s until %s" % (got["holder"], got["expires"]))
    except PodctlError as e:
        print("lease   could not be taken (%s) — continuing; it is advisory" % e)
    try:
        print(write_host_env(prov))
        if getattr(args, "comfy_ref", None):
            print("comfyui ref %s (from --comfy-ref; this install only: the next one without it returns the store to the newest release)" % args.comfy_ref)
        run_env = install_run_env(args, pod)
        if run_env:
            print("env     every run on the machine gets: %s" % ", ".join(sorted(run_env)))
        env = dict(os.environ); env.setdefault("PODCTL_GATE_CACHE", str(pathlib.Path.home() / ".cache" / "podctl" / "gate"))
        return install(args.pod, dirs, PodIO(prov, pod, env=env), every=args.every, timeout=args.timeout, save_pins=args.save_pins,
                       comfy_ref=getattr(args, "comfy_ref", None), provider=getattr(prov, "name", "verda"), run_env=run_env or None)
    finally:
        lease_release(me)


def tunnel_key(pubkey_arg, env=None):
    """The private key beside --pubkey: the flag, else $PODCTL_PUBKEY, else ~/.ssh/id_ed25519.pub (the flag's default is
    None when nothing was given — 2.0.23: the tunnel crashed on it)."""
    env = os.environ if env is None else env
    pub = pubkey_arg or env.get("PODCTL_PUBKEY") or (KEY_PATH + ".pub")
    return re.sub(r"\.pub$", "", os.path.expanduser(pub))


def tunnel_facts_cmd():
    """What the tunnel reads from the machine before it forwards (3.9.0): the JupyterLab token alone (never the rest of
    tokens.env) and the app ports the machine serves, one `port|slug|name` per line."""
    return ("sed -n 's/^JUPYTER_TOKEN=//p' %s/state/tokens.env 2>/dev/null | tail -1; echo '--apps--'; "
            "cat %s/state/apps.d/*.list 2>/dev/null; true" % (REMOTE_HOME, REMOTE_HOME))


def parse_tunnel_facts(text):
    """tunnel_facts_cmd's answer -> (token or None, [(port, slug, name)])."""
    head, _, apps = (text or "").partition("--apps--")
    tok = head.strip().splitlines()[-1].strip() if head.strip() else ""
    return (tok or None), apps_from_listing(apps)


def tunnel_plan(ports, given, machine_apps, declared, token, names=None):
    """The ports to forward and the named localhost links to print: [ports], [lines].
    Without --port: ComfyUI, JupyterLab and every app port the machine serves (else the ones this tree declares).
    With --port: exactly those. A forwarded port is named by the base, then the machine's apps, then this tree's."""
    apps = list(machine_apps or []) or [(a[0], a[1], a[2]) for a in (declared or [])]
    ports = list(ports or TUNNEL_PORTS)
    if not given:
        for a in apps:
            if int(a[0]) not in [int(str(p).rpartition(":")[2]) for p in ports]:
                ports.append(int(a[0]))
    named = dict(names or BASE_PORT_NAMES)
    for a in reversed(list(declared or [])):
        named.setdefault(int(a[0]), a[2])
    for a in apps:
        named[int(a[0])] = a[2]
    pairs = []
    for p in ports:
        local, _, remote = str(p).partition(":")
        remote = int(remote or local)
        pairs.append((remote, int(local), named.get(remote, "port %d" % remote)))
    w = max([len(n) for _, _, n in pairs] + [0])
    lines = []
    for remote, local, name in pairs:
        url = "http://localhost:%d" % local
        if remote == 8888:
            url += "/lab?token=%s" % token if token else "/lab   (no token read: podctl jupyter <pod>)"
        lines.append("tunnel  %-*s  %s" % (w, name, url))
    return ports, lines


def cmd_tunnel(prov, args):
    import subprocess
    addr = prov.address(prov.pod(args.pod))
    if addr is None:
        print("tunnel  the machine has no reachable address yet (podctl ensure / start --wait)"); return 1
    key = tunnel_key(getattr(args, "pubkey", None))
    token, machine_apps = None, []
    probe = ["ssh", "-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=15", "-p", str(int(addr.port)), "%s@%s" % (addr.user or SSH_USER, addr.host), tunnel_facts_cmd()]
    try:
        r = subprocess.run(probe, capture_output=True, text=True, timeout=40)
        if r.returncode == 0:
            token, machine_apps = parse_tunnel_facts(r.stdout)
        else:
            print("tunnel  (the machine did not answer the names probe: forwarding the defaults)")
    except (subprocess.TimeoutExpired, OSError):
        print("tunnel  (the names probe timed out: forwarding the defaults)")
    try:
        declared = declared_apps()
    except PodctlError as e:
        print("tunnel  (this tree's APP_PORTS are unreadable: %s)" % e); declared = []
    ports, lines = tunnel_plan(args.port, getattr(args, "port_given", False), machine_apps, declared, token)
    for line in lines:
        print(line)
    argv = tunnel_argv(ports, host=addr.host, port=addr.port, key=key, user=addr.user)
    print("tunnel  %s" % " ".join(argv), flush=True)
    return subprocess.call(argv)


# ---------------------------------------------------------------- Comfy MCP: the agent's way in
# comfy-mcp is a stdio server. Two transports, and the difference is not cosmetic:
#
#   ssh     the server runs ON the machine, so every tool works — install_node, search_models, get_logs,
#           fetch_outputs, launch_comfyui. The client speaks MCP down an ssh pipe.
#   tunnel  the server runs on this Mac and reaches ComfyUI over COMFYUI_URL through `podctl tunnel`. The
#           run tools work; the ones that need the tree do not, because the tree is on the other machine.
#
# ssh is the default because the smaller set is rarely what anyone wanted. ClearAllForwardings is not
# optional: the Host block ssh-config writes carries four LocalForward lines, and a second session binding
# them while `podctl tunnel` holds them prints warnings onto the stdio channel MCP is trying to speak.
MCP_ENV = {"DO_NOT_TRACK": "1", "COMFY_NO_TELEMETRY": "1"}   # comfy-cli ships mixpanel and posthog; rule 9 says nothing is uploaded
MCP_VENV_DIRNAME = ".venv-cu130"


def mcp_default_bin(volume_root=None):
    """Where lib/45-mcp.sh puts comfy-mcp: the run's venv, beside ComfyUI on the volume. A machine whose tree
    is somewhere else is what --bin is for; `mcp` resolves the real path over ssh when the pod answers."""
    root = (volume_root if volume_root is not None else VOLUME_ROOT).rstrip("/")
    return "%s/ComfyUI/%s/bin/comfy-mcp" % (root, MCP_VENV_DIRNAME)


def mcp_comfy_bin(mcp_bin):
    """comfy-mcp is a wrapper: its lifecycle and discovery tools shell out to comfy-cli, and it finds that
    through COMFY_BIN or PATH. An MCP client is usually launched by a GUI, whose PATH is not your shell's, so
    PATH is the thing not to rely on. MEASURED against a live ComfyUI: without COMFY_BIN, server_info returns
    "Error executing tool server_info"; with it, it answers. `comfy` sits beside `comfy-mcp` in the venv."""
    return posixpath.join(posixpath.dirname(mcp_bin), "comfy") if mcp_bin else None


def mcp_server(alias, mode="ssh", bin_path=None, port=None, comfy_bin=None):
    """One `mcpServers` entry, as a client reads it. Pure: the suite builds it without a pod."""
    if mode == "ssh":
        mcp_bin = bin_path or mcp_default_bin()
        env = dict(MCP_ENV); env["COMFY_BIN"] = comfy_bin or mcp_comfy_bin(mcp_bin)
        # the environment goes in the REMOTE command: an MCP client's "env" is applied here, and would
        # configure the local ssh process instead of the server on the machine
        remote = " ".join("%s=%s" % (k, v) for k, v in sorted(env.items())) + " exec " + mcp_bin
        return {"command": "ssh",
                "args": ["-o", "ClearAllForwardings=yes", "-o", "BatchMode=yes", alias, remote]}
    if mode == "tunnel":
        local = port if port is not None else tunnel_locals(alias)["comfy"]
        env = dict(MCP_ENV); env["COMFYUI_URL"] = "http://127.0.0.1:%d" % int(local)
        # comfy-mcp runs HERE in this mode, so name this machine's comfy when it can be found
        found = comfy_bin or shutil.which("comfy")
        if found:
            env["COMFY_BIN"] = found
        return {"command": "comfy-mcp", "env": env}
    raise PodctlError("unknown mcp transport %r (ssh | tunnel)" % mode)


def mcp_merge(existing, name, server):
    """Add or replace one server, keeping every other one. A .mcp.json is often not only ours."""
    out = dict(existing or {})
    servers = dict(out.get("mcpServers") or {})
    servers[name] = server
    out["mcpServers"] = servers
    return out


def cmd_mcp(prov, args):
    import json
    alias = SSH_ALIAS
    name = "comfy-%s" % alias
    bin_path = getattr(args, "bin", None)
    if args.over == "ssh" and not bin_path:
        # ask the machine where its comfy-mcp actually is; the computed default is only a fallback
        r = ssh_run("ls -1 %s/*/%s/bin/comfy-mcp %s 2>/dev/null | head -1" % (_sq(VOLUME_ROOT), MCP_VENV_DIRNAME, _sq(mcp_default_bin())))
        found = (r.stdout or "").strip().splitlines()
        bin_path = found[0].strip() if r.returncode == 0 and found else None
        if not bin_path:
            bin_path = mcp_default_bin()
            print("mcp     could not ask %s where comfy-mcp is; using %s (override with --bin)" % (alias, bin_path), file=sys.stderr)
    cfg = mcp_merge({}, name, mcp_server(alias, args.over, bin_path=bin_path))
    if args.print_only:
        print(json.dumps(cfg, indent=2))
        return 0
    dest = pathlib.Path(args.out)
    if dest.exists():
        try:
            cfg = mcp_merge(json.loads(dest.read_text(encoding="utf-8")), name, mcp_server(alias, args.over, bin_path=bin_path))
        except ValueError as e:
            raise PodctlError("%s is not valid JSON (%s) — move it aside or use --print" % (dest, e))
    dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print("mcp     wrote %s  (server %s, over %s)" % (dest, name, args.over))
    if args.over == "tunnel":
        print("mcp     needs `podctl tunnel %s` running; ComfyUI is on local port %d" % (args.pod, tunnel_locals(alias)["comfy"]))
        if not cfg["mcpServers"][name]["env"].get("COMFY_BIN"):
            print("mcp     no `comfy` on this PATH, so COMFY_BIN is not set: comfy-mcp's discovery and lifecycle "
                  "tools will fail if your client's PATH differs from your shell's. Add it by hand, or use --over ssh.",
                  file=sys.stderr)
    return 0


def cmd_jupyter(prov, args):
    """The URL, with the token already in it, ready to paste into a browser.

    2.5.6: boot.sh generates a JupyterLab token and writes it to state/tokens.env owner-only, and deliberately does
    NOT print it — the boot log is a file on the volume, and with a shared store that volume is mounted by every
    machine. So the token is fetched over the ssh channel, on demand, by whoever can already reach the machine.
    The local port is the alias's own (tunnel_locals), because with one GPU per workflow every machine serves 8888
    and a single number could only ever reach one of them."""
    r = ssh_run("cat %s/state/tokens.env 2>/dev/null" % REMOTE_HOME, timeout=30)
    if r.returncode != 0:
        print("jupyter  Host %s does not answer (podctl ssh-config first)" % SSH_ALIAS); return 1
    tok = ""
    for line in r.stdout.splitlines():
        if line.startswith("JUPYTER_TOKEN="):
            tok = line.split("=", 1)[1].strip()
    if not tok:
        print("jupyter  no token on the machine yet — it is written the first time boot.sh starts JupyterLab.")
        print("jupyter  If ComfyUI is running but JupyterLab is not, restart the machine's boot (podctl ensure).")
        return 1
    local = tunnel_locals(SSH_ALIAS)["jupyter"]
    try:                                       # 3.9.0: a pod that exposes 8888 through its host's proxy: that link first
        url = prov.http_url(prov.pod(args.pod), 8888) if hasattr(prov, "http_url") else None
    except PodctlError:
        url = None
    if url:
        print("jupyter  Jupyter Terminal:  %s/lab?token=%s" % (url, tok))
    print("jupyter  tunnel:  ssh -L %d:127.0.0.1:8888 %s -N" % (local, SSH_ALIAS))
    print("jupyter  then:    http://127.0.0.1:%d/lab?token=%s" % (local, tok))
    return 0


class _SshIO:
    """The smallest PodIO for a read-only probe over `Host runpod` (a timeout: `status` must never hang on a dead pod)."""
    def remote(self, cmd):
        r = ssh_run(cmd, timeout=40)
        return r.returncode, r.stdout, r.stderr


def gpu_line_over_ssh():
    """The GPU verdict as one printable line, best-effort: no ssh answer is a note, never an error."""
    import subprocess
    try:
        r = ssh_run("true", timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return "gpu     (ssh did not answer within 20 s — podctl ssh-config, then again)"
    if r.returncode != 0:
        return "gpu     (Host %s does not answer — podctl ssh-config first)" % SSH_ALIAS
    lines = []
    gpu_gate(_SshIO(), lines.append, uploading=False)
    return "\n".join(lines)


def status_links(prov, pod, declared, machine=None):
    """3.9.0: the named links a status prints for a pod its host proxies: [lines]. machine is (token, apps) read over
    ssh, or None. The pod's own JUPYTER_TOKEN wins over the machine's saved one."""
    if not hasattr(prov, "http_url"):
        return []
    tok_m, apps_m = machine or (None, [])
    token = (prov.pod_token(pod) if hasattr(prov, "pod_token") else None) or tok_m
    names = dict(port_names(declared))
    for a in apps_m:
        names[int(a[0])] = a[2]
    exposed = sorted(int(p.split("/")[0]) for p in (pod.get("ports") or []) if p.endswith("/http"))
    order = [p for p, _ in BASE_PORT_NAMES if p in exposed] + [p for p in exposed if p not in dict(BASE_PORT_NAMES)]
    lines = links_lines([(p, names.get(p, "HTTP Service (port %d)" % p)) for p in order], lambda p: prov.http_url(pod, p), token)
    if not lines:
        return ["links    none through the proxy (no http port): podctl tunnel %s prints the localhost links" % pod.get("id")]
    return lines


def cmd_status(prov, args):
    pod = prov.pod(args.pod)
    print(prov.status_text(pod))
    machine = None
    try:
        r = ssh_run(tunnel_facts_cmd(), timeout=30)
        if r.returncode == 0:
            machine = parse_tunnel_facts(r.stdout)
    except Exception:                          # a dead pod still gets its facts; the links fall back to the env token
        machine = None
    try:
        declared = declared_apps()
    except PodctlError as e:
        print("links    (this tree's APP_PORTS are unreadable: %s)" % e); declared = []
    for line in status_links(prov, pod, declared, machine):
        print(line)
    print(gpu_line_over_ssh())


def cmd_pods(prov, args):
    for line in prov.pods_lines():
        print(line)


def cmd_image(prov, args):
    if not hasattr(prov, "image_text"):
        raise PodctlError("%s has no image to inspect (a VM host boots from its own image; see hosts/%s/README.md)" % (prov.name, prov.name))
    print(prov.image_text(prov.pod(args.pod)))


def cmd_ensure(prov, args):
    print(prov.ensure(args.pod, pubkey=mac_public_key(args.pubkey), ssh_config=args.ssh_config, entry=getattr(args, "entry", None),
                      library=getattr(args, "library", None), workspace_shared=getattr(args, "workspace_shared", False)))
    print(write_host_env(prov))
    line = prov.record_volume(prov.pod(args.pod))
    if line:
        print(line)


def cmd_ssh_config(prov, args):
    pod = prov.pod(args.pod)
    addr = prov.address(pod)
    if addr is None:
        raise PodctlError("%s has no reachable address yet (status %s)" % (args.pod, prov.facts(pod).get("status")))
    write_ssh_config(args.ssh_config, addr.host, addr.port)
    print("Host %s -> %s:%s" % (SSH_ALIAS, addr.host, addr.port))


class _FreshAppend(__import__("argparse").Action):
    """append that REPLACES the default list on first use (argparse's append extends the default)."""
    def __call__(self, parser, ns, values, option_string=None):
        items = getattr(ns, self.dest, None)
        if items is None or items is self.default:
            items = []
        items.append(values); setattr(ns, self.dest, items)
        setattr(ns, self.dest + "_given", True)          # 3.9.0: the tunnel adds the machine's app ports only to its defaults


# ---- 3.1.0: the buyer verbs (a host that lacks one says so; Verda has them all)
def _offered(prov, *names):
    missing = [n for n in names if not hasattr(prov, n)]
    if missing:
        raise PodctlError("%s: not offered by host %s" % (", ".join(missing), getattr(prov, "name", "?")))


def cmd_balance(prov, args):
    _offered(prov, "balance")
    b = prov.balance()
    print("%.2f %s" % (b["amount"], b["currency"]))


def cmd_availability(prov, args):
    _offered(prov, "availability")
    locs = prov.availability(args.gpu, spot=args.spot)
    print("%s%s: %s" % (args.gpu, " (spot)" if args.spot else "", ", ".join(locs) if locs else "sold out everywhere"))
    return 0 if locs else 1


def cmd_create(prov, args):
    """A machine from nothing. The stock check comes first and names every location that has the GPU now; a location
    the user named (or the existing volume's) that has none stops here, before anything is created or billed."""
    _offered(prov, "create_machine", "availability")
    loc, volume, spot = args.location, getattr(args, "volume", None), " (spot)" if args.spot else ""
    # 3.8.1 (Paul, 2026-10-16: as simple, then as secure, then as private as possible): a new pod is PRIVATE unless --public
    # says otherwise, on every host whose create offers it (RunPod); a host without a private mode keeps its own create, and
    # only an explicit --private there is refused. --private is still accepted, and means what it always meant.
    import inspect
    offers_private = "private" in inspect.signature(prov.create_machine).parameters
    public = bool(getattr(args, "public", False))
    if public and getattr(args, "private", False):
        raise PodctlError("--public and --private contradict each other: a pod is private unless --public")
    private = not public and (offers_private or bool(getattr(args, "private", False)))
    if private and volume:
        raise PodctlError("--volume is a network volume and RunPod cannot encrypt a network volume, so a pod on it cannot be "
                          "private (the default since base 3.8.1): drop --volume for an encrypted pod volume of --data-gb, or pass "
                          "--public to put this pod on the network volume with proxy URLs")
    if volume:
        _offered(prov, "volume_location")                # 3.7.3: reuse an existing volume (RunPod); its data centre is fixed
        vloc = prov.volume_location(volume)
        if loc and loc != vloc:
            raise PodctlError("volume %s is in %s, not %s: a pod can only mount a volume in its own data centre" % (volume, vloc, loc))
        loc = vloc
    elif not args.data_gb:
        raise PodctlError("--data-gb N (a new volume) or --volume <id> (an existing one)")
    # 3.8.0: --gpu names one type; without it the host's default order (RunPod: B200, then B300) is tried, and the first
    # with stock where the pod must go (the --location, the volume's data centre, else anywhere) is the one created
    defaulted = not args.gpu
    gpus = [args.gpu] if args.gpu else list(getattr(prov, "default_gpus", ()) or ())
    if not gpus:
        raise PodctlError("--gpu is required on host %s (it has no default GPU order)" % getattr(prov, "name", "?"))
    seen = []
    for gpu in gpus:
        locs = prov.availability(gpu, spot=args.spot)
        print("stock   %s%s free now in: %s" % (gpu, spot, ", ".join(locs) if locs else "no location"))
        seen.append((gpu, locs))
        if locs and (not loc or loc in locs):
            args.gpu, loc = gpu, loc or locs[0]
            break
    else:
        if not any(locs for _, locs in seen):
            print("STOP: %s sold out in every location right now%s" % (" and ".join(g for g, _ in seen) + (" is" if len(seen) == 1 else " are"), spot)); return 1
        print("STOP: %s has no %s free right now%s; %s%s" % (
            loc, " or ".join(g for g, _ in seen), spot, "; ".join("%s is free in: %s" % (g, ", ".join(l)) for g, l in seen if l),
            " (the volume pins the data centre: wait, or create a new volume there)" if volume else ""))
        return 1
    if defaulted:
        print("gpu     %s, in %s (no --gpu: the host's order is %s)" % (args.gpu, loc, ", ".join(gpus)))
    pub = pathlib.Path(args.pubkey).read_text(encoding="utf-8").strip() if args.pubkey else None
    extra = {k: v for k, v in (("image", getattr(args, "image", None)), ("volume", volume), ("min_cuda", getattr(args, "min_cuda", None)), ("private", private)) if v}
    takes = inspect.signature(prov.create_machine).parameters
    for k in extra:
        if k not in takes:
            raise PodctlError("--%s: not offered by host %s" % (k.replace("_", "-"), getattr(prov, "name", "?")))
    if "names" in takes:                       # 3.9.0: the ports' names (a host that has no console takes none);
        extra["names"] = port_names(declared_apps())     # a private pod prints them as its tunnel's localhost links
        extra["name_ports"] = not private and not getattr(args, "no_names", False)
    pod_id = prov.create_machine(args.name, args.gpu, loc, args.data_gb, os_gb=args.os_gb, pubkey=pub, spot=args.spot,
                                 wait=not args.no_wait, ssh_config=args.ssh_config, **extra)
    print("next: podctl ensure %s · podctl install %s --base --pkg <dir> (or buyer-install with --runtime and the zips)%s" % (
        pod_id, pod_id, (" · podctl tunnel %s, then ComfyUI is http://localhost:8188" % pod_id) if private else ""))


def cmd_pause(prov, args):
    print(prov.stop(args.pod, wait=True))


def cmd_put_away(prov, args):
    _offered(prov, "machine_volumes", "delete_volumes")
    return put_away(prov, args.pod, PodIO(prov, prov.pod(args.pod)), args.to, yes=args.yes)


def cmd_buyer_install(prov, args):
    pod = prov.pod(args.pod)
    print(write_host_env(prov))
    return buyer_install(args.pod, args.base_zip, args.zips, args.runtime, PodIO(prov, pod), jobs=args.jobs,
                         every=args.every, timeout=args.timeout, logs=args.logs)


def cmd_progress(prov, args):
    rc, out, err = PodIO(prov, prov.pod(args.pod)).remote("cat %s 2>/dev/null" % _sq(VOLUME_ROOT + "/comfy-base/state/progress.json"))
    if rc != 0 or not out.strip():
        print("no progress.json on the machine: nothing is staged, or no staged install has run"); return 1
    print(out.rstrip())


def cmd_runtime_capture(prov, args):
    io = PodIO(prov, prov.pod(args.pod))
    out_remote = VOLUME_ROOT + "/.comfy-base-capture"
    inner = 'rm -rf %s && bash %s runtime-capture %s%s; echo "STEP RC=$?"' % (
        _sq(out_remote), _sq(VOLUME_ROOT + "/comfy-base/base.sh"), _sq(out_remote), (" --url-base %s" % _sq(args.url_base)) if args.url_base else "")
    step_rc, text, console = _run_logged(io, "runtime-capture", "capture", inner, every=args.every, timeout=args.timeout)
    if step_rc is None:
        return 1
    if step_rc != 0:
        print(_tail(text, 30)); print("STOP: runtime-capture ended with STEP RC=%d; console: %s" % (step_rc, console)); return 1
    io.fetch(out_remote, pathlib.Path(args.to))
    print("runtime captured -> %s (runtime.json and its parts; publish them, then hand the manifest's URL to buyer-install)" % args.to)


def build_parser():
    ap = argparse.ArgumentParser(prog="podctl", description=__doc__.split("\n")[0])
    ap.add_argument("--provider", default=None, choices=list(_hosts.NAMES), help="the host (default: $PODCTL_PROVIDER, else the repository's brand.toml, else runpod)")
    ap.add_argument("--ssh-config", default=os.environ.get("PODCTL_SSH_CONFIG") or str(pathlib.Path.home() / ".ssh" / "config"))
    ap.add_argument("--pubkey", default=os.environ.get("PODCTL_PUBKEY"), help="the Mac's public key file (default: the provider's key plus .pub)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pods", help="every pod: id, name, image, status, public ip, port 22")
    p = sub.add_parser("status", help="the pod's facts; env NAMES and placeholder verdicts, never values; the named links "
                                      "(the Jupyter Terminal's carries its token); the GPU verdict over ssh"); p.add_argument("pod")
    p = sub.add_parser("image", help="the image's entrypoint/cmd and how ensure would wrap them"); p.add_argument("pod")
    p = sub.add_parser("ensure", help="TCP 22, PUBLIC_KEY, the base's boot as the start command, Host runpod"); p.add_argument("pod")
    p.add_argument("--entry", help="the image's own start command when its config cannot be read, e.g. '/start_script.sh'")
    p.add_argument("--workspace-shared", action="store_true", dest="workspace_shared",
                   help="Verda: make the attached SHARED volume BE /workspace, so the base, ComfyUI, the venv, the packages and the "
                        "models all live on one store and this machine needs no data volume (the RunPod shape)")
    p.add_argument("--library", metavar="HOST:/EXPORT", help="Verda: the NFS endpoint of the SHARED library to mount at /mnt/comfy-library "
                                                            "(the shared volume must already be attached to the machine; env VERDA_LIBRARY does the same)")
    p = sub.add_parser("ssh-config", help="write the Host runpod block from the pod's public ip and port 22"); p.add_argument("pod")
    for name, hlp in (("stop", "stop the pod (--wait: until it reports stopped)"), ("start", "start the pod (--wait: until sshd answers; Host runpod refreshed)"),
                      ("restart", "restart the pod (--wait: until sshd answers)")):
        p = sub.add_parser(name, help=hlp); p.add_argument("pod"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("deploy", help="a NEW pod on the same network volume, cloned from --like <pod> (image, GPU, disk, ports, env, the base's boot as start command); --wait: until sshd answers, Host runpod → the new pod, then the GPU verdict")
    p.add_argument("--like", required=True, metavar="POD"); p.add_argument("--name"); p.add_argument("--gpu", help="a different GPU type id (default: the source pod's)")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("upload", help="scp files to the pod (through Host runpod) and verify size + sha256 on both sides")
    p.add_argument("pod"); p.add_argument("files", nargs="+"); p.add_argument("--to", default=VOLUME_ROOT + "/packages")
    p = sub.add_parser("install", help="the whole documented sequence, one command: upload, extract, preflight, run (everything to its newest), poll, copy logs, save the pack records")
    p.add_argument("pod"); p.add_argument("--base", action="store_true", help="ComfyUI Base as step one")
    p.add_argument("--pkg", action="append", help="a package directory (repeatable, in order); relative to the project folder")
    p.add_argument("--every", type=int, default=5, help="poll interval in seconds (3.2.0: 5; a 30 s poll cost 15 s of dead time per step on average)"); p.add_argument("--timeout", type=int, default=3 * 3600, help="per item, seconds")
    p.add_argument("--no-latest", dest="no_latest", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--comfy-ref", metavar="REF", help="ComfyUI at a ref NEWER than the newest release (master, a branch, a newer tag, a full sha), for this install only")
    p.add_argument("--no-save-pins", dest="save_pins", action="store_false", help="do not write the printed pins back after a green run")
    p.add_argument("--force-lease", action="store_true", help="install even though another session holds the GPU lease")
    p.add_argument("--no-gate", action="store_true", help="skip the Mac gate (each zip's own suite, run from the extracted zip) before uploading")
    p.add_argument("--no-suite", dest="no_suite", action="store_true", help="no suite ON THE MACHINE: BASE_NO_SUITE=1 for the base step and every package run (the Mac gate still runs)")
    p.add_argument("--env", dest="env", action="append", default=[], metavar="KEY=VALUE", type=env_pair,
                   help="an environment variable for every run on the machine (repeatable); names are printed, values never")
    p = sub.add_parser("prune", help="list the old-layout folders the 2026-09-12 rename left under /workspace/packages (and their workflow copies); --yes removes them")
    p.add_argument("pod"); p.add_argument("--yes", action="store_true", help="remove them (without it, only the list)")
    p = sub.add_parser("pins", help="apply a records artefact a green install measured for OTHER packages (deliberate, by that package's session)")
    p.add_argument("artefact"); p.add_argument("--apply", action="store_true", help="write the rows (without it, they are only printed)")
    p = sub.add_parser("lease", help="who holds the pod's GPU: read it, take it for N minutes, or release it (advisory, expires)")
    p.add_argument("pod"); p.add_argument("--take", action="store_true"); p.add_argument("--release", action="store_true")
    p.add_argument("--holder", default=None, help="defaults to $PODCTL_SESSION, else user@host/pid")
    p.add_argument("--purpose", default="", help="what you are doing, so the next session knows whether to wait")
    p.add_argument("--minutes", type=int, default=60, help="how long to hold it (default 60)")
    p.add_argument("--force", action="store_true", help="take it even though another session holds a live one")
    p = sub.add_parser("jupyter", help="the JupyterLab URL with the token in it, read from the machine over ssh"); p.add_argument("pod")
    p = sub.add_parser("tunnel", help="the ONE ssh session that forwards ports (ComfyUI 8188, Jupyter Terminal 8888, each app port the "
                                      "machine serves) and prints them as named localhost links; runs until killed")
    p.add_argument("pod"); p.add_argument("--port", type=tunnel_port, action=_FreshAppend, default=list(TUNNEL_PORTS),
                   help="repeatable; default 8188, 8888 and the machine's app ports. LOCAL:REMOTE shifts the local side, e.g. --port 8190:8188 "
                        "reaches a second machine's ComfyUI on 8190 while the first keeps 8188")
    p = sub.add_parser("mcp", help="write the .mcp.json that points an MCP client at this machine's ComfyUI (Comfy MCP)")
    p.add_argument("pod")
    p.add_argument("--over", choices=("ssh", "tunnel"), default="ssh",
                   help="ssh (default): comfy-mcp runs ON the machine, so every tool works. tunnel: it runs here and "
                        "reaches ComfyUI through `podctl tunnel` — the run tools only")
    p.add_argument("--bin", default=None, help="the machine's comfy-mcp path (default: asked over ssh, else the venv beside ComfyUI)")
    p.add_argument("--out", default=".mcp.json", help="where to write it (default: .mcp.json here)")
    p.add_argument("--print", dest="print_only", action="store_true", help="print the JSON instead of writing it")
    # ---- 3.1.0: a buyer's machine
    sub.add_parser("balance", help="the account's prepaid balance")
    p = sub.add_parser("availability", help="the locations where a GPU type can be deployed now"); p.add_argument("--gpu", required=True, help="an instance type, e.g. 1RTXPRO6000.30V")
    p.add_argument("--spot", action="store_true")
    p = sub.add_parser("create", help="a machine from nothing, after a stock check that names where the GPU is free: a data volume (or --volume), "
                                      "then the machine on it (Verda: NVMe disk + instance; RunPod: network volume + Secure Cloud pod); ensure configures it")
    p.add_argument("--name", required=True); p.add_argument("--gpu", help="Verda: an instance type (required); RunPod: a GPU type id (default: "
                                                                      "'NVIDIA B200', then 'NVIDIA B300 SXM6 AC', the first with stock)")
    p.add_argument("--location", help="Verda: a location; RunPod: a data centre id (default: the first with the GPU free now)")
    p.add_argument("--data-gb", dest="data_gb", type=int, help="the size of a NEW data volume (RunPod: a network volume in that data centre)")
    p.add_argument("--volume", help="RunPod: an EXISTING network volume id to put the pod on (its data centre is the pod's)")
    p.add_argument("--image", help="the machine image (RunPod: required, the container image)")
    p.add_argument("--min-cuda", dest="min_cuda", help="RunPod: the lowest host CUDA the pod accepts, e.g. 13.0 (default: the image tag's, never below 13.0)")
    p.add_argument("--os-gb", dest="os_gb", type=int, default=100, help="Verda: the OS volume; RunPod: the container disk (default 100)")
    p.add_argument("--private", action="store_true", help="the default since 3.8.1 (RunPod): an ENCRYPTED pod volume of --data-gb "
                   "(network volumes cannot be encrypted), ports 22/tcp only (no proxy URL), ComfyUI, JupyterLab and the apps on loopback: "
                   "podctl tunnel names them")
    p.add_argument("--public", action="store_true", help="RunPod: NOT private: a network volume (new, or --volume), ComfyUI, "
                   "the Jupyter Terminal and each app on RunPod's proxy URLs, named in the Connect tab")
    p.add_argument("--no-names", dest="no_names", action="store_true", help="RunPod: no port-names template (the Connect tab "
                   "then says \"HTTP Service\"); the ports are the same")
    p.add_argument("--spot", action="store_true"); p.add_argument("--no-wait", dest="no_wait", action="store_true")
    p = sub.add_parser("pause", help="delete the instance and keep its disks (the same as stop --wait): only storage bills"); p.add_argument("pod")
    p = sub.add_parser("put-away", help="copy output/ and input/ to this Mac; with --yes, then delete the machine and its disks (to the trash)")
    p.add_argument("pod"); p.add_argument("--to", required=True, help="the local folder the images go to"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("buyer-install", help="a buyer's install: the proven runtime, then the base and each package zip, the first flow first; no gate, no pins")
    p.add_argument("pod"); p.add_argument("zips", nargs="*", help="the purchased package zips, in order")
    p.add_argument("--runtime", required=True, help="the runtime manifest: an https URL, or a local runtime.json with its parts beside it")
    p.add_argument("--base-zip", dest="base_zip", required=True, help="comfyui-base.zip")
    p.add_argument("--jobs", type=int, default=1, help="model files downloaded at once (BASE_FETCH_JOBS)")
    p.add_argument("--logs", default=None, help="where the machine's logs are copied (default: a podruns/ folder beside the zips)")
    p.add_argument("--every", type=int, default=5); p.add_argument("--timeout", type=int, default=3 * 3600)
    p = sub.add_parser("progress", help="the machine's state/progress.json: where a staged install's later files are"); p.add_argument("pod")
    p = sub.add_parser("runtime-capture", help="capture this green machine's runtime (parts + runtime.json) and copy it here")
    p.add_argument("pod"); p.add_argument("--to", required=True); p.add_argument("--url-base", dest="url_base", default="", help="the https prefix the parts will be published under")
    p.add_argument("--every", type=int, default=5); p.add_argument("--timeout", type=int, default=3 * 3600)
    for sp in sub.choices.values():                                  # --provider after the verb too (a hook that reads argv sees both forms)
        sp.add_argument("--provider", dest="provider_after", default=None, choices=list(_hosts.NAMES), help=argparse.SUPPRESS)
    return ap


def main(argv=None):
    import shlex
    args = build_parser().parse_args(argv)
    if getattr(args, "entry", None):
        args.entry = shlex.split(args.entry)
    try:
        prov = set_provider(provider_for(args.provider or getattr(args, "provider_after", None)))
        if prov.experimental:
            print("note    host %s is EXPERIMENTAL: written from first-party docs, not yet proven on a live account (hosts/%s/README.md)" % (prov.name, prov.name), file=sys.stderr)
        rc = {"status": cmd_status, "pods": cmd_pods, "image": cmd_image, "ensure": cmd_ensure, "ssh-config": cmd_ssh_config,
         "stop": cmd_stop, "start": cmd_start, "restart": cmd_restart, "deploy": cmd_deploy, "upload": cmd_upload, "tunnel": cmd_tunnel, "jupyter": cmd_jupyter, "install": cmd_install,
         "lease": cmd_lease, "pins": cmd_pins, "prune": cmd_prune, "mcp": cmd_mcp,
         "balance": cmd_balance, "availability": cmd_availability, "create": cmd_create, "pause": cmd_pause, "put-away": cmd_put_away,
         "buyer-install": cmd_buyer_install, "progress": cmd_progress, "runtime-capture": cmd_runtime_capture}[args.cmd](prov, args)
        if isinstance(rc, int):
            return rc
    except PodctlError as e:
        print("podctl: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
