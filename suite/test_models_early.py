"""unit and install tiers, 3.13.0: the model downloads start beside the venv build (lib/52-models-early.sh).

The promises: the downloads really run while the venv builds; the model stage still judges every row, counts an early
file once (as downloaded), and fetches again whatever the early pass did not get, so a file nobody can fetch fails the
run by name; only what the model stage WOULD download goes early; a run that stops before the join stops its downloads."""
import os, pathlib, re, subprocess, time, pytest
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"

ROW_A = "vae|Example|Real|a.safetensors|https://huggingface.co/x/y/resolve/main/a.safetensors|4096|n|"
ROW_B = "vae|Example|Real|b.safetensors|https://huggingface.co/x/y/resolve/main/b.safetensors|4096|n|"
ROW_LOCAL = "loras|SDXL|Detailers|mine.safetensors|LOCAL|0|the user's own|"
ROW_SNAP = "RMBG|||RMBG-2.0/|hf://o/RMBG-2.0|100|snapshot|"
ROW_LATER = "diffusion_models|Example|Raw|later.safetensors|https://huggingface.co/x/y/resolve/main/later.safetensors|8192|n|"
ROW_ELSEWHERE = "vae|Example|Real|moved.safetensors|https://huggingface.co/x/y/resolve/main/moved.safetensors|2048|n|"


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s' % (BASE, snippet)], capture_output=True, text=True, env=e)


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text(""); return c


def _fake(tmp_path, **extra):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "500"}
    env.update(extra)
    return env


def _models(*rows):
    return "MODELS=(%s)" % " ".join('"%s"' % r for r in rows)


def _stub_hf(tmp_path, delay="1"):
    """An hf that logs when each file starts and ends, sleeps, then writes the file at 4096 bytes."""
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True); log = tmp_path / "times.log"
    (bin_ / "hf").write_text(
        '#!/bin/bash\nf="$3"; d=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
        'echo "start $f $(python3 -c "import time; print(time.time())")" >> "%s"; sleep %s\n'
        'mkdir -p "$d/$(dirname "$f")"; python3 -c "import sys; open(sys.argv[1],\'wb\').truncate(4096)" "$d/$f"\n'
        'echo "end $f $(python3 -c "import time; print(time.time())")" >> "%s"\n' % (log, delay, log))
    (bin_ / "hf").chmod(0o755)
    return bin_, log


# ---------------------------------------------------------------- what goes early

def test_unit_only_what_the_model_stage_would_download_goes_early(tmp_path):
    c = _pod(tmp_path)
    (c / "models/vae/Example/Real").mkdir(parents=True)
    (c / "models/vae/Example/Real/a.safetensors").write_bytes(b"\0" * 4096)                  # in place
    (tmp_path / "elsewhere").mkdir(); (tmp_path / "elsewhere/moved.safetensors").write_bytes(b"\0" * 2048)   # a move away
    r = _bash('%s; MODELS_LATER=(later.safetensors); base_env_setup; base_discover quiet; _base_build_index; _base_models_early_specs'
              % _models(ROW_A, ROW_B, ROW_LOCAL, ROW_SNAP, ROW_LATER, ROW_ELSEWHERE), env=_fake(tmp_path, BASE_STAGED="1"))
    kinds = sorted(l.split("|")[0] + " " + l.split("|")[1] for l in r.stdout.splitlines() if l.strip())
    assert kinds == ["FILE vae/Example/Real/b.safetensors", "SNAP RMBG/RMBG-2.0/"], r.stdout + r.stderr


def test_unit_a_partial_or_a_stand_in_is_left_to_the_model_stage(tmp_path):
    c = _pod(tmp_path)
    d = c / "models/vae/Example/Real"; d.mkdir(parents=True)
    (d / "a.safetensors").write_bytes(b"\0" * 10)                                              # partial
    (d / "b.safetensors").write_bytes(b"")                                                     # a stand-in shape
    r = _bash('%s; base_env_setup; base_discover quiet; _base_build_index; _base_models_early_specs' % _models(ROW_A, ROW_B), env=_fake(tmp_path))
    assert r.stdout.strip() == "", r.stdout + r.stderr


def test_unit_the_early_pass_is_off_where_it_must_be(tmp_path):
    _pod(tmp_path)
    for snippet in ('BASE_MODELS_FOREGROUND=1', 'BASE_DRY=1', 'BASE_SEED=1', '_base_runtime_on(){ return 0; }', 'MODELS=()'):
        r = _bash('%s; base_env_setup; base_discover quiet; %s; _base_models_early_start; echo "STARTED=$?"' % (_models(ROW_A), snippet), env=_fake(tmp_path))
        assert "STARTED=1" in r.stdout, (snippet, r.stdout + r.stderr)


# ---------------------------------------------------------------- the overlap, the join, the count

def test_unit_the_downloads_run_while_the_venv_builds(tmp_path):
    c = _pod(tmp_path); bin_, log = _stub_hf(tmp_path, delay="2")
    r = _bash('%s; base_env_setup; base_discover quiet; _base_models_early_start; echo "STARTED=$?";'
              ' t0=$(python3 -c "import time; print(time.time())"); sleep 2; echo "VENV_END=$(python3 -c "import time; print(time.time())")";'
              ' _base_models_early_join; base_models; echo "DL=${#MODEL_DL[@]} OK=${#MODEL_OK[@]} FAIL=${#MODEL_FAIL[@]}"; echo "T0=$t0"'
              % _models(ROW_A, ROW_B), env=_fake(tmp_path, BASE_FAKE_DL="real", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    out = r.stdout
    assert "STARTED=0" in out and "DL=2 OK=0 FAIL=0" in out, out + r.stderr
    venv_end = float(re.search(r"VENV_END=([\d.]+)", out).group(1))
    starts = [float(l.split()[2]) for l in log.read_text().splitlines() if l.startswith("start")]
    assert len(starts) == 2 and min(starts) < venv_end                         # the first download began before the "venv" ended
    assert out.count("a.safetensors downloaded") == 1                            # reported once, at the join
    assert "downloaded beside the venv build" in out
    assert (c / "models/vae/Example/Real/b.safetensors").stat().st_size == 4096


def test_unit_the_join_folds_the_counters_and_the_clock_into_the_run(tmp_path):
    _pod(tmp_path)
    r = _bash('%s; base_env_setup; base_discover quiet; _base_models_early_start; _base_models_early_join; base_models;'
              ' echo "DL=${#MODEL_DL[@]} OK=${#MODEL_OK[@]} GB=$MODEL_DL_GB"; printf "T %%s\\n" "${BASE_STAGE_TIMES[@]}"' % _models(ROW_A, ROW_B),
              env=_fake(tmp_path))
    out = r.stdout
    assert "DL=2 OK=0 GB=0.00" in out, out + r.stderr
    assert re.search(r"T \d+\|MODELS \(in the background, beside the venv build\)", out), out
    assert re.search(r"T \d+\|MODELS · joining the downloads", out), out
    assert "MODELS · started beside the venv build" in out                      # the background's own output, shown whole


def test_unit_a_failed_early_download_fails_the_run_by_name_once_without_a_second_round(tmp_path):
    _pod(tmp_path); bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf $*" >> "%s"; echo "boom"; exit 1\n' % log); (bin_ / "hf").chmod(0o755)
    r = _bash('sleep(){ :; }; %s; base_env_setup; base_discover quiet; _base_models_early_start; _base_models_early_join; FJOIN=${#BASE_FAILED[@]}; base_models;'
              ' echo "FJOIN=$FJOIN DL=${#MODEL_DL[@]}"; printf "F %%s\\n" "${BASE_FAILED[@]}"' % _models(ROW_A, ROW_B),
              env=_fake(tmp_path, BASE_FAKE_DL="real", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    out = r.stdout
    assert "FJOIN=0 DL=0" in out and "the model stage fails the run by their names" in out, out + r.stderr   # the join is not the verdict
    for f in ("a", "b"):
        assert out.count("F model: vae/Example/Real/%s.safetensors not obtained" % f) == 1, out          # the model stage's is, by name
    assert len(log.read_text().splitlines()) == 6                                                          # three attempts each, not six


def test_unit_the_disk_gate_stops_the_early_pass_before_anything_downloads(tmp_path):
    c = _pod(tmp_path)
    r = _bash('%s; base_env_setup; base_discover quiet; _base_models_early_start; _base_models_early_join; echo "DL=${#MODEL_DL[@]}"' % _models(ROW_A),
              env=_fake(tmp_path, BASE_FAKE_FREE_GB="1"))
    assert "DL=0" in r.stdout and "nothing downloaded early" in r.stdout, r.stdout + r.stderr
    assert not (c / "models/vae/Example/Real/a.safetensors").exists()


# ---------------------------------------------------------------- the downloader

def test_unit_every_download_prefers_the_fetch_venv_then_the_main_venv_then_path(tmp_path):
    home = tmp_path / "comfy-base"; venv = tmp_path / "venv"; pathbin = tmp_path / "pb"
    for d in (home / "fetch/bin", venv / "bin", pathbin):
        d.mkdir(parents=True)
    for d in (home / "fetch/bin", venv / "bin", pathbin):
        (d / "hf").write_text("#!/bin/sh\n"); (d / "hf").chmod(0o755)
    env = {"PATH": "%s:%s" % (pathbin, os.environ["PATH"])}
    pick = lambda: _bash('BASE_HOME=%s; VENV=%s; _base_hf_bin' % (home, venv), env=env).stdout.strip()
    assert pick() == str(home / "fetch/bin/hf")
    (home / "fetch/bin/hf").unlink(); assert pick() == str(venv / "bin/hf")
    (venv / "bin/hf").unlink(); assert pick() == str(pathbin / "hf")


def test_unit_the_downloads_and_the_xet_check_name_the_chosen_hf():
    src = (BASE / "lib" / "50-models.sh").read_text()
    for fn in ("_base_download(){", "_base_snapshot(){", "_base_xet_check(){"):
        i = src.index(fn); body = src[i:src.index("\n}\n", i)]
        assert "_base_hf_bin" in body and '"$VENV/bin/hf"' not in body, fn


def test_unit_the_fetch_venv_is_left_alone_while_a_fetch_later_runs_from_it(tmp_path):
    home = tmp_path / "comfy-base"; (home / "fetch/bin").mkdir(parents=True); (home / "state").mkdir(parents=True)
    (home / "fetch/bin/hf").write_text("#!/bin/sh\n"); (home / "fetch/bin/hf").chmod(0o755)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (home / "state/fetch-later.pid").write_text(str(sleeper.pid))
        r = _bash('BASE_HOME=%s; BASE_STATE=%s/state; uv(){ echo "UV CALLED $*"; }; _base_fetch_tool; echo "RC=$?"' % (home, home))
    finally:
        sleeper.kill(); sleeper.wait()
    assert "RC=0" in r.stdout and "UV CALLED" not in r.stdout and "used as it is" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- stopping

def test_unit_kill_tree_stops_a_job_and_everything_under_it(tmp_path):
    marker = tmp_path / "pids"
    r = _bash('( bash -c "sleep 60 & echo \\$! > %s.c; wait" & wait ) & p=$!; sleep 1; _base_kill_tree "$p"; sleep 1;'
              ' kill -0 "$p" 2>/dev/null && echo PARENT_ALIVE; kill -0 "$(cat %s.c)" 2>/dev/null && echo CHILD_ALIVE; echo done' % (marker, marker))
    assert "done" in r.stdout and "PARENT_ALIVE" not in r.stdout and "CHILD_ALIVE" not in r.stdout, r.stdout + r.stderr


def test_unit_a_run_that_ends_before_the_join_takes_its_downloads_with_it(tmp_path):
    _pod(tmp_path); bin_, log = _stub_hf(tmp_path, delay="31.7")
    r = _bash('trap _base_on_exit EXIT; %s; base_env_setup; base_discover quiet; _base_models_early_start; sleep 2; exit 1' % _models(ROW_A),
              env=_fake(tmp_path, BASE_FAKE_DL="real", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    assert r.returncode == 1, r.stdout + r.stderr
    time.sleep(1)
    ps = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout.splitlines()
    left = [l for l in ps if l.strip() == "sleep 31.7" or ("bin/hf download" in l and str(tmp_path) in l)]
    assert left == [], left
    assert "end" not in log.read_text()


# ---------------------------------------------------------------- the pipeline's order

def test_unit_the_early_start_follows_the_os_join_and_the_join_precedes_the_model_stage():
    src = (BASE / "lib" / "95-summary.sh").read_text()
    body = src[src.index("base_install(){"):src.index("_base_use_testbed(){")]
    start = body.index("_base_models_early_start"); join = body.index("_base_models_early_join")
    assert body.index("_base_system_join") < start < body.index("base_venv_snapshot")      # uv's bootstrap needs python3-venv from apt
    assert body.index("_base_hook pkg_pre_models") < join < body.index("base_models ||")
    assert start > body.index("base_packs git")                                             # custom_nodes/<Pack> dests are inside the packs


def test_unit_the_exit_trap_stops_the_early_downloads():
    src = (BASE / "lib" / "00-env.sh").read_text()
    body = src[src.index("_base_on_exit(){"):src.index("_base_install_traps(){")]
    assert "_base_models_early_stop" in body


# ---------------------------------------------------------------- end to end on a fake pod

@pytest.mark.install
@pytest.mark.skipif(not STUB.exists(), reason="_build/stub is repo tooling, not in the shipped base")
def test_install_a_fresh_run_downloads_beside_the_venv_and_counts_each_file_once(tmp_path):
    from basetest import fake_pod, run_script
    fake_pod(tmp_path, STUB)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    out = r.stdout
    assert r.returncode == 0, out[-3000:] + r.stderr[-1000:]
    assert out.index("model downloads start in the background") < out.index("══ VENV")
    assert out.index("══ MODELS · joining") < out.index("══ MODEL LIBRARY")
    assert "stub_vae.safetensors  (0.00 GB, downloaded beside the venv build)" in out
    assert re.search(r"models\s+ok \d+ \(.*\) · moved \d+ \(.*\) · downloaded 1 ", out), out[-3000:]


@pytest.mark.install
@pytest.mark.skipif(not STUB.exists(), reason="_build/stub is repo tooling, not in the shipped base")
def test_install_the_foreground_order_is_one_variable_away(tmp_path):
    from basetest import fake_pod, run_script
    fake_pod(tmp_path, STUB)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path, env={"BASE_MODELS_FOREGROUND": "1"})
    assert r.returncode == 0, r.stdout[-3000:]
    assert "started beside the venv build" not in r.stdout and "stub_vae.safetensors downloaded" in r.stdout
