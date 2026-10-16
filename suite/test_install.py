"""install tier: the stub package (base/comfyui-base/_build/stub) run end to end against a fake pod. Offline, fake
downloads (sparse files at the declared size), a fake venv, no server. This is the base's own proof."""
import os, json, pathlib, pytest
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"
pytestmark = [pytest.mark.install, pytest.mark.skipif(not STUB.exists(), reason="_build/stub is repo tooling, not in the shipped base")]
SCRIPT = STUB / "stub-script.sh"
from basetest import fake_pod, run_script, tree_hash, load_package, manifest_covers_active_loaders


def test_install_help_syntax_and_dispatch_surface(tmp_path):
    fake_pod(tmp_path, STUB)
    r = run_script(SCRIPT, "help", fake_root=tmp_path)
    assert r.returncode == 0 and "--check" in r.stdout and "test [tier]" in r.stdout and "rescue" in r.stdout and "--latest" in r.stdout, r.stdout + r.stderr
    assert run_script(SCRIPT, "bogus", fake_root=tmp_path).returncode == 2


def test_install_check_mode_touches_nothing(tmp_path):
    fake_pod(tmp_path, STUB, strays=True, duplicates=True, foreign=True)
    before = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r = run_script(SCRIPT, "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == before
    assert "would" in r.stdout


def test_install_real_run_moves_verifies_reports_and_second_run_is_a_noop(tmp_path):
    pod = fake_pod(tmp_path, STUB, strays=True)
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert "moved from" in r.stdout and (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists()
    assert (pod / "models" / "loras" / "SDXL" / "Detailers" / "stub_local.safetensors").exists()
    assert (pod / "user" / "default" / "workflows" / "stub-workflow.json").exists()
    wf = json.loads((tmp_path / "pkg" / "stub-workflow.json").read_text())
    assert wf["nodes"][0]["widgets_values"][0] == "Example/Real/stub_vae.safetensors"                      # pristine copy synced
    assert wf["nodes"][1]["widgets_values"][0] == "stub_bypassed.gguf"                                    # bypassed: untouched
    assert "BYPASSED" in r.stdout and "stub_bypassed.gguf" in r.stdout and not list(tmp_path.rglob("stub_bypassed.gguf"))
    assert (tmp_path / "comfy-base" / "state" / "packages" / "stub.manifest").exists()
    assert "ONE STEP LEFT" in r.stdout or "not running" in r.stdout or "skipped" in r.stdout
    h = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r2 = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_FULL": "1"})   # 3.11.0: the full pipeline again (without it the run is ALREADY INSTALLED)
    assert r2.returncode == 0, r2.stdout[-3000:]
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == h, "second run changed the tree"
    assert "downloaded 0" in r2.stdout and "reusing our venv" in r2.stdout


def test_install_yes_deletes_only_duplicates_and_superseded(tmp_path):
    pod = fake_pod(tmp_path, STUB, strays=True, duplicates=True, foreign=True)
    r = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_YES": "1"})
    assert r.returncode == 0, r.stdout[-3000:]
    assert not (tmp_path / "dup" / "stub_vae.safetensors").exists() and not (pod / "models" / "vae" / "Example" / "stub_old.safetensors").exists()
    assert (tmp_path / "foreign" / "my_private_lora.safetensors").exists() and (pod / "models" / "vae" / "stub_legacy" / "unknown.safetensors").exists()
    assert "unknown, left alone" in r.stdout


def test_install_missing_local_file_fails_and_names_the_dest(tmp_path):
    fake_pod(tmp_path, STUB, local_present=False)
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 1 and "stub_local.safetensors" in r.stdout and "loras/SDXL/Detailers" in r.stdout, r.stdout[-3000:]


def test_install_failed_download_exits_non_zero_and_is_named(tmp_path):
    fake_pod(tmp_path, STUB)
    r = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_FAKE_DL": "fail"})
    assert r.returncode == 1 and "stub_vae.safetensors" in r.stdout and "FAILED" in r.stdout, r.stdout[-3000:]


def test_install_disk_gate_stops_before_any_download(tmp_path):
    fake_pod(tmp_path, STUB)
    r = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_FAKE_FREE_GB": "1"})
    assert r.returncode == 1 and "not enough space" in r.stdout and not list((tmp_path / "ComfyUI" / "models").rglob("stub_vae.safetensors")), r.stdout[-3000:]


def test_install_second_package_does_not_refetch_a_shared_family_file(tmp_path):
    pod = fake_pod(tmp_path, STUB)
    assert run_script(SCRIPT, fake_root=tmp_path).returncode == 0
    twin = tmp_path / "twin"; twin.mkdir()
    (twin / "Twin-script.sh").write_text((STUB / "stub-script.sh").read_text().replace('PKG_ID="stub"', 'PKG_ID="twin"').replace('WF_NAME="stub-workflow.json"', 'WF_NAME="Twin Workflow.json"'))
    (twin / "Twin Workflow.json").write_text((STUB / "stub-workflow.json").read_text())
    h = tree_hash(pod / "models")
    r = run_script(twin / "Twin-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:]
    assert tree_hash(pod / "models") == h and "downloaded 0" in r.stdout
    assert (tmp_path / "comfy-base" / "state" / "packages" / "twin.manifest").exists()


def test_install_a_poisoned_venv_is_rebuilt_not_reused(tmp_path):
    pod = fake_pod(tmp_path, STUB, poison_venv=True)
    r = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_PERSIST_ROOT_FORCE": str(tmp_path)})
    assert r.returncode == 0 and "rebuil" in r.stdout.lower(), r.stdout[-3000:]
    assert not (pod / ".venv-cu130" / "pyvenv.cfg").exists() or "/tmp/uv-python" not in (pod / ".venv-cu130" / "pyvenv.cfg").read_text()


def test_install_rescue_puts_back_a_venv_that_cannot_boot_and_changes_nothing_when_healthy(tmp_path):
    pod = fake_pod(tmp_path, STUB, dangling_venv=True)
    r = run_script(SCRIPT, "rescue", fake_root=tmp_path, env={"BASE_YES": "1"})
    assert r.returncode == 0 and os.access(pod / ".venv-cu130" / "bin" / "python", os.X_OK), r.stdout[-3000:]
    h = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r2 = run_script(SCRIPT, "rescue", fake_root=tmp_path)
    assert r2.returncode == 0 and "healthy" in r2.stdout and tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == h


def test_install_a_package_refuses_an_older_base(tmp_path):
    fake_pod(tmp_path, STUB)
    s = tmp_path / "pkg" / "stub-script.sh"; s.write_text(s.read_text().replace('BASE_MIN="1.0.0"', 'BASE_MIN="99.0.0"'))
    r = run_script(s, "--check", fake_root=tmp_path)
    assert r.returncode == 3 and "99.0.0" in r.stderr, r.stdout + r.stderr


def test_install_a_workflow_version_mismatch_refuses_to_run(tmp_path):
    fake_pod(tmp_path, STUB)
    w = tmp_path / "pkg" / "stub-workflow.json"; d = json.loads(w.read_text()); d["extra"]["package_version"] = "0.9.0"; w.write_text(json.dumps(d))
    r = run_script(SCRIPT, "--check", fake_root=tmp_path)
    assert r.returncode == 1 and "0.9.0" in r.stdout and "1.0.0" in r.stdout, r.stdout[-2000:]


def test_install_token_is_saved_once_with_tight_permissions(tmp_path):
    fake_pod(tmp_path, STUB)
    run_script(SCRIPT, fake_root=tmp_path, env={"HF_TOKEN": "hf_x"})
    t = tmp_path / "comfy-base" / "state" / "tokens.env"
    assert oct(t.stat().st_mode)[-3:] == "600" and t.read_text().count("HF_TOKEN") == 1


def test_install_stub_manifest_covers_its_active_loaders_and_the_helper_sees_the_bypassed_one():
    pkg = load_package(STUB)
    assert pkg.id == "stub" and len(pkg.models) == 2 and pkg.superseded == ["stub_old.safetensors"]
    assert manifest_covers_active_loaders(pkg) == []
    from basetest import active_loaders, loader_cats
    assert ("unet", "stub_bypassed.gguf") not in active_loaders(pkg.wf, loader_cats(pkg))


# ---------------------------------------------------------------- base V2.0.0: three layouts

def test_install_bare_volume_gets_a_tree_at_workspace_comfyui_and_second_run_is_a_noop(tmp_path):
    pod = fake_pod(tmp_path, STUB, layout="bare")
    assert not pod.exists()
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert "materialis" in r.stdout and (pod / "main.py").exists() and (pod / "custom_nodes").is_dir()
    assert (pod / "user" / "default" / "workflows" / "stub-workflow.json").exists()
    h = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r2 = run_script(SCRIPT, fake_root=tmp_path)
    assert r2.returncode == 0, r2.stdout[-4000:] + r2.stderr[-1000:]
    assert "materialis" not in r2.stdout.split("SUMMARY")[0] or "already" in r2.stdout
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == h


@pytest.mark.parametrize("layout", ["official", "community", "bare"])
def test_install_check_mode_touches_nothing_on_every_layout(tmp_path, layout):
    fake_pod(tmp_path, STUB, layout=layout, strays=(layout == "official"))
    before = tree_hash(tmp_path, ignore=("logs", "sizes.tsv"))
    r = run_script(SCRIPT, "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert tree_hash(tmp_path, ignore=("logs", "sizes.tsv")) == before
    assert not (tmp_path / "ComfyUI" / "main.py").exists() or layout == "official"
    assert "would" in r.stdout


def test_install_tool_venv_is_built_once_and_reused(tmp_path):
    fake_pod(tmp_path, STUB, layout="official")
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:]
    jl = tmp_path / "comfy-base" / "tools" / "bin" / "jupyter-lab"; stamp = tmp_path / "comfy-base" / "tools" / ".comfy-base-tools"
    assert jl.exists() and os.access(jl, os.X_OK) and stamp.exists()
    m = stamp.stat().st_mtime_ns
    r2 = run_script(SCRIPT, fake_root=tmp_path, env={"BASE_FULL": "1"})   # 3.11.0: the full pipeline again (without it the run is ALREADY INSTALLED)
    assert r2.returncode == 0 and "tools venv present" in r2.stdout and stamp.stat().st_mtime_ns == m


def _sparse(p, size):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.truncate(size)


def test_install_consolidate_scans_only_the_volume_and_lists_unclaimed_files(tmp_path):
    """The container disk is never scanned: a declared file that exists only on the fake image is downloaded, not
    moved, and stays where it was. The template's own model on the volume, claimed by no row, is reported as unclaimed."""
    pod = fake_pod(tmp_path, STUB, layout="community")
    img = tmp_path / "image" / "ComfyUI" / "models" / "vae" / "stub_vae.safetensors"; _sparse(img, 4096)
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists() and img.exists()
    models_line = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("models ")][-1]
    assert "downloaded 1" in models_line and "moved 1" in models_line, models_line       # moved: the fixture's LOCAL file; downloaded: the vae the image tree could not supply
    assert "stub_local.safetensors" in r.stdout.split("moved 1")[1][:400] or (pod / "models" / "loras" / "SDXL" / "Detailers" / "stub_local.safetensors").exists()
    assert "unclaimed 1" in r.stdout and "example_engine_pruned_int8.safetensors" in r.stdout
    assert (pod / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors").exists()


def test_install_a_second_tree_on_the_volume_is_consolidated_into_the_canonical_one_and_reported(tmp_path):
    from basetest import _fake_code_tree
    pod = fake_pod(tmp_path, STUB, layout="official")                            # moves: the fixture's LOCAL file + the second tree's vae
    second = tmp_path / "runpod-slim" / "ComfyUI"; _fake_code_tree(second, "0.34.7")
    stray = second / "models" / "vae" / "stub_vae.safetensors"; _sparse(stray, 4096)
    r = run_script(SCRIPT, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert "other tree" in r.stdout and str(second) in r.stdout
    assert (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists() and not stray.exists()
    models_line = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("models ")][-1]
    assert "moved 2" in models_line and "downloaded 0" in models_line, models_line
    assert (second / "main.py").exists()                                                  # never deleted



def _stub_with_packs(tmp_path, rows):
    s = tmp_path / "pkg" / "stub-script.sh"
    s.write_text(s.read_text().replace("PACKS=()", "PACKS=(\n%s\n)" % "\n".join(' "%s"' % r for r in rows), 1))
    return s


def test_install_a_row_branch_is_planned_cloned_and_recorded_by_a_whole_run(tmp_path):
    """3.6.1, end to end through base_main: a remote whose HEAD is stale (rgthree publishes a 2023 `dev`). The plan clones
    the row's branch, for the base's rgthree row and for a package's own row; a real run keeps the branch in the record
    podctl writes back. (The moves themselves are proven against real git remotes in the unit tier.)"""
    fake_pod(tmp_path, STUB)
    s = _stub_with_packs(tmp_path, ["StubPack|https://github.com/example/StubPack|" + "a" * 40 + "||the fixture pack|main"])
    r = run_script(s, "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "git clone --depth 1 --branch main https://github.com/rgthree/rgthree-comfy" in r.stdout, r.stdout[-3000:]
    assert "git clone --depth 1 --branch main https://github.com/example/StubPack" in r.stdout, r.stdout[-3000:]
    assert "git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes (its HEAD)" in r.stdout          # no branch: as before
    r = run_script(s, fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert '"StubPack|https://github.com/example/StubPack|' + "a" * 40 + '||the fixture pack|main"' in r.stdout, r.stdout[-3000:]
    assert '||switches, bypassers, any-switch, fast group bypassers/muters, radio panels, Power Lora Loader|main"' in r.stdout, r.stdout[-3000:]
    d = run_script(s, env={"BASE_DECLARE_ONLY": "1"}, fake_root=tmp_path)
    assert "PACKROW StubPack|https://github.com/example/StubPack|" + "a" * 40 + "||the fixture pack|main" in d.stdout, d.stdout + d.stderr
    pkg = load_package(tmp_path / "pkg")
    assert {p["dir"]: p["branch"] for p in pkg.packs}["StubPack"] == "main"


def test_install_a_sixth_field_that_is_not_a_branch_stops_the_run(tmp_path):
    fake_pod(tmp_path, STUB)
    s = _stub_with_packs(tmp_path, ["StubPack|https://github.com/example/StubPack|" + "a" * 40 + "||why|" + "b" * 40])
    r = run_script(s, "--check", fake_root=tmp_path)
    assert r.returncode != 0 and "not a branch name" in r.stdout + r.stderr, r.stdout[-3000:] + r.stderr[-1000:]
