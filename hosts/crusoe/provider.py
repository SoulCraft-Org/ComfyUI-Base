"""hosts/crusoe/provider.py: Crusoe Cloud behind the host interface.

One VM, one persistent disk, one startup script. The machine side is hosts/crusoe/startup.sh, which Crusoe runs as
root on EVERY boot: it mounts the data disk, gates the NVIDIA driver at 580, installs the CUDA toolkit, writes the
systemd unit and writes state/host.env. This file is the Mac side: what podctl calls to find, reach and move a
machine. hosts/crusoe/README.md is the recipe and records what a first live run should report back.

STILL EXPERIMENTAL. Every call below is written from Crusoe's own API reference and its published client, and the
whole of it is exercised against a fake REST server in suite/test_crusoe.py, but none of it has run against a live
account. Verda sat here once too, and its first live run corrected three things the documentation implied. Until
somebody does the same here the driver says EXPERIMENTAL once per run, and it is right to.

THE API
  base URL      https://api.cloud.crusoe.ai/v1
  auth          an access key id and a secret. EVERY request is signed:
                  payload   = path + "\n" + query + "\n" + VERB + "\n" + timestamp + "\n"
                              (path includes the /v1 prefix; query: parameters sorted by name as "k=v&k=v", empty
                              when there are none)
                  key       = base64.urlsafe_b64decode(secret + padding)      (pad with "=" to a multiple of 4)
                  signature = base64.urlsafe_b64encode(hmac.new(key, payload, sha256).digest()).rstrip("=")
                  headers   X-Crusoe-Timestamp: <timestamp>          (RFC3339, UTC, whole seconds)
                            Authorization: Bearer 1.0:<access_key_id>:<signature>
                Taken from two of Crusoe's own implementations, which agree: client-go auth/v1/auth.go
                (RawURLEncoding both ways, HMAC-SHA256, time.RFC3339 in UTC) and the Python in
                crusoe-registry-token-rotator. The worked example in the prose docs does NOT reproduce under any
                reading, so the code is the authority and the first live 200 is the proof.
  credentials   env CRUSOE_ACCESS_KEY_ID and CRUSOE_SECRET_KEY, else (3.19.0, on a Mac) the login Keychain items whose
                accounts are those two names, else ~/.crusoe/config (a pair counts only when both halves come from the
                same one of the three; the project name may come from the file either way):
                  [default]
                  access_key_id = ...
                  secret_key = ...
                  default_project = ...
                Never printed, logged or placed in an argument: the secret reaches exactly one place, the HMAC.
  project id    GET /organizations/projects  (the config's default_project by name, else the one project)

THE CALLS                                                    (verified against crusoecloud/client-go swagger/v1)
  GET|POST    /projects/{p}/compute/vms/instances
  GET|PATCH|DELETE
              /projects/{p}/compute/vms/instances/{vm_id}     PATCH body {"action": "START"|"STOP"|"RESET"}
  POST        /projects/{p}/compute/vms/instances/{vm_id}/attach-disks
  POST        /projects/{p}/compute/vms/instances/{vm_id}/detach-disks
  GET         /projects/{p}/compute/vms/instances/operations/{operation_id}
  GET         /projects/{p}/compute/vms/types
  GET         /organizations/projects
  create body name, type, location, ssh_public_key (required); image, startup_script, disks[] optional.
              A disk attachment is {"disk_id": ..., "mode": "read-write", "attachment_type": "data"}.
  async       a lifecycle call answers {"operation": {...}}; poll the operation until state is not IN_PROGRESS.

WHAT IS DIFFERENT HERE, AND WHY THE VERDA PROVIDER IS NOT A TEMPLATE FOR IT
  - stop is a real stop. A stopped Crusoe VM still exists and bills its disks only, so `stop` does not delete and
    `pods` lists instances, never volumes-as-machines. (On Verda a shut-down instance still bills the GPU, which is
    why stopping there deletes and the OS volume stands in for the machine.)
  - the public ip is DYNAMIC: it changes on every stop/start unless the VM was created with a static one. Every
    lifecycle call therefore rewrites the Host block before it returns.
  - the startup script is a CREATE-TIME field and cannot be changed through the API afterwards, and it runs on every
    boot. `ensure` therefore ships the current hosts/crusoe/startup.sh over ssh and runs its --ensure pass, which is
    the only way to update the behaviour of a machine that already exists.
  - the login is ubuntu, not root, so the --ensure pass needs sudo.
  - a disk takes ONE read-write attacher, so `deploy` detaches from the source before creating the clone, and passes
    the disk in the create body rather than attaching afterwards: a VM that boots without its disk sends startup.sh
    into a 15 minute wait for one.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from comfyui_hosts import Address, PodctlError, Provider, keychain

HERE = pathlib.Path(__file__).resolve().parent
VOLUME_ROOT = "/workspace"                          # where startup.sh mounts the data disk; the base's BASE_VOLUME here
DEFAULT_API_URL = "https://api.cloud.crusoe.ai/v1"
KEY_PATH = pathlib.Path.home() / ".ssh" / "id_ed25519"
CREDENTIALS_PATH = pathlib.Path.home() / ".crusoe" / "config"
STARTUP_SCRIPT_FILE = HERE / "startup.sh"
REMOTE_STARTUP = "/home/ubuntu/comfy-base-startup.sh"   # where `ensure` puts the script (ubuntu owns it; /root needs sudo)
REMOTE_EOF = "COMFY_BASE_STARTUP_EOF"
SSH_PORT = 22
SIG_VERSION = "1.0"

# Crusoe's state vocabulary is not fully documented. Normalise case-insensitively and treat anything unknown as
# transitional, which is the safe reading: the caller waits rather than concluding the machine is usable or gone.
STATUS = {"running": "running", "stopped": "stopped", "shutoff": "stopped", "shut_off": "stopped",
          "terminated": "gone", "deleted": "gone", "notfound": "gone"}
OP_DONE = ("succeeded", "complete", "completed", "done", "success")
OP_FAILED = ("failed", "error", "cancelled", "canceled")


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
    """(access_key_id, secret_key, default_project): the environment first, then (3.19.0, a Mac only) the login Keychain
    items whose accounts are CRUSOE_ACCESS_KEY_ID and CRUSOE_SECRET_KEY, then ~/.crusoe/config. Never echoed.

    The file is read loosely on purpose: `[default]` headers, comments, `export ` prefixes and either separator all
    appear in the wild, and a credential file that parses only one way is a support question waiting to happen.
    3.19.0: the key pair counts only when both halves come from the SAME source (an id from the environment and a
    secret from the file are never joined). The project is a name, not a credential: CRUSOE_PROJECT, else the file's."""
    env = os.environ if env is None else env
    project = (env.get("CRUSOE_PROJECT") or "").strip()
    path = pathlib.Path(path) if path else CREDENTIALS_PATH
    pair = None
    akid = (env.get("CRUSOE_ACCESS_KEY_ID") or "").strip()
    secret = (env.get("CRUSOE_SECRET_KEY") or "").strip()
    if akid and secret:
        pair = (akid, secret)
    if pair is None:
        akid = (keychain("CRUSOE_ACCESS_KEY_ID") or "").strip()
        secret = (keychain("CRUSOE_SECRET_KEY") or "").strip() if akid else ""
        if akid and secret:
            pair = (akid, secret)
    if (pair is None or not project) and path.exists():
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#;[":
                continue
            m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1).lower()
            if key in ("access_key_id", "crusoe_access_key_id"):
                found["id"] = _unquote(m.group(2))
            elif key in ("secret_key", "crusoe_secret_key"):
                found["secret"] = _unquote(m.group(2))
            elif key in ("default_project", "crusoe_project", "project"):
                found["project"] = _unquote(m.group(2))
        if pair is None and found.get("id") and found.get("secret"):
            pair = (found["id"], found["secret"])
        project = project or found.get("project", "")
    if pair:
        return pair[0], pair[1], project
    raise PodctlError("no Crusoe credentials: export CRUSOE_ACCESS_KEY_ID and CRUSOE_SECRET_KEY, or (on a Mac) keep both in "
                      "the login Keychain as items whose accounts are those two names, or write both as "
                      "`access_key_id = ...` and `secret_key = ...` lines in %s (hosts/crusoe/README.md)" % path)


# ---------------------------------------------------------------- the signed REST client

class HttpError(PodctlError):
    """A non-2xx answer: the status and the body, never the key or the signature."""

    def __init__(self, method, path, status, text):
        self.status = status
        super().__init__("%s %s -> HTTP %s: %s" % (method, path, status, (text or "")[:300]))


def canonical_query(params):
    """The query as the signature sees it: sorted by name, urlencoded, joined with &. Empty when there are none.

    Crusoe's own client builds this with Go's url.Values.Encode(), which sorts by key; a `;` separator is an error
    there, and we never produce one because the parameters arrive as a dict."""
    if not params:
        return ""
    items = []
    for k in sorted(params):
        v = params[k]
        for one in (v if isinstance(v, (list, tuple)) else [v]):
            items.append((k, str(one)))
    return urllib.parse.urlencode(items)


def sign(secret, path, query, verb, timestamp):
    """The signature for one request. `path` includes the /v1 prefix, exactly as it goes on the wire."""
    key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    payload = "%s\n%s\n%s\n%s\n" % (path, query, verb.upper(), timestamp)
    mac = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def rfc3339_now():
    """UTC, whole seconds, with the Z offset Crusoe's own client writes (Go's time.RFC3339 on a UTC time)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Api:
    """The signed REST client. Stdlib only, and it opens no socket until something asks it for data.

    There is no token and nothing to cache: every request carries its own signature over its own timestamp, so a
    401 means the key is wrong or the clock is off, and retrying identical bytes would answer identically."""

    USER_AGENT = "podctl/%s (ComfyUI Base; Crusoe REST client)" % BASE_VERSION_FOR_UA

    def __init__(self, base=None, access_key_id=None, secret_key=None, project=None, env=None, credentials_path=None):
        self.base = (base or DEFAULT_API_URL).rstrip("/")
        self._akid, self._secret, self._project_name = access_key_id, secret_key, project
        self._env, self._credentials_path = env, credentials_path
        self._project_id = None
        self.now = rfc3339_now                      # the suite pins this to assert an exact signature

    def credentials(self):
        if not (self._akid and self._secret):
            akid, secret, project = load_credentials(self._env, self._credentials_path)
            self._akid, self._secret = akid, secret
            self._project_name = self._project_name or project
        return self._akid, self._secret

    def _call(self, method, path, body=None, params=None):
        akid, secret = self.credentials()
        qs = canonical_query(params)
        # the signature covers the path as it appears on the wire, /v1 and all
        sig_path = urllib.parse.urlsplit(self.base).path + path
        ts = self.now()
        url = self.base + path + (("?" + qs) if qs else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer %s:%s:%s" % (SIG_VERSION, akid, sign(secret, sig_path, qs, method, ts)))
        req.add_header("X-Crusoe-Timestamp", ts)
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
            raise HttpError(method, path, e.code, text)
        except urllib.error.URLError as e:
            raise PodctlError("%s %s -> %s" % (method, path, e.reason))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace").strip()

    def get(self, path, params=None):
        return self._call("GET", path, params=params)

    def post(self, path, body=None):
        return self._call("POST", path, body)

    def patch(self, path, body=None):
        return self._call("PATCH", path, body)

    def delete(self, path):
        return self._call("DELETE", path)

    def project_id(self):
        """The project every compute path is scoped under: the configured name, else the only one there is."""
        if self._project_id:
            return self._project_id
        self.credentials()
        got = self.get("/organizations/projects")
        items = items_of(got)
        if not items:
            raise PodctlError("GET /organizations/projects returned no project: check the key's organisation")
        want = (self._project_name or "").strip()
        if want:
            for p in items:
                if str(p.get("name") or "") == want or str(p.get("id") or "") == want:
                    self._project_id = str(p["id"])
                    return self._project_id
            raise PodctlError("no Crusoe project named %r (found: %s)" % (want, ", ".join(str(p.get("name")) for p in items)))
        if len(items) > 1:
            raise PodctlError("the key sees %d projects (%s): name one with CRUSOE_PROJECT or `default_project` in %s"
                              % (len(items), ", ".join(str(p.get("name")) for p in items), CREDENTIALS_PATH))
        self._project_id = str(items[0]["id"])
        return self._project_id

    def vms(self, tail=""):
        return "/projects/%s/compute/vms/instances%s" % (self.project_id(), tail)


# ---------------------------------------------------------------- record helpers (pure: the suite drives these directly)

def items_of(got):
    """Crusoe wraps collections as {"items": [...]}; a bare list and a single object both turn up too."""
    if isinstance(got, dict):
        for k in ("items", "instances", "projects", "disks"):
            if isinstance(got.get(k), list):
                return got[k]
        return [got] if got.get("id") else []
    return list(got) if isinstance(got, list) else []


def norm_status(raw):
    return STATUS.get((raw or "").strip().lower(), "transitional")


def inst_ip(inst):
    """The public ipv4, or "". network_interfaces[0].ips[0].public_ipv4.address, defensively: a machine that is
    still building has the interface but not the address, and a private-only VM never gets one.

    The value is parsed before it is believed. It ends up in the Host block of the operator's ~/.ssh/config, and a
    string carrying a newline would put whatever followed it there as an ssh directive. That needs the API to be
    lying, which is a high bar, but the check is one line and the blast radius is the operator's ssh config."""
    import ipaddress
    for nic in (inst or {}).get("network_interfaces") or []:
        for ip in (nic or {}).get("ips") or []:
            addr = ((ip or {}).get("public_ipv4") or {}).get("address")
            if not addr:
                continue
            try:
                return str(ipaddress.ip_address(str(addr).strip()))
            except ValueError:
                raise PodctlError("the API gave %r as a public address, which is not an ip: refusing to write it "
                                  "into your ssh config" % (str(addr)[:60],))
    return ""


def inst_disks(inst):
    return [str((d or {}).get("disk_id") or (d or {}).get("id") or "") for d in ((inst or {}).get("disks") or []) if d]


def hostname_for(name):
    h = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")[:63]
    return h or "comfy-base"


def op_of(answer):
    """The operation an async call answers with, whether it is wrapped or bare."""
    if isinstance(answer, dict):
        op = answer.get("operation")
        return op if isinstance(op, dict) else (answer if "state" in answer or "operation_id" in answer else {})
    return {}


def startup_script_text():
    try:
        return STARTUP_SCRIPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise PodctlError("cannot read %s: %s" % (STARTUP_SCRIPT_FILE, e))


def remote_ensure_cmd(text):
    """One ssh command: land startup.sh on the machine through a quoted heredoc and run its --ensure pass as root.

    The script is a CREATE-TIME field on this host and cannot be changed through the API, so shipping the current
    copy and re-running it is the only way to move an existing machine forward. sudo because the login is ubuntu."""
    if REMOTE_EOF in text:
        raise PodctlError("startup.sh contains the heredoc delimiter %s" % REMOTE_EOF)
    return ("cat > %s <<'%s'\n%s\n%s\nchmod 700 %s && sudo bash %s --ensure"
            % (REMOTE_STARTUP, REMOTE_EOF, text.rstrip("\n"), REMOTE_EOF, REMOTE_STARTUP, REMOTE_STARTUP))


DROPIN = "/etc/systemd/system/comfy-base-boot.service.d/jupyter.conf"


def jupyter_dropin(token):
    """(command, stdin) for the drop-in that carries JUPYTER_TOKEN. JupyterLab starts only when that reaches
    boot.sh, and on this host boot.sh is a systemd unit, so without the drop-in `podctl jupyter` reports no token
    forever and reads as a broken command.

    The token goes on STDIN and never into the command. Two reasons, both found in review:

    1. A value in the command is argv. sshd runs a non-login remote command as `bash -c '<cmd>'`, so it lands in
       the machine's /proc/<pid>/cmdline, world-readable on Linux, and in ps on this Mac. Writing the file 0600
       while publishing its contents in the process table protects nothing. lib/boot.sh states the rule for this
       exact secret: the token rides the environment, never argv.
    2. `printf <body>` makes the body printf's FORMAT string, so a token containing % or a backslash escape is
       silently rewritten: MEASURED, `ab%sc` became `abc` and `100%done` became `1000one`. printf failing inside
       a pipeline leaves the pipeline's status as tee's, so the && chain continued and ensure reported success
       over a mangled file - an operator would hold a token the machine does not have."""
    cmd = ("sudo mkdir -p %s && umask 077 && sudo tee %s >/dev/null && sudo chmod 600 %s && sudo systemctl daemon-reload"
           % (shlex.quote(str(pathlib.PurePosixPath(DROPIN).parent)), shlex.quote(DROPIN), shlex.quote(DROPIN)))
    return cmd, "[Service]\nEnvironment=JUPYTER_TOKEN=%s\n" % token


# ---------------------------------------------------------------- the provider

class CrusoeProvider(Provider):
    """Crusoe Cloud: a VM with a persistent disk, reached as ubuntu over ssh, driven by a signed REST API."""

    name = "crusoe"
    volume_root = VOLUME_ROOT
    ssh_user = "ubuntu"
    ssh_alias = "crusoe"
    key_path = KEY_PATH
    experimental = True                 # until a live account has run it end to end; see the module docstring

    def __init__(self, api=None, env=None):
        self.api = api if api is not None else Api(env=env)
        self.sleep = time.sleep         # an instance attribute so the suite can null it

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
        """GET one instance, or None when Crusoe says it is gone."""
        try:
            got = self.api.get(self.api.vms("/" + str(ident)))
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        rec = got.get("instance") if isinstance(got, dict) and isinstance(got.get("instance"), dict) else got
        return rec if isinstance(rec, dict) and rec.get("id") else None

    def _by_name(self, ident):
        for inst in self.pods():
            if str(inst.get("name") or "") == str(ident):
                return inst
        return None

    def _wait_operation(self, answer, what):
        """A lifecycle call answers with an operation, not a finished machine. Poll it until it is not IN_PROGRESS.

        Verda has no analogue: there the resource itself is the truth. Here the resource can still read as the old
        state while the operation runs, so polling the instance instead would report success too early."""
        op = op_of(answer)
        oid = op.get("operation_id") or op.get("id")
        if not oid:
            return op                                      # a synchronous answer: nothing to wait for
        first = (op.get("state") or "").strip().lower()
        if first in OP_FAILED:                             # already terminal: say so now, do not poll a doomed operation
            raise PodctlError("%s failed: %s" % (what, json.dumps(op.get("result") or op)[:300]))
        if first in OP_DONE:
            return op
        path = self.api.vms("/operations/%s" % oid)

        def done():
            cur = op_of(self.api.get(path)) or {}
            state = (cur.get("state") or "").strip().lower()
            if state in OP_FAILED:
                raise PodctlError("%s failed: %s" % (what, json.dumps(cur.get("result") or cur)[:300]))
            return cur if state and state not in ("in_progress", "running", "pending") else None

        return self._wait(done, what, tries=120, every=5)

    def _wait_running(self, ident):
        def probe():
            inst = self._instance(ident)
            if inst is None:
                raise PodctlError("%s is gone while waiting for it to be running" % ident)
            return inst if norm_status(inst.get("state") or inst.get("status")) == "running" and inst_ip(inst) else None
        return self._wait(probe, "%s to be running with a public ip" % ident, tries=120, every=5)

    def _wait_banner(self, host):
        for _ in range(120):
            if self.banner(host, SSH_PORT):
                return
            self.sleep(5)
        raise PodctlError("sshd never answered on %s:%d: read the machine's console (the startup log is "
                          "/var/log/comfy-base-startup.log)" % (host, SSH_PORT))

    def _up_and_configured(self, ident, ssh_config):
        """running -> a public ip -> the ssh banner -> the Host block rewritten. The ip is dynamic on this host, so
        the block is rewritten after every lifecycle call, not only the first."""
        inst = self._wait_running(ident)
        host = inst_ip(inst)
        self._wait_banner(host)
        _c().write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, SSH_PORT)
        return inst, host

    def _resolve(self, ident):
        rec = self._instance(ident) or self._by_name(ident)
        if rec is None:
            raise PodctlError("no Crusoe VM with id or name %r in this project" % ident)
        return rec

    # -- the interface: listing and facts

    def pods(self):
        return [i for i in items_of(self.api.get(self.api.vms())) if isinstance(i, dict) and i.get("id")]

    def pod(self, ident):
        return self._resolve(ident)

    def facts(self, pod):
        ip = inst_ip(pod)
        return {"id": str(pod.get("id") or ""), "name": pod.get("name") or "",
                "status": norm_status(pod.get("state") or pod.get("status")),
                "host": ip, "port": SSH_PORT if ip else 0, "user": self.ssh_user,
                "image": pod.get("image") or "", "gpu": pod.get("type") or "",
                "volumes": inst_disks(pod), "kind": "instance", "location": pod.get("location") or "",
                "instance_type": pod.get("type") or ""}

    def address(self, pod):
        f = self.facts(pod)
        return Address(f["host"], f["port"], self.ssh_user) if f["status"] == "running" and f["host"] else None

    def status_text(self, pod):
        f = self.facts(pod)
        disks = ", ".join(f["volumes"]) or "(none)"
        return "\n".join([
            "id         %s" % f["id"],
            "name       %s" % (f["name"] or "(unnamed)"),
            "type       %s" % (f["instance_type"] or "?"),
            "image      %s" % (f["image"] or "?"),
            "location   %s" % (f["location"] or "?"),
            "state      %s (%s)" % (pod.get("state") or pod.get("status") or "?", f["status"]),
            "address    %s" % ("%s:%d as %s" % (f["host"], f["port"], f["user"]) if f["host"] else "(no public ip)"),
            "disks      %s" % disks,
            "startup    %s" % ("present" if (pod.get("startup_script") or "").strip() else "absent (the VM was created without one)"),
            "firewall   default-deny inbound with 22 open: 8188 and 8888 are NOT reachable from outside, and the "
            "base binds loopback. podctl tunnel is the way in.",
        ])

    def pods_lines(self):
        lines = []
        for inst in self.pods():
            f = self.facts(inst)
            lines.append("%-22s %-18s %-16s %-12s %-16s %s" % (f["id"][:22], (f["name"] or "-")[:18],
                                                               (f["instance_type"] or "-")[:16], f["status"][:12],
                                                               f["host"] or "-", SSH_PORT if f["host"] else "-"))
        return lines

    # -- the interface: lifecycle

    def ensure(self, pod_id, pubkey, ssh_config, log=print, **kw):
        """Make the machine reachable and hand its boot to the base. Idempotent, and there is no API step at all:
        the key went in at create time and 22 is open by default, so this is reachability, the Host block, the
        current startup.sh re-run, and the jupyter drop-in."""
        rec = self._resolve(pod_id)
        ident = str(rec.get("id"))
        if norm_status(rec.get("state") or rec.get("status")) != "running":
            raise PodctlError("%s is %s: podctl start %s first" % (pod_id, norm_status(rec.get("state") or rec.get("status")), pod_id))
        inst, host = self._up_and_configured(ident, ssh_config)
        log("ensure: %s answers on %s:%d as %s" % (ident, host, SSH_PORT, self.ssh_user))
        r = _c().ssh_run(remote_ensure_cmd(startup_script_text()), timeout=1800)
        if r.returncode != 0:
            raise PodctlError("startup.sh --ensure failed on %s (rc %s): %s"
                              % (ident, r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-400:]))
        out_lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()      # startup.sh logs on stderr
        ready = next((ln for ln in reversed(out_lines) if "READY" in ln), out_lines[-1] if out_lines else "(startup.sh printed nothing)")
        log("ensure: %s" % ready)
        tok = (kw.get("jupyter_token") or os.environ.get("JUPYTER_TOKEN") or "").strip()
        if tok:
            cmd, body = jupyter_dropin(tok)
            j = _c().ssh_run(cmd, timeout=120, input=body)
            log("ensure: jupyter token %s" % ("written to a systemd drop-in" if j.returncode == 0 else "NOT written (rc %s)" % j.returncode))
        return "reachable: %s:%d (hostname %s)" % (host, SSH_PORT, _c()._ssh_hostname())

    def _action(self, ident, action, what):
        return self._wait_operation(self.api.patch(self.api.vms("/" + ident), {"action": action}), what)

    def start(self, pod_id, wait=True, ssh_config=None):
        rec = self._resolve(pod_id)
        ident = str(rec.get("id"))
        if norm_status(rec.get("state") or rec.get("status")) == "running":
            if wait:
                self._up_and_configured(ident, ssh_config)
            return "%s is already running" % ident
        self._action(ident, "START", "%s to start" % ident)
        if not wait:
            return "%s starting (the public ip is new on every start; podctl ssh-config %s when it is up)" % (ident, ident)
        inst, host = self._up_and_configured(ident, ssh_config)
        return "started %s: %s:%d as %s (Host %s rewritten: this host's ip changes on every start)" % (
            ident, host, SSH_PORT, self.ssh_user, _c().SSH_ALIAS)

    def stop(self, pod_id, wait=True):
        """A real stop: the VM keeps its disks and its identity, and bills only the disks while it is down."""
        rec = self._resolve(pod_id)
        ident = str(rec.get("id"))
        if norm_status(rec.get("state") or rec.get("status")) == "stopped":
            return "%s is already stopped" % ident
        self._action(ident, "STOP", "%s to stop" % ident)
        if wait:
            self._wait(lambda: norm_status((self._instance(ident) or {}).get("state")) == "stopped",
                       "%s to read as stopped" % ident, tries=60, every=5)
        return "stopped %s: the disks are kept and bill on; podctl start %s brings it back with a NEW public ip" % (ident, ident)

    def restart(self, pod_id, wait=True, ssh_config=None):
        rec = self._resolve(pod_id)
        ident = str(rec.get("id"))
        self._action(ident, "RESET", "%s to reset" % ident)
        if not wait:
            return "%s resetting" % ident
        inst, host = self._up_and_configured(ident, ssh_config)
        return "restarted %s: %s:%d as %s" % (ident, host, SSH_PORT, self.ssh_user)

    def deploy_like(self, like, name=None, gpu=None, wait=True, ssh_config=None, say=print):
        """A NEW machine carrying the source's disk. The disk moves, so the source must be stopped first: Crusoe
        allows one read-write attacher, and the clone is created WITH the disk rather than attaching afterwards,
        because a VM that boots without it sends startup.sh into a 15 minute wait for a disk that never appears."""
        src = self._resolve(like)
        sid = str(src.get("id"))
        if norm_status(src.get("state") or src.get("status")) != "stopped":
            raise PodctlError("%s is %s: a Crusoe disk takes one read-write attacher, so the source must be stopped "
                              "before its disk can move to the clone (podctl stop %s)" % (like, norm_status(src.get("state")), like))
        disks = inst_disks(src)
        location = src.get("location") or ""
        if not location:
            raise PodctlError("%s has no location: a disk is zonal, so the clone must be created in the source's zone" % like)
        pub = ""
        try:
            pub = pathlib.Path(str(self.key_path) + ".pub").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not pub:
            raise PodctlError("no public key at %s.pub: Crusoe takes the key at create time" % self.key_path)
        for d in disks:
            say("deploy: detaching %s from %s" % (d, sid))
            self._wait_operation(self.api.post(self.api.vms("/%s/detach-disks" % sid), {"disk_ids": [d]}),
                                 "%s to detach from %s" % (d, sid))
        body = {"name": hostname_for(name or (str(src.get("name") or sid) + "-2")),
                "type": gpu or src.get("type"), "location": location, "ssh_public_key": pub,
                "startup_script": startup_script_text()}
        if src.get("image"):
            body["image"] = src["image"]
        if disks:
            body["disks"] = [{"disk_id": d, "mode": "read-write", "attachment_type": "data"} for d in disks]
        got = self.api.post(self.api.vms(), body)
        new = op_of(got).get("result") if isinstance(op_of(got).get("result"), dict) else (got.get("instance") if isinstance(got, dict) else None)
        new_id = str((new or got or {}).get("id") or "") if isinstance(new or got, dict) else ""
        if not new_id:
            self._wait_operation(got, "the clone of %s to be created" % sid)
            cand = self._by_name(body["name"])
            new_id = str((cand or {}).get("id") or "")
        if not new_id:
            raise PodctlError("POST %s answered without an instance id: %s" % (self.api.vms(), json.dumps(got)[:300]))
        say("deploy: created %s (%s, %s) carrying %s" % (new_id, body["type"], location, ", ".join(disks) or "no disk"))
        if not wait:
            return new_id
        self._up_and_configured(new_id, ssh_config)
        return new_id

    # -- what the base needs on the machine

    def record_volume(self, pod, env=None):
        """Nothing to record: /workspace is an ext4 disk and df on it reports the truth, so the base's disk gate
        needs no help. (The signature keeps `env` because PodIO.upload passes it by keyword.)"""
        return None
