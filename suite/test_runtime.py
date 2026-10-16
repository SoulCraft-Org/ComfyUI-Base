"""unit tier, 3.1.0: the proven runtime (py/runtime.py, lib/48-runtime.sh, BASE_RUNTIME).

A buyer's machine installs what its maintainer's own sweep proved: ComfyUI, the uv Python and venv, the packs, the tools
venv and the wheel cache, as parts with a manifest that is the ONLY place their versions are written. These tests build
a runtime from a fake green tree, apply it to a fresh one, and install the stub package from it with every fetching tool
stubbed to fail loudly if it is ever called to fetch."""
import json, os, pathlib, shutil, subprocess, tarfile, pytest
pytestmark = pytest.mark.unit
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


FAKE_PY = '#!/bin/bash\ncase "$*" in *"import torch"*) echo "2.99.0 13.0";; *"import sageattention"*) exit 0;; *platform.python_version*) echo "3.99.0";; *) exec python3 "$@";; esac\n'


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
            "--driver-min", "580", "--base-version", "9.9.9", "--python", "3.99.0", "--sage-key", "sageattention-abc-cp399-torch2.99.0-sm_120"]
    if part_bytes:
        args += ["--part-bytes", str(part_bytes)]
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads((out / "runtime.json").read_text())


def _names(out, man):
    data = b"".join((out / p["name"]).read_bytes() for p in man["parts"])
    tmp = out / "joined.tar.gz"; tmp.write_bytes(data)
    with tarfile.open(tmp) as t:
        names = t.getnames()
    tmp.unlink()
    return names


# ---------------------------------------------------------------- py/runtime.py

def test_unit_a_runtime_carries_code_never_data_or_secrets(tmp_path):
    src = tmp_path / "src"; _green_tree(src)
    out = tmp_path / "out"; man = _build(src, out, part_bytes=700)
    assert len(man["parts"]) > 1 and all(p["bytes"] <= 700 for p in man["parts"])
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


def test_unit_verify_parts_names_the_first_part_that_differs(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=700)
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
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; _build(src, out, part_bytes=700)
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


def test_unit_a_corrupt_part_stops_before_anything_is_unpacked(tmp_path):
    src = tmp_path / "src"; _green_tree(src); out = tmp_path / "out"; man = _build(src, out, part_bytes=700)
    p = out / man["parts"][0]["name"]; b = bytearray(p.read_bytes()); b[-1] ^= 1; p.write_bytes(bytes(b))
    dst = tmp_path / "dst"; (dst / "comfy-base" / "state").mkdir(parents=True)
    r = _base(["runtime-apply", str(out / "runtime.json")], dst)
    assert r.returncode != 0 and "do not match" in r.stdout + r.stderr
    assert not (dst / "ComfyUI").exists() and not (dst / "comfy-base" / "state" / "runtime.stamp").exists()


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


def test_unit_run_order_keeps_the_3_0_steps_for_every_plain_run():
    """Runtime mode is a branch, not an edit: the plain path still runs every 3.0.0 step, in the 3.0.0 order."""
    body = (BASE / "lib" / "95-summary.sh").read_text()
    run = body[body.index("base_install(){"):body.index("_base_use_testbed(){")]
    plain = run[run.index('  else\n  if [ "$sysbg" = 1 ]'):]              # 3.5.0: the plain branch opens by joining the background OS stage
    for a, b in (("_base_system_join", "base_venv_snapshot"), ("base_venv_snapshot", "if ! base_venv"), ("if ! base_venv", "base_packs pip"), ("base_packs pip", "base_mcp"), ("base_mcp", "base_venv_latest")):
        assert plain.index(a) < plain.index(b)
    assert "else base_system || src=$?; fi" in run and "else base_update_comfyui || true; fi" in run
