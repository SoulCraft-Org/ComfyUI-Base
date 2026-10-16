"""unit tier: ComfyUI runs from a copy of its code on the machine's own disk (3.15.0), and a first install starts it once.

MEASURED on a RunPod pod with a network volume (the Video Swapper's install): `main.py --quick-test-for-ci` ran 5+
minutes at near-zero CPU, 262 of 265 threads waiting on futexes and one in a FUSE read, because the venv, the uv Python
and custom_nodes were all on the volume. These tests hold py/run_local.py (the copy), lib/84-run-local.sh (when and
from where a process starts) and the import check that rides the install's own launch to what the handbook says (§0.4)."""
import json, os, pathlib, stat, subprocess, sys, textwrap
import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "py" / "run_local.py"
sys.path.insert(0, str(BASE / "py"))
import run_local  # noqa: E402


def _bash(snippet, env=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("BASE_", "COMFY_REF", "COMFY_TAG", "SAGE_")):
            e.pop(k)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e)


def _sync(root, tree, venv, state, *extra):
    r = subprocess.run([sys.executable, str(TOOL), "sync", "--root", str(root), "--tree", str(tree), "--venv", str(venv),
                        "--state", str(state), "--jobs", "4", *extra], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    last = [ln for ln in r.stdout.splitlines() if ln.startswith("RUN_LOCAL ")][-1].split(" ", 5)
    return last[1], last[2], last[3], r.stdout


def _volume(tmp_path, real_venv=True):
    """A volume: a tree with code, a pack (its .git, a weight file), the data folders, a venv backup and a venv."""
    vol = pathlib.Path(os.path.realpath(tmp_path)) / "vol"
    tree = vol / "ComfyUI"
    (tree / "comfy").mkdir(parents=True)
    (tree / "main.py").write_text("print('main')\n")
    (tree / "comfy" / "model.py").write_text("X = 1\n")
    pack = tree / "custom_nodes" / "PackA"
    (pack / ".git").mkdir(parents=True)
    (pack / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (pack / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (pack / "weights.bin").write_bytes(b"w" * 4096)
    for d in ("models/checkpoints", "input", "output", "user/default"):
        (tree / d).mkdir(parents=True)
    (tree / "models" / "checkpoints" / "big.safetensors").write_bytes(b"m" * 100)
    (tree / ".venv-cu130.pre-pkg-20261016").mkdir()
    (tree / ".venv-cu130.pre-pkg-20261016" / "junk").write_text("old")
    venv = tree / ".venv-cu130"
    state = vol / "comfy-base" / "state"; state.mkdir(parents=True)
    if real_venv:
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    return vol, tree, venv, state


def _site(venv):
    return next((venv / "lib").glob("python*")) / "site-packages"


# ---------------------------------------------------------------------------------------------------- the copy itself

def test_unit_a_real_venv_is_copied_relocated_and_runs_from_the_copy(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    (venv / "bin" / "comfy-tool").write_text(f"#!{venv}/bin/python\nprint('tool')\n"); (venv / "bin" / "comfy-tool").chmod(0o755)
    (_site(venv) / "packa.pth").write_text(f"{tree}/custom_nodes/PackA\n")
    root = tmp_path / "run"
    v, ltree, lpy, out = _sync(root, tree, venv, state, "--big", "1024", "--check-import", "json")
    assert v == "synced", out
    gen = pathlib.Path(os.readlink(root / "current"))
    g = root / gen
    assert ltree == f"{g}{tree}" and lpy == f"{g}{venv}/bin/python"
    # the copy's python is a venv of its own: sys.prefix is the copy, not the volume
    r = subprocess.run([lpy, "-I", "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True)
    assert os.path.realpath(r.stdout.strip()) == os.path.realpath(f"{g}{venv}"), r.stdout + r.stderr
    lt = pathlib.Path(ltree)
    # code copied; data, .git and a pack's weight file are the volume's own, through symlinks
    assert (lt / "main.py").read_text() == "print('main')\n" and not (lt / "main.py").is_symlink()
    for name in ("models", "input", "output", "user"):
        assert (lt / name).is_symlink() and os.readlink(lt / name) == str(tree / name), name
    assert os.readlink(lt / "custom_nodes" / "PackA" / ".git") == str(tree / "custom_nodes" / "PackA" / ".git")
    assert os.readlink(lt / "custom_nodes" / "PackA" / "weights.bin") == str(tree / "custom_nodes" / "PackA" / "weights.bin")
    assert not (lt / ".venv-cu130.pre-pkg-20261016").exists()                     # a venv backup is never copied
    # the venv's own paths name the copy: a shebang, a .pth
    assert (pathlib.Path(lpy).parent / "comfy-tool").read_text().startswith(f"#!{g}{venv}/bin/python\n")
    assert (_site(pathlib.Path(f"{g}{venv}")) / "packa.pth").read_text() == f"{g}{tree}/custom_nodes/PackA\n"
    # the volume is never written
    assert (venv / "bin" / "comfy-tool").read_text().startswith(f"#!{venv}/bin/python\n")
    meta = json.loads((g / ".run-local.json").read_text())
    assert meta["complete"] and meta["copied"] > 0


def test_unit_paths_are_rewritten_only_where_they_name_a_copied_root(tmp_path):
    """A uv Python on the volume is copied too; pyvenv.cfg's home and bin/ links follow it. A path that only starts with
    a root's name (/x/ComfyUI-old) and one into the tree's data (models/) are left as they are."""
    vol, tree, venv, state = _volume(tmp_path, real_venv=False)
    pyroot = vol / "comfy-base" / "python"
    (pyroot / "cpython-3.14.0" / "bin").mkdir(parents=True)
    (pyroot / "cpython-3.14.0" / "bin" / "python3.14").symlink_to(sys.executable)
    (pyroot / "cpython-3.14").symlink_to(pyroot / "cpython-3.14.0")                # uv's minor-version link, absolute
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(pyroot / "cpython-3.14" / "bin" / "python3.14")
    (venv / "pyvenv.cfg").write_text(f"home = {pyroot}/cpython-3.14/bin\ninclude-system-site-packages = false\n")
    sp = venv / "lib" / "python3.14" / "site-packages"; sp.mkdir(parents=True)
    (sp / "a.pth").write_text(f"{tree}-old/x\n{tree}/models/y\n{tree}/custom_nodes/PackA\n")
    root = tmp_path / "run"
    v, ltree, lpy, out = _sync(root, tree, venv, state, "--python-root", str(pyroot), "--check-import", "-")
    assert v == "synced", out
    g = root / os.readlink(root / "current")
    lv = pathlib.Path(f"{g}{venv}")
    assert os.readlink(lv / "bin" / "python") == f"{g}{pyroot}/cpython-3.14/bin/python3.14"
    assert os.readlink(f"{g}{pyroot}/cpython-3.14") == f"{g}{pyroot}/cpython-3.14.0"
    assert os.readlink(f"{g}{pyroot}/cpython-3.14.0/bin/python3.14") == sys.executable   # outside every root: kept
    assert (lv / "pyvenv.cfg").read_text().startswith(f"home = {g}{pyroot}/cpython-3.14/bin\n")
    assert (lv / "lib" / "python3.14" / "site-packages" / "a.pth").read_text() == (
        f"{tree}-old/x\n{tree}/models/y\n{g}{tree}/custom_nodes/PackA\n")


def test_unit_an_unchanged_copy_is_not_walked_and_a_changed_one_links_what_did_not_change(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    root = tmp_path / "run"
    v1, t1, _, _ = _sync(root, tree, venv, state, "--check-import", "json")
    v2, t2, _, out2 = _sync(root, tree, venv, state, "--check-import", "json")
    assert (v1, v2) == ("synced", "fresh") and t1 == t2, out2
    # the install bumps run.gen and changes one file: a new generation, the unchanged file hardlinked, the new one copied
    (state / "run.gen").write_text("2\n")
    (tree / "comfy" / "model.py").write_text("X = 2\n")
    v3, t3, _, out3 = _sync(root, tree, venv, state, "--check-import", "json")
    assert v3 == "synced" and t3 != t1, out3
    a, b = pathlib.Path(t1), pathlib.Path(t3)
    assert (b / "comfy" / "model.py").read_text() == "X = 2\n"
    if a.exists():                                       # macOS has no /proc: an old generation is kept, never guessed at
        assert os.stat(a / "main.py").st_ino == os.stat(b / "main.py").st_ino
        assert (a / "comfy" / "model.py").read_text() == "X = 1\n"                 # never written under a running server
    else:
        assert os.stat(b / "main.py").st_nlink == 1                                  # its old twin was pruned
    assert "linked from the previous copy" in out3


def test_unit_a_new_pack_changes_the_fingerprint_without_a_bump(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    root = tmp_path / "run"
    _sync(root, tree, venv, state, "--check-import", "json")
    (tree / "custom_nodes" / "PackB").mkdir(); (tree / "custom_nodes" / "PackB" / "__init__.py").write_text("")
    v, t, _, _ = _sync(root, tree, venv, state, "--check-import", "json")
    assert v == "synced" and (pathlib.Path(t) / "custom_nodes" / "PackB" / "__init__.py").exists()


def test_unit_no_room_and_a_failed_check_leave_the_volume_in_charge(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    root = tmp_path / "run"
    v, t, py, out = _sync(root, tree, venv, state, "--check-import", "json", "--headroom", str(1 << 60))
    assert (v, t, py) == ("noroom", "-", "-") and "GB free" in out
    v, t, py, out = _sync(root, tree, venv, state, "--check-import", "no_such_module_anywhere")
    assert (v, t, py) == ("failed", "-", "-") and "no_such_module_anywhere" in out
    assert not [g for g in root.iterdir() if g.name.startswith("g")]                 # a failed generation is removed
    assert not (root / "current").exists()


def test_unit_auto_copies_only_from_a_network_filesystem(tmp_path):
    for kind in ("fuse", "fuse.mfsmount", "fuse.juicefs", "nfs", "nfs4", "cifs", "smb3", "ceph", "9p"):
        assert run_local.is_remote(kind), kind
    for kind in ("fuseblk", "ext4", "xfs", "overlay", "tmpfs", "btrfs", "zfs", "apfs", ""):
        assert not run_local.is_remote(kind), kind
    mi = tmp_path / "mountinfo"
    mi.write_text("22 1 0:21 / / rw - overlay overlay rw\n"
                  "40 22 0:50 / /workspace rw,relatime - fuse mfs#x rw\n"
                  "41 22 0:51 / /workspace/sub\\040dir rw - ext4 /dev/x rw\n")
    assert run_local.fstype("/workspace/ComfyUI", str(mi)) == "fuse"
    assert run_local.fstype("/workspace/sub dir/a", str(mi)) == "ext4"
    assert run_local.fstype("/opt/comfy-run", str(mi)) == "overlay"
    vol, tree, venv, state = _volume(tmp_path)                                        # this machine's tmp is a local disk
    v, _, _, out = _sync(tmp_path / "run", tree, venv, state, "--auto", "--check-import", "json")
    assert v == "skip" and "runs in place" in out


# ---------------------------------------------------------------------------------------------------- the shell side

def test_unit_the_mode_is_auto_on_runpod_only_and_off_for_a_dry_run_or_a_seed():
    r = _bash('for h in runpod vm local; do BASE_HOST=$h; echo "$h=$(_base_run_mode)"; done;'
              ' BASE_HOST=vm BASE_LOCAL_RUN=1; echo "forced=$(_base_run_mode)"; BASE_HOST=runpod BASE_LOCAL_RUN=0; echo "off=$(_base_run_mode)";'
              ' BASE_LOCAL_RUN=1 BASE_DRY=1; echo "dry=$(_base_run_mode)"; BASE_DRY=0 BASE_SEED=1; echo "seed=$(_base_run_mode)"')
    assert r.stdout.split() == ["runpod=auto", "vm=off", "local=off", "forced=on", "off=off", "dry=off", "seed=off"], r.stdout + r.stderr


def test_unit_a_process_starts_from_the_copy_when_it_is_good_and_from_the_volume_when_not(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    root = tmp_path / "run"
    env = f'COMFY="{tree}"; VENV="{venv}"; PY="{venv}/bin/python"; BASE_STATE="{state}"; BASE_HOME="{vol}/comfy-base"; BASE_RUN_ROOT="{root}"; BASE_RUN_CHECK=json;'
    r = _bash(env + ' BASE_LOCAL_RUN=1; _base_run_here; echo "DIR=$RUN_HERE_DIR"; echo "PY=$RUN_HERE_PY"; echo "V=$BASE_RUN_VERDICT"')
    g = root / os.readlink(root / "current")
    assert f"DIR={g}{tree}" in r.stdout and f"PY={g}{venv}/bin/python" in r.stdout and "V=synced" in r.stdout, r.stdout + r.stderr
    assert "copied from the volume" in r.stdout
    r = _bash(env + ' BASE_LOCAL_RUN=0; _base_run_here; echo "DIR=$RUN_HERE_DIR"; echo "PY=$RUN_HERE_PY"')
    assert f"DIR={tree}" in r.stdout and f"PY={venv}/bin/python" in r.stdout
    # a refused copy is said once, and the second ask for the same code does not walk the volume again
    r = _bash(env + f' BASE_LOCAL_RUN=1; BASE_RUN_ROOT="{root}-2"; BASE_RUN_CHECK=no_such_mod; _base_run_here; _base_run_here; echo "DIR=$RUN_HERE_DIR V=$BASE_RUN_VERDICT"')
    assert f"DIR={tree} V=failed" in r.stdout and r.stdout.count("not copied") == 1, r.stdout


def test_unit_the_pid_matcher_knows_a_server_running_from_the_copy(tmp_path):
    comfy = "/workspace/ComfyUI"
    table = "\n".join([
        f"101|1|/opt/comfy-run/g20261016-120000-7{comfy}|/opt/comfy-run/g20261016-120000-7{comfy}/.venv-cu130/bin/python main.py --listen 0.0.0.0",
        f"102|1|/opt/comfy-run/g20261016-120000-7/workspace/Other|/x/python main.py",
        f"103|1|/elsewhere/g1{comfy}|/x/python main.py"])
    r = _bash(f'COMFY="{comfy}"; printf "%s\\n" "{table}" | _base_pids_from_table')
    assert r.stdout.split() == ["101"], r.stdout + r.stderr
    r = _bash(f'COMFY="{comfy}"; BASE_RUN_ROOT=/elsewhere; printf "%s\\n" "{table}" | _base_pids_from_table')
    assert r.stdout.split() == ["103"], r.stdout + r.stderr


def test_unit_the_boot_sources_the_copy_beside_the_launch_line():
    r = subprocess.run(["bash", "-c", f'source "{BASE}/lib/boot.sh"; BOOT_HOME="{BASE}"; boot_launch_lib && declare -F _base_run_here _base_start_comfy'],
                       capture_output=True, text=True)
    assert "_base_run_here" in r.stdout and "_base_start_comfy" in r.stdout, r.stdout + r.stderr
    src = (BASE / "lib" / "boot.sh").read_text()
    assert 'if [ "$RUN_HERE_PY" = "$PY" ] && ! "$PY" -c \'import torch\'' in src            # the volume's torch only without a copy


# ---------------------------------------------------------------------------------------------------- one start, not two

def _deferred_env(tmp_path):
    c = tmp_path / "ComfyUI"; (c / ".venv-cu130" / "bin").mkdir(parents=True)
    (c / ".venv-cu130" / "bin" / "python").symlink_to(sys.executable)
    marker = tmp_path / "quick-test-ran"
    (c / "main.py").write_text(f"open({str(marker)!r}, 'a').write('x')\n")
    st = tmp_path / "state"; (st / "logs").mkdir(parents=True)
    return c, marker, (f'COMFY="{c}"; VENV="{c}/.venv-cu130"; PY="$VENV/bin/python"; BASE_STATE="{st}"; COMFY_LOG="{st}/logs/comfyui.log";'
                       f' HOSTPORT=127.0.0.1:9; BASE_DRY=0; BASE_NO_NET=0; BASE_VENV_RESULT=reused; PACK_DIRS=(a b);'
                       f' BASE_FAILED=(); BASE_UPSTREAM=(); _base_repair_modules(){{ return 1; }};')


def test_unit_with_no_server_to_keep_the_launch_is_the_import_check(tmp_path):
    c, marker, env = _deferred_env(tmp_path)
    r = _bash(env + ' BASE_RESTART=1; base_import_check >/dev/null; echo "R=$BASE_IMPORT_RESULT D=$BASE_IMPORT_DEFERRED"')
    assert "R=deferred to the launch D=1" in r.stdout and not marker.exists(), r.stdout + r.stderr
    for off in ('BASE_RESTART=0;', 'BASE_RESTART=1; BASE_IMPORT_DEFER=0;', 'BASE_RESTART=1; BASE_SEED=1;'):
        r = _bash(env + off + ' base_import_check >/dev/null; echo "R=$BASE_IMPORT_RESULT D=$BASE_IMPORT_DEFERRED"')
        assert "D=0" in r.stdout and marker.exists(), off + r.stdout + r.stderr
        marker.unlink()


def test_unit_the_launch_is_judged_by_the_import_check_rules(tmp_path):
    c, marker, env = _deferred_env(tmp_path)
    # the start answers and its log names a pack that did not import: named for upstream, the server stays up
    start = ('_base_restart_comfy(){ echo "old lines" >/dev/null; printf "%s\\n" "Traceback" "ModuleNotFoundError: No module named zz"'
             ' "   0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackZ" >> "$COMFY_LOG"; echo started >> "$BASE_STATE/starts"; return 0; };')
    r = _bash(env + start + ' echo "an earlier start: (IMPORT FAILED): /x/custom_nodes/Old" > "$COMFY_LOG"; BASE_RESTART=1;'
              ' base_import_check >/dev/null; _base_restart_proven; echo "RC=$? R=$BASE_IMPORT_RESULT"; printf "U %s\\n" "${BASE_UPSTREAM[@]}"')
    assert "RC=0" in r.stdout and "R=ok: ComfyUI starts; 1 pack(s) named for upstream (PackZ); the launch was the check" in r.stdout, r.stdout + r.stderr
    assert "U pack PackZ" in r.stdout and "Old" not in r.stdout and not marker.exists()        # only THIS start's slice is judged
    assert (tmp_path / "state" / "starts").read_text().count("started") == 1
    # a clean start
    clean = '_base_restart_comfy(){ echo "Import times for custom nodes:" >> "$COMFY_LOG"; return 0; };'
    r = _bash(env + clean + ' BASE_RESTART=1; base_import_check >/dev/null; _base_restart_proven >/dev/null; echo "R=$BASE_IMPORT_RESULT"')
    assert "R=ok (2 packs import; the launch was the check)" in r.stdout, r.stdout + r.stderr


def test_unit_a_launch_that_never_answers_is_judged_the_long_way_and_started_again(tmp_path):
    c, marker, env = _deferred_env(tmp_path)
    n = tmp_path / "n"
    start = (f'_base_restart_comfy(){{ i=$(cat "{n}" 2>/dev/null || echo 0); echo $((i+1)) > "{n}"; [ "$i" -ge 1 ]; }};'
             ' _base_stop_comfy(){ echo STOPPED; };')
    r = _bash(env + start + ' BASE_RESTART=1; base_import_check >/dev/null; _base_restart_proven; echo "RC=$? R=$BASE_IMPORT_RESULT"')
    assert "STOPPED" in r.stdout and marker.exists() and "RC=0" in r.stdout, r.stdout + r.stderr   # the quick test ran, then a second start
    assert n.read_text().strip() == "2" and "R=ok (2 packs import)" in r.stdout


def test_unit_an_own_pack_is_not_judged_before_the_launch_that_imports_it(tmp_path):
    cn = tmp_path / "custom_nodes"; (cn / "Mine").mkdir(parents=True)
    prev = tmp_path / "state" / "own" / "prev" / "base" / "Mine"; prev.mkdir(parents=True)
    r = _bash(f'CN="{cn}"; BASE_STATE="{tmp_path}/state"; OWN_CHANGED=(Mine); BASE_IMPORT_DEFERRED=1; _base_own_packs_judge "{tmp_path}/none.log"; echo done')
    assert "done" in r.stdout and prev.exists(), r.stdout + r.stderr                 # the previous copy is still there to restore


# ---------------------------------------------------------------------------------------------------- the warm read and the head start

def test_unit_warm_reads_every_file_the_copy_would_hold_and_only_on_a_network_filesystem(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    r = subprocess.run([sys.executable, str(TOOL), "warm", "--tree", str(tree), "--venv", str(venv), "--jobs", "8", "--big", "1024"],
                       capture_output=True, text=True)
    line = r.stdout.strip().splitlines()[-1]
    assert r.returncode == 0 and line.startswith("WARM done ") and " files " in line, r.stdout + r.stderr
    n = int(line.split()[2])
    # the venv's files and the tree's code; never models/, a .git, the venv backup or a pack's weight file (--big)
    expect = sum(1 for p in venv.rglob("*") if p.is_file() and not p.is_symlink()) + 3       # main.py, comfy/model.py, PackA/__init__.py
    assert n == expect, (n, expect)
    r = subprocess.run([sys.executable, str(TOOL), "warm", "--tree", str(tree), "--venv", str(venv), "--auto"], capture_output=True, text=True)
    assert r.stdout.startswith("WARM skip "), r.stdout + r.stderr                     # this machine's tmp is a local disk


def test_unit_a_start_from_the_volume_reads_it_in_the_background_once_per_change(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    env = (f'COMFY="{tree}"; VENV="{venv}"; PY="{venv}/bin/python"; BASE_STATE="{state}"; BASE_HOME="{vol}/comfy-base";'
           f' BASE_RUN_ROOT="{tmp_path}/run"; BASE_LOCAL_RUN=1; BASE_RUN_CHECK=no_such_mod;')
    r = _bash(env + ' _base_run_here; _base_run_here; echo "DIR=$RUN_HERE_DIR"; wait')
    assert f"DIR={tree}" in r.stdout and r.stdout.count("reading ComfyUI's code from the volume") == 1, r.stdout + r.stderr
    assert "WARM " in (state / "logs" / "run-warm.log").read_text()


def test_unit_the_head_start_copies_in_the_background_and_the_start_after_finds_it_current(tmp_path):
    vol, tree, venv, state = _volume(tmp_path)
    env = (f'COMFY="{tree}"; VENV="{venv}"; PY="{venv}/bin/python"; BASE_STATE="{state}"; BASE_HOME="{vol}/comfy-base";'
           f' BASE_RUN_ROOT="{tmp_path}/run"; BASE_LOCAL_RUN=1; BASE_RUN_CHECK=json;')
    r = _bash(env + ' _base_run_prewarm; wait; _base_run_here; echo "V=$BASE_RUN_VERDICT"')
    assert "in the background" in r.stdout and "V=fresh" in r.stdout, r.stdout + r.stderr
    assert "head start done: synced" in (state / "logs" / "run-local.log").read_text()


def test_unit_the_boot_starts_the_copy_before_sshd_and_jupyterlab():
    src = (BASE / "lib" / "boot.sh").read_text()
    seq = src[src.index("  boot_tcmalloc\n"):src.index("  boot_apps\n", src.index("  boot_tcmalloc\n"))]
    assert seq.index("boot_prewarm") < seq.index("boot_sshd") < seq.index("boot_jupyter") < seq.index("boot_comfy"), seq
