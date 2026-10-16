"""unit tier: the install does each piece of work once, and says how long each stage took.

The minutes of a warm install were never spent reinstalling: they were spent doing the same checks twice (a
full --check pass before the real run, a base step one before a package run that repeats it) and nobody could
see which stage cost what, because no stage was ever timed. These tests hold the base to one pass and a clock."""
import os, pathlib, subprocess, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]


def _bash(snippet, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e)


# ---------------------------------------------------------------- no duplicate suite

def test_unit_a_dry_run_never_runs_the_suite():
    # a --check ran the whole suite against the OLD server and the old copy of the package: work the real run
    # repeats minutes later against the right one
    r = _bash('BASE_DRY=1; BASE_INNER=; BASE_NO_SUITE=0; _base_run_suite >/dev/null; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"')
    assert "RESULT=skipped (--check) RC=0" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- stage timings

def test_unit_every_stage_header_closes_the_previous_stage_with_its_seconds():
    # SECONDS is bash's own clock: setting it moves the clock without a sleep, so the test is exact and instant
    r = _bash('{ SECONDS=0; hdr "NODE PACKS · locate"; SECONDS=12; hdr "VENV · /x/.venv"; SECONDS=15; hdr "SUMMARY · x"; } >/dev/null;'
              ' printf "%s\\n" "${BASE_STAGE_TIMES[@]}"')
    assert r.returncode == 0, r.stderr
    rows = r.stdout.strip().splitlines()
    assert rows == ["12|NODE PACKS · locate", "3|VENV · /x/.venv"], rows


def test_unit_the_timing_block_lists_the_slowest_stages_first_and_totals_them():
    r = _bash('BASE_STAGE_TIMES=("4|TOKENS" "61|IMPORT CHECK · main.py" "0|HYGIENE" "95|NODE PACKS · requirements");'
              ' _base_timings_block')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].split() == ["time", "160", "s", "in", "4", "stages,", "slowest", "first:"], lines[0]
    assert [l.split()[0] for l in lines[1:]] == ["95", "61", "4"], lines   # a 0 s stage is not worth a line
    assert "NODE PACKS · requirements" in lines[1]


def test_unit_the_timings_are_kept_on_the_store_after_a_real_run(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    r = _bash(f'BASE_STATE="{state}"; PKG_ID=krea; BASE_STAGE_TIMES=("7|TOKENS" "30|VENV"); _base_timings_write; cat "$BASE_STATE"/logs/krea_*.timings.tsv')
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["7\tTOKENS", "30\tVENV"], r.stdout
    r = _bash(f'BASE_STATE="{state}"; BASE_DRY=1; PKG_ID=dry; BASE_STAGE_TIMES=("7|TOKENS"); _base_timings_write; ls "$BASE_STATE/logs"')
    assert "dry_" not in r.stdout      # a dry run changes nothing on the store, its clock included


# ---------------------------------------------------------------- own packs: the package's own node packs, mirrored by the base

def _own_tree(tmp_path, files=None, version="1.0.0", name="ComfyUI-My-Own"):
    """A package folder beside its script with one own pack, and an empty custom_nodes and state."""
    pkg = tmp_path / "pkg"; pack = pkg / name; (pack / "mine").mkdir(parents=True, exist_ok=True)
    for f in list(pack.rglob("*")):
        if f.is_file(): f.unlink()
    (pack / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "%s"\n' % version, encoding="utf-8")
    for rel, text in (files or {"__init__.py": "NODE_CLASS_MAPPINGS = {}\n", "mine/nodes.py": "A = 1\n"}).items():
        (pack / rel).parent.mkdir(parents=True, exist_ok=True); (pack / rel).write_text(text, encoding="utf-8")
    cn = tmp_path / "ComfyUI" / "custom_nodes"; cn.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    return pkg, cn, state


def _own(tmp_path, snippet, pkg_id="a", packs="ComfyUI-My-Own"):
    return _bash(f'PKG_ID={pkg_id}; PKG_DIR="{tmp_path}/pkg"; CN="{tmp_path}/ComfyUI/custom_nodes"; BASE_STATE="{tmp_path}/state";'
                 f' SYS_PY=python3; VENDORED_PACKS=({packs}); {snippet}')


def test_unit_own_packs_are_mirrored_once_and_an_unchanged_pack_is_left_alone(tmp_path):
    pkg, cn, state = _own_tree(tmp_path)
    r = _own(tmp_path, 'base_own_packs >/dev/null; echo "WHY=${PKG_RESTART_WHY[*]}"; echo "CHANGED=${OWN_CHANGED[*]}"')
    assert r.returncode == 0, r.stderr
    assert (cn / "ComfyUI-My-Own" / "mine" / "nodes.py").read_text() == "A = 1\n"
    assert "CHANGED=ComfyUI-My-Own" in r.stdout and "own pack ComfyUI-My-Own" in r.stdout
    stamp = (cn / "ComfyUI-My-Own" / ".comfy-base-own").read_text()
    assert "pkg=a" in stamp and "version=1.0.0" in stamp and "digest=" in stamp
    assert not list(cn.glob(".*")) and not (cn / "ComfyUI-My-Own.new").exists()      # nothing staged inside custom_nodes
    r = _own(tmp_path, 'base_own_packs; echo "WHY=${PKG_RESTART_WHY[*]}"; echo "CHANGED=${OWN_CHANGED[*]}"')
    assert "CHANGED=" + "\n" in r.stdout + "\n" and "WHY=\n" in r.stdout and "unchanged" in r.stdout, r.stdout


def test_unit_own_packs_mirror_drops_a_file_the_pack_no_longer_ships(tmp_path):
    _own_tree(tmp_path, {"__init__.py": "x = 1\n", "mine/nodes.py": "A = 1\n", "mine/old.py": "gone\n"})
    _own(tmp_path, 'base_own_packs >/dev/null')
    pkg, cn, _ = _own_tree(tmp_path, {"__init__.py": "x = 2\n", "mine/nodes.py": "A = 2\n"}, version="1.0.1")
    (cn / "ComfyUI-My-Own" / "mine" / "__pycache__").mkdir(); (cn / "ComfyUI-My-Own" / "mine" / "__pycache__" / "nodes.pyc").write_bytes(b"x")
    r = _own(tmp_path, 'base_own_packs')
    assert r.returncode == 0, r.stderr
    assert not (cn / "ComfyUI-My-Own" / "mine" / "old.py").exists() and (cn / "ComfyUI-My-Own" / "mine" / "nodes.py").read_text() == "A = 2\n"
    assert (tmp_path / "state" / "own" / "prev" / "a" / "ComfyUI-My-Own" / "mine" / "old.py").exists()   # kept until the import check passes


def test_unit_own_pack_newest_version_wins_on_a_shared_store(tmp_path):
    _own_tree(tmp_path, version="2.5.1"); _own(tmp_path, 'base_own_packs >/dev/null', pkg_id="image")
    pkg, cn, _ = _own_tree(tmp_path, {"__init__.py": "older = 1\n"}, version="2.5.0")
    r = _own(tmp_path, 'base_own_packs; echo "WARN=${BASE_WARN[*]}"', pkg_id="character")
    assert r.returncode == 0, r.stderr
    assert "older" not in (cn / "ComfyUI-My-Own" / "__init__.py").read_text()                 # never backwards
    assert "pkg=image" in (cn / "ComfyUI-My-Own" / ".comfy-base-own").read_text()
    assert "2.5.1" in r.stdout and "character" in r.stdout and "needs a release" in r.stdout
    # the same version with different content: the installing package's copy goes in, and both are named
    _own_tree(tmp_path, {"__init__.py": "same_version_other_bytes = 1\n"}, version="2.5.1")
    r = _own(tmp_path, 'base_own_packs; echo "WARN=${BASE_WARN[*]}"', pkg_id="character")
    assert "same_version_other_bytes" in (cn / "ComfyUI-My-Own" / "__init__.py").read_text()
    assert "image" in r.stdout and "character" in r.stdout and "version bump" in r.stdout


def test_unit_an_own_pack_that_fails_to_import_is_put_back_and_blocks_the_restart(tmp_path):
    _own_tree(tmp_path, {"__init__.py": "good = 1\n"}); _own(tmp_path, 'base_own_packs >/dev/null')
    pkg, cn, _ = _own_tree(tmp_path, {"__init__.py": "raise ImportError('broken')\n"}, version="1.0.1")
    log = tmp_path / "import_check.log"
    log.write_text("Cannot import %s module for custom nodes: broken\n   0.1 seconds (IMPORT FAILED): %s\n" % (cn / "ComfyUI-My-Own", cn / "ComfyUI-My-Own"))
    r = _own(tmp_path, f'base_own_packs >/dev/null; BASE_UPSTREAM=("pack ComfyUI-My-Own does not import: needs upgrading upstream");'
                       f' _base_own_packs_judge "{log}"; echo "BLOCK=$BASE_BLOCK_RESTART"; echo "FAILED=${{BASE_FAILED[*]}}"; echo "UP=${{#BASE_UPSTREAM[@]}}"')
    assert r.returncode == 0, r.stderr
    assert (cn / "ComfyUI-My-Own" / "__init__.py").read_text() == "good = 1\n"                  # the previous folder is back
    assert "BLOCK=1" in r.stdout and "ComfyUI-My-Own" in r.stdout.split("FAILED=")[1] and "UP=0" in r.stdout
    # green: the previous copy is dropped
    _own_tree(tmp_path, {"__init__.py": "fixed = 1\n"}, version="1.0.2"); log.write_text("Import times for custom nodes:\n")
    r = _own(tmp_path, f'base_own_packs >/dev/null; _base_own_packs_judge "{log}"; echo "BLOCK=$BASE_BLOCK_RESTART"')
    assert "BLOCK=0" in r.stdout and not (tmp_path / "state" / "own" / "prev" / "a" / "ComfyUI-My-Own").exists()


def test_unit_own_packs_are_declared_and_recorded(tmp_path):
    _own_tree(tmp_path)
    r = _own(tmp_path, '_base_own_rows')
    assert r.returncode == 0, r.stderr
    d, v, dig = r.stdout.strip().split("|")
    assert d == "ComfyUI-My-Own" and v == "1.0.0" and len(dig) == 64
    r = _own(tmp_path, '_base_own_rows', packs='"bad name"')
    assert r.returncode != 0 and "VENDORED_PACKS" in r.stderr
    r = _own(tmp_path, '_base_own_rows', packs="ComfyUI-Not-There")
    assert r.returncode != 0 and "ComfyUI-Not-There" in r.stderr


def test_unit_the_ledger_records_each_own_pack(tmp_path):
    _own_tree(tmp_path)
    r = _own(tmp_path, 'PKG_NAME=A; PKG_VERSION=1; base_own_packs >/dev/null; base_ledger_write >/dev/null; cat "$BASE_STATE/packages/a.manifest"')
    assert r.returncode == 0, r.stderr
    own = [l for l in r.stdout.splitlines() if l.startswith("own\t")]
    assert len(own) == 1 and own[0].split("\t")[1:3] == ["ComfyUI-My-Own", "1.0.0"] and len(own[0].split("\t")[3]) == 64
    import json
    j = subprocess.run(["python3", str(BASE / "py" / "ledger.py"), str(tmp_path / "state"), "json"], capture_output=True, text=True)
    assert json.loads(j.stdout)[0]["own"] == [{"dir": "ComfyUI-My-Own", "version": "1.0.0", "digest": own[0].split("\t")[3]}]


# ---------------------------------------------------------------- the import check loads only what changed

def _scope_env(tmp_path, freeze_now="a==1\n", snap="a==1\n"):
    s = tmp_path / "snap.txt"; s.write_text(snap, encoding="utf-8")
    n = tmp_path / "now.txt"; n.write_text(freeze_now, encoding="utf-8")
    return (f'_base_uv(){{ cat "{n}"; }}; BASE_VENV_SNAPSHOT="{s}"; BASE_VENV_RESULT=reused; COMFY_OLD=0.37.2; COMFY_NEW=0.37.2;'
            f' PACK_UPDATED=(); PACK_CLONED=(); OWN_CHANGED=(ComfyUI-My-Own ComfyUI-Other); PY=python3;')


def test_unit_the_import_check_is_scoped_to_own_packs_only_when_nothing_else_changed(tmp_path):
    r = _bash(_scope_env(tmp_path) + ' _base_import_scope')
    assert r.stdout.split() == ["ComfyUI-My-Own", "ComfyUI-Other"], r.stdout + r.stderr
    for change in ('COMFY_NEW=0.38.0;', 'PACK_UPDATED=(rgthree-comfy);', 'PACK_CLONED=(x);', 'BASE_VENV_RESULT=rebuilt;',
                   'OWN_CHANGED=();', 'BASE_VENV_SNAPSHOT="";'):
        r = _bash(_scope_env(tmp_path) + change + ' _base_import_scope')
        assert r.stdout.strip() == "", change
    r = _bash(_scope_env(tmp_path, freeze_now="a==2\n") + ' _base_import_scope')     # a Python package moved: the full check
    assert r.stdout.strip() == ""


def _fake_comfy(tmp_path, fail_scoped=False):
    comfy = tmp_path / "ComfyUI"; comfy.mkdir(exist_ok=True)
    (comfy / "main.py").write_text(
        "import sys\nprint('ARGS', ' '.join(sys.argv[1:]))\n"
        + ("if '--disable-all-custom-nodes' in sys.argv: print('0.0 seconds (IMPORT FAILED): /x/custom_nodes/ComfyUI-My-Own')\n" if fail_scoped else ""),
        encoding="utf-8")
    return comfy


def test_unit_a_scoped_import_check_loads_only_the_named_packs(tmp_path):
    comfy = _fake_comfy(tmp_path); log = tmp_path / "check.log"
    r = _bash(_scope_env(tmp_path) + f' COMFY="{comfy}"; _base_quick_test_scoped "{log}"; echo rc=$?')
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    assert "--disable-all-custom-nodes --whitelist-custom-nodes ComfyUI-My-Own ComfyUI-Other" in log.read_text()


def test_unit_a_failed_scoped_check_is_judged_again_by_the_full_one(tmp_path):
    comfy = _fake_comfy(tmp_path, fail_scoped=True); log = tmp_path / "check.log"
    r = _bash(_scope_env(tmp_path) + f' COMFY="{comfy}"; _base_quick_test_scoped "{log}"; echo rc=$?')
    text = log.read_text()
    assert "rc=0" in r.stdout and "--disable-all-custom-nodes" not in text and "IMPORT FAILED" not in text, text   # the full check's verdict stands


def test_unit_files_a_hook_adds_to_an_own_pack_survive_a_run_where_the_pack_did_not_change(tmp_path):
    # Qwen 2.1 fetches its rewriter prompts INTO its own pack in pkg_post_models (they cannot ship in the zip). The
    # "unchanged" verdict compares the zip's copy with the STAMP, never with the live folder, so those files stay.
    _own_tree(tmp_path); _own(tmp_path, 'base_own_packs >/dev/null')
    fetched = tmp_path / "ComfyUI" / "custom_nodes" / "ComfyUI-My-Own" / "prompts" / "system.txt"
    fetched.parent.mkdir(); fetched.write_text("fetched by a hook\n", encoding="utf-8")
    r = _own(tmp_path, '_base_own_packs_save; base_own_packs')
    assert "unchanged" in r.stdout and fetched.exists(), r.stdout


def test_unit_own_packs_are_mirrored_before_the_hooks_that_may_add_to_them():
    # the order that keeps a hook's additions: mirror (which empties a changed pack) first, then pkg_pre_models and
    # pkg_post_models, which may write into the pack again. Both branches of base_install (runtime and normal).
    body = (BASE / "lib" / "95-summary.sh").read_text(encoding="utf-8")
    run = body[body.index("base_install(){"):body.index("_base_use_testbed(){")]
    mirrors = [i for i in range(len(run)) if run.startswith("base_own_packs ||", i)]
    assert len(mirrors) == 2, mirrors
    assert max(mirrors) < run.index("_base_hook pkg_pre_models") < run.index("_base_hook pkg_post_models")
