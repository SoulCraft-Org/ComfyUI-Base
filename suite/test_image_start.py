"""unit tier, 3.25.0: the template image's head start.

The template image (runpod-template's ghcr comfyui-base) carries a seed built at the volume's own paths under
/opt/comfy-seed: ComfyUI, a venv, the managed CPython it boots from, and uv's cache with every wheel of that venv
unpacked. A podctl pod never copies the seed, so before 3.25.0 its venv downloaded and unpacked all 4.1 GB again on the
image that already held them. A venv built on such a machine now reads those wheels, and the picks, the newest pass and
everything else about the install are as before. Every test runs the real library against a throwaway tree."""
import functools, http.server, os, pathlib, shutil, subprocess, sys, threading, zipfile, pytest
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
    vol, seed, pre = _seeded(tmp_path, cache=False)                       # an image without the cache (built before 3.25.0)
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


@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv (CI's verify job has it)")
def test_unit_the_image_cache_resolves_the_same_versions_as_a_cold_cache_never_older(tmp_path):
    """An install change must never change the render, so the image's cache may spare downloads and nothing else. Its
    danger is the index LISTINGS it holds from the build day: an index that sent a long max-age has uv trust them, and
    then a package released since resolves to its old version (shown here: the unguarded cache installs demo 1.0).
    _base_venv_image_cache drops those listings, so a build on the image freezes exactly what a cold cache does."""
    idx = tmp_path / "idx"; (idx / "simple" / "demo").mkdir(parents=True); (idx / "files").mkdir()

    def wheel(v):
        n = "demo-%s-py3-none-any.whl" % v; di = "demo-%s.dist-info" % v
        with zipfile.ZipFile(idx / "files" / n, "w") as z:
            z.writestr("demo.py", "V = %r\n" % v)
            z.writestr(di + "/METADATA", "Metadata-Version: 2.1\nName: demo\nVersion: %s\n" % v)
            z.writestr(di + "/WHEEL", "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            z.writestr(di + "/RECORD", "")
        return n

    def listing(*vs):
        (idx / "simple" / "demo" / "index.html").write_text("".join('<a href="/files/%s">%s</a>\n' % (wheel(v), wheel(v)) for v in vs))

    class Index(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "max-age=31536000, public"); super().end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Index, directory=str(idx)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/simple" % srv.server_address[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "BASE_"))}

    def uv(cache, *a):
        r = subprocess.run(["uv", *a], capture_output=True, text=True, env=dict(env, UV_CACHE_DIR=str(cache)))
        assert r.returncode == 0, (a, r.stderr[-2000:])
        return r.stdout

    def venv(v):
        # the stdlib's venv from the interpreter running the suite: CI runs pytest from uv's temporary build env, and
        # `uv venv --python <that env's python>` refuses it; `-m venv` bases the new venv on the real interpreter behind it
        r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(v)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]

    def build(name, cache):
        v = tmp_path / name; venv(v)
        uv(cache, "pip", "install", "-q", "--python", str(v / "bin" / "python"), "--index-url", url, "demo")
        return uv(cache, "pip", "freeze", "--python", str(v / "bin" / "python")).strip()

    try:
        seed = tmp_path / "seed" / ".cache" / "uv"
        listing("1.0"); build("image-build", seed)                            # the image's build day
        listing("1.0", "2.0")                                                 # a release since then
        stale = tmp_path / "stale"; shutil.copytree(seed, stale, symlinks=True)
        assert build("unguarded", stale) == "demo==1.0"                      # the danger, shown
        cold = build("cold", tmp_path / "cold-cache")                       # its own cache, never the venv's folder
        assert cold == "demo==2.0"
        vol = tmp_path / "vol"; (vol / ".cache" / "uv").mkdir(parents=True); (vol / "ComfyUI").mkdir()
        g = tmp_path / "guarded"; venv(g)
        r = subprocess.run(["bash", "-c", 'source "%s/base.sh"; BASE_PERSIST_ROOT="%s"; BASE_IMAGE_SEED="%s"; UV_CACHE_DIR="%s/.cache/uv"; VENV="%s/ComfyUI/.venv"; '
                            '_base_venv_image_cache >/dev/null; uv pip install -q --python "%s/bin/python" --index-url %s demo && uv pip freeze --python "%s/bin/python"'
                            % (BASE, vol, tmp_path / "seed", vol, vol, g, url, g)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == cold, (r.stdout, cold)                    # parity: the freeze diff is empty
        assert list(seed.glob("archive-v0/*")), list(seed.iterdir())          # the unpacked wheels stayed: they spare the download
    finally:
        srv.shutdown(); srv.server_close()
