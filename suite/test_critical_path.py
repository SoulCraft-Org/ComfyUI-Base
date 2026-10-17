"""unit tier, 3.22.0: install speed on the critical path, and the clock that shows it.

The pod of 2026-10-17 (1x B200, base 3.20.0, the Video Replacer with every row) spent 116 s in VENV, the apt install of
cuda-libraries-dev inside it although nothing built from source, and its stage clock could not say what ran beside
what: seconds per stage, no wall times, no split between a file's download and its sha256 re-read. The promises:
the cuBLAS probe runs beside the venv build and every build after it still finds its verdict; the run's timeline is
written as wall-clock spans, background stages and each model file included."""
import os, pathlib, re, subprocess, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]

ROW_A = "vae|Example|Real|a.safetensors|https://huggingface.co/x/y/resolve/main/a.safetensors|4096|n|"
ROW_B = "vae|Example|Real|b.safetensors|https://huggingface.co/x/y/resolve/main/b.safetensors|4096|n|"


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e)


def _stub_nvcc(d, rc=0, delay="0"):
    """A compiler that links (rc 0) or cannot (rc 1) after `delay` seconds: the probe's whole question."""
    d.mkdir(parents=True, exist_ok=True)
    p = d / "nvcc"
    p.write_text('#!/bin/bash\n[ "$1" = "--version" ] && { echo "Cuda compilation tools, release 13.0, V13.0.88"; exit 0; }\n'
                 'sleep %s\nexit %d\n' % (delay, rc))
    p.chmod(0o755)
    return p


def _env(tmp_path, nvcc, **extra):
    e = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_HOME": str(tmp_path / "comfy-base"), "CUDACXX": str(nvcc)}
    e.update(extra)
    return e


NOW = 'python3 -c "import time; print(time.time())"'


# ---------------------------------------------------------------- the cuBLAS probe beside the venv build

def test_unit_the_probe_runs_in_the_background_and_its_verdict_is_folded_at_the_join(tmp_path):
    nvcc = _stub_nvcc(tmp_path / "bin", rc=0, delay="2")
    r = _bash(f'base_env_setup; t0=$({NOW}); _base_cuda_toolchain_start; echo "STARTED=$?"; t1=$({NOW});'
              f' _base_cuda_toolchain_join; t2=$({NOW}); echo "OK=$BASE_CUDA_BUILD_OK START=$t0 BACK=$t1 JOINED=$t2";'
              ' printf "T %s\\n" "${BASE_STAGE_TIMES[@]}"; printf "S %s\\n" "${BASE_SPANS[@]}"', env=_env(tmp_path, nvcc))
    out = r.stdout + r.stderr
    assert "STARTED=0" in out and "OK=1" in out, out[-1500:]
    t0, t1, t2 = (float(re.search(k + r"=([\d.]+)", out).group(1)) for k in ("START", "BACK", "JOINED"))
    assert t1 - t0 < 1.5 <= t2 - t0, out[-1500:]                        # the start returned at once; the join waited for the link
    assert "CUDA toolchain links cuBLAS" in out                          # the probe's own output, shown at the join
    assert re.search(r"T \d+\|CUDA TOOLCHAIN \(in the background", out), out
    assert re.search(r"S \d+\|\d+\|CUDA TOOLCHAIN \(in the background", out), out


def test_unit_a_toolkit_that_cannot_link_is_still_named_after_the_background_probe(tmp_path):
    nvcc = _stub_nvcc(tmp_path / "bin", rc=1)
    r = _bash('base_env_setup; _base_can_root(){ return 1; }; _base_cuda_toolchain_start; _base_cuda_toolchain_join;'
              ' echo "OK=$BASE_CUDA_BUILD_OK"; printf "W %s\\n" "${BASE_WARN[@]}"', env=_env(tmp_path, nvcc))
    out = r.stdout + r.stderr
    assert "OK=0" in out, out[-1500:]
    assert re.search(r"W CUDA toolkit incomplete.*cuda-libraries-dev-13-0", out), out[-1500:]   # the warning comes back to the run


def test_unit_the_foreground_order_is_one_variable_away_and_a_dry_run_keeps_it(tmp_path):
    nvcc = _stub_nvcc(tmp_path / "bin", rc=0)
    for extra in ({"BASE_CUDA_FOREGROUND": "1"}, {"BASE_DRY": "1"}):
        r = _bash('base_env_setup; _base_cuda_toolchain_start; echo "STARTED=$?"; _base_cuda_toolchain_join; echo "OK=$BASE_CUDA_BUILD_OK"',
                  env=_env(tmp_path, nvcc, **extra))
        out = r.stdout + r.stderr
        assert "STARTED=1" in out and "OK=1" in out, (extra, out[-1500:])


def test_unit_a_probe_that_ended_without_a_verdict_is_asked_again(tmp_path):
    nvcc = _stub_nvcc(tmp_path / "bin", rc=0)
    r = _bash('base_env_setup; _BASE_CUDA_DIR="$(mktemp -d)"; true & _BASE_CUDA_PID=$!; _base_cuda_toolchain_join; echo "OK=$BASE_CUDA_BUILD_OK"',
              env=_env(tmp_path, nvcc))
    out = r.stdout + r.stderr
    assert "probing again" in out and "OK=1" in out, out[-1500:]


def test_unit_a_run_that_ends_early_waits_for_the_probe_and_never_kills_it(tmp_path):
    # the probe may be inside apt: killed half way, dpkg needs `dpkg --configure -a` before any apt works again
    mark = tmp_path / "finished"
    r = _bash(f't0=$({NOW}); ( sleep 2; touch "{mark}" ) & _BASE_CUDA_PID=$!; _base_cuda_toolchain_stop;'
              f' echo "WAITED=$(python3 -c "import sys; print(float(sys.argv[2]) - float(sys.argv[1]))" "$t0" "$({NOW})") PID=[$_BASE_CUDA_PID]"')
    out = r.stdout + r.stderr
    assert mark.exists() and "PID=[]" in out, out
    assert float(re.search(r"WAITED=([\d.]+)", out).group(1)) >= 1.5, out
    src = (BASE / "lib" / "40-packs.sh").read_text()
    body = src[src.index("_base_cuda_toolchain_stop(){"):]
    assert "_base_kill_tree" not in body[:body.index("\n}\n")]


def test_unit_the_pipeline_starts_the_probe_with_the_venv_and_joins_it_before_the_first_build():
    src = (BASE / "lib" / "95-summary.sh").read_text()
    body = src[src.index("base_install(){"):src.index("_base_use_testbed(){")]
    start, join = body.index("_base_cuda_toolchain_start"), body.index("_base_cuda_toolchain_join")
    assert body.index("_base_system_join") < start < body.index("base_venv_snapshot")   # apt is free again (the OS stage joined)
    venv = body.index("if ! base_venv;")                                                 # (the runtime branch above has its own hook)
    assert venv < join < body.index("_base_hook pkg_post_venv", venv)                     # the first place a build can start
    assert not re.search(r"^\s*base_cuda_toolchain\b", body, re.M)                       # never a second, foreground probe
    env = (BASE / "lib" / "00-env.sh").read_text()
    assert "_base_cuda_toolchain_stop" in env[env.index("_base_on_exit(){"):env.index("_base_install_traps(){")]


# ---------------------------------------------------------------- the timeline

def test_unit_the_timeline_is_every_span_in_wall_clock_order_beside_the_timings(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    r = _bash(f'{{ SECONDS=0; hdr "NODE PACKS · locate"; SECONDS=5; hdr "VENV · /x/.venv"; SECONDS=9; hdr "SUMMARY · x"; }} >/dev/null;'
              ' _base_span 2 7 "SYSTEM (in the background)"; BASE_FILE_SPANS=("3|8|download vae/a.safetensors" "6|8|sha256 vae/a.safetensors");'
              f' BASE_EPOCH0=1000; BASE_STATE="{state}"; PKG_ID=krea; _base_timings_write;'
              ' cat "$BASE_STATE"/logs/krea_*.timings.tsv; echo ---; cat "$BASE_STATE"/logs/krea_*.timeline.tsv')
    assert r.returncode == 0, r.stderr
    timings, timeline = r.stdout.split("---\n")
    assert timings.splitlines() == ["5\tNODE PACKS · locate", "4\tVENV · /x/.venv"]      # unchanged: seconds TAB stage
    rows = [l.split("\t") for l in timeline.splitlines() if not l.startswith("#")]
    assert rows == [["1000", "1005", "5", "NODE PACKS · locate"], ["1002", "1007", "5", "SYSTEM (in the background)"],
                    ["1003", "1008", "5", "download vae/a.safetensors"], ["1005", "1009", "4", "VENV · /x/.venv"],
                    ["1006", "1008", "2", "sha256 vae/a.safetensors"]], timeline
    assert len(list((state / "logs").iterdir())) == 2 and timeline.startswith("# start\tend\tseconds\twhat")


def test_unit_a_file_s_line_and_spans_carry_its_sha256_seconds(tmp_path):
    dest = tmp_path / "a.safetensors"; dest.write_bytes(b"\0" * 4096)
    r = _bash(f'MODEL_DL=(); DL_DESTS=(); MODEL_DL_GB=0; BASE_DL_BYTES=0; BASE_DL_TIME=0;'
              f' _base_fetch_fold 0 10 "FILE|vae/a.safetensors|u|4096|{dest}" 100 3; _base_fetch_fold 0 4 "FILE|vae/b.safetensors|u|4096|{dest}";'
              ' printf "F %s\\n" "${BASE_FILE_SPANS[@]}"')
    out = r.stdout
    assert "vae/a.safetensors downloaded (0.00 GB, verified)" in out and "(sha256 3s of it)" in out, out + r.stderr
    assert re.findall(r"^F .*", out, re.M) == ["F 100|110|download vae/a.safetensors", "F 107|110|sha256 vae/a.safetensors"], out
    assert out.count("sha256") == 2                                    # the second file gave no start: no spans, no sha256 note


def test_unit_parallel_jobs_hand_back_their_start_and_sha256_seconds(tmp_path):
    a, b = tmp_path / "a.safetensors", tmp_path / "b.safetensors"
    r = _bash('MODEL_DL=(); DL_DESTS=(); MODEL_DL_GB=0; BASE_DL_BYTES=0; BASE_DL_TIME=0; BASE_FETCH_JOBS=2;'
              ' _base_fetch_one(){ sleep 2; BASE_VERIFY_SECS=2; head -c 4096 /dev/zero > "${1##*|}"; };'
              f' _base_fetch_queue "FILE|vae/a.safetensors|u|4096|{a}" "FILE|vae/b.safetensors|u|4096|{b}";'
              ' printf "F %s\\n" "${BASE_FILE_SPANS[@]}"')
    out = r.stdout
    assert out.count("(sha256 2s of it)") == 2, out + r.stderr
    spans = re.findall(r"^F (\d+)\|(\d+)\|(\w+) (\S+)$", out, re.M)
    assert sorted((k, f) for _, _, k, f in spans) == [("download", "vae/a.safetensors"), ("download", "vae/b.safetensors"),
                                                       ("sha256", "vae/a.safetensors"), ("sha256", "vae/b.safetensors")], out


def test_unit_the_early_downloads_bring_their_spans_back_to_the_run(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "500"}
    r = _bash('MODELS=("%s" "%s"); base_env_setup; base_discover quiet; _base_models_early_start; _base_models_early_join;'
              ' printf "F %%s\\n" "${BASE_FILE_SPANS[@]}"; printf "S %%s\\n" "${BASE_SPANS[@]}"' % (ROW_A, ROW_B), env=env)
    out = r.stdout
    assert len(re.findall(r"^F \d+\|\d+\|download vae/Example/Real/[ab]\.safetensors$", out, re.M)) == 2, out + r.stderr
    assert re.search(r"^S \d+\|\d+\|MODELS \(in the background, beside the venv build\)$", out, re.M), out


# ---------------------------------------------------------------- the Mac's half: podctl's line clock

def test_unit_podctl_stamps_its_lines_only_when_asked():
    tool = BASE / "_build" / "pod" / "podctl.py"
    if not tool.exists():
        pytest.skip("no _build/pod/podctl.py (not a repo checkout)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("podctl_clock", tool)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod._stamp({}, 0.0) == [] and mod._stamp({"PODCTL_TIMES": "1"}, None) == []    # off unless asked: parsers read lines as before
    (stamp,) = mod._stamp({"PODCTL_TIMES": "1"}, 0.0)
    assert re.fullmatch(r"\[\d\d:\d\d:\d\dZ \+[\d.]+s\]", stamp), stamp
