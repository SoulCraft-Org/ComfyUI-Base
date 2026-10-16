"""unit tier, 3.1.0: the staged install (MODELS_LATER, stand-ins, fetch-later, progress.json) and BASE_FETCH_JOBS.

A buyer's machine opens the workflow on its first flow's files and fetches the rest behind the started server. The
promise every test here guards: a stand-in (a 0-byte file at a declared dest, listed in state/standins.list) is never
counted, moved, renamed, pruned or reported as a model, and a run that is NOT staged behaves exactly as 3.0 did."""
import json, os, pathlib, subprocess, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]

ROW_FIRST = "diffusion_models|Example|Turbo|first.safetensors|https://huggingface.co/x/y/resolve/main/first.safetensors|4096|first flow|"
ROW_LATER = "diffusion_models|Example|Raw|later.safetensors|https://huggingface.co/x/y/resolve/main/later.safetensors|8192|another flow|"
ROW_LATER2 = "vae|Example|Real|later2.safetensors|https://huggingface.co/x/y/resolve/main/later2.safetensors|2048|another flow|"
ROW_LOCAL = "loras|SDXL|Detailers|mine.safetensors|LOCAL|0|the buyer's own|"
ROW_SNAP = "RMBG|||RMBG-2.0/|hf://o/RMBG-2.0|100|snapshot|"


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("BASE_")}
    e.update(env or {})
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s' % (BASE, snippet)], capture_output=True, text=True, env=e)


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text(""); return c


def _fake(tmp_path, **extra):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    env.update(extra)
    return env


def _models(*rows):
    return "MODELS=(%s)" % " ".join('"%s"' % r for r in rows)


# ---------------------------------------------------------------- the declaration

def test_unit_models_later_must_name_a_downloadable_file_row():
    ok = _bash('%s; MODELS_LATER=(later.safetensors); _base_later_rows; echo rc=$?' % _models(ROW_FIRST, ROW_LATER))
    assert "later.safetensors" in ok.stdout and "rc=0" in ok.stdout, ok.stderr
    r = _bash('%s; MODELS_LATER=(nope.safetensors); _base_later_rows; echo rc=$?' % _models(ROW_FIRST))
    assert "rc=1" in r.stdout and "nope.safetensors" in r.stderr
    r = _bash('%s; MODELS_LATER=(mine.safetensors); _base_later_rows; echo rc=$?' % _models(ROW_FIRST, ROW_LOCAL))
    assert "rc=1" in r.stdout and "LOCAL" in r.stderr
    r = _bash('%s; MODELS_LATER=(RMBG-2.0/); _base_later_rows; echo rc=$?' % _models(ROW_FIRST, ROW_SNAP))
    assert "rc=1" in r.stdout and "snapshot" in r.stderr


def test_unit_the_declare_dump_carries_models_later(tmp_path):
    """Suites read a package through the base: load_package().models_later is what the script declared."""
    import shutil
    from basetest import load_package
    stub = BASE / "_build" / "stub"
    if not stub.exists():
        pytest.skip("_build/stub is repo tooling")
    pkg = tmp_path / "pkg"; shutil.copytree(stub, pkg)
    sc = pkg / "stub-script.sh"; s = sc.read_text()
    s = s.replace("SUPERSEDED=(", "MODELS_LATER=( stub_vae.safetensors )\nSUPERSEDED=(", 1)
    sc.write_text(s)
    assert load_package(pkg).models_later == ["stub_vae.safetensors"]


# ---------------------------------------------------------------- stand-ins

def test_unit_a_staged_run_stands_in_for_later_rows_only_and_queues_them(tmp_path):
    c = _pod(tmp_path)
    r = _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models; '
              'echo DL=${#MODEL_DL[@]} OK=${#MODEL_OK[@]} STANDIN=${#MODEL_STANDIN[@]} FAIL=${#MODEL_FAIL[@]}' % _models(ROW_FIRST, ROW_LATER),
              env=_fake(tmp_path, BASE_STAGED="1"))
    assert "DL=1 OK=0 STANDIN=1 FAIL=0" in r.stdout, r.stdout + r.stderr
    first = c / "models/diffusion_models/Example/Turbo/first.safetensors"
    later = c / "models/diffusion_models/Example/Raw/later.safetensors"
    assert first.stat().st_size == 4096 and later.stat().st_size == 0
    state = tmp_path / "comfy-base" / "state"
    assert (state / "standins.list").read_text().split() == [str(later)]
    q = (state / "later.queue").read_text().splitlines()
    assert len(q) == 1 and q[0].endswith("|" + str(later)) and "|8192|" in q[0]
    doc = json.loads((state / "progress.json").read_text())
    assert doc["files"][0]["state"] == "standin" and doc["later"] == {"files": 1, "bytes": 8192, "done_files": 0, "done_bytes": 0}
    assert "later 1 (stand-ins until they arrive)" in r.stdout


def test_unit_a_second_staged_run_renames_nothing_and_counts_no_stand_in(tmp_path):
    _pod(tmp_path)
    env = _fake(tmp_path, BASE_STAGED="1")
    snippet = '%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models; echo OK=${#MODEL_OK[@]} PARTIAL=${#MODEL_PARTIAL[@]} DL=${#MODEL_DL[@]}' % _models(ROW_FIRST, ROW_LATER)
    _bash(snippet, env=env)
    r = _bash(snippet, env=env)
    assert "OK=1 PARTIAL=0 DL=0" in r.stdout, r.stdout + r.stderr
    assert not list(tmp_path.rglob("*.partial"))
    assert len((tmp_path / "comfy-base/state/later.queue").read_text().splitlines()) == 1        # merged, not appended twice


def test_unit_a_run_that_is_not_staged_treats_a_stand_in_as_absent_and_downloads_it(tmp_path):
    c = _pod(tmp_path)
    _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models' % _models(ROW_FIRST, ROW_LATER), env=_fake(tmp_path, BASE_STAGED="1"))
    r = _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]} PARTIAL=${#MODEL_PARTIAL[@]}' % _models(ROW_FIRST, ROW_LATER),
              env=_fake(tmp_path))
    assert "DL=1 PARTIAL=0" in r.stdout, r.stdout + r.stderr
    assert (c / "models/diffusion_models/Example/Raw/later.safetensors").stat().st_size == 8192
    assert (tmp_path / "comfy-base/state/standins.list").read_text().strip() == ""


def test_unit_prune_is_a_note_in_a_staged_run_and_never_trips_on_a_stand_in(tmp_path):
    _pod(tmp_path)
    r = _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo FAILED=${#BASE_FAILED[@]}' % _models(ROW_FIRST, ROW_LATER),
              env=_fake(tmp_path, BASE_STAGED="1", BASE_YES="1"))
    assert "prune: skipped, a staged install" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr


def test_unit_the_prune_commit_skips_a_registered_stand_in(tmp_path):
    """A stand-in at a declared dest is a promise, not a model: the re-check after a staged deletion never calls it
    'changed size' (which would roll the deletion back and fail the run)."""
    body = (BASE / "lib" / "50-models.sh").read_text()
    i = body.index("_base_prune_commit(){"); j = body.index("base_prune(){")
    assert '_base_standin_is "$dest" && continue' in body[i:j]


# ---------------------------------------------------------------- the later stage

def test_unit_fetch_later_replaces_every_stand_in_and_empties_the_list(tmp_path):
    c = _pod(tmp_path)
    env = _fake(tmp_path, BASE_STAGED="1", BASE_FAKE_FREE_GB="500")
    _bash('%s; MODELS_LATER=(later.safetensors later2.safetensors); base_env_setup; base_discover quiet; base_models' % _models(ROW_FIRST, ROW_LATER, ROW_LATER2), env=env)
    r = _bash('base_fetch_later; echo rc=$?', env=env)
    assert "rc=0" in r.stdout and "every later file is in place" in r.stdout, r.stdout + r.stderr
    assert (c / "models/diffusion_models/Example/Raw/later.safetensors").stat().st_size == 8192
    assert (c / "models/vae/Example/Real/later2.safetensors").stat().st_size == 2048
    state = tmp_path / "comfy-base/state"
    assert (state / "standins.list").read_text().strip() == ""
    doc = json.loads((state / "progress.json").read_text())
    assert doc["stage"] == "done" and all(f["state"] == "present" for f in doc["files"])


def test_unit_fetch_later_refuses_while_another_live_one_runs(tmp_path):
    _pod(tmp_path)
    state = tmp_path / "comfy-base/state"; state.mkdir(parents=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (state / "fetch-later.pid").write_text(str(sleeper.pid))
        r = _bash('base_fetch_later; echo rc=$?', env=_fake(tmp_path))
        assert "already running as pid %d" % sleeper.pid in r.stdout and "rc=0" in r.stdout
    finally:
        sleeper.kill(); sleeper.wait()


def test_unit_fetch_later_does_nothing_on_a_shared_store(tmp_path):
    _pod(tmp_path)
    r = _bash('base_fetch_later; echo rc=$?', env=_fake(tmp_path, BASE_VOLUME_SHARED="1"))
    assert "operator's shape" in r.stdout and "rc=0" in r.stdout


def test_unit_a_failed_later_file_is_named_and_left_as_a_stand_in(tmp_path):
    c = _pod(tmp_path)
    _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; base_models' % _models(ROW_FIRST, ROW_LATER), env=_fake(tmp_path, BASE_STAGED="1"))
    r = _bash('base_fetch_later; echo rc=$?', env=_fake(tmp_path, BASE_FAKE_DL="fail", BASE_FAKE_FREE_GB="500"))
    assert "rc=1" in r.stdout and "not obtained" in r.stdout, r.stdout + r.stderr
    assert (c / "models/diffusion_models/Example/Raw/later.safetensors").stat().st_size == 0
    doc = json.loads((tmp_path / "comfy-base/state/progress.json").read_text())
    assert doc["files"][0]["state"] == "failed"


def test_unit_the_launch_is_detached_and_the_run_goes_on(tmp_path):
    """base_install hands the queue to a detached `base.sh fetch-later` after the server started, never waiting for it."""
    body = (BASE / "lib" / "95-summary.sh").read_text()
    run = body[body.index("base_install(){"):body.index("_base_use_testbed(){")]
    assert run.index("base_combos") < run.index("_base_later_launch") < run.index("_base_run_suite")   # 3.2.0: the suite stage (never in a dry run)
    launch = (BASE / "lib" / "50-models.sh").read_text()
    launch = launch[launch.index("_base_later_launch(){"):launch.index("base_fetch_later(){")]
    assert 'nohup setsid bash "$BASE_DIR/base.sh" fetch-later </dev/null' in launch and launch.rstrip().endswith("}")


# ---------------------------------------------------------------- BASE_FETCH_JOBS

def _stub_hf(tmp_path, delay="1"):
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True); log = tmp_path / "times.log"
    (bin_ / "hf").write_text(
        '#!/bin/bash\nf="$3"; d=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
        'echo "start $f $(python3 -c "import time; print(time.time())")" >> "%s"; sleep %s\n'
        'mkdir -p "$d/$(dirname "$f")"; python3 - "$d/$f" <<"PY"\nimport sys\nsz={"a.safetensors":4096,"b.safetensors":4096,"c.safetensors":4096}\nopen(sys.argv[1],"wb").truncate(sz.get(sys.argv[1].rsplit("/",1)[-1],4096))\nPY\n'
        'echo "end $f $(python3 -c "import time; print(time.time())")" >> "%s"\n' % (log, delay, log))
    (bin_ / "hf").chmod(0o755)
    return bin_, log


def _abc_rows():
    return [("vae|Example|Real|%s.safetensors|https://huggingface.co/x/y/resolve/main/%s.safetensors|4096|n|" % (n, n)) for n in "abc"]


def _spans(log):
    starts, ends = {}, {}
    for ln in log.read_text().splitlines():
        kind, f, t = ln.split()
        (starts if kind == "start" else ends)[f] = float(t)
    return starts, ends


def test_unit_three_jobs_download_at_once_and_report_in_queue_order(tmp_path):
    _pod(tmp_path); bin_, log = _stub_hf(tmp_path)
    r = _bash('%s; base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % _models(*_abc_rows()),
              env=_fake(tmp_path, BASE_FAKE_DL="real", BASE_FETCH_JOBS="3", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    assert "DL=3" in r.stdout, r.stdout + r.stderr
    starts, ends = _spans(log)
    assert max(starts.values()) < min(ends.values())                    # all three were in flight together
    out = r.stdout
    assert out.index("a.safetensors downloaded") < out.index("b.safetensors downloaded") < out.index("c.safetensors downloaded")


def test_unit_one_job_is_the_serial_behaviour(tmp_path):
    _pod(tmp_path); bin_, log = _stub_hf(tmp_path, delay="0.3")
    r = _bash('%s; base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % _models(*_abc_rows()),
              env=_fake(tmp_path, BASE_FAKE_DL="real", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    assert "DL=3" in r.stdout, r.stdout + r.stderr
    starts, ends = _spans(log)
    order = sorted(starts, key=starts.get)
    for x, y in zip(order, order[1:]):
        assert ends[x] <= starts[y]                                     # one after another


def test_unit_a_bad_job_count_falls_back_to_one_loudly():
    r = _bash('BASE_FETCH_JOBS=lots _base_fetch_jobs')
    assert r.stdout.strip() == "1" and "not a positive integer" in r.stderr


# ---------------------------------------------------------------- progress.json

def test_unit_progress_reads_every_state_from_the_disk(tmp_path):
    sys_py = "python3"
    q, lst, failed, out = (tmp_path / n for n in ("q", "standins", "failed", "progress.json"))
    present, standin, arriving, broken = (tmp_path / ("%s.safetensors" % n) for n in ("present", "standin", "arriving", "broken"))
    present.write_bytes(b"x" * 10); standin.write_bytes(b""); arriving.write_bytes(b"x" * 3); broken.write_bytes(b"")
    q.write_text("".join("FILE|m/%s|u|10|%s\n" % (p.name, p) for p in (present, standin, arriving, broken)))
    lst.write_text("%s\n%s\n" % (standin, broken)); failed.write_text("FILE|m/broken|u|10|%s\n" % broken)
    subprocess.run([sys_py, str(BASE / "py" / "stage_progress.py"), "--queue", str(q), "--standins", str(lst), "--failed", str(failed),
                    "--out", str(out), "--stage", "later"], check=True)
    doc = json.loads(out.read_text())
    got = {f["file"]: f["state"] for f in doc["files"]}
    assert got == {"present.safetensors": "present", "standin.safetensors": "standin", "arriving.safetensors": "downloading", "broken.safetensors": "failed"}
    assert doc["later"]["done_files"] == 1 and doc["stage"] == "later" and doc["format"] == 1
    assert not list(tmp_path.glob("progress.json.tmp*"))


def test_unit_comfyui_is_told_where_the_base_keeps_its_state():
    """A package's route reads $COMFY_BASE_STATE/progress.json; the one launch function (install restart and boot
    alike) exports it, from BASE_STATE in an install and from BASE_HOME in the boot."""
    body = (BASE / "lib" / "85-launch.sh").read_text()
    start = body[body.index("_base_start_comfy(){"):]
    start = start[:start.index("\n}\n")]
    assert 'export COMFY_BASE_STATE="$_cbs"' in start and '${BASE_STATE:-${BASE_HOME:+$BASE_HOME/state}}' in start
    assert start.index("COMFY_BASE_STATE") < start.index('nohup "$PY" main.py')
