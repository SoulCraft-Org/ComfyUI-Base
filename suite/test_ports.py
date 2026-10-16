"""unit tier: the named ports (3.9.0). py/app_proxy.py (each customer app at the root of its own port), the boot's app
ports, the install's apps.d rows, and podctl's names: the port-names template on RunPod, the links status prints, the
tunnel's named localhost links. Everything runs on this Mac against fakes: a raw-socket ComfyUI stand-in, the fake
RunPod REST/GraphQL server of test_podctl, a fake workspace. Nothing touches the network or a real pod."""
import importlib.util
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
PROXY = BASE / "py" / "app_proxy.py"
sys.path.insert(0, str(BASE / "suite"))
from test_podctl import FakeRunPod, _load, _pod, B200_STOCK  # noqa: E402  (the one fake RunPod)
from basetest import stop_forwarders  # noqa: E402  (stop AND wait: error log row 78)


def _proxy_mod():
    spec = importlib.util.spec_from_file_location("app_proxy_t", PROXY)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("PODCTL_") or k == "BASE_WORKSPACE":
            monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- the forwarder, pure

def test_unit_app_proxy_rows_and_the_path_rule():
    m = _proxy_mod()
    assert m.parse_row("8189|video-thing|Video Thing App") == (8189, "video-thing", "Video Thing App")
    assert m.parse_row("  # a comment") is None and m.parse_row("") is None
    for bad in ("8188|x|ComfyUI", "8888|x|J", "22|x|s", "80|x|low", "70000|x|high", "8189|Bad_Slug|n", "8189|x|", "8189|x", "p|x|n"):
        with pytest.raises(ValueError):
            m.parse_row(bad)
    # the app's own files and its api go under /apps/<slug>/; ComfyUI's root routes the app calls go through unchanged
    t = lambda p: m.target(p, "vr")
    assert t("/") == "/apps/vr/" and t("/app.js") == "/apps/vr/app.js" and t("/prompts/fast.api.json") == "/apps/vr/prompts/fast.api.json"
    assert t("/api/status") == "/apps/vr/api/status" and t("/api/run") == "/apps/vr/api/run"
    assert t("/manifest.json?x=1") == "/apps/vr/manifest.json?x=1"
    for core in ("/ws?clientId=abc", "/upload/image", "/view?filename=a.mp4&type=output", "/history/123", "/history?max_items=40",
                 "/queue", "/interrupt"):
        assert t(core) == core, core
    assert t("/viewer.html") == "/apps/vr/viewer.html" and t("/historyish") == "/apps/vr/historyish"   # whole segments only
    head, up = m.rewrite_head(b"GET / HTTP/1.1\r\nHost: h:8189\r\nConnection: keep-alive\r\nKeep-Alive: 5\r\n\r\n", lambda l: "GET /apps/vr/ HTTP/1.1")
    assert head == b"GET /apps/vr/ HTTP/1.1\r\nHost: h:8189\r\nConnection: close\r\n\r\n" and up is False
    head, up = m.rewrite_head(b"GET /ws HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n", lambda l: l)
    assert up is True and head.endswith(b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")


def test_unit_app_proxy_reads_every_package_file_and_keeps_the_first_claim(tmp_path, capsys):
    m = _proxy_mod()
    d = tmp_path / "apps.d"; d.mkdir()
    (d / "a-pkg.list").write_text("8189|alpha|Alpha App\n")
    (d / "b-pkg.list").write_text("8190|beta|Beta App\n8189|gamma|Gamma\n8888|bad|Bad\n")
    assert m.read_rows(str(tmp_path)) == [(8189, "alpha", "Alpha App"), (8190, "beta", "Beta App")]
    err = capsys.readouterr().err
    assert "port 8189 is claimed by alpha and gamma" in err and "b-pkg.list" in err


# ---------------------------------------------------------------- the forwarder, live against a ComfyUI stand-in

class Upstream:
    """A raw-socket ComfyUI stand-in: records each request head and body, answers 200 with the path it saw, and on an
    Upgrade answers 101 and echoes every byte back (a websocket's shape, without parsing frames)."""

    def __init__(self):
        self.sock = socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0)); self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.seen = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            b = c.recv(65536)
            if not b:
                c.close(); return
            buf += b
        head, _, rest = buf.partition(b"\r\n\r\n")
        text = head.decode("latin-1")
        n = int((re.search(r"(?im)^content-length:\s*(\d+)", text) or [0, 0])[1])
        while len(rest) < n:
            rest += c.recv(65536)
        self.seen.append((text, rest))
        if re.search(r"(?im)^upgrade:", text):
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                b = c.recv(65536)
                if not b:
                    break
                c.sendall(b)
            c.close(); return
        path = text.split(" ")[1]
        body = ("saw %s %d" % (path, len(rest))).encode()
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body) + body)
        time.sleep(0.05); c.close()

    def close(self):
        self.sock.close()


def _start_proxy(port, upstream_port, slug="vr"):
    p = subprocess.Popen([sys.executable, str(PROXY), "--map", "%d=%s" % (port, slug), "--upstream", "127.0.0.1:%d" % upstream_port,
                          "--listen", "127.0.0.1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    p.kill(); raise AssertionError("the forwarder never listened: %s" % p.communicate(timeout=10)[0])


def _raw(port, data, read_until_close=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(data)
    out = b""
    while True:
        b = s.recv(65536)
        if not b:
            break
        out += b
    s.close()
    return out


def test_unit_app_proxy_serves_the_app_at_its_ports_root_and_passes_websockets(tmp_path):
    up = Upstream(); port = _free_port()
    proc = _start_proxy(port, up.port)
    try:
        r = _raw(port, b"GET /app.js?v=2 HTTP/1.1\r\nHost: pod-8189.proxy.runpod.net\r\nOrigin: https://pod-8189.proxy.runpod.net\r\n"
                       b"Connection: keep-alive\r\n\r\n")
        assert r.endswith(b"saw /apps/vr/app.js?v=2 0") and b"Connection: close" in r and b"keep-alive" not in r, r
        head = up.seen[-1][0]
        assert head.startswith("GET /apps/vr/app.js?v=2 HTTP/1.1") and "Host: pod-8189.proxy.runpod.net" in head
        assert "Origin: https://pod-8189.proxy.runpod.net" in head and "Connection: close" in head    # Host and Origin untouched
        # the app's api, and a ComfyUI root route with a body that is passed through byte for byte
        assert _raw(port, b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n").endswith(b"saw /apps/vr/api/status 0")
        body = b"x" * 300000
        r = _raw(port, b"POST /upload/image HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        assert r.endswith(b"saw /upload/image 300000") and up.seen[-1][1] == body
        assert _raw(port, b"GET /view?filename=a.mp4&type=output HTTP/1.1\r\nHost: x\r\n\r\n").endswith(b"saw /view?filename=a.mp4&type=output 0")
        # a websocket: Upgrade kept, 101 through, then both directions are piped unparsed
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /ws?clientId=abc HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: k\r\n\r\n")
        got = b""
        while b"\r\n\r\n" not in got:
            got += s.recv(4096)
        assert got.startswith(b"HTTP/1.1 101") and b"Connection: Upgrade" in got
        assert up.seen[-1][0].startswith("GET /ws?clientId=abc HTTP/1.1") and "Connection: Upgrade" in up.seen[-1][0]
        s.sendall(b"\x81\x05hello"); echo = b""
        while len(echo) < 7:
            echo += s.recv(4096)
        assert echo == b"\x81\x05hello"
        s.close()
    finally:
        proc.terminate(); out = proc.communicate(timeout=10)[0]; up.close()
    assert ("vr on 127.0.0.1:%d -> 127.0.0.1:%d/apps/vr/" % (port, up.port)) in out, out


def test_unit_app_proxy_says_comfyui_is_starting_while_it_does_not_answer(tmp_path):
    dead = _free_port(); port = _free_port()
    proc = _start_proxy(port, dead)
    try:
        r = _raw(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    finally:
        proc.terminate(); proc.communicate(timeout=10)
    assert r.startswith(b"HTTP/1.1 503") and b"ComfyUI is starting" in r and b"http-equiv=refresh" in r, r


# ---------------------------------------------------------------- the machine: boot_apps and the install's rows

def _boot_home(tmp_path):
    home = tmp_path / "comfy-base"; (home / "py").mkdir(parents=True); (home / "state" / "apps.d").mkdir(parents=True)
    (home / "py" / "app_proxy.py").write_bytes(PROXY.read_bytes())
    (home / "state" / "boot.env").write_text("PORT='%d'\n" % _free_port())
    return home


def test_unit_boot_apps_starts_one_forwarder_for_every_row_and_restarts_it(tmp_path):
    """boot.sh starts the forwarder after ComfyUI (and `boot.sh --apps`, which the install runs, restarts it), bound to
    BASE_LISTEN, ComfyUI's port from boot.env as its upstream; a second call replaces the first, never a second beside it."""
    home = _boot_home(tmp_path)
    port = _free_port()
    (home / "state" / "apps.d" / "my-pkg.list").write_text("%d|my-app|My App\n" % port)
    env = {**os.environ, "BOOT_HOME": str(home), "BASE_LISTEN": "127.0.0.1", "BASE_HOST": "vm"}
    boot = BASE / "lib" / "boot.sh"
    try:
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and ("boot: app port %d: My App (/apps/my-app/" % port) in r.stdout, r.stdout + r.stderr
        for _ in range(100):
            try:
                resp = _raw(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"); break
            except OSError:
                time.sleep(0.05)
        assert resp.startswith(b"HTTP/1.1 503"), resp                       # no ComfyUI behind it in the suite
        pids = lambda: subprocess.run(["pgrep", "-f", "app_proxy\\.py --state %s/state" % home], capture_output=True, text=True).stdout.split()
        first = pids(); assert len(first) == 1, first
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        time.sleep(0.5)
        second = pids(); assert len(second) == 1 and second != first, (first, second)
        # no rows: nothing listens, and the old forwarder is gone
        (home / "state" / "apps.d" / "my-pkg.list").unlink()
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        assert "app ports: none declared" in r.stdout and pids() == [], r.stdout
    finally:
        assert stop_forwarders("%s/state" % home)


def _bash(snippet, env=None):
    """Source base.sh, THEN set env (base.sh resets BASE_HOME and BASE_STATE as it loads), then run the snippet."""
    sets = "; ".join("%s=%s" % (k, _sh(v)) for k, v in (env or {}).items())
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s; %s' % (BASE, sets or "true", snippet)], capture_output=True, text=True)


def _sh(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def test_unit_install_writes_the_packages_app_rows_and_refuses_a_bad_one(tmp_path):
    """The BOOT stage writes state/apps.d/<package>.list from the package's APP_PORTS (the base declares none), refuses a
    row that takes a base port or is not port|slug|name, and removes the file when the package stops declaring one."""
    st = tmp_path / "state"; pkg = tmp_path / "packages" / "my-pkg"; pkg.mkdir(parents=True)
    env = {"BASE_STATE": str(st), "BASE_HOME": str(tmp_path), "BASE_FAKE_ROOT": str(tmp_path), "PKG_DIR": str(pkg)}
    r = _bash('BASE_DRY=0; APP_PORTS=("8189|my-app|My App" "8190|other|Other App"); base_boot_apps; echo "FAILED=${#BASE_FAILED[@]}"', env)
    assert (st / "apps.d" / "my-pkg.list").read_text() == "8189|my-app|My App\n8190|other|Other App\n", r.stdout + r.stderr
    assert "FAILED=0" in r.stdout and "not (re)started in a fake tree" in r.stdout
    r = _bash('BASE_DRY=0; APP_PORTS=("8888|jup|Mine"); base_boot_apps; echo "FAILED=${#BASE_FAILED[@]}"', env)
    assert "FAILED=1" in r.stdout and "8888 is the base's own" in r.stdout + r.stderr
    assert (st / "apps.d" / "my-pkg.list").read_text().startswith("8189|"), "a refused row leaves the last good file alone"
    r = _bash('BASE_DRY=0; APP_PORTS=("8191|Bad Slug|x"); base_boot_apps', env)
    assert "lowercase kebab-case" in r.stdout + r.stderr
    r = _bash('BASE_DRY=0; base_boot_apps', env)
    assert not (st / "apps.d" / "my-pkg.list").exists() and "declares none now" in r.stdout
    r = _bash('BASE_DRY=0; APP_PORTS=("8189|my-app|My App"); PKG_DIR="$BASE_DIR"; base_boot_apps', env)
    assert not (st / "apps.d").exists() or not list((st / "apps.d").iterdir()), "the base's own run declares no app"


# ---------------------------------------------------------------- podctl: the declarations

def _workspace(tmp_path, rows):
    ws = tmp_path / "ws"
    for name, text in rows.items():
        d = ws / name; d.mkdir(parents=True)
        (d / ("%s-script.sh" % name)).write_text('PKG_NAME="%s"\n%s\nsource "$COMFY_BASE/base.sh"\n' % (name, text))
    return ws


def test_unit_podctl_reads_every_packages_app_ports_and_refuses_a_port_claimed_twice(tmp_path):
    podctl = _load()
    ws = _workspace(tmp_path, {"video-thing": 'APP_PORTS=("8189|video-thing|Video Thing App")   # its app',
                               "image-thing": 'APP_PORTS=(\n  "8190|image-thing|Image Thing App"\n  # "8191|old|Old"\n)',
                               "plain-thing": "WF_NAME=x.json"})
    apps = podctl.declared_apps(root=ws)
    assert apps == [(8189, "video-thing", "Video Thing App", "video-thing"), (8190, "image-thing", "Image Thing App", "image-thing")]
    assert podctl.port_names(apps) == [(8188, "ComfyUI"), (8888, "Jupyter Terminal"), (8189, "Video Thing App"), (8190, "Image Thing App")]
    ws2 = _workspace(tmp_path / "two", {"a-one": 'APP_PORTS=("8189|a|A")', "b-two": 'APP_PORTS=("8189|b|B")'})
    with pytest.raises(podctl.PodctlError) as e:
        podctl.declared_apps(root=ws2)
    assert "port 8189 is declared twice: a-one (a) and b-two (b)" in str(e.value)
    ws3 = _workspace(tmp_path / "bad", {"c-three": 'APP_PORTS=("8188|c|C")'})
    with pytest.raises(podctl.PodctlError) as e:
        podctl.declared_apps(root=ws3)
    assert "c-three: APP_PORTS" in str(e.value)
    assert podctl.app_rows_in("APP_PORTS=()\n") == [] and podctl.app_rows_in("X=1\n") == []


# ---------------------------------------------------------------- podctl: RunPod's port names, through a template

class TemplateStore:
    """The template half of a fake RunPod: REST POST/GET /templates and GraphQL myself.podTemplates / saveTemplate,
    plus the B200 stock answer for create. saveTemplate REPLACES the template, as RunPod's does."""

    def __init__(self):
        self.templates = {}; self.saved = []

    def rest(self, method, path, body):
        if path == "/templates" and method == "GET":
            return 200, [dict(t, ports=t["ports"].split(",")) for t in self.templates.values()]
        if path == "/templates" and method == "POST":
            tid = "tpl%03d" % (len(self.templates) + 1)
            self.templates[tid] = {"id": tid, "name": body["name"], "imageName": body["imageName"], "ports": ",".join(body["ports"]),
                                   "env": [], "dockerArgs": "", "volumeInGb": 0, "volumeMountPath": body.get("volumeMountPath"),
                                   "containerDiskInGb": body["containerDiskInGb"], "isPublic": body["isPublic"], "isServerless": False,
                                   "readme": body.get("readme", ""), "category": body.get("category"), "portsConfig": []}
            return 200, dict(self.templates[tid], ports=body["ports"])
        if path.startswith("/templates/") and method == "GET":
            t = self.templates.get(path.split("/")[2])
            return (200, dict(t, ports=t["ports"].split(","))) if t else (404, {})
        return None

    def graphql(self, body):
        q = body.get("query") or ""
        if "dataCenters" in q:
            return B200_STOCK
        if "podTemplates" in q:
            return {"myself": {"podTemplates": [dict(t) for t in self.templates.values()]}}
        if "saveTemplate" in q:
            inp = body["variables"]["input"]
            self.saved.append(inp)
            self.templates[inp["id"]] = dict(inp)
            return {"saveTemplate": {"id": inp["id"]}}
        return {}


def _fake(pod=None):
    store = TemplateStore()
    srv = FakeRunPod(pod or _pod(), gql=store.graphql, rest=store.rest)
    return srv, store


def test_unit_runpod_create_names_every_port_through_a_private_template(tmp_path, capsys, monkeypatch):
    """REST has no port label; GraphQL's portsConfig lives on templates only. So create makes (once) a private template
    that carries the names and nothing else, re-sent whole through saveTemplate the way runpodctl does, and puts the
    pod on it; the pod gets its ports, its own JupyterLab token (never printed), and every other field of its own."""
    podctl = _load()
    ws = _workspace(tmp_path, {"video-thing": 'APP_PORTS=("8189|video-thing|Video Thing App")'})
    monkeypatch.setenv("BASE_WORKSPACE", str(ws))
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    srv, store = _fake()
    try:
        prov = podctl.RunPodProvider(api=podctl.Api("rp_key_names", base_url=srv.url, graphql_url=srv.url + "/graphql"))
        argv = ["--pubkey", str(pub), "create", "--public", "--lock", "--name", "n1", "--gpu", "NVIDIA B200", "--image", "x/comfy:cu130", "--no-wait", "--data-gb", "100"]
        assert podctl.cmd_create(prov, podctl.build_parser().parse_args(argv)) in (None, 0)
        out = capsys.readouterr().out
        body = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert body["ports"] == ["8188/http", "8888/http", "8189/http", "22/tcp"] and body["templateId"] == "tpl001"
        tok = body["env"]["JUPYTER_TOKEN"]
        assert re.fullmatch(r"[0-9a-f]{48}", tok) and body["env"]["PUBLIC_KEY"] == "ssh-ed25519 AAAAMAC mac@key"
        assert "rp_key_names" not in out
        # simple first: the ready links, named, the terminal's with its token (the one line that carries it)
        key = body["env"]["BASE_LINK_TOKEN"]                                        # 3.14.0: the key to every other link
        assert re.fullmatch(r"[0-9a-f]{48}", key) and key != tok
        links = [l.strip() for l in out.splitlines() if l.strip().startswith("links ")]
        assert [re.split(r"\s{2,}", l)[1:] for l in links] == [
            ["ComfyUI", "https://newpod0000001-8188.proxy.runpod.net/?token=%s" % key],
            ["Jupyter Terminal", "https://newpod0000001-8888.proxy.runpod.net/lab?token=%s" % tok],
            ["Video Thing App", "https://newpod0000001-8189.proxy.runpod.net/?token=%s" % key]], out
        assert [l for l in out.splitlines() if tok in l] == [l for l in out.splitlines() if "Jupyter Terminal" in l and "token=" in l]
        tmpl = [b for p, b in srv.bodies if p == "/templates"][0]
        assert tmpl["isPublic"] is False and tmpl["env"] == {} and tmpl["dockerStartCmd"] == [] and tmpl["volumeInGb"] == 0
        assert tmpl["name"].startswith("comfy-base ports ") and tmpl["ports"] == ["8188/http", "8888/http", "8189/http", "22/tcp"]
        saved = store.saved[-1]
        assert saved["portsConfig"] == [{"port": "8188", "name": "ComfyUI"}, {"port": "8888", "name": "Jupyter Terminal"},
                                        {"port": "8189", "name": "Video Thing App"}]
        assert saved["ports"] == "8188/http,8888/http,8189/http,22/tcp" and saved["name"] == tmpl["name"] and saved["isPublic"] is False
        assert "names    8188 ComfyUI · 8888 Jupyter Terminal · 8189 Video Thing App (template tpl001)" in out, out
        # a second pod with the same names reuses the template: no second POST /templates, no second saveTemplate
        n_saved = len(store.saved)
        assert podctl.cmd_create(prov, podctl.build_parser().parse_args(argv)) in (None, 0)
        assert [p for p, _ in srv.bodies].count("/templates") == 1 and len(store.saved) == n_saved
        body2 = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert body2["templateId"] == "tpl001" and body2["env"]["JUPYTER_TOKEN"] != tok          # a token per pod
        assert body2["env"]["BASE_LINK_TOKEN"] != key                                            # and a key per pod
        # --no-names: the same ports, no template
        assert podctl.cmd_create(prov, podctl.build_parser().parse_args(argv + ["--no-names"])) in (None, 0)
        body3 = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert "templateId" not in body3 and body3["ports"] == body["ports"]
        # 3.14.0: no flag is public (the same pod as --public); --private is the opt-in: 22/tcp only, no template, no token
        # and no key in RunPod's env
        plain = [a for a in argv if a != "--public"]
        assert podctl.cmd_create(prov, podctl.build_parser().parse_args(plain)) in (None, 0)
        body5 = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert body5["ports"] == body["ports"] and body5["templateId"] == "tpl001" and "BASE_LINK_TOKEN" in body5["env"]
        capsys.readouterr()
        assert podctl.cmd_create(prov, podctl.build_parser().parse_args(plain + ["--private"])) in (None, 0)
        body4 = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert body4["ports"] == ["22/tcp"] and "templateId" not in body4 and "JUPYTER_TOKEN" not in body4["env"]
        assert "BASE_LINK_TOKEN" not in body4["env"]
        assert "podctl tunnel newpod0000001 prints the links" in capsys.readouterr().out
    finally:
        srv.close()


def test_unit_runpod_port_names_refuse_a_port_the_template_lacks_and_a_failure_still_makes_the_pod(tmp_path, capsys):
    podctl = _load()
    srv, store = _fake()
    try:
        api = podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql")
        tid = podctl._runpod.names_template(api, [(8188, "ComfyUI"), (8888, "Jupyter Terminal")], "x/i:1", say=lambda *a: None, sleep=lambda s: None)
        with pytest.raises(podctl.PodctlError) as e:
            podctl._runpod.save_port_names(api, tid, [(8189, "Not There")], sleep=lambda s: None)
        assert "8189 is not one of template" in str(e.value)
        with pytest.raises(podctl.PodctlError):
            podctl._runpod.save_port_names(api, tid, [(8188, "  ")], sleep=lambda s: None)
        # RunPod refusing the names never costs the pod: create says so and goes on without a template
        srv.gql = lambda body: {"__error__": "saveTemplate refused"} if "saveTemplate" in (body.get("query") or "") else TemplateStore.graphql(store, body)
        said = []
        pod_id = podctl.create(api, "n2", "NVIDIA B200", "EU-RO-1", data_gb=50, image="x/i:2", pubkey="ssh-ed25519 K k",
                               wait=False, say=said.append, names=[(8188, "ComfyUI"), (8888, "Jupyter Terminal"), (8189, "App")])
        assert pod_id == "newpod0000001" and any("could not be written" in s for s in said), said
        assert "templateId" not in [b for p, b in srv.bodies if p == "/pods"][-1]
    finally:
        srv.close()


def test_unit_status_prints_the_named_links_and_deploy_keeps_the_names(tmp_path):
    podctl = _load()
    prov = podctl.RunPodProvider(api=object())
    pod = _pod(id="pod123", ports=["8188/http", "8888/http", "8189/http", "8200/http", "22/tcp"], env={"JUPYTER_TOKEN": "tokfromenv"})
    declared = [(8189, "video-thing", "Video Thing App", "video-thing")]
    lines = podctl.status_links(prov, pod, declared, machine=("tokfrommachine", []))
    got = [re.split(r"\s{2,}", l.replace("links    ", "", 1)) for l in lines]
    assert got == [["ComfyUI", "https://pod123-8188.proxy.runpod.net"],
                   ["Jupyter Terminal", "https://pod123-8888.proxy.runpod.net/lab?token=tokfromenv"],   # the pod's own token wins
                   ["Video Thing App", "https://pod123-8189.proxy.runpod.net"],
                   ["HTTP Service (port 8200)", "https://pod123-8200.proxy.runpod.net"]], lines
    # the machine's own rows name a port this tree does not declare; its saved token is used when the env has none
    pod2 = _pod(id="pod9", ports=["8188/http", "8888/http", "8190/http"], env={})
    l2 = podctl.status_links(prov, pod2, [], machine=("tokm", [(8190, "other", "Other App")]))
    assert "Other App" in l2[2] and l2[1].endswith("/lab?token=tokm"), l2
    # a private pod has nothing to link through the proxy: the tunnel is the way
    l3 = podctl.status_links(prov, _pod(id="priv", ports=["22/tcp"], env={}), declared)
    assert l3 == ["links    none through the proxy (no http port): podctl tunnel priv prints the localhost links"]
    assert "jupyter    open: its Connect link needs no token" in podctl.pod_facts(_pod(env={}))       # 3.19.1: open by default
    facts = podctl.pod_facts(_pod(env={"BASE_LINK_LOCK": "1"}))
    assert "auth: NONE" not in facts and "token: generated by the boot" in facts
    # deploy: a names template is kept, any other template is not; the clone gets its own token
    srv, store = _fake(_pod(templateId="tplX", env={"JUPYTER_TOKEN": "old", "PUBLIC_KEY": "k"}, networkVolume={"id": "v", "dataCenterId": "EU-RO-1"},
                            machine={"gpuTypeId": "NVIDIA B200", "secureCloud": True}))
    store.templates["tplX"] = {"id": "tplX", "name": "comfy-base ports abc", "ports": "8188/http"}
    try:
        api = podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql")
        podctl.deploy(api, "fakepod0000001", wait=False, say=lambda *a: None)
        b = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert b["templateId"] == "tplX" and b["env"]["JUPYTER_TOKEN"] not in ("old", "") and b["env"]["PUBLIC_KEY"] == "k"
        store.templates["tplX"]["name"] = "someone else's template"
        srv.pod = _pod(templateId="tplX", networkVolume={"id": "v", "dataCenterId": "EU-RO-1"}, machine={"gpuTypeId": "NVIDIA B200"})
        podctl.deploy(api, "fakepod0000001", wait=False, say=lambda *a: None)
        assert "templateId" not in [b for p, b in srv.bodies if p == "/pods"][-1]
    finally:
        srv.close()


def test_unit_tunnel_forwards_the_machines_app_ports_and_prints_named_localhost_links():
    podctl = _load()
    tok, apps = podctl.parse_tunnel_facts("abc123\n--apps--\n8189|video-thing|Video Thing App\njunk\n")
    assert tok == "abc123" and apps == [(8189, "video-thing", "Video Thing App")]
    assert podctl.parse_tunnel_facts("\n--apps--\n") == (None, [])
    ports, lines = podctl.tunnel_plan([8188, 8888], False, apps, [], tok)
    assert ports == [8188, 8888, 8189]
    assert lines == ["tunnel  ComfyUI           http://localhost:8188",
                     "tunnel  Jupyter Terminal  http://localhost:8888/lab?token=abc123",
                     "tunnel  Video Thing App   http://localhost:8189"], lines
    # --port given: exactly those, LOCAL:REMOTE named by the remote port
    ports, lines = podctl.tunnel_plan(["8190:8188", "8191:8189"], True, apps, [], None)
    assert ports == ["8190:8188", "8191:8189"] and lines[0].endswith("http://localhost:8190") and "Video Thing App" in lines[1]
    # nothing read from the machine: this tree's declarations name and add the ports
    ports, lines = podctl.tunnel_plan([8188, 8888], False, [], [(8189, "v", "V App", "v")], None)
    assert ports == [8188, 8888, 8189] and "(no token read: podctl jupyter <pod>)" in lines[1] and "V App" in lines[2]
    a = podctl.build_parser().parse_args(["tunnel", "p1"]); assert not getattr(a, "port_given", False)
    a = podctl.build_parser().parse_args(["tunnel", "p1", "--port", "9000"]); assert a.port_given and a.port == [9000]
    cmd = podctl.tunnel_facts_cmd()
    assert "JUPYTER_TOKEN" in cmd and "cat /workspace/comfy-base/state/tokens.env" not in cmd    # the one token, never the file


def test_unit_private_create_hands_the_token_over_ssh_and_prints_the_tunnels_named_links(tmp_path, monkeypatch):
    """A private pod's token never goes into RunPod's env (kept in the clear): once ssh answers, create hands it to the
    pod volume through ssh's STDIN (never argv), keeps one already there, and prints the links the tunnel opens."""
    podctl = _load()
    seen = []

    def fake_ssh(cmd, env=None, timeout=None, input=None):
        seen.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "SEEDED\n", "")
    monkeypatch.setattr(podctl, "ssh_run", fake_ssh)
    srv, store = _fake()
    try:
        api = podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql")
        said = []
        podctl.create(api, "p1", "NVIDIA B200", "EU-RO-1", data_gb=50, image="x/i:cu130", pubkey="ssh-ed25519 K k", private=True,
                      wait=True, sleep=lambda s: None, banner=lambda h, p: True, ssh_config=tmp_path / "config", say=said.append,
                      names=[(8188, "ComfyUI"), (8888, "Jupyter Terminal"), (8189, "Video Thing App")])
    finally:
        srv.close()
    (cmd, inp), = seen
    tok = inp.strip().split("=", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{48}", tok) and tok not in cmd and "tokens.env" in cmd and "umask 077" in cmd
    lines = [s.strip() for s in said if s.strip().startswith("tunnel ")]
    assert lines == ["tunnel  ComfyUI           http://localhost:8188",
                     "tunnel  Jupyter Terminal  http://localhost:8888/lab?token=%s" % tok,
                     "tunnel  Video Thing App   http://localhost:8189"], said
    assert "JUPYTER_TOKEN" not in [b for p, b in srv.bodies if p == "/pods"][-1]["env"]
    # a token already on the volume is kept, and that is the one printed
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None, input=None: subprocess.CompletedProcess(cmd, 0, "keptone\n", ""))
    assert podctl._runpod.seed_jupyter_token(say=lambda *a: None) == "keptone"
    monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None, input=None: subprocess.CompletedProcess(cmd, 255, "", "x"))
    assert podctl._runpod.seed_jupyter_token(say=lambda *a: None) is None


# ---------------------------------------------------------------- 3.14.0: the key on every public link

KEY = "k3y" + "0" * 45


def test_unit_app_proxy_key_rules_pure():
    """The key in the query is taken out byte for byte, compared in constant time, traded for a cookie on GET/HEAD;
    a cookie opens without touching the target; loopback peers pass; no secret means every outside request is denied."""
    m = _proxy_mod()
    assert m.split_token("/x?a=1&token=%s&b=%%20" % KEY) == (KEY, "/x?a=1&b=%20")
    assert m.split_token("/x?token=%s" % KEY) == (KEY, "/x") and m.split_token("/x?tokens=1") == (None, "/x?tokens=1")
    assert m.split_token("/x") == (None, "/x") and m.split_token("/?token=a%2Bb&token=z") == ("a+b", "/")
    out = "1.2.3.4"
    assert m.authorize(KEY, "GET", "/?token=%s" % KEY, [], out) == ("set", "/")
    assert m.authorize(KEY, "HEAD", "/v?x=1&token=%s" % KEY, [], out) == ("set", "/v?x=1")
    assert m.authorize(KEY, "POST", "/prompt?token=%s" % KEY, [], out) == ("open", "/prompt")
    assert m.authorize(KEY, "GET", "/?token=nope", [], out) == ("deny", None)
    assert m.authorize(KEY, "GET", "/", [], out) == ("deny", None)
    assert m.authorize(KEY, "GET", "/", ["Cookie: a=1; base_link=wrong"], out) == ("deny", None)
    ck = ["Cookie: a=1; base_link=%s" % KEY]
    assert m.authorize(KEY, "GET", "/app?token=theirs", ck, out) == ("open", "/app?token=theirs")   # an app's own ?token= passes
    assert m.authorize(KEY, "GET", "/", ck, "::ffff:10.0.0.2") == ("open", "/")
    for peer in ("127.0.0.1", "127.8.9.1", "::1", "::ffff:127.0.0.1"):
        assert m.authorize(KEY, "GET", "/", [], peer) == ("open", "/") and m.loopback(peer)
    assert m.authorize(None, "GET", "/?token=x", [], out) == ("deny", None)
    assert m.authorize(None, "GET", "/", [], "127.0.0.1") == ("open", "/")
    assert m.same(KEY, KEY) and not m.same(KEY, KEY[:-1]) and not m.same(KEY, "é" * 48)
    assert m.public("0.0.0.0") and m.public("::") and m.public("10.1.2.3") and not m.public("127.0.0.1") and not m.public("localhost")
    head, _ = m.rewrite_head(b"GET / HTTP/1.1\r\nHost: x\r\nCookie: a=1; base_link=%s; b=2\r\nCookie: base_link=%s\r\n\r\n" % (KEY.encode(), KEY.encode()), lambda l: l)
    assert KEY.encode() not in head and b"Cookie: a=1; b=2\r\n" in head and head.count(b"Cookie") == 1, head
    assert all(not m.loopback(a) for a in m.front_addrs())


def _start_gated(tmp_path, args, key=KEY):
    """The forwarder as a pod runs it, but with every peer counted as outside (the suite can only connect from loopback)."""
    w = tmp_path / "outside.py"
    w.write_text("import importlib.util, sys\nspec = importlib.util.spec_from_file_location('ap', %r)\nm = importlib.util.module_from_spec(spec)\n"
                 "spec.loader.exec_module(m)\nm.loopback = lambda peer: False\nsys.exit(m.main(sys.argv[1:]))\n" % str(PROXY))
    env = {k: v for k, v in os.environ.items() if k != "BASE_LINK_TOKEN"}
    if key:
        env["BASE_LINK_TOKEN"] = key
    p = subprocess.Popen([sys.executable, str(w), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    return p


def _wait_listen(port, p=None):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); return
        except OSError:
            time.sleep(0.05)
    if p is None:
        raise AssertionError("nothing listened on %d" % port)
    p.kill(); raise AssertionError("never listened: %s" % p.communicate(timeout=10)[0])


def test_unit_app_proxy_key_locks_the_app_and_comfyuis_front_and_trades_the_link_for_a_cookie(tmp_path):
    up = Upstream(); app, front = _free_port(), _free_port()
    p = _start_gated(tmp_path, ["--map", "%d=vr" % app, "--upstream", "127.0.0.1:%d" % up.port, "--listen", "127.0.0.1",
                                "--front", str(front), "--front-listen", "127.0.0.1"])
    try:
        _wait_listen(app, p); _wait_listen(front, p)
        for port in (app, front):
            r = _raw(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert r.startswith(b"HTTP/1.1 401") and b"This link needs its key" in r and b"no-store" in r, r
            assert _raw(port, b"GET /?token=wrong HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 401")
        assert up.seen == []                                        # nothing reached ComfyUI unlocked
        r = _raw(app, b"GET /index.html?a=1&token=%s&b=2 HTTP/1.1\r\nHost: pod-8189.proxy.runpod.net\r\n\r\n" % KEY.encode())
        head = r.split(b"\r\n\r\n")[0].decode()
        assert head.startswith("HTTP/1.1 303") and "\r\nLocation: /index.html?a=1&b=2\r\n" in head, head
        cookie = re.search(r"(?m)^Set-Cookie: (.*)$", head).group(1).strip()
        assert cookie == "base_link=%s; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax" % KEY and "Domain" not in head
        assert "Referrer-Policy: no-referrer" in head and up.seen == []
        # with the cookie: through, and the key never reaches ComfyUI (the cookie is taken out of the header it forwards)
        ck = b"Cookie: theme=dark; base_link=%s\r\n" % KEY.encode()
        assert _raw(app, b"GET /app.js HTTP/1.1\r\nHost: x\r\n" + ck + b"\r\n").endswith(b"saw /apps/vr/app.js 0")
        assert "Cookie: theme=dark" in up.seen[-1][0] and KEY not in up.seen[-1][0]
        assert _raw(front, b"GET /system_stats HTTP/1.1\r\nHost: x\r\n" + ck + b"\r\n").endswith(b"saw /system_stats 0")  # unchanged path
        body = b"{}"
        r = _raw(front, b"POST /prompt?token=%s HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}" % KEY.encode())
        assert r.endswith(b"saw /prompt 2") and up.seen[-1][1] == body and KEY not in up.seen[-1][0]
        s = socket.create_connection(("127.0.0.1", front), timeout=5)
        s.sendall(b"GET /ws?clientId=a HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" + ck + b"\r\n")
        got = b""
        while b"\r\n\r\n" not in got:
            got += s.recv(4096)
        assert got.startswith(b"HTTP/1.1 101"); s.close()
    finally:
        p.terminate(); out = p.communicate(timeout=10)[0]; up.close()
    assert KEY not in out and "every link needs the key" in out and ("ComfyUI on 127.0.0.1:%d" % front) in out, out


def test_unit_app_proxy_with_no_key_refuses_every_outside_request(tmp_path):
    up = Upstream(); app = _free_port()
    p = _start_gated(tmp_path, ["--map", "%d=vr" % app, "--upstream", "127.0.0.1:%d" % up.port, "--listen", "127.0.0.1"], key=None)
    try:
        _wait_listen(app, p)
        assert _raw(app, b"GET /?token= HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 401")
        assert _raw(app, b"GET / HTTP/1.1\r\nHost: x\r\nCookie: base_link=\r\n\r\n").startswith(b"HTTP/1.1 401") and up.seen == []
    finally:
        p.terminate(); out = p.communicate(timeout=10)[0]; up.close()
    assert "no BASE_LINK_TOKEN: every request from outside this machine is refused" in out, out


def test_unit_app_proxy_binds_a_busy_port_once_it_is_free():
    """An install starts the forwarder BEFORE it restarts ComfyUI, so an older ComfyUI may still hold the front's port:
    the forwarder says so once and takes the port the moment it is free."""
    import asyncio
    m = _proxy_mod(); said = []
    blocker = socket.socket(); blocker.bind(("127.0.0.1", 0)); blocker.listen(1); port = blocker.getsockname()[1]

    async def go():
        servers = await m.serve([], "0.0.0.0", ("127.0.0.1", 1), say=said.append, secret=KEY, front=port,
                                front_listen=["127.0.0.1"], retry=0.05)
        assert servers == [] and "NOT served on 127.0.0.1:%d yet" % port in said[0], said
        blocker.close()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if servers:
                break
        assert len(servers) == 1 and any("ComfyUI on 127.0.0.1:%d" % port in x for x in said), said
        for srv in servers:
            srv.close(); await srv.wait_closed()
    asyncio.run(go())
    assert sum("NOT served" in x for x in said) == 1, said


def test_unit_boot_makes_the_key_on_a_public_bind_and_starts_comfyuis_front(tmp_path):
    """On a public bind boot_apps runs the forwarder even with no app (it is ComfyUI's only way in), makes the key when
    neither the env nor state/tokens.env has one (saved 0600, never in the output, never in argv), and ComfyUI binds 127.0.0.1."""
    home = _boot_home(tmp_path)
    port = int(re.search(r"\d+", (home / "state" / "boot.env").read_text()).group())
    env = {k: v for k, v in os.environ.items() if k != "BASE_LINK_TOKEN"}
    env.update({"BOOT_HOME": str(home), "BASE_LISTEN": "0.0.0.0", "BASE_HOST": "vm", "BOOT_ROOT": str(tmp_path / "root"),
                "BOOT_FRONT_LISTEN": "127.0.0.1", "BASE_LINK_LOCK": "1"})        # 3.19.1: the key is the pod's opt-in
    boot = BASE / "lib" / "boot.sh"
    pids = lambda: subprocess.run(["pgrep", "-f", "app_proxy\\.py --state %s/state" % home], capture_output=True, text=True).stdout.split()
    try:
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "the key's front" in r.stdout and "key generated" in r.stdout, r.stdout + r.stderr
        tf = home / "state" / "tokens.env"
        key = tf.read_text().split("BASE_LINK_TOKEN=", 1)[1].strip()
        assert re.fullmatch(r"[0-9a-f]{64}", key) and oct(tf.stat().st_mode & 0o777) == "0o600" and key not in r.stdout + r.stderr
        _wait_listen(port)          # (no request: in the suite the front's address and ComfyUI's are both 127.0.0.1, so it would reach itself)
        ps = subprocess.run(["ps", "-ax", "-o", "args"], capture_output=True, text=True).stdout
        assert "--front %d" % port in ps and key not in ps
        # a second boot reuses the saved key; the env's own wins over it
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        assert "key generated" not in r.stdout and tf.read_text().count("BASE_LINK_TOKEN=") == 1
        # a loopback bind: no front, no key made
        home2 = _boot_home(tmp_path / "two")
        r = subprocess.run(["bash", str(boot), "--apps"], capture_output=True, text=True, timeout=60,
                           env={**env, "BOOT_HOME": str(home2), "BASE_LISTEN": "127.0.0.1"})
        assert "app ports: none declared" in r.stdout and "front" not in r.stdout and not (home2 / "state" / "tokens.env").exists()
    finally:
        assert stop_forwarders("%s/state" % home)
    sh = 'source "%s/lib/boot.sh"; BOOT_HOME=%s; ' % (BASE, _sh(home))
    got = subprocess.run(["bash", "-c", sh + "BASE_LISTEN=0.0.0.0 BASE_LINK_TOKEN=fromenv; boot_link_token; echo $BASE_LINK_TOKEN $BOOT_LINK_MADE"],
                         capture_output=True, text=True, env={**env, "BOOT_ROOT": ""}).stdout.split()
    assert got == ["fromenv", "0"], got
    pid1 = tmp_path / "environ"; pid1.write_bytes(b"A=1\0BASE_LINK_TOKEN=frompid1\0")
    got = subprocess.run(["bash", "-c", sh + "unset BASE_LINK_TOKEN; BASE_LISTEN=0.0.0.0; boot_link_token; echo $BASE_LINK_TOKEN"],
                         capture_output=True, text=True, env={**env, "BOOT_PID1_ENVIRON": str(pid1)}).stdout.split()
    assert got == ["frompid1"], got
    launch = 'source "%s/lib/85-launch.sh"; ' % BASE
    for listen, want in (("0.0.0.0", "127.0.0.1"), ("", "127.0.0.1"), ("10.0.0.5", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"), ("::1", "::1")):
        out = subprocess.run(["bash", "-c", launch + "BASE_LISTEN=%s; _base_comfy_listen" % _sh(listen)], capture_output=True, text=True).stdout.strip()
        assert out == want, (listen, out)


def test_unit_create_reuses_the_named_network_volume_where_the_gpu_is_free(tmp_path, capsys):
    """Models download once: a public create with no --volume goes onto comfy-base-data in a data centre that has it and
    has the GPU free; only where none is does --data-gb make one, under that name; with neither it stops, naming both ways."""
    podctl = _load()
    pub = tmp_path / "id.pub"; pub.write_text("ssh-ed25519 AAAAMAC mac@key\n")
    vols = {"vmine": {"id": "vmine", "name": "comfy-base-data", "dataCenterId": "US-CA-2", "size": 500},
            "vother": {"id": "vother", "name": "someone-elses-data", "dataCenterId": "EU-RO-1", "size": 300}}
    srv = FakeRunPod(_pod(), volumes=dict(vols), gql=B200_STOCK)
    prov = podctl.RunPodProvider(api=podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql"))
    run = lambda *a: podctl.cmd_create(prov, podctl.build_parser().parse_args(
        ["--pubkey", str(pub), "create", "--name", "r1", "--gpu", "NVIDIA B200", "--image", "x/c:cu130", "--no-wait", "--no-names", *a]))
    try:
        assert run() in (None, 0)
        body = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert body["networkVolumeId"] == "vmine" and body["dataCenterIds"] == ["US-CA-2"] and "/networkvolumes" not in srv.posts
        assert "vmine (comfy-base-data) in US-CA-2: reused" in capsys.readouterr().out
        # the named volume only where the GPU is sold out, and no --data-gb: 3.18.0, a fresh pod where the GPU is (no
        # network volume made); with --no-fresh, 3.14.0's STOP, nothing billed
        srv.volumes["vmine"]["dataCenterId"] = "US-NE-1"; srv.posts.clear()
        assert run() in (None, 0)
        body = [b for p, b in srv.bodies if p == "/pods"][-1]
        assert "networkVolumeId" not in body and body["volumeInGb"] == 400 and body["dataCenterIds"] == ["EU-RO-1"] and "/networkvolumes" not in srv.posts
        capsys.readouterr(); srv.posts.clear()
        assert run("--no-fresh") == 1
        out = capsys.readouterr().out
        assert "STOP: comfy-base-data is only in US-NE-1, which has no NVIDIA B200 free" in out and "--data-gb N for a new comfy-base-data" in out and [p for p in srv.posts if p != "/graphql"] == [], out
        # ... with --data-gb: a new one where the GPU is, under the name the next create reuses
        assert run("--data-gb", "200") in (None, 0)
        made = [b for p, b in srv.bodies if p == "/networkvolumes"][-1]
        assert made["name"] == "comfy-base-data" and made["dataCenterId"] == "EU-RO-1"
        # --volume-name picks another; --private never reuses a network volume
        srv.posts.clear()
        assert run("--volume-name", "someone-elses-data") in (None, 0)
        assert [b for p, b in srv.bodies if p == "/pods"][-1]["networkVolumeId"] == "vother" and "/networkvolumes" not in srv.posts
        assert run("--private", "--data-gb", "50") in (None, 0)
        assert "networkVolumeId" not in [b for p, b in srv.bodies if p == "/pods"][-1]
        with pytest.raises(podctl.PodctlError):
            run("--public", "--private", "--data-gb", "50")
    finally:
        srv.close()


def test_unit_status_and_deploy_carry_the_pods_key():
    podctl = _load()
    prov = podctl.RunPodProvider(api=object())
    pod = _pod(id="p7", ports=["8188/http", "8888/http", "8189/http", "22/tcp"], env={"JUPYTER_TOKEN": "jt", "BASE_LINK_TOKEN": "envkey"})
    lines = podctl.status_links(prov, pod, [(8189, "v", "V App", "v")], machine=("jm", []), link="machinekey")
    assert [l.split()[-1] for l in lines] == ["https://p7-8188.proxy.runpod.net/?token=envkey", "https://p7-8888.proxy.runpod.net/lab?token=jt",
                                             "https://p7-8189.proxy.runpod.net/?token=envkey"], lines
    pod2 = _pod(id="p8", ports=["8188/http"], env={})
    assert podctl.status_links(prov, pod2, [], link="machinekey")[0].endswith("/?token=machinekey")
    assert podctl.status_links(prov, pod2, [])[0].split()[-1] == "https://p8-8188.proxy.runpod.net"
    text = "jtok\n--link--\nlkey\n--apps--\n8189|v|V App\n"
    assert podctl.parse_tunnel_facts(text) == ("jtok", [(8189, "v", "V App")]) and podctl.parse_link_fact(text) == "lkey"
    assert podctl.parse_link_fact("jtok\n--apps--\n") is None and "BASE_LINK_TOKEN" in podctl.tunnel_facts_cmd()
    assert "BASE_LINK_TOKEN" in podctl.pod_facts(pod) and "envkey" not in podctl.pod_facts(pod)
    src = _pod(env={"BASE_LINK_TOKEN": "old", "JUPYTER_TOKEN": "j"}, networkVolume={"id": "v", "dataCenterId": "EU-RO-1"},
               machine={"gpuTypeId": "NVIDIA B200", "secureCloud": True})
    b = podctl.deploy_body(src)
    assert re.fullmatch(r"[0-9a-f]{48}", b["env"]["BASE_LINK_TOKEN"])


def test_unit_a_new_pod_runs_nothing_of_its_image_so_no_link_is_ever_open_without_the_key(tmp_path, monkeypatch):
    """Error log row 71 (2026-10-16, pod augydbweqttpme): until the base owned a new pod, the image's own ComfyUI answered
    on the public 8188 link with no key, for the whole install. A pod podctl creates now starts on the base's boot guard
    and sshd only; ensure finds the guard and resets nothing (no first-boot wait either); the install's `boot.sh --apps`
    starts JupyterLab with the pod's own token, so the Jupyter Terminal link works before the first restart."""
    podctl = _load()
    srv, store = _fake()
    try:
        api = podctl.Api("k", base_url=srv.url, graphql_url=srv.url + "/graphql")
        for private in (False, True):
            podctl.create(api, "n%d" % private, "NVIDIA B200", "EU-RO-1", data_gb=50, image="runpod/comfyui:1.4.7-cuda13.0",
                          pubkey="ssh-ed25519 K k", wait=False, say=lambda *a: None, private=private)
            body = [b for p, b in srv.bodies if p == "/pods"][-1]
            assert body["dockerStartCmd"] == [] and body["dockerEntrypoint"][:2] == ["bash", "-c"], body
            line = body["dockerEntrypoint"][2]
            assert line.startswith(podctl.BOOT_GUARD + "; ") and "boot_sshd" in line and line.endswith("; exec sleep infinity"), line
            assert "start_script" not in line and "comfyui" not in line.lower()
        # ensure on such a pod: nothing to patch, so no reset and no first-boot wait
        srv.pod.update(ports=["8188/http", "8888/http", "22/tcp"], portMappings={"22": 15270}, env={"PUBLIC_KEY": "ssh-ed25519 K k"})
        n = len(srv.patches)
        podctl.ensure(api, srv.pod["id"], pubkey="ssh-ed25519 K k", ssh_config=tmp_path / "cfg", bootstrap="B",
                      image_config=lambda ref: (_ for _ in ()).throw(AssertionError("no image read")), banner=lambda h, p: True,
                      sleep=lambda s: None, ssh_check=lambda: "h", log=lambda s: None,
                      probe=lambda u: (_ for _ in ()).throw(AssertionError("no first-boot wait")))
        assert len(srv.patches) == n
    finally:
        srv.close()
    # the install's `boot.sh --apps`: JupyterLab started once, with the pod's own token read from PID 1
    home = _boot_home(tmp_path)
    jl = home / "tools" / "bin" / "jupyter-lab"; jl.parent.mkdir(parents=True)
    seen = tmp_path / "jl.env"
    jl.write_text("#!/bin/sh\necho \"$JUPYTER_TOKEN\" > %s\n" % seen); jl.chmod(0o755)
    pid1 = tmp_path / "environ"; pid1.write_bytes(b"A=1\0JUPYTER_TOKEN=pod-own-jt\0")
    env = {k: v for k, v in os.environ.items() if k not in ("JUPYTER_TOKEN", "JUPYTER_PASSWORD", "BASE_LINK_TOKEN")}
    env.update(BOOT_HOME=str(home), BASE_LISTEN="127.0.0.1", BASE_HOST="vm", BOOT_ROOT=str(tmp_path / "root"), BOOT_PID1_ENVIRON=str(pid1))
    r = subprocess.run(["bash", str(BASE / "lib" / "boot.sh"), "--apps"], capture_output=True, text=True, env=env, timeout=60)
    for _ in range(50):
        if seen.exists():
            break
        time.sleep(0.1)
    assert r.returncode == 0 and "JupyterLab starting on 8888" in r.stdout, r.stdout + r.stderr
    assert seen.read_text().strip() == "pod-own-jt" and "pod-own-jt" not in r.stdout + r.stderr
    assert not (home / "state" / "tokens.env").exists() or "JUPYTER_TOKEN" not in (home / "state" / "tokens.env").read_text()


# ---------------------------------------------------------------- 3.19.1: open by default, the key an opt-in

def test_unit_links_open_on_one_click_unless_the_pod_opts_into_the_lock(tmp_path):
    """Paul, 2026-10-17: "can you fix this so I can click the links?" On RunPod's public bind, every Connect-tab link
    (ComfyUI, each app, the Jupyter Terminal) now opens on one click: RunPod's proxy passes nothing that names the pod's
    owner, so any login is a step. BASE_LINK_LOCK=1 (podctl create --lock) is the opt-in to the key and the token."""
    # the forwarder with --open: a request from outside, with no key and no cookie, goes straight through
    up = Upstream(); app, front = _free_port(), _free_port()
    p = _start_gated(tmp_path, ["--map", "%d=vr" % app, "--upstream", "127.0.0.1:%d" % up.port, "--listen", "127.0.0.1",
                                "--front", str(front), "--front-listen", "127.0.0.1", "--open"], key=None)
    try:
        _wait_listen(app, p); _wait_listen(front, p)
        assert _raw(app, b"GET /app.js HTTP/1.1\r\nHost: x\r\n\r\n").endswith(b"saw /apps/vr/app.js 0")
        assert _raw(front, b"GET /system_stats HTTP/1.1\r\nHost: x\r\n\r\n").endswith(b"saw /system_stats 0")
    finally:
        p.terminate(); out = p.communicate(timeout=10)[0]; up.close()
    assert "every link opens on one click" in out, out
    # the boot on a public bind with no lock: the front runs --open and no key is made
    home = _boot_home(tmp_path / "open")
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_LINK_TOKEN", "BASE_LINK_LOCK", "JUPYTER_TOKEN", "JUPYTER_PASSWORD")}
    env.update(BOOT_HOME=str(home), BASE_LISTEN="0.0.0.0", BASE_HOST="vm", BOOT_ROOT=str(tmp_path / "root"), BOOT_FRONT_LISTEN="127.0.0.1")
    try:
        r = subprocess.run(["bash", str(BASE / "lib" / "boot.sh"), "--apps"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "the front, open (BASE_LINK_LOCK=1 locks it)" in r.stdout, r.stdout + r.stderr
        for _ in range(50):
            ps = subprocess.run(["ps", "-ax", "-o", "args"], capture_output=True, text=True).stdout
            if "%s/state" % home in ps:
                break
            time.sleep(0.1)
        line = [l for l in ps.splitlines() if "app_proxy.py --state %s/state" % home in l][0]
        assert "--open" in line and "--front" in line, line
        assert not (home / "state" / "tokens.env").exists()
    finally:
        assert stop_forwarders("%s/state" % home)
    # JupyterLab on the open public bind: no token, even on a pod whose env still carries one from an older create
    jl = home / "tools" / "bin" / "jupyter-lab"; jl.parent.mkdir(parents=True)
    seen = tmp_path / "jl.txt"
    jl.write_text("#!/bin/sh\necho \"TOKEN=${JUPYTER_TOKEN:-none} ARGS=$*\" > %s\n" % seen); jl.chmod(0o755)
    console = tmp_path / "console.log"
    run = lambda extra: subprocess.run(["bash", "-c", 'source "%s/lib/boot.sh"; boot_jupyter 3>"%s"' % (BASE, console)],
                                       capture_output=True, text=True, env=dict(env, BASE_HOST="runpod", RUNPOD_POD_ID="podabc", **extra))
    r = run({"JUPYTER_TOKEN": "old-pod-token"})
    for _ in range(50):
        if seen.exists():
            break
        time.sleep(0.1)
    got = seen.read_text()
    assert r.returncode == 0 and "open: its Connect link needs no token" in r.stdout, r.stdout + r.stderr
    assert got.startswith("TOKEN=none ") and "--IdentityProvider.token= " in got and "old-pod-token" not in got, got
    assert console.read_text() == "" and not (home / "state" / "tokens.env").exists()
    # and with the lock, the same boot keeps the token (the pod's own, never on argv)
    seen.unlink()
    r = run({"JUPYTER_TOKEN": "locked-pod-token", "BASE_LINK_LOCK": "1"})
    for _ in range(50):
        if seen.exists():
            break
        time.sleep(0.1)
    got = seen.read_text()
    assert got.startswith("TOKEN=locked-pod-token ") and "--IdentityProvider.token" not in got, got
