"""unit tier: the Crusoe host provider (hosts/crusoe/provider.py) against a fake Crusoe REST server, a fake ssh
banner and a fake ssh_run. Nothing here touches the network, a real machine, the user's ssh config or ~/.crusoe:
every provider is built on an Api pointed at 127.0.0.1 with its credentials handed in, so a real key in the
environment can neither be used nor leak into a test.

The fake enforces the signature. A provider that stops signing, signs the wrong payload or drops the timestamp
gets 401s here, which is the point: the signing is the one part of this host nobody has yet checked against a live
200, so the suite checks it against Crusoe's own published formula instead."""
import base64
import hashlib
import hmac
import importlib.util
import json
import pathlib
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "_build" / "pod" / "podctl.py"
STARTUP = BASE / "hosts" / "crusoe" / "startup.sh"

MAC_KEY = "ssh-ed25519 AAAAC3fake mac@key"
AKID = "fake-access-key-id"
SECRET = "uZFGf918DmiBUwBWv8lnEg"          # urlsafe-b64, decodes cleanly; not a real credential
PROJECT = "99999999-0000-4000-8000-000000000009"
VM_ID = "11111111-2222-4333-8444-555555555555"
VM2_ID = "22222222-2222-4333-8444-555555555555"
DISK = "aaaaaaaa-0000-4000-8000-00000000000a"
ITYPE = "l40s-48gb.1x"
IMAGE = "ubuntu22.04-nvidia-pcie-docker:latest"
LOCATION = "us-east1-a"


def _load():
    if not TOOL.exists():
        pytest.skip("no base/comfyui-base/_build/pod/podctl.py (not a repo checkout)")
    spec = importlib.util.spec_from_file_location("podctl", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _crusoe(podctl):
    return podctl._hosts.load_provider("crusoe", podctl.BASE_DIR)


def _vm(**over):
    rec = {"id": VM_ID, "name": "comfy-1", "type": ITYPE, "image": IMAGE, "location": LOCATION,
           "state": "RUNNING", "startup_script": "#!/bin/bash\n",
           "network_interfaces": [{"ips": [{"public_ipv4": {"address": "127.0.0.1"}}]}],
           "disks": [{"disk_id": DISK, "mode": "read-write", "attachment_type": "data"}]}
    rec.update(over)
    return rec


class FakeCrusoe:
    """An in-memory Crusoe compute API: projects, instances, the PATCH actions, attach/detach, and the async
    operations they answer with. It VERIFIES the signature on every call, records each request with its headers,
    and holds an operation IN_PROGRESS for `op_polls` reads so the waiter is actually exercised."""

    def __init__(self, instances=(), projects=None, op_polls=2, akid=AKID, secret=SECRET):
        self.instances = {i["id"]: dict(i) for i in instances}
        self.projects = list(projects) if projects is not None else [{"id": PROJECT, "name": "default"}]
        self.requests = []
        self.headers = []
        self.op_polls = op_polls
        self.ops = {}
        self.counter = 0
        self.akid, self.secret = akid, secret
        self.bad_auth = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _signed_ok(self, verb):
                auth = self.headers.get("Authorization", "")
                ts = self.headers.get("X-Crusoe-Timestamp", "")
                split = urllib.parse.urlsplit(self.path)
                want_sig = srv._sign(split.path, split.query, verb, ts)
                if auth != "Bearer 1.0:%s:%s" % (srv.akid, want_sig) or not ts:
                    srv.bad_auth.append((verb, self.path, auth, ts))
                    self._send(401, {"code": "unauthorized"})
                    return False
                return True

            def _record(self, verb, body=None):
                srv.requests.append((verb, urllib.parse.urlsplit(self.path).path, body))
                srv.headers.append({"auth": self.headers.get("Authorization", ""),
                                    "ua": self.headers.get("User-Agent", ""),
                                    "ts": self.headers.get("X-Crusoe-Timestamp", "")})

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                if not self._signed_ok("GET"):
                    return
                self._record("GET")
                p = urllib.parse.urlsplit(self.path).path
                if p == "/v1/organizations/projects":
                    return self._send(200, {"items": srv.projects})
                base = "/v1/projects/%s/compute/vms/instances" % PROJECT
                if p.startswith(base + "/operations/"):
                    oid = p.rsplit("/", 1)[-1]
                    op = srv.ops.get(oid)
                    if op is None:
                        return self._send(404, {"code": "not_found"})
                    op["reads"] += 1
                    state = "IN_PROGRESS" if op["reads"] <= srv.op_polls else "SUCCEEDED"
                    if state == "SUCCEEDED" and op.get("on_done"):
                        op.pop("on_done")()
                    return self._send(200, {"operation": {"operation_id": oid, "state": state}})
                if p == base:
                    return self._send(200, {"items": list(srv.instances.values())})
                if p.startswith(base + "/"):
                    vid = p[len(base) + 1:]
                    inst = srv.instances.get(vid)
                    return self._send(200, inst) if inst else self._send(404, {"code": "not_found"})
                return self._send(404, {"code": "not_found"})

            def do_PATCH(self):
                if not self._signed_ok("PATCH"):
                    return
                body = self._body()
                self._record("PATCH", body)
                vid = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
                inst = srv.instances.get(vid)
                if inst is None:
                    return self._send(404, {"code": "not_found"})
                action = (body or {}).get("action")
                want = {"START": "RUNNING", "STOP": "STOPPED", "RESET": "RUNNING"}.get(action)
                if want is None:
                    return self._send(400, {"code": "bad_action"})
                return self._send(202, {"operation": srv._op(lambda: inst.update(state=want))})

            def do_POST(self):
                if not self._signed_ok("POST"):
                    return
                body = self._body()
                p = urllib.parse.urlsplit(self.path).path
                self._record("POST", body)
                base = "/v1/projects/%s/compute/vms/instances" % PROJECT
                if p.endswith("/detach-disks"):
                    vid = p.split("/")[-2]
                    inst = srv.instances.get(vid) or {}
                    keep = [d for d in inst.get("disks", []) if d.get("disk_id") not in (body or {}).get("disk_ids", [])]
                    inst["disks"] = keep
                    return self._send(202, {"operation": srv._op()})
                if p.endswith("/attach-disks"):
                    vid = p.split("/")[-2]
                    inst = srv.instances.get(vid) or {}
                    inst.setdefault("disks", []).extend((body or {}).get("disks", []))
                    return self._send(202, {"operation": srv._op()})
                if p == base:
                    srv.counter += 1
                    new = {"id": VM2_ID, "name": (body or {}).get("name"), "type": (body or {}).get("type"),
                           "image": (body or {}).get("image"), "location": (body or {}).get("location"),
                           "state": "RUNNING", "startup_script": (body or {}).get("startup_script") or "",
                           "network_interfaces": [{"ips": [{"public_ipv4": {"address": "127.0.0.1"}}]}],
                           "disks": list((body or {}).get("disks") or [])}
                    srv.instances[VM2_ID] = new
                    return self._send(201, new)
                return self._send(404, {"code": "not_found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _sign(self, path, query, verb, ts):
        key = base64.urlsafe_b64decode(self.secret + "=" * (-len(self.secret) % 4))
        payload = "%s\n%s\n%s\n%s\n" % (path, query, verb, ts)
        return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def _op(self, on_done=None):
        self.counter += 1
        oid = "op-%d" % self.counter
        self.ops[oid] = {"reads": 0, "on_done": on_done} if on_done else {"reads": 0}
        return {"operation_id": oid, "state": "IN_PROGRESS"}

    def calls(self, method=None, path=None):
        return [(m, p, b) for m, p, b in self.requests if (method is None or m == method) and (path is None or p == path)]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _prov(podctl, fake, tmp_path):
    """A provider on the fake, credentials in hand, no waiting, and the driver bound to it."""
    mod = _crusoe(podctl)
    api = mod.Api(fake.url, access_key_id=fake.akid, secret_key=fake.secret, project=PROJECT)
    prov = mod.CrusoeProvider(api=api)
    prov.key_path = tmp_path / "id_ed25519"
    (tmp_path / "id_ed25519.pub").write_text(MAC_KEY + "\n", encoding="utf-8")
    prov.sleep = lambda s: None
    podctl.set_provider(prov, env={})
    return prov


def _cfg(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host other\n  HostName 10.0.0.1\n  Port 2222\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- the signature

# Golden vectors. These constants were computed ONCE from Crusoe's own published Python
# (crusoe-registry-token-rotator, rotate_token_api.py), whose formula client-go auth/v1/auth.go agrees with:
#
#   sig_payload = api_version + request_path + "\n" + query + "\n" + verb + f"\n{dt}\n"
#   decoded     = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
#   signature   = base64.urlsafe_b64encode(hmac.new(decoded, sig_payload, sha256).digest()).rstrip("=")
#
# They are LITERALS on purpose. An assertion that recomputes the signature with the same formula the production
# code uses proves only that the code agrees with itself: if the reading of Crusoe's algorithm were wrong, both
# sides would be wrong identically and the test would still pass. Pinning the bytes means a later "simplification"
# of sign() fails here. It still does not prove the bytes are what the live service wants - only a live 200 does
# that, and nobody has one yet - but it does mean the value under test is not derived from the thing under test.
SIGNING_VECTORS = (
    ("/v1/capacities", "location=us-northcentral1-a", "GET", "2026-09-19T10:00:00Z",
     "m0Hrwj16G11CbOrx6c7HdYC3hj3qjoxe8q6eYCcBKbU"),
    ("/v1/projects/p-1/compute/vms/instances", "", "POST", "2026-01-02T03:04:05Z",
     "pr0YqVv6O7tqwGOTENxbLB4_31NVVV5mAJRWZFDnBC8"),
)


def test_unit_crusoe_signs_exactly_as_crusoes_own_client_does():
    """The one part of this host with no live 200 behind it, so the vectors are pinned rather than recomputed."""
    podctl = _load()
    mod = _crusoe(podctl)
    for path, query, verb, ts, want in SIGNING_VECTORS:
        got = mod.sign(SECRET, path, query, verb, ts)
        assert got == want, "%s %s: got %s, want %s" % (verb, path, got, want)
        assert "=" not in got, "the signature is unpadded"
    # At least one vector must exercise the url-safe alphabet. Standard and url-safe base64 differ only in two
    # characters, so a signature that happens to contain neither cannot tell the encodings apart: vector 1 does
    # not, and on its own it would accept a standard-b64 implementation. Vector 2 does. Asserted, not assumed,
    # because that is a property of the bytes and a later edit could quietly lose it.
    assert any(set(want) & set("-_") for *_, want in SIGNING_VECTORS), \
        "no vector contains - or _, so none of them distinguishes url-safe base64 from standard"
    # and the payload really is newline-separated in that order: changing any field changes the signature
    base_args = SIGNING_VECTORS[0][:4]
    for i in range(4):
        args = list(base_args)
        args[i] = args[i] + "x"
        assert mod.sign(SECRET, *args) != SIGNING_VECTORS[0][4], "field %d does not affect the signature" % i
    # the query is canonical: sorted by name, urlencoded, empty when there is none
    assert mod.canonical_query({"product_name": "a100.8x", "location": "us-northcentral1-a"}) == \
        "location=us-northcentral1-a&product_name=a100.8x"
    assert mod.canonical_query(None) == "" and mod.canonical_query({}) == ""
    # the timestamp is RFC3339, UTC, whole seconds
    assert mod.rfc3339_now().endswith("Z") and "." not in mod.rfc3339_now()


def test_unit_crusoe_every_request_is_signed_and_the_secret_never_leaves_the_hmac(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.pods()
        prov.pod(VM_ID)
        assert fake.requests and not fake.bad_auth, fake.bad_auth
        for (_, path, _), h in zip(fake.requests, fake.headers):
            assert SECRET not in path and SECRET not in h["auth"] and SECRET not in h["ua"]
            assert h["auth"].startswith("Bearer 1.0:%s:" % AKID) and h["ts"], h
            assert "podctl/" in h["ua"] and "Crusoe" in h["ua"]
    finally:
        fake.close()


def test_unit_crusoe_a_wrong_secret_is_a_401_not_a_silent_pass(tmp_path):
    """If the fake did not verify the signature this suite would prove nothing about signing."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        mod = _crusoe(podctl)
        api = mod.Api(fake.url, access_key_id=AKID, secret_key="d3JvbmdzZWNyZXQ", project=PROJECT)
        prov = mod.CrusoeProvider(api=api)
        prov.sleep = lambda s: None
        with pytest.raises(mod.HttpError) as ei:
            prov.pods()
        assert getattr(ei.value, "status", None) == 401
        assert fake.bad_auth, "the fake must have rejected it"
    finally:
        fake.close()


# ---------------------------------------------------------------- credentials and project

def test_unit_crusoe_pair_order_is_env_then_the_keychain_then_the_file_and_never_mixed(tmp_path, keychain_stub, capsys):
    """3.19.0: CRUSOE_ACCESS_KEY_ID + CRUSOE_SECRET_KEY from the environment, else the login Keychain items whose accounts
    are those names, else ~/.crusoe/config. A pair counts only when both halves come from the same source."""
    podctl = _load()
    mod = _crusoe(podctl)
    cfg = tmp_path / "config"
    cfg.write_text("access_key_id = file-id\nsecret_key = file-secret\ndefault_project = proj-file\n", encoding="utf-8")
    kc = {"CRUSOE_ACCESS_KEY_ID": "kc-id", "CRUSOE_SECRET_KEY": "kc-secret"}
    keychain_stub.serve(kc)
    both = {"CRUSOE_ACCESS_KEY_ID": "env-id", "CRUSOE_SECRET_KEY": "env-secret", "CRUSOE_PROJECT": "proj-env"}
    assert mod.load_credentials(both, cfg) == ("env-id", "env-secret", "proj-env")
    assert keychain_stub.calls() == []                                                   # the env pair: nothing asked
    # half in the env: the Keychain's whole pair, never env-id with kc-secret; the project still from the file
    assert mod.load_credentials({"CRUSOE_ACCESS_KEY_ID": "env-id"}, cfg) == ("kc-id", "kc-secret", "proj-file")
    assert keychain_stub.calls() == ["find-generic-password -a CRUSOE_ACCESS_KEY_ID -w",
                                     "find-generic-password -a CRUSOE_SECRET_KEY -w"]
    # half in the Keychain: the file's whole pair
    keychain_stub.serve({"CRUSOE_ACCESS_KEY_ID": "kc-id"})
    assert mod.load_credentials({"CRUSOE_SECRET_KEY": "env-secret"}, cfg) == ("file-id", "file-secret", "proj-file")
    # half everywhere: no pair at all, and the error names every place
    cfg.write_text("access_key_id = file-id\n", encoding="utf-8")
    with pytest.raises(podctl.PodctlError) as ei:
        mod.load_credentials({"CRUSOE_SECRET_KEY": "env-secret"}, cfg)
    msg = str(ei.value)
    assert "CRUSOE_ACCESS_KEY_ID" in msg and "CRUSOE_SECRET_KEY" in msg and "login Keychain" in msg and str(cfg) in msg, msg
    out = capsys.readouterr()
    assert "secret" not in (out.out + out.err).replace("CRUSOE_SECRET_KEY", "")


def test_unit_crusoe_credentials_come_from_the_env_then_the_file(tmp_path):
    podctl = _load()
    mod = _crusoe(podctl)
    cfg = tmp_path / "config"
    cfg.write_text("[default]\n# a comment\naccess_key_id = 'from-file'\nsecret_key: from-file-secret\n"
                   "export default_project = \"proj-from-file\"\n", encoding="utf-8")
    assert mod.load_credentials({}, cfg) == ("from-file", "from-file-secret", "proj-from-file")
    # 3.19.0: a pair comes whole from one source. Half of it in the env is not joined to the file's other half: the
    # file's own pair is used. The project is a name, not a credential, and still comes from the file.
    got = mod.load_credentials({"CRUSOE_ACCESS_KEY_ID": "from-env"}, cfg)
    assert got == ("from-file", "from-file-secret", "proj-from-file")
    got = mod.load_credentials({"CRUSOE_ACCESS_KEY_ID": "from-env", "CRUSOE_SECRET_KEY": "from-env-secret"}, cfg)
    assert got == ("from-env", "from-env-secret", "proj-from-file")
    # and the error names both variables and the path
    with pytest.raises(podctl.PodctlError) as ei:
        mod.load_credentials({}, tmp_path / "absent")
    assert "CRUSOE_ACCESS_KEY_ID" in str(ei.value) and "CRUSOE_SECRET_KEY" in str(ei.value) and "absent" in str(ei.value)


def test_unit_crusoe_picks_the_named_project_and_refuses_an_ambiguous_one(tmp_path):
    podctl = _load()
    two = [{"id": "p-one", "name": "one"}, {"id": "p-two", "name": "two"}]
    fake = FakeCrusoe(instances=[_vm()], projects=two)
    try:
        mod = _crusoe(podctl)
        named = mod.Api(fake.url, access_key_id=AKID, secret_key=SECRET, project="two")
        assert named.project_id() == "p-two"
        ambiguous = mod.Api(fake.url, access_key_id=AKID, secret_key=SECRET)
        with pytest.raises(podctl.PodctlError) as ei:
            ambiguous.project_id()
        assert "CRUSOE_PROJECT" in str(ei.value) and "one" in str(ei.value) and "two" in str(ei.value)
        missing = mod.Api(fake.url, access_key_id=AKID, secret_key=SECRET, project="three")
        with pytest.raises(podctl.PodctlError) as ei:
            missing.project_id()
        assert "no Crusoe project named" in str(ei.value)
    finally:
        fake.close()


# ---------------------------------------------------------------- reads

def test_unit_crusoe_facts_normalise_state_and_address_needs_running_and_an_ip(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        f = prov.facts(prov.pod(VM_ID))
        assert f["id"] == VM_ID and f["status"] == "running" and f["host"] == "127.0.0.1" and f["port"] == 22
        assert f["user"] == "ubuntu" and f["instance_type"] == ITYPE and f["volumes"] == [DISK]
        a = prov.address(prov.pod(VM_ID))
        assert (a.host, a.port, a.user) == ("127.0.0.1", 22, "ubuntu")
        mod = _crusoe(podctl)
        for raw, want in (("RUNNING", "running"), ("running", "running"), ("STOPPED", "stopped"),
                          ("SHUTOFF", "stopped"), ("PROVISIONING", "transitional"), ("", "transitional")):
            assert mod.norm_status(raw) == want, raw
        # a stale ip on a stopped machine must not look reachable
        assert prov.address(_vm(state="STOPPED")) is None
        # nor a running one with no address yet
        assert prov.address(_vm(network_interfaces=[{"ips": [{}]}])) is None
    finally:
        fake.close()


def test_unit_crusoe_status_and_pods_lines_say_the_facts_and_the_firewall(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm(), _vm(id=VM2_ID, name="comfy-2", state="STOPPED", network_interfaces=[])])
    try:
        prov = _prov(podctl, fake, tmp_path)
        txt = prov.status_text(prov.pod(VM_ID))
        for needle in (VM_ID, "comfy-1", ITYPE, LOCATION, "127.0.0.1:22 as ubuntu", DISK, "podctl tunnel"):
            assert needle in txt, needle
        assert "8188" in txt and "NOT reachable" in txt          # the firewall claim is Crusoe's, not Verda's
        lines = prov.pods_lines()
        assert len(lines) == 2 and any("comfy-1" in l and "running" in l for l in lines)
        assert any("comfy-2" in l and "stopped" in l for l in lines)
        # a name resolves as well as an id
        assert prov.pod("comfy-2")["id"] == VM2_ID
        with pytest.raises(podctl.PodctlError) as ei:
            prov.pod("nope")
        assert "no Crusoe VM with id or name" in str(ei.value)
    finally:
        fake.close()


# ---------------------------------------------------------------- lifecycle

def test_unit_crusoe_start_waits_on_the_operation_not_the_instance(tmp_path, monkeypatch):
    """A lifecycle call answers with an operation and the instance can still read as the old state while it runs,
    so polling the instance would report success early. Verda has no analogue of this: there the resource is the
    truth. The fake holds the operation IN_PROGRESS for two reads, so a provider that does not poll fails here."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm(state="STOPPED", network_interfaces=[])], op_polls=2)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    cfg = _cfg(tmp_path)
    try:
        prov = _prov(podctl, fake, tmp_path)
        # the machine gains its ip when the operation completes
        fake.instances[VM_ID]["network_interfaces"] = [{"ips": [{"public_ipv4": {"address": "127.0.0.1"}}]}]
        out = prov.start(VM_ID, wait=True, ssh_config=cfg)
        assert [b for m, p, b in fake.calls("PATCH")] == [{"action": "START"}]
        assert any("/operations/" in p for m, p, b in fake.calls("GET")), "the operation was never polled"
        assert fake.ops["op-1"]["reads"] > 2, fake.ops
        assert fake.instances[VM_ID]["state"] == "RUNNING"
        assert "NEW public ip" in out or "Host" in out
        assert "Host crusoe\n  HostName 127.0.0.1\n  Port 22\n  User ubuntu\n" in cfg.read_text(encoding="utf-8")
        assert "Host other" in cfg.read_text(encoding="utf-8"), "an unrelated block must survive"
        assert "already running" in prov.start(VM_ID, wait=False)
    finally:
        fake.close()


def test_unit_crusoe_stop_is_a_real_stop_and_keeps_the_disk(tmp_path):
    """Not Verda: there a shut-down instance still bills the GPU so stopping deletes it. A stopped Crusoe VM still
    exists, keeps its disks and its id, and bills the disks only."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.stop(VM_ID, wait=True)
        assert [b for m, p, b in fake.calls("PATCH")] == [{"action": "STOP"}]
        assert not fake.calls("DELETE"), "stop must not delete on this host"
        assert VM_ID in fake.instances and fake.instances[VM_ID]["state"] == "STOPPED"
        assert fake.instances[VM_ID]["disks"], "the disk is kept"
        assert "disks are kept" in out and "NEW public ip" in out
        assert "already stopped" in prov.stop(VM_ID)
    finally:
        fake.close()


def test_unit_crusoe_restart_resets_and_rewrites_the_block(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    cfg = _cfg(tmp_path)
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.restart(VM_ID, wait=True, ssh_config=cfg)
        assert [b for m, p, b in fake.calls("PATCH")] == [{"action": "RESET"}]
        assert "restarted %s" % VM_ID in out
        assert "Host crusoe" in cfg.read_text(encoding="utf-8")
    finally:
        fake.close()


def test_unit_crusoe_a_failed_operation_is_the_error(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        mod = _crusoe(podctl)
        with pytest.raises(podctl.PodctlError) as ei:
            prov._wait_operation({"operation": {"operation_id": "x", "state": "FAILED"}}, "the thing")
        assert "the thing failed" in str(ei.value)
        # a synchronous answer with no operation is not waited on
        assert prov._wait_operation({"id": "done"}, "nothing") == {}
    finally:
        fake.close()


# ---------------------------------------------------------------- ensure

def test_unit_crusoe_ensure_ships_the_current_startup_script_and_scrapes_its_ready_line(tmp_path, monkeypatch):
    """There is no API step: the key went in at create time and 22 is open. What ensure does is reachability, the
    Host block, and re-running the CURRENT startup.sh, which is the only way to move a machine forward on a host
    whose startup script is a create-time field."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    calls = []

    def fake_ssh_run(cmd, env=None, timeout=None):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="",
                                     stderr="2026-09-19T10:00:00Z comfy-base-startup[ensure]: READY disk=/dev/vdb mounted=yes unit=active\n")
    monkeypatch.setattr(podctl, "ssh_run", fake_ssh_run)
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert out == "reachable: 127.0.0.1:22 (hostname comfy-base)"
        assert len(calls) == 1
        cmd = calls[0]
        assert "cat > /home/ubuntu/comfy-base-startup.sh <<'COMFY_BASE_STARTUP_EOF'\n" in cmd
        assert cmd.rstrip().endswith("sudo bash /home/ubuntu/comfy-base-startup.sh --ensure"), cmd[-120:]
        assert STARTUP.read_text(encoding="utf-8").rstrip("\n") in cmd, "the CURRENT script is shipped"
        assert any("READY disk=/dev/vdb" in l for l in logs)
        assert "Host crusoe" in cfg.read_text(encoding="utf-8")
        # no API writes at all: ensure is ssh only
        assert not fake.calls("POST") and not fake.calls("PATCH")
        # idempotent: run it again, nothing new is written to the API
        n = len(fake.requests)
        prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert not [1 for m, _, _ in fake.requests[n:] if m in ("POST", "PATCH")] and len(calls) == 2
        # a failing pass is the error, with the script's own tail
        monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None:
                            types.SimpleNamespace(returncode=1, stdout="", stderr="no data disk appeared"))
        with pytest.raises(podctl.PodctlError) as ei:
            prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert "startup.sh --ensure failed" in str(ei.value) and "no data disk appeared" in str(ei.value)
    finally:
        fake.close()


def test_unit_crusoe_ensure_refuses_a_stopped_machine_and_says_what_to_do(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm(state="STOPPED")])
    try:
        prov = _prov(podctl, fake, tmp_path)
        with pytest.raises(podctl.PodctlError) as ei:
            prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=_cfg(tmp_path))
        assert "podctl start %s" % VM_ID in str(ei.value)
    finally:
        fake.close()


def test_unit_crusoe_the_jupyter_token_rides_stdin_and_never_argv(tmp_path, monkeypatch):
    """Two defects found in review, both here. The token must not appear in the COMMAND: sshd runs a non-login
    remote command as `bash -c '<cmd>'`, so anything in it lands in the machine's world-readable
    /proc/<pid>/cmdline and in ps on this Mac, which is the rule lib/boot.sh states for this exact secret. And
    the body must not become printf's FORMAT string: MEASURED, `ab%sc` was written as `abc` and `100%done` as
    `1000one`, and because printf fails inside a pipeline the && chain carried on and ensure reported success
    over a mangled file."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    seen = []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None, input=None: (
        seen.append((cmd, input)), types.SimpleNamespace(returncode=0, stdout="", stderr="READY\n"))[1])
    try:
        prov = _prov(podctl, fake, tmp_path)
        mod = _crusoe(podctl)
        for token in ("s3cret", "ab%sc", "100%done", "tok%", "a b'c\"d"):
            seen.clear()
            prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=_cfg(tmp_path), log=lambda *_: None, jupyter_token=token)
            drop = [(c, i) for c, i in seen if "jupyter.conf" in c]
            assert len(drop) == 1, seen
            cmd, body = drop[0]
            assert token not in cmd, "the token reached argv: %r" % cmd
            assert body == "[Service]\nEnvironment=JUPYTER_TOKEN=%s\n" % token, body
            assert "printf" not in cmd, "printf would interpret the body as a format string"
            assert "tee" in cmd and "chmod 600" in cmd and "daemon-reload" in cmd, cmd
        # the pure form says the same, without an ensure around it
        cmd, body = mod.jupyter_dropin("100%done")
        assert "100%done" not in cmd and body.endswith("JUPYTER_TOKEN=100%done\n")
        # without a token nothing is written at all
        seen.clear()
        prov.ensure(VM_ID, pubkey=MAC_KEY, ssh_config=_cfg(tmp_path), log=lambda *_: None)
        assert not [c for c, _ in seen if "jupyter.conf" in c]
    finally:
        fake.close()


def test_unit_crusoe_an_address_that_is_not_an_ip_is_refused(tmp_path):
    """The address comes from the API and ends up in the Host block of the operator's ~/.ssh/config. A value
    carrying a newline would put whatever followed it there as an ssh directive. That needs the API to be lying,
    which is a high bar, but the check is one line and the blast radius is the operator's own ssh config."""
    podctl = _load()
    mod = _crusoe(podctl)
    assert mod.inst_ip(_vm()) == "127.0.0.1"
    assert mod.inst_ip({"network_interfaces": []}) == ""
    for bad in ("127.0.0.1\n  ProxyCommand /bin/sh", "not-an-ip", "127.0.0.1 evil"):
        with pytest.raises(podctl.PodctlError) as ei:
            mod.inst_ip({"network_interfaces": [{"ips": [{"public_ipv4": {"address": bad}}]}]})
        assert "not an ip" in str(ei.value)


# ---------------------------------------------------------------- deploy

def test_unit_crusoe_deploy_detaches_first_and_creates_the_clone_carrying_the_disk(tmp_path, monkeypatch):
    """A Crusoe disk takes one read-write attacher, so the source must be stopped and the disk detached before the
    clone can have it. The disk goes in the CREATE body rather than a later attach: a VM that boots without its
    disk sends startup.sh into a 15 minute wait for one."""
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm(state="STOPPED", network_interfaces=[])])
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    said = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        new_id = prov.deploy_like(VM_ID, name="comfy-2", wait=False, say=said.append)
        assert new_id == VM2_ID
        order = [p for m, p, b in fake.requests if m == "POST"]
        assert any(p.endswith("/detach-disks") for p in order), order
        assert order.index(next(p for p in order if p.endswith("/detach-disks"))) < \
            order.index(next(p for p in order if p.endswith("/instances"))), "detach must precede create"
        body = next(b for m, p, b in fake.requests if m == "POST" and p.endswith("/instances"))
        assert body["disks"] == [{"disk_id": DISK, "mode": "read-write", "attachment_type": "data"}]
        assert body["location"] == LOCATION and body["type"] == ITYPE and body["ssh_public_key"] == MAC_KEY
        assert body["startup_script"].startswith("#!"), "the clone gets the current startup script"
        assert fake.instances[VM_ID]["disks"] == [], "the source gave the disk up"
        assert any("detaching" in s for s in said)
    finally:
        fake.close()


def test_unit_crusoe_deploy_refuses_a_running_source_with_the_reason(tmp_path):
    podctl = _load()
    fake = FakeCrusoe(instances=[_vm()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        with pytest.raises(podctl.PodctlError) as ei:
            prov.deploy_like(VM_ID, wait=False)
        assert "one read-write attacher" in str(ei.value) and "podctl stop" in str(ei.value)
    finally:
        fake.close()


# ---------------------------------------------------------------- shape

def test_unit_crusoe_provider_shape_and_lazy_construction(tmp_path, monkeypatch):
    """Constructing the provider must open no socket, or `podctl --help` would need credentials. And the module
    must hold exactly ONE Provider subclass, because provider_for picks it with next() over vars(mod)."""
    podctl = _load()
    mod = _crusoe(podctl)

    def boom(*a, **k):
        raise AssertionError("constructing the provider must open no socket")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    p = podctl.provider_for("crusoe", env={"CRUSOE_ACCESS_KEY_ID": "x", "CRUSOE_SECRET_KEY": "y"})
    assert p.name == "crusoe" and p.ssh_user == "ubuntu" and p.ssh_alias == "crusoe"
    assert p.volume_root == "/workspace"
    assert p.experimental is True, "until a live account has run it end to end"
    assert p.host_env() == "BASE_HOST=crusoe\nBASE_VOLUME=/workspace\n"
    subclasses = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, podctl.Provider) and v is not podctl.Provider]
    assert subclasses == [mod.CrusoeProvider]


def test_unit_crusoe_record_volume_takes_the_env_keyword_podio_passes():
    """PodIO.upload calls record_volume(pod, env=...). A signature without `env` is a TypeError, which escapes
    main()'s PodctlError handler and tracebacks instead of diagnosing."""
    podctl = _load()
    mod = _crusoe(podctl)
    prov = mod.CrusoeProvider(api=object())
    assert prov.record_volume({"id": "x"}, env={"PATH": "/usr/bin"}) is None
    assert prov.record_volume({"id": "x"}) is None
