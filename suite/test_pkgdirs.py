"""unit tier: package discovery (3.6.0). A consumer keeps its packages either under <brand>/packages/<name>/ beside a
base submodule at base/comfyui-base, or as one repository per package cloned side by side under a workspace as
<name>/, with the base read from $COMFY_BASE. py/pkgdirs.py is the one place both rules live; every walker
(package.py --all, podctl's pin scan, verify.sh, testbed.sh, basetest.converted_packages) asks it."""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
PKGDIRS = BASE / "py" / "pkgdirs.py"
sys.path.insert(0, str(BASE / "py"))


def _repo_only(what="_build/"):
    if not (BASE / "_build").exists():
        pytest.skip("%s is repo tooling, not in the shipped base" % what)


def _env(**extra):
    """The caller's environment without any BASE_* (a session that exports BASE_WORKSPACE must not steer these tests)."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("BASE_") and k != "ROOT"}
    e.update(extra)
    return e


def _tree(tmp_path):
    """Both shapes at once, plus every decoy the rule must refuse. Returns (root, [p1, brandB-p2]) resolved."""
    root = (tmp_path / "ws").resolve()
    p1 = root / "brandA" / "packages" / "p1"; p1.mkdir(parents=True)
    (p1 / "p1-script.sh").write_text('WF_NAME=""\nPKG_ID="p1"\n')
    p2 = root / "brandB-p2"; p2.mkdir()
    (p2 / "brandB-p2-script.sh").write_text('WF_NAME=""\nPKG_ID="brandB-p2"\n')
    (root / "brandC-p3").mkdir(); (root / "brandC-p3" / "other-script.sh").write_text("# not <name>-script.sh\n")
    (root / "brandA" / "packages" / "p4").mkdir()                              # a package folder without its script
    (root / "p1").symlink_to(p1, target_is_directory=True)                      # the same package reached twice
    for dot in (".git", ".claude", ".superpowers"):
        (root / dot).mkdir()
        (root / dot / ("%s-script.sh" % dot)).write_text("# never a package\n")
    cb = root / "comfyui-base"; cb.mkdir()                                     # a base checkout inside the workspace
    (cb / "comfyui-base-script.sh").write_text("# the base's own script\n"); (cb / "base.sh").write_text("# base\n")
    return root, [p1, p2]


def test_unit_package_dirs_finds_both_shapes_once_each_and_nothing_else(tmp_path):
    import pkgdirs
    root, want = _tree(tmp_path)
    assert pkgdirs.package_dirs(root) == sorted(want)
    assert pkgdirs.package_dirs(tmp_path / "empty-nowhere") == []


def test_unit_consumer_root_precedence_explicit_env_submodule_none(tmp_path):
    import pkgdirs
    sub = tmp_path / "repo"; base = sub / "base" / "comfyui-base"; base.mkdir(parents=True)
    solo = tmp_path / "iso" / "deep" / "solo"; solo.mkdir(parents=True)
    ex = tmp_path / "explicit"; ex.mkdir(); ws = tmp_path / "ws"; ws.mkdir()
    env = {"BASE_WORKSPACE": str(ws)}
    assert pkgdirs.consumer_root(base, str(ex), env) == ex.resolve()           # 1. explicit wins over everything
    assert pkgdirs.consumer_root(base, None, env) == ws.resolve()              # 2. then $BASE_WORKSPACE
    assert pkgdirs.consumer_root(base, None, {}) == sub.resolve()              # 3. then the submodule shape
    assert pkgdirs.consumer_root(base, "", {"BASE_WORKSPACE": ""}) == sub.resolve()   # empty means unset
    assert pkgdirs.consumer_root(solo, None, {}) is None                        # 4. else no root at all


def test_unit_pkgdirs_main_prints_one_package_dir_per_line(tmp_path):
    root, want = _tree(tmp_path)
    r = subprocess.run([sys.executable, str(PKGDIRS), str(root)], capture_output=True, text=True, env=_env())
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [str(p) for p in sorted(want)]
    r = subprocess.run([sys.executable, str(PKGDIRS), "--root"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(root)))
    assert r.returncode == 0 and r.stdout.strip() == str(root), r.stdout + r.stderr


def test_unit_package_py_all_walks_the_named_root_or_base_workspace(tmp_path):
    """--all over --root, and over $BASE_WORKSPACE, in --check mode: both packages are named and no zip is written."""
    _repo_only("_build/package.py")
    root, want = _tree(tmp_path)
    for args, env in (([ "--root", str(root)], _env()), ([], _env(BASE_WORKSPACE=str(root)))):
        r = subprocess.run([sys.executable, str(BASE / "_build" / "package.py"), "--all", "--check", *args], capture_output=True, text=True, env=env)
        assert "p1-runpod.zip: no zip on disk" in r.stdout and "brandB-p2-runpod.zip: no zip on disk" in r.stdout, r.stdout + r.stderr
        assert "nothing to walk" not in r.stdout
    assert not list(root.rglob("*.zip"))


def _load_podctl():
    spec = importlib.util.spec_from_file_location("podctl_pkgdirs", BASE / "_build" / "pod" / "podctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unit_podctl_pin_scan_walks_the_workspace_and_the_bases_own_pack_list(tmp_path):
    """The pin scan finds every other package of the consumer root, in either shape, and the base's OWN lib/40-packs.sh
    (the one beside podctl, not a <root>/base/comfyui-base that a workspace does not have). Read only."""
    _repo_only("_build/pod/podctl.py")
    podctl = _load_podctl()
    root, (p1, p2) = _tree(tmp_path)
    for p in (p1, p2):
        s = p / ("%s-script.sh" % p.name)
        s.write_text(s.read_text() + 'PACKS=(\n "Shared|https://github.com/x/Shared|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa||both"\n)\n')
    own = BASE / "lib" / "40-packs.sh"
    m = re.search(r'"([^"|]+)\|(https://[^"|]+)\|([0-9a-f]{40})\|', own.read_text(encoding="utf-8"))
    assert m, "no pinned row in the base's own 40-packs.sh to measure against"
    runner = tmp_path / "elsewhere" / "runner"; runner.mkdir(parents=True)
    (runner / "runner-script.sh").write_text("# the package whose run measured the rows\n")
    rows = podctl.pin_rows('  "Shared|https://github.com/x/Shared|dddddddddddddddddddddddddddddddddddddddd||both"\n'
                           '  "%s|%s|%s||x"\n' % (m.group(1), m.group(2), "0" * 40))
    before = own.read_text(encoding="utf-8")
    report = podctl.pins_elsewhere(runner / "runner-script.sh", rows, env={"BASE_WORKSPACE": str(root)})
    assert {pathlib.Path(r[0]).resolve() for r in report} == {p1 / "p1-script.sh", p2 / "brandB-p2-script.sh", own.resolve()}, report
    assert own.read_text(encoding="utf-8") == before


def test_unit_verify_sh_walks_packages_through_pkgdirs(tmp_path):
    """verify.sh lists its packages with `python3 py/pkgdirs.py <root>` (no */packages/* glob of its own), and that
    command prints exactly the two packages of a mixed tree."""
    _repo_only("_build/verify.sh")
    src = (BASE / "_build" / "verify.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "py/pkgdirs.py" in code and "/packages/" not in code, "verify.sh must ask py/pkgdirs.py, not glob"
    assert '${ROOT:+' not in code, "verify.sh takes no ROOT= input: $BASE_WORKSPACE is the shell walkers' only override"
    root, want = _tree(tmp_path)
    r = subprocess.run(["python3", "py/pkgdirs.py", str(root)], cwd=BASE, capture_output=True, text=True, env=_env())
    assert r.returncode == 0 and r.stdout.splitlines() == [str(p) for p in sorted(want)], r.stdout + r.stderr


def test_unit_testbed_sh_walks_packages_through_pkgdirs(tmp_path):
    """testbed.sh takes its packages (for list-packs and the vendored links) from py/pkgdirs.py, with the root named by
    $BASE_WORKSPACE: a flat workspace outside the base is found, both packages counted. A bare ROOT= is not an input."""
    _repo_only("the repo's testbed.sh")
    src = (BASE / "testbed.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "pkgdirs.py" in code and "*/packages/*" not in code, "testbed.sh must ask py/pkgdirs.py, not glob"
    root, want = _tree(tmp_path)
    r = subprocess.run(["python3", "py/pkgdirs.py", str(root)], cwd=BASE, capture_output=True, text=True, env=_env())
    assert r.returncode == 0 and r.stdout.splitlines() == [str(p) for p in sorted(want)], r.stdout + r.stderr
    s = subprocess.run(["bash", str(BASE / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(root)))
    assert s.returncode == 0, s.stdout + s.stderr
    assert "packages under %s (2)" % root in s.stdout, s.stdout + s.stderr
    s = subprocess.run(["bash", str(BASE / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env(ROOT=str(root)))
    assert s.returncode == 0 and "packages under %s" % root not in s.stdout, s.stdout + s.stderr


def test_unit_a_named_root_that_does_not_exist_is_an_error_not_an_empty_workspace(tmp_path):
    """A mistyped $BASE_WORKSPACE (or --root) used to resolve to a root with no packages, and every walker went green over
    nothing. It raises now, and each walker says so and fails."""
    import pkgdirs
    gone = tmp_path / "no-such-ws"
    with pytest.raises(pkgdirs.NoSuchRoot, match="consumer root .*no-such-ws does not exist"):
        pkgdirs.consumer_root(BASE, None, {"BASE_WORKSPACE": str(gone)})
    with pytest.raises(pkgdirs.NoSuchRoot):
        pkgdirs.consumer_root(BASE, str(gone), {})
    r = subprocess.run([sys.executable, str(PKGDIRS), "--root"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(gone)))
    assert r.returncode == 1 and "does not exist" in r.stderr, r.stdout + r.stderr
    if not (BASE / "_build").exists():
        return
    r = subprocess.run([sys.executable, str(BASE / "_build" / "package.py"), "--all", "--check"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(gone)))
    assert r.returncode == 1 and "does not exist" in r.stderr, r.stdout + r.stderr
    r = subprocess.run(["bash", str(BASE / "_build" / "verify.sh")], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(gone)))
    assert r.returncode == 1 and "does not exist" in r.stderr and "NOT green" in r.stdout and "══" not in r.stdout, r.stdout + r.stderr
    r = subprocess.run(["bash", str(BASE / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(gone)))
    assert r.returncode == 1 and "does not exist" in r.stderr, r.stdout + r.stderr


def test_unit_the_callers_base_is_never_a_package(tmp_path):
    """The exclusion is the base the CALLER runs from (podctl's base_dir, basetest's BASE_LIB), not only this file's."""
    import pkgdirs
    root, want = _tree(tmp_path)
    other = root / "shared-base"; other.mkdir(); (other / "shared-base-script.sh").write_text("# a base under another name\n")
    assert pkgdirs.package_dirs(root) == sorted(want + [other])
    assert pkgdirs.package_dirs(root, base_dir=other) == sorted(want)


_RAISES = "raise SystemExit('pkgdirs exploded')\n"


def test_unit_verify_sh_fails_closed_when_pkgdirs_fails(tmp_path):
    """A pkgdirs.py that crashes (or is missing) is a red gate that says why, never "verifying the base alone"."""
    _repo_only("_build/verify.sh")
    b = tmp_path / "b"; (b / "_build").mkdir(parents=True); (b / "py").mkdir()
    (b / "_build" / "verify.sh").write_text((BASE / "_build" / "verify.sh").read_text())
    (b / "py" / "pkgdirs.py").write_text(_RAISES)
    r = subprocess.run(["bash", str(b / "_build" / "verify.sh")], capture_output=True, text=True, env=_env())
    assert r.returncode == 1 and "pkgdirs exploded" in r.stderr and "could not resolve" in r.stdout, r.stdout + r.stderr
    assert "verifying the base alone" not in r.stdout and "══" not in r.stdout
    (b / "py" / "pkgdirs.py").unlink()
    r = subprocess.run(["bash", str(b / "_build" / "verify.sh")], capture_output=True, text=True, env=_env())
    assert r.returncode == 1 and "verifying the base alone" not in r.stdout, r.stdout + r.stderr


def test_unit_testbed_sh_dies_when_pkgdirs_fails(tmp_path):
    """testbed.sh beside a base whose pkgdirs.py crashes, or a base without one, stops with the reason."""
    _repo_only("the repo's testbed.sh")
    b = tmp_path / "b"; (b / "py").mkdir(parents=True)
    (b / "base.sh").write_text("# base\n"); (b / "testbed.sh").write_text((BASE / "testbed.sh").read_text())
    (b / "py" / "pkgdirs.py").write_text(_RAISES)
    r = subprocess.run(["bash", str(b / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env())
    assert r.returncode == 1 and "pkgdirs exploded" in r.stderr and "could not resolve" in r.stdout, r.stdout + r.stderr
    (b / "py" / "pkgdirs.py").unlink()
    r = subprocess.run(["bash", str(b / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env())
    assert r.returncode == 1 and "predates 3.6.0" in r.stdout, r.stdout + r.stderr


def test_unit_verify_prints_a_failing_tests_whole_error_block(tmp_path):
    """3.15.3: verify.sh's failure report keeps each failing test's whole E block (it used to keep 12-24 lines, and a long
    assertion lost its cause in CI), capped, then pytest's short summary."""
    _repo_only("_build/verify.sh")
    src = (BASE / "_build" / "verify.sh").read_text(encoding="utf-8")
    fn = src[src.index("_verify_failures(){"):]
    fn = fn[:fn.index("\n}\n") + 3]
    e_lines = "\n".join("E       line %d of a long assertion" % i for i in range(40))
    log = ("x\n=================== FAILURES ===================\n____ test_long ____\n>       assert a == b\n%s\n"
           "=========== short test summary info ===========\nFAILED suite/x.py::test_long - assert\n" % e_lines)
    f = tmp_path / "suite.log"; f.write_text(log)
    r = subprocess.run(["bash", "-c", fn + '\n_verify_failures "$1"', "_", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "line 39 of a long assertion" in r.stdout and ">       assert a == b" in r.stdout, r.stdout
    assert "FAILED suite/x.py::test_long" in r.stdout, r.stdout
    assert "head -16" not in src and "head -24" not in src, "the old 16/24-line caps are back"
