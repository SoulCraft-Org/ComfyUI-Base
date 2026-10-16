"""unit tier: the Verda host provider (hosts/verda/provider.py) against a fake Verda REST server, a fake ssh banner
and a fake ssh_run. Nothing here touches the network, a real machine, the user's ssh config or ~/.verda: every
provider is built on an Api pointed at 127.0.0.1 with a token already in hand, so no oauth call ever happens
unless a test points one at the fake's own /oauth2/token."""
import importlib.util
import json
import pathlib
import re
import subprocess
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "_build" / "pod" / "podctl.py"
STARTUP = BASE / "hosts" / "verda" / "startup.sh"
README = BASE / "hosts" / "verda" / "README.md"

MAC_KEY = "ssh-ed25519 AAAAC3fake mac@key"
INST_ID = "11111111-2222-4333-8444-555555555555"
OS_VOL = "aaaaaaaa-0000-4000-8000-00000000000a"
DATA_VOL = "bbbbbbbb-0000-4000-8000-00000000000b"
OTHER_OS = "cccccccc-0000-4000-8000-00000000000c"
OTHER_DATA = "dddddddd-0000-4000-8000-00000000000d"
LIB_VOL = "eeeeeeee-0000-4000-8000-00000000000e"
LIB_SRC = "nfs.fin-01.verda.com:/pseudo/comfy-library"
ITYPE = "1RTXPRO6000.10V"
IMAGE = "ubuntu-24.04-cuda-13.0-open-docker"


def _load():
    if not TOOL.exists():
        pytest.skip("no base/comfyui-base/_build/pod/podctl.py (not a repo checkout)")
    spec = importlib.util.spec_from_file_location("podctl", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verda(podctl):
    return podctl._hosts.load_provider("verda", podctl.BASE_DIR)


def _instance(**over):
    # the fields api.verda.com/v1/docs lists for GET /instances/{id}
    i = {"id": INST_ID, "hostname": "comfy-base", "image": IMAGE, "instance_type": ITYPE, "ip": "127.0.0.1", "private_ip": "10.0.0.5",
         "status": "running", "os_volume_id": OS_VOL, "volume_ids": [DATA_VOL], "ssh_key_ids": ["key-1"], "startup_script_id": None,
         "location": "FIN-01", "gpu": "RTX PRO 6000", "gpu_memory": 96, "price_per_hour": 1.9, "is_spot": False,
         "created_at": "2026-09-16T10:00:00Z"}
    i.update(over)
    return i


def _volume(vid, name, is_os, status="attached", instance_id=INST_ID, location="FIN-01", size=500):
    return {"id": vid, "name": name, "size": size, "type": "NVMe", "status": status, "instance_id": instance_id,
            "is_os_volume": is_os, "location": location}


def _shared(vid=LIB_VOL, name="comfy-library", instances=(INST_ID,), location="FIN-01", size=500, **over):
    """A Verda SHARED filesystem: a volume of a shared type, attached to SEVERAL instances at once. The record
    names them in `instances`, which is what GET /volumes returns for one (api.verda.com/v1/docs)."""
    # MEASURED: an ATTACHED shared volume reads "exported", a fresh one "created", and `target` carries the
    # NFS endpoint (on a plain block volume the same key carries the device name instead).
    v = {"id": vid, "name": name, "size": size, "type": "NVMe_Shared", "is_shared_fs": True,
         "status": ("exported" if instances else "created"),
         "target": "nfs.fin-01.datacrunch.io:/%s-abc" % name, "pseudo_path": "/%s-abc" % name,
         "instance_id": (instances[0] if instances else None), "instances": [{"id": i} for i in instances],
         "is_os_volume": False, "location": location}
    v.update(over)
    return v


class FakeVerda:
    """An in-memory Verda REST v1: /oauth2/token, /instances (GET, POST, PUT actions), /volumes (GET, POST, PUT
    attach|detach), /sshkeys, /scripts. Records every request as (method, path, body) plus its auth and UA headers.
    A new or started instance is `provisioning` with no ip for `provision_polls` GETs, then `running` with one."""

    def __init__(self, instances=(), volumes=(), keys=(), scripts=(), provision_polls=2, token="tok-fake"):
        self.instances = {i["id"]: dict(i) for i in instances}
        self.volumes = {v["id"]: dict(v) for v in volumes}
        self.keys = [dict(k) for k in keys]
        self.scripts = [dict(s) for s in scripts]
        self.requests = []
        self.headers = []
        self.token_requests = []
        self.provision_polls = provision_polls
        # 2.4.0: a live account showed that DELETING an instance does not make GET /instances/{id} a 404, the
        # record survives, status discontinued. `on_delete` lets a test model that, and `listed` lets the LIST
        # differ from the per-id read, which is the only place the truth shows.
        self.on_delete = None
        self.listed = None          # None: every instance is listed
        self.polls = {}
        self.counter = 0
        self.token = token
        self.delete_fails = False
        # 3.1.0: the buyer verbs' endpoints
        self.balance = {"amount": 42.5, "currency": "usd"}
        self.availability = [{"location_code": "FIN-01", "availabilities": ["1CPU.4V"]},
                             {"location_code": "FIN-03", "availabilities": [ITYPE, "1CPU.4V"]}]
        self.sold_out = False       # POST /instances answers 503 "Not enough resources", as measured in FIN-02
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else None

            def _record(self, method, body):
                srv.requests.append((method, self.path, body))
                srv.headers.append({"auth": self.headers.get("Authorization", ""), "ua": self.headers.get("User-Agent", "")})

            def _authed(self):
                if self.headers.get("Authorization", "") != "Bearer " + srv.token:
                    self._send(401, {"code": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                self._record("GET", None)
                if not self._authed():
                    return
                p = self.path
                if p == "/instances":
                    return self._send(200, [i for k, i in srv.instances.items() if srv.listed is None or k in srv.listed])
                if p.startswith("/instances/"):
                    iid = p.split("/")[2]
                    inst = srv.instances.get(iid)
                    if inst is None:
                        return self._send(404, {"code": "not_found", "message": "instance not found"})
                    srv.polls[iid] = srv.polls.get(iid, 0) + 1
                    if inst["status"] == "provisioning" and srv.polls[iid] >= srv.provision_polls:
                        inst["status"], inst["ip"] = "running", "127.0.0.1"
                    return self._send(200, inst)
                if p == "/volumes":
                    return self._send(200, list(srv.volumes.values()))
                if p.startswith("/volumes/"):
                    vol = srv.volumes.get(p.split("/")[2])
                    return self._send(200, vol) if vol else self._send(404, {"code": "not_found"})
                if p == "/sshkeys":
                    return self._send(200, srv.keys)
                if p == "/scripts":
                    return self._send(200, srv.scripts)
                if p == "/instance-types":
                    return self._send(200, [{"instance_type": ITYPE}])
                if p == "/balance":
                    return self._send(200, srv.balance)
                if p.startswith("/instance-availability"):
                    return self._send(200, srv.availability)
                self._send(404, {"code": "not_found"})

            def do_POST(self):
                body = self._body()
                self._record("POST", body)
                if self.path == "/oauth2/token":
                    srv.token_requests.append(body)
                    return self._send(200, {"access_token": srv.token, "expires_in": 3600})
                if not self._authed():
                    return
                srv.counter += 1
                if self.path == "/instances":
                    if srv.sold_out:
                        return self._send(503, {"code": "service_unavailable", "message": "Not enough resources"})
                    iid = "inst-new-%d" % srv.counter
                    vol = srv.volumes.get(body.get("image"))
                    os_id = vol["id"] if vol else "os-new-%d" % srv.counter
                    if vol:
                        vol["status"], vol["instance_id"] = "attached", iid
                    else:
                        osv = body.get("os_volume") or {}
                        srv.volumes[os_id] = _volume(os_id, osv.get("name") or "OS-" + body["hostname"], True, instance_id=iid,
                                                     location=body.get("location_code"), size=osv.get("size") or 100)
                    for did in body.get("existing_volumes") or []:
                        srv.volumes[did]["status"], srv.volumes[did]["instance_id"] = "attached", iid
                    srv.instances[iid] = _instance(id=iid, hostname=body["hostname"], image=body["image"], instance_type=body["instance_type"],
                                                   ip=None, status="provisioning", os_volume_id=os_id, volume_ids=list(body.get("existing_volumes") or []),
                                                   ssh_key_ids=list(body.get("ssh_key_ids") or []), startup_script_id=body.get("startup_script_id"),
                                                   location=body.get("location_code"), is_spot=bool(body.get("is_spot")))
                    srv.polls[iid] = 0
                    return self._send(202, iid)                      # the id as a JSON string
                if self.path == "/sshkeys":
                    kid = "key-%d" % srv.counter
                    srv.keys.append({"id": kid, "name": body["name"], "key": body["key"], "fingerprint": "SHA256:fake"})
                    return self._send(201, {"id": kid})               # the id in an object
                if self.path == "/scripts":
                    sid = "script-%d" % srv.counter
                    srv.scripts.append({"id": sid, "name": body["name"], "script": body["script"]})
                    return self._send(201, sid)
                if self.path == "/volumes":
                    vid = "vol-new-%d" % srv.counter
                    srv.volumes[vid] = _volume(vid, body["name"], False, status="detached", instance_id=None, location=body.get("location_code"), size=body.get("size"))
                    return self._send(202, vid)
                self._send(404, {"code": "not_found"})

            def do_PUT(self):
                body = self._body()
                self._record("PUT", body)
                if not self._authed():
                    return
                if self.path == "/instances":
                    inst = srv.instances.get(body.get("id"))
                    if inst is None:
                        return self._send(404, {"code": "not_found"})
                    action = body.get("action")
                    if action in ("shutdown", "force_shutdown"):
                        inst["status"], inst["ip"] = "offline", None
                    elif action == "start":
                        inst["status"], inst["ip"] = "provisioning", None
                        srv.polls[inst["id"]] = 0
                    elif action == "delete":
                        keep_not = set(body.get("volume_ids") or [])
                        for vid, vol in list(srv.volumes.items()):
                            if vol.get("instance_id") == inst["id"]:
                                if vid in keep_not:
                                    del srv.volumes[vid]
                                else:
                                    vol["status"], vol["instance_id"] = "detached", None
                        if srv.on_delete is not None:
                            srv.on_delete(inst["id"])
                        else:
                            del srv.instances[inst["id"]]
                    else:
                        return self._send(400, {"code": "bad_action"})
                    return self._send(202, {})
                if self.path == "/volumes":
                    vol = srv.volumes.get(body.get("id"))
                    if vol is None:
                        return self._send(404, {"code": "not_found"})
                    if body.get("action") == "detach":
                        vol["status"], vol["instance_id"] = "detached", None
                        inst = srv.instances.get(body.get("instance_id") or "")
                        if inst and vol["id"] in inst.get("volume_ids", []):
                            inst["volume_ids"].remove(vol["id"])
                    elif body.get("action") == "attach":
                        vol["status"], vol["instance_id"] = "attached", body.get("instance_id")
                    elif body.get("action") == "delete":
                        if body.get("is_permanent"):
                            del srv.volumes[vol["id"]]
                        else:
                            vol["status"] = "deleted"
                    return self._send(202, {})
                self._send(404, {"code": "not_found"})

            def do_DELETE(self):
                self._record("DELETE", self._body())
                if not self._authed():
                    return
                if self.path.startswith("/scripts/"):
                    sid = self.path.split("/")[2]
                    if srv.delete_fails:
                        return self._send(500, {"code": "internal"})
                    before = len(srv.scripts)
                    srv.scripts = [x for x in srv.scripts if x.get("id") != sid]
                    return self._send(200 if len(srv.scripts) < before else 404, {})
                self._send(404, {"code": "not_found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def calls(self, method=None, path=None):
        return [(m, p, b) for m, p, b in self.requests if (method is None or m == method) and (path is None or p == path)]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _prov(podctl, fake, tmp_path, env=None):
    """A provider on the fake with the token in hand, a key pair under tmp_path, no waiting, and the driver bound to it."""
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api(fake.url, token=fake.token), env=env if env is not None else {})
    prov.key_path = tmp_path / "verda_comfyui"
    (tmp_path / "verda_comfyui.pub").write_text(MAC_KEY + "\n", encoding="utf-8")
    prov.sleep = lambda s: None
    podctl.set_provider(prov, env={})                                   # env={}: a PODCTL_HOST in the real environment must not leak in
    return prov


def _cfg(tmp_path):
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host other\n  HostName 1.2.3.4\n  User me\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- the token and the credentials

def test_unit_verda_token_exchange_sends_the_secret_in_the_body_only_and_reuses_the_token():
    podctl = _load()
    mod = _verda(podctl)
    fake = FakeVerda(instances=[_instance()])
    try:
        api = mod.Api(fake.url, credentials=("cid-123", "sec-XYZ"))
        assert api.get("/instances")[0]["id"] == INST_ID
        assert fake.requests[0][:2] == ("POST", "/oauth2/token")
        assert fake.requests[0][2] == {"grant_type": "client_credentials", "client_id": "cid-123", "client_secret": "sec-XYZ"}
        assert fake.headers[0]["auth"] == ""                              # nothing to bear yet
        assert fake.headers[1]["auth"] == "Bearer tok-fake"
        for (_, path, _), h in zip(fake.requests, fake.headers):
            assert "sec-XYZ" not in path and "sec-XYZ" not in h["auth"] and "sec-XYZ" not in h["ua"]
        assert fake.headers[1]["ua"].startswith("podctl/") and "Verda" in fake.headers[1]["ua"]
        api.get("/instances")
        assert len(fake.token_requests) == 1                              # the token is kept, not re-requested
        # an explicit token skips the exchange entirely
        n = len(fake.requests)
        mod.Api(fake.url, token="tok-fake").get("/volumes")
        assert [p for _, p, _ in fake.requests[n:]] == ["/volumes"]
        # a 404 is an HttpError (a PodctlError) that carries its status
        with pytest.raises(podctl.PodctlError) as ei:
            api.get("/instances/nope")
        assert getattr(ei.value, "status", None) == 404 and "sec-XYZ" not in str(ei.value)
    finally:
        fake.close()


def test_unit_verda_pair_order_is_env_then_the_keychain_then_the_file_and_never_mixed(tmp_path, keychain_stub, capsys):
    """3.19.0: VERDA_CLIENT_ID + VERDA_CLIENT_SECRET from the environment, else the login Keychain items whose accounts are
    those names, else ~/.verda/credentials. A pair counts only when both halves come from the same source."""
    podctl = _load()
    mod = _verda(podctl)
    f = tmp_path / "credentials"
    f.write_text("client_id = file-id\nclient_secret = file-secret\n", encoding="utf-8")
    keychain_stub.serve({"VERDA_CLIENT_ID": "kc-id", "VERDA_CLIENT_SECRET": "kc-secret"})
    assert mod.load_credentials(env={"VERDA_CLIENT_ID": "env-id", "VERDA_CLIENT_SECRET": "env-secret"}, path=f) == ("env-id", "env-secret")
    assert keychain_stub.calls() == []
    assert mod.load_credentials(env={"VERDA_CLIENT_ID": "env-id"}, path=f) == ("kc-id", "kc-secret")
    assert keychain_stub.calls() == ["find-generic-password -a VERDA_CLIENT_ID -w", "find-generic-password -a VERDA_CLIENT_SECRET -w"]
    # only the id in the Keychain: the file's whole pair, never kc-id with file-secret
    keychain_stub.serve({"VERDA_CLIENT_ID": "kc-id"})
    assert mod.load_credentials(env={"VERDA_CLIENT_SECRET": "env-secret"}, path=f) == ("file-id", "file-secret")
    # no id in the Keychain: the secret is not even asked for
    keychain_stub.serve({"VERDA_CLIENT_SECRET": "kc-secret"})
    n = len(keychain_stub.calls())
    assert mod.load_credentials(env={}, path=f) == ("file-id", "file-secret")
    assert keychain_stub.calls()[n:] == ["find-generic-password -a VERDA_CLIENT_ID -w"]
    # half everywhere: no pair, and the error names every place
    f.write_text("client_secret = file-secret\n", encoding="utf-8")
    with pytest.raises(podctl.PodctlError) as ei:
        mod.load_credentials(env={"VERDA_CLIENT_ID": "env-id"}, path=f)
    msg = str(ei.value)
    assert "VERDA_CLIENT_ID" in msg and "VERDA_CLIENT_SECRET" in msg and "login Keychain" in msg and str(f) in msg, msg
    out = capsys.readouterr()
    assert "secret" not in (out.out + out.err).replace("VERDA_CLIENT_SECRET", "")


def test_unit_verda_credentials_come_from_env_or_the_file_and_a_missing_pair_names_both(tmp_path):
    podctl = _load()
    mod = _verda(podctl)
    f = tmp_path / "credentials"
    f.write_text('# verda\n[default]\nclient_id = "abc"\nclient_secret = \'s3cr3t\'\n', encoding="utf-8")
    assert mod.load_credentials(env={}, path=f) == ("abc", "s3cr3t")
    f.write_text("client_id=abc2\nCLIENT_SECRET = s3cr3t2   \n", encoding="utf-8")
    assert mod.load_credentials(env={}, path=f) == ("abc2", "s3cr3t2")
    assert mod.load_credentials(env={"VERDA_CLIENT_ID": "e", "VERDA_CLIENT_SECRET": "f"}, path=f) == ("e", "f")
    for env in ({}, {"VERDA_CLIENT_ID": "only-the-id"}):
        with pytest.raises(podctl.PodctlError) as ei:
            mod.load_credentials(env=env, path=tmp_path / "absent")
        assert "VERDA_CLIENT_ID" in str(ei.value) and "VERDA_CLIENT_SECRET" in str(ei.value)
    # the Api is lazy: constructing one reads nothing; the first call is where a missing pair surfaces
    api = mod.Api("http://127.0.0.1:1", env={}, credentials_path=tmp_path / "absent")
    with pytest.raises(podctl.PodctlError) as ei:
        api.get("/instances")
    assert "VERDA_CLIENT_SECRET" in str(ei.value)


# ---------------------------------------------------------------- listing and facts

def test_unit_verda_pods_lists_instances_and_detached_volumes(tmp_path):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    try:
        prov = _prov(podctl, fake, tmp_path)
        pods = prov.pods()
        assert [p["id"] for p in pods] == [INST_ID, OTHER_OS]                 # attached volumes are the instance's, not a row
        assert pods[1]["kind"] == "volume" and pods[1]["status"] == "detached-volume" and pods[1]["name"] == "old-box-os"
        lines = prov.pods_lines()
        assert len(lines) == 2 and INST_ID in lines[0] and "running" in lines[0] and "127.0.0.1" in lines[0]
        assert OTHER_OS in lines[1] and "detached" in lines[1] and "OS volume" in lines[1]
        assert prov.pod(INST_ID)["hostname"] == "comfy-base"
        assert prov.pod(OTHER_OS)["kind"] == "volume"
        with pytest.raises(podctl.PodctlError):
            prov.pod("nope")
        assert "OS volume" in prov.status_text(prov.pod(OTHER_OS)) and "podctl start %s" % OTHER_OS in prov.status_text(prov.pod(OTHER_OS))
    finally:
        fake.close()


def test_unit_verda_facts_normalise_the_status_vocabulary_and_address_waits_for_running_with_an_ip():
    podctl = _load()
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api("http://127.0.0.1:1", token="t"))          # never called
    for raw, want in [("running", "running"), ("offline", "stopped"), ("notfound", "gone"), ("deleting", "gone"),
                      ("provisioning", "transitional"), ("ordered", "transitional"), ("new", "transitional"), ("validating", "transitional"),
                      ("no_capacity", "transitional"), ("installation_failed", "transitional"), ("error", "transitional"),
                      # a live account answers "discontinued" for a DELETED instance (the record outlives the
                      # delete) and for a zero-balance kill. Either way it is gone, not on its way somewhere:
                      # as "transitional" it made `start` pass a deleted machine straight through.
                      ("discontinued", "gone"), ("unknown", "transitional"), (None, "transitional")]:
        assert prov.facts(_instance(status=raw))["status"] == want, raw
    f = prov.facts(_instance())
    assert (f["host"], f["port"], f["user"], f["image"], f["gpu"]) == ("127.0.0.1", 22, "root", IMAGE, "RTX PRO 6000")
    assert f["volumes"] == [OS_VOL, DATA_VOL] and f["name"] == "comfy-base"
    assert prov.address(_instance(status="provisioning", ip=None)) is None
    assert prov.address(_instance(status="running", ip=None)) is None
    assert prov.address(_instance(status="offline", ip="127.0.0.1")) is None      # a stale ip on an offline machine
    a = prov.address(_instance())
    assert (a.host, a.port, a.user) == ("127.0.0.1", 22, "root")
    text = prov.status_text(_instance())
    for needle in ("ssh        root@127.0.0.1:22", OS_VOL, DATA_VOL, "FIN-01", ITYPE, "1.9", IMAGE, "firewall"):
        assert needle in text, needle
    rec = dict(_volume(OS_VOL, "comfy-base-os", True, status="detached", instance_id=None), kind="volume", status="detached-volume")
    vf = prov.facts(rec)
    assert vf["status"] == "stopped" and vf["kind"] == "volume" and vf["volumes"] == [OS_VOL] and prov.address(rec) is None


# ---------------------------------------------------------------- stop, start, restart

def test_unit_verda_stop_deletes_the_instance_keeping_every_volume_and_names_them(tmp_path):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False)])
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.stop(INST_ID, wait=True)
        assert fake.calls("PUT") == [("PUT", "/instances", {"id": INST_ID, "action": "delete", "volume_ids": []})]
        assert INST_ID not in fake.instances
        assert fake.volumes[OS_VOL]["status"] == "detached" and fake.volumes[DATA_VOL]["status"] == "detached"
        assert OS_VOL in out and DATA_VOL in out and "kept" in out and "podctl start %s" % OS_VOL in out
        assert [p["id"] for p in prov.pods()] == [OS_VOL, DATA_VOL]           # the stopped machine stays visible
        assert "nothing to stop" in prov.stop(OS_VOL)                         # a volume is already the stopped form
        with pytest.raises(podctl.PodctlError):
            prov.stop("nope")
    finally:
        fake.close()


def test_unit_verda_start_from_a_detached_os_volume_redeploys_with_its_data_volume_and_the_mac_key(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(volumes=[_volume(OS_VOL, "comfy-base-os", True, status="detached", instance_id=None),
                              _volume(DATA_VOL, "comfy-base-data", False, status="detached", instance_id=None),
                              _volume(OTHER_DATA, "other-box-data", False, status="detached", instance_id=None),
                              _volume("eeee", "comfy-base-data", False, status="detached", instance_id=None, location="ICE-01")],
                     keys=[{"id": "key-9", "name": "laptop", "key": "ssh-ed25519 AAAAOTHER other@key", "fingerprint": "x"}])
    cfg = _cfg(tmp_path)
    banners = iter([False, True])
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: next(banners))
    try:
        prov = _prov(podctl, fake, tmp_path, env={"VERDA_INSTANCE_TYPE": ITYPE})
        out = prov.start(OS_VOL, wait=True, ssh_config=cfg)
        (_, _, post), = fake.calls("POST", "/instances")
        assert post["image"] == OS_VOL and post["instance_type"] == ITYPE and post["location_code"] == "FIN-01"
        assert post["existing_volumes"] == [DATA_VOL]                          # same name prefix, same location; not the others
        assert post["hostname"] == "comfy-base" and "startup_script_id" not in post   # none registered yet: nothing to pass
        keyposts = fake.calls("POST", "/sshkeys")
        assert len(keyposts) == 1 and keyposts[0][2]["key"] == MAC_KEY and post["ssh_key_ids"] == [fake.keys[-1]["id"]]
        new_id = next(i for i in fake.instances if i.startswith("inst-new-"))
        assert fake.instances[new_id]["status"] == "running" and fake.polls[new_id] >= 2   # it waited through provisioning
        assert fake.volumes[DATA_VOL]["instance_id"] == new_id
        text = cfg.read_text(encoding="utf-8")
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n  User root\n  IdentityFile %s\n" % prov.key_path in text
        assert "Host other\n  HostName 1.2.3.4\n  User me\n" in text            # the other block is untouched
        assert new_id in out and "127.0.0.1:22" in out and OS_VOL in out and DATA_VOL in out
        # a data volume is not startable; an unknown id is not either
        with pytest.raises(podctl.PodctlError) as ei:
            prov.start(OTHER_DATA, wait=False)
        assert "data volume" in str(ei.value)
        with pytest.raises(podctl.PodctlError):
            prov.start("nope", wait=False)
    finally:
        fake.close()


def test_unit_verda_start_needs_an_instance_type_and_starts_an_offline_instance_with_the_api(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance(status="offline", ip=None)],
                     volumes=[_volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    try:
        prov = _prov(podctl, fake, tmp_path, env={})
        with pytest.raises(podctl.PodctlError) as ei:
            prov.start(OTHER_OS, wait=False)
        assert "VERDA_INSTANCE_TYPE" in str(ei.value) and not fake.calls("POST")
        out = prov.start(INST_ID, wait=True, ssh_config=cfg)
        assert fake.calls("PUT") == [("PUT", "/instances", {"id": INST_ID, "action": "start"})]
        assert fake.instances[INST_ID]["status"] == "running" and "127.0.0.1:22" in out
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
    finally:
        fake.close()


def test_unit_verda_restart_is_shutdown_then_start_through_the_api_never_a_reboot_in_the_guest(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)

    def no_ssh(*a, **k):
        raise AssertionError("restart must not run anything in the guest")
    monkeypatch.setattr(podctl, "ssh_run", no_ssh)
    monkeypatch.setattr(podctl, "_ssh_hostname", no_ssh)
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.restart(INST_ID, wait=True, ssh_config=cfg)
        assert [b["action"] for _, _, b in fake.calls("PUT", "/instances")] == ["shutdown", "start"]
        assert fake.instances[INST_ID]["status"] == "running" and "127.0.0.1:22" in out
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
        assert not any("reboot" in json.dumps(b or {}) for _, _, b in fake.requests)
        with pytest.raises(podctl.PodctlError):
            prov.restart(OS_VOL, wait=False)
    finally:
        fake.close()


# ---------------------------------------------------------------- ensure

def test_unit_verda_ensure_registers_the_key_and_the_script_once_writes_the_block_and_runs_the_ensure_pass(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance(), _instance(id="off-1", hostname="asleep", status="offline", ip=None)],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []

    def fake_ssh_run(cmd, env=None, timeout=None):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="2026-09-16T10:00:00Z comfy-base-startup[ensure]: READY disk=/dev/vdb mounted=yes unit=active boot.sh=absent host.env=written\n")
    monkeypatch.setattr(podctl, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert out == "reachable: 127.0.0.1:22 (hostname comfy-base)"
        posts = fake.calls("POST")
        assert [p for _, p, _ in posts] == ["/sshkeys", "/scripts"]
        assert posts[0][2]["key"] == MAC_KEY
        assert posts[1][2]["name"] == "comfy-base-startup" and posts[1][2]["script"] == STARTUP.read_text(encoding="utf-8")
        assert len(calls) == 1
        cmd = calls[0]
        assert "cat > /root/comfy-base-startup.sh <<'COMFY_BASE_STARTUP_EOF'\n" in cmd and cmd.rstrip().endswith("bash /root/comfy-base-startup.sh --ensure")
        assert "RequiresMountsFor=$req" in cmd and 'req="/workspace"' in cmd and "BASE_HOST=verda" in cmd
        assert any("READY disk=/dev/vdb" in l for l in logs)
        assert MAC_KEY.split()[1] not in " ".join(l for l in logs if "registering" in l)  # the key body is never logged
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n  User root\n" in cfg.read_text(encoding="utf-8")
        # again: nothing registered twice, the block rewritten, the ensure pass run again (it is idempotent on the machine)
        n = len(fake.requests)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert not [1 for m, _, _ in fake.requests[n:] if m == "POST"] and len(calls) == 2
        # a detached volume and an offline instance are not ensurable: say what to do instead
        for ident in (OTHER_OS, "off-1"):
            with pytest.raises(podctl.PodctlError) as ei:
                prov.ensure(ident, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
            assert "podctl start %s" % ident in str(ei.value)
        # a failing --ensure pass is the error, with the script's tail
        monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: types.SimpleNamespace(returncode=1, stdout="", stderr="no data disk appeared"))
        with pytest.raises(podctl.PodctlError) as ei:
            prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert "startup.sh --ensure failed" in str(ei.value) and "no data disk appeared" in str(ei.value)
    finally:
        fake.close()


# ---------------------------------------------------------------- deploy --like

def test_unit_verda_deploy_like_refuses_a_running_source_and_moves_the_data_volumes_of_an_offline_one(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False)],
                     keys=[{"id": "key-1", "name": "mac", "key": MAC_KEY + "-other-comment", "fingerprint": "x"}],
                     scripts=[{"id": "script-7", "name": "comfy-base-startup", "script": STARTUP.read_text(encoding="utf-8")}])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    said = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        with pytest.raises(podctl.PodctlError) as ei:
            prov.deploy_like(INST_ID, wait=False, say=said.append)
        assert "offline" in str(ei.value) and not fake.calls("PUT") and not fake.calls("POST")
        fake.instances[INST_ID].update(status="offline", ip=None)
        new_id = prov.deploy_like(INST_ID, name="comfy-base-2", wait=True, ssh_config=cfg, say=said.append)
        seq = [(m, p, b) for m, p, b in fake.requests if m in ("PUT", "POST")]
        assert seq[0] == ("PUT", "/volumes", {"id": DATA_VOL, "action": "detach", "instance_id": INST_ID})
        assert seq[1][:2] == ("POST", "/instances") and len(seq) == 2         # the key and the script were already registered
        body = seq[1][2]
        assert body["existing_volumes"] == [DATA_VOL] and body["image"] == IMAGE and body["instance_type"] == ITYPE
        assert body["location_code"] == "FIN-01" and body["ssh_key_ids"] == ["key-1"] and "startup_script_id" not in body   # 3.0.0: ensure configures it over ssh
        assert body["hostname"] == "comfy-base-2" and body["is_spot"] is False
        assert new_id in fake.instances and fake.instances[new_id]["status"] == "running"
        assert fake.volumes[DATA_VOL]["instance_id"] == new_id
        assert said[0].startswith("deployed %s" % new_id) and DATA_VOL in said[0]
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
        with pytest.raises(podctl.PodctlError):
            prov.deploy_like("nope", wait=False, say=said.append)
    finally:
        fake.close()


# ---------------------------------------------------------------- the provider's shape

def test_unit_verda_provider_shape_host_env_and_lazy_construction(monkeypatch, tmp_path):
    podctl = _load()
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api("http://127.0.0.1:1", token="t"))
    assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\n"
    # experimental came off in 2.5.3: the host ran end to end on a live account, so the driver no longer warns
    assert prov.experimental is False and prov.name == "verda" and prov.ssh_user == "root" and prov.ssh_alias == "verda"
    assert prov.volume_root == "/workspace" and str(prov.key_path).endswith("/.ssh/verda_comfyui")
    assert prov.record_volume({"kind": "volume", "id": OS_VOL}) is None      # a volume is not a machine
    assert isinstance(prov, podctl.Provider)
    # the driver picks the one Provider subclass in the module
    subclasses = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, podctl.Provider) and v is not podctl.Provider]
    assert subclasses == [mod.VerdaProvider]

    def boom(*a, **k):
        raise AssertionError("constructing the provider must open no socket")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    p2 = podctl.provider_for("verda", env={"VERDA_CLIENT_ID": "x", "VERDA_CLIENT_SECRET": "y"})
    assert isinstance(p2, mod.VerdaProvider)
    # the id helpers accept every shape the docs allow
    assert mod.new_id("abc") == "abc" and mod.new_id({"id": "d"}) == "d" and mod.new_id(["e"]) == "e" and mod.new_id([{"id": "f"}]) == "f"
    with pytest.raises(podctl.PodctlError):
        mod.new_id({})
    assert mod.volume_stem("comfy-base-os") == mod.volume_stem("OS-comfy-base") == mod.volume_stem("comfy-base-data") == "comfy-base"
    assert mod.stems_match("comfy-base-os", "comfy-base-data") and not mod.stems_match("comfy-base-os", "other-box-data")
    assert mod.key_matches("ssh-ed25519 AAAA x@y", "ssh-ed25519 AAAA other") and not mod.key_matches("ssh-ed25519 BBBB x", "ssh-ed25519 AAAA x")


def test_unit_verda_startup_script_parses_is_ascii_and_installs_the_unit_and_the_host_env():
    text = STARTUP.read_bytes()
    text.decode("ascii")                                                        # pure ASCII, or this raises
    r = subprocess.run(["bash", "-n", str(STARTUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    s = text.decode()
    for needle in ("comfy-base-boot", "RequiresMountsFor=$req", 'req="/workspace"', "BASE_HOST=verda", "BASE_VOLUME=/workspace",
                   "nofail,x-systemd.device-timeout=30", "mkfs.ext4", "comfy-volume", "/var/log/comfy-base-startup.log",
                   "/dev/vd[b-z]", "--ensure", "systemctl daemon-reload", "enable --now", "READY",
                   # 2.4.0: the shared library, and the two packages the Verda image ships without
                   "/mnt/comfy-library", "nconnect=16", "nfs-common", "python3-venv", "--library",
                   "COMFY_LIBRARY_SRC=", "BASE_LIBRARY=", "recall_library", "mount_library"):
        assert needle in s, needle
    assert not re.search(r"^\s*set -e", s, re.M)                                 # a partial boot still reaches READY
    readme = README.read_text(encoding="utf-8")
    # the README must now say it is PROVEN and answer the three questions the first live run was to settle
    assert "PROVEN on a live account" in readme and "EXPERIMENTAL" not in readme
    assert "The ssh user is `root`" in readme and "startup script does re-run" in readme and "`ip` populates" in readme
    assert "\u2014" not in readme and "\u2013" not in readme


# ---------------------------------------------------------------- 2.4.0: the shared library

def test_unit_verda_ensure_mounts_the_shared_library_and_records_it_in_host_env_and_volume_env(tmp_path, monkeypatch):
    """A shared volume attached to the machine is what makes a library, and the driver reads that back from the
    account rather than remembering it on the Mac. The NFS endpoint is the one thing the API may not carry, so it
    comes from --library; without it the library is NOT mounted and the run says so instead of pretending."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance()],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _shared()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []

    def fake_ssh_run(cmd, env=None, timeout=None):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="READY disk=/dev/vdb mounted=yes library=mounted unit=active\n")
    monkeypatch.setattr(podctl, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append, library=LIB_SRC)
        assert calls[-1].rstrip().endswith("bash /root/comfy-base-startup.sh --ensure --library %s" % LIB_SRC)
        assert any("shared library comfy-library" in l for l in logs)
        # host.env must carry it: podctl's write_host_env runs right after ensure and overwrites what the script wrote
        assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\nBASE_LIBRARY=/mnt/comfy-library\n"
        # the library's SIZE is recorded, because df on an NFS mount reads as an unmeasurable pool to the disk gate
        line = prov.record_volume(_instance())
        assert "500 GB" in line and "comfy-library" in line
        rec = next(c for c in calls if "volume.env" in c)
        assert "VOLUME_GB=%s" in rec and "500" in rec and LIB_VOL in rec

    finally:
        fake.close()

    # attached, but this record carries no usable endpoint: no --library, no BASE_LIBRARY, and a warning rather
    # than a mount nobody asked for
    fake2 = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _shared(target=None)])
    logs2, calls2 = [], []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: (calls2.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr="READY\n"))[1])
    try:
        prov2 = _prov(podctl, fake2, tmp_path)
        prov2.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs2.append)
        assert calls2[-1].rstrip().endswith("--ensure")
        assert any("NFS endpoint is not in the API record" in l for l in logs2)
        # host.env still NAMES the library, because the volume IS attached to this machine and every verb that
        # writes host.env must answer the same way. The base then says "recorded but is not a directory" and runs
        # without it, which is louder than a line quietly missing: podctl install used to drop this line and
        # silently un-configure a machine whose library was mounted.
        assert prov2.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\nBASE_LIBRARY=/mnt/comfy-library\n"
    finally:
        fake2.close()


def test_unit_verda_ensure_refuses_a_library_endpoint_when_no_shared_volume_is_attached(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True)])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: (calls.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr="READY\n"))[1])
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append, library=LIB_SRC)
        assert calls[-1].rstrip().endswith("--ensure")                      # never mounted: nothing to mount
        assert any("no shared volume is attached" in l for l in logs)
        assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\n"
        assert prov.record_volume(_instance()) is None                       # no library, nothing to record
    finally:
        fake.close()


def test_unit_verda_start_attaches_the_shared_library_by_type_and_location_never_by_name(tmp_path, monkeypatch):
    """The stem rule finds a machine's own data volume by name. It cannot find the library and must not: one
    library serves comfy-base, comfy-cc and comfy-mm, whose stems all differ. Type and location decide instead."""
    podctl = _load()
    fake = FakeVerda(volumes=[_volume(OS_VOL, "comfy-base-os", True, status="detached", instance_id=None),
                              _volume(DATA_VOL, "comfy-base-data", False, status="detached", instance_id=None),
                              _shared(instances=("some-other-machine",)),                      # still attached elsewhere
                              _shared(vid="ffff", name="other-region-library", instances=(), location="ICE-01")])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    try:
        prov = _prov(podctl, fake, tmp_path, env={"VERDA_INSTANCE_TYPE": ITYPE})
        prov.start(OS_VOL, wait=True, ssh_config=cfg)
        (_, _, post), = fake.calls("POST", "/instances")
        assert post["existing_volumes"] == [DATA_VOL, LIB_VOL]      # its own data volume, and the library of THIS location
        assert "ffff" not in post["existing_volumes"]               # a library in another location is another library
    finally:
        fake.close()


def test_unit_verda_deploy_like_carries_the_shared_library_without_detaching_it(tmp_path, monkeypatch):
    """A block volume must be detached from the source before it can move; a shared one attaches to several
    machines at once, so detaching it would take the library away from every other machine."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance(status="offline", ip=None, volume_ids=[DATA_VOL, LIB_VOL])],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _shared()],
                     keys=[{"id": "key-1", "name": "mac", "key": MAC_KEY, "fingerprint": "x"}])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.deploy_like(INST_ID, name="comfy-base-2", wait=True, ssh_config=cfg, say=lambda s: None)
        detaches = [b for m, p, b in fake.requests if m == "PUT" and p == "/volumes" and b.get("action") == "detach"]
        assert [b["id"] for b in detaches] == [DATA_VOL]                      # the library was NOT detached
        (_, _, post), = fake.calls("POST", "/instances")
        assert post["existing_volumes"] == [DATA_VOL, LIB_VOL]
        assert fake.volumes[LIB_VOL]["status"] == "attached"
    finally:
        fake.close()


def test_unit_verda_shared_volume_helpers_read_every_shape_the_api_uses():
    podctl = _load()
    mod = _verda(podctl)
    assert mod.is_shared_volume({"type": "NVMe_Shared"}) and mod.is_shared_volume({"is_shared_fs": True, "type": "x"})
    assert mod.is_shared_volume({"type": "HDD_Shared"}) and mod.is_shared_volume({"type": "NVMe_Shared_Cluster"})
    assert not mod.is_shared_volume({"type": "NVMe"}) and not mod.is_shared_volume({}) and not mod.is_shared_volume(None)
    # the id lists the docs show: one instance_id, an instance_ids array, or an instances array of records
    assert mod.volume_instance_ids({"instance_id": "a"}) == ["a"]
    assert mod.volume_instance_ids({"instance_ids": ["b", "c"]}) == ["b", "c"]
    assert mod.volume_instance_ids({"instances": [{"id": "d"}, "e"]}) == ["d", "e"]
    assert mod.volume_instance_ids({}) == [] and mod.volume_instance_ids(None) == []
    # the endpoint is taken from the record only when it LOOKS like host:/export, never guessed
    # `target` is where a live account puts it, and the same key on a BLOCK volume is a device name
    assert mod.library_src_from({"target": LIB_SRC}) == LIB_SRC
    assert mod.library_src_from({"target": "vda"}) == ""
    assert mod.library_src_from({"nfs_path": LIB_SRC}) == LIB_SRC
    assert mod.library_src_from({"mount_point": "/mnt/somewhere"}) == ""
    assert mod.library_src_from({}) == "" and mod.library_src_from(None) == ""
    # the library's own name must not stem-match a machine, or start would tie it to one machine
    assert not mod.stems_match("comfy-library", "comfy-base-os")


def test_unit_verda_stop_wait_ends_when_the_instance_leaves_the_list_not_when_its_status_changes(tmp_path, monkeypatch):
    """MEASURED on a live account: a deleted Verda instance is not 404 and never says notfound. GET /instances/{id}
    keeps answering the full record with status discontinued and volume_ids [], so a probe that waits for None or
    for a status gave up after 300 s on a delete that had already completed. Absence from GET /instances is the
    signal, and it has to be, because Verda says discontinued for a zero-balance kill as well as for a delete."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance()],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False)])

    # Verda's real behaviour: PUT delete empties volume_ids and sets discontinued, and the record stays readable
    fake.listed = set(fake.instances)

    def delete_like_verda(inst_id):
        fake.instances[inst_id].update(status="discontinued", volume_ids=[], ip=None)
        fake.listed.discard(inst_id)                 # gone from the LIST, still readable by id
    fake.on_delete = delete_like_verda
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.stop(INST_ID, wait=True)
        assert "instance deleted, volumes kept" in out and OS_VOL in out
        assert fake.instances[INST_ID]["status"] == "discontinued"        # still readable by id, as Verda leaves it
        # and the word reads as gone, so start refuses instead of passing it through as transitional
        assert podctl_norm(podctl, "discontinued") == "gone"
        with pytest.raises(podctl.PodctlError) as ei:
            prov.start(INST_ID, wait=False)
        assert "nothing to start" in str(ei.value)
        # stopping it again is a no-op that says so rather than deleting a second time
        assert "already discontinued" in prov.stop(INST_ID, wait=False)
    finally:
        fake.close()


def podctl_norm(podctl, raw):
    return _verda(podctl).norm_status(raw)


def test_unit_verda_the_library_is_listed_and_mounted_without_being_told_its_endpoint(tmp_path, monkeypatch):
    """MEASURED on a live account: a shared volume's own record carries `target`, the NFS endpoint, so ensure
    needs no --library; and an ATTACHED one reads "exported", which _detached() would hide, so pods() lists
    shared volumes whatever their status or the library is invisible exactly while three machines depend on it."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance()],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _shared()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: (calls.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr="READY\n"))[1])
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=lambda s: None)   # no --library given
        assert calls[-1].rstrip().endswith("--ensure --library nfs.fin-01.datacrunch.io:/comfy-library-abc")
        assert prov.host_env().endswith("BASE_LIBRARY=/mnt/comfy-library\n")
        # an attached (exported) library still shows in pods, beside the instance
        rows = prov.pods()
        assert any(is_lib(r) for r in rows), rows
        assert prov.status_text(prov.pod(LIB_VOL)).splitlines()[1].startswith("kind       shared library")
    finally:
        fake.close()


def is_lib(rec):
    return str(rec.get("id")) == LIB_VOL


def test_unit_verda_workspace_shared_makes_the_store_the_root_and_records_it(tmp_path, monkeypatch):
    """2.5.0, the RunPod shape on a VM host: one network store holds the base, ComfyUI, the venv, the packages and
    the models, so the machine is disposable and carries no data volume. The endpoint is the same one the library
    uses, resolved the same way, so it is never typed twice; and BASE_VOLUME_SHARED is recorded because two things
    follow from it, per-machine user/ and temp/ and a lock before an install."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _shared()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: (calls.append(cmd), types.SimpleNamespace(returncode=0, stdout="", stderr="READY\n"))[1])
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append, workspace_shared=True)
        src = "nfs.fin-01.datacrunch.io:/comfy-library-abc"
        assert calls[-1].rstrip().endswith("--ensure --library %s --workspace %s" % (src, src))
        assert any("needs no data volume" in l for l in logs)
        assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\nBASE_VOLUME_SHARED=1\n"
    finally:
        fake.close()

    # no shared volume attached: refuse rather than mount /workspace from nothing and hang the boot for 600 s
    fake2 = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True)])
    try:
        prov2 = _prov(podctl, fake2, tmp_path)
        with pytest.raises(podctl.PodctlError) as ei:
            prov2.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=lambda s: None, workspace_shared=True)
        assert "attach it" in str(ei.value)
    finally:
        fake2.close()


def test_unit_verda_startup_script_can_mount_the_share_as_the_workspace():
    s = STARTUP.read_bytes().decode("ascii")
    for needle in ("--workspace", "WORKSPACE_SRC", "mount_workspace_share", "recall_workspace", "BASE_VOLUME_SHARED=1"):
        assert needle in s, needle
    # with a shared workspace there is NO data disk to find, so the 600 s hunt must be skipped entirely
    assert 'if [ -n "$WORKSPACE_SRC" ]; then\n    mount_workspace_share' in s
    # fstab is the only record of it, because host.env lives ON the share it would have to name
    assert "recalled from fstab" in s


def test_unit_workspace_share_refuses_to_claim_a_mount_it_did_not_make():
    """MEASURED on a live machine: ensure --workspace-shared rewrote the fstab line correctly, but /workspace was
    already mounted from the block device and mount(8) will not replace a mount under a running system. The script
    reported success anyway, so READY said mounted=yes while the machine was still on its own disk. A report line
    that lies is worse than a failure: it now says REBOOT and fails the step, because the fstab IS correct."""
    s = STARTUP.read_bytes().decode("ascii")
    i = s.index("mount_workspace_share()")
    body = s[i:s.index("\nmount_data_disk()", i)]
    assert "NOT the share" in body and "REBOOT to complete the switch" in body, body[:400]
    # the success branch must be the ONLY one that returns 0 from an existing mount
    assert 'if [ "$src" = "$WORKSPACE_SRC" ]; then' in body, body[:400]


def test_unit_a_shared_workspace_ignores_a_separate_library_and_clears_its_mount():
    """2.5.6, MEASURED as a FEEDBACK LOOP rather than a stale field. host.env lives on the workspace, so once the
    workspace IS the store that file is shared: recall_library read ANOTHER machine's COMFY_LIBRARY_SRC back out
    of it, mounted the store a second time at /mnt/comfy-library, and overwrote the host.env the same run had just
    written. Both live machines showed the double mount despite being built differently, which is what gave it
    away. A shared workspace and a separate library are ALTERNATIVES, as lib/00-env.sh has said since 2.5.2."""
    s = STARTUP.read_bytes().decode("ascii")
    body = s[s.index("recall_library()"):s.index("\nmount_library()")]
    assert 'if [ -n "$WORKSPACE_SRC" ]; then' in body and 'LIBRARY=""' in body, body[:500]
    # and the stale fstab line must be REMOVED, since the OS volume carries it across a delete and recreate
    ws = s[s.index("mount_workspace_share()"):s.index("\nmount_data_disk()")]
    assert "removed the separate-library fstab line" in ws and "umount" in ws, ws[:600]


def test_unit_a_machine_with_no_data_volume_does_not_block_for_ten_minutes():
    """2.5.6: a machine with no data volume is the normal case now. The old code waited DISK_WAIT=600 for a disk
    nobody had attached, because the registered startup script recalls its workspace from an fstab that a fresh OS
    volume does not have. Waiting is right when a disk is attached and slow; pointless when there is none at all."""
    s = STARTUP.read_bytes().decode("ascii")
    body = s[s.index("find_data_disk()"):s.index("\nmount_workspace_share()")]
    assert 'DISK_PROBE:-30' in body and '-z "$(candidates)"' in body, body[:600]
    assert "--workspace-shared" in body, "it must name the fix rather than only the symptom"
    assert "$DISK_WAIT" in body, "a disk that IS attached and slow must still get the full wait"


# ---------------------------------------------------------------- 3.0.0: the registered startup script is kept at its newest

def test_unit_verda_ensure_replaces_a_stale_registered_script_and_start_never_attaches_one(tmp_path):
    """The live account on 2026-09-25 still had the pre-2.4.0 startup.sh registered under the same name. It was matched by
    name, attached to every redeploy, and its host.env writer dropped BASE_VOLUME_SHARED=1 for the whole store."""
    podctl = _load()
    fake = FakeVerda(scripts=[{"id": "old-1", "name": "comfy-base-startup", "script": "#!/bin/bash\necho old\n"}])
    said = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        assert prov.script_id(register=False, log=said.append) is None and "NOT attached" in said[-1]
        sid = prov.script_id(register=True, log=said.append)
        posts = fake.calls("POST", "/scripts"); dels = fake.calls("DELETE")
        assert sid and posts and dels and dels[0][1] == "/scripts/old-1"
        assert fake.requests.index(("POST", "/scripts", posts[0][2])) < fake.requests.index(dels[0])   # the new one first
        assert [x["id"] for x in fake.scripts] == [sid] and fake.scripts[0]["script"] == STARTUP.read_text(encoding="utf-8")
        assert prov.script_id(register=False) == sid                                                   # current: attached
    finally:
        fake.close()


def test_unit_verda_a_failed_delete_leaves_the_stale_script_registered_but_unused(tmp_path):
    podctl = _load()
    fake = FakeVerda(scripts=[{"id": "old-1", "name": "comfy-base-startup", "script": "#!/bin/bash\necho old\n"}])
    fake.delete_fails = True
    said = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        sid = prov.script_id(register=True, log=said.append)
        assert sid and sid != "old-1" and any("stays registered and unused" in m for m in said)
        assert prov.script_id(register=False) == sid                                                   # matched by TEXT, never the stale one
    finally:
        fake.close()


def test_unit_verda_host_env_reads_whether_the_workspace_is_the_store(tmp_path, monkeypatch):
    """Measured on FIN-03 (2026-09-25): host.env was right after `ensure --workspace-shared` and wrong 23 s later, because
    `podctl install` wrote it again from a fresh provider that only knew a shared volume was ATTACHED: it recorded a
    library at /mnt/comfy-library that does not exist and dropped BASE_VOLUME_SHARED=1 for the whole store. It asks
    the machine now: an NFS mount AT /workspace is the shared root."""
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _shared()])
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov._last_pod = INST_ID
        monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: types.SimpleNamespace(returncode=0, stdout="nfs4\n" if "findmnt" in cmd else "", stderr=""))
        assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\nBASE_VOLUME_SHARED=1\n"
        prov2 = _prov(podctl, fake, tmp_path)
        prov2._last_pod = INST_ID
        monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: types.SimpleNamespace(returncode=0, stdout="ext4\n", stderr=""))
        assert prov2.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\nBASE_LIBRARY=/mnt/comfy-library\n"   # a library beside a local root
    finally:
        fake.close()


# ---------------------------------------------------------------- 3.1.0: a buyer's machine

def test_unit_verda_balance_and_availability_read_the_api(tmp_path):
    podctl = _load(); fake = FakeVerda()
    try:
        prov = _prov(podctl, fake, tmp_path)
        assert prov.balance() == {"amount": 42.5, "currency": "usd"}
        assert prov.availability(ITYPE) == ["FIN-03"] and prov.availability("1H200.30V") == []
        assert fake.calls("GET", "/instance-availability?is_spot=false")
        prov.availability(ITYPE, spot=True)
        assert fake.calls("GET", "/instance-availability?is_spot=true")
    finally:
        fake.close()


def test_unit_verda_create_machine_makes_its_own_disks_and_attaches_no_startup_script(tmp_path, monkeypatch):
    podctl = _load(); fake = FakeVerda(provision_polls=1)
    monkeypatch.setattr(podctl, "ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH")
    try:
        prov = _prov(podctl, fake, tmp_path)
        prov.banner = lambda host, port=22: "SSH-2.0-OpenSSH"
        pod = prov.create_machine("buyer-one", ITYPE, "FIN-03", 250, ssh_config=_cfg(tmp_path), say=lambda *a: None)
        (_, _, vbody), = fake.calls("POST", "/volumes")
        assert vbody == {"type": "NVMe", "location_code": "FIN-03", "size": 250, "name": "buyer-one-data"}
        (_, _, ibody), = fake.calls("POST", "/instances")
        assert ibody["os_volume"] == {"name": "buyer-one-os", "size": 100} and ibody["image"] == "24.04.cuda13.2.docker"
        assert ibody["location_code"] == "FIN-03" and ibody["instance_type"] == ITYPE and len(ibody["existing_volumes"]) == 1
        assert "startup_script_id" not in ibody and ibody["ssh_key_ids"]
        assert sorted(prov.machine_volumes(pod)) == sorted([fake.instances[pod]["os_volume_id"]] + ibody["existing_volumes"])
    finally:
        fake.close()


def test_unit_verda_a_sold_out_create_takes_its_new_disk_with_it(tmp_path):
    podctl = _load(); fake = FakeVerda(); fake.sold_out = True
    mod = _verda(podctl)
    try:
        prov = _prov(podctl, fake, tmp_path)
        with pytest.raises(mod.HttpError) as e:
            prov.create_machine("buyer-two", ITYPE, "FIN-02", 100, wait=False, say=lambda *a: None)
        assert e.value.status == 503
        (_, _, vbody), = fake.calls("POST", "/volumes")
        dels = [b for m, p, b in fake.calls("PUT", "/volumes") if b.get("action") == "delete"]
        assert len(dels) == 1 and dels[0]["is_permanent"] is True and not fake.volumes
    finally:
        fake.close()


def test_unit_verda_delete_volumes_goes_to_the_trash_one_call_each(tmp_path):
    podctl = _load()
    fake = FakeVerda(volumes=[_volume("v-os", "buyer-os", True, status="detached", instance_id=None),
                              _volume("v-data", "buyer-data", False, status="detached", instance_id=None)])
    try:
        prov = _prov(podctl, fake, tmp_path)
        assert sorted(prov.machine_volumes("v-os")) == ["v-data", "v-os"]
        prov.delete_volumes(["v-os", "v-data"])
        dels = [b for m, p, b in fake.calls("PUT", "/volumes")]
        assert dels == [{"id": "v-os", "action": "delete", "is_permanent": False}, {"id": "v-data", "action": "delete", "is_permanent": False}]
        assert {v["status"] for v in fake.volumes.values()} == {"deleted"}
    finally:
        fake.close()
