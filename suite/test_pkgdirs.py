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
    """The caller's environment without any BASE_* (a session that exports BASE_WORKSPACE must not steer these tests) and
    without VERIFY_* (3.17.1: the CI job runs the gate with VERIFY_SUITE=zip, and a verify.sh these tests start must not
    inherit it); a test that wants one sets it."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "VERIFY_")) and k != "ROOT"}
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
    if not (BASE / "_build").exists():   # a skip, never a return: VERIFY_SUITE=zip re-runs a skipped test from the repo (3.17.1)
        pytest.skip("the walkers' half is repo tooling (_build/)")
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


def test_unit_verify_names_every_failing_test_before_the_capped_sections(tmp_path):
    """3.22.1: a package's CI log named 4 of 7 failures (2026-10-17). The runner's -rs left only skips in the short summary,
    and `| head -200` cut the sections off and broke awk's pipe. Every FAILED and ERROR line now comes first, then the
    sections, capped inside awk, with nothing on stderr."""
    _repo_only("_build/verify.sh")
    src = (BASE / "_build" / "verify.sh").read_text(encoding="utf-8")
    fn = src[src.index("_verify_failures(){"):]
    fn = fn[:fn.index("\n}\n") + 3]
    assert "| head" not in fn, fn
    names = ["suite/x.py::test_%d" % i for i in range(7)]
    blocks = "".join("____ %s ____\n>       assert f()\n%s\n" % (n.split("::")[1], "\n".join("E       line %d" % j for j in range(60)))
                     for n in names)
    summary = "".join("%s %s - assert\n" % ("ERROR" if i == 6 else "FAILED", n) for i, n in enumerate(names))
    log = ("x\n==================== FAILURES ====================\n" + blocks
           + "========== short test summary info ==========\nSKIPPED [1] suite/y.py:3: no GPU\n" + summary
           + "6 failed, 3 passed, 1 skipped, 1 error in 1.00s\n")
    f = tmp_path / "suite.log"; f.write_text(log)
    r = subprocess.run(["bash", "-c", fn + '\n_verify_failures "$1"', "_", str(f)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == "", r.stderr
    out = r.stdout
    for i, n in enumerate(names):
        line = "%s %s - assert" % ("ERROR" if i == 6 else "FAILED", n)
        assert line in out and out.index(line) < out.index("= FAILURES ="), (line, out[:800])
    assert "SKIPPED" not in out and "E       line 0" in out, out[:800]
    assert len(out.splitlines()) == 7 + 200, len(out.splitlines())                     # the 7 names, then 200 section lines


def _verify_beside(tmp_path, start="exit 0", owned='echo "127.0.0.1:$(cat "$TESTBED_STATE/p")"'):
    """verify.sh in a stand-in base: no consumer root, a packager whose --check is green, a base.sh whose `test` records the
    BASE_SERVER it was handed (from the repo and from the zip), and a testbed.sh that records every call with its
    TESTBED_STATE and TESTBED_PORT; its --start runs `start` and --owned runs `owned`."""
    import zipfile
    b = tmp_path / "b"; (b / "_build").mkdir(parents=True); (b / "py").mkdir()
    (b / "_build" / "verify.sh").write_text((BASE / "_build" / "verify.sh").read_text())
    (b / "py" / "pkgdirs.py").write_text("print('')\n")
    (b / "_build" / "package.py").write_text("print('comfyui-base.zip current')\n")
    log = tmp_path / "calls.log"
    base_sh = ('#!/bin/bash\necho "suite $BASE_SERVER" >> "%s"\n'
               '[ -n "${STUB_SKIP:-}" ] && [ -n "${BASE_SKIPPED_OUT:-}" ] && echo "suite/test_a.py::test_needs_build" > "$BASE_SKIPPED_OUT"\n'
               '[ -n "${BASE_ONLY_IDS:-}" ] && echo "only $(cat "$BASE_ONLY_IDS")" >> "%s"\necho "1 passed"\n') % (log, log)
    (b / "base.sh").write_text(base_sh)
    with zipfile.ZipFile(b / "comfyui-base.zip", "w") as z:
        z.writestr("comfyui-base/base.sh", base_sh.replace('"suite ', '"zip-suite '))
    (b / "testbed.sh").write_text('#!/bin/bash\necho "$1|${TESTBED_STATE:-}|${TESTBED_PORT:-}" >> "%s"\n'
                                  'case "$1" in\n  --start) echo "${TESTBED_PORT:-}" > "$TESTBED_STATE/p"; %s;;\n  --owned) %s;;\nesac\nexit 0\n'
                                  % (log, start, owned))
    run = lambda **kw: subprocess.run(["bash", str(b / "_build" / "verify.sh")], capture_output=True, text=True, env={k: v for k, v in _env(**{"VERIFY_SERVER": "", **kw}).items() if not k.startswith("TESTBED_") or k in kw}, timeout=120)
    calls = lambda: [ln.split("|") for ln in log.read_text().splitlines()] if log.exists() else []
    return run, calls, log


def test_unit_verify_talks_only_to_a_server_it_started_or_proved(tmp_path):
    """3.16.0 (error log row 84): verify.sh used whatever answered on the port in .testbed-server.port, else 8199; another
    session's server answered, the snapshot tier measured its frontend and a server test lost it mid-run. Now, by default,
    each run starts its own on a free port with a folder of its own, both suites (repo and zip) get that address, and it
    is stopped and its folder removed when the run ends; two runs never share one. VERIFY_SERVER=reuse takes the
    checkout's server only when testbed.sh --owned proves it, and refuses a foreign one before any suite runs."""
    _repo_only("_build/verify.sh")
    run, calls, log = _verify_beside(tmp_path)
    folders = []
    for _ in range(2):
        log.unlink(missing_ok=True)
        r = run(); c = calls()
        assert r.returncode == 0 and "this run's own, stopped when it ends" in r.stdout, r.stdout + r.stderr
        start = [x for x in c if x[0] == "--start"]; assert len(start) == 1, c
        state, port = start[0][1], start[0][2]
        assert state and port.isdigit() and port != "8199", c
        addr = "127.0.0.1:%s" % port
        assert ("── server: %s" % addr) in r.stdout and log.read_text().count("suite %s\n" % addr) == 2, r.stdout + log.read_text()
        assert ["--stop", state, ""] in c and c[-1][0] == "--stop" and not pathlib.Path(state).exists(), c
        folders.append(state)
    assert folders[0] != folders[1]
    # reuse: a foreign holder is refused, by name, before anything runs
    run, calls, log = _verify_beside(tmp_path / "r", owned='echo "port 8199 is held by pid 4242, not by this testbed\'s server (pid 77)" >&2; exit 1')
    r = run(VERIFY_SERVER="reuse")
    assert r.returncode == 1 and "refused: port 8199 is held by pid 4242" in r.stdout and "NOT green" in r.stdout, r.stdout + r.stderr
    assert "══" not in r.stdout and [x[0] for x in calls()] == ["--owned"], calls()
    # no venv (exit 3): no server, said out loud as 127.0.0.1:0, and the run folder is gone
    run, calls, log = _verify_beside(tmp_path / "n", start="exit 3")
    r = run()
    assert r.returncode == 0 and "server: none (the testbed has no venv" in r.stdout and log.read_text().count("suite 127.0.0.1:0") == 2, r.stdout + log.read_text()
    assert not pathlib.Path([x for x in calls() if x[0] == "--start"][0][1]).exists()
    # a server that does not come up is a red gate that says so, never a quiet run without one
    run, calls, log = _verify_beside(tmp_path / "f", start='echo "this testbed\'s server did not come up"; exit 1')
    r = run()
    assert r.returncode == 1 and "did not start" in r.stdout and "did not come up" in r.stdout and "══" not in r.stdout, r.stdout + r.stderr
    # the caller's own BASE_SERVER is used as given; testbed.sh is not asked
    run, calls, log = _verify_beside(tmp_path / "c")
    r = run(BASE_SERVER="127.0.0.1:9911")
    assert r.returncode == 0 and "set by the caller" in r.stdout and [x[0] for x in calls()] == ["suite 127.0.0.1:9911", "zip-suite 127.0.0.1:9911"], r.stdout + log.read_text()


def test_unit_verify_suite_zip_runs_the_zip_then_only_what_it_skipped(tmp_path):
    """3.17.1: VERIFY_SUITE=zip (CI's mode) runs the suite from the extracted zip first, which is what a pod runs, then from
    the repo ONLY the tests that run skipped (those need the repo's own files), so every test runs once and none twice;
    the zip check still runs first. With nothing skipped, the repo pass is not run at all. repo runs the tree's alone;
    both (the default) runs the two; anything else is a red gate that says so before any work."""
    _repo_only("_build/verify.sh")
    run, calls, log = _verify_beside(tmp_path, start="exit 3")
    suites = lambda: [x for x in log.read_text().splitlines() if "suite " in x or x.startswith("only ")]
    r = run(VERIFY_SUITE="zip")
    assert r.returncode == 0 and "comfyui-base.zip current" in r.stdout and "the zip run skipped no test" in r.stdout, r.stdout + r.stderr
    assert suites() == ["zip-suite 127.0.0.1:0"], suites()
    log.unlink()
    r = run(VERIFY_SUITE="zip", STUB_SKIP="1")
    assert r.returncode == 0 and "suite the 1 the zip skipped: 1 passed" in r.stdout, r.stdout + r.stderr
    assert suites() == ["zip-suite 127.0.0.1:0", "suite 127.0.0.1:0", "only suite/test_a.py::test_needs_build"], suites()
    log.unlink()
    r = run(VERIFY_SUITE="repo")
    assert r.returncode == 0 and suites() == ["suite 127.0.0.1:0"], r.stdout + log.read_text()
    log.unlink()
    r = run(STUB_SKIP="1")
    assert r.returncode == 0 and suites() == ["suite 127.0.0.1:0", "zip-suite 127.0.0.1:0"], r.stdout     # both: no narrowing
    log.unlink()
    r = run(VERIFY_SUITE="fast")
    assert r.returncode == 1 and "VERIFY_SUITE=fast" in r.stdout and "══" not in r.stdout and not log.exists(), r.stdout


def test_unit_basetest_records_what_a_run_skipped_and_runs_only_that(tmp_path):
    """The plugin half of VERIFY_SUITE=zip, through real pytest: BASE_SKIPPED_OUT gets the node id of every test the run
    skipped (a module skipped whole is recorded by its file), and BASE_ONLY_IDS keeps exactly those, so the repo pass
    runs what the zip pass could not and nothing else."""
    s = tmp_path / "s"; s.mkdir()
    (s / "pytest.ini").write_text("[pytest]\nmarkers =\n    unit: u\n")
    (s / "test_a.py").write_text("import pytest\ndef test_unit_runs(): pass\n"
                                 "def test_unit_needs_build():\n    pytest.skip('no _build')\n")
    (s / "test_b.py").write_text("import pytest\npytest.skip('repo only', allow_module_level=True)\ndef test_unit_x(): pass\n")
    out = tmp_path / "skipped"
    env = {**{k: v for k, v in os.environ.items() if k not in ("BASE_SKIPPED_OUT", "BASE_ONLY_IDS")}, "PYTHONPATH": str(BASE / "py")}
    py = lambda **kw: subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "basetest", "-c", str(s / "pytest.ini"), str(s)],
                                     capture_output=True, text=True, cwd=s, env={**env, **kw}, timeout=120)
    r = py(BASE_SKIPPED_OUT=str(out))
    assert r.returncode == 0 and "1 passed, 2 skipped" in r.stdout, r.stdout + r.stderr
    assert out.read_text().splitlines() == ["test_a.py::test_unit_needs_build", "test_b.py"], out.read_text()
    (s / "test_b.py").write_text("def test_unit_x(): pass\n")          # the repo has what the zip lacked
    (s / "test_a.py").write_text("def test_unit_runs(): pass\ndef test_unit_needs_build(): pass\n")
    r = py(BASE_ONLY_IDS=str(out))
    assert r.returncode == 0 and "2 passed, 1 deselected" in r.stdout, r.stdout + r.stderr


def _vendoring(root, name, version, body="# the pack\n"):
    """A package <name> vendoring Pack-X at <version> (no version: a pack without a pyproject.toml)."""
    d = root / name; d.mkdir(parents=True)
    (d / ("%s-script.sh" % name)).write_text('WF_NAME=""\nPKG_ID="%s"\n' % name)
    pk = d / "Pack-X"; pk.mkdir(); (pk / "__init__.py").write_text(body)
    if version:
        (pk / "pyproject.toml").write_text('[project]\nname = "pack-x"\nversion = "%s"\n' % version)
    return pk


def test_unit_own_packs_newest_keeps_one_copy_per_pack_the_newest(tmp_path):
    """3.20.2: one folder per pack name, the newest version (numeric, so 1.10.0 beats 1.9.0), a copy with no version
    loses to one with a version, the same version keeps the first, and two contents under one version are named."""
    import own_packs
    old = _vendoring(tmp_path, "a", "1.9.0"); new = _vendoring(tmp_path, "b", "1.10.0"); bare = _vendoring(tmp_path, "c", None)
    notes = []
    assert own_packs.newest([str(old), str(bare), str(new)], note=notes.append) == [str(new)]
    assert own_packs.newest([str(bare), str(old)]) == [str(old)] and own_packs.newest([str(old), str(bare)]) == [str(old)]
    assert any("1.10.0 %s kept over 1.9.0 %s" % (new, old) in n for n in notes), notes
    same = _vendoring(tmp_path, "d", "1.9.0"); twin = _vendoring(tmp_path, "e", "1.9.0", body="# other bytes\n")
    notes = []
    assert own_packs.newest([str(old), str(same), str(twin)], note=notes.append) == [str(old)]
    assert notes == ["Pack-X: %s and %s both say version 1.9.0 with different content; one of them missed a version bump"
                     % (old, twin)], notes
    other = tmp_path / "f" / "Pack-Y"; other.mkdir(parents=True)
    assert own_packs.newest([str(old), str(other), str(new)]) == [str(new), str(other)], "first-seen order, per name"


def test_unit_testbed_links_the_newest_copy_when_two_packages_vendor_one_pack(tmp_path):
    """The workspace's integration run linked the Video Replacer's Content Gate 1.5.0 over FLUX 3 Klein's newer copy,
    because the testbed linked each package's pack by folder name and the last package walked won. It links the
    newest version now (what a pod keeps), and --status says which copy and from where."""
    _repo_only("the repo's testbed.sh")
    root = (tmp_path / "ws").resolve()
    _vendoring(root, "a-newer", "1.10.0"); _vendoring(root, "z-older", "1.9.0")    # name order alone picks z-older
    s = subprocess.run(["bash", str(BASE / "testbed.sh"), "--status"], capture_output=True, text=True, env=_env(BASE_WORKSPACE=str(root)))
    assert s.returncode == 0, s.stdout + s.stderr
    assert "packages under %s (2) · 1 vendored" % root in s.stdout, s.stdout + s.stderr
    assert "vendored Pack-X 1.10.0 from a-newer" in s.stdout and "from z-older" not in s.stdout, s.stdout + s.stderr
    assert "1.10.0 %s kept over 1.9.0 %s" % (root / "a-newer" / "Pack-X", root / "z-older" / "Pack-X") in s.stderr, s.stderr
