"""install tier: the one-pass forms (3.2.0) run end to end against a fake pod, with the stub package.

`preflight` is what podctl runs before an install in place of a whole `--check` pipeline: it must stop on exactly
what would stop the real run before it changes anything, and do nothing else (no suite, no uv, no walk)."""
import json, pathlib, pytest
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"
pytestmark = [pytest.mark.install, pytest.mark.skipif(not STUB.exists(), reason="_build/stub is repo tooling, not in the shipped base")]
SCRIPT = STUB / "stub-script.sh"
from basetest import fake_pod, run_script, tree_hash


def test_install_preflight_changes_nothing_and_runs_no_pipeline(tmp_path):
    fake_pod(tmp_path, STUB, strays=True)
    before = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r = run_script(SCRIPT, "preflight", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == before
    assert "PREFLIGHT" in r.stdout and "nothing stops the install" in r.stdout
    for stage in ("══ VENV", "══ NODE PACKS", "══ MODEL LIBRARY", "══ TEST SUITE", "══ CONSOLIDATE"):
        assert stage not in r.stdout, stage


def test_install_preflight_stops_on_a_workflow_that_does_not_match_its_script(tmp_path):
    fake_pod(tmp_path, STUB)
    wf = tmp_path / "pkg" / "stub-workflow.json"
    d = json.loads(wf.read_text(encoding="utf-8")); d.setdefault("extra", {})["package_version"] = "0.0.1"
    wf.write_text(json.dumps(d), encoding="utf-8")
    r = run_script(SCRIPT, "preflight", fake_root=tmp_path)
    assert r.returncode == 1, r.stdout[-3000:] + r.stderr[-1000:]
    assert "ship together" in r.stdout


def test_install_preflight_is_in_the_help(tmp_path):
    fake_pod(tmp_path, STUB)
    r = run_script(SCRIPT, "help", fake_root=tmp_path)
    assert r.returncode == 0 and "preflight" in r.stdout, r.stdout


def _own_package(tmp_path, nodes="A = 1\n", extra=None, version="1.0.0"):
    """The stub package plus one own pack (VENDORED_PACKS), built beside the fake pod the way a real zip would extract."""
    import shutil
    src = tmp_path / "src" / "stub"
    if src.exists(): shutil.rmtree(src)
    shutil.copytree(STUB, src)
    s = (src / "stub-script.sh").read_text(encoding="utf-8")
    (src / "stub-script.sh").write_text(s.replace("PKG_NO_SUITE=1", 'PKG_NO_SUITE=1\nVENDORED_PACKS=( "ComfyUI-Stub-Own" )'), encoding="utf-8")
    pack = src / "ComfyUI-Stub-Own"; (pack / "stub_own").mkdir(parents=True)
    (pack / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n", encoding="utf-8")
    (pack / "pyproject.toml").write_text('[project]\nname = "comfyui-stub-own"\nversion = "%s"\n' % version, encoding="utf-8")
    (pack / "stub_own" / "nodes.py").write_text(nodes, encoding="utf-8")
    for rel, text in (extra or {}).items():
        (pack / rel).write_text(text, encoding="utf-8")
    return src


def _refresh_pkg(tmp_path, src):
    """A new zip extracted over the old one: the package folder on the fake pod takes the new files."""
    import shutil
    dst = tmp_path / "pkg"
    shutil.rmtree(dst / "ComfyUI-Stub-Own", ignore_errors=True)
    shutil.copytree(src / "ComfyUI-Stub-Own", dst / "ComfyUI-Stub-Own")
    shutil.copy(src / "stub-script.sh", dst / "stub-script.sh")


def test_install_own_packs_are_mirrored_recorded_and_left_alone_when_unchanged(tmp_path):
    src = _own_package(tmp_path, extra={"stub_own/old.py": "dropped later\n"})
    pod = fake_pod(tmp_path, src)
    r = run_script(src / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    live = pod / "custom_nodes" / "ComfyUI-Stub-Own"
    assert (live / "stub_own" / "nodes.py").read_text() == "A = 1\n" and (live / ".comfy-base-own").exists()
    assert "OWN PACKS" in r.stdout and "mirrored into custom_nodes" in r.stdout
    manifest = (tmp_path / "comfy-base" / "state" / "packages" / "stub.manifest").read_text()
    assert "own\tComfyUI-Stub-Own\t1.0.0\t" in manifest
    assert "time " in r.stdout and "slowest first" in r.stdout
    # the same zip again: nothing on the store moves
    h = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r = run_script(src / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0 and "ComfyUI-Stub-Own 1.0.0 unchanged" in r.stdout, r.stdout[-3000:]
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == h
    # a new release of the pack: mirrored, and the file it no longer ships is gone
    src = _own_package(tmp_path, nodes="A = 2\n", version="1.0.1"); _refresh_pkg(tmp_path, src)
    r = run_script(src / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert (live / "stub_own" / "nodes.py").read_text() == "A = 2\n" and not (live / "stub_own" / "old.py").exists()
    assert "(was 1.0.0)" in r.stdout


def test_install_the_declarations_dump_names_the_own_packs(tmp_path):
    src = _own_package(tmp_path)
    fake_pod(tmp_path, src)
    r = run_script(src / "stub-script.sh", fake_root=tmp_path, env={"BASE_DECLARE_ONLY": "1"})
    assert r.returncode == 0, r.stderr
    own = [l for l in r.stdout.splitlines() if l.startswith("OWNROW ")]
    assert len(own) == 1 and own[0].split(" ", 1)[1].split("|")[:2] == ["ComfyUI-Stub-Own", "1.0.0"], r.stdout
