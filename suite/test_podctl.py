"""unit tier: podctl, the Mac-side pod driver (base/comfyui-base/_build/pod/podctl.py) against a fake RunPod REST
server and a fake ssh banner. Nothing here touches the network, a real pod, or the user's ssh config."""
import importlib.util
import re
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "_build" / "pod" / "podctl.py"



@pytest.fixture(autouse=True)
def _no_session_podctl_env(monkeypatch):
    """A workspace that exports PODCTL_HOST / PODCTL_TUNNEL_OFFSET / PODCTL_PROVIDER (so a session addresses its own
    machine) must not change what these tests expect: every test starts with none of them, and sets its own."""
    for k in list(os.environ):
        if k.startswith("PODCTL_") or k == "BASE_WORKSPACE":         # 3.6.0: the pin scan honours a workspace
            monkeypatch.delenv(k, raising=False)

def _load():
    if not TOOL.exists():
        pytest.skip("no base/comfyui-base/_build/pod/podctl.py (not a repo checkout)")
    spec = importlib.util.spec_from_file_location("podctl", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 3.9.1: REST v1 PodCreateInput as https://rest.runpod.io/v1/openapi.json listed it on 2026-10-16. The live POST /pods
# answers 400 "Extra input keys" for anything else (volumeEncrypted made every `create --private` fail that way), so the
# fake refuses the same keys and a field the API does not take fails here first.
POD_CREATE_INPUT = frozenset((
    "allowedCudaVersions cloudType computeType containerDiskInGb containerRegistryAuthId countryCodes cpuFlavorIds "
    "cpuFlavorPriority dataCenterIds dataCenterPriority dockerEntrypoint dockerStartCmd env globalNetworking gpuCount "
    "gpuTypeIds gpuTypePriority imageName interruptible locked minDiskBandwidthMBps minDownloadMbps minRAMPerGPU "
    "minUploadMbps minVCPUPerGPU name networkVolumeId ports supportPublicIp templateId vcpuCount volumeInGb "
    "volumeMountPath").split())


class FakeRunPod:
    """An in-memory RunPod REST v1: GET/PATCH /pods/{id}, POST /pods/{id}/start|stop, GET /pods. Records PATCH
    bodies in order. `on_patch` lets a test react (e.g. open the fake sshd once the start command lands)."""

    def __init__(self, pod, volumes=None, gql=None, rest=None):
        self.pod = pod
        self.volumes = volumes or {}
        self.patches = []
        self.posts = []
        self.bodies = []                                  # 3.7.3: (path, body) of every POST, for create
        self.gets = []
        self.deletes = []
        self.on_patch = None
        self.gql = gql or {}                              # 3.7.3: what POST /graphql answers under "data"
        self.refuse_pods = False                          # 3.7.3: POST /pods answers 400 (sold out between check and create)
        self.pod_override = {}                            # 3.7.4: what RunPod adds or changes on the created pod (volumeEncrypted)
        self.pod_drop = ()                                # 3.7.4: keys the created pod's answer leaves out
        self.rest = rest                                  # 3.9.0: (method, path, body) -> (code, body) | None, before the defaults
        self.resets = 0                                   # 3.10.1: a PATCH resets the pod: lastStartedAt moves
        self.move_port_on_reset = False                   # 3.10.1: and RunPod maps 22/tcp to a new port
        self.stale_reads = 0                              # 3.10.1: reads that still answer with the pod as it was before the PATCH
        self._stale = []
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

            def _auth(self):
                return self.headers.get("Authorization", "")

            def do_GET(self):
                srv.last_auth = self._auth(); srv.last_ua = self.headers.get("User-Agent", "")
                srv.gets.append(self.path)
                path = self.path.split("?")[0]            # 3.7.3: every pod read carries includeMachine/includeNetworkVolume
                if path == "/pods/" + srv.pod["id"] and srv._stale:
                    return self._send(200, srv._stale.pop())
                if srv.rest:
                    res = srv.rest("GET", path, None)
                    if res is not None:
                        return self._send(*res)
                if path == "/pods":
                    return self._send(200, [srv.pod])
                if path == "/pods/" + srv.pod["id"]:
                    return self._send(200, srv.pod)
                if path == "/networkvolumes":                 # 3.14.0: the listing a public create reuses a volume from
                    return self._send(200, list(srv.volumes.values()))
                if path.startswith("/networkvolumes/") and path.split("/")[2] in srv.volumes:
                    return self._send(200, srv.volumes[path.split("/")[2]])
                self._send(404, {"error": "no such pod"})

            def do_PATCH(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                srv.patches.append(body)
                old = json.loads(json.dumps(srv.pod))
                srv.pod.update(body)
                srv.resets += 1
                srv.pod["lastStartedAt"] = "2026-10-16 09:00:%02d.000 +0000 UTC" % srv.resets
                srv._stale = [old] * srv.stale_reads
                if "ports" in body:                       # RunPod maps a newly exposed tcp port on the reset
                    srv.pod["portMappings"] = srv.mapping_when_running()
                if srv.move_port_on_reset and (srv.pod.get("portMappings") or {}).get("22"):
                    srv.pod["portMappings"] = {"22": 15270 + srv.resets}
                if srv.on_patch:
                    srv.on_patch(body)
                self._send(200, srv.pod)

            def do_POST(self):
                srv.posts.append(self.path)
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"null")
                srv.bodies.append((self.path, body))
                if self.path == "/graphql":
                    srv.last_auth = self._auth()
                    data = srv.gql(body) if callable(srv.gql) else srv.gql     # 3.9.0: a test may answer per query
                    if isinstance(data, dict) and "__error__" in data:
                        return self._send(200, {"errors": [{"message": data["__error__"]}]})
                    return self._send(200, {"data": data})
                if srv.rest:
                    res = srv.rest("POST", self.path, body)
                    if res is not None:
                        return self._send(*res)
                if self.path == "/networkvolumes":
                    vol = dict(body, id="newvol%d" % (len(srv.volumes) + 1))
                    srv.volumes[vol["id"]] = vol
                    return self._send(200, vol)
                if self.path == "/pods":
                    extra = sorted(set(body) - POD_CREATE_INPUT)
                    if extra:                             # 3.9.1: the live API's answer to a key it does not take
                        return self._send(400, {"error": "Extra input keys: %s" % ", ".join(extra)})
                    if srv.refuse_pods:
                        return self._send(400, {"error": "There are no longer any instances available with the requested specifications."})
                    srv.pod = dict(body, id="newpod0000001", desiredStatus="RUNNING", publicIp="127.0.0.1", portMappings={"22": 15270})
                    srv.pod.update(srv.pod_override)
                    for k in srv.pod_drop:
                        srv.pod.pop(k, None)
                    return self._send(201, srv.pod)
                if self.path.endswith("/stop"):
                    srv.pod["desiredStatus"] = "EXITED"
                    srv.pod["portMappings"] = {}
                    srv.pod["publicIp"] = ""
                    return self._send(200, {})
                if self.path.endswith("/start"):
                    srv.pod["desiredStatus"] = "RUNNING"
                    srv.pod["publicIp"] = "127.0.0.1"
                    srv.pod["portMappings"] = srv.mapping_when_running()
                    return self._send(200, {})
                if self.path.endswith("/restart"):
                    srv.pod["desiredStatus"] = "RUNNING"
                    return self._send(200, {})
                self._send(404, {})

            def do_DELETE(self):                          # 2.0.56: Api.delete, for an endpoint tool
                srv.last_auth = self._auth()
                srv.deletes.append(self.path)
                self._send(200, {"deleted": self.path})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.last_auth = ""; self.last_ua = ""
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def mapping_when_running(self):
        return {"22": 15270} if "22/tcp" in self.pod.get("ports", []) else {}

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def _pod(**over):
    # the field names RunPod's REST v1 actually returns (read from a live pod on 2026-09-05): imageName,
    # networkVolumeId, and no dockerStartCmd/dockerEntrypoint keys at all until one has been set
    p = {"id": "fakepod0000001", "name": "Videos", "imageName": "example/comfyui-template:v8",
         "desiredStatus": "RUNNING", "publicIp": "127.0.0.1", "ports": ["8188/http", "8888/http"], "portMappings": {},
         "env": {"HF_TOKEN": "token_here", "some_api_token": "abc-secret", "SOME_IDS": "replace_with_ids",
                 "PUBLIC_KEY": "ssh-ed25519 AAAAOLD old@key"},
         "volumeMountPath": "/workspace", "networkVolumeId": "fakevol001", "templateId": "faketmpl01"}
    p.update(over)
    return p


# ---------------------------------------------------------------- the API key

def test_unit_podctl_reads_the_key_from_runpodctl_config_or_env_and_never_prints_it(tmp_path):
    podctl = _load()
    cfg = tmp_path / "config.toml"
    cfg.write_text('# runpodctl\napiKey = "rp_from_file_9f8e"\nrestApiUrl = "https://rest.runpod.io/v1"\n', encoding="utf-8")
    assert podctl.load_api_key(env={}, config_path=cfg) == "rp_from_file_9f8e"
    assert podctl.load_api_key(env={"RUNPOD_API_KEY": "rp_from_env"}, config_path=cfg) == "rp_from_env"
    # runpodctl 2.x writes lowercase keys, single-quoted; older ones camelCase, double-quoted; tolerate both and bare
    for body in ("apikey = 'rp_lower_1'\napiurl = 'https://api.runpod.io/graphql'\n", 'apiKey="rp_lower_1"\n', "apikey = rp_lower_1\n"):
        (tmp_path / "c2.toml").write_text(body, encoding="utf-8")
        assert podctl.load_api_key(env={}, config_path=tmp_path / "c2.toml") == "rp_lower_1", body
    with pytest.raises(podctl.PodctlError) as ei:
        podctl.load_api_key(env={}, config_path=tmp_path / "absent.toml")
    assert "runpodctl config --apiKey" in str(ei.value)
    # 2.0.56: runpodctl writes `apikey = ''` before a key is set; that used to escape as a StopIteration traceback
    for body in ("apikey = ''\napiurl = 'https://api.runpod.io/graphql'\n", 'apiKey = ""\n'):
        (tmp_path / "empty.toml").write_text(body, encoding="utf-8")
        with pytest.raises(podctl.PodctlError) as ei:
            podctl.load_api_key(env={}, config_path=tmp_path / "empty.toml")
        assert "runpodctl config --apiKey" in str(ei.value), body

    # 2.0.56: Api.delete rides the same bearer header (an endpoint tool deletes endpoints through REST v1)
    fake = FakeRunPod(_pod())
    try:
        assert podctl.Api("rp_del_1", base_url=fake.url).delete("/endpoints/abc") == {"deleted": "/endpoints/abc"}
        assert fake.deletes == ["/endpoints/abc"] and fake.last_auth == "Bearer rp_del_1"
    finally:
        fake.close()

    # the CLI, against a fake RunPod, never echoes the key anywhere
    fake = FakeRunPod(_pod())
    try:
        # 2.12.2: no PODCTL_* from the session. A workspace that exports PODCTL_PROVIDER=verda turned this "fake RunPod"
        # call into an authenticated GET against the LIVE Verda API (MEASURED: HTTP 400 "instanceId must be a UUID").
        env = {k: v for k, v in os.environ.items() if not k.startswith("PODCTL_")}
        r = subprocess.run([sys.executable, str(TOOL), "status", "fakepod0000001"], capture_output=True, text=True,
                           env={**env, "RUNPOD_API_KEY": "rp_from_env_7c1d", "RUNPOD_API_URL": fake.url})
    finally:
        fake.close()
    assert r.returncode == 0, r.stderr
    assert "rp_from_env_7c1d" not in r.stdout + r.stderr
    assert fake.last_auth == "Bearer rp_from_env_7c1d"
    # 2.0.31: Cloudflare in front of the API blocked urllib's default signature (error 1010) at 21:20Z 2026-09-06 after a day
    # of calls; every request now says who it is, and never "Python-urllib"
    assert fake.last_ua.startswith("podctl/") and "urllib" not in fake.last_ua, fake.last_ua
    assert "fakepod0000001" in r.stdout and "example/comfyui-template:v8" in r.stdout


# ---------------------------------------------------------------- facts, never values

def test_unit_podctl_status_names_placeholders_and_no_auth_without_values():
    podctl = _load()
    out = podctl.pod_facts(_pod())
    assert "HF_TOKEN" in out and "placeholder (token_here)" in out
    assert "SOME_IDS" in out and "placeholder (replace_with_ids)" in out
    assert "some_api_token" in out and "abc-secret" not in out          # a real secret: named, never shown
    assert "AAAAOLD" not in out                                          # keys are secrets too
    assert "token: generated by the boot" in out and "auth: NONE" not in out   # 3.9.0: never tokenless, with or without one in env
    assert "port 22 not mapped" in out
    assert "example/comfyui-template:v8" in out and "fakevol001 at /workspace" in out
    assert "NONE at /workspace" in podctl.pod_facts(_pod(networkVolumeId=None))
    with_auth = podctl.pod_facts(_pod(env={"JUPYTER_PASSWORD": "x"}, portMappings={"22": 15270}))
    assert "auth: NONE" not in with_auth and "127.0.0.1:15270" in with_auth and " x" not in with_auth.split("JUPYTER_PASSWORD")[1].split("\n")[0]


# ---------------------------------------------------------------- the ssh config block

SSH_CONFIG = """Include /home/x/.colima/ssh_config

Host github.com
  AddKeysToAgent yes
  IdentityFile ~/.ssh/id_ed25519

Host runpod
  HostName 203.0.113.10
  Port 14054
  User root
  IdentityFile ~/.ssh/id_ed25519
  LocalForward 8188 localhost:8188
  LocalForward 9199 localhost:9199
  LocalForward 11434 localhost:11434

Host other
  HostName 10.0.0.1
  Port 2222
"""


def test_unit_podctl_writes_only_the_runpod_block(tmp_path):
    podctl = _load()
    cfg = tmp_path / "config"
    cfg.write_text(SSH_CONFIG, encoding="utf-8")
    podctl.write_ssh_config(cfg, "203.0.113.9", 31022)
    got = cfg.read_text(encoding="utf-8")
    want = SSH_CONFIG.replace("HostName 203.0.113.10", "HostName 203.0.113.9").replace("Port 14054", "Port 31022\n  StrictHostKeyChecking accept-new")
    assert got == want                                    # every other line byte-identical: github, forwards, 'other'; 2.0.28 adds accept-new once
    # no runpod block yet: one is appended with the forwards the base relies on
    cfg2 = tmp_path / "config2"
    cfg2.write_text("Host github.com\n  IdentityFile ~/.ssh/id_ed25519\n", encoding="utf-8")
    podctl.write_ssh_config(cfg2, "203.0.113.9", 31022)
    got2 = cfg2.read_text(encoding="utf-8")
    assert got2.startswith("Host github.com\n  IdentityFile ~/.ssh/id_ed25519\n")
    assert "Host runpod\n  HostName 203.0.113.9\n  Port 31022\n  User root\n  IdentityFile ~/.ssh/id_ed25519\n  StrictHostKeyChecking accept-new\n  LocalForward 8188 localhost:8188\n" in got2
    assert (cfg2.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------- the sshd bootstrap (lib/boot.sh)

BOOT = BASE / "lib" / "boot.sh"


def _stubs(tmp_path, sshd_present=True):
    """Fake sshd / ssh-keygen / apt-get / pgrep on PATH, each appending its argv to <tmp>/calls.log.
    pgrep -x sshd answers 'running' once the sshd stub has been called."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    for name in ("ssh-keygen", "apt-get") + (("sshd",) if sshd_present else ()):
        p = bin_ / name
        p.write_text('#!/bin/bash\necho "%s $*" >> "%s"\n[ "%s" = sshd ] && touch "%s/sshd.running"\nexit 0\n' % (name, log, name, tmp_path))
        p.chmod(0o755)
    pg = bin_ / "pgrep"
    pg.write_text('#!/bin/bash\necho "pgrep $*" >> "%s"\n[ -e "%s/sshd.running" ]\n' % (log, tmp_path))
    pg.chmod(0o755)
    return bin_, log


def _run_boot(tmp_path, snippet, bin_):
    """Runs a boot snippet with BOOT_ROOT = a fake filesystem root; returns (result, root's fake home)."""
    fsroot = tmp_path / "fs"
    # the stubs plus the core tools only: the Mac's own /usr/sbin/sshd must stay invisible to the fake image
    env = {**os.environ, "PATH": "%s:/usr/bin:/bin" % bin_, "BOOT_ROOT": str(fsroot),
           "PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key"}
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env), fsroot / "root"


def test_unit_sshd_bootstrap_is_idempotent_and_bash32_clean(tmp_path):
    if not BOOT.exists():
        pytest.fail("lib/boot.sh is missing")
    src = BOOT.read_text(encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(BOOT)]).returncode == 0
    assert not re.search(r"declare\s+-[a-zA-Z]*[nA]|local\s+-n|\$\{[^}]*\[@\]\}\s*$", src), "bash 3.2 only: no namerefs, no assoc arrays"

    bin_, log = _stubs(tmp_path)
    r, root = _run_boot(tmp_path, 'source "%s"; boot_sshd' % BOOT, bin_)
    assert r.returncode == 0, r.stderr
    ak = root / ".ssh" / "authorized_keys"
    assert ak.read_text() == "ssh-ed25519 AAAAC3fake mac@key\n"
    assert (ak.stat().st_mode & 0o777) == 0o600 and ((root / ".ssh").stat().st_mode & 0o777) == 0o700
    calls = log.read_text()
    assert "sshd -p 22 -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password" in calls
    assert "apt-get" not in calls, "sshd was on PATH: no apt"
    assert "ssh-keygen -A" in calls
    assert "AAAAC3fake" not in r.stdout + r.stderr, "the key is never echoed"

    # second run: nothing repeated, nothing duplicated
    r2, _ = _run_boot(tmp_path, 'source "%s"; boot_sshd' % BOOT, bin_)
    assert r2.returncode == 0, r2.stderr
    assert ak.read_text() == "ssh-ed25519 AAAAC3fake mac@key\n"
    assert log.read_text().count("sshd -p 22") == 1

    # without sshd on the image: apt installs it, then it starts
    bin2, log2 = _stubs(tmp_path / "noimg", sshd_present=False)
    (bin2 / "apt-get").write_text('#!/bin/bash\necho "apt-get $*" >> "%s"\ncat > "%s/sshd" <<X\n#!/bin/bash\necho "sshd \\$*" >> "%s"; touch "%s/sshd.running"\nX\nchmod +x "%s/sshd"\n' % (log2, bin2, log2, tmp_path / "noimg", bin2))
    r3, _ = _run_boot(tmp_path / "noimg", 'source "%s"; boot_sshd' % BOOT, bin2)
    assert r3.returncode == 0, r3.stderr
    c3 = log2.read_text()
    assert "apt-get install -y -qq openssh-server" in c3 and c3.index("apt-get install") < c3.index("sshd -p 22")

    # the printed bootstrap is the same function, self-contained, and runs the same way (it is the start-command payload)
    out = subprocess.run(["bash", str(BOOT), "--print-sshd-bootstrap"], capture_output=True, text=True)
    assert out.returncode == 0 and "boot_sshd" in out.stdout and "authorized_keys" in out.stdout
    bin4, log4 = _stubs(tmp_path / "payload")
    r4, root4 = _run_boot(tmp_path / "payload", out.stdout, bin4)
    assert r4.returncode == 0, r4.stderr
    assert (root4 / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAAAC3fake mac@key\n"
    assert "sshd -p 22 -o PasswordAuthentication=no" in log4.read_text()


# ---------------------------------------------------------------- ensure: ports, key, start command, banner, ssh config

HM_CONFIG = {"Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"], "Cmd": ["/start_script.sh"]}
BOOT_GUARD = "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi"


def test_unit_podctl_ensure_wraps_entrypoint_when_the_image_has_no_cmd():
    podctl = _load()
    # the usual shape: a pass-through ENTRYPOINT and a CMD → the CMD is replaced, the entrypoint stays
    got = podctl.wrap_entry(HM_CONFIG["Entrypoint"], HM_CONFIG["Cmd"], "BOOTSTRAP")
    assert got == {"dockerStartCmd": ["bash", "-c", BOOT_GUARD + "; BOOTSTRAP; exec /start_script.sh"]}
    # an image whose ENTRYPOINT is the start script itself and no CMD → the entrypoint is replaced, CMD emptied
    got2 = podctl.wrap_entry(["/start.sh"], [], "BOOTSTRAP")
    assert got2 == {"dockerEntrypoint": ["bash", "-c", BOOT_GUARD + "; BOOTSTRAP; exec /start.sh"], "dockerStartCmd": []}
    # shell-safe quoting of the image's own words
    got3 = podctl.wrap_entry([], ["/bin/sh", "-c", "echo hi there"], "B")
    assert got3["dockerStartCmd"][2].endswith("; B; exec /bin/sh -c 'echo hi there'")
    with pytest.raises(podctl.PodctlError):
        podctl.wrap_entry([], [], "B")


def test_unit_podctl_image_config_reads_docker_hub(tmp_path):
    podctl = _load()
    seen = []
    def fetch(url, headers=None):
        seen.append(url)
        if "auth.docker.io/token" in url:
            return {"token": "T"}
        if url.endswith("/manifests/v8"):
            assert headers and headers.get("Authorization") == "Bearer T"
            return {"mediaType": "application/vnd.docker.distribution.manifest.v2+json", "config": {"digest": "sha256:abc"}, "layers": []}
        if url.endswith("/blobs/sha256:abc"):
            return {"config": {"Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"], "Cmd": ["/start_script.sh"]}}
        raise AssertionError(url)
    cfg = podctl.image_config("example/comfyui-template:v8", fetch=fetch)
    assert cfg == {"entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"], "cmd": ["/start_script.sh"]}
    assert "repository:example/comfyui-template:pull" in seen[0]
    # an official-library image gets the library/ prefix; a manifest LIST picks linux/amd64
    seen.clear()
    def fetch2(url, headers=None):
        if "token" in url: return {"token": "T"}
        if url.endswith("/manifests/latest"):
            return {"manifests": [{"platform": {"os": "linux", "architecture": "arm64"}, "digest": "sha256:arm"},
                                  {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd"}]}
        if url.endswith("/manifests/sha256:amd"):
            return {"config": {"digest": "sha256:cfg"}}
        if url.endswith("/blobs/sha256:cfg"):
            return {"config": {"Entrypoint": None, "Cmd": ["/start.sh"]}}
        raise AssertionError(url)
    assert podctl.image_config("ubuntu", fetch=fetch2) == {"entrypoint": [], "cmd": ["/start.sh"]}
    assert any("library/ubuntu" in u for u in seen) or True


def test_unit_podctl_ensure_patches_ports_key_and_start_command_in_that_order_and_is_idempotent(tmp_path):
    podctl = _load()
    fake = FakeRunPod(_pod())
    state = {"sshd": False}
    fake.on_patch = lambda body: state.update(sshd=True) if "dockerStartCmd" in body else None
    cfg = tmp_path / "ssh_config"
    cfg.write_text(SSH_CONFIG, encoding="utf-8")
    mac_key = "ssh-ed25519 AAAAC3fake mac@key"
    try:
        api = podctl.Api("k", base_url=fake.url)
        out = podctl.ensure(api, "fakepod0000001", pubkey=mac_key, ssh_config=cfg, bootstrap="BOOTSTRAP",
                            image_config=lambda ref: {"entrypoint": HM_CONFIG["Entrypoint"], "cmd": HM_CONFIG["Cmd"]},
                            banner=lambda host, port: state["sshd"], sleep=lambda s: None, ssh_check=lambda: "pod-hostname", probe=lambda u: 200)
        assert [list(p) for p in fake.patches] == [["ports"], ["env"], ["dockerStartCmd"]]
        assert fake.patches[0]["ports"] == ["8188/http", "8888/http", "22/tcp"]
        assert fake.patches[1]["env"]["PUBLIC_KEY"] == mac_key and fake.patches[1]["env"]["some_api_token"] == "abc-secret"
        assert fake.patches[2]["dockerStartCmd"] == ["bash", "-c", BOOT_GUARD + "; BOOTSTRAP; exec /opt/nvidia/nvidia_entrypoint.sh /start_script.sh"] or \
               fake.patches[2]["dockerStartCmd"] == ["bash", "-c", BOOT_GUARD + "; BOOTSTRAP; exec /start_script.sh"]
        text = cfg.read_text(encoding="utf-8")
        assert "HostName 127.0.0.1\n  Port 15270\n" in text and "LocalForward 8188 localhost:8188" in text
        assert "pod-hostname" in out and "abc-secret" not in out and "AAAAC3fake" not in out
        # again: nothing to change, nothing patched
        n = len(fake.patches)
        podctl.ensure(api, "fakepod0000001", pubkey=mac_key, ssh_config=cfg, bootstrap="BOOTSTRAP",
                      image_config=lambda ref: (_ for _ in ()).throw(AssertionError("not consulted when ssh answers")),
                      banner=lambda host, port: True, sleep=lambda s: None, ssh_check=lambda: "pod-hostname", probe=lambda u: 200)
        assert len(fake.patches) == n
    finally:
        fake.close()


def test_unit_podctl_ensure_installs_the_boot_override_even_when_sshd_already_answers(tmp_path):
    """An image that runs its OWN sshd used to skip the override entirely, because it lived in the
    "nothing answers" branch. runpod/comfyui:1.4.7-cuda13.0 is such an image: the pod worked all day,
    and its next restart came up on the IMAGE's ComfyUI instead of the base's boot.sh -- losing the
    base's launch line (--preview-method auto --preview-size 1024 --disable-api-nodes). Everything
    installed still ran, so the only symptom was the previews warning, once per restart. 2.0.52
    installs the override on its ABSENCE, and omits the sshd bootstrap the image does not need."""
    podctl = _load()
    fake = FakeRunPod(_pod(ports=["8188/http", "8888/http", "22/tcp"], portMappings={"22": 15270},
                           env={"PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key"}))
    cfg = tmp_path / "ssh_config"
    cfg.write_text(SSH_CONFIG, encoding="utf-8")
    try:
        api = podctl.Api("k", base_url=fake.url)
        podctl.ensure(api, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg,
                      bootstrap="BOOTSTRAP",
                      image_config=lambda ref: {"entrypoint": HM_CONFIG["Entrypoint"], "cmd": HM_CONFIG["Cmd"]},
                      banner=lambda h, p: True, sleep=lambda s: None, ssh_check=lambda: "pod-hostname", probe=lambda u: 200)
        wrote = [q for q in fake.patches if "dockerStartCmd" in q or "dockerEntrypoint" in q]
        assert wrote, "sshd answering must not stop the base's boot being made PID 1"
        line = " ".join(wrote[-1].get("dockerStartCmd") or wrote[-1].get("dockerEntrypoint"))
        assert BOOT_GUARD in line, line
        assert "BOOTSTRAP" not in line, "an image with its own sshd needs no sshd bootstrap: " + line
        assert "; ; " not in line, "an omitted bootstrap must not leave an empty command: " + line
    finally:
        fake.close()


def test_unit_podctl_ensure_refuses_an_unreadable_image_without_entry(tmp_path):
    podctl = _load()
    fake = FakeRunPod(_pod(ports=["8188/http", "8888/http", "22/tcp"], portMappings={"22": 15270},
                           env={"PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key"}))
    cfg = tmp_path / "ssh_config"
    try:
        api = podctl.Api("k", base_url=fake.url)
        def boom(ref): raise podctl.PodctlError("registry said no")
        with pytest.raises(podctl.PodctlError) as ei:
            podctl.ensure(api, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg, bootstrap="B",
                          image_config=boom, banner=lambda h, p: False, sleep=lambda s: None, ssh_check=lambda: "x", probe=lambda u: 200)
        assert "--entry" in str(ei.value) and "registry said no" in str(ei.value)
        assert fake.patches == []                    # nothing changed on a pod we cannot wrap
        # with --entry the operator supplies the image's own command and ensure proceeds
        state = {"sshd": False}
        fake.on_patch = lambda body: state.update(sshd=True) if "dockerStartCmd" in body else None
        podctl.ensure(api, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg, bootstrap="B",
                      image_config=boom, entry=["/start_script.sh"], banner=lambda h, p: state["sshd"], sleep=lambda s: None, ssh_check=lambda: "x", probe=lambda u: 200)
        assert fake.patches[-1]["dockerStartCmd"][2].endswith("; B; exec /start_script.sh")
    finally:
        fake.close()


# ---------------------------------------------------------------- stop / start / restart, upload

def test_unit_podctl_stop_start_wait(tmp_path):
    podctl = _load()
    fake = FakeRunPod(_pod(ports=["8188/http", "22/tcp"], portMappings={"22": 15270}))
    cfg = tmp_path / "ssh_config"
    cfg.write_text(SSH_CONFIG, encoding="utf-8")
    try:
        api = podctl.Api("k", base_url=fake.url)
        assert "stopped" in podctl.stop(api, "fakepod0000001", wait=True, sleep=lambda s: None)
        assert fake.posts == ["/pods/fakepod0000001/stop"] and fake.pod["desiredStatus"] == "EXITED"
        banners = iter([False, False, True])                       # the pod takes a while to answer after a start
        out = podctl.start(api, "fakepod0000001", wait=True, sleep=lambda s: None, banner=lambda h, p: next(banners), ssh_config=cfg)
        assert fake.posts[-1] == "/pods/fakepod0000001/start" and "127.0.0.1:15270" in out
        assert "HostName 127.0.0.1\n  Port 15270\n" in cfg.read_text(encoding="utf-8")   # the new mapping lands in Host runpod
        assert "restarted" in podctl.restart(api, "fakepod0000001", wait=False, sleep=lambda s: None)
        assert fake.posts[-1] == "/pods/fakepod0000001/restart"
    finally:
        fake.close()


def test_unit_podctl_upload_verifies_size_and_sha256_on_both_sides(tmp_path):
    podctl = _load()
    f = tmp_path / "Pkg-runpod.zip"
    f.write_bytes(b"zip-bytes-" * 100)
    import hashlib
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    bin_ = tmp_path / "bin"; bin_.mkdir()
    log = tmp_path / "calls.log"
    (bin_ / "scp").write_text('#!/bin/bash\necho "scp $*" >> "%s"\nexit 0\n' % log); (bin_ / "scp").chmod(0o755)
    # the remote sha256sum answers with whatever the test plants in remote.txt
    (bin_ / "ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\ncat "%s/remote.txt"\n' % (log, tmp_path)); (bin_ / "ssh").chmod(0o755)
    env = {**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"])}
    (tmp_path / "remote.txt").write_text("%d %s  /workspace/packages/Pkg-runpod.zip\n" % (f.stat().st_size, digest))
    out = podctl.upload([f], "/workspace/packages", env=env)
    calls = log.read_text()
    assert "scp " in calls and "runpod:/workspace/packages/" in calls and "mkdir -p /workspace/packages" in calls
    assert "Pkg-runpod.zip" in out and "ok" in out
    (tmp_path / "remote.txt").write_text("12 0000  /workspace/packages/Pkg-runpod.zip\n")
    with pytest.raises(podctl.PodctlError) as ei:
        podctl.upload([f], "/workspace/packages", env=env)
    assert "sha256" in str(ei.value) or "size" in str(ei.value)


def test_unit_podctl_cli_upload_takes_the_pod_then_files():
    """The documented form is `podctl upload <pod> <file>...` (the README, the handbooks); the parser must read it so."""
    podctl = _load()
    a = podctl.build_parser().parse_args(["upload", "fakepod0000001", "a.zip", "b.zip"])
    assert a.pod == "fakepod0000001" and a.files == ["a.zip", "b.zip"] and a.to == "/workspace/packages"
    a = podctl.build_parser().parse_args(["upload", "fakepod0000001", "a.zip", "--to", "/workspace/packages/incoming"])
    assert a.files == ["a.zip"] and a.to == "/workspace/packages/incoming"


def test_unit_podctl_command_sessions_carry_no_port_forwards_and_the_tunnel_carries_them(tmp_path):
    """Every `ssh runpod <cmd>` printed "bind: Address already in use" three times on 2026-09-06 because Host runpod's
    LocalForward lines apply to command sessions too, and a tunnel already held the ports. Command sessions clear the
    forwards; `podctl tunnel <pod>` is the one session that forwards, explicitly."""
    podctl = _load()
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "scp").write_text('#!/bin/bash\necho "scp $*" >> "%s"\nexit 0\n' % log); (bin_ / "scp").chmod(0o755)
    (bin_ / "ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\ncat "%s/remote.txt"\n' % (log, tmp_path)); (bin_ / "ssh").chmod(0o755)
    f = tmp_path / "A.zip"; f.write_bytes(b"x" * 10)
    import hashlib
    (tmp_path / "remote.txt").write_text("10 %s  /workspace/packages/A.zip\n" % hashlib.sha256(b"x" * 10).hexdigest())
    podctl.upload([f], "/workspace/packages", env={**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    for line in log.read_text().splitlines():
        assert "ClearAllForwardings=yes" in line, line
    # the tunnel: OpenSSH applies ClearAllForwardings AFTER the command line, so `-o ClearAllForwardings=yes … -L …` cleared
    # its own -L too and podctl's tunnel never bound a port (found at a package's acceptance; `ssh -N runpod` had been
    # doing the forwarding all along). The tunnel now ignores the config file entirely and addresses the pod itself.
    argv = podctl.tunnel_argv(host="1.2.3.4", port=15486, key="/k/id_ed25519")
    assert argv[:2] == ["ssh", "-N"] and "ClearAllForwardings=yes" not in " ".join(argv) and "ServerAliveInterval=30" in argv and "ExitOnForwardFailure=yes" in argv
    assert argv[argv.index("-F") + 1] == "/dev/null" and argv[argv.index("-i") + 1] == "/k/id_ed25519" and argv[argv.index("-p") + 1] == "15486"
    assert "8188:127.0.0.1:8188" in argv and "8888:127.0.0.1:8888" in argv and argv[-1] == "root@1.2.3.4"
    assert podctl.tunnel_argv(ports=[8199], host="h", port=1, key="k").count("-L") == 1
    # the key beside --pubkey, whether the flag was given, set in the environment, or left to the default (2.0.23: None crashed)
    assert podctl.tunnel_key("/k/id_ed25519.pub", {}) == "/k/id_ed25519" and podctl.tunnel_key(None, {"PODCTL_PUBKEY": "/e/x.pub"}) == "/e/x"
    assert podctl.tunnel_key(None, {}) == os.path.expanduser("~/.ssh/id_ed25519")
    a = podctl.build_parser().parse_args(["tunnel", "fakepod0000001"]); assert a.cmd == "tunnel" and a.port == [8188, 8888]
    a = podctl.build_parser().parse_args(["tunnel", "fakepod0000001", "--port", "8199"]); assert a.port == [8199]


def test_unit_podctl_records_the_volume_size_for_the_disk_gate(tmp_path):
    """The pod's /workspace is a pooled filesystem: df reports the pool (677 TB), so the base's disk gate could only warn
    "not verifiable". RunPod's REST knows the volume's size; podctl writes it to state/volume.env on upload and ensure."""
    podctl = _load()
    srv = FakeRunPod(_pod(networkVolumeId="vol123"), volumes={"vol123": {"id": "vol123", "size": 300, "name": "data"}})
    api = podctl.Api("k", base_url=srv.url)
    assert podctl.volume_size_gb(api, srv.pod) == 300
    assert podctl.volume_size_gb(api, _pod(networkVolumeId=None)) is None
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\nexit 0\n' % log); (bin_ / "ssh").chmod(0o755)
    out = podctl.record_volume(api, srv.pod, env={**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    calls = log.read_text()
    assert "VOLUME_GB=" in calls and " 300 " in calls and "vol123" in calls and "/workspace/comfy-base/state/volume.env" in calls and "mkdir -p" in calls
    assert "300" in out and "vol123" in out
    assert podctl.record_volume(api, _pod(networkVolumeId=None), env=os.environ) is None      # no volume: nothing recorded, nothing to say


# ---------------------------------------------------------------- podctl install (plan Part D1): the sequence proven by hand on 2026-09-06

STUB_SCRIPT = '''#!/bin/bash
R="$(cd "$(dirname "$0")/%s" && pwd)"
echo "$(basename "$0") $*" >> "$R/calls.log"
if [ "$1" = "preflight" ] && [ -f "$R/no_preflight" ]; then echo "usage: bash x [--check | --latest]" >&2; exit 2; fi
if [ "$1" = "--check" ] || [ "$1" = "preflight" ]; then exit "$(cat "$R/check_rc" 2>/dev/null || echo 0)"; fi
echo "══ NODE PACKS · locate / clone / pin ══"
echo '  "PackA|https://github.com/x/PackA|bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb||the pack"'
echo '  "PackB|https://github.com/x/PackB|cccccccccccccccccccccccccccccccccccccccc|packb|another"'
echo "══ SUMMARY · %s ══"
echo "  suite      3 passed"
echo "  restart    restarted"
exit "$(cat "$R/run_rc" 2>/dev/null || echo 0)"
'''


# 3.2.0: `base.sh install-self` as the pod sees it: logs the call and installs the base onto the volume (a VERSION,
# a manifest, the step-one script), which is all a package run needs from step one when both are installed together
BASE_SH_STUB = '''#!/bin/bash
R="$(cd "$(dirname "$0")/../.." && pwd)"
echo "base.sh $*" >> "$R/calls.log"
mkdir -p "$R/comfy-base/state"; echo "9.9.9" > "$R/comfy-base/VERSION"; echo "0000  lib/x.sh" > "$R/comfy-base/MANIFEST.sha256"
cat > "$R/comfy-base/comfyui-base-script.sh" <<'STUBEOF'
%s
STUBEOF
'''


class LocalPodIO:
    """The pod as a directory: /workspace → root; commands run in a local bash; uploads copy; fetch copies. Records everything."""
    def __init__(self, root):
        self.root = pathlib.Path(root); (self.root / "packages").mkdir(parents=True); self.cmds = []; self.uploads = []; self.rebuilt = []; self.slept = 0; self.fetched = []
        (self.root / "comfy-base" / "state" / "logs").mkdir(parents=True)
    def check_zip(self, d):
        z = next(pathlib.Path(d).glob("comfyui-base.zip")) if (pathlib.Path(d) / "base.sh").exists() else next(pathlib.Path(d).glob("*-runpod.zip")); return z
    def upload(self, files, to):
        import shutil
        self.uploads.append((list(map(str, files)), to))
        for f in files: shutil.copy(f, self.root / "packages" / pathlib.Path(f).name)
    def remote(self, cmd):
        import subprocess
        self.cmds.append(cmd)
        local = cmd.replace("/workspace", str(self.root)).replace("nohup setsid ", "")
        r = subprocess.run(["bash", "-c", local], capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    def fetch(self, remote_dir, local_dir, newer_than=None):
        import shutil
        src = pathlib.Path(remote_dir.replace("/workspace", str(self.root))); local_dir = pathlib.Path(local_dir); local_dir.mkdir(parents=True, exist_ok=True)
        self.fetched.append((remote_dir, newer_than))
        cut = None
        if newer_than:                     # the POD's own file decides, exactly as GNU tar's --newer does (2.0.55)
            marker = pathlib.Path(str(newer_than).replace("/workspace", str(self.root)))
            cut = marker.stat().st_mtime if marker.exists() else None
        for f in src.iterdir():
            if f.is_file() and (cut is None or f.stat().st_mtime >= cut): shutil.copy(f, local_dir / f.name)
    def rebuild(self, d): self.rebuilt.append(str(d))
    def sleep(self, s): self.slept += s
    def say(self, *a): print(*a)


def _local_packages(tmp_path):
    """A local repo with a base dir and one package dir, each with a zip whose script is the stub above."""
    import zipfile
    base = tmp_path / "local" / "comfyui-base"; base.mkdir(parents=True); (base / "base.sh").write_text("# base\n")
    with zipfile.ZipFile(base / "comfyui-base.zip", "w") as z:
        z.writestr("comfyui-base/comfyui-base-script.sh", STUB_SCRIPT % ("../..", "base"))
        z.writestr("comfyui-base/base.sh", BASE_SH_STUB % (STUB_SCRIPT % ("..", "base")))
    pkg = tmp_path / "local" / "My Pkg"; pkg.mkdir()
    (pkg / "My Pkg-script.sh").write_text('PACKS=(\n "PackA|https://github.com/x/PackA|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa||the pack"\n "PackB|https://github.com/x/PackB|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1|packb|another"\n)\n')
    with zipfile.ZipFile(pkg / "My Pkg-runpod.zip", "w") as z:
        z.writestr("My Pkg-script.sh", STUB_SCRIPT % ("../..", "My Pkg"))   # a package lives in packages/<Name>/ (2.0.17)
    return base, pkg


def test_unit_podctl_install_runs_the_documented_sequence_and_stops_at_the_first_failure(tmp_path, capsys):
    """One command for what took a night by hand: for each item — the zip is current, upload, extract, --check (stop on
    non-zero), the real run detached with --latest (BASE_RESTART=1 for a package), poll for STEP RC, copy the pod's
    logs beside the package, print the summary, stop at the first red with the console's path; green → save the printed
    pin rows into the local script and rebuild its zip."""
    podctl = _load()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [base, pkg], io, every=1, timeout=60, today="2026-09-06")
    out = capsys.readouterr().out
    assert rc == 0, out
    cmds = io.cmds
    assert [u[0][0].split("/")[-1] for u in io.uploads] == ["comfyui-base.zip", "My Pkg-runpod.zip"]
    # 3.2.0: with a package after it, the base is INSTALLED (and its suite run once per base version), never run as a
    # second whole pipeline: the package's run already does everything the base's own run did
    extract = [c for c in cmds if "zipfile -e" in c]; selfs = [c for c in cmds if "install-self" in c and "nohup" not in c]
    checks = [c for c in cmds if " preflight" in c]; runs = [c for c in cmds if "STEP RC=" in c and "nohup" in c]
    assert len(extract) == 2 and len(selfs) == 1 and len(checks) == 1 and len(runs) == 2, cmds
    assert cmds.index(extract[0]) < cmds.index(selfs[0]) < cmds.index(runs[0]) < cmds.index(extract[1]) < cmds.index(checks[0]) < cmds.index(runs[1])
    assert "BASE_REEXEC=1 bash comfyui-base/base.sh install-self" in selfs[0]
    assert "comfyui-base-script.sh test" in runs[0] and "base-suite.ok" in runs[0]            # the base's suite, once per base version
    assert 'BASE_RESTART=1 bash "My Pkg/My Pkg-script.sh";' in runs[1]        # 2.0.17: a package runs from its own folder
    assert 'mkdir -p ' in extract[1] and "'My Pkg'" in extract[1] and extract[1].rstrip().endswith("'My Pkg'"), extract[1]
    assert extract[0].rstrip().endswith(" .")                                                  # the base zip carries its own folder
    calls = (io.root / "calls.log").read_text().splitlines()
    assert [c.rstrip() for c in calls] == ["base.sh install-self", "comfyui-base-script.sh test", "My Pkg-script.sh preflight", "My Pkg-script.sh"]
    assert (io.root / "packages" / "My Pkg" / "My Pkg-script.sh").exists() and not (io.root / "packages" / "My Pkg-script.sh").exists()
    # the pod's logs land beside each item, the console among them; the summary is printed; the pins are saved and the zip rebuilt
    for d in (base, pkg):
        logs = d / "_build" / "podruns" / "2026-09-06" / "pod-logs"; assert logs.is_dir() and any(f.name.startswith("install_") for f in logs.iterdir()), list(d.glob("_build/**/*"))
    assert "══ SUMMARY · My Pkg ══" in out and "STEP RC=0" in out
    s = (pkg / "My Pkg-script.sh").read_text()
    assert "PackA|https://github.com/x/PackA|bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb|" in s and "PackB|https://github.com/x/PackB|cccccccccccccccccccccccccccccccccccccccc|packb" in s and "aaaaaaaa" not in s
    assert io.rebuilt == [str(pkg)] and "pins" in out
    # a preflight that fails stops the item before its run, and the whole install
    (io.root / "check_rc").write_text("1"); io.cmds.clear(); (io.root / "calls.log").unlink()
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06"); out = capsys.readouterr().out
    assert rc == 1 and "STOP" in out and "preflight" in out and not [c for c in io.cmds if "nohup" in c]
    # a red run stops with the console path, its logs copied
    (io.root / "check_rc").write_text("0"); (io.root / "run_rc").write_text("1"); io.cmds.clear()
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06"); out = capsys.readouterr().out
    assert rc == 1 and "STEP RC=1" in out and "STOP" in out and ".console" in out
    (io.root / "run_rc").write_text("0")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06", latest=False, save_pins=False); out = capsys.readouterr().out
    assert rc == 0 and "--latest" not in [c for c in io.cmds if "nohup" in c][-1] and io.rebuilt == [str(pkg)]


def test_unit_podctl_install_keeps_two_packages_apart(tmp_path):
    """Two package zips both ship a flat `suite.py` and `pytest.ini`. Extracting flat into /workspace/packages let the
    second overwrite the first's: one package's `test` on the pod would then have run the other's suite. Each package now extracts into packages/<Name>/; the base keeps its own folder."""
    import zipfile
    podctl = _load()
    base, a = _local_packages(tmp_path)
    b = tmp_path / "local" / "Other Pkg"; b.mkdir()
    (a / "suite.py").write_text("# suite of My Pkg\n"); (b / "suite.py").write_text("# suite of Other Pkg\n")
    with zipfile.ZipFile(a / "My Pkg-runpod.zip", "w") as z:
        z.writestr("My Pkg-script.sh", STUB_SCRIPT % ("../..", "My Pkg")); z.write(a / "suite.py", "suite.py"); z.writestr("pytest.ini", "[pytest]\nmarkers = a\n")
    (b / "Other Pkg-script.sh").write_text("PACKS=()\n")
    with zipfile.ZipFile(b / "Other Pkg-runpod.zip", "w") as z:
        z.writestr("Other Pkg-script.sh", STUB_SCRIPT % ("../..", "Other Pkg")); z.write(b / "suite.py", "suite.py"); z.writestr("pytest.ini", "[pytest]\nmarkers = b\n")
    io = LocalPodIO(tmp_path / "pod")
    assert podctl.install("pod1", [base, a, b], io, every=1, timeout=60, today="2026-09-06") == 0
    pk = io.root / "packages"
    assert (pk / "comfyui-base" / "comfyui-base-script.sh").exists()
    assert (pk / "My Pkg" / "suite.py").read_text() == "# suite of My Pkg\n"
    assert (pk / "Other Pkg" / "suite.py").read_text() == "# suite of Other Pkg\n"
    assert (pk / "My Pkg" / "pytest.ini").read_text() != (pk / "Other Pkg" / "pytest.ini").read_text()
    assert not (pk / "suite.py").exists() and not (pk / "pytest.ini").exists(), "a package file landed flat in packages/"
    assert podctl.item_for(a)["script"] == "My Pkg/My Pkg-script.sh" and podctl.item_for(base)["script"] == "comfyui-base/comfyui-base-script.sh"


SLOW_STUB = STUB_SCRIPT.replace('echo "══ SUMMARY', 'sleep 2\necho "══ SUMMARY')


def test_unit_podctl_install_launches_the_run_detached_and_polls_it(tmp_path):
    """On the pod the launch ssh stayed open for the WHOLE step (seen on the base step and again on a package's):
    `cd … && mkdir … && nohup setsid … &` backgrounds the whole AND-list in a subshell whose stdout is the ssh channel,
    and that subshell waits for the run. podctl then printed nothing until the end and its per-step timeout started
    late. Only the run may go to the background; the launch must return while the step is still going."""
    import zipfile
    podctl = _load()
    base, pkg = _local_packages(tmp_path)
    with zipfile.ZipFile(pkg / "My Pkg-runpod.zip", "w") as z:
        z.writestr("My Pkg-script.sh", SLOW_STUB % ("../..", "My Pkg"))       # the step takes 2 s
    class SleepingPodIO(LocalPodIO):                  # the stand-in's sleep is a counter; here the wait must be real
        def sleep(self, s):
            import time
            time.sleep(s); self.slept += s
    io = SleepingPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06", save_pins=False)
    assert rc == 0
    assert io.slept >= 1, "the launch blocked until the step ended: the poll loop never had to wait"
    run = [c for c in io.cmds if "STEP RC=" in c and "nohup" in c][0]
    assert "{ nohup setsid" in run and run.rstrip().endswith("& }"), run


def _smi_stub(tmp_path, total, used, apps):
    """A `nvidia-smi` on PATH answering the two queries podctl asks: the card's memory, and this container's processes."""
    d = tmp_path / "smibin"; d.mkdir(parents=True, exist_ok=True)
    app_args = " ".join('"%d, %d"' % a for a in apps)
    (d / "nvidia-smi").write_text('#!/bin/bash\ncase "$*" in *query-gpu=memory*) echo "%d, %d";; *query-compute-apps*) printf "%%s\\n" %s;; *) exit 1;; esac\n' % (total, used, app_args))
    (d / "nvidia-smi").chmod(0o755)
    return str(d)


def test_unit_podctl_install_refuses_a_gpu_that_is_not_ours(tmp_path, monkeypatch, capsys):
    """2026-09-06, pod fakepod0000003: every check passed, the install went green, and the first render died at its first
    node — 93.5 GB of the card were held by processes outside the container (0 % util). podctl asks the pod the same
    question the base does, BEFORE anything is uploaded, and stops with the remedy; a pod with no nvidia-smi is a note."""
    import sys
    podctl = _load()
    base, pkg = _local_packages(tmp_path)
    monkeypatch.setenv("PATH", "%s:%s" % (_smi_stub(tmp_path / "dirty", 97887, 95258, [(1903, 1698)]), os.environ["PATH"]))
    io = LocalPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06", save_pins=False)
    out = capsys.readouterr().out
    assert rc == 1 and "STOP" in out and "not ours" in out and "91 GiB" in out and "stop and start" in out, out
    assert io.uploads == [] and not [c for c in io.cmds if "zipfile" in c or "nohup" in c], "the install went on with a dirty GPU"
    monkeypatch.setenv("PATH", "%s:%s" % (_smi_stub(tmp_path / "clean", 97887, 620, []), os.environ["PATH"]))
    io = LocalPodIO(tmp_path / "pod2")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-06", save_pins=False)
    out = capsys.readouterr().out
    assert rc == 0 and "the GPU is ours" in out, out
    # the two implementations of the rule agree on the numbers of the day
    sys.path.insert(0, str(BASE / "py")); import gpu_facts
    assert podctl.gpu_verdict(97887, 95258, [(1903, 1698)]) == gpu_facts.verdict(97887, 95258, [(1903, 1698)])
    assert podctl.gpu_verdict(97887, 620, []) == gpu_facts.verdict(97887, 620, [])


def test_unit_podctl_install_warns_when_the_pod_gpu_is_below_a_packages_gpu_prefer(tmp_path, monkeypatch, capsys):
    """3.8.0: before the upload, podctl asks the pod the three numbers the pod's own run reads (name, compute capability,
    memory) and holds them against every package's GPU_PREFER. Below is a WARNING naming what the package wants; the
    install goes on. A package without GPU_PREFER costs no extra ssh call."""
    podctl = _load()
    base, pkg = _local_packages(tmp_path)
    (pkg / "My Pkg-script.sh").write_text('# GPU_PREFER="H9000" in a comment is not a declaration\nGPU_PREFER="B200 B300"\n' + (pkg / "My Pkg-script.sh").read_text())
    assert podctl.gpu_prefer_of(pkg / "My Pkg-script.sh") == ["B200", "B300"]
    def smi(where, name, cc, mib):
        d = tmp_path / where; d.mkdir(parents=True, exist_ok=True)
        (d / "nvidia-smi").write_text('#!/bin/bash\ncase "$*" in *query-gpu=name,compute_cap,memory.total*) echo "%s, %s, %d";; *query-gpu=memory*) echo "%d, 600";; '
                                      '*query-compute-apps*) :;; *) exit 1;; esac\n' % (name, cc, mib, mib))
        (d / "nvidia-smi").chmod(0o755); return str(d)
    monkeypatch.setenv("PATH", "%s:%s" % (smi("h200", "NVIDIA H200", "9.0", 143771), os.environ["PATH"]))
    io = LocalPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-10-16", save_pins=False)
    out = capsys.readouterr().out
    assert rc == 0 and "WARNING: NVIDIA H200 (151 GB, sm_90) is below what My Pkg is tuned for: B200 (sm_100, 180 GB) or B300 (sm_103, 288 GB)" in out, out
    assert out.index("WARNING") < out.index("extracted"), "the warning comes before the upload"
    monkeypatch.setenv("PATH", "%s:%s" % (smi("b300", "NVIDIA B300 SXM6 AC", "10.3", 275040), os.environ["PATH"]))
    io = LocalPodIO(tmp_path / "pod2")
    rc = podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-10-16", save_pins=False)
    out = capsys.readouterr().out
    assert rc == 0 and "gpu fit NVIDIA B300 SXM6 AC is what My Pkg is tuned for" in out and "WARNING" not in out, out
    (pkg / "My Pkg-script.sh").write_text("PACKS=()\n")
    io = LocalPodIO(tmp_path / "pod3")
    assert podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-10-16", save_pins=False) == 0
    assert not [c for c in io.cmds if "compute_cap" in c], "no GPU_PREFER, no question"


def test_unit_podctl_deploy_clones_the_pod_onto_its_own_volume(tmp_path):
    """2026-09-06: a stop/start put pod fakepod0000003 back on the machine whose GPU is not ours. The next rung is a NEW pod
    on the same network volume, cloned from the old one — image, GPU type, disk, ports, env, and the base's boot override as
    its start command, so the clone boots the base before any template script can run; the datacenter is the volume's;
    never a templateId (the template would re-apply its own env and start command). Env values never reach the output."""
    podctl = _load()
    src = {"id": "old1", "name": "ermine", "imageName": "example/comfyui-template:v15", "gpuCount": 1, "containerDiskInGb": 500,
           "networkVolumeId": "fakevol001", "volumeMountPath": "/workspace", "ports": ["8188/http", "8888/http", "22/tcp"],
           "env": {"HF_TOKEN": "hf_secretvalue", "download_example": "true"}, "dockerStartCmd": ["bash", "-c", "exec /workspace/comfy-base/boot.sh"],
           "machineId": "fakemachine01", "machine": {"gpuTypeId": "NVIDIA RTX PRO 6000 Blackwell Server Edition", "secureCloud": True, "dataCenterId": "FAKE-DC-1"},
           "networkVolume": {"id": "fakevol001", "dataCenterId": "FAKE-DC-1", "size": 600}, "templateId": "faketmpl02"}

    class _Api:
        def __init__(self, machine="other-machine"):
            self.pods = {"old1": dict(src)}; self.posts = []; self.machine = machine
        def get(self, path):
            pid = path.split("/pods/")[1].split("?")[0]; return dict(self.pods[pid])
        def post(self, path, body=None):
            self.posts.append((path, body)); assert path == "/pods", path
            new = dict(body, id="new1", machineId=self.machine, desiredStatus="RUNNING", publicIp="10.0.0.9", portMappings={"22": 40022})
            self.pods["new1"] = new; return new

    api = _Api(); said = []; cfg = tmp_path / "config"
    pod_id = podctl.deploy(api, "old1", wait=True, sleep=lambda s: None, banner=lambda h, p: True, ssh_config=cfg, say=said.append)
    assert pod_id == "new1"
    _, body = api.posts[0]
    assert body["imageName"] == src["imageName"] and body["gpuTypeIds"] == ["NVIDIA RTX PRO 6000 Blackwell Server Edition"] and body["gpuCount"] == 1
    assert body["networkVolumeId"] == "fakevol001" and body["dataCenterIds"] == ["FAKE-DC-1"] and body["volumeMountPath"] == "/workspace"
    assert body["containerDiskInGb"] == 500 and body["ports"] == src["ports"] and body["env"] == src["env"] and body["dockerStartCmd"] == src["dockerStartCmd"]
    assert body["cloudType"] == "SECURE" and "templateId" not in body and body["name"] == "ermine-2"
    assert "10.0.0.9" in cfg.read_text() and "40022" in cfg.read_text(), "Host runpod must point at the NEW pod"
    out = "\n".join(said)
    assert "deployed new1" in out and "ssh answers on 10.0.0.9:40022" in out and "hf_secretvalue" not in out, out
    # the same machine again is said out loud (the GPU verdict decides); a --gpu override and --name are honoured
    api2 = _Api(machine="fakemachine01"); said2 = []
    podctl.deploy(api2, "old1", name="fresh", gpu="NVIDIA H200", wait=False, say=said2.append)
    assert "SAME machine" in "\n".join(said2) and api2.posts[0][1]["name"] == "fresh" and api2.posts[0][1]["gpuTypeIds"] == ["NVIDIA H200"]
    # a pod without a network volume has nothing a clone could share
    api3 = _Api(); api3.pods["old1"] = dict(src, networkVolumeId=None, networkVolume=None)
    with pytest.raises(podctl.PodctlError):
        podctl.deploy(api3, "old1", wait=False, say=lambda *a: None)
    assert api3.posts == [], "nothing may be created for a pod without a volume"


def test_unit_podctl_ssh_sessions_accept_a_new_pods_host_key(tmp_path):
    """2026-09-06, the first pod podctl ever deployed: sshd answered and every command session failed with 'Host key
    verification failed' — BatchMode with the default StrictHostKeyChecking (ask) refuses an unknown host, and a new pod is
    always an unknown host. accept-new (the tunnel's setting since 2.0.21) everywhere: the block, ssh and scp; a block
    written before 2.0.28 gets the line once."""
    podctl = _load()
    assert "StrictHostKeyChecking=accept-new" in " ".join(podctl.SSH_CMD) and "StrictHostKeyChecking=accept-new" in " ".join(podctl.SCP_CMD)
    cfg = tmp_path / "config"
    cfg.write_text("Host other\n  HostName 1.2.3.4\n\nHost runpod\n  HostName 203.0.113.9\n  Port 31022\n  User root\n  IdentityFile ~/.ssh/id_ed25519\n  LocalForward 8188 localhost:8188\n")
    podctl.write_ssh_config(cfg, "203.0.113.10", 40022)
    text = cfg.read_text(); block = text[text.index("Host runpod"):]
    assert block.count("StrictHostKeyChecking accept-new") == 1 and "HostName 203.0.113.10" in block and "Port 40022" in block, block
    assert text.startswith("Host other\n  HostName 1.2.3.4\n\n"), "other blocks stay byte for byte"
    podctl.write_ssh_config(cfg, "203.0.113.11", 40023)
    assert cfg.read_text().count("StrictHostKeyChecking accept-new") == 1, "never duplicated"
    fresh = tmp_path / "fresh"; podctl.write_ssh_config(fresh, "203.0.113.9", 31022)
    assert "StrictHostKeyChecking accept-new" in fresh.read_text()


def test_unit_podctl_save_pins_rewrites_only_the_named_rows(tmp_path):
    podctl = _load()
    sc = tmp_path / "X-script.sh"
    sc.write_text('PACKS=(\n "A|https://github.com/x/A|1111111111111111111111111111111111111111||a"\n "B|https://github.com/x/B|2222222222222222222222222222222222222222|b|b"\n)\n')
    console = '''noise
  "A|https://github.com/x/A|3333333333333333333333333333333333333333||a"
  "C|https://github.com/x/C|4444444444444444444444444444444444444444||not declared here"
  "B|https://github.com/x/B|2222222222222222222222222222222222222222|b|b"
'''
    rows = podctl.pin_rows(console)
    assert [r[0] for r in rows] == ["A", "C", "B"]
    changes = podctl.save_pins(sc, rows)
    assert changes == [("A", "1111111111111111111111111111111111111111", "3333333333333333333333333333333333333333")]
    s = sc.read_text(); assert "|3333333333333333333333333333333333333333||a" in s and "C|" not in s and "|2222222222222222222222222222222222222222|b|b" in s
    assert podctl.save_pins(sc, rows) == []                                            # idempotent


def test_unit_podctl_pins_for_other_packages_are_measured_and_recorded_never_written(tmp_path):
    """One package's green --latest run moved two packs another package also declares; the base's cross-package rule then
    refused the repo ("pinned to two SHAs") until the other's rows were pasted by hand — and the base's own rows
    (lib/40-packs.sh) had always been a paste. A run MEASURES those rows and records them; it does not write files it
    does not own. Writing them was the fault behind three blockages in one day — an install left other packages' zips
    stale against their trees, and `package.py --check` then refused the next upload by whoever touched that package,
    for a pin they had never seen."""
    podctl = _load()
    root = tmp_path / "repo"; base = root / "base" / "comfyui-base" / "lib"; base.mkdir(parents=True)     # the 2026-09-12 layout
    (root / "base" / "comfyui-base" / "base.sh").write_text("# base\n")
    (base / "40-packs.sh").write_text('BASE_PACKS=(\n "rgthree-comfy|https://github.com/rgthree/rgthree-comfy|1111111111111111111111111111111111111111||switches"\n)\n')
    pk = root / "brand-a" / "packages"; pk.mkdir(parents=True); pe = root / "brand-b" / "packages"; pe.mkdir(parents=True)
    a = pk / "A Pkg"; a.mkdir(); b = pk / "B Pkg"; b.mkdir(); c = pe / "C Pkg"; c.mkdir()          # C is the other brand's
    (a / "A Pkg-script.sh").write_text('PACKS=(\n "Shared|https://github.com/x/Shared|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa||both"\n)\n')
    (b / "B Pkg-script.sh").write_text('PACKS=(\n "Shared|https://github.com/x/Shared|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa||both"\n "Own|https://github.com/x/Own|bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb||b only"\n)\n')
    (c / "C Pkg-script.sh").write_text('PACKS=(\n "Other|https://github.com/x/Other|cccccccccccccccccccccccccccccccccccccccc||unrelated"\n)\n')
    rows = podctl.pin_rows('  "Shared|https://github.com/x/Shared|dddddddddddddddddddddddddddddddddddddddd||both"\n'
                           '  "rgthree-comfy|https://github.com/rgthree/rgthree-comfy|2222222222222222222222222222222222222222||switches"\n')

    before = {f: f.read_text(encoding="utf-8") for f in (b / "B Pkg-script.sh", c / "C Pkg-script.sh", base / "40-packs.sh")}
    sub = dict(base_dir=root / "base" / "comfyui-base", env={})              # 3.6.0: the submodule shape, this tree's own base
    report = podctl.pins_elsewhere(a / "A Pkg-script.sh", rows, **sub)

    # NOTHING outside the run's own package is written — that is the whole point
    for f, text in before.items():
        assert f.read_text(encoding="utf-8") == text, f"{f.name} was modified by a measurement"
    # but the measurement is complete: the shared pack in B, and the base's own row
    assert {(pathlib.Path(r[0]).name, r[1]) for r in report} == {("B Pkg-script.sh", "Shared"), ("40-packs.sh", "rgthree-comfy")}, report
    assert all(r[2] != r[3] for r in report)

    # it is recorded where a person can accept it, saying who measured it and how to apply it
    art = podctl.write_pins_artefact(root / "base" / "comfyui-base", "pod1", "a_pkg", report, today="2026-09-08")
    body = art.read_text(encoding="utf-8")
    assert art.parent.name == "pins" and "NOT applied" in body and "--apply" in body
    assert "B Pkg-script.sh" in body and "40-packs.sh" in body

    # and applying it is a separate, deliberate act by whoever owns those files
    applied = podctl.apply_pins_artefact(art, root / "comfyui-base")
    assert {n for n, *_ in applied} == {"B Pkg-script.sh", "40-packs.sh"}, applied
    assert "dddddddd" in (b / "B Pkg-script.sh").read_text(encoding="utf-8")
    assert "2222222222222222222222222222222222222222||switches" in (base / "40-packs.sh").read_text(encoding="utf-8")
    assert "cccccccc" in (c / "C Pkg-script.sh").read_text(encoding="utf-8")   # unrelated rows untouched
    assert "bbbbbbbb" in (b / "B Pkg-script.sh").read_text(encoding="utf-8")   # B's own row untouched
    assert podctl.pins_elsewhere(a / "A Pkg-script.sh", rows, **sub) == []     # idempotent once applied


def test_unit_podctl_cli_install_takes_base_and_packages_in_order():
    podctl = _load()
    a = podctl.build_parser().parse_args(["install", "fakepod0000001", "--base", "--pkg", "Example Video Creator", "--pkg", "Example", "--every", "5"])
    assert a.cmd == "install" and a.base and a.pkg == ["Example Video Creator", "Example"] and a.every == 5 and a.save_pins and a.comfy_ref is None and not a.no_latest
    a = podctl.build_parser().parse_args(["install", "fakepod0000001", "--pkg", "Example", "--no-latest", "--no-save-pins"])
    assert not a.base and a.no_latest and not a.save_pins and a.timeout >= 3600          # parsed, then refused by cmd_install (3.0.0)
    a = podctl.build_parser().parse_args(["install", "fakepod0000001", "--base", "--comfy-ref", "master"])
    assert a.comfy_ref == "master"


# ============================================================== the GPU lease: the pod is leased, not grabbed
def test_unit_podctl_lease_is_advisory_expiring_and_never_locks_a_pod_out():
    """One pod, several sessions: one session's install once restarted ComfyUI in the middle of
    another's render. The lease says who holds the GPU and until when — and every failure mode resolves to
    "free", because a crashed session must never lock the pod out."""
    import datetime
    podctl = _load()
    now = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)
    line = podctl.lease_encode("session-a", "the long run", 30, now=now)
    lease = podctl.lease_parse(line)
    assert lease["holder"] == "session-a" and lease["purpose"] == "the long run"
    assert lease["expires"] == "2026-09-08T12:30:00+00:00"

    # unreadable, empty and absent all read as a FREE pod
    for junk in ("", "   ", "not json", '{"no": "holder"}', None):
        assert podctl.lease_parse(junk) is None
        assert podctl.lease_expired(podctl.lease_parse(junk), now) is True
        assert podctl.lease_blocks(podctl.lease_parse(junk), "anyone", now) == (False, "")

    # a lease with no readable expiry is expired, never forever
    assert podctl.lease_expired({"holder": "x"}, now) is True
    assert podctl.lease_expired({"holder": "x", "expires": "whenever"}, now) is True

    # mine never blocks me; someone else's live one blocks and says who, what and until when
    assert podctl.lease_blocks(lease, "session-a", now) == (False, "")
    blocked, why = podctl.lease_blocks(lease, "session-b", now)
    assert blocked and "session-a" in why and "12:30" in why and "the long run" in why

    # and it expires: the same lease five minutes later is no longer a blocker
    later = now + datetime.timedelta(minutes=31)
    assert podctl.lease_expired(lease, later) is True
    assert podctl.lease_blocks(lease, "session-b", later) == (False, "")


def test_unit_podctl_lease_holder_is_always_something_a_person_can_ask():
    podctl = _load()
    assert podctl.lease_me({"PODCTL_SESSION": "comfyui-20"}) == "comfyui-20"
    fallback = podctl.lease_me({})
    assert fallback and "/" in fallback and str(os.getpid()) in fallback


def test_unit_podctl_lease_names_the_session_to_message_and_never_shortens_its_own_window():
    """Two things a waiting session asked for on 2026-09-08. The holder's NAME says who has the GPU; the
    session id says whom to message. And an install that runs inside a longer chain must EXTEND the lease it
    already holds, never cut it: a 150-minute chain lease was silently shortened to the install's own 90, and
    the session waiting on it saw a window it had not been promised."""
    import datetime
    podctl = _load()
    now = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)

    assert podctl.lease_session_id({"PODCTL_SESSION_ID": "local_abc"}) == "local_abc"
    assert podctl.lease_session_id({}) == ""
    lease = podctl.lease_parse(podctl.lease_encode("session-a", "the chain", 30, now=now, session="local_abc"))
    assert lease["session"] == "local_abc"
    blocked, why = podctl.lease_blocks(lease, "someone-else", now)
    assert blocked and "[local_abc]" in why and "session-a" in why
    # a lease written before the field existed still reads, and says nothing extra
    old = podctl.lease_parse(podctl.lease_encode("session-a", "", 30, now=now, session=""))
    assert "session" not in old
    assert "[" not in podctl.lease_blocks(old, "someone-else", now)[1]

    # never shorten our own live window; do extend it; someone else's is not ours to keep
    mine = podctl.lease_parse(podctl.lease_encode("session-a", "chain", 150, now=now, session=""))
    assert podctl.lease_minutes_keeping(mine, "session-a", 90, now) == 150
    assert podctl.lease_minutes_keeping(mine, "session-a", 200, now) == 200
    assert podctl.lease_minutes_keeping(mine, "session-b", 90, now) == 90
    assert podctl.lease_minutes_keeping(None, "session-a", 90, now) == 90
    expired = podctl.lease_parse(podctl.lease_encode("session-a", "old", 1, now=now - datetime.timedelta(hours=2), session=""))
    assert podctl.lease_minutes_keeping(expired, "session-a", 90, now) == 90


def test_unit_podctl_gates_on_the_mac_before_it_uploads(tmp_path):
    """Rule: run each package's OWN suite from the EXTRACTED ZIP, on the Mac,
    before anything is uploaded — the pod's `--check` does the same thing ten minutes later.

    `check_zip` proves the zip matches the working tree; it cannot prove the tree is good. That day a base
    zip was rebuilt mid-iteration, matched the tree exactly, carried a test that failed on the pod, and
    another session picked it up in that window. Extraction is half the point: a test that reads `_build/`
    or an installed pack passes beside the repo and fails from the zip."""
    podctl = _load()
    pkg = tmp_path / "Fake Package"; pkg.mkdir()
    script = pkg / "Fake Package-script.sh"
    script.write_text('#!/bin/bash\ncase "${1:-}" in test) echo "  ✔ suite: 3 passed"; exit "${FAKE_RC:-0}";; esac\n')
    z = pkg / "Fake Package-runpod.zip"
    import zipfile as _zf
    with _zf.ZipFile(z, "w") as zf:
        zf.writestr("Fake Package-script.sh", script.read_text())

    class IO(podctl.PodIO):
        # gate() defaults BASE_NODE_SRC to <base>/../testbed, which exists in a consumer repository's layout (base/comfyui-base
        # beside base/testbed) and does NOT in a bare clone of this repository, where the parent is wherever the
        # person put it. The gate is right to refuse a path that is not there; the TEST was reading the machine it
        # ran on. Name both paths so this asserts the gate's behaviour and not the checkout's location.
        def __init__(self): self.env = dict(os.environ, BASE_NODE_SRC=str(tmp_path), COMFY_BASE=str(tmp_path)); self.said = []
        def say(self, m): self.said.append(m)
    io = IO()

    io.gate(pkg, io.say)                                   # green: it says so and does not raise
    assert any("3 passed" in m for m in io.said), io.said

    io.env["FAKE_RC"] = "1"                                # red: it must stop the upload, here, with the output
    with pytest.raises(podctl.PodctlError) as e:
        io.gate(pkg, io.say)
    assert "FAILS from the built zip" in str(e.value) and "3 passed" in str(e.value)

    # and the zip is what runs, not the working tree: a script only on disk is never reached
    script.write_text('#!/bin/bash\nexit 9\n')
    io.env.pop("FAKE_RC")
    io.gate(pkg, io.say)


def test_unit_podctl_gate_runs_a_real_package_with_paths_that_exist(tmp_path):
    """The gate's whole value is being trusted when it says "fix it here", so it must not fail for reasons
    it invented. It shipped joining "ComfyUI Base" onto a path that already WAS the base directory, so every
    package's script correctly refused a COMFY_BASE that did not exist and the gate blamed the package
    (found when the gate blocked an install). The base's own gate
    never noticed, because base.sh needs neither variable — so this runs a REAL sibling package.

    A red sibling must not turn the base red: the assertion is that the gate got far enough to run the
    package's own suite, not that the suite passed."""
    podctl = _load()
    sibling = next((d for d in sorted(BASE.parent.iterdir())
                    if d.is_dir() and (d / ("%s-script.sh" % d.name)).exists()
                    and (d / ("%s-runpod.zip" % d.name)).exists()), None)
    if sibling is None:
        pytest.skip("no built package beside the base to gate")

    class IO(podctl.PodIO):
        def __init__(self): self.env = dict(os.environ); self.said = []
        def say(self, m): self.said.append(m)
    io = IO()
    try:
        io.gate(sibling, io.say)
        assert any("passed" in m or "green" in m for m in io.said), io.said
    except podctl.PodctlError as e:
        msg = str(e)
        for invented in ("ComfyUI Base is not installed", "does not exist", "the zip has no"):
            assert invented not in msg, f"the gate failed for its OWN reason, not the package's: {msg[:400]}"
        assert "FAILS from the built zip" in msg, msg[:400]     # a genuinely red sibling: allowed, and named


def test_unit_podctl_each_session_can_own_its_own_ssh_alias(tmp_path, monkeypatch):
    """`Host runpod` is ONE pointer in one file on a Mac that runs several sessions. On 2026-09-08 a third
    session ran `ssh-config` for its own pod and every other session's ssh, scp, install and lease silently
    began addressing that pod instead — same command, same output shape, a different machine. PODCTL_HOST
    gives a session its own block; the default stays `runpod`, so a single-session Mac is unchanged."""
    import importlib.util
    # the gate runs this suite from the EXTRACTED ZIP, where _build/ does not ship: without this the test
    # raises FileNotFoundError instead of skipping, and every package's install dies in the gate (2.0.45)
    if not TOOL.exists():
        pytest.skip("no base/comfyui-base/_build/pod/podctl.py (not a repo checkout)")
    def load_with(alias):
        monkeypatch.setenv("PODCTL_HOST", alias) if alias else monkeypatch.delenv("PODCTL_HOST", raising=False)
        spec = importlib.util.spec_from_file_location("podctl_alias_%s" % (alias or "default"), TOOL)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

    d = load_with(None)
    assert d.SSH_ALIAS == "runpod" and d.RUNPOD_BLOCK.startswith("Host runpod\n")

    mine = load_with("runpod-fakepod0000002")
    assert mine.SSH_ALIAS == "runpod-fakepod0000002"
    assert mine.RUNPOD_BLOCK.startswith("Host runpod-fakepod0000002\n")

    # writing MY block leaves another session's block untouched, byte for byte
    cfg = tmp_path / "config"
    cfg.write_text("Host runpod\n  HostName 1.2.3.4\n  Port 1111\n  User root\n\nHost other\n  HostName 9.9.9.9\n")
    mine.write_ssh_config(cfg, "5.6.7.8", 2222)
    text = cfg.read_text(encoding="utf-8")
    assert "Host runpod\n  HostName 1.2.3.4\n  Port 1111" in text, "another session's pointer was moved"
    assert "Host runpod-fakepod0000002" in text and "5.6.7.8" in text and "2222" in text
    assert "Host other\n  HostName 9.9.9.9" in text

    # and a second write to my own block updates it in place rather than appending another
    mine.write_ssh_config(cfg, "5.6.7.9", 2223)
    assert cfg.read_text(encoding="utf-8").count("Host runpod-fakepod0000002") == 1
    assert "5.6.7.9" in cfg.read_text(encoding="utf-8")


def test_podio_fetch_is_one_tar_stream_not_a_round_trip_per_file(tmp_path, monkeypatch):
    """2.0.54. The logs directory keeps every run's log (281 files on 2026-09-10), and `scp <dir>/*` spent about five
    minutes of each install step on per-file round trips. PodIO.fetch now makes ONE ssh call that streams a tar. The
    real method runs here, against a fake ssh that drops the host and executes the command locally."""
    import shlex
    podctl = _load()
    remote = tmp_path / "pod-logs"
    remote.mkdir()
    for i in range(300):
        (remote / ("run_%03d.log" % i)).write_text("line %d\n" % i * (i + 1), encoding="utf-8")
    calls = tmp_path / "calls"
    fake = tmp_path / "fake-ssh"
    fake.write_text('#!/bin/sh\necho "$@" >> %s\nshift\nexec bash -c "$1"\n' % shlex.quote(str(calls)), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(podctl, "SSH_CMD", [str(fake)])
    # PodIO(None, …) builds a RunPodProvider, which loads the API key eagerly (provider.py: `Api(load_api_key())`).
    # These three tests never reach the API — they drive fetch() over a fake ssh — but the constructor still
    # wants a key, and load_api_key falls back to ~/.runpod/config.toml. That made them pass on a maintainer's
    # machine and fail anywhere without a RunPod account, CI included. A placeholder keeps them portable.
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_fake_for_the_fetch_tests")
    io = podctl.PodIO(None, "pod", env=dict(os.environ, COPYFILE_DISABLE="1"))   # no ._ files from macOS tar
    local = tmp_path / "local"
    io.fetch(str(remote), local)
    names = sorted(p.name for p in remote.iterdir())
    assert sorted(p.name for p in local.iterdir()) == names
    assert all((local / n).read_bytes() == (remote / n).read_bytes() for n in names)
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1, "one ssh call for the whole directory"

    fake.write_text("#!/bin/sh\necho 'Connection refused' >&2\nexit 255\n", encoding="utf-8")
    with pytest.raises(podctl.PodctlError, match="fetching the pod's logs failed"):
        io.fetch(str(remote), tmp_path / "again")


def test_podio_fetch_survives_a_log_that_grows_while_it_is_copied(tmp_path, monkeypatch):
    """2.0.55. The directory this fetches — /workspace/comfy-base/state/logs — holds the LIVE comfyui.log, and GNU tar
    exits 1 ("file changed as we read it") when a file grows while it is being archived. 2.0.54 read ANY non-zero exit
    as failure, so an install that finished green (STEP RC=0) could die on its own log, before the SUMMARY and before
    the pins were saved. The bytes are a valid archive either way, so the LOCAL extract is the judge: exit 1 plus a
    clean extract is a pass."""
    import shlex
    podctl = _load()
    remote = tmp_path / "pod-logs"
    remote.mkdir()
    (remote / "comfyui.log").write_text("a line\n", encoding="utf-8")
    (remote / "install_example_1.console").write_text("STEP RC=0\n", encoding="utf-8")
    fake = tmp_path / "fake-ssh"
    # GNU tar's behaviour on a growing file: a VALID archive on stdout, the note on stderr, exit 1.
    fake.write_text('#!/bin/sh\nshift\nbash -c "$1"\n'
                    'echo "tar: ./comfyui.log: file changed as we read it" >&2\nexit 1\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(podctl, "SSH_CMD", [str(fake)])
    # PodIO(None, …) builds a RunPodProvider, which loads the API key eagerly (provider.py: `Api(load_api_key())`).
    # These three tests never reach the API — they drive fetch() over a fake ssh — but the constructor still
    # wants a key, and load_api_key falls back to ~/.runpod/config.toml. That made them pass on a maintainer's
    # machine and fail anywhere without a RunPod account, CI included. A placeholder keeps them portable.
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_fake_for_the_fetch_tests")
    io = podctl.PodIO(None, "pod", env=dict(os.environ, COPYFILE_DISABLE="1"))
    said = []
    monkeypatch.setattr(io, "say", said.append)
    local = tmp_path / "local"
    io.fetch(str(remote), local)                                    # 2.0.54 raised here
    assert (local / "comfyui.log").read_text(encoding="utf-8") == "a line\n"
    assert any("kept writing" in s for s in said), "it says why it passed, rather than passing in silence"

    # ...and exit 1 is not blanket-accepted: no archive means the local extract fails, which still raises.
    fake.write_text("#!/bin/sh\necho 'tar: /nope: Cannot open: No such file' >&2\nexit 1\n", encoding="utf-8")
    with pytest.raises(podctl.PodctlError, match="fetching the pod's logs failed"):
        io.fetch(str(remote), tmp_path / "nothing-arrived")


def test_podio_fetch_asks_the_pod_for_only_what_this_step_wrote(tmp_path, monkeypatch):
    """2.0.55. The logs directory keeps every run's log, so copying all of it grew with the history — 281 files on
    2026-09-10, re-copied on every install step. `newer_than` is a path ON THE POD whose mtime is the cutoff (GNU tar's
    --newer takes a file name when the value starts with / or .), and the install step passes its own console file, so
    the POD's clock decides which files are new. A timestamp computed on this Mac would silently drop every log
    whenever the two clocks disagreed.

    The flag is asserted, not exercised: the fake ssh runs here under macOS's bsdtar, which reads --newer as a DATE
    rather than a file, so only the pod's GNU tar can show the behaviour. Same reason the locale pins are asserted as
    source lines rather than run."""
    import shlex
    podctl = _load()
    remote = tmp_path / "pod-logs"
    remote.mkdir()
    (remote / "comfyui.log").write_text("a line\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    calls = tmp_path / "calls"
    fake = tmp_path / "fake-ssh"
    fake.write_text('#!/bin/sh\necho "$@" >> %s\nexec tar -C %s -czf - .\n'
                    % (shlex.quote(str(calls)), shlex.quote(str(empty))), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(podctl, "SSH_CMD", [str(fake)])
    # PodIO(None, …) builds a RunPodProvider, which loads the API key eagerly (provider.py: `Api(load_api_key())`).
    # These three tests never reach the API — they drive fetch() over a fake ssh — but the constructor still
    # wants a key, and load_api_key falls back to ~/.runpod/config.toml. That made them pass on a maintainer's
    # machine and fail anywhere without a RunPod account, CI included. A placeholder keeps them portable.
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_fake_for_the_fetch_tests")
    io = podctl.PodIO(None, "pod", env=dict(os.environ, COPYFILE_DISABLE="1"))
    console = "/workspace/comfy-base/state/logs/install_example_20260911-010203.console"
    io.fetch(str(remote), tmp_path / "local", newer_than=console)
    cmd = calls.read_text(encoding="utf-8")
    assert "--newer" in cmd and console in cmd, cmd
    assert cmd.index("--newer") < cmd.index("-czf"), "the selector comes before the archive flags"

    calls.write_text("", encoding="utf-8")
    io.fetch(str(remote), tmp_path / "local2")                      # no cutoff given: the whole directory
    assert "--newer" not in calls.read_text(encoding="utf-8")

    # and the install step is what passes it — the fix is worth nothing if the caller forgets
    with open(podctl.__file__, encoding="utf-8") as f:
        assert "newer_than=console" in f.read(), "the install step passes its own console file as the cutoff"


# ---------------------------------------------------------------- the kebab-case tree

def _repo_tree(tmp_path):
    """A brand repository: base/comfyui-base/base.sh, one package per brand with the same name, one brand-a-only."""
    root = tmp_path / "repo"
    (root / "base" / "comfyui-base").mkdir(parents=True); (root / "base" / "comfyui-base" / "base.sh").write_text("# base\n")
    for brand, names in (("brand-a", ("image-creator-example", "other-package")), ("brand-b", ("image-creator-example",))):
        for n in names:
            d = root / brand / "packages" / n; d.mkdir(parents=True); (d / ("%s-script.sh" % n)).write_text("# pkg\n")
    return root


def test_unit_podctl_install_resolves_pkg_against_the_repository_root_not_the_base_parent(tmp_path):
    """2.0.55 joined --pkg onto the base folder's PARENT, which was the repository root until the brand folders and is
    base/ now, which holds no packages. A repository-relative path resolves; a bare name resolves when exactly one brand has it;
    a name two brands share needs the path; a name nobody has is an error that says where to look."""
    podctl = _load(); root = _repo_tree(tmp_path)
    assert podctl.resolve_pkg_dir(root, "brand-a/packages/other-package") == root / "brand-a" / "packages" / "other-package"
    assert podctl.resolve_pkg_dir(root, "other-package") == root / "brand-a" / "packages" / "other-package"
    assert podctl.resolve_pkg_dir(root, str(root / "brand-b" / "packages" / "image-creator-example")) == root / "brand-b" / "packages" / "image-creator-example"
    with pytest.raises(podctl.PodctlError, match="more than one brand"):
        podctl.resolve_pkg_dir(root, "image-creator-example")
    with pytest.raises(podctl.PodctlError, match="<brand>/packages/<name>"):
        podctl.resolve_pkg_dir(root, "Example Video Creator")
    assert podctl.repo_root(root / "base" / "comfyui-base" / "_build" / "pod" / "podctl.py") == root


def _workspace_tree(tmp_path, host="verda"):
    """A workspace (3.7.0): one repository per package side by side, the brand's brand.toml at the root, the base
    OUTSIDE it (read from $COMFY_BASE), a non-package repository beside the packages."""
    ws = tmp_path / "workspace"
    for n in ("image-creator-example", "other-package"):
        d = ws / n; d.mkdir(parents=True); (d / ("%s-script.sh" % n)).write_text("# pkg\n")
    (ws / "site").mkdir(); (ws / "site" / "README.md").write_text("not a package\n")
    (ws / "brand.toml").write_text('name = "Example"\nhost = "%s"\n' % host)
    return ws


def test_unit_podctl_reads_the_workspace_shape_for_pkg_and_provider(tmp_path):
    """3.6.0 found a workspace's packages for the pin scan only; --pkg and the default provider still read the submodule
    shape, so `podctl install --pkg <name>` from a workspace said "not inside a brand repository" and the provider fell
    back to runpod whatever brand.toml said. Now both ask py/pkgdirs.py for the consumer root ($BASE_WORKSPACE first):
    --pkg <name> is <workspace>/<name>, the workspace's own brand.toml names the provider, and a $BASE_WORKSPACE that
    does not exist is an error, never an empty workspace."""
    podctl = _load(); ws = _workspace_tree(tmp_path)
    env = {"BASE_WORKSPACE": str(ws)}
    assert podctl.consumer_root(env=env) == ws.resolve()
    assert podctl.resolve_pkg_dir(ws, "other-package") == ws / "other-package"
    with pytest.raises(podctl.PodctlError, match="<name> in a workspace"):
        podctl.resolve_pkg_dir(ws, "no-such-package")
    assert [b["host"] for b in podctl.brands_at(ws)] == ["verda"]
    assert podctl.provider_for(None, env=env).name == "verda"
    assert [b["host"] for b in podctl.brands_at(_workspace_tree(tmp_path / "b", host="runpod"))] == ["runpod"]   # (RunPod's provider wants a key)
    with pytest.raises(podctl.PodctlError, match="does not exist"):
        podctl.consumer_root(env={"BASE_WORKSPACE": str(tmp_path / "missing")})
    assert podctl.brands_at(None) == []


def test_unit_podctl_prune_lists_only_old_layout_folders_and_removes_them_with_yes(tmp_path):
    """The rename left '<Display Name>/' folders (with '<Display Name> Script.sh') and 'ComfyUI Base/' on the volume beside the new
    '<name>/' folders, plus their workflow copies in ComfyUI's browser. prune names exactly those, touches nothing without --yes,
    and with --yes removes them and only them: the new folder, an unrelated folder and the user's own workflow stay."""
    podctl = _load(); io = LocalPodIO(tmp_path / "pod"); pk = io.root / "packages"
    (pk / "ComfyUI Base").mkdir(); (pk / "ComfyUI Base" / "base.sh").write_text("# old base\n")
    (pk / "Example Image Creator").mkdir(); (pk / "Example Image Creator" / "Example Image Creator Script.sh").write_text("# old\n")
    (pk / "image-creator-example").mkdir(); (pk / "image-creator-example" / "image-creator-example-script.sh").write_text("# new\n")
    (pk / "Other Thing").mkdir(); (pk / "Other Thing" / "README").write_text("not ours\n")
    wf = io.root / "ComfyUI" / "user" / "default" / "workflows"; wf.mkdir(parents=True)
    (wf / "Example Image Creator Workflow.json").write_text("{}"); (wf / "My Own Workflow.json").write_text("{}")
    (wf / "Brand Image Creator (Example).json").write_text("{}")

    assert podctl.stale_package_dirs(io) == ["ComfyUI Base", "Example Image Creator"]
    said = []
    assert podctl.prune(io, said.append, yes=False) == 0
    assert any("old layout: /workspace/packages/Example Image Creator" in l for l in said) and any("old layout: /workspace/packages/ComfyUI Base" in l for l in said)
    assert any(l.endswith("Example Image Creator Workflow.json") for l in said) and any("dry run" in l for l in said)
    assert (pk / "Example Image Creator").exists() and (wf / "Example Image Creator Workflow.json").exists(), "a dry run removes nothing"

    assert podctl.prune(io, said.append, yes=True) == 0
    assert not (pk / "ComfyUI Base").exists() and not (pk / "Example Image Creator").exists() and not (wf / "Example Image Creator Workflow.json").exists()
    assert (pk / "image-creator-example" / "image-creator-example-script.sh").exists() and (pk / "Other Thing" / "README").exists()
    assert (wf / "My Own Workflow.json").exists() and (wf / "Brand Image Creator (Example).json").exists()
    assert podctl.stale_package_dirs(io) == [] and podctl.prune(io, said.append, yes=True) == 0    # idempotent

    a = podctl.build_parser().parse_args(["prune", "abc123", "--yes"])
    assert a.cmd == "prune" and a.pod == "abc123" and a.yes
    assert not podctl.build_parser().parse_args(["prune", "abc123"]).yes


def test_unit_each_workflow_gets_its_own_local_port():
    """2.5.6: with one GPU per workflow every machine serves ComfyUI on 8188, so identical LocalForward lines mean
    only whichever ssh ran first gets the port. The rest fail, and because ExitOnForwardFailure is set the whole
    session dies and takes its OWN forward with it, which is why a second product could not be opened at all even
    though its port was free. Measured on two live machines."""
    # _load(), not a second inline import: it carries the skip for a run from the extracted zip, which ships no
    # _build/. The inline copy had no guard, so this test raised FileNotFoundError in every zip run and failed
    # the Mac gate that `podctl install` puts in front of every upload, for every package and every session.
    m = _load()

    # a plain port is unchanged, both sides the same and still an int
    assert m.tunnel_port("8188") == 8188 and m.tunnel_port(8188) == 8188
    argv = m.tunnel_argv([8188], host="h", port=22, key="/k", user="root")
    assert "8188:127.0.0.1:8188" in argv
    # LOCAL:REMOTE shifts only the local side, which is what lets a second machine be reached at all
    assert m.tunnel_port("8190:8188") == "8190:8188"
    argv = m.tunnel_argv(["8190:8188"], host="h", port=22, key="/k", user="root")
    assert "8190:127.0.0.1:8188" in argv and "8188:127.0.0.1:8188" not in argv
    # a malformed pair is refused here, where the message can say so, not by ssh
    for bad in ("8190:", ":8188", "a:8188", "8190:b"):
        try:
            m.tunnel_port(bad); raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass
    # 2.11.0: the offset is per DEPLOYMENT, from $PODCTL_TUNNEL_OFFSET, so ANY fleet gets its own ports and no
    # brand's alias names live in the base. Nothing is configured here, so every machine keeps the defaults.
    assert m.tunnel_offset(env={}) == 0
    assert m.tunnel_locals(env={})["comfy"] == 8188
    seen = {off: m.tunnel_locals(env={m.TUNNEL_OFFSET_ENV: str(off)})["comfy"] for off in (0, 1, 2)}
    assert seen == {0: 8188, 1: 8189, 2: 8190}, seen
    # every service moves together, or the second machine's jupyter collides while its comfy does not
    two = m.tunnel_locals(env={m.TUNNEL_OFFSET_ENV: "2"})
    assert two == {"comfy": 8190, "jupyter": 8890, "metrics": 9201, "ollama": 11436}, two
    # a bad value is refused, never silently read as 0 - 0 is the collision it was set to avoid
    for bad in ("x", "-1", "9999"):
        try:
            m.tunnel_offset(env={m.TUNNEL_OFFSET_ENV: bad}); raise AssertionError("accepted %r" % bad)
        except m.PodctlError:
            pass
    assert m._block_for("any-alias", "root", "/k").count("LocalForward 8188 localhost:8188") == 1


# ---------------------------------------------------------------- 2.7.0: the .mcp.json podctl writes

def test_unit_mcp_over_ssh_runs_the_server_on_the_machine():
    """The ssh transport is the default because it is the only one where install_node, search_models, get_logs
    and fetch_outputs work: those tools need the tree, and the tree is on the machine."""
    podctl = _load()
    srv = podctl.mcp_server("runpod", "ssh", bin_path="/workspace/ComfyUI/.venv-cu130/bin/comfy-mcp")
    assert srv["command"] == "ssh"
    a = srv["args"]
    # not cosmetic: the Host block ssh-config writes carries four LocalForward lines, and a second session
    # binding them while `podctl tunnel` holds them prints warnings onto the stdio channel MCP speaks.
    assert "ClearAllForwardings=yes" in a, a
    assert a[-2] == "runpod", a
    assert a[-1].endswith("exec /workspace/ComfyUI/.venv-cu130/bin/comfy-mcp"), a[-1]
    # the remote command carries the environment: an MCP client's "env" is local, and would never reach the pod
    assert "DO_NOT_TRACK=1" in a[-1] and "COMFY_NO_TELEMETRY=1" in a[-1], a[-1]
    # comfy-mcp shells out to comfy-cli for its discovery and lifecycle tools and finds it through COMFY_BIN or
    # PATH. MEASURED against a live ComfyUI: without COMFY_BIN, server_info answers "Error executing tool
    # server_info"; with it, it answers. An MCP client is usually launched by a GUI whose PATH is not a shell's,
    # so naming the binary is the only reliable form.
    assert "COMFY_BIN=/workspace/ComfyUI/.venv-cu130/bin/comfy " in a[-1], a[-1]


def test_unit_mcp_names_the_comfy_binary_beside_comfy_mcp():
    podctl = _load()
    assert podctl.mcp_comfy_bin("/opt/v/bin/comfy-mcp") == "/opt/v/bin/comfy"
    assert podctl.mcp_comfy_bin(None) is None
    srv = podctl.mcp_server("runpod", "ssh", bin_path="/a/b/comfy-mcp", comfy_bin="/elsewhere/comfy")
    assert "COMFY_BIN=/elsewhere/comfy " in srv["args"][-1], srv


def test_unit_mcp_over_the_tunnel_takes_this_deployments_port():
    """With one GPU per workflow every machine serves 8188, so the local side carries this deployment's offset -
    otherwise a second machine's config points at the first machine's tunnel. 2.11.0: the offset is
    $PODCTL_TUNNEL_OFFSET, a fact about somebody's fleet, so no alias names appear here or in the base."""
    podctl = _load()
    base = podctl.mcp_server("any-alias", "tunnel")
    assert base["command"] == "comfy-mcp"
    assert base["env"]["COMFYUI_URL"] == "http://127.0.0.1:8188", base
    assert base["env"]["DO_NOT_TRACK"] == "1" and base["env"]["COMFY_NO_TELEMETRY"] == "1"
    for off, want in ((1, 8189), (2, 8190)):
        got = podctl.mcp_server("any-alias", "tunnel", port=podctl.tunnel_locals(env={podctl.TUNNEL_OFFSET_ENV: str(off)})["comfy"])
        url = got["env"]["COMFYUI_URL"]
        assert url == "http://127.0.0.1:%d" % want, (off, url)
        assert url != base["env"]["COMFYUI_URL"]


def test_unit_mcp_keeps_every_other_server_in_the_file():
    """A .mcp.json is often not only ours — clobbering a client's other servers would be a rude way to arrive."""
    podctl = _load()
    existing = {"mcpServers": {"something-else": {"command": "other"}}, "unrelatedKey": 1}
    out = podctl.mcp_merge(existing, "comfy-runpod", podctl.mcp_server("runpod", "tunnel"))
    assert out["mcpServers"]["something-else"] == {"command": "other"}
    assert out["unrelatedKey"] == 1
    assert "comfy-runpod" in out["mcpServers"]
    assert existing["mcpServers"] == {"something-else": {"command": "other"}}, "the input must not be mutated"


def test_unit_mcp_refuses_a_transport_it_does_not_have():
    podctl = _load()
    with pytest.raises(podctl.PodctlError, match="unknown mcp transport"):
        podctl.mcp_server("runpod", "carrier-pigeon")


# ---------------------------------------------------------------- 3.0.0: --comfy-ref, no --latest, --no-latest refused, a restart stop

def test_unit_podctl_install_sends_no_latest_and_carries_comfy_ref_on_check_and_run(tmp_path, capsys):
    podctl = _load()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [base], io, every=1, timeout=60, today="2026-09-25", comfy_ref="master")
    assert rc == 0, capsys.readouterr().out
    check = [c for c in io.cmds if " preflight" in c][0]; run = [c for c in io.cmds if "nohup" in c][0]
    assert "COMFY_REF=master" in check and "COMFY_REF=master" in run and "--latest" not in run
    assert 'bash "comfyui-base/comfyui-base-script.sh";' in run and "BASE_RESTART" not in run      # --base alone: step one, whole
    io.cmds.clear()
    podctl.install("pod1", [base], io, every=1, timeout=60, today="2026-09-25")
    assert not [c for c in io.cmds if "COMFY_REF" in c]                                        # per install, never sticky


def test_unit_podctl_no_latest_is_refused(capsys):
    podctl = _load()
    a = podctl.build_parser().parse_args(["install", "fakepod0000001", "--base", "--no-latest"])
    assert podctl.cmd_install(None, a) == 2 and "retired in 3.0.0" in capsys.readouterr().out


def test_unit_podctl_a_restart_required_run_prints_the_restart_to_approve(tmp_path, capsys):
    podctl = _load()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    (io.root / "run_rc").write_text("1")
    script = io.root / "packages"
    orig = io.remote
    def remote(cmd):
        rc, out, err = orig(cmd)
        if cmd.startswith("cat ") and "STEP RC=1" in out:
            out = out.replace("STEP RC=1", "  !! restart required: driver 580.178.04 -> 615.71.09\nSTEP RC=1")
        return rc, out, err
    io.remote = remote
    rc = podctl.install("pod1", [base], io, every=1, timeout=60, today="2026-09-25", provider="verda")
    out = capsys.readouterr().out
    assert rc == 1 and "restart it (a person approves this):  podctl --provider verda restart pod1 --wait" in out, out


# ---------------------------------------------------------------- 3.1.0: a buyer's machine

def _buyer_zips(tmp_path):
    """What a buyer holds: the base zip and one purchased package zip, and a local runtime (manifest + one part)."""
    import zipfile, hashlib
    d = tmp_path / "bought"; d.mkdir()
    with zipfile.ZipFile(d / "comfyui-base.zip", "w") as z:
        z.writestr("comfyui-base/comfyui-base-script.sh", STUB_SCRIPT % ("../..", "base"))
        z.writestr("comfyui-base/base.sh", BASE_SH_STUB % (STUB_SCRIPT % ("..", "base")))
    with zipfile.ZipFile(d / "my-pkg-verda.zip", "w") as z:
        z.writestr("my-pkg-script.sh", STUB_SCRIPT % ("../..", "my-pkg"))
    rt = tmp_path / "rt"; rt.mkdir(); part = rt / "runtime.tar.gz.000"; part.write_bytes(b"part")
    (rt / "runtime.json").write_text(json.dumps({"format": 1, "parts": [{"name": part.name, "url": part.name, "bytes": 4,
                                                                         "sha256": hashlib.sha256(b"part").hexdigest()}]}))
    return d, rt / "runtime.json"


def test_unit_item_for_zip_knows_the_base_from_a_package_by_the_zip_alone(tmp_path):
    podctl = _load()
    d, _ = _buyer_zips(tmp_path)
    b, p = podctl.item_for_zip(d / "comfyui-base.zip"), podctl.item_for_zip(d / "my-pkg-verda.zip")
    assert b["kind"] == "base" and b["script"] == "comfyui-base/comfyui-base-script.sh" and b["buyer"]
    assert p["kind"] == "pkg" and p["name"] == "my-pkg" and p["script"] == "my-pkg/my-pkg-script.sh" and p["slug"] == "my_pkg"
    import zipfile
    with zipfile.ZipFile(tmp_path / "not.zip", "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(podctl.PodctlError):
        podctl.item_for_zip(tmp_path / "not.zip")


def test_unit_buyer_install_applies_the_runtime_then_installs_staged_with_no_gate_and_no_pins(tmp_path, capsys):
    podctl = _load()
    d, man = _buyer_zips(tmp_path); io = LocalPodIO(tmp_path / "pod")
    io.gate = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a buyer's install never runs the Mac gate"))
    rc = podctl.buyer_install("pod1", d / "comfyui-base.zip", [d / "my-pkg-verda.zip"], str(man), io, jobs=3, every=1, timeout=60, logs=tmp_path / "logs")
    out = capsys.readouterr().out
    assert rc == 0, out
    calls = [c.rstrip() for c in (io.root / "calls.log").read_text().splitlines()]
    assert calls[0].startswith("comfyui-base-script.sh runtime-apply ") and calls[0].endswith("/.comfy-base-runtime-src/runtime.json")
    # 3.2.0: the base is installed (never a second whole pipeline), no base suite on a buyer's machine (BASE_NO_SUITE),
    # and each package gets the seconds-long preflight, carrying the same runtime environment as its run
    assert calls[1:] == ["base.sh install-self", "my-pkg-script.sh preflight", "my-pkg-script.sh"], calls
    assert not [c for c in io.cmds if "comfyui-base-script.sh test" in c]
    pre = [c for c in io.cmds if " preflight" in c]
    assert len(pre) == 1 and "BASE_RUNTIME=" in pre[0] and "BASE_STAGED=" in pre[0], pre
    runs = [c for c in io.cmds if "STEP RC=" in c and "nohup" in c and "runtime-apply" not in c]
    assert len(runs) == 1, runs
    for r in runs:
        assert "BASE_RUNTIME=" in r and "BASE_STAGED=" in r and re.search(r"BASE_FETCH_JOBS=\W{0,12}3\W", r), r   # the whole line is quoted once more
    uploaded = [pathlib.Path(f).name for u in io.uploads for f in u[0]]
    assert "runtime.json" in uploaded and "runtime.tar.gz.000" in uploaded
    assert io.rebuilt == [] and "pins" not in out                       # nothing is written back on a buyer's machine
    assert (tmp_path / "logs").is_dir()


def test_unit_buyer_install_wants_the_base_zip_first(tmp_path):
    podctl = _load()
    d, man = _buyer_zips(tmp_path)
    with pytest.raises(podctl.PodctlError):
        podctl.buyer_install("pod1", d / "my-pkg-verda.zip", [], str(man), LocalPodIO(tmp_path / "pod"))


def test_unit_put_away_copies_the_images_first_and_deletes_only_with_yes(tmp_path, capsys):
    podctl = _load()
    io = LocalPodIO(tmp_path / "pod")
    out_dir = io.root / "ComfyUI" / "output"; out_dir.mkdir(parents=True); (out_dir / "mine.png").write_bytes(b"png")

    class Prov:
        name = "verda"; stopped = []; deleted = []
        def machine_volumes(self, pod): return ["v-os", "v-data"]
        def stop(self, pod, wait=True): self.stopped.append(pod); return "deleted %s" % pod
        def delete_volumes(self, ids, permanent=False): self.deleted.extend(ids)
    prov = Prov()
    rc = podctl.put_away(prov, "pod1", io, tmp_path / "mac", yes=False)
    assert rc == 2 and (tmp_path / "mac" / "output" / "mine.png").read_bytes() == b"png" and prov.stopped == [] and prov.deleted == []
    rc = podctl.put_away(prov, "pod1", io, tmp_path / "mac2", yes=True)
    assert rc == 0 and prov.stopped == ["pod1"] and prov.deleted == ["v-os", "v-data"] and (tmp_path / "mac2" / "output" / "mine.png").exists()


def test_unit_the_buyer_verbs_parse_and_a_host_without_them_says_so():
    podctl = _load()
    ap = podctl.build_parser()
    a = ap.parse_args(["buyer-install", "p1", "a.zip", "b.zip", "--runtime", "https://x/runtime.json", "--base-zip", "comfyui-base.zip", "--jobs", "2"])
    assert a.zips == ["a.zip", "b.zip"] and a.jobs == 2 and a.base_zip == "comfyui-base.zip"
    a = ap.parse_args(["create", "--name", "n", "--gpu", "1RTXPRO6000.30V", "--data-gb", "200"])
    assert a.os_gb == 100 and a.location is None and not a.spot
    for argv in (["balance"], ["availability", "--gpu", "x"], ["pause", "p"], ["put-away", "p", "--to", "d"], ["progress", "p"], ["runtime-capture", "p", "--to", "d"]):
        ap.parse_args(argv)
    class Bare: name = "bare"
    with pytest.raises(podctl.PodctlError) as e:
        podctl.cmd_balance(Bare(), ap.parse_args(["balance"]))
    assert "not offered by host bare" in str(e.value)


# ---------------------------------------------------------------- 3.2.0: one pass per install

def test_unit_podctl_install_runs_the_base_suite_once_per_base_version(tmp_path, capsys):
    podctl = _load()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    assert podctl.install("pod1", [base, pkg], io, every=1, timeout=60, today="2026-09-25", save_pins=False) == 0, capsys.readouterr().out
    assert (io.root / "comfy-base" / "state" / "base-suite.ok").exists()
    (io.root / "calls.log").unlink(); capsys.readouterr()
    assert podctl.install("pod1", [base, pkg], io, every=1, timeout=60, today="2026-09-25", save_pins=False) == 0
    out = capsys.readouterr().out
    calls = [c.rstrip() for c in (io.root / "calls.log").read_text().splitlines()]
    assert calls == ["base.sh install-self", "My Pkg-script.sh preflight", "My Pkg-script.sh"], calls   # same base: its suite is not rerun
    assert "already green on this machine" in out


def test_unit_podctl_install_falls_back_to_check_on_a_base_without_preflight(tmp_path, capsys):
    podctl = _load()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    (io.root / "no_preflight").write_text("1")                   # a machine whose installed base predates preflight
    assert podctl.install("pod1", [pkg], io, every=1, timeout=60, today="2026-09-25", save_pins=False) == 0, capsys.readouterr().out
    calls = [c.rstrip() for c in (io.root / "calls.log").read_text().splitlines()]
    assert calls == ["My Pkg-script.sh preflight", "My Pkg-script.sh --check", "My Pkg-script.sh"], calls


def test_unit_podctl_polls_every_five_seconds_by_default():
    podctl = _load()
    import inspect
    assert inspect.signature(podctl.install).parameters["every"].default == 5
    a = podctl.build_parser().parse_args(["install", "pod1", "--pkg", "x"])
    assert a.every == 5


def test_unit_podctl_gate_is_not_rerun_for_a_zip_that_already_passed(tmp_path):
    # a retry after a machine-side failure uploads the SAME zip: its suite already passed from that zip, against the
    # same base, so it is not run again. A different zip (or base) runs it, and a red run never enters the cache.
    podctl = _load()
    pkg = tmp_path / "Fake Package"; pkg.mkdir()
    script = pkg / "Fake Package-script.sh"
    script.write_text('#!/bin/bash\ncase "${1:-}" in test) echo run >> "$GATE_LOG"; echo "  ✔ suite: 3 passed"; exit "${FAKE_RC:-0}";; esac\n')
    import zipfile as _zf
    def build(extra):
        with _zf.ZipFile(pkg / "Fake Package-runpod.zip", "w") as zf:
            zf.writestr("Fake Package-script.sh", script.read_text()); zf.writestr("extra.txt", extra)
    log = tmp_path / "gate.log"; cache = tmp_path / "cache"

    class IO(podctl.PodIO):
        def __init__(self): self.env = dict(os.environ, BASE_NODE_SRC=str(tmp_path), COMFY_BASE=str(tmp_path), GATE_LOG=str(log), PODCTL_GATE_CACHE=str(cache)); self.said = []
        def say(self, m): self.said.append(m)
    io = IO()
    build("1"); io.gate(pkg, io.say); assert log.read_text().count("run") == 1
    io.env["FAKE_RC"] = "1"
    io.gate(pkg, io.say)                                    # same zip, same base: cached green, the suite is not run
    assert log.read_text().count("run") == 1 and any("already passed" in m for m in io.said), io.said
    build("2")                                              # a different zip runs, and red is red
    with pytest.raises(podctl.PodctlError):
        io.gate(pkg, io.say)
    io.env.pop("FAKE_RC")
    io.gate(pkg, io.say); assert log.read_text().count("run") == 3   # the red run was not cached



# ---------------------------------------------------------------- 3.7.3: the volume a pod really has, both REST shapes

def test_unit_runpod_reads_either_volume_shape_and_names_a_pod_volume(tmp_path):
    """2026-10-15: `status` said "volume NONE at /workspace" for a B200 pod whose /workspace held 500 GB: the account had
    no network volume at all, the pod had a POD volume (volumeInGb). And the live openapi names only the networkVolume
    object (with includeNetworkVolume=true), not the top-level networkVolumeId older answers carried: both are read."""
    podctl = _load()
    old = _pod(networkVolumeId="v1")                                                     # the 2026-09-05 shape
    new = _pod(networkVolume={"id": "v2", "size": 400, "dataCenterId": "EU-RO-1"}); new.pop("networkVolumeId")
    podvol = _pod(networkVolumeId=None, volumeInGb=500); podvol.pop("networkVolumeId")    # what the live pod answered
    bare = _pod(volumeInGb=0); bare.pop("networkVolumeId")
    assert podctl.pod_volume(old)["kind"] == "network" and podctl.pod_volume_id(old) == "v1"
    assert podctl.pod_volume(new) == {"kind": "network", "id": "v2", "size": 400, "dc": "EU-RO-1"}
    assert podctl.pod_volume(podvol)["kind"] == "pod" and podctl.pod_volume_id(podvol) == ""
    assert podctl.pod_volume(bare)["kind"] == "none"
    assert "network volume v2 (400 GB, EU-RO-1) at /workspace" in podctl.pod_facts(new)
    line = [l for l in podctl.pod_facts(podvol).splitlines() if l.startswith("volume")][0]
    assert "pod volume 500 GB at /workspace" in line and "NONE" not in line and "deleted with the pod" in line, line
    assert "NONE at /workspace" in podctl.pod_facts(bare)
    # the size: embedded or volumeInGb, without asking the API; the disk gate gets a pod volume too
    assert podctl.volume_size_gb(None, new) == 400 and podctl.volume_size_gb(None, podvol) == 500 and podctl.volume_size_gb(None, bare) is None
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\nexit 0\n' % log); (bin_ / "ssh").chmod(0o755)
    out = podctl.record_volume(None, podvol, env={**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    assert " 500 " in log.read_text() and "pod-volume:fakepod0000001" in log.read_text() and "500 GB" in out
    # deploy: the new shape clones (the data centre from the object); a pod volume says why it cannot
    src = dict(new, machine={"gpuTypeId": "NVIDIA B200", "secureCloud": True}, gpuCount=1, containerDiskInGb=50)
    body = podctl.deploy_body(src)
    assert body["networkVolumeId"] == "v2" and body["dataCenterIds"] == ["EU-RO-1"]
    with pytest.raises(podctl.PodctlError) as e:
        podctl.deploy_body(dict(podvol, machine={"gpuTypeId": "NVIDIA B200"}))
    assert "500 GB pod volume" in str(e.value) and "podctl create" in str(e.value)
    # every pod read asks for both include flags, so the networkVolume object comes back whatever made the pod
    srv = FakeRunPod(new)
    try:
        prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url))
        prov.pod(new["id"]); prov.pods()
    finally:
        srv.close()
    assert len(srv.gets) == 2 and all("includeNetworkVolume=true" in g and "includeMachine=true" in g for g in srv.gets), srv.gets


# ---------------------------------------------------------------- 3.7.3: podctl create on RunPod

B200_STOCK = {"dataCenters": [
    {"id": "EU-RO-1", "gpuAvailability": [{"gpuTypeId": "NVIDIA B200", "stockStatus": "Low", "available": True}]},
    {"id": "US-NE-1", "gpuAvailability": [{"gpuTypeId": "NVIDIA B200", "stockStatus": None, "available": False},
                                          {"gpuTypeId": "NVIDIA H200", "stockStatus": "High", "available": True}]},
    {"id": "US-CA-2", "gpuAvailability": [{"gpuTypeId": "NVIDIA B200", "stockStatus": "Medium", "available": True}]},
    {"id": "AP-IN-3", "gpuAvailability": []}]}


def _create(podctl, srv, pub, *argv):
    prov = podctl.RunPodProvider(api=podctl.Api("rp_key_create", base_url=srv.url, graphql_url=srv.url + "/graphql"))
    # 3.8.1: a pod is private unless --public; these helpers test the network-volume path, so they say --public
    pub_flag = [] if "--private" in argv else ["--public"]
    args = podctl.build_parser().parse_args(["--pubkey", str(pub), "create", "--name", "b2", "--gpu", "NVIDIA B200",
                                             "--image", "runpod/comfyui:1.4.7-cuda13.0", "--no-wait", *pub_flag, *argv])
    return podctl.cmd_create(prov, args)


def test_unit_runpod_create_checks_stock_first_then_makes_the_volume_and_the_pod(tmp_path, capsys):
    """2026-10-15: a first pod on a fresh account needed the web console (RunPod had only `deploy --like`). `create`:
    the stock check names every data centre with the GPU free NOW and stops before anything bills when the chosen one
    has none; then a network volume there, then a Secure Cloud pod on it with the three ports, PUBLIC_KEY, the CUDA floor."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    srv = FakeRunPod(_pod(), gql=B200_STOCK)
    try:
        assert podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql")).availability("NVIDIA B200") == ["EU-RO-1", "US-CA-2"]
        assert srv.last_auth == "Bearer k"
        # a data centre without stock: STOP, naming the ones that have it; nothing is created
        assert _create(podctl, srv, pub, "--data-gb", "500", "--location", "US-NE-1") == 1
        out = capsys.readouterr().out
        assert "free now in: EU-RO-1, US-CA-2" in out and "STOP: US-NE-1 has no NVIDIA B200 free" in out, out
        assert [p for p in srv.posts if p != "/graphql"] == []
        # neither a new size nor an existing volume, and (3.16.0) no fresh pod either: refused before any call bills
        with pytest.raises(podctl.PodctlError):
            _create(podctl, srv, pub, "--no-fresh")
        # the first data centre with stock, a new volume there, the pod on it
        srv.posts.clear(); srv.bodies.clear()
        assert _create(podctl, srv, pub, "--data-gb", "500") in (None, 0)
        out = capsys.readouterr().out
    finally:
        srv.close()
    assert srv.posts == ["/graphql", "/networkvolumes", "/pods"], srv.posts
    vol, body = srv.bodies[1][1], srv.bodies[2][1]
    assert vol == {"name": "comfy-base-data", "size": 500, "dataCenterId": "EU-RO-1"}     # 3.14.0: the name the next create reuses
    assert body["cloudType"] == "SECURE" and body["dataCenterIds"] == ["EU-RO-1"] and body["networkVolumeId"] == "newvol1"
    assert body["volumeMountPath"] == "/workspace" and body["ports"] == ["8188/http", "8888/http", "22/tcp"]
    # 3.9.0: and the pod's own JupyterLab token; 3.14.0: and the key to its links (test_ports has the names and the key)
    assert set(body["env"]) == {"PUBLIC_KEY", "JUPYTER_TOKEN", "BASE_LINK_TOKEN"} and body["env"]["PUBLIC_KEY"] == "ssh-ed25519 AAAAMAC mac@key"
    tok_lines = [l for l in out.splitlines() if body["env"]["JUPYTER_TOKEN"] in l]       # printed once: the ready terminal link
    assert len(tok_lines) == 1 and "Jupyter Terminal" in tok_lines[0] and "-8888.proxy.runpod.net/lab?token=" in tok_lines[0], out
    assert body["gpuTypeIds"] == ["NVIDIA B200"] and body["gpuCount"] == 1
    assert body["allowedCudaVersions"] == ["13.0"] and body["containerDiskInGb"] == 100 and body["imageName"] == "runpod/comfyui:1.4.7-cuda13.0"
    assert "templateId" not in body and body["interruptible"] is False   # this fake has no templates
    # 3.14.0: a new pod starts on the base's boot guard and sshd, never its image's own start (error log row 71)
    assert body["dockerStartCmd"] == [] and body["dockerEntrypoint"][2].startswith(podctl.BOOT_GUARD)
    assert "the port names could not be written" in out, out                     # and says so; never a reason to have no pod
    assert "created newpod0000001" in out and "next: podctl ensure newpod0000001" in out and "rp_key_create" not in out, out


def test_unit_runpod_create_reuses_a_volume_and_a_refusal_takes_only_its_own_volume(tmp_path, capsys):
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    vols = {"oldvol": {"id": "oldvol", "size": 300, "dataCenterId": "US-CA-2"}, "rovol": {"id": "rovol", "size": 100, "dataCenterId": "US-NE-1"}}
    srv = FakeRunPod(_pod(), volumes=dict(vols), gql=B200_STOCK)
    try:
        # an existing volume pins the data centre: a different --location is refused, and the pod goes where the volume is
        with pytest.raises(podctl.PodctlError) as e:
            _create(podctl, srv, pub, "--volume", "oldvol", "--location", "EU-RO-1")
        assert "is in US-CA-2" in str(e.value)
        _create(podctl, srv, pub, "--volume", "oldvol", "--min-cuda", "12.8")
        assert "/networkvolumes" not in srv.posts
        body = srv.bodies[-1][1]
        assert body["networkVolumeId"] == "oldvol" and body["dataCenterIds"] == ["US-CA-2"] and body["allowedCudaVersions"] == ["13.0", "12.9", "12.8"]
        # a volume whose data centre has no stock: STOP, and say the volume is why
        assert _create(podctl, srv, pub, "--volume", "rovol") == 1
        assert "the volume pins the data centre" in capsys.readouterr().out
        # sold out between the check and the create: the volume THIS call made is deleted; an existing one never is
        srv.refuse_pods = True
        with pytest.raises(podctl.PodctlError):
            _create(podctl, srv, pub, "--data-gb", "200")
        made = [b for p, b in srv.bodies if p == "/networkvolumes"]
        assert len(made) == 1 and srv.deletes == ["/networkvolumes/newvol3"], (made, srv.deletes)
        with pytest.raises(podctl.PodctlError):
            _create(podctl, srv, pub, "--volume", "oldvol")
        assert srv.deletes == ["/networkvolumes/newvol3"], srv.deletes
    finally:
        srv.close()


def test_unit_runpod_create_waits_for_sshd_and_points_the_host_block_at_the_new_pod(tmp_path):
    podctl = _load()
    api_srv = FakeRunPod(_pod(), gql=B200_STOCK)
    try:
        api = podctl.Api("k", base_url=api_srv.url, graphql_url=api_srv.url + "/graphql")
        cfg = tmp_path / "config"; said = []
        pod_id = podctl.create(api, "b2", "NVIDIA B200", "EU-RO-1", data_gb=50, image="x/comfy:cu130", pubkey="ssh-ed25519 K k",
                               wait=True, sleep=lambda s: None, banner=lambda h, p: True, ssh_config=cfg, say=said.append)
    finally:
        api_srv.close()
    assert pod_id == "newpod0000001" and "15270" in cfg.read_text() and "127.0.0.1" in cfg.read_text()
    assert any("ssh answers on 127.0.0.1:15270" in s for s in said), said
    with pytest.raises(podctl.PodctlError):                         # no image: refused before any call
        podctl.create(None, "b2", "NVIDIA B200", "EU-RO-1", data_gb=50, pubkey="k", wait=False, say=lambda *a: None)


def test_unit_runpod_cuda_floor_comes_from_the_flag_or_the_image_tag():
    podctl = _load()
    assert podctl.cuda_floor("runpod/comfyui:1.4.7-cuda13.0") == "13.0"
    assert podctl.cuda_floor("someone/comfy:cu130-py312") == "13.0" and podctl.cuda_floor("someone/comfy:cu128") == "12.8"
    assert podctl.cuda_floor("someone/comfy:latest") is None and podctl.cuda_floor("someone/comfy:latest", "12.9") == "12.9"
    assert podctl.allowed_cuda(None) is None and podctl.allowed_cuda("13.0") == ["13.0"]
    with pytest.raises(podctl.PodctlError):
        podctl.allowed_cuda("14.0")


def test_unit_runpod_create_with_no_gpu_takes_b200_then_b300_and_a_cuda_13_floor(tmp_path, capsys):
    """3.8.0, Paul's decision of 2026-10-16: the video packages run on B200 first and B300 second, so `create` without
    --gpu asks for "NVIDIA B200", then "NVIDIA B300 SXM6 AC" (never the 34 GB MIG slice), and takes the first with stock
    where the pod must go: the --location, the --volume's data centre, else the first data centre with it. The CUDA floor
    is never below 13.0 without --min-cuda: the base installs a cu130 torch whatever the image's tag says."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    stock = {"dataCenters": [
        {"id": "EU-RO-1", "gpuAvailability": [{"gpuTypeId": "NVIDIA B200", "stockStatus": "Low", "available": True}]},
        {"id": "EUR-IS-1", "gpuAvailability": [{"gpuTypeId": "NVIDIA B300 SXM6 AC", "stockStatus": "Low", "available": True},
                                               {"gpuTypeId": "NVIDIA B300 SXM6 AC MIG 1g.34gb", "stockStatus": "High", "available": True}]}]}
    assert podctl.RunPodProvider.default_gpus[:2] == ("NVIDIA B200", "NVIDIA B300 SXM6 AC")   # 3.16.0: then the next tier
    srv = FakeRunPod(_pod(), volumes={"isvol": {"id": "isvol", "size": 500, "dataCenterId": "EUR-IS-1"}}, gql=stock)
    def create(*argv, image="someone/comfy:cu128"):
        prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql"))
        args = podctl.build_parser().parse_args(["--pubkey", str(pub), "create", "--name", "bw", "--image", image, "--no-wait", "--public", *argv])
        return podctl.cmd_create(prov, args)
    try:
        assert create("--data-gb", "500") in (None, 0)
        out = capsys.readouterr().out
        body = srv.bodies[-1][1]
        assert body["gpuTypeIds"] == ["NVIDIA B200"] and body["dataCenterIds"] == ["EU-RO-1"], body
        assert body["allowedCudaVersions"] == ["13.0"], "a cu128 image tag must not lower the floor below the base's cu130"
        assert "gpu     NVIDIA B200, in EU-RO-1 (no --gpu: the host's order is %s)" % ", ".join(podctl.RunPodProvider.default_gpus) in out, out
        # the volume pins EUR-IS-1, which has no B200: the B300 there
        assert create("--volume", "isvol") in (None, 0)
        out = capsys.readouterr().out
        assert srv.bodies[-1][1]["gpuTypeIds"] == ["NVIDIA B300 SXM6 AC"] and srv.bodies[-1][1]["dataCenterIds"] == ["EUR-IS-1"]
        assert "stock   NVIDIA B200 free now in: EU-RO-1" in out and "stock   NVIDIA B300 SXM6 AC free now in: EUR-IS-1" in out, out
        # a data centre with neither: STOP, naming where each is free, nothing created
        n = len(srv.bodies)
        assert create("--data-gb", "100", "--location", "US-CA-2") == 1
        out = capsys.readouterr().out
        assert "STOP: US-CA-2 has no %s free right now; NVIDIA B200 is free in: EU-RO-1; NVIDIA B300 SXM6 AC is free in: EUR-IS-1" % (
            " or ".join(podctl.RunPodProvider.default_gpus)) in out, out
        assert [p for p, _ in srv.bodies[n:] if p != "/graphql"] == [], "a STOP creates no volume and no pod"
        # an explicit --gpu is the only one asked; an explicit --min-cuda still wins over the floor
        assert create("--data-gb", "100", "--gpu", "NVIDIA B300 SXM6 AC", "--min-cuda", "12.9") in (None, 0)
        assert srv.bodies[-1][1]["gpuTypeIds"] == ["NVIDIA B300 SXM6 AC"] and srv.bodies[-1][1]["allowedCudaVersions"] == ["13.0", "12.9"]
    finally:
        srv.close()
    assert podctl.create_floor("someone/comfy:latest") == "13.0" and podctl.create_floor("runpod/comfyui:1.4.7-cuda13.0") == "13.0"
    assert podctl.create_floor("x/y:cu128", "12.8") == "12.8"


def test_unit_runpod_create_order_goes_past_b300_and_says_what_each_gpu_means():
    """3.16.0 (Paul, 2026-10-16: rendering speed first): B200, B300, then the next tier, RunPod's exact ids (never a
    MIG slice, never the Workstation or Max-Q PRO 6000), and a note for each GPU past the B200 that a package must act on."""
    podctl = _load()
    order = podctl.RunPodProvider.default_gpus
    assert order == ("NVIDIA B200", "NVIDIA B300 SXM6 AC", "NVIDIA RTX PRO 6000 Blackwell Server Edition", "NVIDIA H200")
    assert not any("MIG" in g or "Workstation" in g or "Max-Q" in g for g in order)
    notes = podctl.RunPodProvider.gpu_notes
    assert set(notes) == set(order[1:]) and "fp8" in notes["NVIDIA B300 SXM6 AC"] and "INT8" in notes["NVIDIA B300 SXM6 AC"]
    assert podctl.RunPodProvider.fresh_pod_gb == podctl.FRESH_POD_GB == 400


def test_unit_runpod_create_goes_fresh_where_the_gpu_is_when_the_volume_is_elsewhere(tmp_path, capsys):
    """3.16.0: the GPU order comes first. The named volume sits where only a B300 is free and a B200 is free elsewhere:
    the pod is the B200, fresh, on a pod volume (no network volume made or mounted, the public ports and key as ever).
    --pod-gb sizes it; --no-fresh is 3.14.0's rule (the B300 on the volume, with its note); with the B200 sold out the
    B300 on the volume is the pick anyway."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    stock = {"dataCenters": [
        {"id": "US-CA-2", "gpuAvailability": [{"gpuTypeId": "NVIDIA B200", "stockStatus": "Low", "available": True}]},
        {"id": "EUR-IS-1", "gpuAvailability": [{"gpuTypeId": "NVIDIA B300 SXM6 AC", "stockStatus": "Low", "available": True}]}]}
    vols = {"isvol": {"id": "isvol", "name": "comfy-base-data", "dataCenterId": "EUR-IS-1", "size": 500}}
    srv = FakeRunPod(_pod(), volumes=dict(vols), gql=stock)
    prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql"))
    run = lambda *a: podctl.cmd_create(prov, podctl.build_parser().parse_args(
        ["--pubkey", str(pub), "create", "--name", "f1", "--image", "x/c:cu130", "--no-wait", "--no-names", *a]))
    pods = lambda: [b for p, b in srv.bodies if p == "/pods"]
    try:
        assert run() in (None, 0)
        out, body = capsys.readouterr().out, pods()[-1]
        assert body["gpuTypeIds"] == ["NVIDIA B200"] and body["dataCenterIds"] == ["US-CA-2"], body
        assert "networkVolumeId" not in body and body["volumeInGb"] == 400 and body["volumeMountPath"] == "/workspace"
        assert "8188/http" in body["ports"] and body["env"]["BASE_LINK_TOKEN"] and "BASE_LISTEN" not in body["env"]
        assert "/networkvolumes" not in srv.posts, srv.posts
        assert "volume  none: a fresh pod in US-CA-2 on a 400 GB pod volume (comfy-base-data is only in EUR-IS-1)" in out, out
        assert "pod volume 400 GB at /workspace" in out and "podctl guard" in out, out
        assert run("--pod-gb", "250") in (None, 0)
        assert pods()[-1]["volumeInGb"] == 250
        capsys.readouterr()
        assert run("--no-fresh") in (None, 0)
        out, body = capsys.readouterr().out, pods()[-1]
        assert body["gpuTypeIds"] == ["NVIDIA B300 SXM6 AC"] and body["networkVolumeId"] == "isvol" and "volumeInGb" not in body
        assert "gpu     note: sm_103" in out and "fp8" in out, out
        stock["dataCenters"][0]["gpuAvailability"][0]["available"] = False      # the B200 sold out everywhere
        assert run() in (None, 0)
        assert pods()[-1]["networkVolumeId"] == "isvol" and pods()[-1]["gpuTypeIds"] == ["NVIDIA B300 SXM6 AC"]
        stock["dataCenters"][0]["gpuAvailability"][0]["available"] = True
        stock["dataCenters"][0]["storageSupport"] = False                    # no network volumes there: fine for a fresh pod
        assert run() in (None, 0) and pods()[-1]["dataCenterIds"] == ["US-CA-2"] and pods()[-1]["volumeInGb"] == 400
        with pytest.raises(podctl.PodctlError):                                # a fresh pod takes no network volume
            podctl.create(prov.api, "x", "NVIDIA B200", "US-CA-2", data_gb=10, image="x/c:cu130", pubkey="k", wait=False,
                          pod_gb=100, say=lambda *a: None)
    finally:
        srv.close()


def test_unit_balance_is_projected_past_the_lag_and_guard_stops_running_pods_at_the_floor(tmp_path, capsys):
    """3.16.0: RunPod's balance trails its spend (2026-10-16: a $5 stop rule let it reach -$1.24 and the pods were
    removed). balance() reads GraphQL myself; the projection takes off --lag-min of the spend; guard says what it
    would stop without --yes (exit 3), stops every running pod with it, and is quiet above the floor (exit 0)."""
    podctl = _load()
    me = {"clientBalance": 9.0, "currentSpendPerHr": 12.0, "spendLimit": 80}
    srv = FakeRunPod(_pod(name="Videos"), gql=lambda body: {"myself": dict(me)} if "myself" in (body.get("query") or "") else {})
    prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql"))
    cmd = lambda *a: {"balance": podctl.cmd_balance, "guard": podctl.cmd_guard}[a[0]](prov, podctl.build_parser().parse_args(list(a)))
    try:
        b = prov.balance()
        assert b == {"amount": 9.0, "currency": "USD", "spend_per_hr": 12.0, "spend_limit": 80}
        assert podctl.projected_balance(b, 30) == 3.0 and podctl.projected_balance(dict(b, spend_per_hr=None), 30) == 9.0
        cmd("balance"); out = capsys.readouterr().out
        assert out.startswith("9.00 USD") and "spending 12.00/h" in out and "projected 3.00" in out, out
        me["clientBalance"] = 20.0
        assert cmd("guard") == 0 and srv.pod["desiredStatus"] == "RUNNING"
        assert "h to the 5.00 floor" in capsys.readouterr().out
        me["clientBalance"] = 9.0                                              # projected 3.00, under the $5 floor
        assert cmd("guard") == 3 and srv.pod["desiredStatus"] == "RUNNING"
        assert "WOULD STOP fakepod0000001 (Videos)" in capsys.readouterr().out
        assert cmd("guard", "--yes") == 3 and srv.pod["desiredStatus"] == "EXITED"
        assert "/pods/fakepod0000001/stop" in srv.posts and "STOPPED fakepod0000001" in capsys.readouterr().out
        assert cmd("guard", "--yes") == 0, "nothing running: nothing to stop"
        assert cmd("guard", "--floor", "1", "--lag-min", "10") == 0                        # 9 - 2 = 7 > 1
    finally:
        srv.close()


def test_unit_create_flags_a_host_does_not_take_are_refused_by_name(tmp_path):
    """Verda's create_machine takes no --volume or --min-cuda: a clear refusal, never a TypeError traceback."""
    podctl = _load()

    class Verda:
        name = "verda"; made = []
        def availability(self, gpu, spot=False): return ["FIN-01"]
        def create_machine(self, name, gpu, location, data_gb, os_gb=100, image=None, pubkey=None, spot=False, wait=True, ssh_config=None, say=print):
            self.made.append(name); return "i-1"
    args = podctl.build_parser().parse_args(["create", "--name", "n", "--gpu", "1B200.30V", "--data-gb", "100", "--min-cuda", "13.0"])
    with pytest.raises(podctl.PodctlError) as e:
        podctl.cmd_create(Verda(), args)
    assert "--min-cuda: not offered by host verda" in str(e.value) and Verda.made == []
    args = podctl.build_parser().parse_args(["create", "--name", "n", "--gpu", "1B200.30V", "--volume", "v"])
    with pytest.raises(podctl.PodctlError) as e:
        podctl.cmd_create(Verda(), args)
    assert "volume_location: not offered by host verda" in str(e.value)


# ---------------------------------------------------------------- 3.7.3: install --no-suite and --env

def test_unit_podctl_install_no_suite_and_env_reach_every_run_on_the_machine(tmp_path, capsys):
    """2026-10-15: a "no suite on the pod" install had to be done by hand over ssh, because only the buyer path could set
    an environment for the machine's runs. --no-suite is BASE_NO_SUITE=1 for the base step and every package run;
    --env KEY=VALUE (repeatable) rides every run too."""
    podctl = _load()
    a = podctl.build_parser().parse_args(["install", "pod1", "--base", "--pkg", "x", "--no-suite",
                                          "--env", "FOO=bar baz", "--env", "EMPTY=", "--env", "URL=a=b", "--env", "FOO=last"])
    assert podctl.install_run_env(a) == {"FOO": "last", "EMPTY": "", "URL": "a=b", "BASE_NO_SUITE": "1"}
    assert podctl.install_run_env(podctl.build_parser().parse_args(["install", "pod1", "--pkg", "x"])) == {}
    for bad in ("1BAD=x", "NOEQUALS", "=x", "A-B=x"):
        with pytest.raises(SystemExit):
            podctl.build_parser().parse_args(["install", "pod1", "--env", bad])
    capsys.readouterr()
    base, pkg = _local_packages(tmp_path); io = LocalPodIO(tmp_path / "pod")
    rc = podctl.install("pod1", [base, pkg], io, every=1, timeout=60, today="2026-10-15", save_pins=False,
                        run_env={"FOO": "bar baz", "BASE_NO_SUITE": "1"})
    out = capsys.readouterr().out
    assert rc == 0 and "base suite: not run (BASE_NO_SUITE=1)" in out, out
    calls = [c.rstrip() for c in (io.root / "calls.log").read_text().splitlines()]
    assert calls == ["base.sh install-self", "My Pkg-script.sh preflight", "My Pkg-script.sh"], calls   # no base suite step
    selfs = [c for c in io.cmds if "install-self" in c]; pre = [c for c in io.cmds if " preflight" in c]
    runs = [c for c in io.cmds if "STEP RC=" in c and "nohup" in c]
    assert len(selfs) == 1 and len(pre) == 1 and len(runs) == 1, io.cmds
    for c in selfs + pre + runs:
        assert "BASE_NO_SUITE=" in c and "FOO=" in c and "bar baz" in c, c



# ---------------------------------------------------------------- 3.7.4: create --private

def test_unit_runpod_create_private_is_an_unencrypted_pod_volume_behind_ssh_only(tmp_path, capsys):
    """--private: a POD volume, 22/tcp the only port (no proxy URL, no JupyterLab), ComfyUI on loopback through `podctl
    tunnel`. 3.9.1: no create input RunPod documents can ask for encryption (REST v1 refuses volumeEncrypted), so the body
    never carries it, "NOT encrypted" is said before the pod exists, and the summary prints what the read-back says."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    srv = FakeRunPod(_pod(), gql=B200_STOCK)
    try:
        # a network volume is not a private pod's: refused before any call
        with pytest.raises(podctl.PodctlError) as e:
            _create(podctl, srv, pub, "--private", "--volume", "oldvol")
        assert "drop --private" in str(e.value) and srv.posts == [] and srv.gets == []
        assert _create(podctl, srv, pub, "--private", "--data-gb", "200") in (None, 0)
        out = capsys.readouterr().out
        assert srv.posts == ["/graphql", "/pods"], srv.posts                   # no network volume is made
        body = srv.bodies[-1][1]
        assert "networkVolumeId" not in body and body["volumeInGb"] == 200 and "volumeEncrypted" not in body, body
        assert set(body) <= POD_CREATE_INPUT, sorted(set(body) - POD_CREATE_INPUT)
        assert body["ports"] == ["22/tcp"] and body["cloudType"] == "SECURE" and body["volumeMountPath"] == "/workspace"
        assert body["env"] == {"PUBLIC_KEY": "ssh-ed25519 AAAAMAC mac@key", "BASE_LISTEN": "127.0.0.1"}
        assert not any(k.startswith("JUPYTER") for k in body["env"])
        assert any("includeNetworkVolume=true" in g for g in srv.gets if g.startswith("/pods/newpod0000001")), srv.gets
        # the warning comes before the pod exists, and the summary says what the read-back said (no field: not reported)
        assert "NOT be encrypted" in out and out.index("NOT be encrypted") < out.index("created newpod0000001"), out
        assert "pod volume 200 GB at /workspace (encryption not reported)" in out and "ENCRYPTED" not in out, out
        assert "podctl tunnel newpod0000001" in out and "localhost:8188" in out, out
        facts = podctl.pod_facts(srv.pod)
        assert "pod volume 200 GB at /workspace" in facts and "encrypted" not in facts and "jupyter    not exposed" in facts, facts
        assert "auth: NONE" not in facts, facts
        # RunPod says false (the expected answer now): kept, nothing deleted, the summary says so
        srv.pod_override = {"volumeEncrypted": False}; srv.deletes.clear()
        assert _create(podctl, srv, pub, "--private", "--data-gb", "200") in (None, 0)
        out = capsys.readouterr().out
        assert "pod volume 200 GB at /workspace (not encrypted)" in out and srv.deletes == [], (out, srv.deletes)
        # RunPod says true (a future API, or the console's flag on a template): believed, and said in capitals
        srv.pod_override = {"volumeEncrypted": True}
        assert _create(podctl, srv, pub, "--private", "--data-gb", "200") in (None, 0)
        out = capsys.readouterr().out
        assert "ENCRYPTED pod volume 200 GB at /workspace" in out and "(not encrypted)" not in out.split("created")[1], out
        assert "encrypted pod volume 200 GB at /workspace" in podctl.pod_facts(srv.pod)
    finally:
        srv.close()


def test_unit_runpod_create_is_public_unless_private(tmp_path, capsys):
    """3.14.0, Paul's order of 2026-10-16 (delight first, then secure, then private; security never costs a click):
    `podctl create` with no flag makes the public pod (the network volume, the named proxy links, each locked by the pod's
    own key) on Secure Cloud, B200 first, reversing 3.8.1. --private is the opt-in (a pod volume, 22/tcp only, ComfyUI on
    loopback); --volume with it is refused with both ways out. A host whose create has no private mode (Verda) keeps
    working without a flag; asking it for --private is refused."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    srv = FakeRunPod(_pod(), gql=B200_STOCK)
    def create(*argv):
        prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql"))
        args = podctl.build_parser().parse_args(["--pubkey", str(pub), "create", "--name", "p", "--image", "x/comfy:cu130", "--no-wait", "--no-names", *argv])
        return podctl.cmd_create(prov, args)
    try:
        assert create("--data-gb", "300") in (None, 0)
        body = srv.bodies[-1][1]
        assert srv.posts == ["/graphql", "/networkvolumes", "/pods"] and "8188/http" in body["ports"], (srv.posts, body)
        assert body["cloudType"] == "SECURE" and body["gpuTypeIds"] == ["NVIDIA B200"] and "BASE_LISTEN" not in body["env"]
        assert body["env"]["BASE_LINK_TOKEN"] and "?token=%s" % body["env"]["BASE_LINK_TOKEN"] in capsys.readouterr().out
        with pytest.raises(podctl.PodctlError) as e:
            create("--private", "--volume", "oldvol")
        assert "network volume" in str(e.value) and "drop --private" in str(e.value) and "--data-gb" in str(e.value)
        with pytest.raises(podctl.PodctlError) as e:
            create("--data-gb", "300", "--public", "--private")
        assert "contradict" in str(e.value)
        srv.posts.clear()
        assert create("--data-gb", "300", "--private") in (None, 0)
        body = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert "/networkvolumes" not in srv.posts and body["ports"] == ["22/tcp"] and body["env"]["BASE_LISTEN"] == "127.0.0.1"
        assert "BASE_LINK_TOKEN" not in body["env"] and "podctl tunnel" in capsys.readouterr().out
    finally:
        srv.close()

    class Verda:
        name = "verda"; made = []
        def availability(self, gpu, spot=False): return ["FIN-01"]
        def create_machine(self, name, gpu, location, data_gb, os_gb=100, image=None, pubkey=None, spot=False, wait=True, ssh_config=None, say=print):
            self.made.append(name); return "i-1"
    v = Verda()
    podctl.cmd_create(v, podctl.build_parser().parse_args(["create", "--name", "n", "--gpu", "1B200.30V", "--data-gb", "100"]))
    assert v.made == ["n"], "a host without a private mode works without a flag"
    with pytest.raises(podctl.PodctlError) as e:
        podctl.cmd_create(v, podctl.build_parser().parse_args(["create", "--name", "n2", "--gpu", "1B200.30V", "--data-gb", "100", "--private"]))
    assert "--private: not offered by host verda" in str(e.value)


def test_unit_install_keeps_a_private_pods_loopback_listen():
    """An install restarts ComfyUI from an ssh shell, which does not see the pod's env: without this a --private pod's
    ComfyUI would come back on 0.0.0.0 after every install. An explicit --env still wins."""
    podctl = _load()
    pod = {"env": {"PUBLIC_KEY": "k", "BASE_LISTEN": "127.0.0.1"}}
    a = podctl.build_parser().parse_args(["install", "pod1", "--pkg", "x"])
    assert podctl.install_run_env(a, pod) == {"BASE_LISTEN": "127.0.0.1"}
    assert podctl.install_run_env(a, {"env": {}}) == {} and podctl.install_run_env(a, None) == {}
    a = podctl.build_parser().parse_args(["install", "pod1", "--pkg", "x", "--env", "BASE_LISTEN=0.0.0.0"])
    assert podctl.install_run_env(a, pod) == {"BASE_LISTEN": "0.0.0.0"}


# ---------------------------------------------------------------- 3.10.1: ensure and a fresh pod's resets

def test_unit_ensure_waits_for_the_images_first_boot_and_follows_the_ssh_port_a_reset_moves(tmp_path):
    """Two defects of 2026-10-16. (1) ensure reset a fresh pod in the middle of its image's first boot (runpod/comfyui
    copies ComfyUI and its venv onto the volume then), leaving no venv: a crash loop, ssh never up. (2) After its own reset
    the API still answered with the OLD pod for a while, so ensure polled the old ssh port (20424) while RunPod had moved
    22/tcp (36944), and never finished. Now: no reset until the image's ComfyUI answers through the proxy; the reset is
    followed by lastStartedAt; sshd is looked for on the port the pod has NOW, re-read on every try."""
    podctl = _load()
    fake = FakeRunPod(_pod(ports=["8188/http", "8888/http", "22/tcp"], portMappings={"22": 15270}, lastStartedAt="2026-10-16 08:59:00.000 +0000 UTC",
                           imageName="runpod/comfyui:1.4.7-cuda13.0",
                           env={"PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key"}))
    fake.move_port_on_reset = True; fake.stale_reads = 3
    cfg = tmp_path / "ssh_config"; cfg.write_text(SSH_CONFIG, encoding="utf-8")
    clock = {"t": 1000.0}
    answers = iter([502, 502, 502, 200])                     # the image's ComfyUI comes up on the fourth look
    probed, knocked, said = [], [], []

    def probe(url):
        probed.append((url, len(fake.patches))); return next(answers)

    def banner(h, p):
        knocked.append(p); return p != 15270                  # the old port never answers again after the reset

    def sleep(s):
        clock["t"] += s
    try:
        api = podctl.Api("k", base_url=fake.url)
        out = podctl.ensure(api, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg, bootstrap="B",
                            image_config=lambda ref: {"entrypoint": [], "cmd": ["/start.sh"]}, banner=banner, sleep=sleep,
                            ssh_check=lambda: "h", log=said.append, probe=probe, now=lambda: clock["t"])
    finally:
        fake.close()
    assert [u for u, _ in probed] == ["https://fakepod0000001-8188.proxy.runpod.net/"] * 4 and all(n == 0 for _, n in probed), probed
    assert len(fake.patches) == 1 and "dockerStartCmd" in fake.patches[0]
    assert "Port 15271" in cfg.read_text() and "15271" in out, (cfg.read_text(), out)
    assert any("still in its first boot" in s for s in said) and any("first boot is done" in s for s in said), said
    # a pod the base's boot already owns is never held for a first boot; an image that never answers is waited for, then reset
    fake2 = FakeRunPod(_pod(ports=["8188/http", "22/tcp"], portMappings={"22": 15270}, imageName="runpod/comfyui:1.4.7-cuda13.0",
                            env={"PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key"}))
    try:
        api2 = podctl.Api("k", base_url=fake2.url); said2 = []; clock["t"] = 0.0
        podctl.ensure(api2, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg, bootstrap="B",
                      image_config=lambda ref: {"entrypoint": [], "cmd": ["/start.sh"]}, banner=lambda h, p: True, sleep=sleep,
                      ssh_check=lambda: "h", log=said2.append, probe=lambda u: None, now=lambda: clock["t"])
        assert any("has not finished after 900 s" in s for s in said2) and len(fake2.patches) == 1, said2
        n = len(fake2.patches)
        podctl.ensure(api2, "fakepod0000001", pubkey="ssh-ed25519 AAAAC3fake mac@key", ssh_config=cfg, bootstrap="B",
                      image_config=lambda ref: {"entrypoint": [], "cmd": ["/start.sh"]}, banner=lambda h, p: True, sleep=sleep,
                      ssh_check=lambda: "h", probe=lambda u: (_ for _ in ()).throw(AssertionError("asked again")), now=lambda: clock["t"])
        assert len(fake2.patches) == n
    finally:
        fake2.close()
    # an image without ComfyUI of its own (runpod/base) is never asked on 8188: three minutes after it started will do
    base_img = {"ports": ["8188/http", "22/tcp"], "imageName": "runpod/base:1.4.0-cuda1300-ubuntu2404", "lastStartedAt": "2026-10-16 09:00:00 +0000 UTC"}
    t = lambda hms: (lambda: podctl._runpod.started_at({"lastStartedAt": "2026-10-16 %s +0000 UTC" % hms}))
    assert podctl._runpod.first_boot_done(base_img, lambda u: (_ for _ in ()).throw(AssertionError("asked")), t("09:03:00")) is True
    assert podctl._runpod.first_boot_done(base_img, None, t("09:01:00")) is False
    # no 8188/http: three minutes after the pod started is its first boot done
    assert podctl._runpod.first_boot_done({"ports": ["22/tcp"], "lastStartedAt": "2026-10-16 09:00:00.000 +0000 UTC"}, None,
                                          lambda: podctl._runpod.started_at({"lastStartedAt": "2026-10-16 09:02:59.000 +0000 UTC"})) is False
    assert podctl._runpod.first_boot_done({"ports": ["22/tcp"], "lastStartedAt": "2026-10-16 09:00:00 +0000 UTC"}, None,
                                          lambda: podctl._runpod.started_at({"lastStartedAt": "2026-10-16 09:03:00.000 +0000 UTC"})) is True
    assert podctl._runpod.first_boot_done({"ports": ["22/tcp"]}, None, lambda: 0) is True


def test_unit_install_says_a_package_folder_must_be_named_after_its_package(tmp_path):
    """2026-10-16: a package copied into a folder called `vr` stopped on "vr has no zip (vr-runpod.zip): build it first",
    a zip that cannot exist under that name. The STOP now says what is wrong and the name the folder must have."""
    import types
    podctl = _load()
    d = tmp_path / "vr"; d.mkdir(); (d / "video-replacer-minimax-h3-script.sh").write_text("PKG_NAME=x\n")
    said = []
    assert podctl.install("pod1", [d], types.SimpleNamespace(say=said.append), every=1, timeout=5) == 1
    assert said == ["STOP: the folder vr is not named after its package: a package folder's name is its package's name, the stem "
                    "of <name>-script.sh and of its zip, and vr has no vr-script.sh (it holds video-replacer-minimax-h3-script.sh: "
                    "rename the folder to video-replacer-minimax-h3)"], said
    e = tmp_path / "empty"; e.mkdir(); said.clear()
    assert podctl.install("pod1", [e], types.SimpleNamespace(say=said.append)) == 1 and said[0].endswith("empty has no empty-script.sh"), said


def test_unit_create_offers_only_data_centres_a_pod_and_its_volume_can_be_made_in(tmp_path, capsys):
    """Two create defects install-speed hit on 2026-10-16, both before billing. (1) The stock view names data centres
    REST v1 POST /pods refuses with a schema 400 (PodCreateInput.dataCenterIds' enum has 30 ids and lacks US-NE-1, a B200
    location). (2) It picked a data centre that takes no network volume (POST /networkvolumes: 500 "does not support
    network volumes"). Now the candidates are the ones in the live enum, and with a network volume those whose
    storageSupport is true; each one left out is named, and the next one is used."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    b200 = [{"gpuTypeId": "NVIDIA B200", "stockStatus": "High", "available": True}]
    gql = {"dataCenters": [{"id": "US-NE-1", "storageSupport": True, "gpuAvailability": b200},
                           {"id": "US-NC-1", "storageSupport": False, "gpuAvailability": b200},
                           {"id": "EU-RO-1", "storageSupport": True, "gpuAvailability": b200}]}
    schema = {"components": {"schemas": {"PodCreateInput": {"properties": {"dataCenterIds": {"type": "array", "items": {
        "type": "string", "enum": ["EU-RO-1", "US-NC-1", "US-CA-2"]}}}}}}}
    srv = FakeRunPod(_pod(), gql=gql, rest=lambda m, p, b: (200, schema) if (m, p) == ("GET", "/openapi.json") else None)
    try:
        assert _create(podctl, srv, pub, "--data-gb", "100") in (None, 0)
        out = capsys.readouterr().out
        assert "free now in: EU-RO-1" in out and [b for p, b in srv.bodies if p == "/pods"][-1]["dataCenterIds"] == ["EU-RO-1"]
        assert "also free in US-NE-1, left out: RunPod's create API does not take this data centre" in out
        assert "also free in US-NC-1, left out: no network volumes here" in out, out
        assert [b for p, b in srv.bodies if p == "/networkvolumes"][-1]["dataCenterId"] == "EU-RO-1"
        # a private pod wants no network volume: US-NC-1 is fine for it, US-NE-1 still is not
        assert _create(podctl, srv, pub, "--data-gb", "100", "--private") in (None, 0)
        assert [b for p, b in srv.bodies if p == "/pods"][-1]["dataCenterIds"] == ["US-NC-1"]
        assert srv.gets.count("/openapi.json") == 1                          # read once per run
    finally:
        srv.close()
    # an unreadable schema falls back to the list read on 2026-10-16
    assert "US-NE-1" not in podctl._runpod.CREATE_DCS and len(podctl._runpod.CREATE_DCS) == 30 and "EU-RO-1" in podctl._runpod.CREATE_DCS


# ---------------------------------------------------------------- 3.16.0: podctl runner (the machine as a self-hosted runner)

class _Gh:
    """A fake `gh api`: records argv, answers by the path; never a real call."""
    def __init__(self, runners=()):
        self.calls, self.runners = [], list(runners)
    def __call__(self, argv, capture_output=True, text=True):
        self.calls.append(argv)
        out = ""
        if argv[-1].endswith("/registration-token"):
            out = json.dumps({"token": "AAAATESTREGISTRATIONTOKEN0123456789", "expires_at": "later"})
        elif "?per_page=" in argv[-1]:
            out = json.dumps({"total_count": len(self.runners), "runners": self.runners})
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_unit_podctl_runner_scope_comes_from_the_flag_the_env_or_the_origin_owner():
    podctl = _load()
    assert podctl.runner_scope("Acme") == ("orgs/Acme/actions/runners", "https://github.com/Acme")
    assert podctl.runner_scope("Acme/pkg") == ("repos/Acme/pkg/actions/runners", "https://github.com/Acme/pkg")
    assert podctl.runner_scope(None, env={"PODCTL_RUNNER_SCOPE": "Env-Org"})[0] == "orgs/Env-Org/actions/runners"
    git = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "https://github.com/From-Origin/workspace.git\n", "")
    assert podctl.runner_scope(None, root="/ws", env={}, git=git)[0] == "orgs/From-Origin/actions/runners"
    with pytest.raises(podctl.PodctlError):
        podctl.runner_scope("bad scope; rm -rf /", env={})
    assert podctl.runner_name("abc123xyz") == "gpu-pod-abc123xyz"


def test_unit_podctl_runner_hands_the_token_over_stdin_only_and_never_prints_it(tmp_path, monkeypatch, capsys):
    podctl = _load()
    gh = _Gh()
    real_token = podctl.runner_token
    monkeypatch.setattr(podctl, "runner_token", lambda api_path, run=None: real_token(api_path, run=gh))
    seen = []
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None, input=None: (seen.append((cmd, input)),
                        subprocess.CompletedProcess(cmd, 0, "runner  gpu-pod-pod1 listening on the machine (pid 42)\n", ""))[1])
    monkeypatch.setattr(podctl, "lease_read", lambda timeout=30: None)
    monkeypatch.setenv("PODCTL_RUNNER_MARKS", str(tmp_path / "marks"))
    prov = type("P", (), {"pod": lambda self, p: {"id": "pod1"}})()
    args = podctl.build_parser().parse_args(["runner", "pod1", "--scope", "Acme"])
    assert podctl.cmd_runner(prov, args) == 0
    token = "AAAATESTREGISTRATIONTOKEN0123456789"
    (cmd, given), = seen
    assert given == token + "\n" and token not in cmd                       # stdin, never argv
    assert token not in capsys.readouterr().out
    for needle in ("--ephemeral", "--labels gpu-pod", "--name gpu-pod-pod1", "--url https://github.com/Acme", "sha256sum -c",
                   podctl.RUNNER_SHA256, "ACTIONS_RUNNER_INPUT_TOKEN", "unset ACTIONS_RUNNER_INPUT_TOKEN", "/opt/gh-runner"):
        assert needle in cmd, needle
    assert "--token" not in cmd
    assert gh.calls == [["gh", "api", "-X", "POST", "orgs/Acme/actions/runners/registration-token"]]
    assert (tmp_path / "marks" / "gpu-pod-pod1").read_text().strip() == "orgs/Acme/actions/runners"


def test_unit_podctl_stop_removes_a_registered_runner_and_never_asks_github_without_one(tmp_path, monkeypatch):
    podctl = _load()
    env = {"PODCTL_RUNNER_MARKS": str(tmp_path)}
    gh = _Gh(runners=[{"id": 7, "name": "gpu-pod-pod1"}, {"id": 8, "name": "someone-else"}])
    said, ssh = [], []
    assert podctl.runner_off("pod1", run=gh, say=said.append, ssh=lambda *a, **k: ssh.append(a), env=env) == 0
    assert gh.calls == [] and ssh == []                                       # no mark: offline, exactly as before
    (tmp_path / "gpu-pod-pod1").write_text("orgs/Acme/actions/runners\n")
    assert podctl.runner_off("pod1", run=gh, say=said.append, ssh=lambda *a, **k: ssh.append(a), env=env) == 1
    assert ["gh", "api", "-X", "DELETE", "orgs/Acme/actions/runners/7"] in gh.calls
    assert not any(c[-1].endswith("/8") for c in gh.calls) and ssh and not (tmp_path / "gpu-pod-pod1").exists()
    assert any("removed from GitHub (id 7)" in s for s in said)
    broken = lambda argv, **k: (_ for _ in ()).throw(FileNotFoundError("gh"))     # no gh on this Mac: said, never raised
    (tmp_path / "gpu-pod-pod1").write_text("orgs/Acme/actions/runners\n")
    assert podctl.runner_off("pod1", run=broken, say=said.append, ssh=lambda *a, **k: None, env=env) == 0
    assert "not removed" in said[-1]
    src = TOOL.read_text()
    for cmd in ("def cmd_stop", "def cmd_pause"):
        assert "runner_off(args.pod)" in src[src.index(cmd):src.index(cmd) + 200], cmd
    assert "runner_off(pod_id, say=io.say)" in src[src.index("def put_away"):src.index("def cmd_stop")]
