"""hosts/verda/provider.py: Verda (DataCrunch), the second host. A VM behind a REST API, an OS volume that IS the
image next time, one data volume the first-boot script mounts at /workspace, and the base's boot as a systemd unit.

PROVEN on a live account (2026-09-17 and 2026-09-18): instances created, stopped and redeployed from an OS volume,
a shared filesystem created, attached to two machines at once and mounted as the whole workspace, a 150 GB
migration, and six volumes restored from the trash after a zero balance. hosts/verda/README.md records what the
live runs answered and what they measured.

The credentials are a client id + client secret pair: VERDA_CLIENT_ID + VERDA_CLIENT_SECRET, else (3.19.0, on a Mac) the
login Keychain items whose accounts are those two names, else ~/.verda/credentials (`client_id = ...` / `client_secret = ...`,
quoted or not). A pair counts only when both halves come from the same one of the three. They are exchanged for a bearer token at POST /oauth2/token
and are never printed, logged or placed in a URL or an argument. Everything is lazy: constructing the provider reads
nothing and opens no socket; the first REST call loads the pair and takes the token.

The machine model, and why `stop` DELETES:
  * a shut-down instance still bills compute (the GPU stays reserved), so the cheap stop is `delete` with
    `volume_ids: []`, which removes the instance and KEEPS every volume: the OS volume (bootable, redeployed with
    `image=<its id>`) and the data volume(s);
  * `pods()` therefore lists detached volumes beside instances, so a stopped machine stays visible, and `start` takes
    either an instance id (offline -> `start`) or an OS volume id (-> a fresh `POST /instances` from that volume with
    every detached data volume that shares its name prefix attached);
  * the public IP changes on every start, and there is no port mapping and no cloud firewall: sshd is root@<ip>:22
    and every bound port is public, so the base binds loopback on this host and is reached through the driver's tunnel.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import sys
import time
import urllib.error
import urllib.request

from comfyui_hosts import Address, PodctlError, Provider, keychain

HERE = pathlib.Path(__file__).resolve().parent
VOLUME_ROOT = "/workspace"                         # where startup.sh mounts the data volume; the base's BASE_VOLUME here
DEFAULT_API_URL = "https://api.verda.com/v1"
KEY_PATH = pathlib.Path.home() / ".ssh" / "verda_comfyui"
CREDENTIALS_PATH = pathlib.Path.home() / ".verda" / "credentials"
LIBRARY_MOUNT = "/mnt/comfy-library"               # where startup.sh mounts the SHARED library; the base's BASE_LIBRARY
LIBRARY_SRC_ENV = "VERDA_LIBRARY"                  # the library's NFS endpoint, e.g. nfs.fin-02.verda.com:/pseudo
STARTUP_SCRIPT_NAME = "comfy-base-startup"         # the name the script is registered under (GET /scripts)
DEFAULT_IMAGE = "24.04.cuda13.2.docker"   # the image TYPE, which POST /instances wants (measured 2026-09-24); the console shows a display name
STARTUP_SCRIPT_FILE = HERE / "startup.sh"
REMOTE_STARTUP = "/root/comfy-base-startup.sh"     # where `ensure` puts the script on the running machine
SSH_PORT = 22
KEY_NAME = "comfyui-mac"                           # the name the Mac key is registered under (POST /sshkeys)

# Verda's `status` vocabulary (api.verda.com/v1/docs) and the driver's four words.
STATUS = {"running": "running", "offline": "stopped", "notfound": "gone", "deleting": "gone", "discontinued": "gone"}
# "discontinued" reads as GONE, not transitional. Verda answers it for an instance that has been DELETED (the
# record survives the delete, with volume_ids []) and also for one killed by a zero balance; either way the
# machine is not coming back and `start` must refuse it rather than silently pass it through as "on its way".
# Deletion itself is confirmed from the instance LIST, not from this word, because the two cases share it.
TRANSITIONAL = ("provisioning", "unknown", "ordered", "new", "error", "validating", "no_capacity", "installation_failed")
STATUS_DELETED = "discontinued"


def _c():
    """The driver module (podctl.py): write_ssh_config, ssh_banner, ssh_run, _ssh_hostname, _sq, SSH_ALIAS."""
    return sys.modules["comfyui_podctl"]


def _base_version():
    try:
        return (HERE.parents[1] / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


BASE_VERSION_FOR_UA = _base_version()


# ---------------------------------------------------------------- credentials

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def load_credentials(env=None, path=None):
    """(client_id, client_secret): VERDA_CLIENT_ID + VERDA_CLIENT_SECRET, else (3.19.0, a Mac only) the login Keychain
    items whose accounts are those two names, else ~/.verda/credentials. Never echoed.
    3.19.0: a pair counts only when both halves come from the SAME source. An id from the environment and a secret from
    the file used to be joined, and a half-set environment then signed in with a pair nobody had put together."""
    env = os.environ if env is None else env
    cid = (env.get("VERDA_CLIENT_ID") or "").strip()
    secret = (env.get("VERDA_CLIENT_SECRET") or "").strip()
    if cid and secret:
        return cid, secret
    cid = (keychain("VERDA_CLIENT_ID") or "").strip()
    secret = (keychain("VERDA_CLIENT_SECRET") or "").strip() if cid else ""
    if cid and secret:
        return cid, secret
    path = pathlib.Path(path) if path else CREDENTIALS_PATH
    if path.exists():
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#;[":
                continue
            m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1).lower()
            if key in ("client_id", "verda_client_id"):
                found["id"] = _unquote(m.group(2))
            elif key in ("client_secret", "verda_client_secret"):
                found["secret"] = _unquote(m.group(2))
        if found.get("id") and found.get("secret"):
            return found["id"], found["secret"]
    raise PodctlError("no Verda credentials: export VERDA_CLIENT_ID and VERDA_CLIENT_SECRET, or (on a Mac) keep both in the "
                      "login Keychain as items whose accounts are those two names, or write both as `client_id = ...` and "
                      "`client_secret = ...` lines in %s (hosts/verda/README.md)" % path)


# ---------------------------------------------------------------- the REST client

class HttpError(PodctlError):
    """A non-2xx answer: the status and the body, never the token or the secret."""
    def __init__(self, method, path, status, text):
        self.status = status
        super().__init__("%s %s -> HTTP %s: %s" % (method, path, status, (text or "")[:300]))


class Api:
    """The few REST calls podctl needs: get/post/put, JSON in, JSON out, one bearer token taken lazily from the
    client-credentials grant. A test passes `base_url` and `token` and no oauth call ever happens."""

    USER_AGENT = "podctl/%s (ComfyUI Base; Verda REST client)" % BASE_VERSION_FOR_UA

    def __init__(self, base_url=None, token=None, credentials=None, env=None, credentials_path=None):
        env = os.environ if env is None else env
        self.base = (base_url or env.get("VERDA_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._token = token
        self._until = float("inf") if token else 0.0
        self._credentials = credentials
        self._env = env
        self._credentials_path = credentials_path

    # -- the token
    def credentials(self):
        if self._credentials is None:
            self._credentials = load_credentials(env=self._env, path=self._credentials_path)
        return self._credentials

    def token(self):
        if self._token and time.time() < self._until:
            return self._token
        cid, secret = self.credentials()
        body = {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
        r = self._call("POST", "/oauth2/token", body, auth=False)
        tok = (r or {}).get("access_token") if isinstance(r, dict) else None
        if not tok:
            raise PodctlError("POST /oauth2/token answered without an access_token (check the client id and secret)")
        try:
            ttl = float((r or {}).get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        self._token, self._until = tok, time.time() + max(ttl - 60, 30)
        return tok

    # -- the calls
    def _call(self, method, path, body=None, auth=True, _retry=True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if auth:
            req.add_header("Authorization", "Bearer " + self.token())
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self.USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode(errors="replace")
            finally:
                e.close()                                    # an unclosed HTTPError is a ResourceWarning on newer Pythons
            if e.code == 401 and auth and _retry:              # an expired token: take a fresh one, once
                self._token, self._until = None, 0.0
                return self._call(method, path, body, auth=auth, _retry=False)
            raise HttpError(method, path, e.code, text)
        except urllib.error.URLError as e:
            raise PodctlError("%s %s -> %s" % (method, path, e.reason))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace").strip()

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body=None):
        return self._call("POST", path, body)

    def put(self, path, body=None):
        return self._call("PUT", path, body)

    def delete(self, path, body=None):
        return self._call("DELETE", path, body)


# ---------------------------------------------------------------- record helpers

def norm_status(raw):
    return STATUS.get((raw or "").strip().lower(), "transitional")


def inst_ip(inst):
    return inst.get("ip") or inst.get("public_ip") or ""


def is_volume(rec):
    return (rec or {}).get("kind") == "volume"


def is_shared_volume(v):
    """A Verda SHARED FILESYSTEM is not a separate object: it is a volume whose type is one of the shared ones
    (GET /volume-types marks them is_shared_fs: NVMe_Shared, HDD_Shared, NVMe_Shared_Cluster). The difference
    that matters here is that it attaches to SEVERAL instances at once (POST /volumes takes instance_ids, and
    PUT /volumes action attach takes instance_ids), while a plain block volume attaches to exactly one. That is
    what lets one model library serve every machine instead of a copy per machine."""
    if (v or {}).get("is_shared_fs") is True:
        return True
    return "shared" in ((v or {}).get("type") or "").lower()


def volume_instance_ids(v):
    """Every instance id a volume record names. A plain volume answers one (instance_id); a shared one answers
    several, and the API spells them in more than one place depending on the call."""
    out = []
    one = (v or {}).get("instance_id")
    if one:
        out.append(str(one))
    for i in ((v or {}).get("instance_ids") or []):
        out.append(str(i))
    for inst in ((v or {}).get("instances") or []):
        if isinstance(inst, dict) and inst.get("id"):
            out.append(str(inst["id"]))
        elif isinstance(inst, str):
            out.append(inst)
    return out


def library_src_from(v):
    """The NFS endpoint a shared volume is mounted from. MEASURED on a live account (2026-09-17): it arrives as
    `target`, e.g. `nfs.fin-03.datacrunch.io:/comfy-library-<id>`, beside `pseudo_path` and ready-made
    `mount_command` / `filesystem_to_fstab_command` strings. `target` is overloaded, on a plain BLOCK volume it
    is the device name, "vda", so the host:/export shape is what decides, never the key alone. That makes this
    safe to read for any volume, and it is why --library is an override rather than the only route."""
    for k in ("target", "nfs_endpoint", "nfs_path", "export_path", "share_path", "endpoint"):
        val = (v or {}).get(k)
        if isinstance(val, str) and re.match(r"^[^\s:]+:/\S*$", val):
            return val
    return ""


def new_id(resp):
    """POST /instances answers the new id as a JSON string or as {"id": ...}; accept both."""
    if isinstance(resp, str):
        return resp.strip().strip('"')
    if isinstance(resp, dict):
        v = resp.get("id") or resp.get("instance_id")
        if v:
            return str(v)
        for k in ("instances", "ids"):
            if isinstance(resp.get(k), list) and resp[k]:
                first = resp[k][0]
                return str(first.get("id") if isinstance(first, dict) else first)
    if isinstance(resp, list) and resp:
        first = resp[0]
        return str(first.get("id") if isinstance(first, dict) else first)
    raise PodctlError("POST /instances answered without an id: %r" % (resp,))


def volume_stem(name):
    """The part of a volume name that a data volume shares with its OS volume: `comfy-base-os` and `comfy-base-data`
    both stem to `comfy-base`, as do `OS-comfy-base` and `comfy-base`."""
    s = (name or "").strip().lower()
    s = re.sub(r"^(os|data)[-_ .]+", "", s)
    s = re.sub(r"[-_ .]+(os|root|boot|system|data|workspace|models|vol|volume|disk)$", "", s)
    return s


def stems_match(a, b):
    a, b = volume_stem(a), volume_stem(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def hostname_for(name):
    h = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")[:63]
    return h or "comfy-base"


def key_matches(registered, pub):
    """Two OpenSSH public keys are the same key when their type and base64 body agree (the comment may differ)."""
    def core(k):
        parts = (k or "").split()
        return tuple(parts[:2]) if len(parts) >= 2 else (k or "").strip()
    return core(registered) == core(pub)


def startup_script_text():
    try:
        return STARTUP_SCRIPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise PodctlError("cannot read %s: %s" % (STARTUP_SCRIPT_FILE, e))


REMOTE_EOF = "COMFY_BASE_STARTUP_EOF"


def remote_ensure_cmd(text, library="", workspace=""):
    """One ssh command: land startup.sh on the machine through a quoted heredoc and run its --ensure pass.
    With a library, the endpoint is passed so the script writes the fstab line and mounts it; without one the
    script recalls whatever endpoint it recorded last time, so a plain `ensure` never drops a machine's library."""
    if REMOTE_EOF in text:
        raise PodctlError("startup.sh contains the heredoc delimiter %s" % REMOTE_EOF)
    tail = " --ensure" + ((" --library " + shlex.quote(library)) if library else "")
    tail += (" --workspace " + shlex.quote(workspace)) if workspace else ""
    return "cat > %s <<'%s'\n%s\n%s\nchmod 700 %s && bash %s%s" % (REMOTE_STARTUP, REMOTE_EOF, text.rstrip("\n"), REMOTE_EOF,
                                                                  REMOTE_STARTUP, REMOTE_STARTUP, tail)


# ---------------------------------------------------------------- the provider

class VerdaProvider(Provider):
    """Verda behind the host interface. `sleep` and the banner are instance attributes so the suite drives every wait
    against a fake REST server without waiting; the ssh helpers are the driver's, reached at call time."""
    name = "verda"
    volume_root = VOLUME_ROOT
    ssh_user = "root"
    ssh_alias = "verda"
    key_path = KEY_PATH
    experimental = False       # proven live 2026-09-18; see hosts/verda/README.md

    def __init__(self, api=None, env=None):
        self.api = api or Api(env=env)
        self.env = os.environ if env is None else env
        self.sleep = time.sleep
        self.library_mount = LIBRARY_MOUNT
        self._library_src = ""      # set by ensure(): the endpoint the machine was told to mount
        self._library_on = False    # ensure() resolved a library for the machine it addressed
        self._workspace_shared = False   # 2.5.0: /workspace IS the shared store on the machine ensure() addressed
        self._last_pod = ""         # the machine this run addressed, for host_env()

    # -- small plumbing
    def banner(self, host, port=SSH_PORT):
        return _c().ssh_banner(host, port)

    def _wait(self, probe, what, tries=120, every=5):
        for _ in range(tries):
            v = probe()
            if v:
                return v
            self.sleep(every)
        raise PodctlError("gave up waiting for %s (%ds)" % (what, tries * every))

    def _instance(self, ident):
        """GET /instances/{id}, or None when Verda says it is gone (404 or status notfound)."""
        try:
            inst = self.api.get("/instances/" + ident)
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(inst, dict) or (inst.get("status") or "").lower() == "notfound":
            return None
        return inst

    def _volume(self, ident):
        try:
            return self.api.get("/volumes/" + ident)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def _volumes(self):
        vols = self.api.get("/volumes") or []
        return [v for v in vols if isinstance(v, dict)]

    def _detached(self, vols=None):
        return [v for v in (vols if vols is not None else self._volumes()) if (v.get("status") or "").lower() == "detached"]

    def _shared_volumes(self, vols=None):
        return [v for v in (vols if vols is not None else self._volumes()) if is_shared_volume(v)]

    def _library_for(self, pod_id, vols=None):
        """The shared volume this instance is attached to, or None. This is the whole of the driver's knowledge of
        the library: nothing is remembered on the Mac, so a machine's library is always read back from the account."""
        for v in self._shared_volumes(vols):
            if str(pod_id) in volume_instance_ids(v):
                return v
        return None

    def _volume_record(self, v):
        rec = dict(v)
        rec.update(kind="volume", name=v.get("name") or "", status="%s-volume" % ((v.get("status") or "unknown").lower()))
        return rec

    def _wait_running(self, ident, what="running with a public ip"):
        def probe():
            inst = self._instance(ident)
            if inst is None:
                raise PodctlError("%s is gone (Verda answers notfound) while waiting for it to be %s" % (ident, what))
            st = (inst.get("status") or "").lower()
            if st in ("error", "installation_failed", "no_capacity", "discontinued"):
                raise PodctlError("%s is %s: read the console (a discontinued instance usually means a zero balance)" % (ident, st))
            return inst if st == "running" and inst_ip(inst) else None
        return self._wait(probe, "%s to be %s" % (ident, what), tries=120, every=5)

    def _wait_banner(self, host):
        for _ in range(120):
            if self.banner(host, SSH_PORT):
                return
            self.sleep(5)
        raise PodctlError("sshd never answered on %s:%d: read the instance's console (the startup log is /var/log/comfy-base-startup.log)" % (host, SSH_PORT))

    def _up_and_configured(self, ident, ssh_config):
        inst = self._wait_running(ident)
        host = inst_ip(inst)
        self._wait_banner(host)
        _c().write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, SSH_PORT)
        return inst, host

    # -- keys and scripts
    def _local_pubkey(self):
        p = pathlib.Path(str(self.key_path) + ".pub")
        if not p.exists():
            raise PodctlError("no public key at %s: ssh-keygen -t ed25519 -f %s (hosts/verda/README.md)" % (p, self.key_path))
        return p.read_text(encoding="utf-8").strip()

    def key_ids(self, pubkey=None, register=True, log=None):
        """The ids of every registered key equal to the Mac key; registers it (once) when none is."""
        pub = (pubkey or self._local_pubkey()).strip()
        keys = [k for k in (self.api.get("/sshkeys") or []) if isinstance(k, dict)]
        ids = [str(k["id"]) for k in keys if key_matches(k.get("key"), pub) and k.get("id")]
        if ids or not register:
            return ids
        if log:
            log("ensure: registering the Mac key as %s (POST /sshkeys)" % KEY_NAME)
        r = self.api.post("/sshkeys", {"name": KEY_NAME, "key": pub})
        try:
            return [new_id(r)]
        except PodctlError:
            return [str(k["id"]) for k in (self.api.get("/sshkeys") or []) if key_matches(k.get("key"), pub) and k.get("id")]

    def script_id(self, register=True, log=None):
        """The id of the registered comfy-base-startup script whose TEXT is this base's hosts/verda/startup.sh.

        3.0.0: a registered script is a copy of a tool, so it is kept at its newest like every other tool. It is matched
        by text, never by name alone: an older copy under the same name (the pre-2.4.0 script, still registered on the
        live account on 2026-09-25) would otherwise be attached to every redeploy and rewrite state/host.env without
        BASE_VOLUME_SHARED=1, for every machine on a shared store.
          register=True  (ensure, a command a person approves): POST the current text, then DELETE every stale copy
                         (Verda's API has no PUT for scripts: DELETE /v1/scripts/{scriptId}). A DELETE that fails
                         leaves the stale copy registered but never matched, so it is never used.
          register=False (start): a stale copy is OMITTED (None), and `ensure` configures the machine over ssh."""
        text = startup_script_text()
        scripts = [s for s in (self.api.get("/scripts") or []) if isinstance(s, dict)]
        mine = [s for s in scripts if s.get("name") == STARTUP_SCRIPT_NAME and s.get("id")]
        current = next((str(s["id"]) for s in mine if (s.get("script") or "").strip() == text.strip()), None)
        stale = [str(s["id"]) for s in mine if (s.get("script") or "").strip() != text.strip()]
        if not register:
            if not current and stale and log:
                log("the registered %s script differs from this base's hosts/verda/startup.sh: it is NOT attached "
                    "(podctl ensure replaces it and configures the machine over ssh)" % STARTUP_SCRIPT_NAME)
            return current
        if not current:
            if log:
                log("ensure: registering hosts/verda/startup.sh as %s (POST /scripts)" % STARTUP_SCRIPT_NAME)
            r = self.api.post("/scripts", {"name": STARTUP_SCRIPT_NAME, "script": text})
            try:
                current = new_id(r)
            except PodctlError:
                current = next((str(s["id"]) for s in (self.api.get("/scripts") or [])
                                if isinstance(s, dict) and s.get("name") == STARTUP_SCRIPT_NAME
                                and (s.get("script") or "").strip() == text.strip()), None)
            if log and current:
                log("ensure: %s registered as %s" % (STARTUP_SCRIPT_NAME, current))
        if current:
            for sid in stale:
                try:
                    self.api.delete("/scripts/%s" % sid)
                    if log:
                        log("ensure: the stale %s script %s deleted (DELETE /scripts/%s)" % (STARTUP_SCRIPT_NAME, sid, sid))
                except PodctlError as e:
                    if log:
                        log("ensure: could not delete the stale %s script %s (%s): it stays registered and unused" % (STARTUP_SCRIPT_NAME, sid, e))
        return current

    # -- the interface: listing and facts
    def pods(self):
        out = [i for i in (self.api.get("/instances") or []) if isinstance(i, dict)]
        out += [self._volume_record(v) for v in self._detached()]
        # the shared library is listed whatever its status. MEASURED: a fresh one is `created`, an ATTACHED one is
        # `exported` (not "attached"), and only a detached one says `detached`, so _detached() never shows it, and
        # a store three machines depend on would be invisible in `podctl pods` exactly when it is in use.
        seen = {str(p.get("id")) for p in out}
        out += [self._volume_record(v) for v in self._shared_volumes() if str(v.get("id")) not in seen]
        return out

    def pod(self, ident):
        # remember which machine this run is addressing, so host_env() can answer for it. MEASURED: without this,
        # `podctl install` rewrote state/host.env with no BASE_LIBRARY line and silently un-configured the library
        # on a machine that had it mounted, because write_host_env() runs on a provider that never called ensure().
        self._last_pod = str(ident)
        inst = self._instance(ident)
        if inst is not None:
            return inst
        vol = self._volume(ident)
        if vol is not None:
            return self._volume_record(vol)
        raise PodctlError("%s: no such Verda instance or volume (podctl pods lists both)" % ident)

    def facts(self, pod):
        if is_volume(pod):
            return {"id": pod.get("id"), "name": pod.get("name") or "", "status": "stopped" if "detached" in (pod.get("status") or "") else "transitional",
                    "host": "", "port": 0, "user": self.ssh_user, "image": pod.get("id") if pod.get("is_os_volume") else "",
                    "gpu": "", "volumes": [pod.get("id")], "kind": "volume", "location": pod.get("location") or "",
                    "instance_type": pod.get("instance_type") or ""}
        ip = inst_ip(pod)
        vols = ([pod["os_volume_id"]] if pod.get("os_volume_id") else []) + [str(v) for v in (pod.get("volume_ids") or [])]
        return {"id": pod.get("id"), "name": pod.get("hostname") or "", "status": norm_status(pod.get("status")), "host": ip,
                "port": SSH_PORT if ip else 0, "user": self.ssh_user, "image": pod.get("image") or "", "gpu": pod.get("gpu") or "",
                "volumes": vols, "kind": "instance", "location": pod.get("location") or "", "instance_type": pod.get("instance_type") or ""}

    def address(self, pod):
        f = self.facts(pod)
        return Address(f["host"], f["port"], self.ssh_user) if f["status"] == "running" and f["host"] else None

    def status_text(self, pod):
        if is_volume(pod):
            kind = "shared library (attaches to several machines at once)" if is_shared_volume(pod) else ("OS volume" if pod.get("is_os_volume") else "data volume")
            lines = ["volume     %s (%s)" % (pod.get("id"), pod.get("name") or ""),
                     "kind       %s, %s" % (kind, pod.get("status")),
                     "size       %s GB %s" % (pod.get("size", "?"), pod.get("type") or ""),
                     "location   %s" % (pod.get("location") or "?")]
            if pod.get("is_os_volume"):
                lines.append("next       podctl start %s (redeploys from this OS volume; the data volumes sharing its name come along)" % pod.get("id"))
            elif is_shared_volume(pod):
                lines.append("next       attached to every machine started in %s, by type and location, not by name" % (pod.get("location") or "?"))
            else:
                lines.append("next       attached automatically when its OS volume is started (name prefix), or by the console")
            return "\n".join(lines)
        ip = inst_ip(pod)
        price = pod.get("price_per_hour")
        lines = ["instance   %s (%s)" % (pod.get("id"), pod.get("hostname") or ""),
                 "status     %s (%s)" % (pod.get("status"), norm_status(pod.get("status"))),
                 "type       %s%s%s" % (pod.get("instance_type") or "?", (" / %s" % pod.get("gpu")) if pod.get("gpu") else "",
                                         (" %s GB" % pod.get("gpu_memory")) if pod.get("gpu_memory") else ""),
                 "price      %s/h%s" % (price if price is not None else "?", " (spot)" if pod.get("is_spot") else ""),
                 "location   %s" % (pod.get("location") or "?"),
                 "image      %s" % (pod.get("image") or "?"),
                 "public ip  %s" % (ip or "(none yet)"),
                 "ssh        %s" % (("root@%s:%d" % (ip, SSH_PORT)) if ip else "(no public ip yet)"),
                 "os volume  %s" % (pod.get("os_volume_id") or "?"),
                 "volumes    %s" % (", ".join(str(v) for v in (pod.get("volume_ids") or [])) or "NONE (startup.sh has no data disk to mount)"),
                 "script     %s" % (pod.get("startup_script_id") or "(none: the first boot ran no startup script; podctl ensure installs it over ssh)"),
                 "created    %s" % (pod.get("created_at") or "?"),
                 "firewall   none on this host: every bound port is public; the base binds loopback and is reached through podctl tunnel"]
        return "\n".join(lines)

    def pods_lines(self):
        out = []
        for p in self.pods():
            if is_volume(p):
                out.append("%-36s %-18s %-22s %-12s %-15s %s" % (p.get("id"), (p.get("name") or "")[:18],
                                                                  "%s volume, %s GB" % ("shared" if is_shared_volume(p) else ("OS" if p.get("is_os_volume") else "data"), p.get("size", "?")),
                                                                  "detached", "-", p.get("location") or ""))
            else:
                out.append("%-36s %-18s %-22s %-12s %-15s %s" % (p.get("id"), (p.get("hostname") or "")[:18], (p.get("instance_type") or "")[:22],
                                                                  p.get("status"), inst_ip(p) or "-", p.get("location") or ""))
        return out

    # -- the interface: lifecycle
    def stop(self, pod_id, wait=True):
        """Delete the instance and KEEP every volume (`volume_ids: []`): a shut-down Verda instance still bills the GPU.
        The OS volume is what `start` redeploys from; the data volumes come along by name."""
        inst = self._instance(pod_id)
        if inst is None:
            vol = self._volume(pod_id)
            if vol is not None:
                return "%s is a %s volume, already detached: nothing to stop" % (pod_id, "OS" if vol.get("is_os_volume") else "data")
            raise PodctlError("%s: no such Verda instance" % pod_id)
        if (inst.get("status") or "").lower() == STATUS_DELETED:
            # the record outlives the delete; deleting it again would be a second charge-free no-op, but saying so is honest
            return ("%s is already %s: the instance is gone and its volumes were kept. `podctl pods` lists them; "
                    "`podctl start <os volume>` brings a machine back" % (pod_id, STATUS_DELETED))
        os_vol = inst.get("os_volume_id") or "?"
        data = [str(v) for v in (inst.get("volume_ids") or [])]
        self.api.put("/instances", {"id": pod_id, "action": "delete", "volume_ids": []})
        if wait:
            def gone():
                # MEASURED on a live account (2026-09-17): a DELETED Verda instance is not 404 and does not say
                # notfound. GET /instances/{id} keeps answering the full record indefinitely, with
                # status "discontinued" and volume_ids [], so a probe that waits for None, "deleting" or
                # "notfound" can never come true and `stop --wait` gave up after 300 s on a delete that had in
                # fact completed a minute earlier. Status cannot decide this anyway: Verda uses the same word
                # "discontinued" for an instance killed by a zero balance. Absence from the LIST is the
                # authoritative signal, so that is what is polled; the per-id checks stay as a fast path.
                cur = self._instance(pod_id)
                if cur is None or (cur.get("status") or "").lower() in ("deleting", "notfound"):
                    return True
                try:
                    listed = self.api.get("/instances") or []
                except HttpError:
                    return False
                return not any(str((i or {}).get("id")) == str(pod_id) for i in listed if isinstance(i, dict))
            self._wait(gone, "%s to be deleted" % pod_id, tries=60, every=5)
        return ("stopped %s: instance deleted, volumes kept: OS volume %s%s. `podctl start %s` redeploys from the OS volume; "
                "the ip will be new" % (pod_id, os_vol, (", data volumes " + ", ".join(data)) if data else " (no data volumes)", os_vol))

    def start(self, pod_id, wait=True, ssh_config=None):
        """`pod_id` is an instance (offline -> action start) or a detached OS volume (-> a new instance booted from it,
        with the detached data volumes that share its name attached). With wait: running + ip, an ssh banner, the
        Host block rewritten (the public ip changes on every start)."""
        inst = self._instance(pod_id)
        attached = []
        if inst is not None:
            st = (inst.get("status") or "").lower()
            if st == "offline":
                self.api.put("/instances", {"id": pod_id, "action": "start"})
            elif st == "running":
                pass
            elif norm_status(st) == "transitional":
                pass
            else:
                raise PodctlError("%s is %s: nothing to start" % (pod_id, st))
            new_id_ = pod_id
        else:
            vol = self._volume(pod_id)
            if vol is None:
                raise PodctlError("%s: no such Verda instance or volume (podctl pods lists the detached OS volumes a stopped machine leaves)" % pod_id)
            if not vol.get("is_os_volume"):
                raise PodctlError("%s is a data volume (%s), not an OS volume: start the OS volume and this one comes along by name" % (pod_id, vol.get("name")))
            if (vol.get("status") or "").lower() != "detached":
                raise PodctlError("%s is %s (instance %s): start that instance instead" % (pod_id, vol.get("status"), vol.get("instance_id")))
            location = vol.get("location") or ""
            itype = (self.env.get("VERDA_INSTANCE_TYPE") or "").strip() or vol.get("instance_type") or ""
            if not itype:
                raise PodctlError("which instance type? export VERDA_INSTANCE_TYPE=<type> (GET /instance-types names them; hosts/verda/README.md)")
            for dv in self._detached():
                if dv.get("id") != vol.get("id") and not dv.get("is_os_volume") and (dv.get("location") or "") == location \
                        and stems_match(dv.get("name"), vol.get("name")) and not is_shared_volume(dv):
                    attached.append(str(dv["id"]))
            # the SHARED library comes along by TYPE and location, never by name: one library serves machines whose
            # names differ (comfy-base, comfy-cc, comfy-mm), so the stem rule that finds a machine's own data volume
            # cannot find it, and must not: a stem match would tie the library to one machine's name.
            for sv in self._shared_volumes():
                if (sv.get("location") or "") == location and str(sv.get("id")) not in attached:
                    attached.append(str(sv["id"]))
            body = {"hostname": hostname_for(volume_stem(vol.get("name")) or vol.get("name")), "image": str(vol["id"]), "instance_type": itype,
                    "location_code": location, "ssh_key_ids": self.key_ids(register=True), "existing_volumes": attached,
                    "description": "ComfyUI Base, redeployed from OS volume %s by podctl" % vol["id"]}
            sid = self.script_id(register=False, log=lambda m: print(m))
            if sid:
                body["startup_script_id"] = sid
            new_id_ = new_id(self.api.post("/instances", body))
        if not wait:
            if attached:
                return "started %s from OS volume %s with data volumes %s attached (no --wait: podctl ssh-config %s once it runs)" % (
                    new_id_, pod_id, ", ".join(attached), new_id_)
            return "started %s" % new_id_
        inst, host = self._up_and_configured(new_id_, ssh_config)
        extra = (" from OS volume %s, data volumes %s" % (pod_id, ", ".join(attached) or "none")) if new_id_ != pod_id else ""
        return "started %s%s: ssh answers on %s:%d (Host %s updated)" % (new_id_, extra, host, SSH_PORT, _c().SSH_ALIAS)

    def restart(self, pod_id, wait=True, ssh_config=None):
        """`shutdown`, wait offline, `start`, wait running + ip + banner, rewrite the Host block. Never `ssh reboot`:
        Verda's confidential-computing instances forbid an in-guest reboot, and the API path is the same for every
        type. The ip may change, so the block is always rewritten."""
        inst = self._instance(pod_id)
        if inst is None:
            raise PodctlError("%s: no such Verda instance (a detached OS volume is started, not restarted)" % pod_id)
        if (inst.get("status") or "").lower() != "offline":
            self.api.put("/instances", {"id": pod_id, "action": "shutdown"})

            def off():
                cur = self._instance(pod_id)
                if cur is None:
                    raise PodctlError("%s vanished during the shutdown" % pod_id)
                return (cur.get("status") or "").lower() == "offline"
            self._wait(off, "%s to be offline" % pod_id, tries=60, every=5)
        self.api.put("/instances", {"id": pod_id, "action": "start"})
        if not wait:
            return "restarted %s (no --wait: podctl ssh-config %s once it runs; the ip may be new)" % (pod_id, pod_id)
        inst, host = self._up_and_configured(pod_id, ssh_config)
        return "restarted %s: ssh answers on %s:%d (Host %s updated)" % (pod_id, host, SSH_PORT, _c().SSH_ALIAS)

    def ensure(self, pod_id, pubkey, ssh_config, log=print, library=None, workspace_shared=False, **kw):
        """Make the machine reachable and hand its boot to the base. Idempotent:
        (1) the Mac key is registered; (2) hosts/verda/startup.sh is registered as comfy-base-startup so the console's
        next fresh deploy can pick it; (3) running + ip + banner; (4) the Host block; (5) startup.sh --ensure over ssh,
        which mounts the data volume at /workspace and installs the comfy-base-boot unit on the RUNNING machine
        (a startup script runs only on the first boot of a fresh image, so this is what makes an instance deployed
        without it, or redeployed from an OS volume, equal to one that had it); (6) `ssh <alias> hostname`."""
        log = log or (lambda s: None)
        rec = self.pod(pod_id)
        if is_volume(rec):
            raise PodctlError("%s is a detached %s volume: podctl start %s first" % (pod_id, "OS" if rec.get("is_os_volume") else "data", pod_id))
        if norm_status(rec.get("status")) == "stopped":
            raise PodctlError("%s is offline: podctl start %s first" % (pod_id, pod_id))
        ids = self.key_ids(pubkey=pubkey, register=True, log=log)
        log("ensure: Mac key registered as %s" % ", ".join(ids))
        sid = self.script_id(register=True, log=log)
        log("ensure: startup script %s registered as %s" % (STARTUP_SCRIPT_NAME, sid))
        inst, host = self._up_and_configured(pod_id, ssh_config)
        log("ensure: Host %s -> %s:%d" % (_c().SSH_ALIAS, host, SSH_PORT))
        if not inst.get("ssh_key_ids") or not any(str(k) in ids for k in inst.get("ssh_key_ids") or []):
            log("ensure: note: the instance was deployed without the Mac key (ssh_key_ids %s); if `ssh %s` is refused, add the key in the console"
                % (inst.get("ssh_key_ids"), _c().SSH_ALIAS))
        # the shared library: which one this machine is attached to is read back from the account, never remembered
        # on the Mac, so a machine's library is whatever Verda says it is. The NFS endpoint is the one thing the API
        # may not carry yet (hosts/verda/README.md), hence --library / VERDA_LIBRARY.
        lib_vol = self._library_for(pod_id)
        src = (library or "").strip() or (self.env.get(LIBRARY_SRC_ENV) or "").strip() or (library_src_from(lib_vol) if lib_vol else "")
        self._library_on = lib_vol is not None and bool(src)
        self._library_src = src
        if lib_vol is not None:
            log("ensure: shared library %s (%s, %s GB) is attached to this machine" % (lib_vol.get("name") or "", lib_vol.get("id"), lib_vol.get("size")))
            if not src:
                log("ensure: WARNING its NFS endpoint is not in the API record, so it was NOT mounted. Pass --library "
                    "host:/export (the console's mount command names it) or set %s, then run ensure again." % LIBRARY_SRC_ENV)
        elif src:
            log("ensure: library %s was given but no shared volume is attached to %s, attach it in the console (or "
                "PUT /volumes action attach with instance_ids) first" % (src, pod_id))
            src = ""
            self._library_on = False
        # 2.5.0: --workspace makes the shared volume BE /workspace, the RunPod shape on a VM host: the base,
        # ComfyUI, the venv, the packages and the models all live on one store and the machine carries no data
        # volume. The endpoint is the same one, so it is resolved the same way and never typed twice.
        ws = ""
        if workspace_shared:
            if not src:
                raise PodctlError("--workspace needs the shared volume attached to %s with a readable NFS endpoint; "
                                  "attach it (PUT /volumes action attach) and run ensure again, or pass --library host:/export" % pod_id)
            ws = src
            log("ensure: /workspace will BE the shared store (%s): this machine needs no data volume" % ws)
        self._workspace_shared = bool(ws)
        r = _c().ssh_run(remote_ensure_cmd(startup_script_text(), library=src, workspace=ws), timeout=900)
        if r.returncode != 0:
            raise PodctlError("startup.sh --ensure failed on %s (rc %s): %s" % (pod_id, r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-400:]))
        out_lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()      # startup.sh logs on stderr
        ready = next((ln for ln in reversed(out_lines) if "READY" in ln), out_lines[-1] if out_lines else "(startup.sh printed nothing)")
        log("ensure: %s" % ready)
        hostname = _c()._ssh_hostname()
        return "reachable: %s:%d (hostname %s)" % (host, SSH_PORT, hostname)

    def deploy_like(self, like, name=None, gpu=None, wait=True, ssh_config=None, say=print):
        """A NEW instance from the source's image, on the source's data volumes (moved: a Verda volume attaches to ONE
        instance, so the source must be offline and its data volumes are detached first). The clone boots a fresh OS
        disk, so the registered startup script runs and mounts /workspace. Returns the new id."""
        src = self._instance(like)
        if src is None:
            raise PodctlError("%s: no such Verda instance (a stopped machine is an OS volume: podctl start <volume id> instead)" % like)
        st = (src.get("status") or "").lower()
        if st != "offline":
            raise PodctlError("%s is %s: a Verda BLOCK volume attaches to one instance at a time, so the source must be shut down (offline) "
                              "before its data volumes can move to the clone. (A SHARED volume is different: it attaches to several at once "
                              "and is carried to the clone without being detached.) Shut it down in the console (or `PUT /instances action "
                              "shutdown`) and deploy again; podctl stop would delete it instead" % (like, st))
        data, shared = [], []
        for vid in (src.get("volume_ids") or []):
            rec = self._volume(str(vid))
            (shared if (rec is not None and is_shared_volume(rec)) else data).append(str(vid))
        for vid in data:
            self.api.put("/volumes", {"id": vid, "action": "detach", "instance_id": like})
        itype = gpu or src.get("instance_type")
        if not itype:
            raise PodctlError("%s: the API did not name the instance type; pass --gpu <instance type>" % like)
        if not src.get("image"):
            raise PodctlError("%s: the API did not name the image; deploy from the console" % like)
        body = {"hostname": hostname_for(name or "%s-2" % (src.get("hostname") or "comfy-base")), "image": src["image"], "instance_type": itype,
                "location_code": src.get("location") or "", "ssh_key_ids": self.key_ids(register=True), "existing_volumes": data + shared,
                "description": "ComfyUI Base, cloned from %s by podctl" % like, "is_spot": bool(src.get("is_spot"))}
        # 3.0.0: no startup script at create time (root docs: a fresh OS volume has no /etc/fstab entry for the
        # workspace, so the registered script waits ten minutes at first boot); `ensure` configures it over ssh
        sid = None
        pod_id = new_id(self.api.post("/instances", body))
        say("deployed %s (%s): %s on %s in %s, data volumes %s (detached from %s), startup script %s" % (
            pod_id, body["hostname"], body["image"], itype, body["location_code"], ", ".join(data) or "none", like, sid or "none"))
        if not wait:
            return pod_id
        inst, host = self._up_and_configured(pod_id, ssh_config)
        say("  ssh answers on %s:%d (Host %s -> %s)" % (host, SSH_PORT, _c().SSH_ALIAS, pod_id))
        return pod_id

    # -- 3.1.0: a buyer's machine, made from nothing in a fresh project (buyer verbs in podctl; the app drives them)
    def balance(self):
        """The prepaid balance: {"amount": float, "currency": str} (GET /balance)."""
        b = self.api.get("/balance") or {}
        if not isinstance(b, dict):
            raise PodctlError("GET /balance answered %r" % (b,))
        return {"amount": float(b.get("amount") or 0), "currency": str(b.get("currency") or "")}

    def availability(self, instance_type, spot=False):
        """The locations where `instance_type` can be deployed now (GET /instance-availability), in the API's order."""
        rows = self.api.get("/instance-availability?is_spot=%s" % ("true" if spot else "false")) or []
        return [str(r.get("location_code")) for r in rows
                if isinstance(r, dict) and r.get("location_code") and instance_type in (r.get("availabilities") or [])]

    def create_machine(self, name, instance_type, location, data_gb, os_gb=100, image=None, pubkey=None, spot=False,
                       wait=True, ssh_config=None, say=print):
        """A buyer's machine from nothing: an NVMe data volume, then the instance with its own OS volume and that data
        volume attached. NO startup script at create time (a fresh OS volume has no fstab entry, and the registered
        script would wait ten minutes for one); `ensure` configures it over ssh. A refused instance (503: sold out
        here) takes its new data volume with it, so nothing is left billing. Returns the instance id."""
        img = image or self.env.get("VERDA_IMAGE") or DEFAULT_IMAGE
        keys = self.key_ids(pubkey=pubkey, register=True, log=say)
        vid = new_id(self.api.post("/volumes", {"type": "NVMe", "location_code": location, "size": int(data_gb),
                                                "name": "%s-data" % name}))
        body = {"hostname": hostname_for(name), "image": img, "instance_type": instance_type, "location_code": location,
                "ssh_key_ids": keys, "os_volume": {"name": "%s-os" % name, "size": int(os_gb)}, "existing_volumes": [vid],
                "description": "ComfyUI Base, a buyer's machine, created by podctl", "is_spot": bool(spot)}
        try:
            pod_id = new_id(self.api.post("/instances", body))
        except Exception:
            try:
                self.api.put("/volumes", {"id": vid, "action": "delete", "is_permanent": True})
            except Exception as e:      # the refusal is the news; a leftover volume is named so it can be removed
                say("  !! could not delete the new data volume %s after the refusal: %s" % (vid, e))
            raise
        say("created %s (%s): %s on %s in %s, OS volume %s-os %d GB, data volume %s %d GB" % (
            pod_id, body["hostname"], img, instance_type, location, name, int(os_gb), vid, int(data_gb)))
        if wait:
            inst, host = self._up_and_configured(pod_id, ssh_config)
            say("  ssh answers on %s:%d (Host %s -> %s)" % (host, SSH_PORT, _c().SSH_ALIAS, pod_id))
        return pod_id

    def machine_volumes(self, ident):
        """The ids of every volume a machine keeps: its instance's volumes, or an OS volume and its stem's data volumes."""
        inst = self._instance(ident)
        if inst is not None:
            ids = [str(v) for v in (inst.get("volume_ids") or [])]
            osv = inst.get("os_volume_id")
            return ([str(osv)] if osv and str(osv) not in ids else []) + ids
        vol = self._volume(ident)
        if vol is None:
            raise PodctlError("%s: no such instance or volume" % ident)
        stem = volume_stem(vol.get("name"))
        return [str(v["id"]) for v in (self.api.get("/volumes") or [])
                if isinstance(v, dict) and v.get("id") and not is_shared_volume(v) and stems_match(v.get("name"), stem)]

    def delete_volumes(self, ids, permanent=False):
        """To the trash (restorable for Verda's 96 hours) unless permanent. One call per volume."""
        for vid in ids:
            self.api.put("/volumes", {"id": vid, "action": "delete", "is_permanent": bool(permanent)})

    def host_env(self):
        """BASE_HOST and BASE_VOLUME, plus BASE_LIBRARY when the machine has a shared library.
        This override is not optional: podctl's write_host_env() runs right after ensure() and overwrites the file
        startup.sh just wrote, so a library recorded only by the script would be erased a second later."""
        text = Provider.host_env(self)
        if self._workspace_shared:
            # recorded on the shared store itself, which every machine reads: the base then keeps user/ and temp/
            # per machine and takes a lock before it installs.
            text += "BASE_VOLUME_SHARED=1\n"
            return text
        on = self._library_on
        if not on and self._last_pod:
            # every verb that writes host.env must answer the same way, not only ensure. The library is attached
            # to the instance or it is not; that is a fact about the account, so read it rather than remember it.
            try:
                on = self._library_for(self._last_pod) is not None
            except Exception:
                on = False
        if on and self._workspace_is_the_store():
            # 3.0.0: an attached shared volume that IS /workspace (ensure --workspace-shared, in an earlier process) is
            # the shared root, not a library beside it. `podctl install` wrote BASE_LIBRARY=/mnt/comfy-library here and
            # dropped BASE_VOLUME_SHARED=1 on every install (measured on FIN-03, 2026-09-25). Read from the machine.
            self._workspace_shared = True
            return text + "BASE_VOLUME_SHARED=1\n"
        if on:
            text += "BASE_LIBRARY=%s\n" % self.library_mount
        return text

    def _workspace_is_the_store(self):
        """True when the machine's /workspace is an NFS mount (the shared store itself); False when it cannot tell."""
        try:
            r = _c().ssh_run("findmnt -n -o FSTYPE /workspace", timeout=30)
        except Exception:
            return False
        return getattr(r, "returncode", 1) == 0 and (getattr(r, "stdout", "") or "").strip().startswith("nfs")

    def record_volume(self, pod, env=None):
        """With a SHARED library, record ITS size in state/volume.env; with only a block volume, record nothing.
        Why: the base's disk gate measures free space on the library's filesystem, and _base_fs_is_pool treats any
        `host:/path` device, every NFS mount, as a pool it cannot measure, so without a recorded quota the gate
        prints 'free space is not verifiable here' and never fires. A plain Verda data volume needs no record: it
        is a real block device and `df` on /workspace is honest."""
        import datetime
        if is_volume(pod):
            return None
        pod_id = str((pod or {}).get("id") or "")
        if not pod_id:
            return None
        vol = self._library_for(pod_id)
        if vol is None:
            return None
        size = vol.get("size")
        if size is None:
            return None
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = "%s/comfy-base/state" % self.volume_root
        cmd = ("mkdir -p %s && printf 'VOLUME_GB=%%s\\nVOLUME_ID=%%s\\nRECORDED=%%s\\n' %s %s %s > %s/volume.env"
               % (shlex.quote(path), shlex.quote(str(size)), shlex.quote(str(vol.get("id") or "")), shlex.quote(ts), shlex.quote(path)))
        r = _c().ssh_run(cmd, env=env)
        if r.returncode != 0:
            raise PodctlError("could not record the library size on the machine: %s" % ((r.stderr or r.stdout) or "").strip()[-300:])
        return "library %s GB (%s, shared) recorded in state/volume.env: the disk gate measures the library, not /workspace" % (size, vol.get("name") or vol.get("id"))
