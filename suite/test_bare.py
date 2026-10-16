"""bare tier: the base against an environment that gives it NOTHING.

Every defect the cold-install pod found on 2026-09-08 was the same shape — the base depended on
something its usual surroundings happened to supply for free, and nobody noticed until a machine
turned up without it:

    the shared network volume supplied  tools/bin/uv     -> --check could not bootstrap  (2.0.45)
    the repo checkout supplied         _build/          -> a test crashed the gate       (2.0.46)
    the MooseFS mount supplied         no lost+found    -> the model index gave up       (2.0.47)
    the older pod images supplied      a full CUDA kit  -> both source builds died       (2.0.49)

The rest of the suite tests the base against a realistic environment, which is exactly why none of
these appeared in it. These tests are hostile on purpose: empty roots, missing tools, unreadable
directories, a compiler that cannot link. They run on a Mac in seconds and cost no pod.
"""
import os, re, subprocess, pathlib, pytest

pytestmark = pytest.mark.bare
BASE = pathlib.Path(__file__).resolve().parents[1]
MODELS_SH = BASE / "lib" / "50-models.sh"
PACKS_SH = BASE / "lib" / "40-packs.sh"


def _bash(snippet, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'],
                          capture_output=True, text=True, env=e)


def _stub_nvcc(d, rc):
    """A compiler that answers the only question base_cuda_toolchain asks: does this link?"""
    d.mkdir(parents=True, exist_ok=True)
    p = d / "nvcc"
    p.write_text('#!/bin/bash\n[ "$1" = "--version" ] && { echo "Cuda compilation tools, release 13.0, V13.0.88"; exit 0; }\nexit %d\n' % rc)
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------- 2.0.45: no uv anywhere
def test_bare_dry_run_survives_a_pod_with_no_uv(tmp_path):
    """--check must not need a tool that only the real run installs.

    _base_ensure_uv correctly refuses to write during a dry run, so on a pod with no comfy-base/tools
    it can only PROMISE uv. _base_python_pick then ran `uv python list` against nothing, found no
    CPython and failed the venv step -- and because podctl runs --check before the real run and stops
    on non-zero, the base could not be installed onto any genuinely new pod."""
    home = tmp_path / "comfy-base"
    r = _bash("base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT",
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_HOME": str(home), "BASE_DRY": "1",
                   "PATH": "/usr/bin:/bin"})
    out = r.stdout + r.stderr
    assert "failed-pick" not in out, "the dry run failed over a venv that does not exist yet:\n" + out[-1500:]
    assert "RESULT=would-build" in out or "RESULT=would-rebuild" in out, out[-1500:]


# ---------------------------------------------------------------- 2.0.47: an unreadable lost+found
def test_bare_lost_and_found_is_pruned_from_both_walks():
    """Every ext4 volume has one, it is mode 700 owned by nobody, and a root-squashed pod-local mount
    denies it even to uid 0. find then exits 1, the index is 'incomplete', and the documented remedy
    for a short index is to re-download -- 122 GiB that was already on the disk."""
    src = MODELS_SH.read_text(encoding="utf-8")
    for var in ("BASE_PRUNE=", "BASE_PRUNE_DIRS="):
        i = src.index(var)
        block = src[i:src.index("')' )", i)]
        assert "lost+found" in block, f"{var} does not prune lost+found"


# ---------------------------------------------------------------- 2.0.53: the Linux temp roots
def test_bare_temp_roots_are_pruned_from_both_walks():
    """The install test leaves zero-filled files of each row's declared size under pytest's /tmp tree. A real run
    walks the whole pod from /, matched one by name AND size, and moved 862 MB of zeros in as a LoRA.
    The Mac's temp root was pruned; Linux's were not. An exact -path, never a
    prefix: a test's own walk starts INSIDE its fake root, which on Linux lives under /tmp."""
    import re
    src = MODELS_SH.read_text(encoding="utf-8")
    for var in ("BASE_PRUNE=", "BASE_PRUNE_DIRS="):
        i = src.index(var)
        block = src[i:src.index("')' )", i)]
        for root in ("/var/folders", "/tmp", "/var/tmp"):
            assert re.search(r"-path %s(\s|$)" % re.escape(root), block), f"{var} does not prune {root}"
        assert "/tmp/" not in block and "/tmp*" not in block, f"{var} prunes a /tmp PREFIX, which hides a test's fake root"

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory, so the trap cannot be reproduced as root")
def test_bare_an_unreadable_directory_does_not_void_the_model_index(tmp_path):
    """The behavioural half: a directory the walk cannot enter must not make the whole index untrusted."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.safetensors").write_bytes(b"x" * 16)
    bad = tmp_path / "lost+found"
    bad.mkdir()
    (bad / "hidden.safetensors").write_bytes(b"y" * 16)
    bad.chmod(0o000)
    try:
        r = _bash("base_env_setup; base_discover quiet; _base_index >/dev/null 2>&1; echo RC=$?",
                  env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_HOME": str(tmp_path / "comfy-base")})
        out = r.stdout + r.stderr
        assert "model index incomplete" not in out, "lost+found still voids the index:\n" + out[-1200:]
    finally:
        bad.chmod(0o700)


# ---------------------------------------------------------------- 2.0.49: nvcc without cuBLAS
def test_bare_a_toolkit_that_cannot_link_cublas_is_detected_and_named(tmp_path):
    """'nvcc exists' is not 'this toolkit can build'. runpod/comfyui:1.4.7-cuda13.0 ships nvcc and
    cudart and nothing else; llama-cpp died at CMake's generate step and SageAttention at nvcc exit
    255 seven minutes in, both AFTER the run had spent 131 GB on downloads."""
    nvcc = _stub_nvcc(tmp_path / "bin", rc=1)
    r = _bash('base_env_setup; base_cuda_toolchain; echo OK=$BASE_CUDA_BUILD_OK; printf "%s\\n" "${BASE_WARN[@]}"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_HOME": str(tmp_path / "comfy-base"),
                   "CUDACXX": str(nvcc), "BASE_DRY": "1"})
    out = r.stdout + r.stderr
    assert "OK=0" in out, out[-1200:]
    assert "cuda-libraries-dev" in out, "the operator is not told the one command that fixes it:\n" + out[-1200:]


def test_bare_a_toolkit_that_links_cublas_is_accepted(tmp_path):
    nvcc = _stub_nvcc(tmp_path / "bin", rc=0)
    r = _bash("base_env_setup; base_cuda_toolchain; echo OK=$BASE_CUDA_BUILD_OK",
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_HOME": str(tmp_path / "comfy-base"),
                   "CUDACXX": str(nvcc)})
    assert "OK=1" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-1200:]


def test_bare_no_source_build_starts_when_the_toolchain_cannot_finish_it():
    """Seven minutes of nvcc to reach a link error the probe can predict in two seconds."""
    src = PACKS_SH.read_text(encoding="utf-8")
    body = src[src.index("_base_sage_install()"):]             # 3.7.5: base_build_sageattention = this install, then the proof
    body = body[:body.index("\n}\n")]
    assert "base_cuda_toolchain" in body, "the SageAttention build does not consult the toolchain probe"
    guard = body.index("base_cuda_toolchain")
    assert body.index("git clone") > guard, "the probe must run BEFORE the clone and the build"
