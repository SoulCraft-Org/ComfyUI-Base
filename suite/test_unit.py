"""unit tier: source contracts on the base library. Every test reads the library's text or runs a
function against a throwaway tree; nothing here needs the pod, the network, or a ComfyUI checkout."""
import os, re, json, shutil, subprocess, pathlib, sys, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
LIB = sorted((BASE / "lib").glob("*.sh"))


def _repo_only(what="_build/"):
    """The zip ships lib/ py/ suite/ hosts/, not _build/ (the stub package, package.py, rehearse.py) nor the repo's testbed:
    a test that needs them skips on a pod with the reason, instead of failing 35 times (pod run 2026-09-05)."""
    if not (BASE / "_build").exists():
        pytest.skip("%s is repo tooling, not in the shipped base" % what)


def _stub():
    _repo_only("_build/stub")
    return BASE / "_build" / "stub"


def _src(p):
    return p.read_text(encoding="utf-8")


def code_only(text):
    """Drop full-line comments and trailing '  # ...' comments so a word in a comment never satisfies a test."""
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        out.append(re.sub(r"\s{2,}#.*$", "", ln))
    return "\n".join(out)


def _consumer_root():
    """3.6.0: the root the cross-package checks walk: $BASE_WORKSPACE (handed to the suite by base_test), else <root> of
    <root>/base/comfyui-base; a standalone base has none, and those checks skip (the 2.2.0 guard, kept)."""
    import pkgdirs
    root = pkgdirs.consumer_root(BASE, None, os.environ)
    if root is None:
        pytest.skip("no consumer root: no $BASE_WORKSPACE and this base is not <root>/base/comfyui-base")
    return root


def _no_workspace_env():
    """The inherited environment minus BASE_WORKSPACE, for a subprocess that builds its OWN consumer tree: a workspace
    the session exported would otherwise take precedence over the temp tree the test is about."""
    return {k: v for k, v in os.environ.items() if k != "BASE_WORKSPACE"}


def _bash(snippet, env=None, cwd=None):
    """Source base.sh, then run a bash snippet. Returns CompletedProcess."""
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e, cwd=cwd)


# ---------------------------------------------------------------- T1: skeleton and plumbing

def test_unit_every_bash_file_parses():
    for p in [BASE / "base.sh", *LIB]:
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0, p.name


def test_unit_version_file_is_semver_and_base_prints_it():
    v = (BASE / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == v
    # 2.4.0: the handbook SHIPS inside comfyui-base.zip, so a stale version line installs a guide describing a base
    # the machine is not running. Nothing executes prose, so nothing caught it until a person read it; this does.
    hb = (BASE / "comfyui-base-handbook.md").read_text()
    assert "**Version %s.**" % v in hb, "the handbook does not open with **Version %s.** - it has drifted from VERSION" % v
    assert "\n- %s:" % v in hb, "the handbook's Record section has no '- %s:' entry" % v


def test_unit_sourcing_defines_the_contract_helpers():
    probe = ('for f in ok miss err note hdr would todo warn base_mkdir base_run _base_confirm _base_confirm_yes '
             '_base_on_err _base_on_exit _base_install_traps base_env_setup _base_vge _base_fs_is_pool _base_sha256 '
             '_base_replace_file _base_git _base_ts _base_timeout _base_in_list _base_fsize _base_fdev _base_free_kb; do '
             'declare -F "$f" >/dev/null || { echo "missing $f"; exit 1; }; done; echo "v=$BASE_VERSION"')
    r = _bash(probe)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "v=" in r.stdout


_FN_DEF = re.compile(r"(?:^\s*|[;&|({]\s*|\b(?:then|else|do)\s+)(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")


def test_unit_no_function_is_defined_twice_in_one_shell():
    """3.7.2: base.sh sources lib/[0-9][0-9]-*.sh in order into ONE shell, so a name defined in two files is silently the
    later one. 00-env's hook helper base_run was shadowed by 95-summary's install pipeline of the same name, and a hook
    that called the helper re-entered the whole install without end. boot.sh is its own shell and is checked alone."""
    shells = {"base.sh + lib/[0-9][0-9]-*.sh": [BASE / "base.sh", *sorted((BASE / "lib").glob("[0-9][0-9]-*.sh"))],
              "lib/boot.sh": [BASE / "lib" / "boot.sh"]}
    for shell, files in shells.items():
        where = {}
        for p in files:
            for i, ln in enumerate(_src(p).splitlines(), 1):   # not code_only(): it drops lines, and the message names lines
                if ln.lstrip().startswith("#"):
                    continue
                for m in _FN_DEF.finditer(re.sub(r"\s{2,}#.*$", "", ln)):
                    where.setdefault(m.group(1), []).append("%s:%d" % (p.name, i))
        twice = {f: w for f, w in where.items() if len(w) > 1}
        assert len(where) >= 5, "%s: the definition pattern found only %d functions; it has stopped matching" % (shell, len(where))
        assert not twice, "%s defines these names more than once (the later file wins): %s" % (shell, twice)


def test_unit_base_run_is_the_hook_helper_after_the_whole_library_is_sourced():
    """3.7.2: the §4 hook helper survives every lib/ file being sourced: it runs its command, or under --check prints it."""
    r = _bash('declare -F base_install >/dev/null || { echo "no base_install"; exit 1; }; '
              'case "$(declare -f base_run)" in *base_init*) echo "base_run is the pipeline"; exit 1 ;; esac; '
              'base_run echo ran-it; BASE_DRY=1 base_run echo skipped-it')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ran-it" in r.stdout and "would echo skipped-it" in r.stdout, r.stdout + r.stderr


def test_unit_every_trap_has_a_handler():
    src = code_only("\n".join(_src(p) for p in LIB))
    found = 0
    for m in re.finditer(r"trap\s+'?([^' ]+)", src):
        fn = m.group(1)
        if fn.startswith("_base_on_"):
            found += 1
            assert re.search(rf"^{re.escape(fn)}\(\)\s*\{{", src, re.M), fn
    assert found >= 2, "expected an ERR and an EXIT trap"


def test_unit_no_and_list_ends_a_loop_body():
    # `[ x ] && cmd` as the LAST statement of a loop body becomes the loop's exit status under errexit
    lines = "\n".join(_src(p) for p in LIB).splitlines()
    bad = []
    for i, ln in enumerate(lines):
        s = code_only(ln).strip()
        if re.match(r"^\[\[?.*\]\]?\s*&&", s) and not s.endswith(("|| true", "|| :")):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith("done"):
                bad.append((i + 1, s))
    assert not bad, bad


def test_unit_outbound_hosts_are_allowlisted():
    allow = {"huggingface.co", "github.com", "pypi.org", "pypi.nvidia.com", "download.pytorch.org",
             "developer.download.nvidia.com", "127.0.0.1", "localhost", "0.0.0.0"}   # 3.0.0: NVIDIA's CUDA repository (the driver)
    src = "\n".join(_src(p) for p in [*LIB, *sorted((BASE / "py").glob("*.py"))])
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", src))
    assert hosts <= allow, hosts - allow


def test_unit_nothing_is_ever_uploaded():
    src = code_only("\n".join(_src(p) for p in [*LIB, *sorted((BASE / "py").glob("*.py"))]))
    assert not re.search(r"curl [^\n]*(-X ?(POST|PUT|PATCH)|--data|-F )", src)
    assert not re.search(r"urlopen\([^\n]*data=|requests\.post", src)


def test_unit_pip_is_never_bare():
    # pip on PATH belongs to whatever interpreter start.sh did not use. Only two forms name theirs:
    # `uv pip install --python "$PY"` and `"$PY" -m pip install`.
    src = code_only("\n".join(_src(p) for p in LIB))
    for m in re.finditer(r"\bpip3? install", src):
        before = src[max(0, m.start() - 3):m.start()]
        assert before.endswith("uv ") or before.endswith("-m "), src[max(0, m.start() - 60):m.end()]


def test_unit_fallbacks_are_loud():
    # every echo/return of a *_FALLBACK value is preceded by a '!!' line to stderr
    lines = "\n".join(_src(p) for p in LIB).splitlines()
    for i, ln in enumerate(lines):
        if "_FALLBACK" in ln and ("echo" in ln or "return" in ln) and "!!" not in ln:
            assert "!!" in lines[i - 1] and ">&2" in lines[i - 1], (i + 1, ln)


# ---------------------------------------------------------------- T2: discovery and banner

def test_unit_discover_finds_comfyui_by_main_py_not_models(tmp_path):
    (tmp_path / "ComfyUI" / "models").mkdir(parents=True)          # a stray tree: models/ but no main.py
    r = _bash("base_env_setup; base_discover quiet; echo rc=$?", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "rc=4" in r.stdout, r.stdout + r.stderr          # 4 = the volume holds no tree (3 = no network volume at all)
    (tmp_path / "ComfyUI" / "main.py").write_text("")
    (tmp_path / "ComfyUI" / "comfyui_version.py").write_text('__version__ = "0.34.7"\n')
    r = _bash("base_env_setup; base_discover quiet; echo COMFY=$COMFY M=$M VENV=$VENV PORT=$PORT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"COMFY={tmp_path}/ComfyUI" in r.stdout and f"M={tmp_path}/ComfyUI/models" in r.stdout and "PORT=8188" in r.stdout, r.stdout + r.stderr


def test_unit_discover_honours_extra_model_paths_both_shapes(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    lib = tmp_path / "vol" / "library"; (lib / "diffusion_models").mkdir(parents=True)
    (c / "extra_model_paths.yaml").write_text(f"comfyui:\n  base_path: {lib}\n")
    r = _bash("base_env_setup; base_discover quiet; echo M=$M", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"M={lib}" in r.stdout                                   # base_path holds category dirs → it IS the library
    parent = tmp_path / "vol2"; (parent / "models" / "loras").mkdir(parents=True)
    (c / "extra_model_paths.yaml").write_text(f"comfyui:\n  base_path: {parent}\n")
    r = _bash("base_env_setup; base_discover quiet; echo M=$M", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"M={parent}/models" in r.stdout                        # else base_path/models


def test_unit_discover_reads_port_from_args_file_and_never_adopts_a_backup_venv(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 8288\n--fast\n")
    for v in (".venv-cu130.pre-comfy-base-20260904", ".venv-cu130"):
        (c / v / "bin").mkdir(parents=True); (c / v / "bin" / "python").write_text("#!/bin/sh\n"); os.chmod(c / v / "bin" / "python", 0o755)
    r = _bash("base_env_setup; base_discover quiet; echo VENV=$VENV PORT=$PORT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"VENV={c}/.venv-cu130" in r.stdout and "PORT=8288" in r.stdout, r.stdout + r.stderr


def test_unit_comfy_version_reads_both_sources_and_is_loud_otherwise(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    (d / "pyproject.toml").write_text('[project]\nname = "ComfyUI"\nversion = "0.35.1"\n')
    assert "0.35.1" in _bash(f'_base_comfy_version "{d}"').stdout
    (d / "comfyui_version.py").write_text('__version__ = "0.34.9"\n')
    assert "0.34.9" in _bash(f'_base_comfy_version "{d}"').stdout
    r = _bash(f'_base_comfy_version "{tmp_path}"')
    assert r.stdout.strip() == "0.0.0" and "!!" in r.stderr


def test_unit_discover_refuses_an_outside_comfy_dir_in_fake_mode(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    other = tmp_path.parent / (tmp_path.name + "-elsewhere") / "ComfyUI"; other.mkdir(parents=True); (other / "main.py").write_text("")
    r = _bash("base_env_setup; base_discover quiet; echo COMFY=$COMFY", env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_DIR": str(other)})
    assert f"COMFY={c}" in r.stdout   # a fake run can never address a real tree


def test_unit_banner_runs_on_a_fake_pod_and_names_the_pool_honestly(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    r = _bash("PKG_NAME=T; PKG_VERSION=1; base_env_setup; base_discover quiet; base_banner", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 0 and "ComfyUI" in r.stdout and "models" in r.stdout and "venv" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "10-discover.sh").read_text())
    assert "shared pool" in src and "_base_fs_is_pool" in src


def test_unit_gpu_fit_names_b200_and_b300_and_judges_every_other_gpu_below(tmp_path):
    """3.8.0: GPU_PREFER="B200 B300" is the video packages' declaration, and py/gpu_fit.py the one rule behind it: a GPU
    of a preferred architecture fits, a newer one with the memory fits, everything else is BELOW (a warning, never a
    stop). The numbers are nvidia-smi's own: B200 reads 183359 MiB, an H200 143771, an RTX PRO 6000 97887. A MIG slice
    of a B300, which RunPod sells as its own type, is never a B300."""
    sys.path.insert(0, str(BASE / "py")); import gpu_fit
    want = ["B200", "B300"]
    assert gpu_fit.fits(want, "10.0", 183359) == "ok" and gpu_fit.fits(want, "10.3", 275040) == "ok"
    assert gpu_fit.fits(want, "9.0", 143771) == "below"            # H200: Hopper, 141 GB
    assert gpu_fit.fits(want, "12.0", 97887) == "below"            # RTX PRO 6000 Blackwell: the right decade, half the memory
    assert gpu_fit.fits(want, "11.0", 200000) == "ok"              # a newer chip with the memory, before the table names it
    assert gpu_fit.fits(want, "12.0", 120000) == "below"
    assert gpu_fit.fits(want, None, None) == "unknown"
    assert gpu_fit.fits([], "8.9", 24000) == "ok"                   # no preference, nothing to fall short of
    # a host's type id, read on the Mac with no numbers: the name decides
    assert gpu_fit.class_of_name("NVIDIA B200") == "B200" and gpu_fit.class_of_name("NVIDIA B300 SXM6 AC") == "B300"
    assert gpu_fit.class_of_name("NVIDIA B300 SXM6 AC MIG 1g.34gb") is None and gpu_fit.class_of_name("NVIDIA H200") is None
    assert gpu_fit.fits(want, None, None, "NVIDIA B300 SXM6 AC") == "ok" and gpu_fit.fits(want, None, None, "NVIDIA H100 80GB HBM3") == "below"
    assert gpu_fit.want_text(want) == "B200 (sm_100, 180 GB) or B300 (sm_103, 288 GB)"
    assert gpu_fit.unknown_classes(["B200", "B2OO"]) == ["B2OO"]
    r = subprocess.run([sys.executable, str(BASE / "py" / "gpu_fit.py"), "--prefer", "B200 B300", "--name", "NVIDIA H200", "--cc", "9.0", "--mib", "143771"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.split("\t")[:4] == ["fit=below", "gpu=NVIDIA H200", "sm=sm_90", "gb=151"], r.stdout
    assert r.stdout.rstrip("\n").split("\t")[4] == "want=B200 (sm_100, 180 GB) or B300 (sm_103, 288 GB)", r.stdout
    r = subprocess.run([sys.executable, str(BASE / "py" / "gpu_fit.py"), "--check", "B200 H9000"], capture_output=True, text=True)
    assert r.returncode == 1 and "H9000" in r.stdout


def test_unit_a_gpu_below_gpu_prefer_is_a_warning_in_the_banner_and_a_typo_is_refused(tmp_path):
    """The pod's own run (preflight and the real run share base_init → base_banner) says, once, whether this GPU is the one
    the package is tuned for: a warning that lands in the SUMMARY (podctl prints it), never a failure. An unknown class
    in GPU_PREFER is an invalid declaration: a typo must not silence the warning on every pod."""
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    run = "PKG_NAME=T; PKG_VERSION=1; GPU_PREFER='B200 B300'; base_env_setup; base_discover quiet; base_banner; echo W=${#BASE_WARN[@]}; printf '%s\n' \"${BASE_WARN[@]}\""
    r = _bash(run, env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_GPU": "NVIDIA RTX PRO 6000 Blackwell Server Edition|12.0|97887"})
    assert r.returncode == 0 and "W=1" in r.stdout, r.stdout + r.stderr
    assert "below what T is tuned for: B200 (sm_100, 180 GB) or B300 (sm_103, 288 GB)" in r.stdout and "(103 GB, sm_120)" in r.stdout, r.stdout
    r = _bash(run, env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_GPU": "NVIDIA B200|10.0|183359"})
    assert r.returncode == 0 and "W=0" in r.stdout and "NVIDIA B200 is what T is tuned for" in r.stdout, r.stdout + r.stderr
    r = _bash("PKG_NAME=T; PKG_VERSION=1; base_env_setup; base_discover quiet; base_banner; echo W=${#BASE_WARN[@]}",
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_GPU": "NVIDIA RTX 4090|8.9|24564"})
    assert "W=0" in r.stdout and "gpu fit" not in r.stdout, "a package that declares no GPU_PREFER is never judged"
    r = _bash("PKG_ID=t; PKG_NAME=T; PKG_VERSION=1; BASE_MIN=0; COMFY_MIN=0; GPU_PREFER='B200 B3OO'; base_env_setup; _base_validate_tables; echo RC=$?")
    assert "RC=1" in r.stdout and "GPU_PREFER: unknown GPU class B3OO" in r.stderr, r.stdout + r.stderr
    r = _bash("PKG_ID=t; PKG_NAME=T; PKG_VERSION=1; BASE_MIN=0; COMFY_MIN=0; GPU_PREFER='B200 B300'; base_env_setup; _base_validate_tables; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- T3: tokens, ComfyUI update, version gate

def _fake_comfy_repo(tmp_path):
    """A git repo shaped like ComfyUI with tags v0.34.0 and v0.35.0 and a bare remote."""
    origin = tmp_path / "origin.git"; work = tmp_path / "ComfyUI"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, stderr=subprocess.DEVNULL)
    def commit(ver):
        (work / "main.py").write_text(""); (work / "comfyui_version.py").write_text(f'__version__ = "{ver}"\n')
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", ver], check=True)
        subprocess.run(["git", "-C", str(work), "tag", f"v{ver}"], check=True)
    commit("0.34.0"); commit("0.35.0")
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:master", "--tags"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "v0.34.0"], check=True)
    return work


def test_unit_update_checks_out_newest_tag_on_the_base_branch(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW; git -C "$COMFY" rev-parse --abbrev-ref HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "OLD=0.34.0 NEW=0.35.0" in r.stdout and "comfy-base-stable" in r.stdout, r.stdout + r.stderr


def test_unit_check_mode_gates_on_the_post_update_version_without_touching_the_tree(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    r = _bash("COMFY_MIN=0.35.0; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate; echo GATE=$?", env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert "GATE=0" in r.stdout, r.stdout + r.stderr
    assert before == subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout


def test_unit_gate_fails_below_the_floor(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    r = _bash("COMFY_MIN=9.9.9; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate || echo GATE=$?; echo FAILED=${#BASE_FAILED[@]}", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "GATE=1" in r.stdout and "FAILED=1" in r.stdout, r.stdout + r.stderr


def test_unit_tokens_are_saved_once_with_tight_permissions(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; ls -l "$BASE_STATE/tokens.env"; cat "$BASE_STATE/tokens.env"', env={"BASE_FAKE_ROOT": str(tmp_path), "HF_TOKEN": "hf_abc"})
    assert "-rw-------" in r.stdout and "HF_TOKEN=hf_abc" in r.stdout, r.stdout + r.stderr
    # the value is compared, never echoed: on the pod this test once printed the real token into the run log (2026-09-05)
    r2 = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; grep -c HF_TOKEN "$BASE_STATE/tokens.env"; [ "$HF_TOKEN" = hf_abc ] && echo T=match', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r2.stdout.strip().split("\n")[-2].endswith("1") and "T=match" in r2.stdout and "hf_abc" not in r2.stdout.split("tokens.env")[-1]   # not re-asked, not duplicated, re-loaded


# ---------------------------------------------------------------- T4: venv — picks, owner, probe, build, verify, rescue

def test_unit_torch_pick_has_no_soak_and_no_fallback():
    src = (BASE / "py" / "torch_pick.py").read_text()
    assert "AGE" not in src and "days" not in src and "cp" in src
    assert "sys.exit(1)" in src                                              # fetch failure is fatal, not a stale pick
    assert "pypi.org/pypi/torch/json" not in src


def test_unit_python_pick_resolves_pack_requirements_too():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    body = src[src.index("_base_python_pick()"):src.index("_base_torch_pick()")]
    # 3.0.0: the derived set (every upstream pin lifted) and uv's own torch backend, never a hard-coded CUDA index
    assert "uv pip compile" in body and "_base_reqlift" in body and "derived.txt" in body and "--torch-backend" in body
    assert "whl/cu1" not in body
    assert "FALLBACK" not in body and "BASE_FAILED" in body                   # no fallback Python


def test_unit_venv_interpreter_lives_on_the_volume():
    src = code_only((BASE / "lib" / "00-env.sh").read_text())
    assert "UV_MANAGED_PYTHON=1" in src
    line = [ln for ln in src.splitlines() if "export UV_PYTHON_INSTALL_DIR=" in ln][0]
    assert "/.cache/" not in line and "BASE_HOME" in line


def test_unit_persistence_predicate_knows_what_a_pod_restores():
    # truth table on the real bash function with the root forced
    cases = {"/workspace/comfy-base/python/cpython-3.14/bin/python3": 0, "/workspace/pkg/python/x/bin/python3": 0,
             "/usr/bin/python3.12": 0, "/opt/conda/bin/python3": 0,
             "/root/.local/share/uv/python/cpython-3.13/bin/python3": 1, "/tmp/py/bin/python3": 1, "/home/u/.local/x/python3": 1}
    for path, want in cases.items():
        r = _bash(f'HOME=/root; _base_path_is_persistent "{path}"; echo rc=$?', env={"BASE_PERSIST_ROOT_FORCE": "/workspace"})
        assert f"rc={want}" in r.stdout, (path, r.stdout, r.stderr)
    r = _bash('_base_path_is_persistent /tmp/x; echo rc=$?')                 # off the pod: nothing to prove
    assert "rc=0" in r.stdout


def test_unit_venv_owner_recognises_every_stamp(tmp_path):
    v = tmp_path / ".venv"; v.mkdir()
    assert "none" in _bash(f'VENV="{v}"; _base_venv_owner').stdout
    (v / ".comfy-base-venv").write_text("x"); assert "base" in _bash(f'VENV="{v}"; _base_venv_owner').stdout


def test_unit_probe_condemns_a_runtime_written_interpreter():
    src = (BASE / "py" / "venv_probe.py").read_text()
    assert "pyvenv.cfg" in src and "_base_executable" in src and "/root" in src and "/tmp" in src and "hard.append" in src
    assert "sm_120" not in code_only(src) and "BASE_WANT_SM" in src         # device sm is a parameter, not a constant


def test_unit_verify_asks_for_a_gpu_except_in_a_seed_build(tmp_path):
    """3.9.1: the template image's seed build (BASE_SEED=1) runs where no GPU is, and "torch.cuda.is_available() is False"
    rolled its venv back every time, so no image was ever built. BASE_NO_GPU=1 drops that one question; the install sets
    it for BASE_SEED=1 alone, so a real machine with no visible device still fails."""
    fake = tmp_path / "fake"; (fake / "torch").mkdir(parents=True)
    (fake / "torch" / "__init__.py").write_text(
        "__version__ = '2.14.1+cu130'\n"
        "class version:\n    cuda = '13.0'\n"
        "class cuda:\n    @staticmethod\n    def is_available():\n        return False\n"
        "    @staticmethod\n    def get_arch_list():\n        return ['sm_100', 'sm_120']\n")
    env = {**os.environ, "VENV": str(fake), "PYTHONPATH": str(fake)}
    env.pop("BASE_PERSIST_ROOT", None)
    run = lambda **e: subprocess.run([sys.executable, str(BASE / "py" / "venv_verify.py")], capture_output=True, text=True, env={**env, **e})
    r = run()
    assert r.returncode == 1 and "is_available() is False" in r.stdout, r.stdout + r.stderr
    r = run(BASE_NO_GPU="1")
    assert r.returncode == 0 and "is_available" not in r.stdout and "no GPU asked (seed build)" in r.stdout, r.stdout + r.stderr
    py = tmp_path / "py"; py.write_text('#!/bin/sh\necho "BASE_NO_GPU=$BASE_NO_GPU"\n'); py.chmod(0o755)
    assert "BASE_NO_GPU=1" in _bash(f'PY="{py}"; BASE_SEED=1; _base_venv_verify; echo "$BASE_TORCH_INFO"').stdout
    assert "BASE_NO_GPU=0" in _bash(f'PY="{py}"; unset BASE_SEED; _base_venv_verify; echo "$BASE_TORCH_INFO"').stdout


def test_unit_import_check_runs_on_cpu_in_a_seed_build_only(tmp_path):
    """3.10.0: the seed build's import check ran `main.py --quick-test-for-ci` where no GPU is and ComfyUI died "Found no
    NVIDIA driver" before importing a pack (runpod-template image job, 2026-10-16). Both the full and the scoped check
    pass --cpu for BASE_SEED=1 alone; a real machine's check still starts on its GPU."""
    comfy = tmp_path / "comfy"; comfy.mkdir()
    py = tmp_path / "py"; py.write_text('#!/bin/sh\necho "ARGS $*"\n'); py.chmod(0o755)
    log = tmp_path / "check.log"
    def args(seed, scoped):
        call = ('_base_import_scope(){ echo packA; }; _base_quick_test_scoped' if scoped else '_base_quick_test') + f' "{log}"'
        r = _bash(f'COMFY="{comfy}"; PY="{py}"; {"BASE_SEED=1" if seed else "unset BASE_SEED"}; {call}')
        assert r.returncode == 0, r.stdout + r.stderr
        return [ln for ln in log.read_text().splitlines() if ln.startswith("ARGS")]
    for scoped in (False, True):
        seeded, real = args(True, scoped), args(False, scoped)
        assert seeded and all("--quick-test-for-ci" in ln and " --cpu" in ln for ln in seeded), (scoped, seeded)
        assert real and not any("--cpu" in ln for ln in real), (scoped, real)
    assert "--whitelist-custom-nodes packA" in args(True, True)[0]


def test_unit_verify_checks_the_whole_startup_closure_and_extra_imports():
    src = (BASE / "py" / "venv_verify.py").read_text()
    for needle in ("_base_executable", "pyvenv.cfg", ".pth", "sys.path", "BASE_EXTRA_IMPORTS", "BASE_WANT_SM", "BASE_WANT_PY"):
        assert needle in src, needle


def test_unit_fake_discovery_never_leaves_the_fake_root(tmp_path):
    """On a community template image with its own /opt/venv, A fake pod's discovery (BASE_FAKE_ROOT) still
    walked the real container-disk candidates (/venv, /opt/venv): the dry run's suite wrote a fake stamp into the image's
    venv, and the real run's suite then saw "reused" where "fake" was expected (2026-09-06, the first base step on the new
    pod). Container-disk paths are consulted only through BASE_FAKE_IMAGE_ROOT in fake mode — never the real ones."""
    c = tmp_path / "pod" / "ComfyUI"; c.mkdir(parents=True); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    img = tmp_path / "img"; (img / "opt" / "venv" / "bin").mkdir(parents=True); (img / "opt" / "venv" / "bin" / "python").write_text("#!/bin/bash\nexit 0\n")
    (img / "opt" / "venv" / "bin" / "python").chmod(0o755)
    r = _bash('base_env_setup; base_discover quiet; echo "VENV=$VENV"', env={"BASE_FAKE_ROOT": str(tmp_path / "pod"), "BASE_NO_NET": "1", "BASE_FAKE_IMAGE_ROOT": str(img)})
    assert f"VENV={img}/opt/venv" in r.stdout, "the fake image's /opt/venv must be found THROUGH the image root: " + r.stdout + r.stderr
    r = _bash('base_env_setup; base_discover quiet; echo "VENV=$VENV"', env={"BASE_FAKE_ROOT": str(tmp_path / "pod"), "BASE_NO_NET": "1"})
    venv = [l for l in r.stdout.splitlines() if l.startswith("VENV=")][-1][5:]
    assert venv.startswith(str(tmp_path / "pod")), "a fake pod's venv lies outside the fake root: " + venv


def test_unit_fake_venv_path_stamps_and_reports(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    r = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT; cat "$VENV/.comfy-base-venv"', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "RESULT=fake" in r.stdout and "python=" in r.stdout and "by=t" in r.stdout, r.stdout + r.stderr
    r2 = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "RESULT=reused" in r2.stdout, r2.stdout + r2.stderr


def test_unit_rescue_never_runs_the_interpreter_it_repairs():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    body = src[src.index("base_rescue()"):]
    assert '"$PY"' not in body and "$PY " not in body


def test_unit_a_pre_backup_is_never_adopted_and_stale_backups_drop_only_at_swap():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    swap = src[src.index("_base_venv_swap_aside()"):src.index("_base_venv_verify()")]
    assert "_base_drop_stale_venv_backups" in swap
    assert "_base_drop_stale_venv_backups" not in src[:src.index("_base_venv_swap_aside()")].replace("_base_drop_stale_venv_backups()", "")
    build = src[src.index("_base_venv_build()"):src.index("base_venv()")]
    assert build.index("_base_venv_swap_aside") < build.index('--seed "$VENV"')


def test_unit_every_venv_change_flags_a_restart():
    src = code_only("\n".join(p.read_text() for p in LIB))
    assigned = set(re.findall(r'BASE_VENV_RESULT="?(built|rebuilt|adopted)\b', src))
    assert assigned >= {"built", "rebuilt", "adopted"}, assigned
    if "base_restart()" in src:
        body = src[src.index("base_restart()"):]
        m = re.search(r'case "\$BASE_VENV_RESULT" in\s*([^)]+)\)', body)
        assert m and {"built", "rebuilt", "adopted"} <= set(m.group(1).split("|")), m.group(1) if m else "no case"


# ---------------------------------------------------------------- T5: node packs

def _fake_pack_repo(tmp_path, name, n_commits=2):
    src = tmp_path / "remotes" / name; src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    shas = []
    for i in range(n_commits):
        (src / "nodes.py").write_text(f"# {i}\nimport numpy\n"); (src / "requirements.txt").write_text("numpy\n")
        subprocess.run(["git", "-C", str(src), "add", "."], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", str(i)], check=True)
        shas.append(subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    return src, shas


def test_unit_packs_go_to_their_head_and_the_records_are_printed_every_run(tmp_path):
    """3.0.0: a row's sha is the last-tested record, never a target. Every run clones or moves the pack to its remote's
    HEAD and prints the record rows, so podctl can write back what a green install measured."""
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    src, shas = _fake_pack_repo(tmp_path, "FakePack")
    row = f"FakePack|{src}|{shas[0]}||test pack"
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); base_env_setup; base_discover quiet; base_packs git; git -C "$CN/FakePack" rev-parse HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r.stdout.strip().endswith(shas[1]), r.stdout + r.stderr
    assert f'"FakePack|{src}|{shas[1]}||test pack"' in r.stdout, r.stdout


def test_unit_a_pack_that_cannot_reach_its_head_is_a_failure_not_a_warning(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "custom_nodes").mkdir(parents=True); (c / "main.py").write_text("")
    src, shas = _fake_pack_repo(tmp_path, "FakePack")
    subprocess.run(["git", "clone", "-q", str(src), str(c / "custom_nodes" / "FakePack")], check=True)
    gone = tmp_path / "gone.git"
    subprocess.run(["git", "-C", str(c / "custom_nodes" / "FakePack"), "remote", "set-url", "origin", str(gone)], check=True)
    row = f"FakePack|{gone}|{'0' * 40}||test pack"
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); base_env_setup; base_discover quiet; base_packs git || true; echo FAILED=${{#BASE_FAILED[@]}}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "FAILED=1" in r.stdout and "not at its newest" in r.stdout, r.stdout + r.stderr


def test_unit_a_package_may_not_redeclare_a_base_pack():
    r = _bash('PACKS=("rgthree-comfy|https://github.com/rgthree/rgthree-comfy|' + "a" * 40 + '||dup"); _base_pack_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "rgthree-comfy" in r.stderr


def test_unit_pack_rows_require_a_40_hex_sha():
    r = _bash('PACKS=("X|https://github.com/x/X|main||why"); _base_pack_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout


def test_unit_base_packs_are_the_shared_six_and_pinned():
    r = _bash('printf "%s\\n" "${BASE_PACKS[@]}"')
    dirs = [ln.split("|")[0] for ln in r.stdout.splitlines() if ln]
    assert dirs == ["rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"], dirs
    for ln in r.stdout.splitlines():
        assert re.fullmatch(r"[0-9a-f]{40}", ln.split("|")[2]), ln


def test_unit_basetest_base_packs_matches_the_table():
    """A converted suite reads the base's packs through basetest, not by re-parsing 40-packs.sh itself."""
    from basetest import base_packs
    rows = base_packs()
    assert {p["dir"] for p in rows} == {"rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"}
    assert all(re.fullmatch(r"[0-9a-f]{40}", p["sha"]) for p in rows), rows
    assert all(p["url"].startswith("https://github.com/") for p in rows)


def test_unit_list_packs_merges_and_dedupes_across_package_dirs(tmp_path):
    for name, sha in (("A", "1" * 40), ("B", "1" * 40)):
        d = tmp_path / f"{name} Pkg"; d.mkdir()
        (d / f"{name} Pkg-script.sh").write_text(
            f'PKG_ID={name.lower()}; PKG_NAME="{name}"; PKG_VERSION=1; BASE_MIN=1.0.0; WF_NAME=w.json; COMFY_MIN=0.34.0\n'
            f'PACKS=("Shared|https://github.com/x/Shared|{sha}||s")\nMODELS=()\nsource "{BASE}/base.sh"; base_main "$@"\n')
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(tmp_path / "A Pkg"), str(tmp_path / "B Pkg")], capture_output=True, text=True)
    assert r.returncode == 0 and sum(1 for ln in r.stdout.splitlines() if ln.startswith("Shared|")) == 1 and "rgthree-comfy|" in r.stdout, r.stdout + r.stderr
    s = tmp_path / "B Pkg" / "B Pkg-script.sh"; s.write_text(s.read_text().replace("1" * 40, "2" * 40))
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(tmp_path / "A Pkg"), str(tmp_path / "B Pkg")], capture_output=True, text=True)
    assert r.returncode == 0 and "Shared: records differ" in r.stderr, r.stdout + r.stderr       # 3.0.0: records, not pins
    s.write_text(s.read_text().replace("github.com/x/Shared", "github.com/y/Shared"))
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(tmp_path / "A Pkg"), str(tmp_path / "B Pkg")], capture_output=True, text=True)
    assert r.returncode == 1 and "Shared comes from two URLs" in r.stderr, r.stdout + r.stderr


def test_unit_all_reqfiles_covers_comfyui_and_every_located_pack(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "custom_nodes" / "P").mkdir(parents=True); (c / "main.py").write_text("")
    (c / "requirements.txt").write_text(""); (c / "custom_nodes" / "P" / "requirements.txt").write_text("x\n")
    r = _bash('base_env_setup; base_discover quiet; PACK_DIRS=("P|$CN/P"); _base_all_reqfiles', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"{c}/requirements.txt" in r.stdout and f"{c}/custom_nodes/P/requirements.txt" in r.stdout


# ---------------------------------------------------------------- T6: models — rows, index, download, move, disk gate, prune

ROW_A = "diffusion_models|Example|Turbo|a.safetensors|https://huggingface.co/x/y/resolve/main/a.safetensors|1048576|A|"
ROW_LOCAL = "loras|SDXL|Detailers|mine.safetensors|LOCAL|0|private lora|"
ROW_BAD_FAMILY = "vae|No pe||v.safetensors|https://huggingface.co/x/y/resolve/main/v.safetensors|10|bad|"   # a space: not a folder name


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text(""); return c


def test_unit_model_rows_validate_family_bytes_and_local():
    assert "rc=0" in _bash(f'MODELS=("{ROW_A}" "{ROW_LOCAL}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    r = _bash(f'MODELS=("{ROW_BAD_FAMILY}"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout and "No pe" in r.stderr
    r = _bash('MODELS=("vae|Example||v.safetensors|https://huggingface.co/x/y/resolve/main/v.safetensors|0|zero|"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout
    r = _bash('MODELS=("vae|Example||v.safetensors|https://evil.example/v.safetensors|5|host|"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout
    assert "diffusion_models/Example/Turbo/a.safetensors" in _bash(f'_base_dest_rel "{ROW_A}"').stdout
    assert "LLM/Qwen2.5-Omni/x.gguf" in _bash('_base_dest_rel "LLM||Qwen2.5-Omni|x.gguf|https://huggingface.co/a/b/resolve/main/x.gguf|5|n|"').stdout
    assert "vae_approx/taeh3.safetensors" in _bash('_base_dest_rel "vae_approx|||taeh3.safetensors|https://huggingface.co/a/b/resolve/main/taeh3.safetensors|5|flat|"').stdout


def test_unit_fake_run_moves_a_stray_and_fake_downloads_the_rest(tmp_path):
    c = _pod(tmp_path); stray = tmp_path / "old" / "a.safetensors"; stray.parent.mkdir(); stray.write_bytes(b"\0" * 1048576)
    row_b = "vae|Example|Real|b.safetensors|https://huggingface.co/x/y/resolve/main/b.safetensors|4096|B|"
    r = _bash(f'MODELS=("{ROW_A}" "{row_b}"); base_env_setup; base_discover quiet; base_models; echo MOVED=${{#MODEL_MOVED[@]}} DL=${{#MODEL_DL[@]}}; ls "$M/diffusion_models/Example/Turbo" "$M/vae/Example/Real"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "MOVED=1 DL=1" in r.stdout and "a.safetensors" in r.stdout and "b.safetensors" in r.stdout and not stray.exists(), r.stdout + r.stderr
    assert (c / "models" / "vae" / "Example" / "Real" / "b.safetensors").stat().st_size == 4096


def test_unit_a_missing_local_file_fails_the_run_by_name(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; printf "%s\\n" "${{BASE_FAILED[@]}}"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=1" in r.stdout and "mine.safetensors" in r.stdout and "loras/SDXL/Detailers" in r.stdout, r.stdout + r.stderr


def test_unit_a_found_local_file_is_relocated_any_size(tmp_path):
    c = _pod(tmp_path); (tmp_path / "somewhere").mkdir(); (tmp_path / "somewhere" / "mine.safetensors").write_bytes(b"xyz")
    r = _bash(f'MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; ls "$M/loras/SDXL/Detailers"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0" in r.stdout and "mine.safetensors" in r.stdout, r.stdout + r.stderr


def test_unit_a_dry_run_cannot_judge_a_local_row_a_hook_will_place_but_still_gates_one_nothing_will(tmp_path):
    """A LOCAL row is placed by the package's own pkg_pre_models, which does nothing under BASE_DRY, so at --check
    time the file is absent BY DESIGN and its absence carries no information. Before this guard, `podctl install
    --pkg` died on --check for every package with a LOCAL row: a gate on a condition the gate itself creates.
    But the relaxation is only honest where a hook exists, so all four cases live in one test and none of them
    can be deleted without the others."""
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    tail = f'MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}'
    hook = 'pkg_pre_models(){ :; }; '

    dry_hook = _bash(hook + tail, env={**env, "BASE_DRY": "1"})
    assert "FAILED=0" in dry_hook.stdout, "a hook may place it, so --check cannot judge it" + dry_hook.stdout + dry_hook.stderr
    assert "mine.safetensors" in dry_hook.stdout, "the report must still NAME the row it could not judge" + dry_hook.stdout

    dry_nohook = _bash(tail, env={**env, "BASE_DRY": "1"})
    assert "FAILED=1" in dry_nohook.stdout, \
        "with no pkg_pre_models nothing will ever place it, so --check must still say so" + dry_nohook.stdout + dry_nohook.stderr

    for label, extra in (("with a hook", hook), ("without one", "")):
        real = _bash(extra + tail, env=env)
        assert "FAILED=1" in real.stdout, f"a real run must still fail on a LOCAL row nothing placed ({label})" + real.stdout + real.stderr


def test_unit_a_dry_run_still_reports_a_local_file_that_is_present_as_ok(tmp_path):
    """The positive control, and the reason the guard above stays honest. If a careless edit ever widened it to
    every LOCAL row, the dry run would report each one as unjudgeable and stop being able to tell anyone
    anything. A LOCAL file that IS on disk is judgeable, so it is judged, hook or no hook."""
    c = _pod(tmp_path); (tmp_path / "somewhere").mkdir(); (tmp_path / "somewhere" / "mine.safetensors").write_bytes(b"xyz")
    r = _bash(f'pkg_pre_models(){{ :; }}; MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_DRY": "1"})
    assert "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "cannot judge" not in r.stdout, \
        "a LOCAL file that is present must be reported as found, never as unjudgeable: " + r.stdout


def test_unit_the_disk_gate_runs_before_anything_is_downloaded_and_admits_what_it_cannot_check(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} DL=${{#MODEL_DL[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "0"})
    assert "FAILED=1 DL=0" in r.stdout and "not enough space" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "50-models.sh").read_text()); gate = src[src.index("_base_disk_gate()"):src.index("base_models()")]
    assert gate.index("BASE_FAKE_FREE_GB") < gate.index("_base_fs_is_pool")


def test_unit_prune_offers_only_unclaimed_duplicates_and_needs_the_prompt(tmp_path):
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    (dest / "a.safetensors").write_bytes(b"\0" * 1048576); dup = tmp_path / "dup" / "a.safetensors"; dup.parent.mkdir(); dup.write_bytes(b"\0" * 1048576)
    sup = c / "models" / "diffusion_models" / "Example" / "old.safetensors"; sup.write_bytes(b"1")
    other = c / "models" / "diffusion_models" / "Example-V" / "old.safetensors"; other.parent.mkdir(parents=True); other.write_bytes(b"1")   # another family: never ours to offer
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env=env)
    assert "DEL=2" in r.stdout and dup.exists() and sup.exists(), r.stdout + r.stderr          # no TTY → No
    r = _bash(f'MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune', env={**env, "BASE_YES": "1"})
    assert not dup.exists() and not sup.exists() and other.exists() and (dest / "a.safetensors").exists(), r.stdout + r.stderr


def test_unit_a_models_symlink_pointing_outside_the_store_is_repointed(tmp_path):
    """MEASURED 2026-09-19 on a fresh boot: `/workspace/ComfyUI/models -> /mnt/comfy-library/models` with the
    target absent, so `df` failed, the disk gate read 0.00 GB free on a store with 272 GB free, and every install
    refused. ComfyUI's own models directory did not resolve either.

    `_base_library_link` wrote that symlink when a machine had a separate library mounted, it lives ON THE STORE
    so every later machine inherits it, and since 2.5.2 BASE_LIBRARY is correctly ignored under a shared root,
    which means the function that created it returns at its first line and can never repair it. A second mount of
    the same store hid the problem until the release that stopped making one."""
    c = _pod(tmp_path)
    (c / "models").rmdir() if (c / "models").is_dir() and not any((c / "models").iterdir()) else None
    import os
    import shutil
    if (c / "models").exists() and not (c / "models").is_symlink():
        shutil.rmtree(c / "models")
    # the real shape: a target OUTSIDE the volume, left by the 2.4.0 library mount that no longer exists
    os.symlink("/mnt/comfy-library/models", str(c / "models"))
    assert (c / "models").is_symlink() and not (c / "models").exists(), "the fixture must dangle"

    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path),
           "BASE_VOLUME_SHARED": "1"}
    r = _bash('base_env_setup; base_discover quiet; echo M=$M', env=env)
    assert (c / "models").is_symlink(), "it must stay a symlink, not become a directory"
    assert os.readlink(str(c / "models")).endswith("/models"), r.stdout + r.stderr
    assert (c / "models").exists(), "the repointed models/ must resolve: " + r.stdout + r.stderr
    assert "repointed" in r.stdout or "repointed" in r.stderr, r.stdout + r.stderr

    # a symlink ALREADY inside the store is left exactly alone
    before = os.readlink(str(c / "models"))
    r = _bash('base_env_setup; base_discover quiet', env=env)
    assert os.readlink(str(c / "models")) == before, "an already-correct link must not be rewritten"
    assert "repointed" not in r.stdout, "it must not report a change it did not make: " + r.stdout


def test_unit_prune_never_offers_one_file_reached_by_two_paths(tmp_path):
    """MEASURED on a live three-machine store, 2026-09-19: the sweep offered 63.52 GB of LIVE models for
    deletion (Krea 2 RAW, Turbo, the text encoder, the VAE and an adapter). The store was mounted at two
    points and `ComfyUI/models` symlinked into the second, so every file was indexed under two different
    path strings. The predicate is basename plus size, the identity check compared two path STRINGS, and
    the ledger interlock is scoped to paths under $M, so nothing between the arithmetic and `rm -f` could
    catch it. `rm -f` then unlinks the ONLY directory entry and the file is gone from both paths.

    A hardlink is the same defect reachable without mounting anything: one inode, two names, and
    unlinking either reclaims nothing. That is what this asserts, because a test may not mount."""
    import os
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    keeper = dest / "a.safetensors"; keeper.write_bytes(b"\0" * 1048576)
    second = tmp_path / "second-mount"; second.mkdir()
    link = second / "a.safetensors"; os.link(keeper, link)
    assert link.stat().st_ino == keeper.stat().st_ino, "the fixture must be one inode under two names"
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env=env)
    assert "DEL=0" in r.stdout, "one file under two names was offered for deletion: " + r.stdout + r.stderr
    # BASE_YES removes the prompt rather than answering it, so an unattended install must still not delete it
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; base_prune', env={**env, "BASE_YES": "1"})
    assert keeper.exists() and link.exists(), "an unattended install deleted a live model: " + r.stdout + r.stderr


def test_unit_prune_rolls_back_when_a_declared_model_would_disappear(tmp_path):
    """Every guard before the delete is an INFERENCE about identity, and 2.5.11 fixed one that was wrong after it
    had already offered 63.52 GB of live models. An inference can be wrong again in a shape nobody has met, so the
    delete itself is staged: rename aside, re-stat every destination the package declares, and only then remove.

    This drives the case the inference is supposed to prevent, by handing the sweep a candidate that IS the
    declared destination reached by a second name. If the guards were ever bypassed, staging catches it: the
    destination goes missing at verify time, every rename is undone, and the run fails rather than reporting a
    tidy-up it did not do."""
    import os
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    keeper = dest / "a.safetensors"; keeper.write_bytes(b"\0" * 1048576)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_YES": "1"}
    # DEL_FILES is forced to the keeper itself: the guards would never put it there, and that is the point.
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; '
              f'rows="$(_base_model_rows)"; DEL_FILES=("{keeper}"); DEL_BYTES=1048576; '
              f'_base_prune_commit "$rows"; echo RC=$?', env=env)
    assert "RC=1" in r.stdout, "a vanishing destination must fail the step: " + r.stdout + r.stderr
    assert "ROLLED BACK" in r.stdout or "ROLLED BACK" in r.stderr, r.stdout + r.stderr
    assert keeper.exists() and keeper.stat().st_size == 1048576, \
        "the model was not put back: " + r.stdout + r.stderr
    assert not (dest / "a.safetensors.comfy-base-pending").exists(), "a staged file was left behind"


def test_unit_prune_commits_when_every_declared_model_survives(tmp_path):
    """The other half: a genuine duplicate on a private volume is still removed, and the declared destination is
    untouched. Staging must not turn the feature off, only make it reversible."""
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    keeper = dest / "a.safetensors"; keeper.write_bytes(b"\0" * 1048576)
    dup = tmp_path / "dup" / "a.safetensors"; dup.parent.mkdir(); dup.write_bytes(b"\0" * 1048576)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_YES": "1"}
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; base_prune; echo RC=$?', env=env)
    assert not dup.exists(), "a real duplicate is still removed: " + r.stdout + r.stderr
    assert keeper.exists() and keeper.stat().st_size == 1048576, "the declared model was touched: " + r.stdout
    assert "re-checked first" in r.stdout, "the commit must say it verified: " + r.stdout
    assert not list(tmp_path.rglob("*.comfy-base-pending")), "a staged file was left behind"


def test_unit_prune_skips_the_duplicate_sweep_on_a_shared_store(tmp_path):
    """"Reclaimable" is not a property one machine can determine on a store several mount: this run sees
    none of the other machines' ledgers, and a file it calls surplus may be the only copy another is
    rendering from. The sweep is skipped, and says why."""
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    (dest / "a.safetensors").write_bytes(b"\0" * 1048576)
    dup = tmp_path / "dup" / "a.safetensors"; dup.parent.mkdir(); dup.write_bytes(b"\0" * 1048576)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env=env)
    assert "DEL=1" in r.stdout, "a real duplicate on a private volume is still offered: " + r.stdout + r.stderr
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}',
              env={**env, "BASE_VOLUME_SHARED": "1"})
    assert "DEL=0" in r.stdout, "the sweep ran on a shared store: " + r.stdout + r.stderr
    assert "shared store" in r.stdout, "it must say why it skipped: " + r.stdout
    assert dup.exists(), "the sweep touched a file on a shared store"


def test_unit_prune_warns_once_about_a_superseded_pattern_it_ignores(tmp_path):
    """SUPERSEDED matches basenames ONLY, ever: a pattern with a slash would widen the sweep across a store three
    machines share, and a trained LoRA is an output, not a row. The constraint stays; the SILENCE was the defect.
    A package inheriting such a row got no signal that its entry did nothing.
    The warning is hoisted out of the per-file loop, so it fires once per pattern and not once per indexed file."""
    c = _pod(tmp_path); d = c / "models" / "diffusion_models" / "Example"; d.mkdir(parents=True)
    for n in ("one.safetensors", "two.safetensors", "three.safetensors"): (d / n).write_bytes(b"1")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'MODELS=("{ROW_A}"); SUPERSEDED=(sub/dir/old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo WARN=${{#BASE_WARN[@]}}',
              env=env)
    assert "sub/dir/old.safetensors" in r.stdout, "the ignored pattern must be named: " + r.stdout + r.stderr
    assert "WARN=1" in r.stdout, "once per pattern, not once per indexed file: " + r.stdout + r.stderr


def test_unit_prune_never_deletes_a_symlink_or_uses_rm_rf_on_files():
    src = code_only((BASE / "lib" / "50-models.sh").read_text()); body = src[src.index("base_prune()"):]
    assert "-L" in body and "rm -rf" not in body.split("DEL_DIRS")[0]


# ---------------------------------------------------------------- T7: workflow path sync

def _wf(nodes, subgraphs=None):
    d = {"id": "x", "revision": 0, "last_node_id": 99, "last_link_id": 0, "nodes": nodes, "links": [], "groups": [], "version": 0.4,
         "extra": {"package_version": "1.0.0"}}
    if subgraphs: d["definitions"] = {"subgraphs": subgraphs}
    return d


def _node(i, t, wv, mode=0):
    return {"id": i, "type": t, "mode": mode, "pos": [0, 0], "size": [100, 50], "widgets_values": wv, "inputs": [], "outputs": []}


def _sync(wf, m, rows, cats, *extra):
    return subprocess.run(["python3", str(BASE / "py" / "sync_workflow.py"), str(wf), str(m), str(rows), str(cats), *extra], capture_output=True, text=True)


def test_unit_sync_rewrites_to_canonical_dests_normalises_backslashes_and_reports_bypassed(tmp_path):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["Example2\\example-2-9b.safetensors", "default"]),
                                    _node(2, "UnetLoaderGGUF", ["Example2\\example-2-9b-Q8_0.gguf"], mode=4),
                                    _node(3, "VAELoader", ["unknown_active.safetensors"])]), indent=2) + "\n")
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample.2\tVariant\texample-2-9b.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nUnetLoaderGGUF\tunet\nVAELoader\tvae\n")
    m = tmp_path / "models"; m.mkdir()
    r = _sync(wf, m, rows, cats)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(wf.read_text())
    assert out["nodes"][0]["widgets_values"][0] == "Example.2/Variant/example-2-9b.safetensors"
    assert out["nodes"][1]["widgets_values"][0] == "Example2\\example-2-9b-Q8_0.gguf"        # bypassed: reported, untouched
    assert "SYNC fixed=1 active_missing=1 bypassed_missing=1" in r.stdout, r.stdout
    assert "  ACTIVE " in r.stdout and "unknown_active.safetensors" in r.stdout and "  BYPASSED " in r.stdout
    assert (tmp_path / "w.json.bak").exists()
    assert wf.read_text().startswith('{\n  "id"')                                             # indent preserved (2)


def test_unit_sync_reaches_subgraph_definitions_instances_and_power_lora_rows(tmp_path):
    sg = {"id": "fc72fed4-0000-0000-0000-000000000000", "name": "Load", "nodes": [_node(171, "UNETLoader", ["example_engine_a_pruned_int8.safetensors", "default"])], "links": [], "inputs": [], "outputs": []}
    inst = _node(7, "fc72fed4-0000-0000-0000-000000000000", ["example_engine_a_pruned_int8.safetensors", "example_encoder_nvfp4.safetensors"])
    pl = _node(9, "Power Lora Loader (rgthree)", [{"type": "PowerLoraLoaderHeaderWidget"}, {"on": True, "lora": "Example 2\\Variant\\insta variant9b.safetensors", "strength": 1},
                                                  {"on": False, "lora": "Example 2\\Variant\\off_row.safetensors", "strength": 1}])
    wf = tmp_path / "w.json"; wf.write_text(json.dumps(_wf([inst, pl], [sg]), indent=1))
    rows = tmp_path / "rows.tsv"
    rows.write_text("diffusion_models\tExample-V\t\texample_engine_a_pruned_int8.safetensors\ntext_encoders\tExample-V\t\texample_encoder_nvfp4.safetensors\nloras\tExample.2\tVariant\tinsta variant9b.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nCLIPLoader\ttext_encoders\n")
    (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    out = json.loads(wf.read_text())
    assert out["definitions"]["subgraphs"][0]["nodes"][0]["widgets_values"][0] == "Example-V/example_engine_a_pruned_int8.safetensors"
    assert out["nodes"][0]["widgets_values"] == ["Example-V/example_engine_a_pruned_int8.safetensors", "Example-V/example_encoder_nvfp4.safetensors"]
    assert out["nodes"][1]["widgets_values"][1]["lora"] == "Example.2/Variant/insta variant9b.safetensors"
    assert out["nodes"][1]["widgets_values"][2]["lora"] == "Example 2\\Variant\\off_row.safetensors"                 # an OFF row is bypassed: reported, untouched
    assert "SYNC fixed=4 active_missing=0 bypassed_missing=1" in r.stdout, r.stdout
    assert wf.read_text().startswith('{\n "id"')                                              # indent preserved (1)


def test_unit_sync_dry_run_writes_nothing(tmp_path):
    wf = tmp_path / "w.json"; wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["Example2\\a.safetensors", "default"])])))
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample.2\tVariant\ta.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\n"); (tmp_path / "models").mkdir()
    before = wf.read_text(); r = _sync(wf, tmp_path / "models", rows, cats, "--dry")
    assert "SYNC fixed=1" in r.stdout and wf.read_text() == before and not (tmp_path / "w.json.bak").exists()


def test_unit_base_sync_fails_the_run_on_an_active_unresolved_loader_and_copies_the_workflow(tmp_path):
    c = _pod(tmp_path); (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "VAELoader", ["nowhere.safetensors"])])))
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "FAILED=1" in r.stdout and "nowhere.safetensors" in r.stdout, r.stdout + r.stderr
    assert (c / "user" / "default" / "workflows" / "W.json").exists()


def test_unit_loader_cats_cover_the_core_loaders_and_the_packages_known_ones():
    r = _bash('printf "%s\\n" "${BASE_LOADER_CATS[@]}"')
    have = {ln.split("|")[0] for ln in r.stdout.splitlines() if ln}
    for cls in ("UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "LoraLoader", "LoraLoaderModelOnly", "CheckpointLoaderSimple",
                "UpscaleModelLoader", "UnetLoaderGGUF", "ClipLoaderGGUF", "ModelPreviewOverrideKJ", "SAMLoader", "SAM_SmartInpainter"):
        assert cls in have, cls


# ---------------------------------------------------------------- T8: hygiene

def _base_args(tmp_path):
    return tmp_path / "comfy-base" / "state" / "comfyui_args.txt"


def test_unit_args_file_is_edited_by_flag_and_value_idempotently(tmp_path):
    """The base owns ComfyUI's flags in state/comfyui_args.txt. A template's runpod-slim/comfyui_args.txt is imported
    ONCE (its flags become the base's starting point) and is never edited or read again."""
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"
    a.write_text("--preview-method latent2rgb\n--use-sage-attention\n--fast\n")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash('base_env_setup; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}', env=env)
    b = _base_args(tmp_path); txt = b.read_text()
    assert "imported" in r.stdout and str(a) in r.stdout, r.stdout + r.stderr
    assert "--preview-method auto" in txt and "latent2rgb" not in txt and "--use-sage-attention" not in txt, txt + r.stdout + r.stderr
    assert "--fast" in txt and "--preview-size 1024" in txt and "--disable-api-nodes" in txt, txt
    assert a.read_text() == "--preview-method latent2rgb\n--use-sage-attention\n--fast\n"      # the template's file: untouched
    r2 = _bash('base_env_setup; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}', env=env)
    assert "CHANGED=0" in r2.stdout and b.read_text() == txt and "imported" not in r2.stdout, r2.stdout


def test_unit_sage_flag_survives_when_sageattention_imports(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--use-sage-attention\n")
    v = c / ".venv-cu130" / "bin"; v.mkdir(parents=True); py = v / "python"; py.write_text("#!/bin/sh\nexit 0\n"); os.chmod(py, 0o755)   # every import "succeeds"
    r = _bash('base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "--use-sage-attention" in _base_args(tmp_path).read_text(), r.stdout + r.stderr


def test_unit_check_mode_never_writes_the_args_file_or_manager_config(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--fast\n")
    ini = c / "user" / "default" / "ComfyUI-Manager" / "config.ini"; ini.parent.mkdir(parents=True); ini.write_text("[default]\nsecurity_level = strong\n")
    _bash('base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert a.read_text() == "--fast\n" and "strong" in ini.read_text() and not _base_args(tmp_path).exists()


def test_unit_manager_security_level_is_set_to_normal_and_package_hygiene_hook_runs(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("")
    ini = c / "user" / "default" / "ComfyUI-Manager" / "config.ini"; ini.parent.mkdir(parents=True); ini.write_text("[default]\nsecurity_level = strong\nnetwork_mode = public\n")
    r = _bash('pkg_hygiene(){ echo HOOK_RAN; }; HYGIENE_ARGS=("--fp8_e4m3fn-unet"); base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "security_level = normal" in ini.read_text() and "network_mode = public" in ini.read_text() and "HOOK_RAN" in r.stdout
    assert "--fp8_e4m3fn-unet" in _base_args(tmp_path).read_text()
    assert (tmp_path / "runpod-slim" / "comfyui_args.txt").read_text() == ""                  # the template's file: untouched


def test_unit_hygiene_marks_the_app_mode_tour_seen_and_keeps_every_other_setting(tmp_path):
    """3.17.1 (error log row 81): frontend 1.55.x opens a full-screen "Welcome to Apps" tour over the App Mode form on a
    first visit. Hygiene appends "appMode" to "Comfy.OnboardingCoachmarks.Seen" (ComfyUI_frontend v1.55.17's
    TOUR_SEEN_SETTING) in the settings file the server reads, keeping the list's items and every other key; a dry run
    writes nothing, a second run changes nothing, a corrupt file is left alone and named, and on a shared volume the file
    is the one under BASE_LOCAL_STATE (the server's --user-directory), never $COMFY/user."""
    import json
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("")
    f = c / "user" / "default" / "comfy.settings.json"; f.parent.mkdir(parents=True)
    orig = json.dumps({"Comfy.ColorPalette": "light", "Comfy.OnboardingCoachmarks.Seen": ["firstRun"], "pysssss.X": 3}, indent=4)
    f.write_text(orig)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    run = lambda extra="", **kw: _bash('%s base_env_setup; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}' % extra, env={**env, **kw})
    r = run(BASE_DRY="1")
    assert f.read_text() == orig and "would mark the App Mode tour seen" in r.stdout, r.stdout + r.stderr
    r = run(); got = json.loads(f.read_text())
    assert got == {"Comfy.ColorPalette": "light", "Comfy.OnboardingCoachmarks.Seen": ["firstRun", "appMode"], "pysssss.X": 3}, got
    assert "the App Mode tour marked seen" in r.stdout, r.stdout + r.stderr
    before = f.read_bytes(); r = run()
    assert f.read_bytes() == before and "CHANGED=0" in r.stdout and "tour is marked seen" in r.stdout, r.stdout + r.stderr
    f.write_text("{ half a file"); r = run()
    assert f.read_text() == "{ half a file" and "left untouched" in r.stdout and str(f) in r.stdout, r.stdout + r.stderr
    f.unlink(); run()
    assert json.loads(f.read_text()) == {"Comfy.OnboardingCoachmarks.Seen": ["appMode"]}
    f.unlink(); state = tmp_path / "local-state"
    r = run(BASE_VOLUME_SHARED="1", BASE_LOCAL_STATE=str(state))
    shared = state / "user" / "default" / "comfy.settings.json"
    assert json.loads(shared.read_text()) == {"Comfy.OnboardingCoachmarks.Seen": ["appMode"]} and not f.exists(), r.stdout + r.stderr


def test_unit_comfy_settings_merge_appends_and_never_breaks_the_list():
    """The merge does what the frontend's own markTourSeen does: the list kept in order, the missing entry appended; a
    value that is not a list (the frontend calls .includes on it) becomes the seed's list; other keys never move."""
    import comfy_settings as cs
    key = "Comfy.OnboardingCoachmarks.Seen"
    assert cs.SEED == {key: ["appMode"]}
    assert cs.merge({}) == ({key: ["appMode"]}, [key])
    assert cs.merge({key: ["firstRun"], "a": 1}) == ({key: ["firstRun", "appMode"], "a": 1}, [key])
    assert cs.merge({key: ["appMode", "firstRun"]}) == ({key: ["appMode", "firstRun"]}, [])
    assert cs.merge({key: "appMode"}) == ({key: ["appMode"]}, [key])


def test_unit_yaml_register_appends_once_and_respects_a_foreign_entry(tmp_path):
    y = tmp_path / "extra_model_paths.yaml"; y.write_text("comfyui:\n  base_path: /x\n")
    r = _bash(f'EXTRA_YAML="{y}"; _base_yaml_register pkg_a VHS_video_formats /workspace/pkg_a/video_formats; _base_yaml_register pkg_a VHS_video_formats /workspace/pkg_a/video_formats; echo CHANGED=${{#BASE_CHANGED[@]}}')
    assert y.read_text().count("VHS_video_formats") == 1 and "CHANGED=1" in r.stdout, y.read_text() + r.stdout
    r = _bash(f'EXTRA_YAML="{y}"; _base_yaml_register other VHS_video_formats /elsewhere; echo WARN=${{#BASE_WARN[@]}}')
    assert "/elsewhere" not in y.read_text() and "WARN=1" in r.stdout


# ---------------------------------------------------------------- T9: server — launch line, restart, import check, smoke, combos

def test_unit_never_restarts_comfyui_by_default():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_restart()"):]
    guard = body.index('"$BASE_RESTART" = "1"')
    for m in re.finditer(r"\b(kill|pkill)\b", body):
        assert m.start() > guard, "a kill before the BASE_RESTART guard"


def test_unit_restart_reasons_are_matched_by_pattern_not_one_string():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_restart()"):]
    m = re.search(r'case "\$BASE_VENV_RESULT" in\s*([^)]+)\)', body)
    assert m and {"built", "rebuilt", "adopted"} <= set(m.group(1).split("|")), body[:400]
    assert '= "built"' not in body


def test_unit_launch_line_defers_preview_flags_to_the_args_file(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--preview-method taesd\n")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r.stdout.count("--preview-method") == 1 and "taesd" in r.stdout and "--preview-size" in r.stdout, r.stdout
    assert "--listen 127.0.0.1" in r.stdout and "--enable-cors-header" in r.stdout      # 3.14.0: behind the key's front


def test_unit_a_dead_server_is_not_a_smoke_failure_and_check_mode_skips(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 8198\n")
    r = _bash('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; base_restart; base_smoke; base_combos; echo R=$BASE_RESTART_RESULT S=$BASE_SMOKE_RESULT C=$BASE_COMBO_RESULT FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "FAILED=0" in r.stdout and "not running" in r.stdout and "S=skipped" in r.stdout and "C=skipped" in r.stdout, r.stdout + r.stderr
    assert "nohup" in r.stdout and "main.py" in r.stdout                                           # the launch line is printed
    r = _bash('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; base_import_check; base_restart; echo I=$BASE_IMPORT_RESULT R=$BASE_RESTART_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert "I=skipped" in r.stdout and "R=skipped" in r.stdout


def test_unit_combos_never_rewrite_with_a_restart_pending():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_combos()"):]
    assert "BASE_RESTART_NEEDED" in body and "--check-only" in body


def test_unit_smoke_and_combo_tools_agree_on_frontend_only_types():
    """A first pod run failed smoke on GetNode, SetNode and LC Groups Bypasser: KJNodes' Get/Set and
    LC123's group bypasser live in the packs' web/js only and are never in /object_info — frontend-only, like rgthree's.

    2.0.33: this used to grep the two files for each name, which was a proxy for "the tool
    treats it as frontend-only" and stopped being true the moment the three copies of the set
    became one import. Assert the VALUE the tools actually use."""
    import smoke, combofix, canvas
    for t in ("Label (rgthree)", "Fast Groups Bypasser (rgthree)", "MarkdownNote", "PrimitiveNode",
              "GetNode", "SetNode", "LC Groups Bypasser", "LC Bypasser", "LC Bypasser Panel"):
        assert t in smoke.FRONTEND_ONLY, f"smoke does not treat {t!r} as frontend-only"
        assert t in combofix.FRONTEND_ONLY, f"combofix does not treat {t!r} as frontend-only"
    assert smoke.FRONTEND_ONLY is canvas.FRONTEND_ONLY_TYPES is combofix.FRONTEND_ONLY, \
        "three copies of this set drifted once already; there is one now"
    s = (BASE / "py" / "smoke.py").read_text(); c = (BASE / "py" / "combofix.py").read_text()
    assert "definitions" in s and "definitions" in c                                                 # both look inside subgraphs


# ---------------------------------------------------------------- T10: the ledger

def test_unit_ledger_records_packs_models_and_hooks_and_scopes_deletion(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; pkg_post_venv(){{ :; }}; MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_ledger_write; cat "$BASE_STATE/packages/a.manifest"', env=env)
    assert "pkg=a" in r.stdout and "model\tdiffusion_models/Example/Turbo/a.safetensors\t1048576" in r.stdout and "hooks=pkg_post_venv" in r.stdout and "superseded\told.safetensors" in r.stdout, r.stdout + r.stderr
    r = _bash('PKG_ID=b; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/a.safetensors; echo rc=$?', env=env); assert "rc=0" in r.stdout
    r = _bash('PKG_ID=a; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/a.safetensors; echo rc=$?', env=env); assert "rc=1" in r.stdout   # own claim does not count
    r = _bash('PKG_ID=a; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/nope.safetensors; echo rc=$?', env=env); assert "rc=1" in r.stdout


def test_unit_prune_keeps_a_file_another_package_claims(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    shared = c / "models" / "diffusion_models" / "Example" / "old.safetensors"; shared.parent.mkdir(parents=True); shared.write_bytes(b"1")
    row_b = "diffusion_models|Example||old.safetensors|https://huggingface.co/x/y/resolve/main/old.safetensors|1|B still needs it|"
    _bash(f'PKG_ID=b; PKG_NAME=B; PKG_VERSION=1; MODELS=("{row_b}"); base_env_setup; base_discover quiet; base_models; base_ledger_write', env=env)
    r = _bash(f'PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env={**env, "BASE_YES": "1"})
    assert "DEL=0" in r.stdout and shared.exists() and "claimed by another installed package" in r.stdout, r.stdout + r.stderr


def test_unit_dropped_pack_claimed_elsewhere_is_not_warned_about(tmp_path):
    c = _pod(tmp_path); (c / "custom_nodes" / "OldPack").mkdir(parents=True); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash('PKG_ID=a; DROPPED_PACKS="OldPack"; BASE_PACKS=(); PACKS=(); base_env_setup; base_discover quiet; base_packs git', env=env)
    assert "OldPack is no longer used" in r.stdout
    (tmp_path / "comfy-base" / "state" / "packages").mkdir(parents=True, exist_ok=True)
    (tmp_path / "comfy-base" / "state" / "packages" / "b.manifest").write_text(f"pkg=b\tname=B\tversion=1\tbase=1.0.0\tts=x\tstatus=ok\npack\tOldPack\t{'1'*40}\t{c}/custom_nodes/OldPack\thttps://github.com/x/OldPack\n")
    r = _bash('PKG_ID=a; DROPPED_PACKS="OldPack"; BASE_PACKS=(); PACKS=(); base_env_setup; base_discover quiet; base_packs git', env=env)
    assert "OldPack is no longer used" not in r.stdout, r.stdout


def test_unit_status_lists_installed_packages(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    _bash('PKG_ID=a; PKG_NAME="Pkg A"; PKG_VERSION=1.2.3; MODELS=(); base_env_setup; base_discover quiet; base_ledger_write', env=env)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "status"], capture_output=True, text=True, env={**os.environ, **env})
    assert r.returncode == 0 and "Pkg A" in r.stdout and "1.2.3" in r.stdout, r.stdout + r.stderr


def test_unit_check_mode_writes_no_ledger(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_DRY": "1"}
    _bash('PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; MODELS=(); base_env_setup; base_discover quiet; base_ledger_write', env=env)
    assert not (tmp_path / "comfy-base" / "state" / "packages" / "a.manifest").exists()


# ---------------------------------------------------------------- T12: step-one entry, self-install, manifest, packager

def test_unit_manifest_covers_every_shipped_file_and_a_tamper_is_fatal(tmp_path):
    _repo_only("_build/package.py")
    import shutil, zipfile
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--base", "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(BASE / "comfyui-base.zip") as z: z.extractall(tmp_path / "up")
    inst = tmp_path / "up" / "comfyui-base"
    listed = {ln.split(None, 1)[1].lstrip("*") for ln in (inst / "MANIFEST.sha256").read_text().splitlines() if ln.strip()}
    shipped = {str(p.relative_to(inst)) for p in inst.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}
    assert listed == shipped, (listed ^ shipped)
    assert "base.sh" in shipped and "lib/00-env.sh" in shipped and "py/basetest.py" in shipped and "comfyui-base-script.sh" in shipped
    # 2.12.2: the MIT notice travels with the zip, because the zip is what a project built on the base hands on
    assert "LICENSE" in shipped and "MIT License" in (inst / "LICENSE").read_text(encoding="utf-8")
    assert not any(p.startswith("_build") or p.endswith(".zip") for p in shipped)
    home = tmp_path / "comfy-base"; shutil.copytree(inst, home)
    (home / "lib" / "00-env.sh").write_text((home / "lib" / "00-env.sh").read_text() + "\n# tamper\n")
    r = subprocess.run(["bash", str(home / "base.sh"), "version"], capture_output=True, text=True, env={**os.environ, "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 5 and "00-env.sh" in r.stderr, r.stdout + r.stderr
    r = subprocess.run(["bash", str(inst / "base.sh"), "version"], capture_output=True, text=True, env={**os.environ, "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 0                                                                        # an extracted (not installed) copy is not checked


def test_unit_the_installer_copies_everything_the_packager_ships():
    """Two hand-kept lists must agree: what package.py puts in comfyui-base.zip, and what step one copies from the
    extracted zip into the home. 2.12.2 added LICENSE to the first and the install died on its own manifest check,
    because the second did not know the file existed."""
    _repo_only("_build/package.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("pkg_for_list", BASE / "_build" / "package.py")
    pkg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pkg)
    m = re.search(r"^\s*for f in (base\.sh [^;]+); do", (BASE / "lib" / "95-summary.sh").read_text(encoding="utf-8"), re.M)
    assert m, "the installer's copy list moved; re-point this test"
    copied = {w.strip('"') for w in m.group(1).split()}
    shipped = set(pkg.BASE_MEMBERS) | set(pkg.BASE_DIRS) | {"MANIFEST.sha256"}
    assert shipped <= copied, "shipped but never installed: %s" % sorted(shipped - copied)


def test_unit_step_one_installs_itself_and_runs_the_toolchain_in_fake_mode(tmp_path):
    _repo_only("the built zip")
    import zipfile
    with zipfile.ZipFile(BASE / "comfyui-base.zip") as z: z.extractall(tmp_path / "up")
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "comfyui_version.py").write_text('__version__ = "0.34.7"\n'); (c / "requirements.txt").write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASE_") and k not in ("HF_TOKEN", "COMFY_DIR")}
    env.update({"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_INNER": "1", "HOME": str(tmp_path / "home")})
    r = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1500:]
    assert (tmp_path / "comfy-base" / "base.sh").exists() and (tmp_path / "comfy-base" / "MANIFEST.sha256").exists()
    assert (tmp_path / "comfy-base" / "state" / "packages" / "base.manifest").exists()
    assert "SUMMARY" in r.stdout and "INSTALL BASE" in r.stdout
    r2 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "already installed" in r2.stdout, r2.stdout[-2000:]
    # 2.0.30: the SAME version with different content (an amended zip) must refresh the home — the pod kept a stale suite
    # for three runs on 2026-09-06 because only the version and the installed copy's own manifest were consulted
    import hashlib, re
    up = tmp_path / "up" / "comfyui-base"; fam = up / "pytest.ini"; fam.write_text(fam.read_text() + "# amended under the same version\n")
    man = up / "MANIFEST.sha256"; digest = hashlib.sha256(fam.read_bytes()).hexdigest()
    man.write_text(re.sub(r"^[0-9a-f]{64}(?=\s+\*?pytest\.ini$)", digest, man.read_text(), flags=re.M))
    note = "base %s already installed" % (up / "VERSION").read_text().strip()          # the base's own note, not a pack's "already installed"
    r3 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r3.returncode == 0 and "INSTALL BASE" in r3.stdout and note not in r3.stdout, r3.stdout[-2000:]
    assert "amended under the same version" in (tmp_path / "comfy-base" / "pytest.ini").read_text(), "the home must carry the amended file"
    r4 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r4.returncode == 0 and note in r4.stdout, r4.stdout[-2000:]


def test_unit_package_zip_is_reproducible_and_check_detects_drift(tmp_path):
    _repo_only("_build/package.py")
    import hashlib, shutil
    a = hashlib.sha256((BASE / "comfyui-base.zip").read_bytes()).hexdigest()
    subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--base"], check=True, capture_output=True)
    assert hashlib.sha256((BASE / "comfyui-base.zip").read_bytes()).hexdigest() == a
    # a package dir: the packager builds <Name>-runpod.zip from the package's files, flat, with modes
    pkg = tmp_path / "Toy Pkg"; pkg.mkdir()
    (pkg / "Toy Pkg-script.sh").write_text("#!/usr/bin/env bash\nZIP_EXTRA=( notes.txt )\n"); (pkg / "Toy Pkg Workflow.json").write_text("{}"); (pkg / "suite.py").write_text(""); (pkg / "notes.txt").write_text("n")
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg)], capture_output=True, text=True); assert r.returncode == 0, r.stdout + r.stderr
    import zipfile
    with zipfile.ZipFile(pkg / "Toy Pkg-runpod.zip") as z:
        names = z.namelist(); assert set(names) == {"Toy Pkg Workflow.json", "Toy Pkg-script.sh", "suite.py", "notes.txt"}
        assert (z.getinfo("Toy Pkg-script.sh").external_attr >> 16) & 0o777 == 0o700
    assert subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg), "--check"], capture_output=True).returncode == 0
    (pkg / "notes.txt").write_text("changed")
    assert subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg), "--check"], capture_output=True).returncode == 1


def test_unit_zip_extra_carries_filenames_with_spaces(tmp_path):
    """Every filename this project ships has spaces in it. A whitespace split turned
    ZIP_EXTRA=( "A B.json" ) into two members that do not exist, and the packager then died with
    `missing: .../A` — naming a file nobody had written, which is the worst kind of error message.
    Unquoted single-word entries must keep parsing exactly as before, or this breaks every package."""
    _repo_only("_build/package.py")
    import zipfile
    pkg = tmp_path / "Toy Pkg"; pkg.mkdir()
    (pkg / "Toy Pkg-script.sh").write_text(
        '#!/usr/bin/env bash\nZIP_EXTRA=( plain.txt "Spaced Name.json" )   # trailing comment\n')
    (pkg / "Toy Pkg Workflow.json").write_text("{}")
    (pkg / "suite.py").write_text("")
    (pkg / "plain.txt").write_text("p")
    (pkg / "Spaced Name.json").write_text("{}")
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(pkg / "Toy Pkg-runpod.zip") as z:
        assert set(z.namelist()) == {"Toy Pkg Workflow.json", "Toy Pkg-script.sh", "suite.py",
                                     "plain.txt", "Spaced Name.json"}


def test_unit_a_package_can_ask_for_a_restart():
    """base_restart() REBUILDS BASE_RESTART_WHY from the base's own signals, so a hook appending to
    that array had its reason thrown away. A package that ships its own ComfyUI node then ended up
    with the file on disk, never registered (ComfyUI imports custom nodes at start), and a GREEN
    install report — the worst shape a failure can take. PKG_RESTART_WHY is the package's channel
    and base_restart folds it in."""
    src = (BASE / "lib" / "00-env.sh").read_text()
    assert "PKG_RESTART_WHY=()" in src, "PKG_RESTART_WHY is not initialised"
    srv = (BASE / "lib" / "80-server.sh").read_text()
    body = srv[srv.index("base_restart()"):]
    wipe = body.index("BASE_RESTART_WHY=()")
    fold = body.index("PKG_RESTART_WHY[@]")
    assert fold > wipe, "PKG_RESTART_WHY must be folded in AFTER base_restart clears the array"
    # Structural on purpose. The end-to-end path needs a live ComfyUI to answer /system_stats —
    # base_restart returns early otherwise — and standing one up here would test the harness, not
    # the fold. What actually regressed was the ORDER, and that is what is asserted above.
    assert "PKG_RESTART_WHY" in (BASE / "comfyui-base-handbook.md").read_text(), \
        "a package-facing hook that is not in the handbook is a hook nobody will find"


def test_unit_pod_tiers_skip_off_pod_and_the_handbook_exists():
    src = (BASE / "suite" / "test_pod.py").read_text()
    assert "BASE_ON_POD" in src and "pytest.skip" in src
    hb = (BASE / "comfyui-base-handbook.md").read_text()
    for needle in ("step one", "BASE_RESTART", "LOCAL", "category|Family|Purpose|file", "dir|url|sha|cnr_id|why", "pkg_post_venv", "/workspace/comfy-base", "rescue"):
        assert needle in hb, needle


# ---------------------------------------------------------------- T13: the testbed derives its packs; list-packs is safe on unconverted scripts

def test_unit_list_packs_never_runs_a_script_that_does_not_source_the_base(tmp_path):
    d = tmp_path / "Old Pkg"; d.mkdir(); marker = tmp_path / "ran"
    (d / "Old Pkg-script.sh").write_text('#!/usr/bin/env bash\ntouch "%s"\nexit 0\n' % marker)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(d), str(BASE)], capture_output=True, text=True)
    assert r.returncode == 0 and not marker.exists() and "does not source the base" in r.stderr, r.stdout + r.stderr
    assert sum(1 for ln in r.stdout.splitlines() if ln.startswith("rgthree-comfy|")) == 1        # the base dir itself is not a package


def test_unit_testbed_derives_its_packs_from_the_packages():
    # 2.6.0: testbed.sh ships in this repository, so a standalone clone can run the comfyui tier instead of
    # skipping it forever. A brand repository still has one beside the base; both locations are accepted.
    _repo_only("the repo's testbed.sh")
    tbsh = next((q for q in (BASE / "testbed.sh", BASE.parent / "testbed.sh") if q.exists()), None)
    if tbsh is None:
        pytest.skip("no testbed.sh in or beside the base")
    tb = tbsh.read_text()
    assert "list-packs" in tb and "rgthree/rgthree-comfy" not in tb and "cg-use-everywhere" not in tb   # the base packs are never hand-listed
    assert "EXTRA=(" in tb                                                                        # packs of packages not yet converted
    r = subprocess.run(["bash", str(tbsh), "--status"], capture_output=True, text=True)
    assert r.returncode == 0 and "BASE_NODE_SRC=" in r.stdout and "BASE_SERVER=" in r.stdout, r.stdout + r.stderr
    # it must FIND the base from wherever it sits: without that, derive_packs falls through to an empty EXTRA
    # and the testbed provisions a ComfyUI with no custom nodes at all, silently.
    assert "no ComfyUI Base beside this script" not in (r.stdout + r.stderr), r.stdout + r.stderr


def test_unit_the_testbed_finds_packages_without_naming_one():
    """Nothing in the base knows a brand's name (handbook rule 0). testbed.sh used to carry three hard-coded
    `../<brand>/packages/...` vendored paths: a brand's name in a public repository, and a list that went stale
    every time a package moved. They are derived from the tree now.

    This asserts the INVARIANT rather than a list of forbidden names. A name list would have to cite the very
    organisations the rule exists to keep out, and would go stale as consumers change; the property is that every
    `packages/` glob is anchored on a VARIABLE and never on a literal directory name, which is exactly what the
    original leak violated and what catches any future one."""
    _repo_only("the repo's testbed.sh")
    tbsh = next((q for q in (BASE / "testbed.sh", BASE.parent / "testbed.sh") if q.exists()), None)
    if tbsh is None:
        pytest.skip("no testbed.sh in or beside the base")
    bad = []
    for n, line in enumerate(tbsh.read_text().splitlines(), 1):
        code = line.split("#", 1)[0]
        for m in re.finditer(r"(\S*)/packages/", code):
            before = m.group(1)
            if before and "$" not in before and not before.startswith(("*", '"*')):
                bad.append((n, line.strip()))
    assert not bad, "a packages/ glob anchored on a literal directory, not a variable: %r" % (bad,)



# ---------------------------------------------------------------- 3.7.1: the testbed's Python is the newest torch ships for

_FAKE_UV = r"""#!/bin/bash
# a uv stand-in: `python list` prints a canned listing, `pip compile` resolves only on $TORCH_OK's minors,
# `venv` behaves like uv 0.13 (refuses an existing directory) and makes a python that reports the minor it was given
echo "$*" >> "$UV_LOG"
case "$1 $2" in
  "python list") cat <<'EOF'
cpython-3.16.0a1-macos-aarch64-none                 <download available>
cpython-3.16.0a1+freethreaded-macos-aarch64-none    <download available>
cpython-3.15.0-macos-aarch64-none                   /x/cpython-3.15/bin/python3.15
cpython-3.15.0+freethreaded-macos-aarch64-none      <download available>
cpython-3.14.8-macos-aarch64-none                   /x/cpython-3.14/bin/python3.14
cpython-3.13.15-macos-aarch64-none                  <download available>
pypy-3.12.14-macos-aarch64-none                     <download available>
EOF
    exit 0;;
  "pip compile") cat >/dev/null; v=""; prev=""
    for a in "$@"; do [ "$prev" = "--python-version" ] && v="$a"; prev="$a"; done
    for ok in $TORCH_OK; do [ "$ok" = "$v" ] && exit 0; done
    echo "No solution found when resolving dependencies: torch" >&2; exit 1;;
  "venv --python") d="$4"
    [ -e "$d" ] && { echo "error: A virtual environment already exists at $d" >&2; exit 2; }
    mkdir -p "$d/bin"; printf '#!/bin/bash\necho %s\n' "$3" > "$d/bin/python"; chmod +x "$d/bin/python"; exit 0;;
esac
exit 0
"""


def _testbed_sandbox(tmp_path, torch_ok):
    """testbed.sh copied into an empty folder (no base beside it, so no packs and no pkgdirs), and a fake uv first on PATH."""
    _repo_only("the repo's testbed.sh")
    tb = tmp_path / "tb"; tb.mkdir(parents=True); (tb / "testbed.sh").write_text((BASE / "testbed.sh").read_text())
    bindir = tmp_path / "bin"; bindir.mkdir(); uv = bindir / "uv"; uv.write_text(_FAKE_UV); uv.chmod(0o755)
    log = tmp_path / "uv.log"
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ.get("PATH", "")), UV_LOG=str(log), TORCH_OK=torch_ok, TMPDIR=str(tmp_path))
    def run(snippet):
        return subprocess.run(["bash", "-c", 'source "%s/testbed.sh"; %s' % (tb, snippet)], capture_output=True, text=True, env=env)
    return tb, log, run


def test_unit_testbed_picks_the_newest_python_torch_ships_for(tmp_path):
    """uv listed 3.15 on 2026-10-15 before torch had a cp315 wheel, and `testbed.sh --server` died in the torch install.
    The pick goes newest first and takes the first minor the torch family resolves on; pre-releases and free-threaded
    builds are never candidates."""
    _repo_only("the repo's testbed.sh")
    tb, log, run = _testbed_sandbox(tmp_path, "3.14 3.13")
    r = run("_testbed_python_pick")
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "3.14", r.stdout + r.stderr
    assert "3.15: torch has no build for it yet" in r.stderr, r.stderr
    tried = [ln.split("--python-version ")[1].split()[0] for ln in log.read_text().splitlines() if ln.startswith("pip compile")]
    assert tried == ["3.15", "3.14"], tried                                   # newest first, stops at the first yes, no 3.16a1
    # the day torch ships for 3.15, the same run takes it: still always the newest
    tb, log, run = _testbed_sandbox(tmp_path / "later", "3.15 3.14")
    r = run("_testbed_python_pick")
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "3.15", r.stdout + r.stderr


def test_unit_testbed_pick_keeps_a_working_venv_when_nothing_resolves(tmp_path):
    """Offline, no minor resolves: the pick keeps the venv's own Python instead of tearing a working venv down, and with
    no venv names the newest, with a warning either way."""
    _repo_only("the repo's testbed.sh")
    tb, log, run = _testbed_sandbox(tmp_path, "")
    r = run("_testbed_python_pick")
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "3.15" and "torch resolves on none of" in r.stderr, r.stdout + r.stderr
    v = tb / "testbed" / ".venv-testbed" / "bin"; v.mkdir(parents=True)
    (v / "python").write_text("#!/bin/bash\necho 3.13\n"); (v / "python").chmod(0o755)
    r = run("_testbed_python_pick")
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "3.13" and "keeping the venv's Python 3.13" in r.stderr, r.stdout + r.stderr


def test_unit_testbed_replaces_a_half_made_venv(tmp_path):
    """A first build that died (or whose interpreter uv later removed) left $VENV with a dangling bin/python. `-x` could not
    see it, the rebuild was skipped, and `uv venv` refused with "A virtual environment already exists". Anything at $VENV
    whose python does not run, or runs the wrong minor, is removed first; a venv already on the picked minor is reused."""
    _repo_only("the repo's testbed.sh")
    tb, log, run = _testbed_sandbox(tmp_path, "3.14")
    venv = tb / "testbed" / ".venv-testbed"
    (venv / "bin").mkdir(parents=True); (venv / "bin" / "python").symlink_to(tmp_path / "gone" / "python3.15")
    (venv / "lib").mkdir(); (venv / "lib" / "half-installed").write_text("")
    r = run("_testbed_venv 3.14 && _venv_minor")
    assert r.returncode == 0 and r.stdout.strip().endswith("3.14"), r.stdout + r.stderr
    assert "half-made build" in r.stdout and not (venv / "lib").exists(), r.stdout + r.stderr
    # reused when it already is the picked minor: no `uv venv` call
    n = sum(1 for ln in log.read_text().splitlines() if ln.startswith("venv "))
    r = run("_testbed_venv 3.14")
    assert r.returncode == 0 and sum(1 for ln in log.read_text().splitlines() if ln.startswith("venv ")) == n, r.stdout + r.stderr
    # an older minor is rebuilt onto the picked one
    r = run("_testbed_venv 3.15 && _venv_minor")
    assert r.returncode == 0 and "rebuilding it" in r.stdout and r.stdout.strip().endswith("3.15"), r.stdout + r.stderr


def test_unit_testbed_sourced_runs_nothing(tmp_path):
    """The suite sources testbed.sh for its functions: sourcing defines and returns, it never provisions or prints usage."""
    _repo_only("the repo's testbed.sh")
    tb, log, run = _testbed_sandbox(tmp_path, "3.14")
    r = run("type provision_server >/dev/null && echo defined")
    assert r.returncode == 0 and r.stdout.strip().endswith("defined") and "usage:" not in r.stdout and "SOURCE" not in r.stdout, r.stdout + r.stderr
    assert not (tb / "testbed").exists() and not log.exists()



# ---------------------------------------------------------------- plan 2 / T1: tier markers by prefix; converted packages agree

def test_unit_prefix_markers_apply_to_converted_suites(tmp_path):
    import sys
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --strict-markers\nmarkers =\n    graph: g\n    unit: u\n")
    (tmp_path / "suite.py").write_text("def test_graph_a(): pass\ndef test_unit_b(): pass\ndef test_other_c(): pass\n")
    r = subprocess.run([sys.executable, "-m", "pytest", str(tmp_path / "suite.py"), "-c", str(tmp_path / "pytest.ini"), "-q", "-p", "no:cacheprovider", "-p", "basetest", "-m", "graph"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(BASE / "py")})
    assert "1 passed" in r.stdout and "2 deselected" in r.stdout, r.stdout + r.stderr


def test_unit_every_package_family_is_in_its_brands_families_file():
    """2.3.0: the base lists no model families of its own. A brand keeps <brand>/families.txt beside brand.toml (one
    spelling per line) and every MODELS row of every package in that brand names one of them, so two packages on the
    same pod never file the same family under two spellings. Standalone, or for a brand without the file, this skips."""
    from basetest import brand_families, converted_packages, load_package
    checked = 0
    for d in converted_packages(_consumer_root()):
        fams = brand_families(d)
        if fams is None:
            continue
        p = load_package(d); checked += 1
        for m in p.models:
            if m["family"]:
                assert m["family"] in fams, "%s: family %r for %s is not in the brand's families.txt (%s)" % (d.name, m["family"], m["file"], ", ".join(sorted(fams)))
    if not checked: pytest.skip("no brand with a families.txt beside this base")


def test_unit_converted_packages_agree_on_dests_superseded_and_pins():
    from basetest import converted_packages, load_package
    pkgs = [load_package(d) for d in converted_packages(_consumer_root())]    # the workspace, or <root> of base/comfyui-base
    if not pkgs: pytest.skip("no converted package beside the base yet")
    dest_by_name, sup_by_pkg, claims = {}, {}, {}
    for p in pkgs:
        for m in p.models:
            dest = "/".join(x for x in (m["category"], m["family"], m["purpose"], m["file"]) if x)
            assert dest_by_name.setdefault(m["file"].lower(), dest) == dest, "%s filed at two dests: %s vs %s" % (m["file"], dest_by_name[m["file"].lower()], dest)
            claims.setdefault(m["file"].lower(), set()).add(p.id)
        sup_by_pkg[p.id] = {s.lower() for s in p.superseded}
    for pid, sup in sup_by_pkg.items():
        for name in sup:
            others = claims.get(name, set()) - {pid}
            assert not others, "%s supersedes %s, which %s still installs" % (pid, name, sorted(others))
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", *[str(p.dir) for p in pkgs]], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unit_sageattention_builder_is_dry_run_aware_and_idempotent(tmp_path):
    """Three packages need SageAttention built for THIS GPU's sm (no PyPI wheel has sm_100/sm_120 kernels; 1.0.6 crashes
    there): one package carried the build in its own hook, another's first render died on `No module named
    'sageattention'`. The base offers the build to every hook: a dry run says "would", a fake venv skips,
    an importable sageattention is left alone, a missing GPU is a failure by name."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True); py = v / "bin" / "python"
    py.write_text('#!/bin/bash\ncase "$*" in *"import sageattention"*) exit "${SAGE_STUB_RC:-1}";; *) exit 0;; esac\n'); py.chmod(0o755)
    base = f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; '
    r = _bash(base + 'BASE_DRY=1; base_build_sageattention; echo F=${#BASE_FAILED[@]}')
    assert "would install SageAttention" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=1; base_build_sageattention; echo F=${#BASE_FAILED[@]}')
    assert "SageAttention: skipped" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    # 3.0.0: "imports" is not enough; it must be built from main's newest commit for this venv (the stamped cache key)
    gitbin = tmp_path / "gitbin"; gitbin.mkdir()
    (gitbin / "git").write_text('#!/bin/bash\n[ "$1" = ls-remote ] && { echo "abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main"; exit 0; }\nexec /usr/bin/git "$@"\n'); (gitbin / "git").chmod(0o755)
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp-torch0-sm\n")
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=""; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env={"SAGE_STUB_RC": "0", "PATH": f"{gitbin}:{os.environ['PATH']}"})
    assert "already built from SageAttention abcdef123456" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=""; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env={"SAGE_STUB_RC": "1"})
    assert "no CUDA device visible" in r.stdout and "F=1" in r.stdout, r.stdout + r.stderr
    # torch 2.14's headers demand C++20 and upstream's setup.py hardcodes -std=c++17: every kernel failed at the first include
    # on the pod (2.0.25). The build clones the ref and patches the flag before pip sees it; the source says so once.
    src = (BASE / "lib" / "40-packs.sh").read_text()
    body = src[src.index("base_build_sageattention()"):src.index("base_consolidate()")]
    assert "git clone" in body and "SAGE_REF" in body and "s/-std=c++17/-std=c++20/g" in body and "setup.py" in body, body[-600:]


def _sage_published_env(tmp_path, wheel_bytes=b"wheel", sums=None, sm="sm_100"):
    """A venv whose python answers the key's questions (cp314, torch 2.14.1-cu130) and imports sageattention once a pip
    install ran; git ls-remote answering one commit; a file:// release mirror holding one wheel and the sm_<arch>.sha256 beside it."""
    import hashlib, platform
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True); py = v / "bin" / "python"; mark = tmp_path / "installed"
    py.write_text('#!/bin/bash\ncase "$*" in *"import sageattention"*) [ -f "%s" ];; *version_info*) echo cp314;; *torch.__version__*) echo 2.14.1-cu130;; *) exit 0;; esac\n' % mark)
    py.chmod(0o755)
    gitbin = tmp_path / "gitbin"; gitbin.mkdir()
    (gitbin / "git").write_text('#!/bin/bash\n[ "$1" = ls-remote ] && { echo "abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main"; exit 0; }\nexec /usr/bin/git "$@"\n'); (gitbin / "git").chmod(0o755)
    whl = "sageattention-2.2.0-cp314-cp314-linux_%s.whl" % platform.machine()
    rel = tmp_path / "rel" / "sage-abcdef123456-torch2.14.1-cu130-cp314"; rel.mkdir(parents=True)
    (rel / ("%s.%s" % (sm, whl))).write_bytes(wheel_bytes)
    if sums is None:
        sums = "%s  %s.%s\n" % (hashlib.sha256(wheel_bytes).hexdigest(), sm, whl)
    (rel / ("%s.sha256" % sm)).write_text(sums)
    snippet = (f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}/state"; BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=sm_100; '
               f'SAGE_WHEELS_URL="file://{tmp_path}/rel"; '
               f'_base_pip(){{ echo "PIP $*"; touch "{mark}"; }}; base_cuda_toolchain(){{ echo TOOLCHAIN-PROBED; BASE_CUDA_BUILD_OK=0; }}; ')
    env = {"PATH": f"{gitbin}:{os.environ['PATH']}"}
    return snippet, env, v, whl


def test_unit_sage_installs_the_published_wheel_after_checking_its_sha256(tmp_path):
    """3.13.0: SageAttention was built from source on every fresh volume (10-20 min, the biggest step of a pod install
    once the models download in parallel). A wheel published for exactly this key (commit, Python, torch + CUDA build,
    GPU arch) is downloaded instead, checked against the sm_<arch>.sha256 beside it BEFORE pip sees it, and kept in the volume
    cache; a wheel needs no CUDA toolkit, so the cuBLAS probe (which may apt-install a toolkit) is never reached."""
    snippet, env, v, whl = _sage_published_env(tmp_path)
    r = _bash(snippet + '_base_sage_install; echo F=${#BASE_FAILED[@]}', env=env)
    out = r.stdout + r.stderr
    assert "sha256 matches its release" in out and "a published wheel matches" in out and "F=0" in r.stdout, out
    assert "TOOLCHAIN-PROBED" not in out and "building SageAttention" not in out, out
    key = "sageattention-abcdef123456-cp314-torch2.14.1-cu130-sm_100"
    assert (v / ".comfy-base-sageattention").read_text().strip() == key, out
    assert (tmp_path / "state" / "wheels" / key / whl).is_file(), out          # the volume cache: the next venv needs no download
    assert ("PIP install -q --force-reinstall --no-deps %s/state/wheels/%s/%s" % (tmp_path, key, whl)) in out, out


def test_unit_sage_refuses_a_published_wheel_whose_sha256_differs(tmp_path):
    snippet, env, v, whl = _sage_published_env(tmp_path, sums="%s  sm_100.%s\n" % ("0" * 64, "sageattention-2.2.0-cp314-cp314-linux_%s.whl" % __import__("platform").machine()))
    r = _bash(snippet + '_base_sage_install; echo F=${#BASE_FAILED[@]}', env=env)
    out = r.stdout + r.stderr
    assert "NOT installed" in out and "PIP install" not in out, out
    assert "TOOLCHAIN-PROBED" in out and "F=1" in r.stdout, out                  # it fell through to the build (no toolkit here)
    assert not list((tmp_path / "state" / "wheels").glob("*/*.whl")), out


def test_unit_sage_builds_when_no_wheel_is_published_or_wheels_are_off(tmp_path):
    snippet, env, v, whl = _sage_published_env(tmp_path, sm="sm_120")            # the release has an sm_120 wheel only
    r = _bash(snippet + '_base_sage_install; echo F=${#BASE_FAILED[@]}', env=env)
    assert "no published sm_100 wheel for sage-abcdef123456-torch2.14.1-cu130-cp314" in r.stdout and "TOOLCHAIN-PROBED" in r.stdout and "PIP install" not in r.stdout, r.stdout + r.stderr
    r = _bash(snippet + f'SAGE_WHEELS_URL="file://{tmp_path}/nowhere"; _base_sage_install', env=env)
    assert "no published sm_100 wheel" in r.stdout and "TOOLCHAIN-PROBED" in r.stdout, r.stdout + r.stderr
    r = _bash(snippet + 'SAGE_WHEELS=0; _base_sage_install', env=env)
    assert "published wheels off" in r.stdout and "PIP install" not in r.stdout, r.stdout + r.stderr
    r = _bash(snippet + 'SAGE_ARCHS="9.0;12.0"; _base_sage_install', env=env)    # an image build wants several archs in one wheel
    assert "published" not in r.stdout and "TOOLCHAIN-PROBED" in r.stdout, r.stdout + r.stderr


def test_unit_sage_prefers_the_volume_cache_to_a_download(tmp_path):
    snippet, env, v, whl = _sage_published_env(tmp_path)
    key = "sageattention-abcdef123456-cp314-torch2.14.1-cu130-sm_100"
    (tmp_path / "state" / "wheels" / key).mkdir(parents=True); (tmp_path / "state" / "wheels" / key / whl).write_bytes(b"built here")
    r = _bash(snippet + f'SAGE_WHEELS_URL="file://{tmp_path}/nowhere"; _base_sage_install; echo F=${{#BASE_FAILED[@]}}', env=env)
    assert "a cached wheel matches" in r.stdout and "no published" not in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr


def _sage_src_fixture(tmp_path, branches=("sm80", "sm90", "sm120")):
    """A SageAttention checkout's two files the base reads: setup.py's qattn gate, core.py's sageattn() dispatch."""
    src = tmp_path / "fixture"; (src / "sageattention").mkdir(parents=True)
    (src / "setup.py").write_text('    NVCC_FLAGS = ["-std=c++17"]\n    if HAS_SM80 or HAS_SM86:\n        ext_modules.append("_qattn_sm80")\n    ext_modules.append("_fused")\n')
    (src / "sageattention" / "core.py").write_text("".join('    elif arch == "%s":\n        pass\n' % b for b in branches))
    return src


def test_unit_sage_builds_fused_only_where_the_triton_mode_is_the_one_that_runs(tmp_path):
    """3.13.0: the Triton mode (what B200 and B300 run, upstream's sageattn() having no sm_100/sm_103 branch) needs only
    the small `_fused` extension: core.py imports it unconditionally, the qattn kernels are optional imports. A GPU
    whose kept proof names Triton, or with no proof yet and no branch in sageattn(), builds `_fused` alone (about a
    minute, not 10-20); a proof naming auto or a CUDA mode, SAGE_FUSED_ONLY=0, or an image's arch list builds them all."""
    src = _sage_src_fixture(tmp_path)
    st = tmp_path / "state"; (st / "sage").mkdir(parents=True)
    run = f'BASE_STATE="{st}"; _base_sage_fused_only "{src}" && echo "FUSED $_BASE_SAGE_FUSED_WHY" || echo FULL'
    assert "no sm_100 branch" in _bash("BASE_GPU_SM=sm_100; " + run).stdout
    assert "no sm_103 branch" in _bash("BASE_GPU_SM=sm_103; " + run).stdout
    assert "FULL" in _bash("BASE_GPU_SM=sm_120; " + run).stdout                     # auto has a branch: the CUDA kernels
    assert "FULL" in _bash("BASE_GPU_SM=sm_100; SAGE_FUSED_ONLY=0; " + run).stdout
    assert "FULL" in _bash('BASE_GPU_SM=sm_100; SAGE_ARCHS="9.0;10.0"; ' + run).stdout
    (st / "sage" / "sm_100.mode").write_text("sageattn_qk_int8_pv_fp8_cuda\n")     # proven to run a CUDA mode here
    assert "FULL" in _bash("BASE_GPU_SM=sm_100; " + run).stdout
    (st / "sage" / "sm_120.mode").write_text("sageattn_qk_int8_pv_fp16_triton\n")  # proven Triton, even where auto has a branch
    assert "names the Triton mode" in _bash("BASE_GPU_SM=sm_120; " + run).stdout
    # the patch: every qattn flag cleared just before upstream reads them; once only; _fused left as it was
    r = _bash(f'_base_sage_fused_setup "{src}/setup.py" && _base_sage_fused_setup "{src}/setup.py" && cat "{src}/setup.py"')
    assert r.stdout.count("comfyui-base: _fused only") == 1 and r.stdout.index("_fused only") < r.stdout.index("if HAS_SM80 or"), r.stdout + r.stderr
    (src / "setup.py").write_text("ext_modules = []\n")                              # upstream's layout moved: the full build
    assert "RC=1" in _bash(f'_base_sage_fused_setup "{src}/setup.py"; echo RC=$?').stdout


def test_unit_sage_fused_only_build_is_stamped_and_cached_under_its_own_key(tmp_path):
    snippet, env, v, whl = _sage_published_env(tmp_path)
    fixture = _sage_src_fixture(tmp_path)
    gitbin = tmp_path / "gitbin"
    (gitbin / "git").write_text('#!/bin/bash\n[ "$1" = ls-remote ] && { echo "abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main"; exit 0; }\n'
                                '[ "$1" = clone ] && { d="${@: -1}"; mkdir -p "$d" && cp -R "%s/." "$d/"; exit 0; }\nexit 1\n' % fixture)
    build = (f'base_cuda_toolchain(){{ BASE_CUDA_BUILD_OK=1; }}; _base_run_watched(){{ shift; "$@"; }}; '
             f'_base_pip(){{ echo "PIP $*"; if [ "$1" = wheel ]; then cp "${{@: -1}}/setup.py" "{tmp_path}/built-setup.py"; while [ "$1" != -w ]; do shift; done; touch "$2/{whl}"; else touch "{tmp_path}/installed"; fi; }}; ')
    r = _bash(snippet + build + f'SAGE_WHEELS_URL="file://{tmp_path}/nowhere"; _base_sage_install; echo F=${{#BASE_FAILED[@]}}', env=env)
    out = r.stdout + r.stderr
    key = "sageattention-abcdef123456-cp314-torch2.14.1-cu130-sm_100-fused"
    assert "_fused only, about a minute" in out and "F=0" in r.stdout, out
    assert (v / ".comfy-base-sageattention").read_text().strip() == key, out
    assert (tmp_path / "state" / "wheels" / key / whl).is_file(), out
    assert "comfyui-base: _fused only" in (tmp_path / "built-setup.py").read_text()
    r = _bash(snippet + build + f'SAGE_WHEELS_URL="file://{tmp_path}/nowhere"; _base_sage_install', env=env)     # the next run: nothing to do
    assert "_fused only: this GPU runs the Triton mode" in r.stdout and "PIP wheel" not in r.stdout, r.stdout + r.stderr


def test_unit_sage_a_fused_only_build_whose_triton_mode_fails_gets_every_kernel(tmp_path):
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True); py = v / "bin" / "python"; py.write_text("#!/bin/bash\nexit 0\n"); py.chmod(0o755)
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp314-torch2.14.1-cu130-sm_100-fused\n")
    r = _bash(f'VENV="{v}"; PY="{py}"; BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=sm_100; n=0; '
              '_base_sage_install(){ echo "INSTALL FUSED_ONLY=${SAGE_FUSED_ONLY:-1}"; }; '
              'base_sage_proof(){ n=$((n+1)); if [ $n = 1 ]; then BASE_SAGE_VERDICT=none; else BASE_SAGE_VERDICT=ok; fi; }; '
              'base_build_sageattention; echo F=${#BASE_FAILED[@]}')
    assert "building every kernel" in r.stdout and "INSTALL FUSED_ONLY=0" in r.stdout and "Sage: OK" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr


def test_unit_sage_wheels_are_built_and_named_the_way_a_pod_keys_them(tmp_path):
    """The published wheel only helps if the workflow's key is the pod's key, and only if it is built from the same patched
    source. Both sides name the release sage-<commit12>-torch<ver>-<cuda>-<cpXY>, the asset sm_<arch>.<wheel> and its sum
    sm_<arch>.sha256. Published assets are write-once: the workflow never passes --clobber and refuses a name already there."""
    _repo_only("_build/sage/")
    lib = (BASE / "lib" / "40-packs.sh").read_text()
    sh = (BASE / "_build" / "sage" / "build-wheel.sh").read_text()
    rs = (BASE / "_build" / "sage" / "resolve.py").read_text()
    wf = (BASE / ".github" / "workflows" / "sage-wheels.yml").read_text()
    assert 'tag="sage-${sha}-torch${tv}-${pytag}"' in lib and '"sage-%s-torch%s-%s-%s" % (sha[:12], torch, backend, pytag)' in rs
    assert 'key="sageattention-${sha:0:12}-${pytag}-torch${tv}-${sm}"' in sh and '_base_sage_prepare_src "$SRC"' in sh
    assert '_base_sage_prepare_src "$src"' in lib and 'p="${BASE_GPU_SM}."' in lib and '"$up/$sm.$(basename' in wf
    assert '"$root/$tag/${BASE_GPU_SM}.sha256"' in lib and '"sm_%s.sha256" % x.replace(".", "") not in have' in rs
    assert "--clobber" not in wf and "published assets are never replaced" in wf
    up = wf[wf.index("(cd \"$up\" && sha256sum \"$a\")"):]
    assert up.index('upload "$TAG" --repo "$GITHUB_REPOSITORY" "$w"') < up.index('upload "$TAG" --repo "$GITHUB_REPOSITORY" "$up/$sm.sha256"'), up[:600]   # the sum after its wheel
    # the shared source patch: C++20 for torch >= 2.14's headers, and 10.3 in 10.0's row
    src = tmp_path / "src"; src.mkdir()
    (src / "setup.py").write_text('    NVCC_FLAGS = ["-std=c++17"]\n        elif capability.startswith("10.0"):\n            HAS_SM100 = True\n            num = "100a"\n')
    r = _bash(f'_base_sage_prepare_src "{src}"; cat "{src}/setup.py"')
    assert "-std=c++20" in r.stdout and "c++17" not in r.stdout and 'startswith("10.3")' in r.stdout and 'num = "103a"' in r.stdout, r.stdout + r.stderr


def _fake_nvidia_smi(tmp_path, total, used, apps):
    """A nvidia-smi answering the two queries gpu_facts.py makes, with the given card and visible-process numbers."""
    d = tmp_path / "smibin"; d.mkdir(parents=True, exist_ok=True)
    apps_csv = "\\n".join("%d, %d" % a for a in apps)
    (d / "nvidia-smi").write_text('#!/bin/bash\ncase "$*" in *query-gpu=memory*) echo "%d, %d";; *query-compute-apps*) printf "%s\\n";; *) exit 1;; esac\n' % (total, used, apps_csv))
    (d / "nvidia-smi").chmod(0o755)
    return d


def test_unit_gpu_facts_names_memory_held_outside_the_container(tmp_path):
    """The 2026-09-06 numbers: 97887 MiB card, 95258 used, one visible process holding 1698 — 93560 MiB are nobody's we can see."""
    import subprocess, sys
    sys.path.insert(0, str(BASE / "py")); import gpu_facts
    assert gpu_facts.verdict(97887, 95258, [(1903, 1698)]) == (1698, 93560, "NOT-OURS")
    assert gpu_facts.verdict(97887, 620, []) == (0, 620, "ours")                      # the driver's own reserve is not a tenant
    assert gpu_facts.verdict(97887, 20000, [(1, 19500)]) == (19500, 500, "ours")     # our models, nothing foreign
    env = {**os.environ, "PATH": "%s:%s" % (_fake_nvidia_smi(tmp_path, 97887, 95258, [(1903, 1698)]), os.environ["PATH"])}
    out = subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip()
    assert out == "total=97887 used=95258 ours=1698 foreign=93560 procs=1 verdict=NOT-OURS", out
    env["PATH"] = "%s:%s" % (_fake_nvidia_smi(tmp_path / "clean", 97887, 620, []), os.environ["PATH"])
    assert subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip().endswith("verdict=ours")
    empty = tmp_path / "empty-path"; empty.mkdir()                                    # no nvidia-smi at all: say so, never guess
    env["PATH"] = str(empty)                                                          # (2.0.29: a pod's container HAS /usr/bin/nvidia-smi — the real card answered there)
    assert subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip() == "verdict=no-gpu"


def test_unit_discovery_fails_the_run_when_the_gpu_is_not_ours():
    """The gate: a NOT-OURS line is a failed run with the remedy printed; an ours line is a fact; no answer is a note."""
    r = _bash('BASE_GPU_LINE="total=97887 used=95258 ours=1698 foreign=93560 procs=1 verdict=NOT-OURS"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}; printf "%s\\n" "${BASE_FAILED[@]}"')
    assert "F=1" in r.stdout and "91 GiB" in r.stdout and "OUTSIDE this container" in r.stdout and "stop and start the pod" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_GPU_LINE="total=97887 used=20000 ours=19500 foreign=500 procs=1 verdict=ours"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}')
    assert "F=0" in r.stdout and "the GPU is ours" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_GPU_LINE="verdict=no-gpu"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}')
    assert "F=0" in r.stdout and "not verifiable" in r.stdout, r.stdout + r.stderr


def test_unit_check_never_restamps_a_reused_real_venv(tmp_path):
    """A --check on the live pod rewrote the venv stamp (ts, base version, by=) through the reuse path: the stamp write there
    had no dry guard, and the fake-pod tests return before it. A dry run
    changes nothing; the real path still refreshes the stamp."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True)
    py = v / "bin" / "python"
    py.write_text('#!/bin/bash\ncase "$*" in *version_info*) echo 3.14.7;; *torch*) echo "torch 2.14.0 · CUDA 13.0";; *) exit 0;; esac\n'); py.chmod(0o755)
    stamp = v / ".comfy-base-venv"; before = "python=3.14.7 torch=2.14.0 base=0.0.1 ts=20260101-000000 by=pkg_a\n"; stamp.write_text(before)
    def run(dry):
        snippet = (f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; BASE_DRY={dry}; BASE_NO_NET=0; '
                   '_base_ensure_uv(){ return 0; }; _base_python_pick(){ echo 3.14; }; _base_torch_pick(){ echo 2.14.0; }; '
                   '_base_venv_probe(){ return 0; }; _base_venv_owner(){ echo base; }; base_venv; echo "RESULT=$BASE_VENV_RESULT"')
        return _bash(snippet)
    r = run(1)
    assert "RESULT=reused" in r.stdout, r.stdout + r.stderr
    assert stamp.read_text() == before, "a --check rewrote the venv stamp: " + stamp.read_text()
    assert "would" in r.stdout and "stamp" in r.stdout, r.stdout
    r = run(0)
    assert "RESULT=reused" in r.stdout and stamp.read_text() != before and "by=base" in stamp.read_text(), r.stdout + r.stderr


def test_unit_check_mode_reports_a_full_disk_as_a_warning_not_a_failure(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} WARN=${{#BASE_WARN[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "0", "BASE_DRY": "1"})
    assert "FAILED=0 WARN=1" in r.stdout and "would stop here" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- plan 3 / T1: the launched server keeps its Hugging Face cache on the volume

def test_unit_launch_line_puts_the_hf_cache_on_the_volume(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd; echo; echo "HOME_ENV=${HF_HOME:-unset}"', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PERSIST_ROOT_FORCE": "/workspace"})
    assert r.stdout.startswith("env HF_HOME=/workspace/huggingface ")   # env: the line is printed after nohup (2.0.11) and "main.py" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "HF_HOME" not in r.stdout                                  # off the pod there is no volume to prefer
    src = code_only((BASE / "lib" / "85-launch.sh").read_text())            # the launch line lives in 85-launch.sh (shared with boot.sh)
    assert "HF_HOME" in src[src.index("_base_start_comfy()"):]


def test_unit_sync_treats_declared_placeholders_as_intentional(tmp_path):
    c = _pod(tmp_path); (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "LoraLoaderModelOnly", ["Example/Subject/PUT-YOUR-TRAINED-LORA-HERE.safetensors", 1.0]),
                                                              _node(2, "VAELoader", ["really_missing.safetensors"])])))
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); PLACEHOLDERS=(PUT-YOUR-TRAINED-LORA-HERE.safetensors); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env=env)
    assert "PLACEHOLDER" in r.stdout and "placeholders=1" in r.stdout and "active_missing=1" in r.stdout and "FAILED=1" in r.stdout, r.stdout + r.stderr
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "LoraLoaderModelOnly", ["Example/Subject/PUT-YOUR-TRAINED-LORA-HERE.safetensors", 1.0])])))
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); PLACEHOLDERS=(PUT-YOUR-TRAINED-LORA-HERE.safetensors); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env=env)
    assert "FAILED=0" in r.stdout and "placeholders=1" in r.stdout and "PLACEHOLDER" in r.stdout, r.stdout + r.stderr


def test_unit_sync_checks_a_local_labelled_value_without_rewriting_it(tmp_path):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "AILab_QwenVL_GGUF_PromptEnhancer", ["[local] Qwen3-4B-instruct-bf16.gguf", "prompt"]),
                                    _node(2, "AILab_QwenVL_GGUF_PromptEnhancer", ["[local] nowhere.gguf", "prompt"])])))
    rows = tmp_path / "rows.tsv"; rows.write_text("LLM\t\tGGUF\tQwen3-4B-instruct-bf16.gguf\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("AILab_QwenVL_GGUF_PromptEnhancer\tLLM\n"); (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    out = json.loads(wf.read_text())
    assert out["nodes"][0]["widgets_values"][0] == "[local] Qwen3-4B-instruct-bf16.gguf"     # a pack label, not a path: untouched
    assert "SYNC fixed=0 active_missing=1" in r.stdout and "nowhere.gguf" in r.stdout, r.stdout


# ---------------------------------------------------------------- plan 5: snapshots, flat embeddings, extras on every run
def test_unit_dir_size_ignores_the_download_cache(tmp_path):
    """`hf download --local-dir` writes .cache/huggingface/ beside the files; a snapshot verified by total size must not count it."""
    d = tmp_path / "snap"; (d / ".cache" / "huggingface").mkdir(parents=True); (d / "sub").mkdir()
    (d / "a.bin").write_bytes(b"1234"); (d / "sub" / "b.bin").write_bytes(b"56"); (d / ".cache" / "huggingface" / "meta").write_bytes(b"xyz")
    # a pack that imports code from its snapshot (ComfyUI-RMBG's birefnet.py) leaves __pycache__ behind after the first
    # render; the byte total then no longer equals the declared size and the base re-fetched a complete snapshot (2.0.25)
    (d / "__pycache__").mkdir(); (d / "__pycache__" / "birefnet.cpython-314.pyc").write_bytes(b"pyc-bytes"); (d / "sub" / "__pycache__").mkdir(); (d / "sub" / "__pycache__" / "x.pyc").write_bytes(b"!")
    assert _bash(f'_base_dir_size "{d}"').stdout.strip() == "6"
    assert _bash('_base_dir_size /nonexistent').stdout.strip() == "0"


def test_unit_embeddings_are_a_flat_freeform_category():
    """ComfyUI resolves `embedding:<name>` in the embeddings folder root only (sd1_clip.load_embed joins dir + name), so no Family."""
    row = "embeddings|||example_x.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|512|style|"
    assert "rc=0" in _bash(f'MODELS=("{row}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    assert _bash(f'_base_dest_rel "{row}"').stdout.strip() == "embeddings/example_x.safetensors"
    r = _bash('MODELS=("embeddings|Example-V||e.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|5|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr
    # the packs that os.listdir their folder (SAM3 SmartInpainter, DepthAnythingV2, SeedVR2, ComfyUI-RMBG's models/RMBG/<name>/,
    # LayerStyle's models/grounding-dino/) get flat categories too (RMBG, grounding-dino: 2.0.17)
    # ultralytics/bbox and /segm (3.20.0): the face-detector packs join models/ultralytics/bbox/<name> themselves
    for cat in ("sams", "depthanything", "SEEDVR2", "RMBG", "grounding-dino", "ultralytics/bbox", "ultralytics/segm"):
        assert "rc=0" in _bash(f'MODELS=("{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"); _base_model_rows >/dev/null; echo rc=$?').stdout, cat
        assert _bash(f'_base_dest_rel "{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"').stdout.strip() == f"{cat}/m.pt"
    assert _bash('_base_dest_rel "RMBG|||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"').stdout.strip() == "RMBG/RMBG-2.0/"
    r = _bash('MODELS=("RMBG|RMBG||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "RMBG with a Family must be refused: the pack reads models/RMBG/<name>/"
    r = _bash('MODELS=("ultralytics/bbox|Face||face_yolov8m.pt|https://huggingface.co/x/y/resolve/main/f.pt|9|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "a detector with a Family must be refused: the packs read models/ultralytics/bbox/<name>"


SNAP_ROW = "LLM||Qwen-VL|Qwen3-VL-32B-Std-v2/|LOCAL|0|the describers' vision LLM; its Hub repo is gone|"



def test_unit_latent_upscale_models_is_a_flat_freeform_category():
    """ComfyUI resolves `embedding:<name>` in the latent_upscale_models folder root only (sd1_clip.load_embed joins dir + name), so no Family."""
    row = "latent_upscale_models|||example_x.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|512|style|"
    assert "rc=0" in _bash(f'MODELS=("{row}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    assert _bash(f'_base_dest_rel "{row}"').stdout.strip() == "latent_upscale_models/example_x.safetensors"
    r = _bash('MODELS=("latent_upscale_models|Example-V||e.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|5|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr
    # the packs that os.listdir their folder (SAM3 SmartInpainter, DepthAnythingV2, SeedVR2, ComfyUI-RMBG's models/RMBG/<name>/,
    # LayerStyle's models/grounding-dino/) get flat categories too (RMBG, grounding-dino: 2.0.17)
    # ultralytics/bbox and /segm (3.20.0): the face-detector packs join models/ultralytics/bbox/<name> themselves
    for cat in ("sams", "depthanything", "SEEDVR2", "RMBG", "grounding-dino", "ultralytics/bbox", "ultralytics/segm"):
        assert "rc=0" in _bash(f'MODELS=("{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"); _base_model_rows >/dev/null; echo rc=$?').stdout, cat
        assert _bash(f'_base_dest_rel "{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"').stdout.strip() == f"{cat}/m.pt"
    assert _bash('_base_dest_rel "RMBG|||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"').stdout.strip() == "RMBG/RMBG-2.0/"
    r = _bash('MODELS=("RMBG|RMBG||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "RMBG with a Family must be refused: the pack reads models/RMBG/<name>/"
    r = _bash('MODELS=("ultralytics/bbox|Face||face_yolov8m.pt|https://huggingface.co/x/y/resolve/main/f.pt|9|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "a detector with a Family must be refused: the packs read models/ultralytics/bbox/<name>"


SNAP_ROW = "LLM||Qwen-VL|Qwen3-VL-32B-Std-v2/|LOCAL|0|the describers' vision LLM; its Hub repo is gone|"

def test_unit_a_local_snapshot_folder_is_relocated_from_anywhere_on_the_pod(tmp_path):
    """A LOCAL row whose file ends in / is a FOLDER: found by name (holding a config.json) through the one directory scan, moved into place."""
    c = _pod(tmp_path); src = tmp_path / "old-install" / "models" / "LLM" / "Qwen3-VL-32B-Std-v2"; src.mkdir(parents=True)
    (src / "config.json").write_text("{}"); (src / "model-00001.safetensors").write_bytes(b"\0" * 4096)
    r = _bash(f'MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} MOVED=${{#MODEL_MOVED[@]}}; ls "$M/LLM/Qwen-VL/Qwen3-VL-32B-Std-v2"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0 MOVED=1" in r.stdout and "config.json" in r.stdout and not src.exists(), r.stdout + r.stderr
    # second run: in place, verified by the config.json, nothing moved
    r = _bash(f'MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} MOVED=${{#MODEL_MOVED[@]}} OK=${{#MODEL_OK[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0 MOVED=0 OK=1" in r.stdout, r.stdout + r.stderr


def test_unit_a_missing_local_snapshot_fails_the_run_by_name_and_check_only_reports(tmp_path):
    c = _pod(tmp_path); (tmp_path / "decoy" / "Qwen3-VL-32B-Std-v2").mkdir(parents=True)     # a folder of that name WITHOUT config.json is not it
    for dry in ("", "BASE_DRY=1;"):
        r = _bash(f'{dry} MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; printf "%s\\n" "${{BASE_FAILED[@]}}"',
                  env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
        assert "FAILED=1" in r.stdout and "Qwen3-VL-32B-Std-v2" in r.stdout and "LLM/Qwen-VL" in r.stdout, r.stdout + r.stderr
    assert not (c / "models" / "LLM").exists()


def test_unit_the_launch_line_names_a_system_ffmpeg_only_when_it_has_nvenc(tmp_path):
    """VideoHelperSuite prefers its own bundled ffmpeg, and that build has NO nvenc encoders, so a package whose
    video format asks for h264_nvenc dies at the LAST node, after the sample is computed and paid for. Measured on
    a GPU 2026-09-19. VHS honours VHS_FORCE_FFMPEG_PATH, so the launch line names a real ffmpeg when one is there.

    Only when it HAS nvenc: forcing a system build without it trades one silently wrong encoder for another, and
    the bundled one at least works for the stock formats. Both halves are here so neither can drift.

    The "no ffmpeg anywhere" case is not tested: isolating it needs a PATH with no ffmpeg on it, and a PATH that
    short cannot find `bash` either, so the test would measure its own harness. `[ -n "$f" ] || return 0` is the
    whole of that branch.
    """
    bindir = tmp_path / "bin"; bindir.mkdir()
    path = "%s:/usr/bin:/bin:/usr/sbin:/sbin" % bindir
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "PATH": path}

    def _ffmpeg(nvenc):
        f = bindir / "ffmpeg"
        f.write_text("#!/bin/sh\necho '%s'\n" % ("V....D h264_nvenc NVIDIA NVENC H.264 encoder"
                                                  if nvenc else "V....D libx264 libx264 H.264"))
        f.chmod(0o755)

    _ffmpeg(True)
    r = _bash('base_env_setup; base_discover quiet; _base_ffmpeg_path', env=env)
    assert str(bindir / "ffmpeg") in r.stdout, "an ffmpeg WITH nvenc must be named: " + r.stdout + r.stderr

    _ffmpeg(False)
    r = _bash('base_env_setup; base_discover quiet; _base_ffmpeg_path; echo END', env=env)
    assert str(bindir / "ffmpeg") not in r.stdout, "an ffmpeg WITHOUT nvenc must NOT be forced: " + r.stdout
    assert "END" in r.stdout, "and it must not fail the run either: " + r.stdout + r.stderr


def test_unit_pip_extra_is_installed_every_run_not_only_on_a_rebuild():
    """A venv the base built for another package (or before any package) has never seen this package's PIP_EXTRA."""
    body = code_only(_src(BASE / "lib" / "40-packs.sh"))
    pip_half = body[body.index('hdr "NODE PACKS · requirements'):]
    assert "_base_reqlift" in pip_half and "_base_derived_install" in pip_half, "the pip stage installs the derived set every run"
    assert "BASE_DRY" in pip_half and "BASE_NO_NET" in pip_half, "dry-run and fake runs must not pip"
    latest = code_only(_src(BASE / "lib" / "47-latest.sh"))
    lift = latest[latest.index("_base_reqlift(){"):latest.index("_base_lift_who(){")]
    assert 'PIP_EXTRA[@]' in lift and "https://pypi.nvidia.com" in latest, "PIP_EXTRA goes into the derived set, NVIDIA's index with it"


def test_unit_sync_rewrites_a_bypassed_loader_whose_file_has_a_row_and_keeps_a_compact_file(tmp_path):
    """A mode radio ships every option but one bypassed (quality tiers, engine variants); the user flips them later, so a
    bypassed loader's path must be canonical too. Only a name that resolves to NOTHING is treated by mode. And a one-line
    workflow stays one line."""
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["example_engine_pruned_int8.safetensors", "default"]),
                                    _node(2, "UNETLoader", ["example_engine_bf16.safetensors", "default"], mode=4),
                                    _node(3, "LoraLoaderModelOnly", ["Other/turbo.safetensors", 1], mode=4)]), ensure_ascii=False) + "\n")
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample-V\t\texample_engine_pruned_int8.safetensors\ndiffusion_models\tExample-V\t\texample_engine_bf16.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nLoraLoaderModelOnly\tloras\n"); (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    assert r.returncode == 0, r.stdout + r.stderr
    text = wf.read_text(); out = json.loads(text)
    assert out["nodes"][1]["widgets_values"][0] == "Example-V/example_engine_bf16.safetensors", "the bypassed Master engine must be rewritten"
    assert out["nodes"][2]["widgets_values"][0] == "Other/turbo.safetensors", "no row: untouched"
    assert "SYNC fixed=2 active_missing=0 bypassed_missing=1" in r.stdout, r.stdout
    assert text.count("\n") == 1 and text.startswith('{"'), "a compact workflow must stay compact"


# ---------------------------------------------------------------- T11: the three fake pod layouts (base V2.0.0)

def test_unit_fake_pod_layouts_official_community_bare(tmp_path):
    """official = RunPod's ComfyUI image (tree on the volume, template venv, args file); community = code on the
    container disk (<root>/image/ComfyUI), persist dirs on the volume (<root>/ComfyUI without main.py); bare = an
    empty volume. Every layout carries PID 1's env in <root>/pid1.env and run_script hands both facts to the script."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    off = fake_pod(tmp_path / "off", STUB, layout="official")
    assert (off / "main.py").exists() and (off / ".venv-cu130" / "bin" / "python").exists() and (tmp_path / "off" / "runpod-slim" / "comfyui_args.txt").exists()
    hm = fake_pod(tmp_path / "hm", STUB, layout="community")
    assert hm == tmp_path / "hm" / "ComfyUI" and not (hm / "main.py").exists()
    for d in ("models", "user", "output", "input", "custom_nodes"):
        assert (hm / d).is_dir()
    img = tmp_path / "hm" / "image"
    assert (img / "ComfyUI" / "main.py").exists() and (img / "comfyui-runtime" / "src" / "start.sh").exists()
    assert (img / "comfyui-template" / "template.json").exists() and (img / "opt" / "venv" / "bin" / "python3").exists()
    assert (hm / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors").exists()   # the template's own file, unclaimed by any row
    bare = fake_pod(tmp_path / "bare", STUB, layout="bare")
    assert bare == tmp_path / "bare" / "ComfyUI" and not bare.exists()
    for root in (tmp_path / "off", tmp_path / "hm", tmp_path / "bare"):
        env = (root / "pid1.env").read_text()
        assert "PUBLIC_KEY=" in env and "HF_TOKEN=token_here" in env and "SOME_IDS=" in env and "JUPYTER_PASSWORD=" in env
        probe = root / "pkg" / "probe.sh"
        probe.write_text('#!/bin/bash\necho "PID1=$BASE_FAKE_PID1_ENV IMG=${BASE_FAKE_IMAGE_ROOT:-none}"\n'); probe.chmod(0o755)
        r = run_script(probe, fake_root=root)
        assert r.returncode == 0 and ("PID1=%s" % (root / "pid1.env")) in r.stdout, r.stdout + r.stderr
        assert ("IMG=%s" % (root / "image")) in r.stdout if root.name == "hm" else "IMG=none" in r.stdout


def test_unit_fake_pod_carries_every_file_zip_extra_ships(tmp_path):
    """The fake machine's <root>/pkg is what the zip unpacks to: top-level files, vendored packs, and every ZIP_EXTRA path,
    a directory that is not a node pack included. The Video Replacer's app/ (no __init__.py) was missing, so its install
    test copied it by hand (2026-10-16). What ZIP_EXTRA does not list stays out, as it does in the zip; a listed file that
    is not there is an error, never a silent skip."""
    from basetest import fake_pod, zip_extra
    pkg = tmp_path / "toy-pkg"; (pkg / "app" / "prompts").mkdir(parents=True); (pkg / "Spaced Dir").mkdir()
    (pkg / "toy-pkg-script.sh").write_text('#!/usr/bin/env bash\nZIP_EXTRA=( "app/index.html" app/prompts/fast.api.json\n "Spaced Dir/a b.txt" )\n')
    (pkg / "app" / "index.html").write_text("<p>"); (pkg / "app" / "prompts" / "fast.api.json").write_text("{}")
    (pkg / "Spaced Dir" / "a b.txt").write_text("x"); (pkg / "app" / "draft.txt").write_text("not shipped")
    assert zip_extra(pkg) == ["app/index.html", "app/prompts/fast.api.json", "Spaced Dir/a b.txt"]
    fake_pod(tmp_path / "pod", pkg, layout="bare")
    got = tmp_path / "pod" / "pkg"
    assert (got / "app" / "index.html").read_text() == "<p>" and (got / "app" / "prompts" / "fast.api.json").exists()
    assert (got / "Spaced Dir" / "a b.txt").exists() and (got / "toy-pkg-script.sh").exists()
    assert not (got / "app" / "draft.txt").exists(), "a file ZIP_EXTRA does not list is not in the zip, so not on the fake machine"
    (pkg / "app" / "index.html").unlink()
    with pytest.raises(FileNotFoundError, match="app/index.html"):
        fake_pod(tmp_path / "pod2", pkg, layout="bare")


# ---------------------------------------------------------------- T12: discovery on the volume only (base V2.0.0)

def test_unit_discover_never_adopts_a_container_disk_tree(tmp_path):
    """community layout: the only main.py is on the container disk (<root>/image/ComfyUI). --check must not adopt it
    and must say what a real run does: materialise the tree beside the persist dirs on the volume."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="community")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "would materialise" in r.stdout and str(pod) in r.stdout
    assert str(tmp_path / "image" / "ComfyUI") not in r.stdout.split("would materialise")[0]   # never a candidate
    assert not (pod / "main.py").exists()
    # a real run puts the code beside the persist dirs and leaves them untouched
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert (pod / "main.py").exists() and (pod / "comfyui_version.py").exists()
    assert (pod / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors").exists()
    assert ("tree         %s" % pod) in r.stdout or ("ComfyUI      %s" % pod) in r.stdout or str(pod) in r.stdout


def test_unit_discover_records_the_canonical_tree_and_prefers_it_over_a_second_one(tmp_path):
    """Two trees on one volume (a volume that met two templates): the one recorded in state/boot.env is canonical,
    the other is reported, never deleted."""
    from basetest import fake_pod, run_script, _fake_code_tree
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")                          # <root>/ComfyUI
    second = tmp_path / "runpod-slim" / "ComfyUI"; _fake_code_tree(second, "0.34.7")
    state = tmp_path / "comfy-base" / "state"; state.mkdir(parents=True, exist_ok=True)
    (state / "boot.env").write_text("COMFY=%s\n" % second)
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert ("ComfyUI      %s" % second) in r.stdout or ("tree         %s" % second) in r.stdout, r.stdout[:2500]
    assert "other tree" in r.stdout and str(pod) in r.stdout.split("other tree")[1][:300]
    assert (pod / "main.py").exists() and (second / "main.py").exists()


def test_unit_a_pod_without_a_network_volume_is_refused(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path, env={"BASE_FAKE_NO_VOLUME": "1"})
    assert r.returncode == 3, r.stdout[-2000:] + r.stderr[-1000:]
    assert "network volume" in r.stdout + r.stderr and "stop/start" in r.stdout + r.stderr


# ---------------------------------------------------------------- T13: tokens (base V2.0.0)

def _stub_with_github_row(root):
    """The copied stub gains one model served by GitHub (a release asset), so the curl leg is exercised."""
    sc = root / "pkg" / "stub-script.sh"; s = sc.read_text()
    s = s.replace('MODELS=(\n', 'MODELS=(\n "loras|Example|Real|stub_gh.safetensors|https://github.com/x/y/releases/download/v1/stub_gh.safetensors|2048|a release asset|"\n', 1)
    assert "stub_gh" in s
    sc.write_text(s)


def _run_logs(root):
    return [p.read_text(errors="replace") for p in (root / "comfy-base" / "state" / "logs").glob("*.log")]


def test_unit_tokens_come_from_pid1_env_with_aliases_and_placeholders_are_named_not_used(tmp_path):
    """pid1.env carries HF_TOKEN=token_here (a placeholder) and HF_HUB_TOKEN=abc-secret (an alias other templates use).
    The placeholder is named and skipped; the alias becomes HF_TOKEN; no value ever reaches stdout or the log."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    with open(tmp_path / "pid1.env", "a") as f: f.write("HF_HUB_TOKEN=abc-secret\n")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "HF_TOKEN" in r.stdout and "placeholder" in r.stdout and "token_here" in r.stdout
    assert "HF_TOKEN present (pod env: HF_HUB_TOKEN)" in r.stdout
    assert "abc-secret" not in r.stdout + r.stderr and not any("abc-secret" in l for l in _run_logs(tmp_path))
    tokens = tmp_path / "comfy-base" / "state" / "tokens.env"
    assert not tokens.exists()                                             # --check stores nothing
    r2 = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r2.returncode == 0, r2.stdout[-3000:] + r2.stderr[-1000:]
    assert tokens.read_text() == "HF_TOKEN=abc-secret\n" and (tokens.stat().st_mode & 0o777) == 0o600
    assert "abc-secret" not in r2.stdout + r2.stderr and not any("abc-secret" in l for l in _run_logs(tmp_path))


def test_unit_token_rejected_by_its_service_fails_the_run_before_models(tmp_path):
    """A token its service answers 401 to fails the run before any model is fetched, naming the SOURCE, never the value."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from basetest import fake_pod, run_script
    STUB = _stub()
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(401); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pod = fake_pod(tmp_path, STUB, layout="official")
        r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                       env={"HF_TOKEN": "hf_real_looking_value", "BASE_TOKEN_CHECK_URL_HF": "http://127.0.0.1:%d/whoami" % srv.server_address[1]})
    finally:
        srv.shutdown(); srv.server_close()
    assert r.returncode != 0, r.stdout[-3000:]
    assert "HF_TOKEN" in r.stdout and "rejected" in r.stdout and "401" in r.stdout and "(env)" in r.stdout
    assert not (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists()      # nothing was fetched
    assert "hf_real_looking_value" not in r.stdout + r.stderr and not any("hf_real_looking_value" in l for l in _run_logs(tmp_path))


def test_unit_hf_gets_the_token_off_the_process_list(tmp_path):
    """hf sees HF_TOKEN in its environment, never --token. BASE_FAKE_DL=real routes the fake run through the real
    download branches with a stubbed hf on PATH."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf argv: $*" >> "%s"; echo "hf env HF_TOKEN=${HF_TOKEN:-unset}" >> "%s"\n'
                             'd=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
                             'mkdir -p "$d"; python3 -c "open(\\"$d/stub_vae.safetensors\\",\\"wb\\").truncate(4096)"\n' % (log, log))
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl argv: $*" >> "%s"\nexit 0\n' % log)
    for p in (bin_ / "hf", bin_ / "curl"): p.chmod(0o755)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                   env={"PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_FAKE_DL": "real", "HF_TOKEN": "hf_env_value"})
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    calls = log.read_text()
    assert "hf env HF_TOKEN=hf_env_value" in calls and "--token" not in calls and "hf_env_value" not in "".join(l for l in calls.splitlines() if "argv" in l)
    assert (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").stat().st_size == 4096
    assert "hf_env_value" not in r.stdout + r.stderr and not any("hf_env_value" in l for l in _run_logs(tmp_path))


# ---------------------------------------------------------------- T14: the base owns the launch line (base V2.0.0)

@pytest.mark.parametrize("layout", ["official", "community", "bare", "volume"])
def test_unit_args_file_is_base_owned_on_every_layout_and_imports_the_official_file_once(tmp_path, layout):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout=layout)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    b = _base_args(tmp_path); txt = b.read_text()
    assert txt.startswith("#") and "--preview-method auto" in txt and "--preview-size 1024" in txt and "--disable-api-nodes" in txt
    assert "--use-sage-attention" not in txt
    if layout == "official":
        assert "imported" in r.stdout and (tmp_path / "runpod-slim" / "comfyui_args.txt").read_text() == "# template args\n--use-sage-attention\n"
    else:
        assert not (tmp_path / "runpod-slim").exists()                                        # never a phantom
    r2 = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r2.returncode == 0 and b.read_text() == txt and "imported" not in r2.stdout.split("SUMMARY")[0]


def test_unit_hygiene_remove_entries_never_reappear(tmp_path):
    """After the one-time import the template's file has no say: a flag that re-appears there (an image bump) does
    not come back, and HYGIENE_ARGS_REMOVE keeps the base file clean of it on every run."""
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--fast\n")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    _bash('base_env_setup; base_discover quiet; base_hygiene', env=env)
    a.write_text("--fast\n--use-sage-attention\n--evil\n")
    r = _bash('HYGIENE_ARGS_REMOVE=(--evil); base_env_setup; base_discover quiet; base_hygiene; _base_start_cmd', env=env)
    txt = _base_args(tmp_path).read_text()
    assert "--use-sage-attention" not in txt and "--evil" not in txt and "--fast" in txt, txt + r.stdout
    assert "--evil" not in r.stdout.splitlines()[-1] and "--fast" in r.stdout.splitlines()[-1]


def test_unit_launch_line_is_composed_from_the_args_file_only(tmp_path):
    c = _pod(tmp_path)
    b = _base_args(tmp_path); b.parent.mkdir(parents=True)
    b.write_text("# comfy-base launch args\n--preview-method taesd\n--fast\n")
    (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 9999\n--evil\n")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd; echo; echo PORT=$PORT', env={"BASE_FAKE_ROOT": str(tmp_path)})
    line = r.stdout.splitlines()[0]
    assert "--fast" in line and "taesd" in line and line.count("--preview-method") == 1 and "--preview-size" in line, line
    assert "--evil" not in line and "--port 8188" in line and "PORT=8188" in r.stdout                # the template's file is not consulted once the base's exists


def test_unit_no_phantom_runpod_slim_is_ever_created(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    for layout in ("community", "bare"):
        root = tmp_path / layout; fake_pod(root, STUB, layout=layout)
        r = run_script(STUB / "stub-script.sh", fake_root=root)
        assert r.returncode == 0, r.stdout[-3000:]
        assert not (root / "runpod-slim").exists() and (root / "comfy-base" / "state" / "comfyui_args.txt").exists()


# ---------------------------------------------------------------- T15: the base boots the pod (base V2.0.0)

def _boot_stubs(tmp_path, torch_ok=True):
    from basetest import boot_stubs
    return boot_stubs(tmp_path, torch_ok=torch_ok)


def _run_boot_sh(tmp_path, bin_, extra_env=None):
    from basetest import boot_fake_pod
    return boot_fake_pod(tmp_path, bin_, extra_env)


def test_unit_boot_env_is_written_by_every_run_and_read_by_boot_sh(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0 and not (tmp_path / "comfy-base" / "state" / "boot.env").exists() and "would" in r.stdout
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    env = (tmp_path / "comfy-base" / "state" / "boot.env").read_text()
    assert ("COMFY='%s'" % pod) in env and ("VENV='%s'" % (pod / ".venv-cu130")) in env and "PORT='8188'" in env
    assert ("ARGS_FILE='%s'" % (tmp_path / "comfy-base" / "state" / "comfyui_args.txt")) in env
    assert (tmp_path / "comfy-base" / "boot.sh").exists() and (tmp_path / "comfy-base" / "lib" / "85-launch.sh").exists()
    assert "boot       " in r.stdout and "tools" in r.stdout.split("boot       ")[1].split("\n")[0]


def test_unit_boot_sh_starts_sshd_then_jupyter_then_comfyui_and_never_exits(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    r, blog = _run_boot_sh(tmp_path, bin_, {"JUPYTER_PASSWORD": "pw-secret"})
    assert r.returncode == 0, r.stderr + blog
    calls = log.read_text()
    # the boot log is written in order (JupyterLab is backgrounded, so its stub's line may land after ComfyUI's)
    assert blog.index("boot: sshd up") < blog.index("boot: JupyterLab starting") < blog.index("boot: starting ComfyUI"), blog
    assert calls.splitlines()[0].startswith(("ssh-keygen", "sshd", "pgrep")) and "jupyter-lab " in calls and "python main.py" in calls, calls
    assert "JUPYTER_TOKEN=pw-secret" in calls and "--ServerApp.token" not in calls and "pw-secret" not in blog     # env, not argv; never logged
    launch = [ln for ln in calls.splitlines() if ln.startswith("python main.py")][0]
    want = _bash("base_env_setup; base_discover quiet; _base_start_cmd", env={"BASE_FAKE_ROOT": str(tmp_path)}).stdout.strip()
    assert launch == "python " + want.split(" ", 1)[1], (launch, want)                        # the SAME line the base's restart uses
    assert "--preview-method auto" in launch and "--disable-api-nodes" in launch and "--listen 127.0.0.1" in launch
    assert "boot: done" in blog and "sleeping" in blog
    # sleep infinity only when not BOOT_ONCE: the script's last statement
    src = (BASE / "lib" / "boot.sh").read_text()
    assert "sleep infinity" in src and "trap 'exit 0' TERM" in src


def test_unit_boot_sh_refuses_to_start_a_venv_that_cannot_import_torch_and_says_so(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path, torch_ok=False)
    r, blog = _run_boot_sh(tmp_path, bin_)
    assert r.returncode == 0 and "cannot import torch" in blog and "rescue" in blog
    assert "main.py" not in log.read_text()
    # and with no boot.env at all (a volume the base never installed on): sshd and jupyter still come up
    (tmp_path / "comfy-base" / "state" / "boot.env").unlink()
    r, blog = _run_boot_sh(tmp_path, bin_)
    assert r.returncode == 0 and "no " in blog and "boot.env" in blog and "sshd" in log.read_text()


def test_unit_boot_sh_never_starts_jupyter_tokenless_and_no_sshd_without_a_key(tmp_path):
    """2.1.0: a stranger's pod from the public template sets neither JUPYTER_TOKEN nor PUBLIC_KEY (RunPod's toggles exist only
    on official templates, distribution plan blocker 2). The old tokenless start was root on the box.
    3.9.0: the terminal is one of the pod's named links now, so the boot GENERATES its token even on RunPod's public bind,
    and the one place it names it is the container log (the pod's Logs tab, the owner's): never the boot log, never argv."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    # 3.19.1: on RunPod's public bind this holds for a LOCKED pod (BASE_LINK_LOCK=1); an open one is test_ports'
    r, blog = _run_boot_sh(tmp_path, bin_, {"BASE_LINK_LOCK": "1"})   # PUBLIC_KEY set by the helper, no JUPYTER_*
    tf = tmp_path / "comfy-base" / "state" / "tokens.env"
    tok = [l.split("=", 1)[1] for l in tf.read_text().splitlines() if l.startswith("JUPYTER_TOKEN=")][-1]
    assert r.returncode == 0 and "JupyterLab starting on 8888" in blog and "token generated" in blog, blog
    calls = log.read_text()
    assert ("JUPYTER_TOKEN=%s" % tok) in calls and "--IdentityProvider.token=" not in calls and "--ServerApp.token" not in calls
    assert tok not in blog, "the generated token was written to the boot log on the volume"
    assert ("Jupyter Terminal token: %s" % tok) in r.stdout and "fakepod-8888.proxy.runpod.net/lab?token=" in r.stdout, r.stdout
    assert "python main.py" in log.read_text()                    # ComfyUI still comes up
    src = code_only((BASE / "lib" / "boot.sh").read_text())
    # 3.19.1: the one tokenless start is the open public bind, behind `boot_public && ! boot_link_locked`, and never on loopback
    assert src.count("--IdentityProvider.token=") == 1 and "boot_public && ! boot_link_locked" in src
    # no PUBLIC_KEY: no sshd at all (a listener nobody can log in to is a port with no purpose)
    (tmp_path / "sshd.running").unlink(missing_ok=True); log.write_text("")
    r, blog = _run_boot_sh(tmp_path, bin_, {"PUBLIC_KEY": ""})
    assert r.returncode == 0 and "sshd not started" in blog and "PUBLIC_KEY" in blog, blog
    assert not [ln for ln in log.read_text().splitlines() if ln.startswith("sshd ")], log.read_text()
    assert "python main.py" in log.read_text()


def test_unit_boot_sh_exports_the_button_names_and_loads_saved_tokens_without_logging_values(tmp_path):
    """2.1.0: boot_tokens — state/tokens.env joins the pod's own environment (the pod's value wins), and the Hub browser
    pack gets its own name; no value ever reaches the boot log or the ComfyUI stub's argv."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
    (st / "tokens.env").write_text("HF_TOKEN=hf_saved_value\nOTHER_SAVED=other_saved_value\n")
    bin_, log = _boot_stubs(tmp_path)
    # the python stub records its environment for this test only
    py = tmp_path / "ComfyUI" / ".venv-cu130" / "bin" / "python"
    py.write_text(py.read_text().replace('echo "python $*"', 'echo "python $* ENV hf=${HUGGINGFACE_API_KEY-unset} oth=${OTHER_SAVED-unset} tok=${HF_TOKEN-unset}"'))
    r, blog = _run_boot_sh(tmp_path, bin_, {"HF_TOKEN": "hf_pod_value"})
    assert r.returncode == 0, r.stderr + blog
    assert "boot: tokens" in blog and "HUGGINGFACE_API_KEY" in blog, blog
    assert "hf_pod_value" not in blog and "other_saved_value" not in blog and "hf_saved_value" not in blog, blog
    launch = [ln for ln in log.read_text().splitlines() if ln.startswith("python main.py")][0]
    assert "hf=hf_pod_value" in launch and "tok=hf_pod_value" in launch and "oth=other_saved_value" in launch, launch   # the pod's HF_TOKEN won; the other saved one loaded


def test_unit_an_unresolved_runpod_secret_reference_is_a_placeholder_and_civitai_aliases_are_read(tmp_path):
    """3.19.0: RunPod leaves "{{ RUNPOD_SECRET_HF_TOKEN }}" in the pod's env as it was when the account has no secret of
    that name. The install names it as a placeholder and ignores it, like token_here (it is a reference, never a value),
    and a package's CIVITAI_TOKEN is read under the names other tools use too (CIVITAI_API_KEY, CIVITAI_API_TOKEN)."""
    r = _bash('for v in "{{ RUNPOD_SECRET_HF_TOKEN }}" "{{RUNPOD_SECRET_X}}" token_here "" hf_real_value; do '
              'if _base_token_is_placeholder "$v"; then echo "P:$v"; else echo "R:$v"; fi; done; '
              '_base_token_aliases CIVITAI_TOKEN; _base_token_aliases HF_TOKEN')
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["P:{{ RUNPOD_SECRET_HF_TOKEN }}", "P:{{RUNPOD_SECRET_X}}", "P:token_here", "P:", "R:hf_real_value",
                                     "CIVITAI_TOKEN CIVITAI_API_KEY CIVITAI_API_TOKEN", "HF_TOKEN HUGGING_FACE_HUB_TOKEN HF_HUB_TOKEN"], r.stdout
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    sc = tmp_path / "pkg" / "stub-script.sh"; src = sc.read_text()
    row = 'TOKENS=( "HF_TOKEN|optional|lifts anonymous rate limits" )'
    assert row in src
    sc.write_text(src.replace(row, 'TOKENS=( "HF_TOKEN|optional|lifts anonymous rate limits" "CIVITAI_TOKEN|optional|Civitai downloads" )'))
    pid1 = tmp_path / "pid1.env"
    pid1.write_text(pid1.read_text().replace("HF_TOKEN=token_here", "HF_TOKEN={{ RUNPOD_SECRET_HF_TOKEN }}") + "CIVITAI_API_KEY=civ-fake\n")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "is the placeholder '{{ RUNPOD_SECRET_HF_TOKEN }}'" in r.stdout and "HF_TOKEN present" not in r.stdout, r.stdout[-3000:]
    assert "CIVITAI_TOKEN present (pod env: CIVITAI_API_KEY)" in r.stdout, r.stdout[-3000:]
    assert "civ-fake" not in r.stdout + r.stderr and not any("civ-fake" in l for l in _run_logs(tmp_path))


def test_unit_boot_sh_drops_an_unresolved_runpod_secret_reference_so_the_saved_token_applies(tmp_path):
    """3.19.0: the boot's twin of the rule above. A pod whose env says HF_TOKEN={{ RUNPOD_SECRET_HF_TOKEN }} (unresolved)
    starts ComfyUI with the token an earlier install saved, never with the literal, and says which names it dropped."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
    (st / "tokens.env").write_text("HF_TOKEN=hf_saved_value\n")
    bin_, log = _boot_stubs(tmp_path)
    py = tmp_path / "ComfyUI" / ".venv-cu130" / "bin" / "python"
    py.write_text(py.read_text().replace('echo "python $*"', 'echo "python $* ENV hf=${HUGGINGFACE_API_KEY-unset} tok=${HF_TOKEN-unset} civ=${CIVITAI_API_KEY-unset}"'))
    r, blog = _run_boot_sh(tmp_path, bin_, {"HF_TOKEN": "{{ RUNPOD_SECRET_HF_TOKEN }}", "CIVITAI_API_KEY": "{{ RUNPOD_SECRET_CIVITAI_TOKEN }}"})
    assert r.returncode == 0, r.stderr + blog
    assert "unresolved RunPod Secret references ignored (placeholders, not tokens): HF_TOKEN CIVITAI_API_KEY" in blog, blog
    assert "hf_saved_value" not in blog, blog
    launch = [ln for ln in log.read_text().splitlines() if ln.startswith("python main.py")][0]
    assert "tok=hf_saved_value" in launch and "hf=hf_saved_value" in launch and "civ=unset" in launch and "{{" not in launch, launch


# ---------------------------------------------------------------- T16: restart finds ComfyUI by what it runs (base V2.0.0)

def test_unit_comfy_pids_match_by_argv_or_cwd_and_never_pid1(tmp_path):
    """_base_pids_from_table reads `pid|ppid|cwd|argv` lines (what /proc gives on the pod) and prints the pids of
    ComfyUI servers: a python running main.py from $COMFY (by cwd), or one naming a …/ComfyUI/main.py path (an image's
    own launch, whatever its cwd). PID 1 is never returned, only reported."""
    comfy = tmp_path / "ComfyUI"; comfy.mkdir()
    table = "\n".join([
        "1|0|/|bash /comfyui-runtime/src/start.sh /comfyui-template",
        "100|1|/|python3 /ComfyUI/main.py --listen --enable-cors-header *",         # the image's launch: argv names the tree
        "200|50|%s|python main.py --listen 0.0.0.0 --port 8188" % comfy,             # ours: cwd is the canonical tree
        "300|50|/tmp|python other.py main.py",                                       # not a ComfyUI server
        "400|50|/tmp|/usr/bin/python3 /somewhere/else/app.py",
        "500|1|/root|bash -c sleep infinity",
    ])
    r = _bash('COMFY="%s"; printf "%%s\\n" "%s" | _base_pids_from_table' % (comfy, table.replace('"', '\\"')))
    assert r.stdout.split() == ["100", "200"], r.stdout + r.stderr
    # PID 1 running ComfyUI itself (a bare image whose CMD is python main.py): reported, never killed
    table2 = "1|0|%s|python main.py --listen" % comfy
    r = _bash('COMFY="%s"; printf "%%s\\n" "%s" | _base_pids_from_table' % (comfy, table2))
    assert r.stdout.split() == [] and "PID 1" in r.stdout + r.stderr, r.stdout + r.stderr


def test_unit_restart_uses_the_same_launch_function_as_boot():
    lib = BASE / "lib"
    defs = {p.name: code_only(p.read_text()) for p in lib.glob("*.sh")}
    assert sum("_base_start_cmd()" in t for t in defs.values()) == 1 and "_base_start_cmd()" in defs["85-launch.sh"]
    assert sum("_base_start_comfy()" in t for t in defs.values()) == 1 and "_base_start_comfy()" in defs["85-launch.sh"]
    # 3.0.3: base_restart goes through _base_restart_comfy, which starts with the same _base_start_comfy when no unit owns ComfyUI
    restart = defs["80-server.sh"][defs["80-server.sh"].index("_base_restart_comfy()"):defs["80-server.sh"].index("base_smoke()")]
    # 3.15.0: through _base_restart_proven, which is _base_restart_comfy plus the judgement of a deferred import check
    proven = defs["80-server.sh"][defs["80-server.sh"].index("_base_restart_proven()"):defs["80-server.sh"].index("_base_restart_comfy()")]
    assert "_base_start_comfy" in restart and "_base_restart_proven" in restart[restart.index("base_restart()"):]
    assert "_base_restart_comfy" in proven
    assert "85-launch.sh" in defs["boot.sh"] and "_base_start_comfy" in defs["boot.sh"][defs["boot.sh"].index("boot_comfy()"):]
    # the real /proc walk feeds the same matcher (no second opinion on what a ComfyUI process is), and since 3.0.3 it lives
    # in 85-launch.sh so the boot has it too
    assert sum("_base_comfy_pids()" in t for t in defs.values()) == 1 and "_base_comfy_pids()" in defs["85-launch.sh"]
    body = defs["85-launch.sh"][defs["85-launch.sh"].index("_base_comfy_pids()"):defs["85-launch.sh"].index("_base_stop_comfy()")]
    assert "_base_pids_from_table" in body


# ---------------------------------------------------------------- T17: packs and the scan root (base V2.0.0)

def test_unit_base_packs_are_six_and_pinned():
    from basetest import base_packs
    rows = base_packs()
    assert [r["dir"] for r in rows] == ["rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", r["sha"]) for r in rows), rows
    assert "BAKED_PACKS" not in "".join(code_only(p.read_text()) for p in LIB)      # no image-owned pack list: the tree is ours


def test_unit_venv_requirement_set_covers_every_pack_dir_present(tmp_path):
    """Whatever sits in custom_nodes — managed by a row or not — gets its requirements into the venv, so the tree imports."""
    c = _pod(tmp_path); (c / "requirements.txt").write_text("")
    for d in ("Foo", "some-unmanaged-pack"):
        (c / "custom_nodes" / d).mkdir(parents=True); (c / "custom_nodes" / d / "requirements.txt").write_text("x\n")
    (c / "custom_nodes" / "NoReqs").mkdir()
    r = _bash("base_env_setup; base_discover quiet; _base_all_reqfiles", env={"BASE_FAKE_ROOT": str(tmp_path)})
    files = r.stdout.split()
    assert f"{c}/requirements.txt" in files and f"{c}/custom_nodes/Foo/requirements.txt" in files and f"{c}/custom_nodes/some-unmanaged-pack/requirements.txt" in files, r.stdout + r.stderr


# ---------------------------------------------------------------- T18: the version gate, the pod tier's inputs, the rehearsal (base V2.0.0)

def test_unit_version_gate_accepts_an_older_base_min_and_refuses_a_newer_one(tmp_path):
    """A package declares BASE_MIN, the base it was written against. Base 2.0.0 runs a package that says 1.0.0; a package
    that needs a newer base than the one installed is refused with both versions named."""
    r = _bash('BASE_MIN=1.0.0; _base_base_min_gate && echo GATE_OK')
    assert "GATE_OK" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_MIN=99.0.0; PKG_NAME=Future; _base_base_min_gate || echo GATE_REFUSED')
    assert "GATE_REFUSED" in r.stdout and "99.0.0" in r.stdout + r.stderr and "Future" in r.stdout + r.stderr, r.stdout + r.stderr
    v = (BASE / "VERSION").read_text().strip()
    assert v in r.stdout + r.stderr
    src = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert "_base_base_min_gate" in src[src.index("_base_validate_tables()"):src.index("_base_declare_dump()")]


def test_unit_base_test_hands_the_suite_the_venv_and_the_tree():
    src = code_only((BASE / "lib" / "95-summary.sh").read_text()); body = src[src.index("base_test()"):src.index("_base_pkg_command()")]
    assert 'BASE_VENV="${VENV:-}"' in body and 'BASE_COMFY="${COMFY:-}"' in body
    pod = (BASE / "suite" / "test_pod.py").read_text()
    assert 'os.environ.get("BASE_VENV")' in pod and "test_runpod_venv_is_stamped_at_the_path_the_boot_activates" in pod
    assert "test_runpod_the_running_comfyui_uses_the_base_venv" in pod and "test_runpod_boot_sh_is_pid1_and_sshd_jupyter_answer" in pod


def test_unit_rehearsal_knows_the_three_layouts_and_boots_the_fake_pod():
    _repo_only("_build/rehearse.py")
    src = (BASE / "_build" / "rehearse.py").read_text()
    assert '"--layout"' in src and all(l in src for l in ("official", "community", "bare"))
    assert "boot_stubs" in src and "boot.sh" in src and "layout=a.layout" in src
    assert "def boot_stubs" in (BASE / "py" / "basetest.py").read_text()          # shared with the unit tier's boot tests


# ---------------------------------------------------------------- T19: found on the live pod, 2026-09-05 (base V2.0.0 fix loop)

SILENT_GIT = """#!/bin/bash
while [ "${1:-}" = "-c" ]; do shift 2; done
case "${1:-}" in
  clone) d="${@: -1}"; mkdir -p "$d/.git"; exit 0 ;;
  -C) shift 2
      case "${1:-}" in
        remote) [ "${2:-}" = "get-url" ] && echo "https://example.invalid/x"; exit 0 ;;
        rev-parse) echo 0123456789abcdef0123456789abcdef01234567; exit 0 ;;
        *) exit 0 ;;
      esac ;;
  *) exit 0 ;;
esac
"""

FAILING_GIT = """#!/bin/bash
while [ "${1:-}" = "-c" ]; do shift 2; done
[ "${1:-}" = clone ] && exit 128
exit 0
"""


def test_unit_a_silent_git_clone_is_a_success_not_a_failure(tmp_path):
    """On a live pod (a community template image) every base pack 'clone failed' although the directories were there —
    git -q printed nothing, `grep -v` on empty input exited 1, and pipefail called that a failed clone. git's own
    exit status is the only judge."""
    c = _pod(tmp_path); (c / "custom_nodes").mkdir(exist_ok=True)
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "git").write_text(SILENT_GIT); (bin_ / "git").chmod(0o755)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_}
    # a real run has pipefail on (base_main); the harness must too, or grep's exit status is masked by tail's
    r = _bash('set -o pipefail; base_env_setup; base_discover quiet; base_packs git; echo CLONED=${#PACK_CLONED[@]} FAILED=${#BASE_FAILED[@]}', env=env)
    assert "clone failed" not in r.stdout and "CLONED=6" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr   # six shared packs
    assert (c / "custom_nodes" / "rgthree-comfy" / ".git").exists()
    # a clone that really fails (git exits non-zero, silently) is still a failure
    (bin_ / "git").write_text(FAILING_GIT)
    (c / "custom_nodes" / "rgthree-comfy").rename(tmp_path / "gone")
    r = _bash('set -o pipefail; base_env_setup; base_discover quiet; base_packs git; echo FAILED=${#BASE_FAILED[@]}', env=env)
    assert "clone failed: rgthree-comfy" in r.stdout and "FAILED=0" not in r.stdout, r.stdout + r.stderr


def test_unit_uv_is_installed_into_the_tools_venv_on_the_volume_never_the_system_interpreter(tmp_path):
    """Live pod 2026-09-05: `pip install --user uv` into the image's python is refused (PEP 668, externally managed).
    uv comes from PyPI into the base's tools venv on the volume ($BASE_HOME/tools, made with the system python's venv
    module); the system interpreter is never written to; the second run finds it."""
    c = _pod(tmp_path); bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    tools = tmp_path / "comfy-base" / "tools-bootstrap"                                  # 3.0.0: only to fetch uv; uv itself is $BASE_HOME/bin/uv
    # one stub python: as the SYSTEM interpreter it refuses `-m pip` (PEP 668); copied into the bootstrap venv by `-m venv`
    # it becomes that venv's python, whose `-m pip install … uv` creates bin/uv. Every call logs "$0 $*".
    py = bin_ / "python3"
    py.write_text('#!/bin/bash\necho "$0 $*" >> "%s"\n'
                  'case "$0" in */tools-bootstrap/bin/python)\n'
                  '  case "$*" in "-m pip --version") echo "pip 99 (stub)"; exit 0;; *"-m pip install"*uv*) printf "#!/bin/bash\\necho uv 9.9-stub\\n" > "$(dirname "$0")/uv"; chmod +x "$(dirname "$0")/uv"; exit 0;; esac; exit 0;;\n'
                  'esac\n'
                  'if [ "$1 $2" = "-m venv" ]; then d="$3"; mkdir -p "$d/bin"; cp "$0" "$d/bin/python"; exit 0; fi\n'
                  'if [ "$1 $2" = "-m pip" ]; then echo "error: externally-managed-environment" >&2; exit 1; fi\nexit 0\n' % log)
    py.chmod(0o755)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_, "SYS_PY": str(py)}
    r = _bash('base_env_setup; _base_ensure_uv && echo UV=$(command -v uv)', env=env)
    assert ("UV=%s" % (tmp_path / "comfy-base" / "bin" / "uv")) in r.stdout, r.stdout + r.stderr
    calls = log.read_text()
    assert ("%s -m venv %s" % (py, tools)) in calls and ("%s/bin/python -m pip install" % tools) in calls and "uv" in calls.split("-m pip install")[1]
    assert ("%s -m pip" % py) not in calls                                                 # the system interpreter is never touched
    r2 = _bash('base_env_setup; _base_ensure_uv && echo UV=$(command -v uv)', env=env)
    assert "UV=" in r2.stdout and log.read_text().count("-m venv") == 1                  # found, not rebuilt


UV_STUB = """#!/bin/bash
echo "uv $*" >> "__LOG__"
case "$1 $2" in
  "python list") printf 'cpython-3.14.7-linux-x86_64-gnu    <download available>\\ncpython-3.13.5-linux-x86_64-gnu    <download available>\\n'; exit 0 ;;
  "pip compile")
    # uv 0.12: requirement files are positional; `-r` is rejected exactly like this
    for a in "$@"; do [ "$a" = "-r" ] && { echo "error: unexpected argument '-r' found" >&2; exit 2; }; done
    [ -n "__FAIL__" ] && { echo "  x No solution found when resolving dependencies: torch==0 has no wheel for cp313" >&2; exit 1; }
    for a in "$@"; do case "$prev" in -o) : > "$a";; esac; prev="$a"; done
    exit 0 ;;
  "--version"*) echo "uv 0.12.10"; exit 0 ;;
esac
exit 0
"""


def _uv_stub(tmp_path, fail=False):
    """A uv 0.12-shaped stub in the tools venv (so _base_ensure_uv finds it) that logs argv and either resolves or not."""
    t = tmp_path / "comfy-base" / "bin"; t.mkdir(parents=True, exist_ok=True); log = tmp_path / "calls.log"   # 3.0.0: the base's own uv
    (t / "uv").write_text(UV_STUB.replace("__LOG__", str(log)).replace("__FAIL__", "1" if fail else "")); (t / "uv").chmod(0o755)
    return log


def test_unit_python_pick_passes_requirement_files_positionally_and_a_failed_pick_fails_the_run(tmp_path):
    """Live pod 2026-09-05: uv 0.12 rejects `uv pip compile -r file` (files are positional), so every Python 'did not
    resolve'; uv's message was discarded; and the pick's BASE_FAILED entry, made inside $(…), was lost — the run exited 0."""
    c = _pod(tmp_path); (c / "requirements.txt").write_text("torch\n")
    (c / "custom_nodes" / "Pack").mkdir(parents=True); (c / "custom_nodes" / "Pack" / "requirements.txt").write_text("x\n")
    log = _uv_stub(tmp_path)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = _bash('base_env_setup; base_discover quiet; _base_ensure_uv; PICK=$(_base_python_pick); echo PICK=$PICK', env=env)
    assert "PICK=3.14" in r.stdout, r.stdout + r.stderr
    compile_line = [ln for ln in log.read_text().splitlines() if "pip compile" in ln and "--extra-index-url" in ln][0]   # 3.9.2: the family check comes first
    assert " -r " not in compile_line and str(c / "requirements.txt") in compile_line and str(c / "custom_nodes" / "Pack" / "requirements.txt") in compile_line
    # a pick that really cannot resolve: the reason is shown, and the run is FAILED (exit 1), not a silent 0
    log = _uv_stub(tmp_path, fail=True)
    r = _bash('base_env_setup; base_discover quiet; base_venv; echo FAILED=${#BASE_FAILED[@]}; base_summary', env=env)
    assert "FAILED=1" in r.stdout and "No solution found" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 1


OPTIONAL_DECLS = ("DRIVER_MIN", "SUPERSEDED", "LEGACY_DIRS", "DROPPED_PACKS", "PIP_EXTRA", "HYGIENE_ARGS", "HYGIENE_ARGS_REMOVE",
                  "LOADER_CATS", "PKG_COMMANDS", "PKG_IMPORT_CHECK", "PKG_NO_SUITE", "ZIP_EXTRA", "VENDORED_PACKS")


def test_unit_pip_extra_step_is_safe_when_a_package_declares_none(tmp_path):
    """Live pod 2026-09-05: the base's own step one died seven minutes into the venv build with
    'PIP_EXTRA: unbound variable' — an optional package declaration counted with ${#…[@]} under set -u."""
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; _base_venv_pip_extra; echo RC=$?')
    assert "RC=0" in r.stdout and "PIP" not in r.stdout, r.stdout + r.stderr
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PIP_EXTRA=(nvidia-vfx sageattention); _base_venv_pip_extra; echo RC=$?')
    assert "RC=0" in r.stdout and "nvidia-vfx sageattention" in r.stdout and "-c /dev/null" in r.stdout, r.stdout + r.stderr


def test_unit_no_upstream_file_is_installed_on_its_own_so_nothing_overshoots_a_cap():
    """2.12.3, measured on a Verda machine: transformers 5.17.0 (its newest) "has requirement huggingface-hub<2.0,>=1.5.0,
    but you have huggingface-hub 2.0.0", because `pip install --upgrade -r <pack file>` took a bare huggingface_hub to
    its newest outside any resolve with transformers. 2.12.3 repaired it afterwards with `pip check`. 3.0.0 removes the
    cause: every requirement is installed in ONE uv resolve of the derived set (the newest set that agrees: transformers
    5.17.0 with hub 1.33.0), and a force past a cap is kept only if the package that set the cap still imports
    (py/holders.py), so hub 2.0.0 is taken back and named instead of breaking ComfyUI's startup."""
    src = code_only("\n".join(_src(p) for p in LIB))
    assert "_base_venv_reconcile" not in src                                   # superseded, and it would undo a deliberate force
    assert not re.search(r"(pip|uvpip)[^\n]*install[^\n]*-r \"\$(f|rf|COMFY/requirements\.txt)\"", src)   # no upstream file on its own
    latest = code_only(_src(BASE / "lib" / "47-latest.sh"))
    assert "_base_revert_forced" in latest and "py/holders.py" in latest


def test_unit_optional_declarations_are_never_counted_unguarded():
    """${#NAME[@]} on an array a package may not declare is an unbound-variable exit under set -u on bash 5:
    every such count must sit behind ${NAME[@]+…} on the same line."""
    bad = []
    for p in LIB:
        for i, ln in enumerate(code_only(p.read_text()).splitlines(), 1):
            for name in OPTIONAL_DECLS:
                if ("${#%s[@]}" % name) in ln and ("${%s+" % name) not in ln and ("${%s[@]+" % name) not in ln:
                    bad.append("%s:%d %s" % (p.name, i, ln.strip()[:90]))
    assert not bad, bad


# ---------------------------------------------------------------- T20: found on the live pod, 2026-09-05, second batch

def test_unit_a_fake_run_never_reads_the_real_pid1_environment(tmp_path):
    """On the pod a fake-mode test picked up the pod's REAL HF_TOKEN from /proc/1/environ. In fake mode the only PID 1
    environment is BASE_FAKE_PID1_ENV; in real mode BASE_PID1_ENVIRON (a test seam) stands in for /proc/1/environ."""
    environ = tmp_path / "environ"; environ.write_bytes(b"HF_TOKEN=real-value\0OTHER=x\0")
    r = _bash('_base_pid1_env HF_TOKEN', env={"BASE_PID1_ENVIRON": str(environ)})
    assert r.stdout.strip() == "real-value", r.stdout + r.stderr                        # real mode: read
    r = _bash('_base_pid1_env HF_TOKEN; echo "[end]"', env={"BASE_PID1_ENVIRON": str(environ), "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.stdout.strip() == "[end]", r.stdout + r.stderr                             # fake mode, no fake file: nothing
    fake = tmp_path / "pid1.env"; fake.write_text("HF_TOKEN=fake-value\n")
    r = _bash('_base_pid1_env HF_TOKEN', env={"BASE_PID1_ENVIRON": str(environ), "BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": str(fake)})
    assert r.stdout.strip() == "fake-value", r.stdout + r.stderr


def test_unit_on_pod_means_linux_with_any_nvidia_device_node(tmp_path):
    """The pod's GPU was /dev/nvidia3, not /dev/nvidia0: the pod tiers skipped and the volume check thought it was off-pod."""
    dev = tmp_path / "dev"; dev.mkdir(); (dev / "nvidiactl").write_text(""); (dev / "nvidia3").write_text("")
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Linux"})
    assert "POD" in r.stdout and "NOPOD" not in r.stdout, r.stdout + r.stderr
    (dev / "nvidia3").unlink()
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Linux"})
    assert "NOPOD" in r.stdout
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Darwin"})
    assert "NOPOD" in r.stdout
    text = "".join(code_only(q.read_text()) for q in LIB) + (BASE / "py" / "basetest.py").read_text()
    assert "nvidia0" not in text, "a literal /dev/nvidia0 gate is still there"


def test_unit_an_empty_model_index_is_not_a_warning(tmp_path):
    """GNU xargs runs `stat` with no arguments on empty input (BSD does not): a fresh volume's empty index raised
    'the model index walk did not complete' on the pod. The pipeline goes through _base_xargs0, which passes -r where it exists."""
    c = _pod(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; _base_build_index; echo WARN=${#BASE_WARN[@]} LINES=$(wc -l < "$IDX")', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_SEARCH_ROOTS": str(tmp_path / "ComfyUI" / "models")})
    assert "WARN=0" in r.stdout and "LINES=0" in r.stdout.replace(" ", ""), r.stdout + r.stderr
    src = code_only((BASE / "lib" / "50-models.sh").read_text())
    assert "_base_xargs0" in src[src.index("_base_build_index()"):]
    r = _bash('printf "" | _base_xargs0 stat -c %s; echo RC=$?')
    assert "RC=0" in r.stdout and "missing operand" not in r.stdout + r.stderr


def test_unit_the_shipped_suite_skips_repo_only_tests_instead_of_failing():
    unit = (BASE / "suite" / "test_unit.py").read_text(); inst = (BASE / "suite" / "test_install.py").read_text()
    assert not re.findall(r'^\s+STUB = BASE / "_build" / "stub"$', unit, re.M)            # every use goes through the skipping helper
    for needle in ("package.py", "rehearse.py", "testbed.sh", 'comfyui-base.zip"'):
        for m in re.finditer(r"def (test_unit_[a-z_]+)\(", unit):
            body = unit[m.end():m.end() + 1800].split("\ndef ")[0]
            if needle in body and "_repo_only(" not in body and "_stub()" not in body:
                raise AssertionError("%s uses %s without _repo_only()" % (m.group(1), needle))
    assert "pytestmark = [pytest.mark.install, pytest.mark.skipif(not STUB.exists()" in inst


def test_unit_the_test_command_runs_the_suite_with_the_base_s_own_uv_when_none_is_on_path(tmp_path):
    """On the pod `…-script.sh test` said "no pytest in the venv and no uv": uv lives in the base's tools venv on the
    volume (2.0.1), which the install path puts on PATH and the test command, run on its own, did not (2.0.5)."""
    _pod(tmp_path)
    tools = tmp_path / "comfy-base" / "bin"; tools.mkdir(parents=True)                  # 3.0.0: the base's own uv lives here
    uv = tools / "uv"; uv.write_text('#!/bin/bash\necho "uv $*" >> "$(dirname "$0")/calls.log"\necho "1 passed"\n'); uv.chmod(0o755)
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test unit; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})   # HOME too: base_env_setup adds ~/.local/bin, where the Mac's real uv lives (a real uv here re-runs the whole suite inside this test)
    assert "did NOT run" not in r.stdout and "RESULT=1 passed RC=0" in r.stdout, r.stdout + r.stderr
    assert (tools / "calls.log").exists() and "pytest" in (tools / "calls.log").read_text()
    # 2.0.14: `bash base.sh test` reported "✔ suite: 1 failed …" — the pipeline into tee hid pytest's exit code (base.sh, unlike
    # the package scripts, never set pipefail). The runner judges pytest's OWN status, whatever the shell's options are.
    uv.write_text('#!/bin/bash\necho "uv $*" >> "$(dirname "$0")/calls.log"\necho "1 failed, 3 passed"\nexit 1\n')
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test unit; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "RESULT=1 failed, 3 passed RC=1" in r.stdout and "✖ suite" in r.stdout and "✔ suite" not in r.stdout, r.stdout + r.stderr



def test_unit_the_test_command_takes_quick_as_every_tier_that_needs_no_live_machine(tmp_path):
    """3.16.0, tier 0 (_build/quick.sh): `test quick` runs the suite's declared tiers minus BASE_HEAVY_TIERS, as one -m
    expression; a suite with a tier really named quick keeps its own meaning."""
    _pod(tmp_path)
    tools = tmp_path / "comfy-base" / "bin"; tools.mkdir(parents=True)
    uv = tools / "uv"; uv.write_text('#!/bin/bash\necho "uv $*" >> "$(dirname "$0")/calls.log"\necho "1 passed"\n'); uv.chmod(0o755)
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test quick; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "RESULT=1 passed RC=0" in r.stdout and "unknown tier" not in r.stdout, r.stdout + r.stderr
    call = (tools / "calls.log").read_text()
    assert "-m unit" in call and "install" not in call.split("-m unit", 1)[1].split()[:2], call      # the base's own: unit only
    assert "quick: unit (not: install comfyui runpod gpu bare" in r.stdout, r.stdout
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test quack; echo "RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "unknown tier 'quack'" in r.stdout + r.stderr and "(or quick)" in r.stdout + r.stderr and "RC=2" in r.stdout

def test_unit_boot_sh_records_its_pid_so_the_pod_tier_can_tell_a_base_boot_from_the_images(tmp_path):
    """On the pod both runpod tests took PID 1 for boot.sh because podctl's wrapper text mentions boot.sh (2.0.6)."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    r, blog = _run_boot_sh(tmp_path, bin_, {"JUPYTER_PASSWORD": "pw"})
    pidfile = tmp_path / "comfy-base" / "state" / "boot.pid"
    assert r.returncode == 0 and pidfile.exists() and pidfile.read_text().strip().isdigit(), r.stderr + blog


def _pod_tier():
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_pod", BASE / "suite" / "test_pod.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_unit_the_pod_tier_tells_a_base_boot_from_the_images_wrapper(tmp_path):
    m = _pod_tier()
    state = tmp_path / "state"; state.mkdir(); proc = tmp_path / "proc"; proc.mkdir()
    assert m._booted_through_base(state, proc) is False                                      # no boot.pid: the image's boot
    (state / "boot.pid").write_text("7\n")
    assert m._booted_through_base(state, proc) is False                                      # a dead pid (a previous boot)
    (proc / "7").mkdir(); (proc / "7" / "cmdline").write_bytes(b"bash\0-c\0if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi; exec /start_script.sh\0")
    assert m._booted_through_base(state, proc) is False                                      # podctl's wrapper is not boot.sh
    (proc / "7" / "cmdline").write_bytes(b"bash\0/workspace/comfy-base/boot.sh\0")
    assert m._booted_through_base(state, proc) is True
    src = (BASE / "suite" / "test_pod.py").read_text()
    assert src.count("_booted_through_base(") >= 3 and '"comfy-base/boot.sh" not in' not in src   # both runpod tests use it; the substring test is gone
    assert 'BASE_STATE=' in code_only((BASE / "lib" / "95-summary.sh").read_text())           # the runner hands the state dir over


def test_unit_a_base_upgrade_refreshes_the_boot_script_the_pod_runs(tmp_path):
    """`test` on the pod re-installed base 2.0.6 into /workspace/comfy-base and left the 2.0.3 boot.sh that podctl's
    wrapper execs: only the install flow wrote it. Every self-install refreshes an existing boot.sh (2.0.7)."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    boot = tmp_path / "comfy-base" / "boot.sh"; assert boot.exists()
    boot.write_text("#!/bin/bash\n# a boot.sh from an older base\n")
    r = run_script(BASE / "comfyui-base-script.sh", "test", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert boot.read_text() == (BASE / "lib" / "boot.sh").read_text(), r.stdout
    assert "refreshed" in r.stdout and "boot.sh" in r.stdout


def test_unit_the_banner_never_threatens_a_rebuild_of_a_venv_whose_interpreter_survives(tmp_path):
    """On the pod the banner's "boots from" line said "on the volume — survives a pod restart; this run rebuilds the
    venv" in one breath: it handed the interpreter's home DIRECTORY to a predicate that expects a venv (2.0.8).
    The line is one function, _base_venv_boot_line <venv>, judged with the same predicate the venv stage uses."""
    vol = tmp_path / "vol"; venv = vol / "ComfyUI" / ".venv-cu130"; venv.mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = %s/comfy-base/python/cpython-3.14/bin\nversion = 3.14.7\n" % vol)
    r = _bash('BASE_PERSIST_ROOT="%s"; _base_venv_boot_line "%s"' % (vol, venv))        # set AFTER sourcing: base.sh resets its globals
    assert "boots from" in r.stdout and "survives" in r.stdout and "rebuilds" not in r.stdout, r.stdout + r.stderr
    venv.joinpath("pyvenv.cfg").write_text("home = /root/.local/share/uv/python/cpython-3.14/bin\n")
    r = _bash('BASE_PERSIST_ROOT="%s"; _base_venv_boot_line "%s"' % (vol, venv))
    assert "CONTAINER DISK" in r.stdout and "rebuilds the venv" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "10-discover.sh").read_text())
    assert "_base_venv_boot_line" in src and "this run rebuilds the venv" not in src   # the banner calls the helper, no second copy


def test_unit_a_failed_download_prints_the_tools_last_lines_and_fails_the_run(tmp_path):
    """On the pod a model failed three attempts and the console said only "attempt N failed": the tool ran quietly and
    its output went nowhere. The tool's last lines are the diagnosis (as uv's are since 2.0.2) — 2.0.9; the curl leg
    (GitHub) keeps that behaviour."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official"); _stub_with_github_row(tmp_path)
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\nd=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\nmkdir -p "$d"; python3 -c "open(\\"$d/stub_vae.safetensors\\",\\"wb\\").truncate(4096)"\n'); (bin_ / "hf").chmod(0o755)
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl argv: $*" >> "%s"\ncase " $* " in *github.com*) echo "curl: (22) The requested URL returned error: 404"; exit 1;; esac\nexit 0\n' % log)
    (bin_ / "curl").chmod(0o755)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                   env={"PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_FAKE_DL": "real", "HF_TOKEN": "hf_env_value"})
    assert r.returncode != 0
    assert "returned error: 404" in r.stdout and "download failed" in r.stdout and "stub_gh" in r.stdout, r.stdout[-3000:]
    assert len([l for l in log.read_text().splitlines() if l.startswith("curl argv:") and "github.com" in l]) == 3   # three attempts, then the verdict


def test_unit_a_snapshot_download_keeps_the_token_off_argv_and_shows_its_error(tmp_path):
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf argv: $*" >> "%s"; echo "hf env HF_TOKEN=${HF_TOKEN:-unset}" >> "%s"\necho "401 Client Error: Unauthorized for url"; exit 1\n' % (log, log)); (bin_ / "hf").chmod(0o755)
    r = _bash('export HF_TOKEN=hf_env_value; VENV=/nonexistent; BASE_NO_NET=0; _base_snapshot hf://owner/repo "%s" 4096; echo RC=$?' % (tmp_path / "dest"),
              env={"PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    calls = log.read_text()
    assert "--token" not in calls and "hf env HF_TOKEN=hf_env_value" in calls
    assert calls.count("hf argv:") == 1                         # 3.12.0: a 401 is said once, never retried
    assert "Unauthorized" in r.stdout and "refused HF_TOKEN" in r.stdout and "RC=1" in r.stdout, r.stdout + r.stderr


def test_unit_the_launch_sets_hf_home_in_the_environment_and_still_runs_python(tmp_path):
    """The pod's restart FAILED with "85-launch.sh: line 37: HF_HOME=/workspace/huggingface: No such file or directory":
    the launch line put the assignment in front of nohup as a WORD, and bash ran it as a command. The fake boot test
    never saw it — its curl stub answers "up" whatever happened. This test runs the launch with a python stub (2.0.10)."""
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"; vol = tmp_path / "vol"; comfy = vol / "ComfyUI"; comfy.mkdir(parents=True)
    (bin_ / "python").write_text('#!/bin/bash\necho "python argv: $*" >> "%s"; echo "python env HF_HOME=${HF_HOME:-unset} cwd=$PWD" >> "%s"\n' % (log, log)); (bin_ / "python").chmod(0o755)
    (bin_ / "curl").write_text('#!/bin/bash\nexit 0\n'); (bin_ / "curl").chmod(0o755)
    r = _bash('BASE_PERSIST_ROOT="%s"; PY="%s"; COMFY="%s"; PORT=8188; HOSTPORT=127.0.0.1:8188; ARGS_FILE=/nonexistent; COMFY_LOG="%s"; BASE_START_TRIES=1; _base_start_comfy; echo RC=$?; sleep 1'
              % (vol, bin_ / "python", comfy, tmp_path / "comfyui.log"), env={"PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    calls = log.read_text() if log.exists() else (tmp_path / "comfyui.log").read_text() if (tmp_path / "comfyui.log").exists() else ""
    assert "python argv: main.py --listen 127.0.0.1 --port 8188" in calls, (calls, r.stdout, r.stderr)   # 3.14.0: a raw caller is public, so loopback
    assert "python env HF_HOME=%s/huggingface cwd=%s" % (vol, comfy) in calls, calls
    assert "No such file" not in (tmp_path / "comfyui.log").read_text()


def test_unit_pytest_is_installed_into_our_venv_so_the_suite_runs_inside_it(tmp_path):
    """On the pod a package suite's gpu tier failed with "No module named comfy_kitchen" and "no metadata for nvidia-vfx" —
    both ARE in the venv; the suite ran under uv's throwaway Python 3.12 because our venv had no pytest (2.0.10).
    pytest is installed in the build, checked on every reuse, and installed by base_test itself when missing — proven by import."""
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "py-with").write_text('#!/bin/bash\ncase "$*" in *pytest*) echo 8.4.0;; esac; exit 0\n'); (bin_ / "py-with").chmod(0o755)
    (bin_ / "py-without").write_text('#!/bin/bash\ncase "$*" in *"import pytest"*) exit 1;; esac; exit 0\n'); (bin_ / "py-without").chmod(0o755)
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PY="%s"; _base_venv_pytest; echo RC=$?' % (bin_ / "py-with"))
    assert "RC=0" in r.stdout and "PIP install" in r.stdout and " pytest" in r.stdout and "-c /dev/null" in r.stdout and "pytest 8.4.0 in the venv" in r.stdout, r.stdout + r.stderr
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PY="%s"; _base_venv_pytest; echo RC=$?' % (bin_ / "py-without"))
    assert "RC=1" in r.stdout and "does not import" in r.stdout, r.stdout + r.stderr                 # an install that leaves no pytest is a failure, said so
    r = _bash('set -u; _base_venv_pytest(){ echo INSTALL; }; PY="%s"; _base_venv_ensure_pytest; PY="%s"; _base_venv_ensure_pytest; echo RC=$?' % (bin_ / "py-with", bin_ / "py-without"))
    assert r.stdout.count("INSTALL") == 1 and "RC=0" in r.stdout, r.stdout + r.stderr
    src30 = code_only((BASE / "lib" / "30-venv.sh").read_text()); src95 = code_only((BASE / "lib" / "95-summary.sh").read_text())
    src47 = code_only((BASE / "lib" / "47-latest.sh").read_text())
    assert " pytest " in src47[src47.index("_base_derived_install(){"):] and "_base_venv_ensure_pytest" in src30[src30.index("base_venv()"):]   # 3.0.0: in the one resolve
    assert "_base_venv_pytest" in src95[src95.index("base_test()"):]
    assert '-c "$CONSTRAINTS" pytest "huggingface_hub' not in src30     # the one-line install whose pytest half silently never happened


def _launch_stubs(tmp_path):
    """A python that records argv + HF_HOME and raises an `up` marker; a curl that answers only once the marker exists."""
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True); log = tmp_path / "calls.log"; up = tmp_path / "up"
    (bin_ / "python").write_text('#!/bin/bash\necho "python argv: $*" >> "%s"; echo "python env HF_HOME=${HF_HOME:-unset} cwd=$PWD" >> "%s"; touch "%s"\n' % (log, log, up))
    (bin_ / "curl").write_text('#!/bin/bash\n[ -e "%s" ]\n' % up)
    for p in (bin_ / "python", bin_ / "curl"): p.chmod(0o755)
    return bin_, log


def test_unit_restart_opt_in_starts_a_server_when_none_runs(tmp_path):
    """Second step two on the pod (2.0.10): BASE_RESTART=1, no ComfyUI running (the first run had stopped the image's and
    its own launch had died) — and the stage only PRINTED a start line. Opting in means a running server at the end (2.0.11)."""
    _pod(tmp_path); bin_, log = _launch_stubs(tmp_path)
    snippet = ('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; BASE_NO_NET=0; BASE_DRY=0; PY="%s"; COMFY_LOG="%s"; BASE_START_TRIES=3; '
               'MODEL_DL=(one); BASE_RESTART=1 base_restart; echo R=$BASE_RESTART_RESULT FAILED=${#BASE_FAILED[@]}') % (bin_ / "python", tmp_path / "comfyui.log")
    r = _bash(snippet, env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "R=started FAILED=0" in r.stdout, r.stdout + r.stderr
    assert log.exists() and "python argv: main.py --listen 127.0.0.1 --port" in log.read_text(), r.stdout
    # the import check's block still wins: nothing is started on top of a failed import check
    (tmp_path / "up").unlink(); log.unlink()
    r = _bash(snippet.replace("BASE_RESTART=1 base_restart", "BASE_BLOCK_RESTART=1 BASE_RESTART=1 base_restart"), env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "R=started" not in r.stdout and not log.exists() and "import check failed" in r.stdout, r.stdout + r.stderr


def test_unit_the_hand_off_line_is_runnable_as_printed(tmp_path):
    """The stage's hand-off was `cd … && nohup HF_HOME=/x python main.py …`: nohup executes "HF_HOME=/x" as a command.
    Whatever _base_start_cmd prints must run verbatim after nohup (2.0.11)."""
    bin_, log = _launch_stubs(tmp_path); vol = tmp_path / "vol"; comfy = vol / "ComfyUI"; comfy.mkdir(parents=True)
    r = _bash('BASE_PERSIST_ROOT="%s"; PY="%s"; PORT=8188; ARGS_FILE=/nonexistent; line="$(_base_start_cmd)"; echo "LINE=$line"; cd "%s" && nohup $line > "%s" 2>&1; echo RC=$?'
              % (vol, bin_ / "python", comfy, tmp_path / "nohup.out"), env={"PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout and "HF_HOME=%s/huggingface" % vol in r.stdout, r.stdout + r.stderr
    assert log.exists() and "python env HF_HOME=%s/huggingface cwd=%s" % (vol, comfy) in log.read_text(), (r.stdout, (tmp_path / "nohup.out").read_text())


def _cgroup(tmp_path, v2=None, v1=None):
    """A fake /sys/fs/cgroup: v2 is cpu.max's text ("max 100000", "250000 100000"); v1 is (quota_us, period_us)."""
    root = tmp_path / "cgroup"; root.mkdir(parents=True, exist_ok=True)
    if v2 is not None: (root / "cpu.max").write_text(v2 + "\n")
    if v1 is not None:
        d = root / "cpu,cpuacct"; d.mkdir(exist_ok=True)
        (d / "cpu.cfs_quota_us").write_text("%s\n" % v1[0]); (d / "cpu.cfs_period_us").write_text("%s\n" % v1[1])
    return root


def test_unit_the_cpu_quota_is_the_cgroups_not_the_hosts(tmp_path):
    """3.10.0: a RunPod container is a slice of a big host. nproc and torch see the HOST's CPUs while the CFS quota lets
    the container run its own, so a thread pool sized by the host is throttled. The quota is cpu.max (v2) or
    cpu.cfs_quota_us / cpu.cfs_period_us (v1), rounded up, never more than the CPUs online; unlimited says nothing."""
    online = int(_bash("_base_cpus_online").stdout.strip())
    cases = [({"v2": "250000 100000"}, "3"), ({"v2": "100000 100000"}, "1"), ({"v2": "50000 100000"}, "1"),
             ({"v2": "max 100000"}, ""), ({"v1": (200000, 100000)}, "2"), ({"v1": (-1, 100000)}, ""),
             ({"v2": "%d 100000" % ((online + 7) * 100000)}, str(online)), ({}, "")]
    for i, (kw, want) in enumerate(cases):
        root = _cgroup(tmp_path / str(i), **kw) if kw else tmp_path / "none"
        r = _bash("_base_cpu_quota", env={"BASE_CGROUP_ROOT": str(root)})
        assert r.returncode == 0 and r.stdout.strip() == want, (kw, r.stdout, r.stderr)


def test_unit_launch_env_is_a_contract_not_a_free_for_all():
    """3.10.0: LAUNCH_ENV=( "KEY=value" ) lets a package set what ComfyUI is started with. Only thread counts and allocator
    tuning (and CTranslate2's CT2_*), one word of [A-Za-z0-9_.,:+-] each: no path, no library, no credential, no
    variable the base owns, no shell expansion. A key set twice, or anything else, refuses the package's declarations."""
    good = ["OMP_NUM_THREADS=16", "MKL_NUM_THREADS=8", "CT2_VERBOSE=1", "CT2_USE_EXPERIMENTAL_PACKED_GEMM=1",
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:512", "TOKENIZERS_PARALLELISM=false",
            "RENDER_SPEED_ATTENTION=cudnn"]                                          # 3.18.0: a pack's attention order, one word
    bad = ["PATH=/usr/bin", "RENDER_SPEED_ATTENTION=/x", "RENDER_SPEED=cudnn", "LD_PRELOAD=libx.so", "LD_LIBRARY_PATH=/x", "PYTHONPATH=/x", "HF_HOME=/x", "HF_TOKEN=hf_x",
           "VHS_FORCE_FFMPEG_PATH=/x", "COMFY_BASE_STATE=/x", "OMP_NUM_THREADS=", "OMP_NUM_THREADS=$(id)", "OMP_NUM_THREADS=1 2",
           "OMP_NUM_THREADS=/tmp", "OMP_NUM_THREADS=`id`", "ct2_verbose=1", "CT2_=1x;", "OMP_NUM_THREADS", "=1"]
    for kv in good + bad:
        r = _bash('_base_launch_env_ok "$KV" && echo YES || echo NO', env={"KV": kv})
        assert r.stdout.strip() == ("YES" if kv in good else "NO"), (kv, r.stdout, r.stderr)
    r = _bash('LAUNCH_ENV=("OMP_NUM_THREADS=8" "CT2_VERBOSE=1"); _base_launch_env_rows; echo RC=$?')
    assert r.stdout == "OMP_NUM_THREADS=8\nCT2_VERBOSE=1\nRC=0\n", r.stdout + r.stderr
    r = _bash('LAUNCH_ENV=("OMP_NUM_THREADS=8" "LD_PRELOAD=x" "OMP_NUM_THREADS=4"); _base_launch_env_rows; echo RC=$?')
    assert "RC=1" in r.stdout and "LD_PRELOAD=x" in r.stderr and "sets OMP_NUM_THREADS twice" in r.stderr, r.stdout + r.stderr
    decl = 'PKG_ID=p; PKG_NAME=P; PKG_VERSION=1.0.0; BASE_MIN=1.0.0; COMFY_MIN=0.0.0; WF_NAME=""; PACKS=(); MODELS=(); '
    r = _bash(decl + 'LAUNCH_ENV=("PATH=/x"); _base_validate_tables; echo RC=$?')
    assert "RC=1" in r.stdout and "PATH=/x" in r.stderr, r.stdout + r.stderr
    r = _bash(decl + 'LAUNCH_ENV=("OMP_NUM_THREADS=8"); _base_validate_tables; echo RC=$?; _base_declare_dump')
    assert "RC=0" in r.stdout and "LAUNCHENV OMP_NUM_THREADS=8" in r.stdout, r.stdout + r.stderr


def test_unit_launch_env_precedence_machine_then_packages_then_the_quota(tmp_path):
    """The machine's own environment wins (a pod env var someone set on purpose); then the packages' files in name order,
    the first naming a key winning and a different later value named; then the base's OMP_NUM_THREADS from the quota.
    A line a package file should never hold (the folder is on a shared volume) is left out and said."""
    state = tmp_path / "state"; d = state / "launch.d"; d.mkdir(parents=True); cg = _cgroup(tmp_path, v2="300000 100000")
    (d / "a-pkg.env").write_text("# a\nOMP_NUM_THREADS=8\nCT2_VERBOSE=1\n")
    (d / "b-pkg.env").write_text("OMP_NUM_THREADS=4\nPYTORCH_CUDA_ALLOC_CONF=expandable_segments:True\nPATH=/evil\nCT2_VERBOSE=1")
    env = {"BASE_CGROUP_ROOT": str(cg)}; le = 'BASE_STATE="%s"; _base_launch_env' % state      # base.sh resets BASE_STATE on source
    r = _bash(le, env=env)
    assert r.stdout.splitlines() == ["OMP_NUM_THREADS=8", "CT2_VERBOSE=1", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"], r.stdout + r.stderr
    assert "a-pkg sets 8, b-pkg sets 4: 8 is used" in r.stderr and "'PATH=/evil' is not an allowed launch variable" in r.stderr, r.stderr
    assert "CT2_VERBOSE" not in r.stderr                                    # the same value twice is no conflict
    (d / "a-pkg.env").unlink(); (d / "b-pkg.env").unlink()
    r = _bash(le, env=env)
    assert r.stdout == "OMP_NUM_THREADS=3\n" and r.stderr == "", r.stdout + r.stderr          # no package says: the quota does
    r = _bash(le, env=dict(env, OMP_NUM_THREADS="5"))
    assert r.stdout == "" and "OMP_NUM_THREADS=5 is set in this machine's environment: it wins over OMP_NUM_THREADS=3 (the CPU quota)" in r.stderr, r.stdout + r.stderr
    r = _bash(le, env={"BASE_CGROUP_ROOT": str(tmp_path / "nothing")})
    assert r.stdout == "", r.stdout                                          # no quota, no package: torch keeps its own default
    # the boot knows BASE_HOME (boot.env), not BASE_STATE: the same folder, found from there; 85-launch.sh alone, as boot.sh sources it
    (d / "a-pkg.env").write_text("OMP_NUM_THREADS=6\n")
    sh = "/bin/bash" if os.path.exists("/bin/bash") else "bash"           # the Mac's bash 3.2, the oldest the boot must run on
    r = subprocess.run([sh, "-c", '. "%s/lib/85-launch.sh"; BASE_HOME="%s"; _base_launch_env' % (BASE, tmp_path)], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "BASE_CGROUP_ROOT": str(cg)})
    assert r.stdout == "OMP_NUM_THREADS=6\n", r.stdout + r.stderr


def test_unit_launch_env_reaches_comfyui_and_only_comfyui(tmp_path):
    """The variables are exported into ComfyUI's process (the subshell that starts it), printed on the hand-off line in a
    form nohup runs verbatim, and never exported into the installing shell, which would then read the base's own value
    back as the machine's at the next install."""
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"; up = tmp_path / "up"
    (bin_ / "python").write_text('#!/bin/bash\necho "python env OMP=${OMP_NUM_THREADS:-unset} CT2=${CT2_VERBOSE:-unset} argv=$*" >> "%s"; touch "%s"\n' % (log, up))
    (bin_ / "curl").write_text('#!/bin/bash\n[ -e "%s" ]\n' % up)
    for p in (bin_ / "python", bin_ / "curl"): p.chmod(0o755)
    comfy = tmp_path / "ComfyUI"; comfy.mkdir(); state = tmp_path / "state"; (state / "launch.d").mkdir(parents=True)
    (state / "launch.d" / "pkg.env").write_text("CT2_VERBOSE=1\n"); cg = _cgroup(tmp_path, v2="200000 100000")
    env = {"PATH": "%s:/usr/bin:/bin" % bin_, "BASE_CGROUP_ROOT": str(cg)}
    r = _bash('BASE_STATE="%s"; PY="%s"; COMFY="%s"; PORT=8188; HOSTPORT=127.0.0.1:8188; ARGS_FILE=/nonexistent; COMFY_LOG="%s"; BASE_START_TRIES=3; '
              '_base_start_comfy; echo RC=$? PARENT_OMP=${OMP_NUM_THREADS:-unset}' % (state, bin_ / "python", comfy, tmp_path / "comfyui.log"), env=env)
    assert "RC=0 PARENT_OMP=unset" in r.stdout and "launch env: CT2_VERBOSE=1 OMP_NUM_THREADS=2" in r.stdout, r.stdout + r.stderr
    assert "python env OMP=2 CT2=1 argv=main.py --listen" in log.read_text(), log.read_text()
    log.unlink()
    r = _bash('BASE_STATE="%s"; PY="%s"; PORT=8188; ARGS_FILE=/nonexistent; line="$(_base_start_cmd)"; echo "LINE=$line"; cd "%s" && nohup $line > "%s" 2>&1; echo RC=$?'
              % (state, bin_ / "python", comfy, tmp_path / "nohup.out"), env=env)
    assert "LINE=env CT2_VERBOSE=1 OMP_NUM_THREADS=2 %s main.py" % (bin_ / "python") in r.stdout and "RC=0" in r.stdout, r.stdout + r.stderr
    assert "python env OMP=2 CT2=1" in log.read_text()


def test_unit_launch_env_is_written_per_package_and_removed_when_dropped(tmp_path):
    """One store serves several packages and each install knows only its own LAUNCH_ENV, so each writes its own file,
    state/launch.d/<PKG_ID>.env; a package that stops declaring one has its file removed. A change is a restart reason."""
    state = tmp_path / "state"; f = state / "launch.d" / "pkg-a.env"
    base = 'BASE_STATE="%s"; PKG_ID=pkg-a; PKG_NAME="Pkg A"; PKG_VERSION=1.2.0; BASE_DRY=0; BASE_CHANGED=(); ' % state
    r = _bash(base + 'LAUNCH_ENV=("OMP_NUM_THREADS=12" "CT2_VERBOSE=1"); base_launch_env_write; echo CHANGED=${#BASE_CHANGED[@]}')
    assert f.read_text().splitlines()[1:] == ["OMP_NUM_THREADS=12", "CT2_VERBOSE=1"] and "Pkg A V1.2.0" in f.read_text(), f.read_text()
    assert "CHANGED=1" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'LAUNCH_ENV=("OMP_NUM_THREADS=12" "CT2_VERBOSE=1"); base_launch_env_write; echo CHANGED=${#BASE_CHANGED[@]}')
    assert "CHANGED=0" in r.stdout and "launch env current" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=1; LAUNCH_ENV=(); base_launch_env_write')
    assert f.exists() and "remove" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'LAUNCH_ENV=(); base_launch_env_write; echo CHANGED=${#BASE_CHANGED[@]}')
    assert not f.exists() and "CHANGED=1" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "75-boot.sh").read_text())
    assert "base_launch_env_write" in src[src.index("base_boot_install(){"):]


def test_unit_combofix_reports_a_widget_order_the_server_would_reject(tmp_path, capsys):
    """On the pod, `combos ok` — and the server refused the queue: node 1199's widgets_values were stored in an older
    pack's order (its mask string first), the frontend restores widgets POSITIONALLY in /object_info's order, and so the
    server got `model: '{}'` and `seed: 'standard'`. The value-by-membership check cannot see a shift; a positional pass
    can, because it models the widget sequence the frontend builds (required then optional inputs of widget types,
    forceInput skipped, control_after_generate after a seed) — 2.0.12."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("combofix", BASE / "py" / "combofix.py"); cf = importlib.util.module_from_spec(spec); spec.loader.exec_module(cf)
    labels = ["Qwen2.5-Omni-3B · 3.4 GB", "Qwen2.5-Omni-7B · 5.8 GB", "on disk: Qwen2.5-Omni-7B-Q4_K_M.gguf"]
    obj = {"Cap": {"input": {"required": {"model": ["COMBO", {"options": labels}], "length": ["COMBO", {"options": ["short", "standard", "long"]}], "seed": ["INT", {"default": 0, "control_after_generate": True}]},
                             "optional": {"previous": ["STRING", {"forceInput": True}], "clip": ["CLIP", {}], "options": ["PKG_OPTIONS", {}], "enabled_mask": ["STRING", {"default": "{}"}],
                                          "max_frames": ["INT", {"default": 8}], "context_size": ["INT", {"default": 0}], "bypass": ["BOOLEAN", {"default": False}],
                                          "reference_instructions": ["STRING", {"default": ""}], "repeat_last": ["BOOLEAN", {"default": False}]}}}}
    assert [w[0] for w in cf.widget_sequence(obj["Cap"])] == ["model", "length", "seed", "control_after_generate", "enabled_mask", "max_frames", "context_size", "bypass", "reference_instructions", "repeat_last"]
    shifted = {"nodes": [{"id": 1199, "type": "Cap", "widgets_values": ["{}", labels[1], "standard", 42, "fixed", 8, 0, False, "{}", False]}]}
    good = {"nodes": [{"id": 1199, "type": "Cap", "widgets_values": [labels[1], "standard", 42, "fixed", "{}", 8, 0, False, "", False]}]}
    f = tmp_path / "wf.json"; f.write_text(json.dumps(shifted, indent=2))
    assert cf.process(obj, str(f), True) is True
    out = capsys.readouterr().out
    assert "UNRESOLVED #1199 Cap" in out and "position" in out and "model" in out and "'{}'" in out, out
    assert json.loads(f.read_text()) == shifted                                          # a shift is never "repaired" by guessing
    f.write_text(json.dumps(good, indent=2)); capsys.readouterr()
    assert cf.process(obj, str(f), True) is False and "UNRESOLVED" not in capsys.readouterr().out
    # 2.0.13: two shapes the first model skipped, which made TWO false positives on the pod (two nodes of one package):
    # a multi-type input ("STRING,COMBO") is a widget; a dynamic combo (COMFY_DYNAMICCOMBO_V3) is a selector whose
    # sub-widgets depend on the selection, so the judgement STOPS there (nothing after it is known positionally)
    obj["Guide"] = {"input": {"required": {"prompt": ["STRING", {"multiline": True}], "task": ["COMBO", {"options": ["T2VA", "FL2VA"]}], "duration": ["INT", {"default": 10}]},
                              "optional": {"aspect_ratio": ["STRING,COMBO", {"default": ""}], "auto_download": ["BOOLEAN", {"default": True}], "format": ["COMBO", {"options": ["plain", "json"]}]}}}
    assert [w[0] for w in cf.widget_sequence(obj["Guide"])] == ["prompt", "task", "duration", "aspect_ratio", "auto_download", "format"]
    obj["Up"] = {"input": {"required": {"model_name": ["COMBO", {"options": ["a.safetensors"]}], "resize_type": ["COMFY_DYNAMICCOMBO_V3", {"options": [{"key": "scale by multiplier"}, {"key": "target dimensions"}]}],
                                        "align": ["INT", {"default": 32}], "device": ["COMBO", {"options": ["cuda", "cpu"]}]}}}
    seq = cf.widget_sequence(obj["Up"]); assert [w[0] for w in seq] == ["model_name", "resize_type"] and seq[-1][1] == "DYNAMIC", seq
    wf3 = {"nodes": [{"id": 1195, "type": "Guide", "widgets_values": ["", "FL2VA", 5, "", True, "plain"]},
                     {"id": 819, "type": "Up", "widgets_values": ["a.safetensors", "scale by multiplier", 2, 32, "cuda"]}]}
    f.write_text(json.dumps(wf3, indent=2)); capsys.readouterr()
    assert cf.process(obj, str(f), True) is False and "UNRESOLVED" not in capsys.readouterr().out


# ---------------------------------------------------------------- T21: the sweep after the sixth run (base 2.0.14)

def test_unit_the_disk_gate_uses_the_volume_size_podctl_recorded_on_a_pooled_filesystem(tmp_path):
    """Every run on the pod warned "the volume reports 677 TB free — the backing pool, not your quota". podctl records
    the volume's size (RunPod REST) in state/volume.env; the gate then measures what the volume holds (du) and judges
    the real headroom. Without the record it still warns — and says how to record it."""
    _pod(tmp_path)
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
    (st / "volume.env").write_text("VOLUME_GB='300'\nVOLUME_ID='vol123'\nRECORDED='2026-09-06T03:00:00Z'\n")
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; BASE_FAKE_USED_KB=100000000; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=0 W=0" in r.stdout and "300" in r.stdout and "podctl" in r.stdout and "not verifiable" not in r.stdout, r.stdout + r.stderr   # 100 GB used of 300: 200 free ≥ 60 needed
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; BASE_FAKE_USED_KB=260000000; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]} F=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=1" in r.stdout and "F=1" in r.stdout and "not enough space" in r.stdout, r.stdout + r.stderr                    # 260 used: 40 free < 60
    (st / "volume.env").unlink()
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=0 W=1" in r.stdout and "not verifiable" in r.stdout and "podctl" in r.stdout, r.stdout + r.stderr                # honest, and tells how to fix it


def test_unit_latest_is_a_plain_run_and_nothing_is_left_to_settle():
    """3.0.0: every run takes everything to its newest, so `--latest` is the same as a plain run and there is no "off its
    pins" warning to settle any more: the packs are always at their HEAD and their records are printed every run."""
    src = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert "--latest)  base_install" in src and "_base_warn_settle" not in src and "BASE_LATEST=1" not in src
    assert "BASE_LATEST" not in code_only((BASE / "lib" / "40-packs.sh").read_text())


def test_unit_snapshot_drivers_serve_the_seed_and_report_a_tour():
    """3.17.1 (row 81): the App-view drivers' settings stub serves what the base seeds, and coach_tour, the one shared
    replacement for the packages' dismiss loops, returns None when no tour shows and, when ComfyUI_frontend's landing
    (data-testid coach-landing-overlay) shows, clicks its exact Skip and reports {"shown", "dismissed"}."""
    _repo_only("_build/canvas/snapshot.py")
    import importlib.util, comfy_settings
    spec = importlib.util.spec_from_file_location("snapshot", BASE / "_build" / "canvas" / "snapshot.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert all(m.SETTINGS.get(k) == v for k, v in comfy_settings.SEED.items())

    class Page:
        def __init__(self, showing): self.showing = showing; self.calls = []; self.keyboard = self
        def evaluate(self, js, *a):
            self.calls.append(js)
            if js == m.JS_COACH_SHOWN: return self.showing
            if js == m.JS_COACH_SKIP:
                if self.showing: self.showing = None; return "coach-landing"
                return None
        def wait_for_timeout(self, ms): pass
        def press(self, key): self.calls.append(key); self.showing = None

    assert m.coach_tour(Page(None), wait_ms=500) is None
    p = Page("coach-landing-overlay")
    assert m.coach_tour(p) == {"shown": "coach-landing-overlay", "dismissed": True} and m.JS_COACH_SKIP in p.calls and "Escape" not in p.calls


def test_unit_snapshot_console_errors_are_classified_not_counted(tmp_path):
    """The snapshot printed "console errors: 15" on the pod and 13 on the testbed and nobody could say which mattered.
    Every line is either a KNOWN pattern with a written reason (the frontend's, not ours) or unexplained — and the
    tier fails on an unexplained one, so a new frontend cannot slip a new error past the count."""
    _repo_only("_build/canvas/snapshot.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("snapshot", BASE / "_build" / "canvas" / "snapshot.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    lines = ["[error] Failed to load resource: the server responded with a status of 404 (Not Found) http://x/api/userdata/comfy.settings.json",
             "[error] Failed to load resource: the server responded with a status of 400 (Bad Request) http://x/api/userdata/workflows%2Fa.json",
             "[error] Loading media from 'https://cdn.example/x.mp4' violates the following Content Security Policy directive: \"default-src 'self'\"",
             "[error] Loading the image 'https://cdn.example/x.png' violates the following Content Security Policy directive: \"img-src 'self' data: blob:\"",
             "[error] ComfyApp graph accessed before initialization",
             "[error] [vite:preloadError] {url: null, fileType: unknown, chunkName: null, message: Extension named 'ColorOverlay' already registered",
             "[error] TypeError: Cannot read properties of undefined (reading 'foo') at ourNode.js:12",
             # 3.10.0: comfyui-obvpm 0.2.16 hands ComfyUI-Manager's wrapper an explicit undefined (reproduced, frontend 1.55.17)
             "[error] Error loading previous workflow TypeError: Cannot read properties of undefined (reading 'extra')\n    at app.loadGraphData (http://127.0.0.1:8211/extensions/ComfyUI-Manager/components-manager.js:741:29)",
             "[error] TypeError: Cannot read properties of undefined (reading 'extra')\n    at app.loadGraphData (http://127.0.0.1:8211/extensions/ComfyUI-Manager/components-manager.js:741:29)",
             "[error] TypeError: Cannot read properties of undefined (reading 'extra')\n    at x (http://127.0.0.1:8211/extensions/other/x.js:1:1)",
             "[log] fine"]
    known, unexplained = m.classify_console(lines)
    assert len(known) == 8 and len(unexplained) == 2 and all("TypeError" in u for u in unexplained)
    assert all(reason for _line, reason in known) and len({reason for _l, reason in known}) >= 3
    assert set(m.console_summary(lines).keys()) == {"errors", "known", "unexplained", "reasons"}
    assert m.console_summary(lines)["unexplained"][0] == "[error] TypeError: Cannot read properties of undefined (reading 'foo') at ourNode.js:12"
    assert "other/x.js" in m.console_summary(lines)["unexplained"][1]     # the same TypeError from anywhere else stays unexplained


def test_unit_foreign_source_is_parsed_with_its_syntax_warnings_accounted_for(tmp_path):
    """A package suite parses the packs' own .py files (ast) and Python's SyntaxWarning for THEIR invalid escapes surfaced
    as OUR suite's "12 warnings". parse_foreign_source keeps the tree, records (file, warning) — nothing escapes, and
    a test can assert every recorded file is a pack's, never ours."""
    import warnings
    from basetest import parse_foreign_source, FOREIGN_SYNTAX_WARNINGS
    f = tmp_path / "custom_nodes" / "pack" / "node.py"; f.parent.mkdir(parents=True); f.write_text('import re\nX = re.compile("\\s+\\d")\nclass N: pass\n')
    del FOREIGN_SYNTAX_WARNINGS[:]
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        tree = parse_foreign_source(f)
    assert tree is not None and any(isinstance(n, __import__("ast").ClassDef) for n in tree.body)
    assert escaped == [] and len(FOREIGN_SYNTAX_WARNINGS) >= 1 and FOREIGN_SYNTAX_WARNINGS[0][0] == str(f) and "escape" in FOREIGN_SYNTAX_WARNINGS[0][1]
    assert parse_foreign_source(tmp_path / "missing.py") is None
    g = tmp_path / "bad.py"; g.write_text("def (:\n")
    assert parse_foreign_source(g) is None                                  # a SyntaxError is not a warning: skipped, as before


def test_unit_the_suite_never_inherits_the_runs_tokens_and_a_red_suite_fails_the_run(tmp_path):
    """On the pod (2.0.14) the base's fake-mode tokens test found the REAL HF token in its shell environment — the run had
    exported the pod's tokens and the suite inherited them — and validated it against huggingface.co from inside a test.
    The runner strips every Hub token name from the suite's environment. And the step reported STEP RC=0 with
    "suite: 2 failed": a red suite is a failed run, not a warning."""
    _pod(tmp_path)
    tools = tmp_path / "comfy-base" / "bin"; tools.mkdir(parents=True)
    uv = tools / "uv"; uv.write_text('#!/bin/bash\necho "uv env HF_TOKEN=${HF_TOKEN:-unset} HF_HUB_TOKEN=${HF_HUB_TOKEN:-unset}" >> "$(dirname "$0")/calls.log"\necho "2 failed, 3 passed"\nexit 1\n'); uv.chmod(0o755)
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; export HF_TOKEN=hf_real HF_HUB_TOKEN=hub_real; base_test unit; echo "RC=$BASE_TEST_RC FAILED=${#BASE_FAILED[@]} WARN=${#BASE_WARN[@]}"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "uv env HF_TOKEN=unset HF_HUB_TOKEN=unset" in (tools / "calls.log").read_text(), (tools / "calls.log").read_text()
    assert "RC=1 FAILED=1" in r.stdout and "hf_real" not in r.stdout, r.stdout + r.stderr

    # 2.0.16: the rehearsal's fake step one ran the shipped suite with the OUTER run's mode variables in its environment
    # (BASE_NO_NET=1, BASE_FAKE_ROOT, BASE_DRY…): tests that build their own fake pods inherited them and saw fake clones and
    # skipped stages. The suite gets the documented hand-over (BASE_COMFY, BASE_VENV, BASE_STATE, BASE_ON_POD…) and no other BASE_*.
    uv.write_text('#!/bin/bash\nenv | grep "^BASE_" | sort | tr "\\n" " " >> "$(dirname "$0")/env.log"; echo "1 passed"\nexit 0\n')
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; export BASE_NO_NET=1 BASE_DRY=1 BASE_LATEST=1 BASE_RESTART=1 BASE_FAKE_DL=fail BASE_WORKSPACE=/ws; base_test unit; echo "RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": str(tmp_path / "pid1.env"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    seen = (tools / "env.log").read_text()
    for leaked in ("BASE_NO_NET=", "BASE_FAKE_ROOT=", "BASE_FAKE_PID1_ENV=", "BASE_DRY=", "BASE_LATEST=", "BASE_RESTART=", "BASE_FAKE_DL="):
        assert leaked not in seen, (leaked, seen)
    for handed in ("BASE_COMFY=", "BASE_VENV=", "BASE_STATE=", "BASE_LIB=", "BASE_ON_POD=", "BASE_WORKSPACE=/ws"):   # 3.6.0: the workspace root
        assert handed in seen, (handed, seen)
    assert "RC=0" in r.stdout, r.stdout + r.stderr


def test_unit_a_fake_run_never_validates_a_token_over_the_network(tmp_path):
    """The same pod failure's other half: base_tokens in fake mode reached huggingface.co. A fake run (BASE_FAKE_ROOT,
    BASE_NO_NET) accepts a present token as "not validated (fake run)" and calls nothing."""
    _pod(tmp_path)
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "curl.log"
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl $*" >> "%s"\nexit 0\n' % log); (bin_ / "curl").chmod(0o755)
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; export HF_TOKEN=hf_fake; base_tokens; echo RC=$?',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout and not log.exists() and "whoami" not in r.stdout and "hf_fake" not in r.stdout, r.stdout + r.stderr
    assert "not validated" in r.stdout or "fake" in r.stdout.lower(), r.stdout


def test_unit_start_waits_while_the_process_lives_and_gives_up_when_it_dies(tmp_path):
    """MEASURED 2026-09-08 on pod fakepod0000002: the custom nodes import for over three minutes, so a flat
    180 s window called a healthy restart FAILED and failed the install — the server was up a minute later.
    Waiting longer alone would make a DEAD server cost ten minutes of silence, so the wait is patient while
    the process lives and gives up the moment it does not, saying which happened.

    `_base_comfy_pids` is stubbed here on purpose: this is about the two BRANCHES, and pid discovery has its
    own tests. A fake python cannot be recognised as ComfyUI, so without the stub both branches look alike."""
    import time
    log = tmp_path / "comfy.log"
    py = tmp_path / "python"
    py.write_text("#!/bin/bash\nsleep 30\n"); py.chmod(0o755)
    base = ('BASE_PERSIST_ROOT="%s"; PY="%s"; COMFY="%s"; PORT=8188; HOSTPORT=127.0.0.1:59999; '
            'ARGS_FILE=/nonexistent; COMFY_LOG="%s"; ' % (tmp_path, py, tmp_path, log))

    # the process is gone: give up at once rather than sitting out the window
    t0 = time.time()
    r = _bash(base + '_base_comfy_pids(){ return 0; }; BASE_START_WAIT=60; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout, r.stdout + r.stderr
    assert "did not survive its own start" in r.stdout, r.stdout
    assert time.time() - t0 < 30, "a dead process must not cost the whole window"

    # the process lives and never answers: wait the window out, then say it is still alive
    r = _bash(base + '_base_comfy_pids(){ echo 424242; }; BASE_START_WAIT=6; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout and "still alive" in r.stdout, r.stdout

    # the old tries-based knob still sizes the window, so existing callers keep working
    r = _bash(base + '_base_comfy_pids(){ echo 424242; }; BASE_START_TRIES=2; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout and "after 4 s" in r.stdout, r.stdout


# ---------------------------------------------------------------- T21: the two modes of 2.1.0 (a baked template image)

def test_unit_noninteractive_means_no_tty_and_every_prompt_takes_its_default():
    """BASE_NONINTERACTIVE=1 is the boot on a customer's pod: nobody answers, so the deletion prompt is No, the safe-default
    questions are yes, and BASE_TTY is 0 whatever is attached. BASE_YES=1 still answers the deletion prompt."""
    r = _bash('echo TTY=$BASE_TTY; _base_confirm "delete?" && echo YES || echo NO; _base_confirm_yes "roll back?" && echo SAFE_YES', env={"BASE_NONINTERACTIVE": "1"})
    assert "TTY=0" in r.stdout and "\nNO" in r.stdout and "default No" in r.stdout and "SAFE_YES" in r.stdout, r.stdout + r.stderr
    r2 = _bash('_base_confirm "delete?" && echo YES || echo NO', env={"BASE_NONINTERACTIVE": "1", "BASE_YES": "1"})
    assert "YES" in r2.stdout, r2.stdout
    r3 = _bash('echo "NI=$BASE_NONINTERACTIVE PIN=$BASE_PINNED"; bash -c \'echo "child NI=$BASE_NONINTERACTIVE PIN=$BASE_PINNED"\'', env={"BASE_NONINTERACTIVE": "1", "BASE_PINNED": "1"})
    assert "NI=1 PIN=1" in r3.stdout and "child NI=1 PIN=1" in r3.stdout, r3.stdout   # exported: a package script the boot runs inherits them


def test_unit_the_retired_pinned_mode_freezes_nothing(tmp_path):
    """3.0.0 retired BASE_PINNED and COMFY_TAG: a machine that sets them still moves ComfyUI to its newest release, and
    says the variables are retired; `--latest` is never refused (it is a plain run)."""
    work = _fake_comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PINNED": "1", "COMFY_TAG": "v0.34.0"})
    assert "OLD=0.34.0 NEW=0.35.0" in r.stdout and "retired" in r.stdout + r.stderr, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert "_base_refuse_latest_if_pinned" not in src and "refused in pinned mode" not in src


def test_unit_a_retired_pinned_flag_builds_the_venv_like_any_run(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_PINNED": "1"}
    r = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT FAILED=${#BASE_FAILED[@]}', env=env)
    assert "RESULT=fake FAILED=0" in r.stdout and "retired" in r.stdout, r.stdout + r.stderr
    assert (c / ".venv-cu130" / "bin" / "python").exists()


def test_unit_tokens_export_the_button_names_and_never_a_value(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; echo "B=${HUGGINGFACE_API_KEY:-unset}"; bash -c \'echo "child=${HUGGINGFACE_API_KEY:-unset}"\'',
              env={"BASE_FAKE_ROOT": str(tmp_path), "HF_TOKEN": "hf_abc"})
    assert "exported for the download buttons: HUGGINGFACE_API_KEY" in r.stdout and "B=hf_abc" in r.stdout and "child=hf_abc" in r.stdout, r.stdout + r.stderr
    note_line = [ln for ln in r.stdout.splitlines() if "exported for the download buttons" in ln][0]
    assert "hf_abc" not in note_line
    bare = tmp_path / "bare"; (bare / "ComfyUI").mkdir(parents=True); (bare / "ComfyUI" / "main.py").write_text("")
    r2 = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; echo "B=${HUGGINGFACE_API_KEY:-unset}"', env={"BASE_FAKE_ROOT": str(bare)})
    assert "B=unset" in r2.stdout and "exported for the download buttons" not in r2.stdout, r2.stdout + r2.stderr


# ---------------------------------------------------------------- T22: the workflow's own `models` array, and the template tool (2.1.0)

def test_unit_stamp_models_writes_the_frontend_model_list_from_the_rows_and_is_idempotent(tmp_path):
    """py/stamp_models.py on the stub: every URL row becomes an entry (name, url, directory), LOCAL rows do not, hf:// becomes a
    Hub URL, the file keeps its indent, stamping twice changes nothing, --check reports staleness."""
    import shutil
    STUB = _stub()
    pkg = tmp_path / "stub"; shutil.copytree(STUB, pkg)
    wf = pkg / "stub-workflow.json"
    r = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], capture_output=True, text=True)
    assert r.returncode == 0 and "stamped 1 model entries" in r.stdout, r.stdout + r.stderr
    doc = json.loads(wf.read_text())
    assert doc["models"] == [{"name": "stub_vae.safetensors", "url": "https://huggingface.co/x/y/resolve/main/stub_vae.safetensors", "directory": "vae/Example/Real"}], doc["models"]
    assert doc["nodes"]                                                                     # the rest of the file is intact
    before = wf.read_text()
    r2 = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], capture_output=True, text=True)
    assert r2.returncode == 0 and "current" in r2.stdout and wf.read_text() == before
    assert subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg), "--check"], capture_output=True, text=True).returncode == 0
    doc["models"] = []; wf.write_text(json.dumps(doc, indent=2) + "\n")
    r3 = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg), "--check"], capture_output=True, text=True)
    assert r3.returncode == 1 and "STALE" in r3.stdout
    # an hf:// snapshot row becomes a repository URL with the directory as its name
    sc = pkg / "stub-script.sh"
    sc.write_text(sc.read_text().replace(' "loras|SDXL|Detailers|stub_local.safetensors|LOCAL|0|a file that only exists on the pod|"',
                                         ' "RMBG|||BEN2/|hf://1038lab/BEN2|100|a snapshot|"'))
    subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], check=True, capture_output=True)
    names = {(e["name"], e["url"], e["directory"]) for e in json.loads(wf.read_text())["models"]}
    assert ("BEN2", "https://huggingface.co/1038lab/BEN2", "RMBG") in names
    r4 = _bash('bash "$BASE_DIR/base.sh" stamp-models "%s" --check; echo RC=$?' % pkg)
    assert "RC=0" in r4.stdout, r4.stdout + r4.stderr


def test_unit_every_shipped_workflow_models_array_equals_its_rows():
    """All instances: every converted package's workflow carries the `models` array its rows say (2.1.0). A package not yet
    stamped is reported by name and skipped, never silently passed."""
    from basetest import converted_packages
    root = _consumer_root()
    unstamped, stale = [], []
    for d in converted_packages(root):
        s = d / (d.name + "-script.sh")
        if not s.exists():
            continue
        m = re.search(r'^[^#\n]*?\bWF_NAME="([^"]*)"', s.read_text(encoding="utf-8"), re.M)
        if not m or not m.group(1) or not (d / m.group(1)).exists():
            continue
        doc = json.loads((d / m.group(1)).read_text(encoding="utf-8"))
        if "models" not in doc:
            unstamped.append(d.name); continue
        r = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(d), "--check"], capture_output=True, text=True)
        if r.returncode != 0:
            stale.append((d.name, r.stdout.strip()))
    assert not stale, stale
    if unstamped:
        pytest.skip("not stamped yet (run `base.sh stamp-models <dir>` in each package, with its bump): %s" % ", ".join(unstamped))



def test_unit_the_shared_library_link_replaces_comfyui_placeholders_but_never_real_models(tmp_path):
    """MEASURED: a freshly materialised ComfyUI tree is never empty. It ships a placeholder in every category
    folder (put_checkpoints_here and friends), so counting "has files" as "has models" made the link refuse on
    exactly the fresh machine it exists for, sending every machine back to its own copy of the library, silently
    and looking like it had worked."""
    lib = tmp_path / "library"
    (lib / "models").mkdir(parents=True)

    comfy = tmp_path / "ComfyUI"
    for d in ("checkpoints", "loras", "vae"):
        (comfy / "models" / d).mkdir(parents=True)
        (comfy / "models" / d / ("put_%s_here" % d)).write_text("")
    r = _bash(f'COMFY="{comfy}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy}/extra_model_paths.yaml"; _base_library_link')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (comfy / "models").is_symlink(), "placeholders are not content: it must link. said: " + r.stdout
    assert os.readlink(str(comfy / "models")) == str(lib / "models")
    # with models/ pointing AT the library, a search path to the same place would list every model twice
    assert not (comfy / "extra_model_paths.yaml").exists(), r.stdout

    # a real model IS content: never moved without being asked, and the library becomes a search path instead
    comfy2 = tmp_path / "ComfyUI2"
    (comfy2 / "models" / "loras").mkdir(parents=True)
    (comfy2 / "models" / "loras" / "put_loras_here").write_text("")
    (comfy2 / "models" / "loras" / "real.safetensors").write_text("x")
    r2 = _bash(f'COMFY="{comfy2}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy2}/extra_model_paths.yaml"; _base_library_link')
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert not (comfy2 / "models").is_symlink(), "a tree holding a real model must be left alone"
    assert (comfy2 / "models" / "loras" / "real.safetensors").exists(), "it must never delete a model"
    assert "real.safetensors" in r2.stdout, "it must name what stopped it: " + r2.stdout
    y = (comfy2 / "extra_model_paths.yaml").read_text()
    assert "comfy-library:" in y and str(lib / "models") in y

    # the migration this function's own warning tells people to run: move the models, run again. The link is made
    # AND the now-redundant search path is dropped, or every model would be listed twice in every dropdown.
    (comfy2 / "models" / "loras" / "real.safetensors").unlink()
    r2b = _bash(f'COMFY="{comfy2}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy2}/extra_model_paths.yaml"; _base_library_link')
    assert r2b.returncode == 0, r2b.stdout + r2b.stderr
    assert (comfy2 / "models").is_symlink(), r2b.stdout
    assert not (comfy2 / "extra_model_paths.yaml").exists(), (
        "the search path must go when models/ becomes the library itself, or every model is listed twice: " + r2b.stdout)

    # no library configured: nothing happens at all, on any host
    comfy3 = tmp_path / "ComfyUI3"
    (comfy3 / "models").mkdir(parents=True)
    r3 = _bash(f'COMFY="{comfy3}"; BASE_LIBRARY=""; EXTRA_YAML="{comfy3}/extra_model_paths.yaml"; _base_library_link')
    assert r3.returncode == 0 and not (comfy3 / "models").is_symlink()
    assert not (comfy3 / "extra_model_paths.yaml").exists()


def test_unit_the_install_lock_serialises_installs_only_on_a_shared_volume(tmp_path):
    """2.5.0: with the whole root on a store several machines mount, two installs writing one venv corrupts it,
    because pip and uv write in place and neither expects a second writer. RUNNING is never locked: running only
    reads, which is what makes several GPUs on one store safe. mkdir is the lock because it is atomic over NFSv4."""
    vol = tmp_path / "vol"; (vol / "comfy-base" / "state").mkdir(parents=True)
    lock = vol / "comfy-base" / "state" / "install.lock"
    env = {"BASE_VOLUME": str(vol), "BASE_HOST": "local", "BASE_VOLUME_SHARED": "1", "BASE_DRY": "0"}

    # a real run installs the traps first (base_main does), and the EXIT trap is what releases the lock, so a
    # finished run never blocks the next machine. Without the traps the lock would outlive the process, which is
    # why this asserts the release rather than assuming it.
    r = _bash('_base_install_traps; _base_install_lock && echo GOT', env=env)
    assert "GOT" in r.stdout, r.stdout + r.stderr
    assert not lock.exists(), "the exit trap must release the lock: " + r.stdout

    # a lock a LIVE run holds is honoured
    lock.mkdir()
    (lock / "owner").write_text("host=other-machine\npid=1\nsince=%d\n" % int(__import__("time").time()))
    r = _bash('_base_install_lock && echo GOT || echo BLOCKED', env={**env, "BASE_LOCK_WAIT": "10"})
    assert "GOT" not in r.stdout, "a live lock must not be taken: " + r.stdout
    assert "other-machine is installing" in r.stdout, r.stdout

    # a lock whose owner died is honoured for two hours, then broken WITH THE OWNER NAMED
    (lock / "owner").write_text("host=dead-machine\npid=1\nsince=%d\n" % (int(__import__("time").time()) - 10800))
    r = _bash('_base_install_lock && echo GOT', env=env)
    assert "GOT" in r.stdout and "breaking" in r.stdout and "dead-machine" in r.stdout, r.stdout

    # and none of it happens when the root is the machine's own disk
    lock.mkdir(exist_ok=True)
    r = _bash('_base_install_lock && echo GOT', env={**env, "BASE_VOLUME_SHARED": "0"})
    assert "GOT" in r.stdout and "installing" not in r.stdout, r.stdout


def test_unit_auth_file_reads_its_own_argument_not_the_callers_scope(tmp_path):
    """MEASURED on a machine (2.5.1): `local name="$1" f="...$name"` expands every word BEFORE local binds any, so
    the second `$name` read the CALLER's scope. It worked only because every in-tree caller happened to hold `name`
    set to the same token name. A caller with `name=""` wrote `.auth-` carrying the WRONG value and no error, so a
    bearer token for the wrong name went out and read as a plain auth failure."""
    env = {"BASE_VOLUME": str(tmp_path), "BASE_HOST": "local", "HF_TOKEN": "tok-correct", "OTHER_TOKEN": "tok-wrong"}
    # the caller holds `name` set to something ELSE, which is the silent-wrong case
    r = _bash('_base_tmp; name=OTHER_TOKEN; f="$(_base_auth_file HF_TOKEN)"; echo "PATH=$(basename "$f")"; cat "$f"', env=env)
    assert "PATH=.auth-HF_TOKEN" in r.stdout, "the file must be named for the ARGUMENT: " + r.stdout + r.stderr
    assert "Bearer tok-correct" in r.stdout, "the wrong token went out: " + r.stdout
    assert "tok-wrong" not in r.stdout
    # and a caller with no `name` in scope at all must not abort under set -u
    r = _bash('set -u; _base_tmp; f="$(_base_auth_file HF_TOKEN)"; echo "OK=$(basename "$f")"', env=env)
    assert "OK=.auth-HF_TOKEN" in r.stdout, r.stdout + r.stderr

    # the third outcome, and the one the split declaration alone cannot prevent: a name the CALLER passes with a
    # slash in it makes the write fail in a subshell whose failure was discarded, and the function used to hand
    # back a path to a file it had not created. curl then said "option -H: error encountered when reading a file"
    # and HTTP 000, which points at curl and the vendor rather than here. It must name itself instead.
    r = _bash('_base_tmp; f="$(_base_auth_file "a/b")" && echo "RETURNED=$f" || echo "REFUSED"', env=env)
    assert "REFUSED" in r.stdout and "RETURNED=" not in r.stdout, "a failed write must not return a path: " + r.stdout
    assert "could not write the auth file" in r.stdout or "could not write the auth file" in r.stderr, r.stdout + r.stderr


def test_unit_the_library_link_ignores_everything_comfyui_itself_ships(tmp_path):
    """2.5.1: the placeholder filter covered put_*_here and missed models/configs/v1-inference*.yaml, which ComfyUI
    ships too, so the refusal fired on every fresh machine and printed advice nobody could act on: the files it
    objected to were ComfyUI's own."""
    lib = tmp_path / "library"; (lib / "models").mkdir(parents=True)
    comfy = tmp_path / "ComfyUI"
    (comfy / "models" / "configs").mkdir(parents=True)
    (comfy / "models" / "checkpoints").mkdir(parents=True)
    (comfy / "models" / "checkpoints" / "put_checkpoints_here").write_text("")
    (comfy / "models" / "configs" / "v1-inference.yaml").write_text("model: {}\n")
    (comfy / "models" / "configs" / "v1-inference_clip_skip_2.yaml").write_text("model: {}\n")
    r = _bash(f'COMFY="{comfy}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy}/extra_model_paths.yaml"; _base_library_link')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (comfy / "models").is_symlink(), "ComfyUI's own configs are not the operator's models: " + r.stdout
    # and they are not thrown away either: the shared tree should look like a ComfyUI models/ does
    assert (lib / "models" / "configs" / "v1-inference.yaml").exists(), "the configs must move to the library, not vanish"
    # a real model still stops it, which is the whole point of the guard
    c2 = tmp_path / "ComfyUI2"; (c2 / "models" / "vae").mkdir(parents=True)
    (c2 / "models" / "vae" / "real.safetensors").write_text("x")
    r2 = _bash(f'COMFY="{c2}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{c2}/extra_model_paths.yaml"; _base_library_link')
    assert not (c2 / "models").is_symlink() and "real.safetensors" in r2.stdout, r2.stdout


def test_unit_the_workflow_is_written_where_the_server_actually_reads_it():
    """2.5.6. 2.5.0 moved the SERVER's --user-directory to BASE_LOCAL_STATE, because two machines
    sharing one store cannot share one user/, and left every WRITER pointing at $COMFY/user.

    So on a shared-root machine the installer put each workflow in ComfyUI's own tree while the
    server read the machine's, and `GET /api/userdata?dir=workflows` answered `[]`. No package
    could be opened from the server's own workflow list by name, which is precisely what an App
    Mode pass is required to do (root CLAUDE.md §1 phase 3). Measured on three machines on
    2026-09-18, one per brand, each with two workflows sitting in a directory nothing served.

    One accessor now answers it, so the two halves cannot drift apart again."""
    shared = _bash('COMFY=/w/ComfyUI BASE_LOCAL_STATE=/var/lib/m BASE_VOLUME_SHARED=1 _base_workflows_dir')
    assert shared.stdout.strip() == "/var/lib/m/user/default/workflows", shared.stdout + shared.stderr
    own = _bash('COMFY=/w/ComfyUI BASE_LOCAL_STATE=/var/lib/m BASE_VOLUME_SHARED=0 _base_workflows_dir')
    assert own.stdout.strip() == "/w/ComfyUI/user/default/workflows", own.stdout + own.stderr
    # unset, not just 0: a non-shared machine never sets it at all
    bare = _bash('COMFY=/w/ComfyUI BASE_LOCAL_STATE=/var/lib/m; unset BASE_VOLUME_SHARED; _base_workflows_dir')
    assert bare.stdout.strip() == "/w/ComfyUI/user/default/workflows", bare.stdout + bare.stderr


def test_unit_no_lib_file_hardcodes_comfyuis_own_user_directory():
    """The companion to the test above, and the reason it exists separately: the bug was several
    call sites agreeing with each other and disagreeing with one.

    2.5.8 widened it from `workflows` to the WHOLE of $COMFY/user, because scoping it to the path
    that had bitten let an identical bug sit one file away. `comfy.settings.json` was written to
    ComfyUI's own tree while the server read the machine's, so this package's own hygiene fix,
    `pysssss.ImageFeed.Location=hidden`, never took effect and the image feed sat on top of App
    Mode's Run button. Every click landed on a DIV.pysssss-image-feed-menu, no POST /prompt was
    ever made, and a whole sweep reported "did not queue" with no reason. Measured 2026-09-19.

    A house rule applies to every instance of its pattern, never the one that happened to bite."""
    bad = []
    for p in LIB:
        inside_accessor = False
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            # The accessor's own body is the one place $COMFY/user is the RIGHT answer: it is what
            # a machine that is not on a shared root should use.
            if line.startswith("_base_user_dir()"):
                inside_accessor = True
            elif inside_accessor and line.startswith("}"):
                inside_accessor = False
            if inside_accessor:
                continue
            # $COMFY/user is ComfyUI's OWN tree, which is the wrong tree on a shared-root machine.
            # A bare relative "user/default/workflows" in the scaffold that lays out a fresh ComfyUI
            # (20-comfyui.sh) is fine and must stay: it should exist whether or not this machine
            # serves from it. A FALLBACK after the accessor is fine too, which is how 70-hygiene
            # still finds a Manager config an older install left behind.
            if "$COMFY/user" not in line or line.lstrip().startswith("#"):
                continue
            if "_base_user_dir" in line:            # the accessor first, $COMFY/user only as a fallback
                continue
            bad.append(f"{p.name}:{n}: {line.strip()}")
    assert not bad, ("these write into ComfyUI's own user/, which the server may not read:\n"
                     + "\n".join(bad))


# ---------------------------------------------------------------- 2.7.0: Comfy MCP

def test_unit_comfy_mcp_never_phones_home():
    """comfy-cli depends on mixpanel and posthog. Rule 9 says the outbound hosts are an allowlist and nothing
    is uploaded, and the wheel's telemetry is not something the allowlist test can see, because it is not our
    source. The environment is the mechanism: comfy-cli's tracking module honours the DO_NOT_TRACK convention
    by never importing the telemetry extension at all. So both switches must be set where the stage runs AND
    in the boot environment — otherwise a machine that reboots comes back chattier than it was installed."""
    stage = (BASE / "lib" / "45-mcp.sh").read_text()
    assert "export DO_NOT_TRACK=1 COMFY_NO_TELEMETRY=1" in stage, "the stage must kill telemetry before the wheel runs"
    assert "tracking disable" in stage, "the config file is the belt to the environment's braces"
    boot = (BASE / "lib" / "boot.sh").read_text()
    assert "DO_NOT_TRACK=1" in boot and "COMFY_NO_TELEMETRY=1" in boot, "a rebooted machine must come back quiet"


def test_unit_the_mcp_stage_is_optional_and_never_fails_a_run():
    stage = (BASE / "lib" / "45-mcp.sh").read_text()
    assert "${BASE_MCP:-1}" in stage, "BASE_MCP=0 must skip the stage entirely"
    body = "\n".join(l for l in stage.splitlines() if not l.strip().startswith("#"))
    assert "return 1" not in body, "base_mcp must never fail an install: every exit is return 0"


def test_unit_the_mcp_stage_runs_after_the_packs_and_before_the_import_check():
    """The venv and the packs have to exist; the models do not, and MCP needs none of them."""
    src = (BASE / "lib" / "95-summary.sh").read_text()
    run = src[src.index("base_install(){"):]
    run = run[:run.index("\n}")]
    assert "base_mcp" in run, "base_mcp is never called"
    assert run.index("base_packs pip") < run.index("base_mcp") < run.index("base_venv_latest") < run.index("base_import_check"), run
    assert run.index("base_system") < run.index("_base_driver_gate") < run.index("_base_install_lock") < run.index("base_venv_snapshot") < run.index("if ! base_venv"), run


def test_unit_sageattention_follows_main_and_a_commit_is_no_longer_a_pin():
    """3.0.0: SAGE_REF names a branch or a tag (default main); a 40-hex SAGE_REF and SAGE_WHEEL are retired, because each
    held SageAttention below its newest. It is rebuilt whenever its full cache key (commit, Python, torch, sm) moves."""
    body = (BASE / "lib" / "40-packs.sh").read_text()
    sage = body[body.index("base_build_sageattention()"):body.index("base_consolidate()")]
    assert "SAGE_REF:-main" in sage and '--branch "$ref"' in sage and "retired in 3.0.0" in sage
    assert "SAGE_WHEEL=<url-or-path>" not in sage and '_base_pip install -q "$SAGE_WHEEL"' not in sage
    assert ".comfy-base-sageattention" in sage and '"$key" > "$stampf"' in sage


def test_unit_the_testbed_finds_a_consumer_repositorys_packages(tmp_path):
    """2.6.0 moved testbed.sh INTO the base, which changed $HERE by one level and silently broke the glob that
    finds a consumer repository's packages: `$HERE/../*/packages/*/` from <repo>/base/comfyui-base resolves to
    <repo>/base/*/packages/*/ and matches nothing. The testbed would then provision the base's own packs and
    none of the package's, and the comfyui tier would run against a tree missing the very packs it exists to
    exercise - green, and meaningless. The root is searched for now, and a candidate only counts when it holds
    a <name>/<name>-script.sh, because two levels up from a standalone base is an unrelated directory."""
    _repo_only("the repo's testbed.sh")
    tbsh = next((q for q in (BASE / "testbed.sh", BASE.parent / "testbed.sh") if q.exists()), None)
    if tbsh is None:
        pytest.skip("no testbed.sh in or beside the base")
    base = tmp_path / "base" / "comfyui-base"
    (base / "lib").mkdir(parents=True)
    for f in ("base.sh", "VERSION", "pytest.ini"):
        (base / f).write_text((BASE / f).read_text())
    for q in (BASE / "lib").glob("*.sh"):
        (base / "lib" / q.name).write_text(q.read_text())
    (base / "py").mkdir()
    (base / "py" / "pkgdirs.py").write_text((BASE / "py" / "pkgdirs.py").read_text())   # 3.6.0: where testbed.sh asks
    (base / "testbed.sh").write_text(tbsh.read_text())
    pkg = tmp_path / "brand" / "packages" / "pkg-x"
    pkg.mkdir(parents=True)
    (pkg / "pkg-x-script.sh").write_text('PKG_ID="pkg-x"\n')

    r = subprocess.run(["bash", str(base / "testbed.sh"), "--status"], capture_output=True, text=True, env=_no_workspace_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "packages under %s" % tmp_path.resolve() in r.stdout, r.stdout + r.stderr
    assert "no ComfyUI Base beside this script" not in (r.stdout + r.stderr), r.stdout

    # And a standalone base, which has no packages, must NOT claim a root. It is placed two levels deep in an
    # otherwise empty tree: a copy beside `brand/` would legitimately find it one level up, which is the same
    # thing a checkout sitting next to a brand tree does, and is not what this asserts.
    solo = tmp_path / "iso" / "deep" / "solo"
    solo.parent.mkdir(parents=True)
    shutil.copytree(base, solo)
    r2 = subprocess.run(["bash", str(solo / "testbed.sh"), "--status"], capture_output=True, text=True, env=_no_workspace_env())
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "packages under" not in r2.stdout, r2.stdout


def test_unit_the_testbed_test_holds_with_a_workspace_exported(tmp_path, monkeypatch):
    """3.6.0: base_test hands BASE_WORKSPACE to the suite, and testbed.sh takes it over the submodule shape. The test
    above builds its own submodule tree, so it must not inherit a session's workspace: with BASE_WORKSPACE exported
    to an empty directory, the temp submodule tree still wins."""
    _repo_only("the repo's testbed.sh")
    ws = tmp_path / "empty-ws"; ws.mkdir()
    monkeypatch.setenv("BASE_WORKSPACE", str(ws))
    tree = tmp_path / "tree"; tree.mkdir()
    test_unit_the_testbed_finds_a_consumer_repositorys_packages(tree)


# ---------------------------------------------------------------- 3.0.3: renders on the store, one ComfyUI per machine

def test_unit_hygiene_removes_output_and_input_dirs_that_are_not_on_the_store(tmp_path):
    """3.0.3, MEASURED on a Verda machine on a shared store: comfyui_args.txt still carried --output-directory and
    --input-directory from when the library was a second mount. 2.5.2 dropped that library under a shared root but the
    hygiene only ever ENSURED the flags, so ComfyUI made the old mount point on the OS disk and wrote every render and
    upload there. Without a library, a value off the store is removed (ComfyUI then writes inside its tree, on the
    store); a value on the store stays; a package that names the flag in HYGIENE_ARGS keeps its own choice."""
    _pod(tmp_path)
    b = _base_args(tmp_path); b.parent.mkdir(parents=True)
    b.write_text("# comfy-base launch args\n--preview-method auto\n"
                 "--output-directory /mnt/comfy-library/output\n--input-directory /mnt/comfy-library/input\n--fast\n")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_VOLUME_SHARED": "1", "BASE_LOCAL_STATE": str(tmp_path / "machine")}
    run = 'base_env_setup 2>/dev/null; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}'
    r = _bash(run, env=env)
    txt = b.read_text()
    assert "/mnt/comfy-library" not in txt, txt + r.stdout + r.stderr
    assert "--output-directory" not in txt and "--input-directory" not in txt and "--fast" in txt, txt
    assert "--user-directory %s/user" % (tmp_path / "machine") in txt, txt      # the shared-root flags are untouched
    assert "not on the store" in r.stdout, r.stdout
    r2 = _bash(run, env=env)
    assert "CHANGED=0" in r2.stdout and b.read_text() == txt, r2.stdout            # idempotent

    # a value ON the store is the store's, and stays
    on = tmp_path / "renders"
    b.write_text(txt + "--output-directory %s\n" % on)
    r = _bash(run, env=env)
    assert "--output-directory %s" % on in b.read_text() and "is on the store" in r.stdout, r.stdout

    # a package's own HYGIENE_ARGS choice is not undone and redone on every run
    b.write_text(txt)
    pkg = 'HYGIENE_ARGS=("--input-directory /data/inbox"); ' + run
    _bash(pkg, env=env)
    r = _bash(pkg, env=env)
    assert "--input-directory /data/inbox" in b.read_text() and "CHANGED=0" in r.stdout, b.read_text() + r.stdout


def test_unit_launch_line_leaves_off_a_directory_on_the_os_disk_and_never_makes_it(tmp_path):
    """3.0.3: the boot runs no hygiene, so the launch line is the last guard. With the store a mount of its own, an
    --output-directory or --input-directory on the OS disk (a library that is not mounted, a stale args file) is left
    off the launch line and never created; ComfyUI then writes inside its tree, on the store. `_base_mount_of` is
    stubbed: which mount holds a path is the machine's fact, and the suite has one disk."""
    store = tmp_path / "store"; home = store / "comfy-base"; (home / "state").mkdir(parents=True)
    off = tmp_path / "osdisk" / "comfy-library"
    args = home / "state" / "comfyui_args.txt"
    args.write_text("# args\n--output-directory %s/output --input-directory %s/input\n--user-directory %s/u\n--fast\n" % (off, store, store))
    base = 'BASE_HOME="%s"; ARGS_FILE="%s"; COMFY="%s/ComfyUI"; PY=python; PORT=8188; ' % (home, args, store)
    stub = '_base_mount_of(){ case "$1" in /|"%s"*) echo /;; *) echo /store;; esac; }; ' % (tmp_path / "osdisk")
    r = _bash(base + stub + '_base_launch_args; _base_local_dirs; echo "CMD $(_base_start_cmd)"')
    words = r.stdout.splitlines()
    assert "%s/output" % off not in r.stdout and "left off the launch line" in r.stderr, r.stdout + r.stderr
    assert words[:6] == ["--input-directory", "%s/input" % store, "--user-directory", "%s/u" % store, "--fast", words[5]], words
    assert not (off / "output").exists() and not off.exists()                      # never made on the OS disk
    assert (store / "u").is_dir()                                                  # the per-machine one still is
    cmd = [l for l in words if l.startswith("CMD ")][0]
    assert str(off) not in cmd and "--input-directory %s/input" % store in cmd, cmd
    # an owned box whose store IS the OS disk has no second disk to land on by mistake: nothing is left off
    r = _bash(base + '_base_mount_of(){ echo /; }; _base_launch_args')
    assert "%s/output" % off in r.stdout and "left off" not in r.stderr, r.stdout + r.stderr


def test_unit_restart_goes_through_the_boot_unit_when_the_unit_owns_comfyui(tmp_path):
    """3.0.3, MEASURED on a Verda machine: the install's BASE_RESTART=1 started ComfyUI detached (ppid 1), outside
    comfy-base-boot.service's cgroup, so a later `systemctl restart comfy-base-boot` started a SECOND ComfyUI beside it
    (two main.py; the old one held :8188 and 34 GB of VRAM). Where the unit owns ComfyUI, the restart is the unit's:
    stop the unit, stop any ComfyUI it never owned, start the unit, wait with a grace for the boot's own stages."""
    stubs = ('_base_root(){ echo "ROOT $*"; }; _base_stop_comfy(){ echo STOP_ALL; }; '
             '_base_wait_comfy(){ echo "WAIT ${1:-0}"; }; _base_start_comfy(){ echo DIRECT_START; }; ')
    r = _bash(stubs + '_base_boot_unit_owns(){ return 0; }; _base_restart_comfy; echo "RC=$? VIA=$BASE_RESTART_VIA"')
    lines = [l for l in r.stdout.splitlines() if l.split(" ")[0] in ("ROOT", "STOP_ALL", "WAIT", "DIRECT_START", "RC=0")]
    assert lines == ["ROOT systemctl stop comfy-base-boot.service", "STOP_ALL", "ROOT systemctl start comfy-base-boot.service",
                     "WAIT 180", "RC=0 VIA=unit"], r.stdout + r.stderr
    # no unit (RunPod, an owned box without systemd): every ComfyUI stops, then one starts directly
    r = _bash(stubs + '_base_boot_unit_owns(){ return 1; }; _base_restart_comfy; echo "RC=$? VIA=$BASE_RESTART_VIA"')
    assert "STOP_ALL\nDIRECT_START\nRC=0 VIA=direct" in r.stdout and "ROOT" not in r.stdout, r.stdout
    # the unit cannot be stopped (no root, no sudo): said, then the direct start, which the unit's next start replaces
    r = _bash(stubs.replace('_base_root(){ echo "ROOT $*"; }', '_base_root(){ return 1; }')
              + '_base_boot_unit_owns(){ return 0; }; _base_restart_comfy; echo "RC=$? VIA=$BASE_RESTART_VIA"')
    assert "could not stop comfy-base-boot" in r.stdout and "DIRECT_START" in r.stdout and "VIA=direct" in r.stdout, r.stdout
    # a ComfyUI that survives TERM and KILL: nothing is started beside it
    r = _bash(stubs.replace("_base_stop_comfy(){ echo STOP_ALL; }", "_base_stop_comfy(){ return 1; }")
              + '_base_boot_unit_owns(){ return 1; }; _base_restart_comfy; echo "RC=$?"')
    assert "RC=1" in r.stdout and "DIRECT_START" not in r.stdout, r.stdout
    # base_restart itself never kills or starts on its own any more: one way in, _base_restart_comfy
    src = code_only((BASE / "lib" / "80-server.sh").read_text())
    body = src[src.index("base_restart()"):src.index("base_smoke()")]
    assert "kill " not in body.replace("echo \"        kill", "") and "_base_start_comfy" not in body, body


def test_unit_the_unit_owns_comfyui_only_on_a_systemd_host_with_it_enabled_and_from_outside_it(tmp_path):
    """The predicate behind the restart: never RunPod (a container) nor a fake root; the unit enabled; and the caller NOT
    inside the unit's own cgroup (a JupyterLab terminal, a boot extension), whose `systemctl stop` would stop the caller."""
    bin_ = tmp_path / "bin"; bin_.mkdir()
    sc = bin_ / "systemctl"; sc.write_text('#!/bin/bash\n[ "$1" = is-enabled ] && exit "${UNIT_ENABLED_RC:-0}"; exit 0\n'); sc.chmod(0o755)
    env = {"PATH": "%s:%s" % (bin_, os.environ["PATH"])}
    q = '_base_boot_unit_owns && echo OWNS || echo NO'
    assert "NO" in _bash('BASE_HOST=runpod; ' + q, env=env).stdout
    assert "NO" in _bash('BASE_HOST=verda; BASE_FAKE_ROOT=/x; ' + q, env=env).stdout
    live = 'BASE_HOST=verda; _base_systemd_live(){ return 0; }; '
    assert "OWNS" in _bash(live + '_base_in_boot_unit(){ return 1; }; ' + q, env=env).stdout
    assert "NO" in _bash(live + '_base_in_boot_unit(){ return 0; }; ' + q, env=env).stdout
    assert "NO" in _bash(live + '_base_in_boot_unit(){ return 1; }; ' + q, env={**env, "UNIT_ENABLED_RC": "1"}).stdout
    # the hand-off names the unit, never a nohup line that would start the detached second ComfyUI
    r = _bash(live + 'COMFY=/c; _base_restart_hint', env=env)
    assert "systemctl restart comfy-base-boot" in r.stdout and "nohup" not in r.stdout, r.stdout


def test_unit_stop_comfy_stops_every_server_and_waits_for_it(tmp_path):
    """_base_stop_comfy is TERM, a wait, then KILL, and says which pids it stopped. The process here is a real one,
    orphaned so init reaps it (a zombie child would still answer kill -0), and the matcher is stubbed to name it."""
    r = _bash('P=$( (sleep 120 >/dev/null 2>&1 & echo $!) ); _base_comfy_pids(){ kill -0 "$P" 2>/dev/null && echo "$P"; return 0; }; '
              '_base_stop_comfy; echo "RC=$?"; kill -0 "$P" 2>/dev/null && { echo ALIVE; kill -9 "$P"; } || echo GONE')
    assert "stopping ComfyUI:" in r.stdout and "RC=0" in r.stdout and "GONE" in r.stdout, r.stdout + r.stderr
    # nothing running: nothing said, success
    r = _bash('_base_comfy_pids(){ return 0; }; _base_stop_comfy; echo "RC=$?"')
    assert r.stdout.strip() == "RC=0", r.stdout


def test_unit_the_boot_stops_a_comfyui_it_does_not_own_before_starting_its_own():
    """3.0.3: at boot_comfy this boot has started nothing, so a ComfyUI already running is one it does not own (an earlier
    install's detached start). It is stopped before the boot's own starts, so `systemctl restart comfy-base-boot` replaces
    the server instead of adding a second. The boot now has the matcher, which 85-launch.sh carries since 3.0.3."""
    src = code_only((BASE / "lib" / "boot.sh").read_text())
    body = src[src.index("boot_comfy()"):src.index("boot_main()")]
    assert "_base_stop_comfy" in body and body.index("_base_stop_comfy") < body.index("if _base_start_comfy"), body
    r = subprocess.run(["bash", "-c", 'BOOT_HOME=/nonexistent; source "%s/lib/boot.sh"; boot_launch_lib; '
                        'declare -F _base_comfy_pids _base_pids_from_table _base_stop_comfy _base_wait_comfy' % BASE],
                       capture_output=True, text=True)
    assert r.returncode == 0 and len(r.stdout.split()) == 4, r.stdout + r.stderr

