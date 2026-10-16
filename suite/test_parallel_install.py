"""unit and install tiers, 3.11.0: the install's independent steps run at once (Paul, 2026-10-16: "Parallelization of
everything. Speed for everything!"), and a store that already holds the exact install starts in seconds.

What each test guards: six downloads at once and the biggest started first; the packs cloned and fetched together and
recorded in row order; JupyterLab's tools venv built beside the main venv and joined at BOOT; a B300 (sm_103) accepted
by a torch built for sm_100; no second resolve of a set already installed; the heartbeat that no longer walks the
venv; the Xet settings that do nothing gone; and the skip for a store that already holds this package green."""
import os, pathlib, re, subprocess, sys, time, pytest
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"
sys.path.insert(0, str(BASE / "py"))


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s' % (BASE, snippet)], capture_output=True, text=True, env=e)


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "custom_nodes").mkdir(); (c / "main.py").write_text(""); return c


def _fake(tmp_path, **extra):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "500"}
    env.update(extra)
    return env


def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _code(path, start, end=None):
    src = (BASE / path).read_text()
    i = src.index(start)
    return src[i:src.index(end, i)] if end else src[i:]


# ---------------------------------------------------------------- the B300: torch's sm_100 kernels run on sm_103

def test_unit_a_gpu_is_covered_by_its_family_or_by_ptx():
    from arch_fit import covering
    torch_2_14 = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"]
    assert covering("sm_103", torch_2_14) == "sm_100"                       # B300: a template default
    assert covering("sm_100", torch_2_14) == "sm_100"                       # B200: exact
    assert covering("sm_121", torch_2_14) == "sm_120"
    assert covering("sm_89", torch_2_14) == "sm_86"                         # the closest older minor of the family
    assert covering("sm_110", torch_2_14 + ["compute_100"]) == "compute_100"   # PTX the driver compiles forward


def test_unit_a_truly_missing_arch_is_still_missing():
    from arch_fit import covering
    assert covering("sm_110", ["sm_90", "sm_100", "sm_120"]) == ""          # another family and no PTX
    assert covering("sm_70", ["sm_75", "sm_80"]) == ""                      # newer minors never run on an older GPU
    assert covering("sm_103", ["sm_100a"]) == ""                            # an arch-specific build runs on 10.0 only
    assert covering("sm_120", ["compute_121"]) == ""                        # PTX never runs on an older GPU


def test_unit_the_probe_and_the_verify_both_judge_by_family():
    for f in ("py/venv_probe.py", "py/venv_verify.py"):
        src = (BASE / f).read_text()
        assert "from arch_fit import covering" in src and "not in torch.cuda.get_arch_list()" not in src and "want_sm not in archs" not in src, f


def test_unit_the_probe_keeps_a_b300_venv_built_for_sm_100(tmp_path):
    fake = tmp_path / "torch"; fake.mkdir()
    (fake / "__init__.py").write_text("__version__ = '2.14.1+cu130'\nclass version: cuda = '13.0'\n"
                                      "class cuda:\n    @staticmethod\n    def get_arch_list(): return ['sm_90', 'sm_100', 'sm_120']\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "VENV": str(tmp_path), "BASE_WANT_BACKEND": "cu130"}
    for sm, ok in (("sm_103", True), ("sm_110", False)):
        r = subprocess.run([sys.executable, str(BASE / "py" / "venv_probe.py")], capture_output=True, text=True, env={**env, "BASE_WANT_SM": sm})
        assert ("no kernels that run on" not in r.stdout) == ok, (sm, r.stdout + r.stderr)


# ---------------------------------------------------------------- downloads: six at once, the biggest first

def test_unit_six_downloads_at_once_by_default_three_on_a_small_machine():
    assert _bash('_base_fetch_jobs').stdout.strip() in ("3", "6")
    src = _code("lib/50-models.sh", "_base_fetch_jobs(){", "\n}\n")
    assert 'n=3; else n=6' in src and "33554432" in src                      # 32 GiB in kB, from /proc/meminfo
    assert _bash('BASE_FETCH_JOBS=2 _base_fetch_jobs').stdout.strip() == "2"


def test_unit_the_largest_download_starts_first_and_the_report_keeps_row_order(tmp_path):
    _pod(tmp_path); bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "times.log"
    sizes = {"small": 1024, "big": 8192, "mid": 4096}
    (bin_ / "hf").write_text(
        '#!/bin/bash\nf="$3"; d=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
        'echo "start $f $(python3 -c "import time; print(time.time())")" >> "%s"; sleep 0.5\nmkdir -p "$d"\n'
        'python3 - "$d/$f" <<"PY"\nimport sys\nn = sys.argv[1].rsplit("/", 1)[-1].split(".")[0]\n'
        'open(sys.argv[1], "wb").truncate({"small": 1024, "big": 8192}.get(n, 4096))\nPY\n' % log)
    (bin_ / "hf").chmod(0o755)
    rows = ['vae|Example|Real|%s.safetensors|https://huggingface.co/x/y/resolve/main/%s.safetensors|%d|n|' % (n, n, b) for n, b in sizes.items()]
    r = _bash('MODELS=(%s); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % " ".join('"%s"' % x for x in rows),
              env=_fake(tmp_path, BASE_FAKE_DL="real", BASE_FETCH_JOBS="1", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    assert "DL=3" in r.stdout, r.stdout + r.stderr                           # one job: row order, as 3.0 did
    log.unlink()
    for f in tmp_path.glob("ComfyUI/models/vae/Example/Real/*"): f.unlink()
    r = _bash('MODELS=(%s); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % " ".join('"%s"' % x for x in rows),
              env=_fake(tmp_path, BASE_FAKE_DL="real", BASE_FETCH_JOBS="2", PATH="%s:%s" % (bin_, os.environ["PATH"])))
    assert "DL=3" in r.stdout and "largest first" in r.stdout, r.stdout + r.stderr
    starts = [l.split()[1] for l in log.read_text().splitlines()]
    assert starts[0] == "big.safetensors", starts                            # the longest transfer never waits for a slot
    out = r.stdout
    assert out.index("small.safetensors downloaded") < out.index("big.safetensors downloaded") < out.index("mid.safetensors downloaded")


def test_unit_the_download_queue_waits_only_for_its_own_jobs():
    src = _code("lib/50-models.sh", "_base_fetch_queue(){", "\n}\n")
    assert "jobs -pr" not in src and re.search(r"\n\s*wait\s*\n", src) is None and 'wait ${pids[@]+"${pids[@]}"}' in src


# ---------------------------------------------------------------- packs: cloned and fetched together

def _origin(tmp_path, name):
    origin = tmp_path / f"{name}.git"; seed = tmp_path / f"{name}-seed"
    _git("init", "-q", "--bare", str(origin)); _git("clone", "-q", str(origin), str(seed))
    (seed / "__init__.py").write_text("v = 1\n"); _git("add", ".", cwd=seed)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c1", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed); _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, _git("rev-parse", "HEAD", cwd=seed)


def _git_spy(tmp_path):
    """git, but a clone or fetch logs its start and end and takes a second: overlapping spans prove they ran together."""
    bin_ = tmp_path / "gitbin"; bin_.mkdir(); log = tmp_path / "git.log"
    real = subprocess.run(["bash", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    (bin_ / "git").write_text('#!/bin/bash\nfor a in "$@"; do case "$a" in clone|fetch) k=$a;; esac; done\n'
                              'if [ -n "${k:-}" ]; then echo "start $k $$ $(python3 -c "import time; print(time.time())")" >> "%s"; sleep 1; fi\n'
                              '"%s" "$@"; rc=$?\n[ -n "${k:-}" ] && echo "end $k $$ $(python3 -c "import time; print(time.time())")" >> "%s"\nexit $rc\n' % (log, real, log))
    (bin_ / "git").chmod(0o755)
    return bin_, log


def test_unit_packs_are_cloned_at_once_and_recorded_in_row_order(tmp_path):
    origins = [_origin(tmp_path, n) for n in ("PackA", "PackB", "PackC")]
    _pod(tmp_path); bin_, log = _git_spy(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    rows = " ".join('"Pack%s|%s|%s||t"' % (n, o, "0" * 40) for n, (o, _) in zip("ABC", origins))
    r = _bash('base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=(%s); base_packs git; echo FAILED=${#BASE_FAILED[@]}; printf "D %%s\\n" "${PACK_DIRS[@]}"' % rows,
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    out = r.stdout
    assert "FAILED=0" in out, out + r.stderr
    for n, (o, head) in zip("ABC", origins):
        assert _git("rev-parse", "HEAD", cwd=cn / f"Pack{n}") == head
    spans = [l.split() for l in log.read_text().splitlines() if " clone " in l]
    starts = [float(s[3]) for s in spans if s[0] == "start"]; ends = [float(s[3]) for s in spans if s[0] == "end"]
    assert len(starts) == 3 and max(starts) < min(ends), spans                # all three in flight together
    assert out.index("PackA cloned") < out.index("PackB cloned") < out.index("PackC cloned")
    assert out.index('"PackA|') < out.index('"PackB|') < out.index('"PackC|')   # the records podctl saves, in row order


def test_unit_a_failed_clone_among_good_ones_is_named_and_is_no_pack_dir(tmp_path):
    good, _ = _origin(tmp_path, "PackG")
    _pod(tmp_path)
    rows = '"PackG|%s|%s||t" "PackX|%s|%s||t"' % (good, "0" * 40, tmp_path / "missing.git", "0" * 40)
    r = _bash('base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=(%s); base_packs git; printf "F %%s\\n" "${BASE_FAILED[@]}"; printf "D %%s\\n" "${PACK_DIRS[@]}"' % rows,
              env={"BASE_FAKE_ROOT": str(tmp_path)})
    out = r.stdout
    assert "F pack: PackX clone failed" in out and "PackG cloned" in out, out + r.stderr
    assert "D PackG|" in out and "D PackX|" not in out


# ---------------------------------------------------------------- JupyterLab's tools venv, beside the main venv

def test_unit_the_tools_venv_builds_in_the_background_and_its_results_reach_the_run(tmp_path):
    r = _bash('base_boot_tools(){ ok "jupyter built"; BASE_TOOLS_RESULT="built (Python 3.15)"; BASE_CHANGED+=("tools venv: JupyterLab"); warn "a note"; };'
              ' _base_tools_start; echo "STARTED=$?"; echo "BEFORE=${BASE_TOOLS_RESULT:-}"; _base_tools_join; echo "JOINED=$?";'
              ' echo "RESULT=$BASE_TOOLS_RESULT"; printf "C %s\\n" "${BASE_CHANGED[@]}"; printf "T %s\\n" "${BASE_STAGE_TIMES[@]}"', env={"BASE_NO_NET": "1"})
    out = r.stdout
    assert "STARTED=0" in out and "BEFORE=not run\n" in out and "JOINED=0" in out, out + r.stderr
    assert "jupyter built" in out and "RESULT=built (Python 3.15)" in out and "C tools venv: JupyterLab" in out
    assert re.search(r"T \d+\|TOOLS VENV \(in the background\)", out), out
    assert out.index("BEFORE=") < out.index("jupyter built")                   # its output is shown at the join


def test_unit_the_boot_stage_joins_the_tools_venv_or_builds_it_itself():
    src = _code("lib/75-boot.sh", "base_boot_install(){", "\n}\n")
    assert "_base_tools_join || base_boot_tools" in src
    r = _bash('BASE_DRY=1; _base_tools_start; echo "STARTED=$?"; _base_tools_join; echo "JOINED=$?"')
    assert "STARTED=1" in r.stdout and "JOINED=1" in r.stdout


def test_unit_the_pipeline_starts_the_background_work_after_the_os_join_and_hooks_the_local_runtime():
    body = _code("lib/95-summary.sh", "base_install(){", "_base_use_testbed(){")
    tools = body.index("_base_tools_start"); early = body.index("_base_models_early_start")
    assert body.index("_base_system_join") < early < tools < body.index("base_venv_snapshot")
    prewarm = body.index("_base_run_prewarm")
    assert body.index("base_venv_latest") < prewarm < body.index("_base_hook pkg_pre_models")
    assert "! declare -F _base_run_prewarm >/dev/null ||" in body               # absent until lib/84-run-local.sh lands
    exitsrc = _code("lib/00-env.sh", "_base_on_exit(){", "_base_install_traps(){")
    assert "_base_tools_stop" in exitsrc and "_base_models_early_stop" in exitsrc


# ---------------------------------------------------------------- no second resolve of the same set

def test_unit_a_first_install_pays_no_dry_run_and_no_second_resolve():
    build = _code("lib/30-venv.sh", "_base_venv_build(){", "\n}\n")
    assert 'if [ "$had" = "0" ]; then' in build and "no venv to protect" in build
    assert 'BASE_DERIVED_DONE="$(_base_derived_fp)"' in build
    i = build.index('BASE_DERIVED_DONE="$(_base_derived_fp)"')
    assert build.rindex("base_torch_constraints", 0, i) > build.index("_base_derived_install")   # taken as the packs' step takes it
    pip = _code("lib/40-packs.sh", "# ---- pip half", "_base_pack_imports_report ${probe")
    assert '[ "$(_base_derived_fp)" = "$BASE_DERIVED_DONE" ]' in pip and "not resolved twice" in pip


# ---------------------------------------------------------------- the heartbeat and the Xet settings

def test_unit_the_heartbeat_never_walks_the_venv():
    body = _code("lib/00-env.sh", "_base_watch(){", "\n}\n")
    code = "\n".join(l.split("#", 1)[0] for l in body.splitlines()[1:])
    assert not re.search(r"\bdu\b", code), code


def test_unit_the_xet_settings_that_do_nothing_are_gone():
    for f in ("lib/00-env.sh", "lib/30-venv.sh", "lib/50-models.sh"):
        src = (BASE / f).read_text()
        assert "export HF_XET_NUM_CONCURRENT_RANGE_GETS" not in src and "export HF_XET_CHUNK_CACHE_SIZE_BYTES" not in src, f
        assert "concurrent range gets" not in src, f
    assert "export HF_XET_HIGH_PERFORMANCE=1" in (BASE / "lib" / "00-env.sh").read_text()


# ---------------------------------------------------------------- a store that already holds the exact install

def _green_store(tmp_path):
    """A store after a green run of a one-row package: the venv, boot.env, the ledger entry, the file in place."""
    _pod(tmp_path)
    pkg = tmp_path / "pkg"; pkg.mkdir(); (pkg / "p-script.sh").write_text("# the package\n")
    state = tmp_path / "comfy-base" / "state"; (state / "packages").mkdir(parents=True); (state / "last-run").mkdir()
    (state / "boot.env").write_text("COMFY=x\n"); (state / "packages" / "p.manifest").write_text("p\n")
    venv = tmp_path / "ComfyUI" / ".venv-cu130" / "bin"; venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    d = tmp_path / "ComfyUI" / "models" / "vae" / "Example" / "Real"; d.mkdir(parents=True); (d / "a.safetensors").write_bytes(b"\0" * 4096)
    return pkg, state


ROW = '"vae|Example|Real|a.safetensors|https://huggingface.co/x/y/resolve/main/a.safetensors|4096|n|"'


def _green(tmp_path, pkg, state, line_extra="", pre="", sm=""):
    fp = _bash('PKG_DIR=%s; _base_green_fp' % pkg).stdout.strip()
    ver = _bash('echo $BASE_VERSION').stdout.strip()
    (state / "last-run" / "p").write_text("green 20261016-120000 1.0.0 base=%s sm=%s %s%s\n" % (ver, sm, fp, line_extra))
    return _bash('%s PKG_ID=p PKG_VERSION=1.0.0 PKG_DIR=%s MODELS=(%s); base_env_setup; base_discover quiet; BASE_GPU_SM="%s";'
                 ' if _base_already_green; then echo GREEN; else echo "FULL: $BASE_GREEN_WHY"; fi' % (pre, pkg, ROW, sm), env=_fake(tmp_path))


def test_unit_the_exact_install_already_on_the_store_is_recognised(tmp_path):
    pkg, state = _green_store(tmp_path)
    assert "GREEN" in _green(tmp_path, pkg, state).stdout


def test_unit_any_difference_means_a_full_install(tmp_path):
    pkg, state = _green_store(tmp_path)
    r = _green(tmp_path, pkg, state, pre="BASE_FULL=1;"); assert "FULL: a full run was asked for" in r.stdout, r.stdout + r.stderr
    r = _green(tmp_path, pkg, state, pre="BASE_DRY=1;"); assert "FULL:" in r.stdout
    (pkg / "p-script.sh").write_text("# the package, changed under the same version\n")
    r = _bash('PKG_ID=p PKG_VERSION=1.0.0 PKG_DIR=%s MODELS=(%s); base_env_setup; base_discover quiet; if _base_already_green; then echo GREEN; else echo "FULL: $BASE_GREEN_WHY"; fi' % (pkg, ROW), env=_fake(tmp_path))
    assert "FULL: the package's files or the base's differ" in r.stdout, r.stdout + r.stderr
    r = _bash('PKG_ID=p PKG_VERSION=1.0.1 PKG_DIR=%s MODELS=(%s); base_env_setup; base_discover quiet; if _base_already_green; then echo GREEN; else echo "FULL: $BASE_GREEN_WHY"; fi' % (pkg, ROW), env=_fake(tmp_path))
    assert "installed 1.0.0, this is 1.0.1" in r.stdout


def test_unit_another_gpu_arch_or_a_missing_file_means_a_full_install(tmp_path):
    pkg, state = _green_store(tmp_path)
    fp = _bash('PKG_DIR=%s; _base_green_fp' % pkg).stdout.strip(); ver = _bash('echo $BASE_VERSION').stdout.strip()
    (state / "last-run" / "p").write_text("green 20261016-120000 1.0.0 base=%s sm=sm_100 %s\n" % (ver, fp))
    probe = 'PKG_ID=p PKG_VERSION=1.0.0 PKG_DIR=%s MODELS=(%s); base_env_setup; base_discover quiet; BASE_GPU_SM=%s; if _base_already_green; then echo GREEN; else echo "FULL: $BASE_GREEN_WHY"; fi'
    assert "GREEN" in _bash(probe % (pkg, ROW, "sm_100"), env=_fake(tmp_path)).stdout
    assert "this GPU is sm_103" in _bash(probe % (pkg, ROW, "sm_103"), env=_fake(tmp_path)).stdout
    (tmp_path / "ComfyUI/models/vae/Example/Real/a.safetensors").unlink()
    assert "a.safetensors is no longer in place" in _bash(probe % (pkg, ROW, "sm_100"), env=_fake(tmp_path)).stdout


def test_unit_a_run_before_3_11_never_counts_as_the_exact_install(tmp_path):
    pkg, state = _green_store(tmp_path)
    (state / "last-run" / "p").write_text("green 20261016-120000 1.0.0\n")      # the 3.10 line: no base, no fingerprint
    r = _bash('PKG_ID=p PKG_VERSION=1.0.0 PKG_DIR=%s MODELS=(%s); base_env_setup; base_discover quiet; if _base_already_green; then echo GREEN; else echo "FULL: $BASE_GREEN_WHY"; fi' % (pkg, ROW), env=_fake(tmp_path))
    assert "on base before 3.11.0" in r.stdout, r.stdout + r.stderr


@pytest.mark.install
@pytest.mark.skipif(not STUB.exists(), reason="_build/stub is repo tooling, not in the shipped base")
def test_install_a_second_run_of_the_same_install_takes_seconds_and_full_runs_everything(tmp_path):
    from basetest import fake_pod, run_script
    fake_pod(tmp_path, STUB)
    first = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert first.returncode == 0, first.stdout[-3000:]
    line = (tmp_path / "comfy-base/state/last-run/stub").read_text()
    assert line.startswith("green ") and "base=" in line and "pkgfp=" in line and "basefp=" in line, line
    t0 = time.time(); again = run_script(STUB / "stub-script.sh", fake_root=tmp_path); took = time.time() - t0
    assert again.returncode == 0 and "ALREADY INSTALLED" in again.stdout and "══ VENV" not in again.stdout, again.stdout[-3000:]
    assert took < 60, took
    full = run_script(STUB / "stub-script.sh", fake_root=tmp_path, env={"BASE_FULL": "1"})
    assert full.returncode == 0 and "ALREADY INSTALLED" not in full.stdout and "══ VENV" in full.stdout, full.stdout[-3000:]

