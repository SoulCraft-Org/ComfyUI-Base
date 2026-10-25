"""unit tier, 3.24.0: the template image's head start.

The template image (runpod-template's ghcr comfyui-base) carries a seed built at the volume's own paths under
/opt/comfy-seed: ComfyUI, a venv, the managed CPython it boots from, and uv's cache with every wheel of that venv
unpacked. A podctl pod never copies the seed, so before 3.24.0 its venv downloaded and unpacked all 4.1 GB again on the
image that already held them. A venv built on such a machine now reads those wheels, and the picks, the newest pass and
everything else about the install are as before. Every test runs the real library against a throwaway tree."""
import os, pathlib, subprocess, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "UV_"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e)


def _seeded(tmp_path, cache=True):
    """A volume with a cold uv cache, and an image seed that mirrors it; the snippet prefix that names both."""
    vol = tmp_path / "vol"; (vol / "ComfyUI").mkdir(parents=True); (vol / ".cache" / "uv").mkdir(parents=True)
    seed = tmp_path / "seed"; (seed / ".cache" / "uv").mkdir(parents=True)
    if cache:
        (seed / ".cache" / "uv" / "archive-v0" / "Zp3x").mkdir(parents=True)
    pre = ('BASE_PERSIST_ROOT="%s"; BASE_IMAGE_SEED="%s"; UV_CACHE_DIR="%s"; VENV="%s"; '
           % (vol, seed, vol / ".cache" / "uv", vol / "ComfyUI" / ".venv-cu130"))
    return vol, seed, pre


def test_unit_a_venv_built_on_the_image_reads_the_images_wheels(tmp_path):
    vol, seed, pre = _seeded(tmp_path)
    r = _bash(pre + '_base_venv_image_cache; echo "UV=$UV_CACHE_DIR LINK=${UV_LINK_MODE:-default}"')
    assert r.returncode == 0, r.stderr
    assert "UV=%s LINK=default" % (seed / ".cache" / "uv") in r.stdout, r.stdout + r.stderr      # one filesystem: uv hardlinks
    assert "the image's (%s)" % (seed / ".cache" / "uv") in r.stdout and "linked into the venv, not downloaded" in r.stdout, r.stdout


def test_unit_across_filesystems_the_images_wheels_are_copied_without_a_hardlink_attempt(tmp_path):
    # a network volume, or a volume disk beside the container disk: a hardlink cannot cross, so uv is told to copy
    vol, seed, pre = _seeded(tmp_path)
    r = _bash(pre + '_base_dev(){ case "$1" in "%s"*) echo 7;; *) echo 9;; esac; }; '
              '_base_venv_image_cache; echo "UV=$UV_CACHE_DIR LINK=${UV_LINK_MODE:-default}"' % seed)
    assert "UV=%s LINK=copy" % (seed / ".cache" / "uv") in r.stdout and "copied into the venv" in r.stdout, r.stdout + r.stderr


def test_unit_no_image_cache_leaves_the_volumes_own_cache_alone(tmp_path):
    vol, seed, pre = _seeded(tmp_path, cache=False)                       # an image without the cache (built before 3.24.0)
    r = _bash(pre + '_base_venv_image_cache; echo "UV=$UV_CACHE_DIR LINK=${UV_LINK_MODE:-default}"')
    assert "UV=%s LINK=default" % (vol / ".cache" / "uv") in r.stdout and "the image's" not in r.stdout, r.stdout + r.stderr
    # a cache that is not on the volume has no twin in the seed: it is never swapped
    vol, seed, pre = _seeded(tmp_path / "b")
    r = _bash(pre + 'UV_CACHE_DIR=/elsewhere/uv; _base_venv_image_cache; echo "UV=$UV_CACHE_DIR"')
    assert "UV=/elsewhere/uv" in r.stdout and "the image's" not in r.stdout, r.stdout + r.stderr
    # a second build in the same run: already reading the image's, nothing to say
    r = _bash(pre + '_base_venv_image_cache >/dev/null; _base_venv_image_cache; echo "UV=$UV_CACHE_DIR"')
    assert r.stdout.strip() == "UV=%s" % (tmp_path / "b" / "seed" / ".cache" / "uv"), r.stdout + r.stderr


def test_unit_only_a_build_reads_the_image_cache_and_before_it_installs_anything():
    # a venv that passes its probe is reused against the volume's own cache: the swap belongs to the build alone
    src = (BASE / "lib" / "30-venv.sh").read_text(encoding="utf-8")
    build = src[src.index("_base_venv_build(){"):src.index("_base_torch_family_upgrade(){")]
    assert build.index("_base_venv_image_cache") < build.index("uv venv -q --python")
    stage = src[src.index("base_venv(){"):src.index("_base_venv_probe_ro(){")]
    assert "_base_venv_image_cache" not in stage


def test_unit_the_images_python_is_copied_onto_a_volume_that_has_none(tmp_path):
    vol, seed, pre = _seeded(tmp_path)
    img = seed / "comfy-base" / "python"
    for d in ("cpython-3.14.8-linux-x86_64-gnu", "cpython-3.14.8+freethreaded-linux-x86_64-gnu", "cpython-3.13.9-linux-x86_64-gnu"):
        (img / d / "bin").mkdir(parents=True); (img / d / "bin" / "python3").write_text("")
    (img / "cpython-3.14-linux-x86_64-gnu").symlink_to("cpython-3.14.8-linux-x86_64-gnu")    # uv's minor-version link
    dst = vol / "comfy-base" / "python"
    r = _bash(pre + 'UV_PYTHON_INSTALL_DIR="%s"; _base_image_python 3.14' % dst)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in dst.iterdir()) == ["cpython-3.14-linux-x86_64-gnu", "cpython-3.14.8-linux-x86_64-gnu"], list(dst.iterdir())
    assert (dst / "cpython-3.14-linux-x86_64-gnu").is_symlink() and (dst / "cpython-3.14-linux-x86_64-gnu" / "bin" / "python3").exists()
    assert "the image's own cpython-3.14.8-linux-x86_64-gnu" in r.stdout, r.stdout
    # the volume already has a 3.14: uv's own upgrade decides, nothing is copied
    (dst / "cpython-3.14-linux-x86_64-gnu").unlink(); (dst / "cpython-3.14.8-linux-x86_64-gnu").rename(dst / "cpython-3.14.9-linux-x86_64-gnu")
    r = _bash(pre + 'UV_PYTHON_INSTALL_DIR="%s"; _base_image_python 3.14' % dst)
    assert sorted(p.name for p in dst.iterdir()) == ["cpython-3.14.9-linux-x86_64-gnu"] and "image's own" not in r.stdout, r.stdout


def test_unit_consolidate_neither_reports_nor_sizes_the_images_seed(tmp_path):
    # on the image, CONSOLIDATE warned about the seed's ComfyUI and its venv as a second install, and ran du over both
    root = tmp_path / "root"
    (root / "ComfyUI" / "custom_nodes").mkdir(parents=True); (root / "ComfyUI" / "main.py").write_text("")
    for t in (root / "opt" / "comfy-seed" / "ComfyUI", root / "old" / "ComfyUI"):
        (t / "comfy").mkdir(parents=True); (t / "main.py").write_text(""); (t / ".venv-cu130" / "bin").mkdir(parents=True)
    r = _bash('base_env_setup; base_discover quiet; _base_pack_rows(){ :; }; base_consolidate',
              env={"BASE_FAKE_ROOT": str(root), "BASE_IMAGE_SEED": str(root / "opt" / "comfy-seed")})
    out = r.stdout + r.stderr
    assert "another ComfyUI tree: %s" % (root / "old" / "ComfyUI") in out, out
    assert "other venv: %s" % (root / "old" / "ComfyUI" / ".venv-cu130") in out, out
    assert "comfy-seed" not in out, out
