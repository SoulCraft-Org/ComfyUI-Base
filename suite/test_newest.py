"""unit tier, 3.0.0: always the newest, for everything the base installs. Every test runs the real library against a
throwaway tree, with uv, apt and the GPU tools replaced by stand-ins on PATH; nothing here reaches the network or the OS."""
import json, os, pathlib, re, shutil, stat, subprocess, sys, textwrap, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]


def _bash(snippet, env=None, cwd=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("BASE_", "COMFY_REF", "COMFY_TAG", "SAGE_")):
            e.pop(k)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e, cwd=cwd)


def _stub(d, name, body):
    p = pathlib.Path(d) / name
    p.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------- A: ComfyUI, the newest release or a newer COMFY_REF

def _comfy_repo(tmp_path):
    """v0.34.0, v0.35.0 on master, one untagged master commit after it (still reporting 0.35.0, as master does), and
    v0.35.1 on a side branch cut from v0.35.0: the shape ComfyUI has (v0.37.x beside master)."""
    origin = tmp_path / "origin.git"; work = tmp_path / "ComfyUI"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(work))
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    def commit(ver, msg, tag=None):
        (work / "main.py").write_text(msg); (work / "comfyui_version.py").write_text(f'__version__ = "{ver}"\n')
        _git("add", ".", cwd=work); _git(*cfg, "commit", "-q", "-m", msg, cwd=work)
        if tag:
            _git("tag", tag, cwd=work)
    _git("checkout", "-q", "-b", "master", cwd=work)
    commit("0.34.0", "a", "v0.34.0"); commit("0.35.0", "b", "v0.35.0")
    _git("checkout", "-q", "-b", "release-0.35", cwd=work); commit("0.35.1", "side", "v0.35.1")
    _git("checkout", "-q", "master", cwd=work); commit("0.35.0", "master-fix")
    master = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", "master", "release-0.35", "--tags", cwd=work)
    _git("checkout", "-q", "v0.34.0", cwd=work)
    return work, master


def test_unit_comfyui_default_is_the_newest_release_even_on_a_side_branch(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW; echo INFO=$COMFY_REF_INFO; git -C "$COMFY" rev-parse --abbrev-ref HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "OLD=0.34.0 NEW=0.35.1" in r.stdout and "INFO=v0.35.1 @" in r.stdout and "comfy-base-stable" in r.stdout, r.stdout + r.stderr


def test_unit_comfy_ref_master_is_honoured_and_says_what_it_reports(tmp_path):
    work, master = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo NEW=$COMFY_NEW; echo INFO=$COMFY_REF_INFO; echo FAILED=${#BASE_FAILED[@]}; git -C "$COMFY" rev-parse HEAD',
              env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": "master"})
    assert master in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "INFO=master @" in r.stdout and "newest release v0.35.1" in r.stdout, r.stdout   # master reports 0.35.0, below the release


def test_unit_comfy_ref_full_sha_works_and_a_short_one_is_refused(tmp_path):
    work, master = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}; git -C "$COMFY" rev-parse HEAD', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": master})
    assert r.stdout.strip().endswith(master) and "FAILED=0" in r.stdout, r.stdout + r.stderr
    r2 = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": master[:12]})
    assert "FAILED=1" in r2.stdout and "full 40-hex" in r2.stdout, r2.stdout + r2.stderr


def test_unit_comfy_ref_older_than_the_newest_release_is_refused(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=work)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": "v0.34.0"})
    assert "FAILED=1" in r.stdout and "OLDER than the newest release" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=work) == before


def test_unit_retired_comfy_tag_and_pinned_are_notes_never_freezes(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo NEW=$COMFY_NEW', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_TAG": "v0.34.0", "BASE_PINNED": "1"})
    assert "NEW=0.35.1" in r.stdout and "retired" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_unit_dry_run_reads_the_refs_own_version_and_leaves_the_tree(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=work)
    r = _bash("COMFY_MIN=0.35.0; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate; echo GATE=$? NEW=$COMFY_NEW",
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1", "COMFY_REF": "master"})
    assert "GATE=0 NEW=0.35.0" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=work) == before


# ---------------------------------------------------------------- C: py/reqlift.py lifts every upstream hold

def test_unit_reqlift_lifts_holds_keeps_floors_markers_and_names_the_pack(tmp_path):
    (tmp_path / "inc.txt").write_text("included==1.0\n")
    (tmp_path / "req.txt").write_text(textwrap.dedent("""\
        comfyui-frontend-package==1.52.7   # pinned by ComfyUI
        protobuf>=3.20.2,<6.0.0
        pydantic~=2.0
        numpy>=1.25.0,!=2.0.1
        pywin32==306 ; platform_system == "Windows"
        requests[socks]<=2.0
        torch==2.1.0
        torchsde==0.2.6
        -r inc.txt
        -c constraints.txt
        --index-url https://example.invalid/simple
        git+https://github.com/o/r.git@v1.2#egg=thing
        withhash==1.0 --hash=sha256:abc
        https://example.invalid/a-1.0.whl
        """))
    out, mp = tmp_path / "derived.txt", tmp_path / "map.tsv"
    r = subprocess.run([sys.executable, str(BASE / "py" / "reqlift.py"), "--out", str(out), "--map", str(mp),
                        "--extra", "PIP_EXTRA=nvidia-vfx==1.0", f"PackA={tmp_path / 'req.txt'}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = out.read_text().splitlines()
    assert "comfyui-frontend-package" in d and "protobuf>=3.20.2" in d and "pydantic>=2.0" in d
    assert "numpy!=2.0.1,>=1.25.0" in d or "numpy>=1.25.0,!=2.0.1" in d
    assert any(l.startswith("pywin32 ; platform_system") and "==" in l for l in d)          # the marker's == stays
    assert "requests[socks]" in d and "included" in d and "torchsde" in d and "nvidia-vfx" in d
    assert not any(l.startswith("torch") and not l.startswith("torchsde") for l in d)       # the torch step owns torch
    assert "--extra-index-url https://example.invalid/simple" in d                          # never a replacement for PyPI
    assert "git+https://github.com/o/r.git#egg=thing" in d and "withhash" in d
    assert "https://example.invalid/a-1.0.whl" in d
    rows = [l.split("\t") for l in mp.read_text().splitlines()]
    who = {r_[0]: r_[1] for r_ in rows if r_[0]}
    assert who["comfyui-frontend-package"] == "PackA" and who["nvidia-vfx"] == "PIP_EXTRA"
    assert any(r_[2] == "dropped-constraints" for r_ in rows) and any(r_[2] == "held-archive" for r_ in rows)
    assert "lifted\tPackA\tcomfyui-frontend-package" in r.stdout


# ---------------------------------------------------------------- D: torch from uv's --torch-backend=auto, never backwards

def _uv_torch(tmp_path, auto, no_family=""):
    """uv stand-in: `--torch-backend auto` answers `auto`; the torch family (with torchaudio) fails on `no_family`."""
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    _stub(d, "uv", f"""\
        if [ "$1 $2" = "pip install" ] && [ "$3" = "--help" ]; then echo "possible values: auto, cpu, cu132, cu130, cu129"; exit 0; fi
        b=""; while [ $# -gt 0 ]; do [ "$1" = "--torch-backend" ] && b="$2"; shift; done
        [ "$b" = auto ] && b="{auto}"
        req="$(cat)"
        case "$req" in *torchaudio*) [ "$b" = "{no_family}" ] && {{ echo "no torchaudio for $b" >&2; exit 1; }};; esac
        if [ "$b" = cpu ]; then echo "torch==2.14.0"; else echo "torch==2.14.0+$b"; fi
        """)
    return {"UV": str(d / "uv"), "PATH": f"{d}:{os.environ['PATH']}"}


@pytest.mark.parametrize("auto,keep,want,why", [
    ("cu130", "", "2.14.0 cu130", "uv --torch-backend=auto"),
    ("cu130", "cu132", "2.14.0 cu132", "never backwards"),
    ("cpu", "", "2.14.0 cu132", "newest CUDA backend uv knows"),
    ("cpu", "cu130", "2.14.0 cu130", "the venv's cu130 is kept"),
    ("cu132", "cu120", "2.14.0 cu132", "newer CUDA major"),
    ("cu120", "cu130", "2.14.0 cu130", "OLDER CUDA major"),
])
def test_unit_torch_pick_asks_uv_and_never_moves_a_shared_venv_backwards(tmp_path, auto, keep, want, why):
    env = dict(os.environ); env.update(_uv_torch(tmp_path, auto))
    r = subprocess.run([sys.executable, str(BASE / "py" / "torch_pick.py"), "3.14", keep], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == want and why in r.stderr, (r.stdout, r.stderr)



def _uv_new_python(tmp_path):
    """uv stand-in, 2026-10-15's shape: it lists CPython 3.15 before torch has a cp315 wheel. `pip compile` of anything
    with torch fails for 3.15 and resolves for 3.14; torch_pick's `pip install --help` names the CUDA backends."""
    d = tmp_path / "newpy"; d.mkdir(exist_ok=True)
    _stub(d, "uv", """\
        case "$1 $2" in
          "python list") printf 'cpython-3.15.0-linux-x86_64-gnu    <download available>\\ncpython-3.14.8-linux-x86_64-gnu    <download available>\\n'; exit 0 ;;
          "pip install") echo "possible values: auto, cpu, cu132, cu130"; exit 0 ;;
          "pip compile")
            v=""; o=""; prev=""; for a in "$@"; do [ "$prev" = --python-version ] && v="$a"; [ "$prev" = -o ] && o="$a"; prev="$a"; done
            cat >/dev/null
            [ "$v" = 3.15 ] && { echo "hint: You require CPython 3.15 (cp315), but we only found wheels for torch with cp314" >&2; exit 1; }
            [ -n "$o" ] && : > "$o"; echo "torch==2.14.1+cu130"; exit 0 ;;
          "--version"*) echo "uv 0.13.1"; exit 0 ;;
        esac
        exit 0
        """)
    return {"UV": str(d / "uv"), "PATH": f"{d}:/usr/bin:/bin"}


def test_unit_the_python_pick_is_the_newest_minor_torch_has_a_build_for(tmp_path):
    # with no requirement file to resolve against, the pick used to take the newest minor uv listed outright
    env = _uv_new_python(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; PICK=$(_base_python_pick); echo PICK=$PICK', env={"BASE_FAKE_ROOT": str(tmp_path), **env})
    assert "PICK=3.14" in r.stdout and "trying Python 3.15 ... no (the torch family does not resolve)" in r.stderr, r.stdout + r.stderr
    r = _bash('base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT BACKEND=$BASE_TORCH_BACKEND',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1", **env})
    assert "failed-pick" not in r.stdout + r.stderr and "target: Python 3.14" in r.stdout and "BACKEND=cu130" in r.stdout, r.stdout + r.stderr


def test_unit_no_cuda_or_index_literal_and_no_pin_remains_in_the_library():
    for p in [*(BASE / "lib").glob("*.sh"), *(BASE / "py").glob("*.py")]:
        code = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"whl/cu\d|\+cu\d{3}\b", code), p.name                        # uv picks the CUDA build
        assert not re.search(r"install[^\n]*\s[\"']?[A-Za-z0-9_.-]+==[0-9]", code), p.name  # no package pinned to a number


# ---------------------------------------------------------------- B: packs at their HEAD, never backwards

def _pack_origin(tmp_path, name):
    origin = tmp_path / f"{name}.git"; seed = tmp_path / f"{name}-seed"
    _git("init", "-q", "--bare", str(origin)); _git("clone", "-q", str(origin), str(seed))
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    for i in (1, 2):
        (seed / "__init__.py").write_text(f"v = {i}\n"); _git("add", ".", cwd=seed); _git(*cfg, "commit", "-q", "-m", f"c{i}", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, _git("rev-parse", "HEAD", cwd=seed), _git("rev-parse", "HEAD~1", cwd=seed)


def _packs_env(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "custom_nodes").mkdir(parents=True, exist_ok=True); (c / "main.py").write_text("")
    return {"BASE_FAKE_ROOT": str(tmp_path)}


def test_unit_packs_move_to_head_stash_edits_and_print_their_records(tmp_path):
    origin, head, old = _pack_origin(tmp_path, "PackA")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackA")); _git("checkout", "-q", "--detach", old, cwd=cn / "PackA")
    (cn / "PackA" / "__init__.py").write_text("local edit\n")
    row = f"PackA|{origin}|{'0' * 40}||test pack"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackA") == head, r.stdout + r.stderr
    assert "stashed as 'comfy-base" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert f'"PackA|{origin}|{head}||test pack"' in r.stdout                                  # the record podctl saves


def test_unit_a_pack_ahead_of_its_remote_is_never_moved_back(tmp_path):
    origin, head, _ = _pack_origin(tmp_path, "PackB")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackB"))
    (cn / "PackB" / "x.py").write_text("x"); _git("add", ".", cwd=cn / "PackB")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ahead", cwd=cn / "PackB")
    ahead = _git("rev-parse", "HEAD", cwd=cn / "PackB")
    row = f"PackB|{origin}|{'0' * 40}||t"
    _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackB") == ahead


def test_unit_a_declared_pack_that_is_not_a_checkout_goes_aside_and_is_cloned(tmp_path):
    origin, head, _ = _pack_origin(tmp_path, "PackC")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    (cn / "PackC").mkdir(); (cn / "PackC" / "__init__.py").write_text("registry copy")
    row = f"PackC|{origin}|{'0' * 40}||t"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git', env=env)
    assert (cn / "PackC" / ".git").exists() and _git("rev-parse", "HEAD", cwd=cn / "PackC") == head, r.stdout + r.stderr
    assert list((cn / "comfy-base-aside.disabled").glob("PackC-*"))                          # ComfyUI skips *.disabled


def test_unit_an_undeclared_git_pack_moves_to_its_head_too(tmp_path):
    origin, head, old = _pack_origin(tmp_path, "PackD")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackD")); _git("checkout", "-q", "--detach", old, cwd=cn / "PackD")
    _bash('base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=(); base_packs git', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackD") == head


def test_unit_list_packs_notes_differing_records_and_fails_on_differing_urls():
    a = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||" "X|https://h/x.git|' + "2" * 40 + '||")'
    r = _bash(f"{a}; base_list_packs; echo RC=$?")
    assert "RC=0" in r.stdout and "records differ" in r.stderr, r.stdout + r.stderr
    b = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||" "X|https://h/other|' + "1" * 40 + '||")'
    r2 = _bash(f"{b}; base_list_packs; echo RC=$?")
    assert "RC=1" in r2.stdout and "two URLs" in r2.stderr, r2.stdout + r2.stderr



# ---------------------------------------------------------------- 3.6.1: a row names the branch to track (a stale remote HEAD)

def _stale_head_origin(tmp_path, name):
    """rgthree's shape: the remote publishes as HEAD a `dev` branch left at an old commit, while the work goes on `main`.
    Returns (origin, main's tip, dev's commit)."""
    origin = tmp_path / f"{name}.git"; seed = tmp_path / f"{name}-seed"
    _git("init", "-q", "--bare", str(origin)); _git("clone", "-q", str(origin), str(seed))
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    def commit(i):
        (seed / "__init__.py").write_text(f"v = {i}\n"); _git("add", ".", cwd=seed); _git(*cfg, "commit", "-q", "-m", f"c{i}", cwd=seed)
    commit(1); dev = _git("rev-parse", "HEAD", cwd=seed)
    commit(2); commit(3); main = _git("rev-parse", "HEAD", cwd=seed)
    _git("push", "-q", "origin", f"{dev}:refs/heads/dev", "HEAD:refs/heads/main", cwd=seed)
    _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/dev")
    return origin, main, dev


def test_unit_a_row_without_a_branch_follows_the_remote_head_even_when_it_is_stale(tmp_path):
    origin, main, dev = _stale_head_origin(tmp_path, "PackS")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("PackS|{origin}|{"0" * 40}||t"); base_packs git', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackS") == dev, r.stdout + r.stderr       # the defect a sixth field exists for


def test_unit_a_row_branch_is_cloned_instead_of_the_stale_head_and_its_record_keeps_it(tmp_path):
    origin, main, dev = _stale_head_origin(tmp_path, "PackT")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    row = f"PackT|file://{origin}|{'0' * 40}||test pack|main"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env=env)
    assert "FAILED=0" in r.stdout and "the tip of main" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=cn / "PackT") == main and _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cn / "PackT") == "main"
    assert f'"PackT|file://{origin}|{main}||test pack|main"' in r.stdout                 # podctl saves the row whole


def test_unit_a_shallow_checkout_on_the_stale_head_moves_to_the_rows_branch_then_is_not_fetched_again(tmp_path):
    # what every pod cloned before 3.6.1 holds: `clone --depth 1` of the HEAD branch, so its refspec maps `dev` only
    origin, main, dev = _stale_head_origin(tmp_path, "PackU")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"; url = f"file://{origin}"
    _git("clone", "-q", "--depth", "1", url, str(cn / "PackU"))
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cn / "PackU") == "dev"
    row = f"PackU|{url}|{dev}||t|main"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env=env)
    assert "FAILED=0" in r.stdout and "was on 'dev'" in r.stdout + r.stderr, r.stdout + r.stderr
    d = cn / "PackU"
    assert _git("rev-parse", "HEAD", cwd=d) == main and _git("rev-parse", "--abbrev-ref", "HEAD", cwd=d) == "main"
    assert _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=d) == "origin/main"
    spy, log = _git_spy(tmp_path)
    r2 = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env={**env, **spy})
    calls = log.read_text().split()
    assert "FAILED=0" in r2.stdout and "ls-remote" in calls and "fetch" not in calls, (r2.stdout + r2.stderr, calls)


def test_unit_a_row_branch_the_remote_does_not_have_fails_the_pack_and_says_so(tmp_path):
    origin, main, dev = _stale_head_origin(tmp_path, "PackV")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackV"))
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("PackV|{origin}|{dev}||t|trunk"); base_packs git; printf "%s\\n" "${{BASE_FAILED[@]}}"', env=env)
    assert "no branch 'trunk'" in r.stdout + r.stderr and "pack PackV" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=cn / "PackV") == dev                                 # left where it was


def test_unit_pack_heads_answers_a_tracked_branch_under_its_own_key(tmp_path):
    origin, main, dev = _stale_head_origin(tmp_path, "PackW")
    r = subprocess.run([sys.executable, str(BASE / "py" / "pack_heads.py")], input=f"{origin}\n{origin}#main\n{origin}#nope\n",
                       capture_output=True, text=True)
    rows = sorted(l.split("\t") for l in r.stdout.splitlines())
    assert rows == sorted([[str(origin), "dev", dev], [f"{origin}#main", "main", main]]), r.stdout + r.stderr


@pytest.mark.parametrize("tail,ok", [("", True), ("|main", True), ("|release/1.x", True),
                                     ("|5f940ddfbb83ff9c667f32ae7d0c3ad0bb8cc9ee", False), ("|5f940dd", False),
                                     ("|two words", False), ("|main|extra", False), ("|../x", False)])
def test_unit_the_sixth_field_is_a_branch_name_or_the_row_is_refused(tail, ok):
    row = "X|https://h/x|" + "1" * 40 + "||why" + tail
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); _base_pack_rows; echo RC=$?')
    assert ("RC=0" in r.stdout) == ok, r.stdout + r.stderr
    if not ok:
        assert "not a branch name" in r.stderr, r.stderr


def test_unit_the_rgthree_base_row_tracks_main():
    from basetest import base_packs
    rows = {p["dir"]: p for p in base_packs()}
    assert rows["rgthree-comfy"]["branch"] == "main" and rows["rgthree-comfy"]["why"].startswith("switches"), rows["rgthree-comfy"]
    assert all(p["branch"] == "" for d, p in rows.items() if d != "rgthree-comfy")     # only where the remote's HEAD is wrong


def test_unit_list_packs_hands_on_the_branch_and_fails_on_two_branches():
    a = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||w|main" "Y|https://h/y|' + "2" * 40 + '||w")'
    r = _bash(f"{a}; base_list_packs; echo RC=$?")
    assert "RC=0" in r.stdout and "X|https://h/x|" + "1" * 40 + "|main" in r.stdout and "Y|https://h/y|" + "2" * 40 + "|\n" in r.stdout, r.stdout + r.stderr
    b = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||w|main" "X|https://h/x|' + "1" * 40 + '||w")'
    r2 = _bash(f"{b}; base_list_packs; echo RC=$?")
    assert "RC=1" in r2.stdout and "tracks two branches: main vs its HEAD" in r2.stderr, r2.stdout + r2.stderr


def test_unit_podctl_saves_a_six_field_record_without_losing_the_branch(tmp_path):
    import importlib.util
    if not (BASE / "_build" / "pod" / "podctl.py").exists():
        pytest.skip("_build/pod is repo tooling, not in the shipped base")
    spec = importlib.util.spec_from_file_location("podctl_b", BASE / "_build" / "pod" / "podctl.py"); podctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(podctl)
    s = tmp_path / "p-script.sh"
    s.write_text('PACKS=(\n "rg|https://github.com/rgthree/rgthree-comfy|' + "1" * 40 + '||switches|main"\n)\n')
    rows = podctl.pin_rows('  "rg|https://github.com/rgthree/rgthree-comfy|' + "2" * 40 + '||switches|main"\n')
    assert rows == [("rg", "https://github.com/rgthree/rgthree-comfy", "2" * 40, "|switches|main")], rows
    assert podctl.save_pins(s, rows) == [("rg", "1" * 40, "2" * 40)]
    assert '"rg|https://github.com/rgthree/rgthree-comfy|' + "2" * 40 + '||switches|main"' in s.read_text()


def _testbed_beside(tmp_path, rows):
    """testbed.sh in a throwaway folder, beside a stand-in base whose list-packs prints `rows` (dir|url|sha|branch) and
    whose py/ is the real one (testbed.sh asks py/pkgdirs.py where the packages are)."""
    here = tmp_path / "tb"; cb = here / "comfyui-base"; cb.mkdir(parents=True)
    (here / "testbed.sh").write_text((BASE / "testbed.sh").read_text(encoding="utf-8"))
    (cb / "py").symlink_to(BASE / "py")
    lines = "".join("echo '%s'\n" % r for r in rows)
    (cb / "base.sh").write_text('#!/bin/bash\n[ "${1:-}" = list-packs ] || exit 2\n' + lines)
    return here


def test_unit_testbed_packs_clone_the_rows_branch_and_move_a_stale_checkout_across(tmp_path):
    if not (BASE / "testbed.sh").exists() or not (BASE / "_build").exists():
        pytest.skip("testbed.sh is repo tooling, not in the shipped base")
    a, a_main, a_dev = _stale_head_origin(tmp_path, "PackX"); b, b_main, b_dev = _stale_head_origin(tmp_path, "PackY")
    c, c_main, c_dev = _stale_head_origin(tmp_path, "PackZ")
    here = _testbed_beside(tmp_path, [f"PackX|file://{a}|{'0' * 40}|main", f"PackY|file://{b}|{'0' * 40}|main", f"PackZ|file://{c}|{'0' * 40}|"])
    cn = here / "testbed" / "custom_nodes"; cn.mkdir(parents=True)
    _git("clone", "-q", "--depth", "1", f"file://{b}", str(cn / "PackY"))                 # a testbed cloned before 3.6.1
    e = {k: v for k, v in os.environ.items() if not k.startswith("BASE_")}
    r = subprocess.run(["bash", str(here / "testbed.sh"), "--packs"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=cn / "PackX") == a_main, r.stdout                 # cloned at the row's branch
    assert _git("rev-parse", "HEAD", cwd=cn / "PackY") == b_main, r.stdout                 # moved off the stale HEAD
    assert _git("rev-parse", "HEAD", cwd=cn / "PackZ") == c_dev, r.stdout                  # no branch named: the remote's HEAD
    assert "(main)" in r.stdout and "(HEAD)" in r.stdout, r.stdout


# ---------------------------------------------------------------- C: the lift, the override loop, the import check

def _lift_env(tmp_path, outdated_rounds, override_fails=()):
    """A fake venv whose python is this interpreter, and a uv stand-in that records every call. `outdated_rounds` is the
    JSON `uv pip list --outdated` answers, one per call; an install whose overrides name a package in `override_fails`
    exits 1."""
    c = tmp_path / "ComfyUI"; c.mkdir(parents=True, exist_ok=True); (c / "main.py").write_text("")
    (c / "requirements.txt").write_text("comfyui-frontend-package==1.52.7\nprotobuf\n")
    v = c / ".venv-cu130" / "bin"; v.mkdir(parents=True); (v / "python").symlink_to(sys.executable)
    (c / ".venv-cu130" / ".comfy-base-venv").write_text("python=3 torch=0 backend=cu130")
    d = tmp_path / "bin"; d.mkdir(); log = tmp_path / "uv.log"; n = tmp_path / "n"
    (tmp_path / "rounds.json").write_text(json.dumps(outdated_rounds))
    fails = " ".join(override_fails)
    _stub(d, "uv", f"""\
        echo "$*" >> "{log}"
        if [ "$1 $2" = "pip list" ]; then
          i=$(cat "{n}" 2>/dev/null || echo 0); echo $((i+1)) > "{n}"
          "{sys.executable}" -c 'import json,sys; r=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(json.dumps(r[min(i,len(r)-1)]))' "{tmp_path / 'rounds.json'}" "$i"
          exit 0
        fi
        if [ "$1 $2" = "pip freeze" ]; then echo "protobuf==5.29.6"; exit 0; fi
        if [ "$1 $2" = "pip check" ]; then exit 0; fi
        if [ "$1 $2" = "pip install" ]; then
          ov=""; prev=""; for a in "$@"; do [ "$prev" = "--overrides" ] && ov="$a"; prev="$a"; done
          for f in {fails}; do [ -n "$ov" ] && grep -q "^$f==" "$ov" && exit 1; done
          exit 0
        fi
        if [ "$1" = run ]; then shift; while [ "$1" != python ]; do shift; done; shift; exec "{sys.executable}" "$@"; fi
        exit 0
        """)
    # HOME too: base_env_setup puts ~/.local/bin first, where the Mac's real uv lives, and a real uv here would install
    return {"BASE_FAKE_ROOT": str(tmp_path), "PATH": f"{d}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "BASE_FAKE_HOLDERS": "/dev/null"}, log


def test_unit_the_lift_forces_a_capped_package_to_its_newest(tmp_path):
    env, log = _lift_env(tmp_path, [[{"name": "protobuf", "version": "5.29.6", "latest_version": "7.36.2"}], []])
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "ROW %s\\n" "${BASE_LIFT_ROWS[@]}"; cat "$BASE_LIFT_DIR/overrides.txt"', env=env)
    assert "protobuf==7.36.2" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "ROW protobuf → 7.36.2 (forced" in r.stdout and "ROW comfyui-frontend-package" in r.stdout, r.stdout
    calls = [l for l in log.read_text().splitlines() if l.startswith("pip install")]
    assert calls and all(" -r " in l for l in calls) and any("--overrides" in l for l in calls), calls


def test_unit_a_package_the_base_could_move_and_did_not_fails_the_run(tmp_path):
    stuck = [{"name": "protobuf", "version": "5.29.6", "latest_version": "7.36.2"}]
    env, _ = _lift_env(tmp_path, [stuck, stuck, stuck, stuck, stuck])
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; printf "F %s\\n" "${BASE_FAILED[@]}"', env=env)
    assert "still behind after the override loop: protobuf 5.29.6 < 7.36.2" in r.stdout, r.stdout + r.stderr


def test_unit_a_newest_that_cannot_install_here_is_named_upstream_not_failed(tmp_path):
    env, _ = _lift_env(tmp_path, [[{"name": "oddpkg", "version": "1.0", "latest_version": "2.0"}], [{"name": "oddpkg", "version": "1.0", "latest_version": "2.0"}]], override_fails=("oddpkg",))
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"', env=env)
    assert "FAILED=0" in r.stdout and "U oddpkg 2.0: cannot be installed here" in r.stdout, r.stdout + r.stderr


def test_unit_a_pack_that_cannot_import_is_named_and_the_restart_goes_on(tmp_path):
    env, _ = _lift_env(tmp_path, [[]])
    (tmp_path / "ComfyUI" / "main.py").write_text('print("   0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackZ")\n')
    r = _bash('base_env_setup; base_discover quiet; BASE_VENV_RESULT=reused; base_import_check; echo FAILED=${#BASE_FAILED[@]} BLOCK=${BASE_BLOCK_RESTART:-0}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"', env=env)
    assert "FAILED=0 BLOCK=0" in r.stdout and "U pack PackZ does not import" in r.stdout, r.stdout + r.stderr


def test_unit_comfyui_core_failing_rolls_back_only_what_this_run_changed(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    flag = tmp_path / "rolled"
    (tmp_path / "ComfyUI" / "main.py").write_text(f'import os, sys\nif not os.path.exists({str(flag)!r}):\n    print("Traceback: core"); sys.exit(1)\n')
    snap = tmp_path / "snap.txt"; snap.write_text("protobuf==5.29.6\ncomfy-aimdo==0.5.5\n")
    r = _bash(f'base_env_setup; base_discover quiet; _base_lift_dir; BASE_VENV_SNAPSHOT="{snap}"; BASE_VENV_RESULT=reused; '
              f'_base_uv(){{ if [ "$1 $2" = "pip freeze" ]; then echo "protobuf==5.29.6"; echo "comfy-aimdo==0.6.0"; else touch "{flag}"; fi; }}; '
              'base_import_check; printf "F %s\\n" "${BASE_FAILED[@]}"; cat "$BASE_LIFT_DIR/rollback.txt"', env=env)
    rb = next(pathlib.Path(tmp_path).rglob("rollback.txt")).read_text().split()
    assert rb == ["comfy-aimdo==0.5.5"], rb                                                # only what this run changed
    assert "did not start with the newest of: comfy-aimdo==0.5.5" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- G: the OS and the NVIDIA driver, against stand-ins

def _apt_env(tmp_path, installed, running="580.178.04", module="615.71.09", kernels=("6.8.0-139-generic", "6.8.0-142-generic"), host="verda"):
    d = tmp_path / "fakeapt"; d.mkdir(); log = tmp_path / "apt.log"
    for k in kernels:
        (d / "root" / "lib" / "modules" / k).mkdir(parents=True)
    _stub(d, "apt-get", f'echo "apt-get $*" >> "{log}"; [ "$*" != "${{*#*full-upgrade}}" ] && echo "3 upgraded, 1 newly installed, 0 to remove and 0 not upgraded."; exit 0\n')
    _stub(d, "apt-cache", 'echo "  Candidate: 615.71.09-1"\n')
    _stub(d, "apt-mark", f'echo "apt-mark $*" >> "{log}"\n')
    (tmp_path / "installed.txt").write_text("".join(p_ + "\n" for p_ in installed))
    _stub(d, "dpkg-query", r"""
        inst="INSTFILE"; fmt="$3"; shift 3
        case "$fmt" in
          '${db:Status-Abbrev} ${Package}\n') sed 's/^/ii  /' "$inst" ;;
          '${db:Status-Abbrev}') if [ "$1" = nvidia-open ]; then grep -qx nvidia-open "$inst" && printf 'ii ' || printf 'un '; else printf 'ii '; fi ;;
          '${Package}\n') cat "$inst" ;;
          '${Version}') echo 615.71.09 ;;
        esac
        exit 0
        """.replace("INSTFILE", str(tmp_path / "installed.txt")))
    _stub(d, "nvidia-smi", f'case "$*" in *driver_version*) echo "{running}";; *) echo "CUDA Version: 13.0";; esac\n')
    _stub(d, "modinfo", f'[ "$2" = "{kernels[-1]}" ] && [ -n "{module}" ] && echo "{module}"; exit 0\n')
    _stub(d, "dkms", 'echo "nvidia/580.178.04, 6.8.0-139-generic, x86_64: installed"\n')
    _stub(d, "systemctl", f'echo "systemctl $*" >> "{log}"; exit 0\n')
    _stub(d, "uname", f'[ "$1" = -r ] && echo "{kernels[0]}" || /usr/bin/uname "$@"\n')
    _stub(d, "curl", "exit 0\n")
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    return {"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_APT": str(d), "BASE_HOST": host, "HOME": str(tmp_path / "home")}, log


def test_unit_the_driver_switches_family_in_one_transaction_first_then_stops_for_a_restart(tmp_path):
    env, log = _apt_env(tmp_path, ["nvidia-driver-580-server-open", "nvidia-dkms-580-server-open",
                                   "linux-modules-nvidia-580-server-open-6.8.0-139-generic", "libnvidia-compute-580-server",
                                   "nvidia-container-toolkit"])
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? REBOOT=$BASE_REBOOT_REQUIRED; printf 'F %s\\n' \"${BASE_FAILED[@]}\"", env=env)
    lines = log.read_text().splitlines()
    sw = next(i for i, l in enumerate(lines) if "install --purge nvidia-open cuda-toolkit" in l)
    fu = next(i for i, l in enumerate(lines) if "full-upgrade" in l)
    assert sw < fu, lines                                                                       # the switch comes FIRST
    for p in ("nvidia-driver-580-server-open-", "nvidia-dkms-580-server-open-", "linux-modules-nvidia-580-server-open-6.8.0-139-generic-", "libnvidia-compute-580-server-"):
        assert p in lines[sw], (p, lines[sw])
    assert "nvidia-container-toolkit-" not in lines[sw]
    assert "RC=10" in r.stdout and "restart required: driver 580.178.04 -> 615.71.09" in r.stdout, r.stdout + r.stderr
    stop = next(i for i, l in enumerate(lines) if "systemctl stop comfy-base-boot" in l)
    assert stop < sw, lines                                                                     # the unit stops before the purge


def test_unit_no_module_for_the_newest_kernel_fails_and_advises_no_restart(tmp_path):
    env, _ = _apt_env(tmp_path, ["nvidia-open"], running="615.71.09", module="")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$?; printf 'F %s\\n' \"${BASE_FAILED[@]}\"", env=env)
    assert "RC=10" in r.stdout and "not built for kernel 6.8.0-142-generic" in r.stdout, r.stdout + r.stderr


def test_unit_a_kernel_only_update_is_a_warning(tmp_path):
    env, _ = _apt_env(tmp_path, ["nvidia-open"], running="615.71.09", module="615.71.09")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? F=${#BASE_FAILED[@]}; printf 'W %s\\n' \"${BASE_WARN[@]}\"", env=env)
    assert "RC=0 F=0" in r.stdout and "restart when convenient" in r.stdout, r.stdout + r.stderr


def test_unit_runpod_holds_the_hosts_driver_and_installs_none(tmp_path):
    env, log = _apt_env(tmp_path, ["libnvidia-compute-580"], host="runpod")
    _bash("base_env_setup; base_discover quiet; base_system", env=env)
    text = log.read_text()
    assert "apt-mark hold" in text and "nvidia-open" not in text, text


def test_unit_no_root_warns_and_the_run_goes_on(tmp_path):
    env, log = _apt_env(tmp_path, [])
    env["BASE_FAKE_APT_NOROOT"] = "1"
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? F=${#BASE_FAILED[@]}; printf 'W %s\\n' \"${BASE_WARN[@]}\"", env=env)
    assert "RC=0 F=0" in r.stdout and "no root and no passwordless sudo" in r.stdout and not log.exists(), r.stdout


def test_unit_a_fake_run_without_the_stand_ins_leaves_the_os_alone(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo R=$BASE_SYSTEM_RESULT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "R=skipped (fake)" in r.stdout, r.stdout + r.stderr


def test_unit_sudo_carries_the_non_interactive_settings(tmp_path):
    d = tmp_path / "bin"; d.mkdir()
    _stub(d, "sudo", 'echo "SUDO $*"\n'); _stub(d, "id", 'echo 1000\n')
    r = _bash("_base_apt update", env={"PATH": f"{d}:{os.environ['PATH']}"})
    assert "SUDO -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l" in r.stdout and "--force-confold" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- H: what the install says it put on the store

def test_unit_the_versions_block_names_everything_and_the_store_only_when_shared(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    snip = ('base_env_setup; base_discover quiet; COMFY_NEW=0.37.2; COMFY_REF_INFO="v0.37.2 @ 830232b"; BASE_TORCH_BACKEND=cu132; '
            'BASE_FRONTEND_INFO="frontend 1.54.7"; BASE_LIFT_RESULT=ok; BASE_UPSTREAM=("pack X needs upgrading upstream"); _base_versions_block')
    r = _bash(snip, env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "VERSIONS   base" in r.stdout and "v0.37.2 @ 830232b" in r.stdout and "frontend 1.54.7" in r.stdout and "(cu132)" in r.stdout, r.stdout
    assert "UPSTREAM   1" in r.stdout and "STORE" not in r.stdout
    r2 = _bash(snip, env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_VOLUME_SHARED": "1"})
    assert "EVERY machine on this store" in r2.stdout, r2.stdout


def test_unit_a_machine_on_an_older_cuda_major_is_named(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('base_env_setup; base_discover quiet; mkdir -p "$BASE_STATE/machines"; printf "host=other\\ncuda=12.8\\n" > "$BASE_STATE/machines/other.env"; '
              'BASE_TORCH_BACKEND=cu132; _base_machines_behind', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "other runs a driver for CUDA 12.8" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- the rehearsal's fake machine carries what the package ships

def test_unit_the_fake_machine_takes_the_packages_comfy_min_and_its_vendored_packs(tmp_path):
    """Measured by the Qwen 2.1 session against 2.12.2: the fake machine was always ComfyUI 0.34.7, so a package needing
    0.37.0 stopped at the version gate; and only the package's top-level files were copied, so its vendored node packs
    (which its zip ships) were missing and every suite test reading a pack file failed inside the rehearsal."""
    sys.path.insert(0, str(BASE / "py"))
    from basetest import fake_pod
    pkg = tmp_path / "src" / "my-pkg"; (pkg / "ComfyUI-My-Pack").mkdir(parents=True); (pkg / "_build").mkdir()
    (pkg / "my-pkg-script.sh").write_text('PKG_ID=x; COMFY_MIN="0.37.0"\n')
    (pkg / "ComfyUI-My-Pack" / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (pkg / "_build" / "tool.py").write_text("")
    c = fake_pod(tmp_path / "pod", pkg, layout="official")
    assert '__version__ = "0.37.0"' in (c / "comfyui_version.py").read_text()
    assert (tmp_path / "pod" / "pkg" / "ComfyUI-My-Pack" / "__init__.py").exists()
    assert not (tmp_path / "pod" / "pkg" / "_build").exists()                                  # repo tooling stays out


def test_unit_the_cuda_backend_carries_the_whole_torch_family_comfyui_needs(tmp_path):
    """Measured 2026-09-25: torchaudio (which ComfyUI requires) has no cu132 build, so a driver that allows cu132 must
    still get the newest backend on which torch, torchvision and torchaudio resolve together, and say what holds it."""
    env = dict(os.environ); env.update(_uv_torch(tmp_path, "cu132", no_family="cu132"))
    r = subprocess.run([sys.executable, str(BASE / "py" / "torch_pick.py"), "3.14", ""], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "2.14.0 cu130" and "held by the torch family" in r.stderr, (r.stdout, r.stderr)


def test_unit_a_force_its_holder_refuses_at_import_is_taken_back_and_named(tmp_path):
    """Measured on a live machine 2026-09-25: transformers 5.17.0 (its newest) raises ImportError on huggingface-hub
    2.0.0. Forcing hub past transformers' cap would have broken ComfyUI's startup on every install; the force is taken
    back out, hub stays at the newest transformers allows, and the pair is named upstream. The run stays green."""
    env, log = _lift_env(tmp_path, [[{"name": "huggingface-hub", "version": "1.33.0", "latest_version": "2.0.0"}],
                                     [{"name": "huggingface-hub", "version": "1.33.0", "latest_version": "2.0.0"}]])
    r = _bash('base_env_setup; base_discover quiet; '
              '_base_probe_forced(){ printf "huggingface-hub\\ttransformers\\t5.17.0\\tImportError: huggingface-hub<2.0 is required\\n"; }; '
              'base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"; echo OVR=$(cat "$BASE_LIFT_DIR/overrides.txt")', env=env)
    assert "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "U huggingface-hub 2.0.0: cannot be forced: transformers 5.17.0 refuses it at import" in r.stdout, r.stdout
    assert "OVR=\n" in r.stdout or r.stdout.rstrip().endswith("OVR="), r.stdout                   # nothing forced in the end


def test_unit_a_sageattention_build_for_a_list_of_archs_serves_each_gpu_in_it(tmp_path):
    """3.0.1, measured by the SoulCraft image session on 3.0.0: an image built with no GPU stamps SageAttention for
    SAGE_ARCHS="9.0;12.0", and the machine it serves computed its key from sm_120, so the two never matched and every first
    install rebuilt it (10-20 min). The same commit, Python and torch built for a list holding this GPU is current; a
    GPU the list does not hold still builds."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True); py = v / "bin" / "python"
    py.write_text('#!/bin/bash\ncase "$*" in *"import sageattention"*) exit 0;; *) exit 0;; esac\n'); py.chmod(0o755)
    gitbin = tmp_path / "gitbin"; gitbin.mkdir()
    (gitbin / "git").write_text('#!/bin/bash\n[ "$1" = ls-remote ] && { echo "abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main"; exit 0; }\nexec /usr/bin/git "$@"\n'); (gitbin / "git").chmod(0o755)
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp-torch0-archs-9-0_12-0\n")
    env = {"PATH": f"{gitbin}:{os.environ['PATH']}", "HOME": str(tmp_path / "home")}
    base = f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; BASE_DRY=0; BASE_NO_NET=0; '
    r = _bash(base + 'BASE_GPU_SM=sm_120; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env=env)
    assert "already built from SageAttention abcdef123456" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_GPU_SM=sm_90; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env=env)
    assert "already built" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_GPU_SM=sm_89; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env=env)
    assert "already built" not in r.stdout, r.stdout + r.stderr                              # 8.9 is not in the list: it builds


# ---------------------------------------------------------------- 3.3.0: every resolve once, and a force is never undone

def _installs(log):
    return [l for l in pathlib.Path(log).read_text().splitlines() if l.startswith("pip install")]


def test_unit_a_forced_package_is_never_pushed_back_down_by_the_next_run(tmp_path):
    # 3.2 and before: base_packs pip installed WITHOUT the overrides, so every package the newest pass had forced past a
    # cap went back down under it, and the newest pass forced it up again: two whole resolves and a reinstall, every run.
    env, log = _lift_env(tmp_path, [[{"name": "protobuf", "version": "5.29.6", "latest_version": "7.36.2"}], []])
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest >/dev/null; cat "$BASE_LIFT_DIR/forced.txt"', env=env)
    assert "protobuf>=7.36.2" in r.stdout, r.stdout + r.stderr                  # kept as a FLOOR: never a pin, always the newest
    log.write_text("")
    r = _bash('base_env_setup; base_discover quiet; _base_lift_dir; _base_derived_install; cat "$BASE_LIFT_DIR/overrides.effective.txt"', env=env)
    first = _installs(log)
    assert len(first) == 1 and "--overrides" in first[0] and "protobuf>=7.36.2" in r.stdout, (first, r.stdout, r.stderr)


def test_unit_an_explicit_round_keeps_the_earlier_floors_too(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    r = _bash('base_env_setup; base_discover quiet; _base_lift_dir; printf "protobuf>=7.36.2\\nmcp>=2.2.0\\n" > "$BASE_LIFT_DIR/forced.txt";'
              ' printf "mcp==2.3.0\\n" > "$BASE_LIFT_DIR/overrides.txt"; _base_derived_install --overrides "$BASE_LIFT_DIR/overrides.txt";'
              ' cat "$BASE_LIFT_DIR/overrides.effective.txt"', env=env)
    lines = sorted(r.stdout.split())
    assert lines == ["mcp==2.3.0", "protobuf>=7.36.2"], lines                  # this round's force wins its own name


def test_unit_the_same_requirement_set_is_not_resolved_twice_in_one_run(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    r = _bash('base_env_setup; base_discover quiet; _base_reqlift >/dev/null; base_torch_constraints; _base_derived_install_once >/dev/null;'
              ' base_venv_latest; echo DONE', env=env)
    assert "DONE" in r.stdout and "not resolved twice" in r.stdout, r.stdout + r.stderr
    assert len(_installs(log)) == 1, _installs(log)


def test_unit_the_outdated_scan_runs_once_when_nothing_is_behind(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    _bash('base_env_setup; base_discover quiet; base_venv_latest >/dev/null', env=env)
    scans = [l for l in log.read_text().splitlines() if l.startswith("pip list")]
    assert len(scans) == 1, scans


def test_unit_a_refused_force_is_not_retried_until_either_version_moves(tmp_path):
    hub = {"name": "huggingface-hub", "version": "1.9.0", "latest_version": "2.0.0"}
    env, log = _lift_env(tmp_path, [[hub]])
    lift = tmp_path / "comfy-base" / "state" / "lift"; lift.mkdir(parents=True, exist_ok=True)
    (lift / "refused.tsv").write_text("huggingface-hub\t2.0.0\ttransformers\t5.17.0\n")
    snippet = ('base_env_setup; base_discover quiet; _base_dist_version(){ [ "$1" = transformers ] && echo "%s"; }; base_venv_latest;'
               ' echo FAILED=${#BASE_FAILED[@]}; printf "U %%s\\n" "${BASE_UPSTREAM[@]}"')
    r = _bash(snippet % "5.17.0", env=env)
    assert "FAILED=0" in r.stdout and "U huggingface-hub 2.0.0: cannot be forced" in r.stdout, r.stdout + r.stderr
    assert not [l for l in _installs(log) if "--overrides" in l], _installs(log)   # no round tried it
    (tmp_path / "n").unlink(); log.write_text("")
    _bash(snippet % "5.18.0", env=env)                                          # transformers moved: worth one more try
    assert [l for l in _installs(log) if "--overrides" in l], _installs(log)


def test_unit_python_packages_that_moved_are_a_reason_to_restart(tmp_path):
    env, _ = _lift_env(tmp_path, [[]])
    snap = tmp_path / "snap.txt"; snap.write_text("protobuf==5.29.6\nmcp==2.2.0\n")
    r = _bash(f'base_env_setup; base_discover quiet; BASE_VENV_SNAPSHOT="{snap}";'
              ' _base_uv(){ [ "$1 $2" = "pip freeze" ] && printf "protobuf==7.36.2\\nmcp==2.2.0\\n"; }; _base_python_moved; echo "MOVED=$BASE_PY_MOVED"', env=env)
    assert "MOVED=1" in r.stdout, r.stdout + r.stderr
    r = _bash(f'base_env_setup; base_discover quiet; BASE_VENV_SNAPSHOT="{snap}"; _base_runtime_on(){{ return 0; }};'
              ' _base_uv(){ [ "$1 $2" = "pip freeze" ] && printf "protobuf==7.36.2\\n"; }; _base_python_moved; echo "MOVED=${BASE_PY_MOVED:-0}"', env=env)
    assert "MOVED=0" in r.stdout, r.stdout                                      # a runtime's Python never moves within a run
    r = _bash('base_env_setup; base_discover quiet; BASE_PY_MOVED=3; BASE_DRY=0; BASE_NO_NET=0; curl(){ return 0; }; _base_comfy_pids(){ :; };'
              ' base_restart', env=env)
    assert "3 Python package(s) moved" in r.stdout, r.stdout + r.stderr


def test_unit_a_missing_module_is_installed_only_from_the_curated_map(tmp_path):
    log = tmp_path / "import.log"
    log.write_text("Traceback...\nModuleNotFoundError: No module named 'cv2'\n0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackA\n"
                   "ModuleNotFoundError: No module named 'skimage.color'\n0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackB\n"
                   "ModuleNotFoundError: No module named 'totally_unknown_mod'\n0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackC\n")
    r = _bash(f'_base_missing_dists "{log}"')
    assert sorted(r.stdout.split()) == ["opencv-python-headless", "scikit-image"], r.stdout + r.stderr   # never a guessed name
    assert "totally_unknown_mod" in r.stderr


def test_unit_the_import_check_installs_a_mapped_missing_module_and_judges_again(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    flag = tmp_path / "installed"
    (tmp_path / "ComfyUI" / "main.py").write_text(
        f'import os\nif not os.path.exists({str(flag)!r}):\n    print("ModuleNotFoundError: No module named \'cv2\'")\n'
        f'    print("   0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackZ")\n')
    r = _bash(f'base_env_setup; base_discover quiet; BASE_VENV_RESULT=reused; _base_uvpip(){{ echo "UVPIP $*"; touch "{flag}"; }};'
              ' base_import_check; echo "RESULT=$BASE_IMPORT_RESULT"; echo "MOVED=${BASE_PY_MOVED:-0}"; printf "U %s\\n" "${BASE_UPSTREAM[@]}"', env=env)
    assert "UVPIP" in r.stdout and "opencv-python-headless" in r.stdout, r.stdout + r.stderr
    assert "RESULT=ok" in r.stdout and "U pack PackZ" not in r.stdout and "MOVED=1" in r.stdout, r.stdout


def test_unit_a_reused_venv_never_moves_torch_backwards(tmp_path):
    site = tmp_path / "site"; (site / "torch-2.15.0.dist-info").mkdir(parents=True)
    (site / "torch-2.15.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: torch\nVersion: 2.15.0\n")
    calls = tmp_path / "uvpip.log"
    r = _bash(f'PY="{sys.executable}"; _base_uvpip(){{ echo "UVPIP $*" >> "{calls}"; }}; base_torch_constraints(){{ :; }}; _base_torch_family_upgrade 2.14.0 cu130',
              env={"PYTHONPATH": str(site)})
    got = calls.read_text()
    assert "torch==2.15.0" in got and "torch==2.14.0" not in got, got + r.stdout + r.stderr


def test_unit_comfy_mcp_is_not_installed_a_second_time(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    site = tmp_path / "site"
    for name, ver in (("comfy-mcp", "0.10.0"), ("comfy-cli", "1.21.0")):
        d = site / ("%s-%s.dist-info" % (name.replace("-", "_"), ver)); d.mkdir(parents=True)
        (d / "METADATA").write_text("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, ver))
    v = tmp_path / "ComfyUI" / ".venv-cu130" / "bin"
    for b in ("comfy-mcp", "comfy"): _stub(v, b, "exit 0\n")
    r = _bash('base_env_setup; base_discover quiet; _base_uvpip(){ echo "UVPIP $*"; }; base_mcp; echo "R=$BASE_MCP_RESULT"', env={**env, "PYTHONPATH": str(site)})
    assert "UVPIP" not in r.stdout and "R=ok" in r.stdout, r.stdout + r.stderr


def test_unit_the_sageattention_key_names_the_cuda_backend_and_an_existing_build_is_adopted(tmp_path):
    # torch 2.14.0 exists for cu130 AND cu132 with the same public version: a key without the backend could serve a
    # cu130 build to a cu132 venv. The key now carries it; a build stamped under the old key for THIS venv (it imports
    # with this torch) is adopted, not rebuilt (10 to 20 minutes on every machine otherwise).
    site = tmp_path / "site"; (site / "torch").mkdir(parents=True); (site / "sageattention").mkdir()
    (site / "torch" / "__init__.py").write_text('__version__ = "2.14.0+cu130"\n'); (site / "sageattention" / "__init__.py").write_text("")
    venv = tmp_path / "venv"; venv.mkdir()
    tag = "cp%d%d" % sys.version_info[:2]
    old = "sageattention-abcdef123456-%s-torch2.14.0-sm_120" % tag
    (venv / ".comfy-base-sageattention").write_text(old + "\n")
    r = _bash(f'PY="{sys.executable}"; VENV="{venv}"; BASE_STATE="{tmp_path}/state"; BASE_GPU_SM=sm_120; BASE_DRY=0; BASE_NO_NET=0;'
              ' git(){ echo "abcdef1234567890abcdef1234567890abcdef12 refs/heads/main"; }; base_build_sageattention',
              env={"PYTHONPATH": str(site)})
    stamp = (venv / ".comfy-base-sageattention").read_text().strip()
    assert stamp == "sageattention-abcdef123456-%s-torch2.14.0-cu130-sm_120" % tag, (stamp, r.stdout, r.stderr)
    assert "already built" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- 3.5.0: every probe once, in parallel

def _git_spy(tmp_path):
    """git on PATH that logs each call's subcommand, then runs the real git."""
    real = shutil.which("git"); d = tmp_path / "gitspy"; d.mkdir(exist_ok=True); log = tmp_path / "git.log"
    _stub(d, "git", f'skip=0; for a in "$@"; do if [ "$skip" = 1 ]; then skip=0; continue; fi; case "$a" in -c|-C) skip=1;; -*) ;; *) echo "$a" >> "{log}"; break;; esac; done\nexec "{real}" "$@"\n')
    return {"PATH": f"{d}:{os.environ['PATH']}"}, log


def test_unit_a_pack_already_at_its_remote_head_is_not_fetched(tmp_path):
    origin, head, _ = _pack_origin(tmp_path, "PackA")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackA"))
    spy, log = _git_spy(tmp_path)
    row = f"PackA|{origin}|{head}||test pack"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env={**env, **spy})
    assert "FAILED=0" in r.stdout and "PackA @" in r.stdout, r.stdout + r.stderr
    calls = log.read_text().split()
    assert "ls-remote" in calls and "fetch" not in calls, calls                    # one probe, no fetch
    assert f'"PackA|{origin}|{head}||test pack"' in r.stdout                        # the record is printed all the same


def test_unit_a_pack_whose_remote_moved_is_fetched_and_moved(tmp_path):
    origin, head, old = _pack_origin(tmp_path, "PackA")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackA")); _git("reset", "-q", "--hard", old, cwd=cn / "PackA")
    spy, log = _git_spy(tmp_path)
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("PackA|{origin}|{old}||t"); base_packs git', env={**env, **spy})
    assert _git("rev-parse", "HEAD", cwd=cn / "PackA") == head, r.stdout + r.stderr
    assert "fetch" in log.read_text().split()


def test_unit_pack_heads_are_probed_in_parallel_and_an_unreachable_one_says_nothing(tmp_path):
    a, ha, _ = _pack_origin(tmp_path, "PackA"); b, hb, _ = _pack_origin(tmp_path, "PackB")
    r = subprocess.run([sys.executable, str(BASE / "py" / "pack_heads.py")], input=f"{a}\n{b}\n{tmp_path / 'nope.git'}\n",
                       capture_output=True, text=True)
    rows = sorted(l.split("\t") for l in r.stdout.splitlines())
    assert rows == sorted([[str(a), "main", ha], [str(b), "main", hb]]), r.stdout + r.stderr


def test_unit_comfyui_already_at_the_newest_release_is_not_fetched(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    env = {"BASE_FAKE_ROOT": str(tmp_path)}
    _bash('base_env_setup; base_discover quiet; base_update_comfyui >/dev/null', env=env)            # first run: moves to v0.35.1
    spy, log = _git_spy(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo NEW=$COMFY_NEW; echo INFO=$COMFY_REF_INFO', env={**env, **spy})
    assert "NEW=0.35.1" in r.stdout and "INFO=v0.35.1 @" in r.stdout and "nothing to fetch" in r.stdout, r.stdout + r.stderr
    calls = log.read_text().split()
    assert "ls-remote" in calls and "fetch" not in calls, calls


def test_unit_the_os_stage_runs_in_the_background_and_its_results_reach_the_run(tmp_path):
    # apt is slow and touches nothing the network checks after it need: it runs beside them, and everything it decided
    # (its result, its rows, the driver, its failures and warnings, its "restart required") reaches the run at the join
    r = _bash('base_system(){ hdr "SYSTEM · stand-in"; ok "apt ran"; BASE_SYSTEM_RESULT="upgraded 3"; BASE_SYSTEM_ROWS=("pkg a 1 -> 2" "pkg b");'
              ' BASE_DRIVER_AFTER=590.1; BASE_FAILED+=("apt: one failed"); warn "kernel updated"; return 0; };'
              ' _base_system_start; echo "STARTED=$?"; echo "BEFORE_JOIN=$BASE_SYSTEM_RESULT"; _base_system_join; echo "RC=$?";'
              ' echo "RESULT=$BASE_SYSTEM_RESULT"; printf "ROW %s\\n" "${BASE_SYSTEM_ROWS[@]}"; echo "DRIVER=$BASE_DRIVER_AFTER";'
              ' printf "F %s\\n" "${BASE_FAILED[@]}"; printf "W %s\\n" "${BASE_WARN[@]}"')
    out = r.stdout
    assert "STARTED=0" in out and "BEFORE_JOIN=\n" in out and "RC=0" in out, out + r.stderr
    assert "apt ran" in out and "RESULT=upgraded 3" in out and "ROW pkg a 1 -> 2" in out and "ROW pkg b" in out and "DRIVER=590.1" in out, out
    assert "F apt: one failed" in out and "W kernel updated" in out, out
    assert out.index("BEFORE_JOIN") < out.index("apt ran")                     # its output is shown at the join, whole


def test_unit_a_restart_required_from_the_background_os_stage_still_stops_the_run(tmp_path):
    r = _bash('base_system(){ echo "  restart required: driver 590"; BASE_SYSTEM_RESULT="restart required"; return 10; };'
              ' _base_system_start; _base_system_join; echo "RC=$?"')
    assert "RC=10" in r.stdout and "restart required: driver 590" in r.stdout, r.stdout + r.stderr


def test_unit_the_os_stage_stays_in_the_foreground_on_a_runtime_machine_or_when_asked(tmp_path):
    r = _bash('_base_runtime_on(){ return 0; }; _base_system_start; echo "STARTED=$?"')
    assert "STARTED=1" in r.stdout
    r = _bash('BASE_SYSTEM_FOREGROUND=1; _base_system_start; echo "STARTED=$?"')
    assert "STARTED=1" in r.stdout


def test_unit_the_walks_skip_the_caches_but_never_the_staging_folder(tmp_path):
    r = _bash('UV_CACHE_DIR=/v/.cache/uv; PIP_CACHE_DIR=/v/.cache/pip; HF_HOME=/v/hf; HF_XET_CACHE=/x/xet; printf "%s\\n" "${BASE_PRUNE[@]}"; _base_cache_prune')
    out = r.stdout
    assert ".comfy-base-staging" in out
    for p in ("/v/.cache/uv", "/v/.cache/pip", "/v/hf", "/x/xet"):
        assert p in out, (p, out)


def test_unit_a_backup_venv_is_sized_once_until_it_changes(tmp_path):
    d = tmp_path / "venv.pre-x"; d.mkdir(); (d / "f").write_bytes(b"x" * 5000)
    st = tmp_path / "state"; st.mkdir()
    spy = tmp_path / "bin"; spy.mkdir(); log = tmp_path / "du.log"
    _stub(spy, "du", f'echo du >> "{log}"\nexec /usr/bin/du "$@"\n')
    snippet = f'BASE_STATE="{st}"; a=$(_base_dir_bytes "{d}"); b=$(_base_dir_bytes "{d}"); echo "A=$a B=$b"'
    r = _bash(snippet, env={"PATH": f"{spy}:{os.environ['PATH']}"})
    assert r.stdout.split()[0].split("=")[1] == r.stdout.split()[1].split("=")[1] != "0", r.stdout + r.stderr
    assert log.read_text().count("du") == 1                                        # the second answer came from the cache


def test_unit_the_venv_backend_is_read_from_torch_metadata_without_importing_it(tmp_path):
    site = tmp_path / "site"; (site / "torch-2.14.0+cu130.dist-info").mkdir(parents=True)
    (site / "torch-2.14.0+cu130.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: torch\nVersion: 2.14.0+cu130\n")
    (site / "torch").mkdir(); (site / "torch" / "__init__.py").write_text("raise ImportError('must not be imported')\n")
    r = _bash(f'PY="{sys.executable}"; _base_venv_backend', env={"PYTHONPATH": str(site)})
    assert r.stdout.strip() == "cu130", r.stdout + r.stderr


def test_unit_the_cublas_probe_compiles_once_per_toolkit_on_a_machine(tmp_path):
    cuda = tmp_path / "cuda"; (cuda / "bin").mkdir(parents=True); (cuda / "lib64").mkdir(); (cuda / "include").mkdir()
    (cuda / "lib64" / "libcublas.so.13").write_text("lib"); (cuda / "include" / "cublas_v2.h").write_text("h")
    log = tmp_path / "nvcc.log"
    _stub(cuda / "bin", "nvcc", f'if [ "$1" = --version ]; then echo "Cuda compilation tools, release 13.0, V13.0.88"; exit 0; fi\necho compile >> "{log}"\nexit 0\n')
    st = tmp_path / "state"; st.mkdir()
    run = f'BASE_STATE="{st}"; CUDACXX="{cuda}/bin/nvcc"; base_cuda_toolchain; BASE_CUDA_BUILD_OK=""; base_cuda_toolchain; echo OK=$BASE_CUDA_BUILD_OK'
    r = _bash(run)
    assert "OK=1" in r.stdout and log.read_text().count("compile") == 1, (r.stdout, r.stderr, log.read_text())
    (cuda / "lib64" / "libcublas.so.13").unlink()                                   # the library went (a restarted container)
    r = _bash(f'BASE_STATE="{st}"; CUDACXX="{cuda}/bin/nvcc"; base_cuda_toolchain')
    assert log.read_text().count("compile") == 2, log.read_text()                   # a changed toolkit is probed again


# ---------------------------------------------------------------- 3.7.5: Sage is proven by RUNNING it, mode by mode

B200_PROOF = """SAGE_PROOF verdict=only sm=sm100 use=sageattn_qk_int8_pv_fp16_triton version=2.2.0
SAGE_MODE auto fail ValueError: Unsupported CUDA architecture: sm100
SAGE_MODE sageattn_qk_int8_pv_fp16_cuda fail RuntimeError: no kernel image is available for execution on the device
SAGE_MODE sageattn_qk_int8_pv_fp16_triton ok cos=0.9998
SAGE_MODE sageattn_qk_int8_pv_fp8_cuda fail RuntimeError: no kernel image is available for execution on the device
SAGE_MODE sageattn_qk_int8_pv_fp8_cuda++ fail RuntimeError: no kernel image is available for execution on the device
SAGE_MODE sageattn3 fail sageattn3 not installed
SAGE_MODE sageattn3_per_block_mean fail sageattn3 not installed"""


def _sage_probe_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sage_probe", BASE / "py" / "sage_probe.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_unit_the_sage_probe_judges_auto_first_then_names_the_mode_that_runs():
    """A B200 (2026-10-16) imported a correct sm_100a build and died at the first step: upstream's sageattn(), which KJ's
    "auto" calls, has no sm100 branch. The probe calls every KJ mode; an exception is a failed mode, never a broken probe."""
    m = _sage_probe_module()

    def b200(mode):
        if mode == "auto":
            raise ValueError("Unsupported CUDA architecture: sm100")
        if mode == "sageattn_qk_int8_pv_fp16_triton":
            return True, "cos=0.9998"
        if mode == "sageattn_qk_int8_pv_fp8_cuda":
            return False, "cos=0.4100"                                  # it ran, and disagreed with PyTorch: not counted
        raise RuntimeError("no kernel image is available for execution on the device")
    res = m.probe(b200)
    assert list(res) == list(m.MODES) and res["auto"] == (False, "ValueError: Unsupported CUDA architecture: sm100")
    assert m.verdict(res) == ("only", "sageattn_qk_int8_pv_fp16_triton")
    out = m.report(*m.verdict(res)[:1], "sm100", m.verdict(res)[1], "2.2.0", res).splitlines()
    assert out[0] == "SAGE_PROOF verdict=only sm=sm100 use=sageattn_qk_int8_pv_fp16_triton version=2.2.0"
    assert "SAGE_MODE sageattn_qk_int8_pv_fp8_cuda fail cos=0.4100" in out and len(out) == 1 + len(m.MODES)
    assert m.verdict({"auto": (True, "cos=0.9999")}) == ("ok", "auto")
    assert m.verdict({"sageattn_qk_int8_pv_fp16_cuda": (True, ""), "sageattn_qk_int8_pv_fp8_cuda": (True, "")}) == ("only", "sageattn_qk_int8_pv_fp16_cuda")
    assert m.verdict(m.probe(lambda mode: (_ for _ in ()).throw(RuntimeError("x")))) == ("none", "-")


def test_unit_the_sage_probe_says_noimport_and_nogpu_instead_of_crashing(tmp_path):
    probe = str(BASE / "py" / "sage_probe.py")
    r = subprocess.run([sys.executable, probe], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(tmp_path / "empty")})
    assert r.returncode == 0 and r.stdout.startswith("SAGE_PROOF verdict=noimport "), r.stdout + r.stderr
    site = tmp_path / "site"; (site / "torch" / "nn").mkdir(parents=True); (site / "sageattention").mkdir()
    (site / "sageattention" / "__init__.py").write_text("def sageattn(*a, **k):\n    raise ValueError('never called')\n")
    (site / "torch" / "__init__.py").write_text("class cuda:\n    @staticmethod\n    def is_available():\n        return False\n")
    (site / "torch" / "nn" / "__init__.py").write_text(""); (site / "torch" / "nn" / "functional.py").write_text("")
    r = subprocess.run([sys.executable, probe], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(site)})
    assert r.returncode == 0 and r.stdout.strip() == "SAGE_PROOF verdict=nogpu sm=- use=- version=-", r.stdout + r.stderr


def _sage_venv(tmp_path, proof, count=None):
    """A venv whose python imports sageattention and answers sage_probe.py with <proof> (and counts the probes)."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True, exist_ok=True); py = v / "bin" / "python"
    pf = tmp_path / "proof.txt"; pf.write_text(proof + "\n")
    cnt = f'echo x >> "{count}"; ' if count else ""
    py.write_text(f'#!/bin/bash\ncase "$*" in *sage_probe.py*) {cnt}cat "{pf}";; *"import sageattention"*) exit 0;; *) exit 0;; esac\n'); py.chmod(0o755)
    gitbin = tmp_path / "gitbin"; gitbin.mkdir(exist_ok=True)
    (gitbin / "git").write_text('#!/bin/bash\n[ "$1" = ls-remote ] && { echo "abcdef1234567890abcdef1234567890abcdef12\trefs/heads/main"; exit 0; }\nexec /usr/bin/git "$@"\n'); (gitbin / "git").chmod(0o755)
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp-torch0-sm_100\n")
    env = {"PATH": f"{gitbin}:{os.environ['PATH']}", "HOME": str(tmp_path / "home")}
    base = f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=sm_100; '
    return v, pf, env, base


def test_unit_a_b200_build_that_imports_but_fails_auto_says_plainly_only_triton_works(tmp_path):
    """The build is kept (it is correct, and Triton runs on it), the run is not failed, and the report says what to set."""
    count = tmp_path / "probes"
    v, pf, env, base = _sage_venv(tmp_path, B200_PROOF, count)
    r = _bash(base + 'base_build_sageattention; echo F=${#BASE_FAILED[@]} W=${#BASE_WARN[@]}; printf "%s\\n" "${BASE_WARN[@]}"', env=env)
    assert "already built from SageAttention abcdef123456" in r.stdout and "F=0 W=1" in r.stdout, r.stdout + r.stderr
    assert 'KJ\'s "auto" (and --use-sage-attention) FAILS here: ValueError: Unsupported CUDA architecture: sm100' in r.stdout, r.stdout
    assert "Only the Triton mode works on this GPU: set the Sage node's mode to sageattn_qk_int8_pv_fp16_triton" in r.stdout, r.stdout
    assert "Sage: OK" not in r.stdout, r.stdout
    kept = (v / ".comfy-base-sage" / "sm_100.proof").read_text().splitlines()
    assert kept[0] == "key=sageattention-abcdef123456-cp-torch0-sm_100 sm=sm_100" and kept[1].startswith("SAGE_PROOF verdict=only"), kept
    # the same answer as data a package (or a node, at sys.prefix) reads: the KJ widget value for THIS GPU
    data = json.loads((v / ".comfy-base-sage" / "sm_100.json").read_text())
    assert data["mode"] == "sageattn_qk_int8_pv_fp16_triton" and data["verdict"] == "only" and data["sm"] == "sm_100", data
    assert data["runs"] == ["sageattn_qk_int8_pv_fp16_triton"] and data["fails"][0] == "auto" and data["build"].endswith("sm_100"), data
    r = _bash(base + 'base_sage_mode; echo RC=$?; BASE_GPU_SM=sm_103; base_sage_mode; echo RC=$?', env=env)
    assert r.stdout.split() == ["sageattn_qk_int8_pv_fp16_triton", "RC=0", "RC=1"], r.stdout + r.stderr
    # `sage` reads the kept proof (no second GPU process beside ComfyUI) and exits 3: a named mode runs, "auto" does not
    r = _bash(base + 'base_sage_check && echo RC=0 || echo RC=$?', env=env)
    assert "RC=3" in r.stdout and 'Sage: "auto" FAILS on sm_100' in r.stdout and "Only the Triton mode works" in r.stdout, r.stdout + r.stderr
    assert re.search(r"sageattn_qk_int8_pv_fp16_triton\s+ok", r.stdout) and re.search(r"auto\s+fail", r.stdout), r.stdout
    assert count.read_text().count("x") == 1, "the kept proof was not reused"
    # another GPU (or a rebuild) asks again; --fresh always does
    _bash(base + 'BASE_GPU_SM=sm_120; base_sage_check || true', env=env)
    _bash(base + 'base_sage_check --fresh || true', env=env)
    assert count.read_text().count("x") == 3


def test_unit_sage_ok_only_when_auto_runs_and_none_fails_the_run(tmp_path):
    v, pf, env, base = _sage_venv(tmp_path, "SAGE_PROOF verdict=ok sm=sm120 use=auto version=2.2.0\nSAGE_MODE auto ok cos=0.9999")
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp-torch0-sm_120\n")
    r = _bash(base + 'BASE_GPU_SM=sm_120; base_build_sageattention; echo F=${#BASE_FAILED[@]} W=${#BASE_WARN[@]}; base_sage_check; echo RC=$?', env=env)
    assert "Sage: OK -> sageattention 2.2.0 RUNS on sm_120" in r.stdout and "F=0 W=0" in r.stdout and "RC=0" in r.stdout, r.stdout + r.stderr
    assert json.loads((v / ".comfy-base-sage" / "sm_120.json").read_text())["mode"] == "auto"
    (v / ".comfy-base-sageattention").write_text("sageattention-abcdef123456-cp-torch0-sm_100\n")
    pf.write_text("SAGE_PROOF verdict=none sm=sm100 use=- version=2.2.0\nSAGE_MODE auto fail ValueError: Unsupported CUDA architecture: sm100\n")
    r = _bash(base + 'base_build_sageattention; echo F=${#BASE_FAILED[@]}; printf "%s\\n" "${BASE_FAILED[@]}"', env=env)
    assert "F=1" in r.stdout and "imports, but no mode runs on sm_100" in r.stdout, r.stdout + r.stderr
    # a probe that prints nothing (it broke) is said to be unknown, never OK, and keeps nothing
    assert json.loads((v / ".comfy-base-sage" / "sm_100.json").read_text())["mode"] == "disabled"
    (v / ".comfy-base-sage" / "sm_100.proof").unlink(); (v / ".comfy-base-sage" / "sm_100.json").unlink(); pf.write_text("")
    r = _bash(base + 'base_build_sageattention; echo F=${#BASE_FAILED[@]} W=${#BASE_WARN[@]}', env=env)
    assert "the Sage proof gave no verdict" in r.stdout and "F=0 W=1" in r.stdout and "Sage: OK" not in r.stdout, r.stdout
    assert not (v / ".comfy-base-sage" / "sm_100.proof").exists() and not (v / ".comfy-base-sage" / "sm_100.json").exists()


def test_unit_the_sage_command_is_the_bases_and_hygiene_drops_the_flag_where_auto_fails(tmp_path):
    """`bash <script> sage` was each package's import check, which said OK on the B200. It is the base's proof now; and
    ComfyUI's --use-sage-attention calls the same sageattn() (caught, then PyTorch attention on every call)."""
    src = (BASE / "lib" / "95-summary.sh").read_text()
    assert 'sage)      shift; base_env_setup; base_discover quiet || true; base_sage_check "$@" && exit 0; exit $? ;;' in src
    pkg = tmp_path / "pkg"; pkg.mkdir(); script = pkg / "p-script.sh"
    script.write_text(f'PKG_COMMANDS=( "sage|pkg_cmd_sage|import only" "hello|pkg_hello|hi" )\npkg_cmd_sage(){{ echo "Sage: OK -> imports"; exit 0; }}\n'
                      f'source "{BASE}/base.sh"\nbase_main "$@"\n')
    r = subprocess.run(["bash", str(script), "help"], capture_output=True, text=True)
    assert r.stdout.count(" sage ") == 1 and "hello" in r.stdout, r.stdout + r.stderr
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--use-sage-attention\n")
    vb = c / ".venv-cu130" / "bin"; vb.mkdir(parents=True); pf = tmp_path / "proof.txt"; pf.write_text(B200_PROOF + "\n")
    (vb / "python").write_text(f'#!/bin/bash\ncase "$*" in *sage_probe.py*) cat "{pf}";; *) exit 0;; esac\n'); (vb / "python").chmod(0o755)
    env = {"BASE_FAKE_ROOT": str(tmp_path)}
    r = _bash('base_env_setup; base_discover quiet; base_sage_proof; base_hygiene', env=env)
    args = tmp_path / "comfy-base" / "state" / "comfyui_args.txt"
    assert "--use-sage-attention" not in args.read_text() and "sageattn() fails on" in r.stdout, args.read_text() + r.stdout + r.stderr


UPSTREAM_ARCH_TABLE = """    for capability in compute_capabilities:
        if capability.startswith("9.0"):
            HAS_SM90 = True
            num = "90a"
        elif capability.startswith("10.0"):
            HAS_SM100 = True
            num = "100a"
        elif capability.startswith("12.0"):
            HAS_SM120 = True
            num = "120a"
        else:
            continue
        NVCC_FLAGS += ["-gencode", f"arch=compute_{num},code=sm_{num}"]
"""


def test_unit_a_b300_builds_the_qattn_kernels_as_sm_103a(tmp_path):
    """Upstream's arch table (thu-ml/SageAttention main d1a57a5) has no 10.3: TORCH_CUDA_ARCH_LIST=10.3 fell to `continue`
    and a B300 got no qattn kernel at all. 10.3 joins 10.0's row as sm_103a, once, and never over upstream's own row."""
    f = tmp_path / "setup.py"; f.write_text(UPSTREAM_ARCH_TABLE)
    _bash(f'_base_sage_patch_setup "{f}"; _base_sage_patch_setup "{f}"')
    body = f.read_text()
    assert body.count('startswith("10.3")') == 1 and 'num = "103a"' in body, body
    ns = {"NVCC_FLAGS": [], "compute_capabilities": {"10.3"}, "HAS_SM90": False, "HAS_SM100": False, "HAS_SM120": False}
    exec(textwrap.dedent(body), ns)
    assert ns["HAS_SM100"] and ns["NVCC_FLAGS"] == ["-gencode", "arch=compute_103a,code=sm_103a"], ns["NVCC_FLAGS"]
    ns = {"NVCC_FLAGS": [], "compute_capabilities": {"10.0"}, "HAS_SM90": False, "HAS_SM100": False, "HAS_SM120": False}
    exec(textwrap.dedent(body), ns); assert ns["NVCC_FLAGS"] == ["-gencode", "arch=compute_100a,code=sm_100a"]
    own = UPSTREAM_ARCH_TABLE.replace('        elif capability.startswith("12.0")', '        elif capability.startswith("10.3"):\n            num = "103f"\n        elif capability.startswith("12.0")')
    f.write_text(own); _bash(f'_base_sage_patch_setup "{f}"'); assert f.read_text() == own
    src = (BASE / "lib" / "40-packs.sh").read_text()
    assert '_base_sage_patch_setup "$src/setup.py"' in src[src.index("_base_sage_install()"):src.index("base_consolidate()")]
