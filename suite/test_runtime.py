"""unit tier, 3.1.0: the proven runtime (py/runtime.py, lib/48-runtime.sh, BASE_RUNTIME).

A buyer's machine installs what its maintainer's own sweep proved: ComfyUI, the uv Python and venv, the packs, the tools
venv and the wheel cache, as parts with a manifest that is the ONLY place their versions are written. These tests build
a runtime from a fake green tree, apply it to a fresh one, and install the stub package from it with every fetching tool
stubbed to fail loudly if it is ever called to fetch."""
import functools, http.server, json, os, pathlib, shutil, subprocess, tarfile, threading, time, pytest
pytestmark = [pytest.mark.unit, pytest.mark.skipif(not shutil.which("zstd"), reason="the runtime's parts are zstd tars: the machine needs zstd (CI has it)")]
BASE = pathlib.Path(__file__).resolve().parents[1]
RT = BASE / "py" / "runtime.py"


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def _repo(d, url=None):
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    if url:
        _git(d, "remote", "add", "origin", url)
    (d / "README").write_text("x")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    return _git(d, "rev-parse", "HEAD")


FAKE_PY = '#!/bin/bash\ncase "$*" in *"print(torch.version.cuda"*) echo "13.0";; *"import torch"*) echo "2.99.0 13.0";; *"import sageattention"*) exit 0;; *platform.python_version*) echo "3.99.0";; *) exec python3 "$@";; esac\n'


def _green_tree(root):
    """A machine after a green run: ComfyUI with one pack, a venv, the uv Python, the tools venv, a wheel, and the data
    and secrets a runtime must never carry."""
    c = root / "ComfyUI"
    _repo(c, "https://github.com/comfyanonymous/ComfyUI")
    (c / "comfyui_version.py").write_text('__version__ = "0.99.0"\n')
    (c / "main.py").write_text("")
    _repo(c / "custom_nodes" / "PackA", "https://github.com/o/PackA.git")
    from basetest import base_packs                                     # a real runtime carries the base's own packs too
    for p in base_packs():
        _repo(c / "custom_nodes" / p["dir"], p["url"])
    for d in ("models/loras", "output", "input", "user", "temp"):
        (c / d).mkdir(parents=True, exist_ok=True)
    (c / "models" / "loras" / "big.safetensors").write_bytes(b"\0" * 64)
    vend = c / "custom_nodes" / "ComfyUI-Paid-Vendored"; vend.mkdir(parents=True)          # a package's own pack: no .git
    (vend / "__init__.py").write_text("# paid code\n")
    (c / "output" / "mine.png").write_bytes(b"png")
    v = c / ".venv-cu130" / "bin"; v.mkdir(parents=True)
    (v / "python").write_text(FAKE_PY); (v / "python").chmod(0o755)
    (c / ".venv-cu130" / ".comfy-base-sageattention").write_text("sageattention-abc-cp399-torch2.99.0-sm_120")
    (c / "custom_nodes" / "PackA" / "__pycache__").mkdir(); (c / "custom_nodes" / "PackA" / "__pycache__" / "x.pyc").write_bytes(b"c")
    cb = root / "comfy-base"
    (cb / "python" / "cpython-3.99").mkdir(parents=True); (cb / "python" / "cpython-3.99" / "bin").mkdir()
    (cb / "bin").mkdir(); (cb / "bin" / "uv").write_text("#!/bin/sh\n")
    (cb / "tools.3.99.1").mkdir(); (cb / "tools.3.99.1" / "bin").mkdir(); (cb / "tools.3.99.1" / "bin" / "jupyter-lab").write_text("#!/bin/sh\n")
    (cb / "tools.3.99.1" / "bin" / "jupyter-lab").chmod(0o755)
    os.symlink("tools.3.99.1", cb / "tools")
    (cb / "state" / "wheels" / "sageattention-abc").mkdir(parents=True); (cb / "state" / "wheels" / "sageattention-abc" / "s.whl").write_bytes(b"w")
    (cb / "state" / "tokens.env").write_text("HF_TOKEN=secret\n")
    return c


def _build(root, out, part_bytes=None):
    args = ["python3", str(RT), "build", "--root", str(root), "--comfy", str(root / "ComfyUI"), "--out", str(out),
            "--torch-cuda", "13.0", "--base-version", "9.9.9", "--python", "3.99.0", "--sage-key", "sageattention-abc-cp399-torch2.99.0-sm_120"]
    if part_bytes:
        args += ["--part-bytes", str(part_bytes)]
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads((out / "runtime.json").read_text())


def _part_names(path):
    z = subprocess.run(["zstd", "-dc", "-q", str(path)], capture_output=True, check=True)
    tmp = path.with_suffix(".tar")
    tmp.write_bytes(z.stdout)
    with tarfile.open(tmp) as t:
        names = t.getnames()
    tmp.unlink()
    return names


def _names(out, man):
    return [n for p in man["parts"] for n in _part_names(out / p["name"])]


# ---------------------------------------------------------------- py/runtime.py

def test_unit_a_runtime_carries_code_never_data_or_secrets(tmp_path):
    src = tmp_path / "src"; _green_tree(src)
    out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    assert len(man["parts"]) > 1 and man["format"] == 2 and man["compression"] == "zstd"
    assert all(p["name"].endswith(".tar.zst") and len(p["sha256"]) == 64 and p["bytes"] > 0 for p in man["parts"])
    names = _names(out, man)
    assert "ComfyUI/main.py" in names and "ComfyUI/custom_nodes/PackA/README" in names and "ComfyUI/.venv-cu130/bin/python" in names
    assert "comfy-base/tools" in names and "comfy-base/tools.3.99.1/bin/jupyter-lab" in names and "comfy-base/state/wheels/sageattention-abc/s.whl" in names
    for never in ("ComfyUI/models", "ComfyUI/output", "ComfyUI/input", "ComfyUI/user", "ComfyUI/temp", "comfy-base/state/tokens.env"):
        assert not any(n == never or n.startswith(never + "/") for n in names), never
    assert not any("__pycache__" in n for n in names)
    assert not any("ComfyUI-Paid-Vendored" in n for n in names)             # 3.1.1: vendored packs come with their zip, never a runtime
    assert man["comfyui"]["version"] == "0.99.0" and man["driver_min"] == "580" and man["sageattention"]["key"].endswith("sm_120")
    packa = [p for p in man["packs"] if p["dir"] == "PackA"]
    assert len(packa) == 1 and packa[0]["url"] == "https://github.com/o/PackA.git" and len(man["packs"]) == 1 + len(__import__("basetest").base_packs())


def test_unit_the_driver_floor_comes_from_the_torch_build_never_the_capture_machine(tmp_path):
    """Issue #2: a runtime captured on a machine that runs driver 615 must install on a 580 machine, because its cu130 wheels
    need only 580. Capture runs with the machine's driver faked to 615; the apply side holds the real gate (BASE_FAKE_ROOT off)."""
    src = tmp_path / "src"; _green_tree(src); (src / "comfy-base" / "state" / "last-run").mkdir(parents=True)
    (src / "comfy-base" / "state" / "last-run" / "pkg").write_text("green 20261026-000000 1.0.0\n")
    cap = ('source "%s/base.sh"; eval "$(declare -f base_discover | sed "1s/base_discover/_orig_discover/")"; '
           'base_discover(){ _orig_discover "$@"; DRIVER=615.10; }; base_runtime_capture "%s"' % (BASE, tmp_path / "cap"))
    r = subprocess.run(["bash", "-c", cap], capture_output=True, text=True, env=_env(src))
    assert r.returncode == 0, r.stdout + r.stderr
    m = json.loads((tmp_path / "cap" / "runtime.json").read_text())
    assert m["driver_min"] == "580" and m["cuda"] == "13.0"
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    r = _base(["runtime-apply", str(tmp_path / "cap" / "runtime.json")], dst)
    assert r.returncode == 0, r.stdout + r.stderr
    gate = ('source "%s/base.sh"; base_env_setup; DRIVER=%s; _base_runtime_init >/dev/null && BASE_FAKE_ROOT= _base_driver_gate >/dev/null' % (BASE, "%s"))
    env = _env(dst, BASE_RUNTIME=str(dst / "comfy-base" / "state" / "runtime.json"))
    assert subprocess.run(["bash", "-c", gate % "580.178.04"], capture_output=True, text=True, env=env).returncode == 0
    assert subprocess.run(["bash", "-c", gate % "575.10"], capture_output=True, text=True, env=env).returncode != 0


def test_unit_the_driver_floor_table_and_a_build_with_no_cuda(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("runtime_mod", RT); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.driver_floor("13.0") == 580 and mod.driver_floor("13.2") == 580 and mod.driver_floor("12.8") == 525
    for bad in ("", None, "cpu", "99.0"):
        with pytest.raises(SystemExit):
            mod.driver_floor(bad)
    src = tmp_path / "src"; _green_tree(src)
    r = subprocess.run(["python3", str(RT), "build", "--root", str(src), "--comfy", str(src / "ComfyUI"), "--out", str(tmp_path / "o"), "--torch-cuda", ""],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "driver floor" in r.stderr


def test_unit_every_part_unpacks_alone_and_in_any_order(tmp_path):
    """3.29.3: independent parts. Each one unpacks into an empty folder by itself (the directories above its files ride in it),
    and the parts applied at once, largest first, make exactly the tree the parts applied one at a time make."""
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    assert len(man["parts"]) > 3
    for i, p in enumerate(man["parts"]):
        alone = tmp_path / ("alone%d" % i); alone.mkdir()
        with subprocess.Popen(["zstd", "-dc", "-q", str(out / p["name"])], stdout=subprocess.PIPE) as z:
            r = subprocess.run(["tar", "-xf", "-", "-C", str(alone)], stdin=z.stdout, capture_output=True, text=True)
        assert z.returncode == 0 and r.returncode == 0, r.stderr
    trees = {}
    for jobs in (1, 6):
        dst = tmp_path / ("dst%d" % jobs); dst.mkdir()
        r = subprocess.run(["python3", str(RT), "apply", str(out / "runtime.json"), str(out / "runtime.json"), "--root", str(dst),
                            "--work", str(tmp_path / ("w%d" % jobs)), "--jobs", str(jobs)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        trees[jobs] = sorted(str(p.relative_to(dst)) + ("->" + os.readlink(p) if p.is_symlink() else "") + ("" if p.is_symlink() or p.is_dir() else ":" + str(p.stat().st_size))
                             for p in dst.rglob("*"))
    assert trees[1] == trees[6] and "ComfyUI/main.py:0" in trees[1] and "comfy-base/tools->tools.3.99.1" in trees[1]
    assert not any("/output" in t or "tokens.env" in t for t in trees[1])


class _Server:
    """A loopback http server for the parts: counts the requests in flight at once, can demand a signature in the query."""

    def __init__(self, directory, need="", delay=0.0):
        self.live = self.peak = 0
        self.lock = threading.Lock()
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if outer.need and outer.need not in self.path:
                    self.send_error(403); return
                with outer.lock:
                    outer.live += 1; outer.peak = max(outer.peak, outer.live)
                try:
                    time.sleep(outer.delay)
                    super().do_GET()
                finally:
                    with outer.lock:
                        outer.live -= 1
        self.need, self.delay = need, delay
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(directory)))
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start(); return self

    def __exit__(self, *a):
        self.srv.shutdown(); self.srv.server_close(); self.thread.join()


def test_unit_apply_fetches_several_parts_at_once_from_a_manifest_url(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    dst = tmp_path / "dst"; dst.mkdir()
    with _Server(out, delay=0.15) as srv:
        (tmp_path / "m").mkdir(); (tmp_path / "m" / "runtime.json").write_bytes((out / "runtime.json").read_bytes())
        r = subprocess.run(["python3", str(RT), "apply", str(tmp_path / "m" / "runtime.json"), srv.url + "/runtime.json", "--root", str(dst),
                            "--work", str(tmp_path / "w"), "--jobs", "4", "--result", str(tmp_path / "res.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 2 <= srv.peak <= 4, srv.peak                                      # several at once, never more than --jobs
    assert (dst / "ComfyUI" / "custom_nodes" / "PackA" / "README").exists() and not (tmp_path / "w" / "runtime.000.tar.zst").exists()
    res = json.loads((tmp_path / "res.json").read_text())
    assert res["parts"] == len(man["parts"]) and res["bytes"] == sum(p["bytes"] for p in man["parts"]) and res["jobs"] == 4 and res["gbps"] >= 0


def test_unit_signed_links_beside_a_local_manifest_are_used_and_never_printed(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    held = tmp_path / "held"; held.mkdir(); (held / "runtime.json").write_bytes((out / "runtime.json").read_bytes())      # no parts beside it
    dst = tmp_path / "dst"; dst.mkdir()
    with _Server(out, need="sig=s3cret") as srv:
        links = [{"name": p["name"], "url": "%s/%s?sig=s3cret" % (srv.url, p["name"]), "sha256": p["sha256"], "bytes": p["bytes"]} for p in man["parts"]]
        mraw = (held / "runtime.json").read_bytes()               # the site's entitlement `runtime` object, as it comes
        ent = {"id": "rt-1", "manifest": {"url": srv.url + "/runtime.json", "sha256": __import__("hashlib").sha256(mraw).hexdigest(), "bytes": len(mraw)}, "parts": links}
        (held / "links.json").write_text(json.dumps(ent))
        r = subprocess.run(["python3", str(RT), "apply", str(held / "runtime.json"), str(held / "runtime.json"), "--parts", str(held / "links.json"),
                            "--root", str(dst), "--work", str(tmp_path / "w")], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "s3cret" not in r.stdout + r.stderr
        ent["manifest"]["sha256"] = "0" * 64                       # links that name another manifest are refused before any fetch
        (held / "links.json").write_text(json.dumps(ent))
        other = subprocess.run(["python3", str(RT), "apply", str(held / "runtime.json"), str(held / "runtime.json"), "--parts", str(held / "links.json"),
                                "--root", str(tmp_path / "dx"), "--work", str(tmp_path / "wx")], capture_output=True, text=True)
        assert other.returncode != 0 and "not the one the link list names" in other.stderr and not (tmp_path / "dx").exists()
        (held / "links.json").write_text(json.dumps(links))        # and a bare list of parts is accepted too
        links[1]["url"] = "%s/%s?sig=wrong" % (srv.url, man["parts"][1]["name"])              # a link the server refuses
        (held / "links.json").write_text(json.dumps(links))
        dst2 = tmp_path / "dst2"; dst2.mkdir()
        bad = subprocess.run(["python3", str(RT), "apply", str(held / "runtime.json"), str(held / "runtime.json"), "--parts", str(held / "links.json"),
                              "--root", str(dst2), "--work", str(tmp_path / "w2")], capture_output=True, text=True)
    assert bad.returncode == 1 and man["parts"][1]["name"] in bad.stderr and "wrong" not in bad.stdout + bad.stderr
    links[0]["bytes"] += 1; (held / "links.json").write_text(json.dumps(links))
    odd = subprocess.run(["python3", str(RT), "sources", str(held / "runtime.json"), str(held / "runtime.json"), "--parts", str(held / "links.json")], capture_output=True, text=True)
    assert odd.returncode != 0 and "manifest says" in odd.stderr


def test_unit_a_runtime_of_the_old_format_is_refused_by_name(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out)
    m = json.loads((out / "runtime.json").read_text()); m["format"] = 1; (out / "runtime.json").write_text(json.dumps(m))
    r = subprocess.run(["python3", str(RT), "apply", str(out / "runtime.json"), str(out / "runtime.json"), "--root", str(tmp_path / "d"), "--work", str(tmp_path / "w")],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "capture it again" in r.stderr


def test_unit_runtime_apply_records_a_runtime_row_in_the_timing_ledger(tmp_path):
    src, out, dst = _applied(tmp_path)
    logs = dst / "comfy-base" / "state" / "logs"
    timings = "".join(f.read_text() for f in logs.glob("runtime-apply_*.timings.tsv"))
    timeline = "".join(f.read_text() for f in logs.glob("runtime-apply_*.timeline.tsv"))
    assert "RUNTIME fetch and unpack" in timings and "GB/s" in timings and "parts" in timings
    row = [ln for ln in timeline.splitlines() if "RUNTIME fetch and unpack" in ln]
    assert len(row) == 1 and row[0].split("\t")[0].isdigit()
    res = json.loads((dst / "comfy-base" / "state" / "runtime-apply.json").read_text())
    assert res["bytes"] > 0 and res["raw"] > 0 and res["parts"] > 1 and "gbps" in res


def test_unit_runtime_apply_takes_jobs_and_a_manifest_url(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out, part_bytes=40000)
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    with _Server(out, delay=0.1) as srv:
        r = _base(["runtime-apply", srv.url + "/runtime.json", "--jobs", "3"], dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 2 <= srv.peak <= 3 and (dst / "comfy-base" / "state" / "runtime.stamp").exists()
    assert _base(["runtime-apply", "x.json", "--jobs", "0"], dst).returncode == 2


def test_unit_verify_parts_names_the_first_part_that_differs(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    ok = subprocess.run(["python3", str(RT), "verify-parts", str(out / "runtime.json"), str(out)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    p = out / man["parts"][1]["name"]; b = bytearray(p.read_bytes()); b[0] ^= 1; p.write_bytes(bytes(b))
    bad = subprocess.run(["python3", str(RT), "verify-parts", str(out / "runtime.json"), str(out)], capture_output=True, text=True)
    assert bad.returncode == 1 and man["parts"][1]["name"] in bad.stderr


def test_unit_check_tree_and_covers_name_every_difference(tmp_path):
    src = tmp_path / "src"; c = _green_tree(src); out = tmp_path / "out"; _build(src, out)
    m = str(out / "runtime.json")
    assert subprocess.run(["python3", str(RT), "check-tree", m, "--comfy", str(c)]).returncode == 0
    (c / "custom_nodes" / "PackA" / "more").write_text("y"); _git(c / "custom_nodes" / "PackA", "add", "-A")
    _git(c / "custom_nodes" / "PackA", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "moved")
    r = subprocess.run(["python3", str(RT), "check-tree", m, "--comfy", str(c)], capture_output=True, text=True)
    assert r.returncode == 1 and "pack PackA" in r.stderr
    r = subprocess.run(["python3", str(RT), "covers", m, "https://github.com/o/packa", "https://github.com/o/PackB"], capture_output=True, text=True)
    assert r.returncode == 1 and "PackB" in r.stderr and "packa" not in r.stderr.lower().replace("packb", "")


def test_unit_sources_resolve_relative_parts_beside_the_manifest(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out)
    local = subprocess.run(["python3", str(RT), "sources", str(out / "runtime.json"), str(out / "runtime.json")], capture_output=True, text=True).stdout
    name, where, n = local.splitlines()[0].split("\t")
    assert where == str((out / name).resolve()) and int(n) > 0
    remote = subprocess.run(["python3", str(RT), "sources", str(out / "runtime.json"), "https://github.com/o/r/releases/download/v1/runtime.json"],
                            capture_output=True, text=True).stdout
    assert remote.splitlines()[0].split("\t")[1] == "https://github.com/o/r/releases/download/v1/" + name


# ---------------------------------------------------------------- runtime-apply and runtime-capture

def _env(root, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("BASE_")}
    e.update({"BASE_FAKE_ROOT": str(root), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(root)})
    e.update(extra)
    return e


def _base(cmd, root, **extra):
    return subprocess.run(["bash", str(BASE / "base.sh"), *cmd], capture_output=True, text=True, env=_env(root, **extra))


def _applied(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out, part_bytes=40000)
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    r = _base(["runtime-apply", str(out / "runtime.json")], dst)
    assert r.returncode == 0, r.stdout + r.stderr
    return src, out, dst


def test_unit_runtime_apply_unpacks_stamps_and_is_idempotent(tmp_path):
    src, out, dst = _applied(tmp_path)
    assert (dst / "ComfyUI" / "custom_nodes" / "PackA" / "README").exists() and os.path.islink(dst / "comfy-base" / "tools")
    state = dst / "comfy-base" / "state"
    assert (state / "runtime.json").exists() and len((state / "runtime.stamp").read_text().strip()) == 64
    assert not (dst / "ComfyUI" / "output").exists() and not (state / "tokens.env").exists()
    again = _base(["runtime-apply", str(out / "runtime.json")], dst)
    assert again.returncode == 0 and "already applied" in again.stdout


def test_unit_a_corrupt_part_fails_the_apply_by_name_and_stamps_nothing(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=40000)
    p = out / man["parts"][0]["name"]; b = bytearray(p.read_bytes()); b[-1] ^= 1; p.write_bytes(bytes(b))
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    r = _base(["runtime-apply", str(out / "runtime.json")], dst)
    assert r.returncode != 0 and man["parts"][0]["name"] in r.stdout + r.stderr and "sha256" in r.stdout + r.stderr
    assert not (dst / "comfy-base" / "state" / "runtime.stamp").exists()           # parts unpack as they land: the stamp is what a run trusts


def test_unit_runtime_apply_refuses_a_shared_store(tmp_path):
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    r = _base(["runtime-apply", str(tmp_path / "nowhere.json")], dst, BASE_VOLUME_SHARED="1")
    assert r.returncode == 0 and "operator's shape" in r.stdout


def test_unit_capture_needs_every_last_run_green(tmp_path):
    src = tmp_path / "src"; _green_tree(src); (src / "comfy-base" / "state" / "last-run").mkdir(parents=True)
    (src / "comfy-base" / "state" / "last-run" / "pkg").write_text("red 20260925-000000 1.0.0\n")
    r = _base(["runtime-capture", str(tmp_path / "cap")], src)
    assert r.returncode != 0 and "not green" in r.stdout + r.stderr
    (src / "comfy-base" / "state" / "last-run" / "pkg").write_text("green 20260925-000000 1.0.0\n")
    r = _base(["runtime-capture", str(tmp_path / "cap")], src)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PackA" in [p["dir"] for p in json.loads((tmp_path / "cap" / "runtime.json").read_text())["packs"]]


def test_unit_the_summary_keeps_each_packages_verdict():
    body = (BASE / "lib" / "95-summary.sh").read_text()
    s = body[body.index("base_summary(){"):]
    assert '"$BASE_STATE/last-run/$PKG_ID"' in s and s.index("last-run") < s.index("exit 0")


# ---------------------------------------------------------------- BASE_RUNTIME: an install that fetches nothing

def test_unit_a_manifest_this_machine_has_not_applied_is_refused(tmp_path):
    src, out, dst = _applied(tmp_path)
    other = tmp_path / "other.json"; other.write_text((out / "runtime.json").read_text().replace("9.9.9", "9.9.8"))
    r = subprocess.run(["bash", "-c", 'source "%s/base.sh"; base_env_setup; _base_runtime_init' % BASE], capture_output=True, text=True,
                       env=_env(dst, BASE_RUNTIME=str(other)))
    assert r.returncode == 2 and "has not applied" in r.stdout + r.stderr


def test_unit_the_stub_installs_from_the_runtime_with_no_git_pip_or_uv_fetch(tmp_path):
    from basetest import run_script
    stub = BASE / "_build" / "stub"
    if not stub.exists():
        pytest.skip("_build/stub is repo tooling")
    src, out, dst = _applied(tmp_path)
    shutil.copytree(stub, dst / "pkg")
    (dst / "somewhere").mkdir(); (dst / "somewhere" / "stub_local.safetensors").write_bytes(b"mine")
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    real_git = shutil.which("git")
    (bin_ / "git").write_text('#!/bin/bash\necho "git $*" >> "%s"\ncase " $* " in *" clone "*|*" fetch "*|*" pull "*|*" ls-remote "*) exit 97;; esac\nexec "%s" "$@"\n' % (log, real_git))
    for tool in ("uv", "pip", "pip3", "apt-get"):
        (bin_ / tool).write_text('#!/bin/bash\necho "%s $*" >> "%s"\nexit 97\n' % (tool, log))
    for f in bin_.iterdir():
        f.chmod(0o755)
    r = run_script(dst / "pkg" / "stub-script.sh", fake_root=dst,
                   env={"BASE_RUNTIME": str(dst / "comfy-base" / "state" / "runtime.json"), "PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_NO_SUITE": "1"})
    calls = log.read_text() if log.exists() else ""
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1500:]
    lines = [ln for ln in calls.splitlines() if ln.strip()]
    for ln in lines:                                                     # reading a version is not fetching; nothing else may run
        tool, _, rest = ln.partition(" ")
        if tool == "git":
            assert not any(w in rest.split() for w in ("clone", "fetch", "pull", "ls-remote")), ln
        else:
            assert tool == "uv" and rest.strip() == "--version", ln
    assert "runtime mode" in r.stdout and "runtime " in r.stdout
    assert (dst / "ComfyUI" / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists()      # 3.30.1: fetched by the early pass
    tl = "".join(f.read_text() for f in (dst / "comfy-base" / "state" / "logs").glob("stub_*.timeline.tsv"))
    assert "MODELS (in the background" in tl and "RUNTIME (in the background" not in tl      # the runtime was applied before this run: no wait


def test_unit_run_order_keeps_the_3_0_steps_for_every_plain_run():
    """Runtime mode is a branch, not an edit: the plain path still runs every 3.0.0 step, in the 3.0.0 order."""
    body = (BASE / "lib" / "95-summary.sh").read_text()
    run = body[body.index("base_install(){"):body.index("_base_use_testbed(){")]
    plain = run[run.index('  else\n  if [ "$sysbg" = 1 ]'):]              # 3.5.0: the plain branch opens by joining the background OS stage
    for a, b in (("_base_system_join", "base_venv_snapshot"), ("base_venv_snapshot", "if ! base_venv"), ("if ! base_venv", "base_packs pip"), ("base_packs pip", "base_mcp"), ("base_mcp", "base_venv_latest")):
        assert plain.index(a) < plain.index(b)
    assert "else base_system || src=$?; fi" in run and "else base_update_comfyui || true; fi" in run


# ---------------------------------------------------------------- 3.30.1: the models start while the runtime is still unpacking

def _need_stub():
    stub = BASE / "_build" / "stub"
    if not stub.exists():
        pytest.skip("_build/stub is repo tooling")
    return stub


def _install_beside_an_apply(dst, tmp_path, extra=None):
    from basetest import run_script
    stub = _need_stub()
    shutil.copytree(stub, dst / "pkg")
    (dst / "somewhere").mkdir(exist_ok=True); (dst / "somewhere" / "stub_local.safetensors").write_bytes(b"mine")
    env = {"BASE_RUNTIME": str(dst / "comfy-base" / "state" / "runtime.json"), "BASE_NO_SUITE": "1"}
    env.update(extra or {})
    return run_script(dst / "pkg" / "stub-script.sh", fake_root=dst, env=env)


def _spans(dst):
    rows = {}
    for f in (dst / "comfy-base" / "state" / "logs").glob("stub_*.timeline.tsv"):
        for ln in f.read_text().splitlines():
            if ln and not ln.startswith("#"):
                t0, t1, secs, what = ln.split("\t", 3)
                rows[what] = (int(t0), int(t1))
    return rows


def test_unit_the_models_start_before_the_unpack_ends_and_only_the_tree_waits(tmp_path):
    _need_stub()                                                  # before the apply is started: a skip must leave nothing running
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out, part_bytes=80000)
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    lock = dst / "comfy-base" / "state" / "runtime.applying"
    with _Server(out, delay=1.5) as srv:
        with subprocess.Popen(["bash", str(BASE / "base.sh"), "runtime-apply", srv.url + "/runtime.json", "--jobs", "1"], env=_env(dst),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as ap:      # the with waits for it and closes its pipe
            try:
                for _ in range(200):
                    if lock.exists():
                        break
                    time.sleep(0.05)
                assert lock.exists()
                r = _install_beside_an_apply(dst, tmp_path)
                apply_out = ap.communicate(timeout=120)[0]
            except BaseException:
                ap.kill()                                         # a failed assertion must not leave the apply running to be finalized later
                raise
    assert ap.returncode == 0, apply_out
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1500:]
    assert "still unpacking" in r.stdout and "the runtime is unpacked" in r.stdout
    assert not lock.exists() and (dst / "comfy-base" / "state" / "runtime.stamp").exists()
    spans = _spans(dst)
    models = spans["MODELS (in the background, beside the venv build)"]
    unpack = next(v for k, v in spans.items() if k.startswith("RUNTIME (in the background"))
    waited = spans["RUNTIME · waiting for the unpack"]
    assert models[0] < unpack[1]                                  # the downloads began before the unpack ended
    assert waited[1] >= unpack[1] - 1 and waited[0] < unpack[1]   # and the tree is what waited for it
    assert "COMFYUI · the proven runtime" in spans and spans["COMFYUI · the proven runtime"][0] >= waited[1]
    assert (dst / "ComfyUI" / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists()
    assert (dst / "ComfyUI" / "custom_nodes" / "PackA" / "README").exists()


def test_unit_an_apply_that_ends_without_a_stamp_fails_the_install_by_name(tmp_path):
    _need_stub()
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out)
    dst = tmp_path / "dst"; st = dst / "comfy-base" / "state"; st.mkdir(parents=True)
    shutil.copy(out / "runtime.json", st / "runtime.json")
    sha = __import__("hashlib").sha256((st / "runtime.json").read_bytes()).hexdigest()
    pid = subprocess.run(["sh", "-c", "sleep 4 </dev/null >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout.strip()   # reaped by init
    (st / "runtime.applying").write_text("%s %s\n" % (pid, sha))
    r = _install_beside_an_apply(dst, tmp_path)
    assert r.returncode != 0 and "ended without finishing" in r.stdout + r.stderr
    assert not (st / "runtime.stamp").exists()


def test_unit_a_lock_whose_process_is_gone_is_no_lock_and_a_library_waits_for_the_tree(tmp_path):
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    (dst / "comfy-base" / "state" / "runtime.applying").write_text("999999 abc\n")
    snippet = 'source "%s/base.sh"; base_env_setup; _base_runtime_applying abc && echo LOCKED || echo FREE; ' % BASE
    r = subprocess.run(["bash", "-c", snippet + '_base_runtime_early_ok && echo EARLY || echo LATE'], capture_output=True, text=True, env=_env(dst))
    assert r.stdout.split() == ["FREE", "EARLY"], r.stdout + r.stderr
    (tmp_path / "lib").mkdir()                                  # base_env_setup ignores a BASE_LIBRARY that is not a directory
    r = subprocess.run(["bash", "-c", snippet + '_base_runtime_early_ok && echo EARLY || echo LATE'], capture_output=True, text=True, env=_env(dst, BASE_LIBRARY=str(tmp_path / "lib")))
    assert r.stdout.split() == ["FREE", "LATE"], r.stdout + r.stderr
