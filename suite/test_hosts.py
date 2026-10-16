"""unit tier: the host layer of 2.2.0. The four knobs (BASE_HOST, BASE_VOLUME, BASE_VOLUME_KIND, BASE_LISTEN), the boot's
host detection and extension seam, the systemd unit, brand.toml, and the driver's provider binding. Nothing here touches
the network, a real machine, or the user's ssh config."""
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py"))


def _bash(snippet, env=None, cwd=None):
    # scrub what a machine's environment would otherwise lend the test: this helper built a raw os.environ, so an
    # ambient UV_CACHE_DIR from a previous base run made test_unit_base_volume_moves_the_home_and_the_caches fail
    # on every live install while passing on every Mac.
    from basetest import _LEAK
    e = {k: v for k, v in os.environ.items() if k not in _LEAK}
    e.update(env or {})
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s' % (BASE, snippet)], capture_output=True, text=True, env=e, cwd=cwd)


def _pid1(tmp_path, text):
    f = tmp_path / "pid1.env"; f.write_text(text); return str(f)


# ---------------------------------------------------------------- the knobs

def test_unit_host_resolves_runpod_from_pid1_env_and_vm_otherwise(tmp_path):
    fake = {"BASE_FAKE_ROOT": str(tmp_path)}
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN K=$BASE_VOLUME_KIND"', env={**fake, "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "RUNPOD_POD_ID=abc\n")})
    assert "H=runpod L=0.0.0.0 K=mount" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN K=$BASE_VOLUME_KIND"', env={**fake, "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "HF_TOKEN=x\n")})
    assert "H=runpod L=0.0.0.0 K=mount" in r.stdout, r.stdout + r.stderr                # a fake pod is RunPod-shaped unless its layout says otherwise
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN K=$BASE_VOLUME_KIND"', env={"BASE_VOLUME": str(tmp_path / "novol")})
    assert "H=vm L=127.0.0.1 K=mount" in r.stdout, r.stdout + r.stderr                  # a real machine with no evidence is a VM


def test_unit_host_reads_the_drivers_host_env_and_an_explicit_setting_wins(tmp_path):
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True)
    (st / "host.env").write_text("BASE_HOST=verda\nBASE_VOLUME=/workspace\n")
    fake = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "HF_TOKEN=x\n")}
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN"', env=fake)
    assert "H=verda L=127.0.0.1" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN K=$BASE_VOLUME_KIND"', env={**fake, "BASE_HOST": "local"})
    assert "H=local L=127.0.0.1 K=dir" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; echo "H=$BASE_HOST L=$BASE_LISTEN"', env={**fake, "BASE_HOST": "runpod", "BASE_LISTEN": "127.0.0.1"})
    assert "H=runpod L=127.0.0.1" in r.stdout, r.stdout + r.stderr                       # an explicit BASE_LISTEN is never overridden
    r = _bash('base_env_setup 2>&1; echo "H=$BASE_HOST"', env={**fake, "BASE_HOST": "mars"})
    assert "not one of" in r.stdout and "H=vm" in r.stdout, r.stdout + r.stderr


def test_unit_base_volume_moves_the_home_and_the_caches(tmp_path):
    vol = tmp_path / "vol"; vol.mkdir()
    r = _bash('base_env_setup; echo "HOME=$BASE_HOME PERSIST=$BASE_PERSIST_ROOT UV=$UV_CACHE_DIR"', env={"BASE_VOLUME": str(vol), "BASE_HOST": "local", "BASE_PERSIST_ROOT_FORCE": ""})
    assert "HOME=%s/comfy-base PERSIST=%s UV=%s/.cache/uv" % (vol, vol, vol) in r.stdout, r.stdout + r.stderr
    r = _bash('echo "V=$BASE_VOLUME"', env={"BASE_VOLUME": "/data/x/"})
    assert "V=/data/x" in r.stdout, r.stdout                                              # a trailing slash is dropped


def test_unit_volume_check_accepts_a_directory_only_on_an_owned_box(tmp_path):
    vol = tmp_path / "vol"; vol.mkdir()
    # a fake root answers for itself; the real predicate is exercised with BASE_FAKE_ROOT empty and a Linux uname faked
    env = {"BASE_VOLUME": str(vol), "BASE_FAKE_UNAME": "Linux", "BASE_DEV_DIR": str(tmp_path / "dev")}
    (tmp_path / "dev").mkdir(); (tmp_path / "dev" / "nvidia0").write_text("")             # looks like a pod
    r = _bash('base_env_setup; _base_volume_ok && echo OK || echo REFUSED', env={**env, "BASE_HOST": "local"})
    assert "OK" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; _base_volume_ok && echo OK || echo REFUSED', env={**env, "BASE_HOST": "vm"})
    assert "REFUSED" in r.stdout, r.stdout + r.stderr                                    # a directory is not a mount on a pod


def test_unit_launch_line_binds_the_hosts_listen_address(tmp_path):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "HF_TOKEN=x\n")}
    snippet = 'base_env_setup; PY=/usr/bin/python3; PORT=8188; ARGS_FILE=/nonexistent; BASE_PERSIST_ROOT=""; _base_start_cmd; echo'
    r = _bash(snippet, env={**env, "BASE_HOST": "verda"})
    assert "--listen 127.0.0.1 --port 8188" in r.stdout, r.stdout + r.stderr
    r = _bash(snippet, env={**env, "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "RUNPOD_POD_ID=abc\n")})
    assert "--listen 0.0.0.0 --port 8188" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- the boot unit and the extension seam

def test_unit_boot_unit_text_names_the_home_the_host_and_the_mount():
    r = _bash('BASE_HOME=/vol/comfy-base BASE_VOLUME=/vol BASE_HOST=verda BASE_LISTEN=127.0.0.1 BASE_VOLUME_KIND=mount; _base_boot_unit_text')
    u = r.stdout
    assert "RequiresMountsFor=/vol" in u and "Environment=BOOT_HOME=/vol/comfy-base" in u and "Environment=BASE_HOST=verda" in u, u + r.stderr
    assert "exec bash /vol/comfy-base/boot.sh" in u and "WantedBy=multi-user.target" in u
    r = _bash('BASE_HOME=/home/me/comfy/comfy-base BASE_VOLUME=/home/me/comfy BASE_HOST=local BASE_LISTEN=127.0.0.1 BASE_VOLUME_KIND=dir; _base_boot_unit_text')
    assert "RequiresMountsFor" not in r.stdout and "BOOT_HOME=/home/me/comfy/comfy-base" in r.stdout


def test_unit_boot_install_writes_the_unit_beside_boot_sh_on_a_vm_and_only_prints_it_on_runpod(tmp_path):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "HF_TOKEN=x\n")}
    r = _bash('base_env_setup; BASE_DRY=0; base_boot_unit; cat "$BASE_HOME/comfy-base-boot.service"', env={**env, "BASE_HOST": "verda"})
    assert "boot unit written" in r.stdout and "Environment=BASE_HOST=verda" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; BASE_DRY=1; base_boot_unit', env={**env, "BASE_FAKE_PID1_ENV": _pid1(tmp_path, "RUNPOD_POD_ID=abc\n")})
    assert "would write" in r.stdout and "BASE_HOST=runpod" in r.stdout, r.stdout + r.stderr


def _boot(tmp_path, ext_where, extra_env=None):
    from basetest import boot_stubs, boot_fake_pod
    root = tmp_path; home = root / "comfy-base"; home.mkdir(parents=True, exist_ok=True)
    (home / "boot.sh").write_text((BASE / "lib" / "boot.sh").read_text()); (home / "lib").mkdir(exist_ok=True)
    (home / "lib" / "85-launch.sh").write_text((BASE / "lib" / "85-launch.sh").read_text())
    marker = root / "ext-ran"
    if ext_where:
        d = root / ext_where; d.mkdir(parents=True, exist_ok=True)
        (d / "10-hello.sh").write_text('echo "boot: hello from an extension"; touch "%s"\n' % marker)
    bin_, log = boot_stubs(root)
    r, text = boot_fake_pod(root, bin_, extra_env)
    if extra_env and extra_env.get("BOOT_HOME"):                                       # the log lands under that home, not <root>/comfy-base
        logs = sorted(pathlib.Path(extra_env["BOOT_HOME"], "state", "logs").glob("boot_*.log"))
        text = logs[-1].read_text() if logs else ""
    return r, text, marker


def test_unit_boot_extensions_are_sourced_from_the_volume_or_beside_boot_sh(tmp_path):
    r, text, marker = _boot(tmp_path / "a", "comfy-base/ext")
    assert marker.exists() and "hello from an extension" in text and "boot: extension" in text, text
    r, text, marker = _boot(tmp_path / "b", None)
    assert not marker.exists() and "no extensions" in text, text
    # a baked image's first boot: BOOT_HOME is the (empty) volume, the seed's own ext/ sits beside the seed's boot.sh
    root = tmp_path / "c"; vol = root / "vol" / "comfy-base"; vol.mkdir(parents=True)
    r, text, marker = _boot(root, "comfy-base/ext", extra_env={"BOOT_HOME": str(vol)})
    assert marker.exists() and "hello from an extension" in text, text


def test_unit_boot_reports_the_host_and_binds_loopback_off_runpod(tmp_path):
    r, text, _ = _boot(tmp_path / "rp", None)
    assert "boot: host runpod · listen 0.0.0.0" in text, text                            # boot_fake_pod sets RUNPOD_POD_ID
    r, text, _ = _boot(tmp_path / "vm", None, extra_env={"RUNPOD_POD_ID": ""})
    assert "boot: host vm · listen 127.0.0.1" in text, text
    st = tmp_path / "vd" / "comfy-base" / "state"; st.mkdir(parents=True); (st / "host.env").write_text("BASE_HOST=verda\nBASE_VOLUME=/workspace\n")
    r, text, _ = _boot(tmp_path / "vd", None, extra_env={"RUNPOD_POD_ID": ""})
    assert "boot: host verda · listen 127.0.0.1" in text, text


# ---------------------------------------------------------------- brand.toml

def test_unit_brand_toml_names_the_host_and_a_package_finds_its_brand(tmp_path, monkeypatch):
    import brand
    b = tmp_path / "brand-b"; (b / "packages" / "x").mkdir(parents=True)
    (b / "brand.toml").write_text('name = "Brand B"\nhost = "verda"   # the venue\n')
    assert brand.host_of(b / "packages" / "x") == "verda" and brand.brand_of(b)["name"] == "Brand B"
    assert brand.host_of(tmp_path / "nowhere", "runpod") == "runpod" and brand.brand_of(tmp_path) == {}
    assert [x["host"] for x in brand.brands_under(tmp_path)] == ["verda"]
    (b / "brand.toml").write_text('host = "mars"\n')
    with pytest.raises(ValueError):
        brand.brand_of(b)
    assert brand.parse_brand_toml('name = "A"\nhost = "crusoe"\n') == {"name": "A", "host": "crusoe"}
    monkeypatch.setitem(sys.modules, "tomllib", None)                                    # the fallback parser, for a Python without tomllib
    assert brand.parse_brand_toml('name = "A" # c\nhost = "local"\nbad line\n') == {"name": "A", "host": "local"}


# ---------------------------------------------------------------- the driver's provider binding

def _podctl():
    if not (BASE / "_build" / "pod" / "podctl.py").exists():
        pytest.skip("no _build/pod/podctl.py: the driver is repo tooling, not in the shipped base")
    spec = importlib.util.spec_from_file_location("podctl", BASE / "_build" / "pod" / "podctl.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_unit_driver_binds_alias_user_key_and_volume_from_the_provider(monkeypatch):
    podctl = _podctl()
    prov = podctl.RunPodProvider(api=object())
    monkeypatch.delenv("PODCTL_HOST", raising=False)
    podctl.set_provider(prov, env={})
    assert (podctl.SSH_ALIAS, podctl.SSH_USER, podctl.VOLUME_ROOT) == ("runpod", "root", "/workspace")
    # 2.5.5: the lease is the MACHINE's, so it is under BASE_LOCAL_STATE and NOT derived from the volume.
    # On the shared root the volume is one store every machine mounts, and a lease there is a global mutex.
    assert podctl.LEASE_PATH == "/var/lib/comfy-base-machine/gpu.lease"
    assert podctl.LEGACY_LEASE_PATH == "/workspace/comfy-base/state/gpu.lease"   # read for a note, never obeyed
    assert "/workspace" not in podctl.LEASE_PATH, "a lease on the volume is shared by every machine that mounts it"
    podctl.set_provider(prov, env={"BASE_LOCAL_STATE": "/var/lib/elsewhere"})
    assert podctl.LEASE_PATH == "/var/lib/elsewhere/gpu.lease"
    podctl.set_provider(prov, env={})
    assert podctl.pkgs_dir() == "/workspace/packages" and prov.host_env() == "BASE_HOST=runpod\nBASE_VOLUME=/workspace\n"
    podctl.set_provider(prov, env={"PODCTL_HOST": "runpod-abc"})
    assert podctl.SSH_ALIAS == "runpod-abc"                                                # a session's own block still wins
    argv = podctl.tunnel_argv([8188], host="1.2.3.4", port=22, key="/k", user="ubuntu")
    assert argv[-1] == "ubuntu@1.2.3.4" and argv[-2] == "22"


def test_unit_provider_choice_honours_the_flag_the_env_and_refuses_two_brands(tmp_path):
    podctl = _podctl()
    ns = podctl.build_parser().parse_args(["--provider", "verda", "pods"]); assert ns.provider == "verda"
    ns = podctl.build_parser().parse_args(["stop", "x", "--provider", "crusoe"]); assert ns.provider_after == "crusoe"
    root = tmp_path; (root / "base" / "comfyui-base").mkdir(parents=True); (root / "base" / "comfyui-base" / "base.sh").write_text("")
    for b, h in (("brand-a", "runpod"), ("brand-b", "verda")):
        (root / b).mkdir(); (root / b / "brand.toml").write_text('host = "%s"\n' % h)
    with pytest.raises(podctl.PodctlError):
        podctl.provider_for(None, base_dir=root / "base" / "comfyui-base", env={})
    with pytest.raises(podctl.PodctlError):
        podctl.provider_for("mars", base_dir=BASE, env={})
    assert podctl.repo_root(BASE) is None or (podctl.repo_root(BASE) / "base" / "comfyui-base").resolve() == BASE.resolve()


def test_unit_the_shipped_tree_carries_every_host_and_the_manifest_covers_them():
    if not (BASE / "_build").exists():
        pytest.skip("repo tooling")
    for host in ("runpod", "verda", "crusoe", "local"):
        assert (BASE / "hosts" / host / "README.md").exists(), host
    for f in ("hosts/__init__.py", "hosts/runpod/provider.py", "hosts/verda/provider.py", "hosts/verda/startup.sh", "hosts/crusoe/provider.py", "hosts/crusoe/startup.sh", "hosts/local/install-local.sh"):
        assert (BASE / f).exists(), f
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--base", "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    listed = (BASE / "MANIFEST.sha256").read_text()
    assert "hosts/runpod/provider.py" in listed and "hosts/verda/startup.sh" in listed and "hosts/__init__.py" in listed
    for p in (BASE / "hosts").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".sh", ".md"):
            assert chr(0x2014) not in p.read_text(encoding="utf-8"), "%s carries an em dash" % p
    for p in (BASE / "hosts").rglob("*.sh"):
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0, p


def test_unit_a_package_without_a_workflow_ships_none(tmp_path):
    if not (BASE / "_build").exists():
        pytest.skip("repo tooling")
    pkg = tmp_path / "stub-x"; pkg.mkdir()
    (pkg / "stub-x-script.sh").write_text('#!/usr/bin/env bash\nPKG_NAME="Stub X"; PKG_ID="stubx"; PKG_VERSION="0.0.0"; BASE_MIN="2.2.0"\nWF_NAME=""; PKG_NO_SUITE=1\nPACKS=(); MODELS=()\n')
    (pkg / "pytest.ini").write_text("[pytest]\n")
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    import zipfile
    names = zipfile.ZipFile(pkg / "stub-x-runpod.zip").namelist()
    assert "stub-x-script.sh" in names and not any(n.endswith(".json") for n in names), names
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--host", str(pkg)], capture_output=True, text=True)
    assert r.stdout.strip() == "runpod", r.stdout + r.stderr


def test_unit_the_testbed_port_file_picks_the_server(tmp_path):
    here = tmp_path / "base"; base = here / "comfyui-base"; base.mkdir(parents=True)
    for f in ("base.sh", "VERSION"):
        (base / f).write_text((BASE / f).read_text())
    (base / "lib").mkdir(); [ (base / "lib" / p.name).write_text(p.read_text()) for p in (BASE / "lib").glob("*.sh") ]
    (base / "py").mkdir()
    (here / "testbed.sh").write_text("#!/bin/bash\n"); (here / "testbed" ).mkdir(); (here / "testbed" / "main.py").write_text("")
    (here / ".testbed-server.port").write_text("8299\n")
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "curl").write_text('#!/bin/bash\ncase "$*" in *8299*) exit 0;; esac; exit 1\n'); (bin_ / "curl").chmod(0o755)
    env = {**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_NODE_SRC": "", "BASE_SERVER": ""}
    r = subprocess.run(["bash", "-c", 'source "%s/base.sh"; BASE_NODE_SRC=""; BASE_SERVER=""; _base_use_testbed; echo "S=$BASE_SERVER"' % base], capture_output=True, text=True, env=env)
    assert "S=127.0.0.1:8299" in r.stdout, r.stdout + r.stderr


def test_unit_the_testbed_is_found_inside_the_base_too(tmp_path):
    """2.6.0: testbed.sh ships in the ComfyUI Base repository, so the tree is <base>/testbed — not only
    base/testbed beside base/comfyui-base, which is what a brand repository has. The sibling test above
    covers the brand layout; this one covers a standalone checkout, because a clone of this repository
    alone used to skip the comfyui tier forever."""
    base = tmp_path / "comfyui-base"; base.mkdir(parents=True)
    for f in ("base.sh", "VERSION"):
        (base / f).write_text((BASE / f).read_text())
    (base / "lib").mkdir(); [ (base / "lib" / q.name).write_text(q.read_text()) for q in (BASE / "lib").glob("*.sh") ]
    (base / "py").mkdir()
    # everything in the base itself, not one level up
    (base / "testbed.sh").write_text("#!/bin/bash\n"); (base / "testbed").mkdir(); (base / "testbed" / "main.py").write_text("")
    (base / ".testbed-server.port").write_text("8399\n")
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "curl").write_text('#!/bin/bash\ncase "$*" in *8399*) exit 0;; esac; exit 1\n'); (bin_ / "curl").chmod(0o755)
    env = {**os.environ, "PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_NODE_SRC": "", "BASE_SERVER": ""}
    r = subprocess.run(["bash", "-c", 'source "%s/base.sh"; BASE_NODE_SRC=""; BASE_SERVER=""; _base_use_testbed; echo "S=$BASE_SERVER"; echo "N=$BASE_NODE_SRC"' % base], capture_output=True, text=True, env=env)
    assert "S=127.0.0.1:8399" in r.stdout, r.stdout + r.stderr
    assert "N=%s" % (base / "testbed").resolve() in r.stdout, r.stdout + r.stderr


def test_unit_a_shared_root_makes_a_separate_library_redundant(tmp_path):
    """2.5.2, measured on a live machine: with BASE_VOLUME_SHARED the whole root IS the store, so a BASE_LIBRARY
    pointing at the same store mounts it twice. The run then warns about "another ComfyUI tree" which is its own
    seen through the second path, and --output-directory goes to the other mount. They are alternatives."""
    vol = tmp_path / "store"; vol.mkdir()
    lib = tmp_path / "mnt"; lib.mkdir()
    r = _bash('base_env_setup 2>&1; echo "LIB=[$BASE_LIBRARY] SHARED=$BASE_VOLUME_SHARED"',
              env={"BASE_VOLUME": str(vol), "BASE_HOST": "local", "BASE_VOLUME_SHARED": "1", "BASE_LIBRARY": str(lib)})
    assert "LIB=[] SHARED=1" in r.stdout, r.stdout + r.stderr
    assert "redundant and is ignored" in r.stdout, r.stdout
    # and without a shared root the library is untouched, because that is the narrower shape and still valid
    r = _bash('base_env_setup 2>&1; echo "LIB=[$BASE_LIBRARY]"',
              env={"BASE_VOLUME": str(vol), "BASE_HOST": "local", "BASE_VOLUME_SHARED": "0", "BASE_LIBRARY": str(lib)})
    assert "LIB=[%s]" % lib in r.stdout, r.stdout + r.stderr


def test_unit_jupyter_generates_one_token_keeps_it_private_and_never_goes_tokenless(tmp_path):
    """2.5.6: a customer needs the terminal to upload a LoRA, and every comparable product hands them one with a
    token printed in the startup log. Tokenless is still refused, which was the whole point of 2.1.0; what changes
    is that the base GENERATES the token rather than leaving the terminal dark until the customer invents one.
    This is Jupyter's own model restored, and a per-machine random token beats the shared default password some
    public templates fall back to."""
    home = tmp_path / "comfy-base"; (home / "tools" / "bin").mkdir(parents=True); (home / "state").mkdir()
    jl = home / "tools" / "bin" / "jupyter-lab"; jl.write_text("#!/bin/sh\nexit 0\n"); jl.chmod(0o755)
    tf = home / "state" / "tokens.env"
    # boot.sh is standalone: base.sh does not source it, so the helper that sources base.sh cannot see it
    def run():
        e = {k: v for k, v in os.environ.items() if k not in ("JUPYTER_TOKEN", "JUPYTER_PASSWORD")}
        e.update(BOOT_HOME=str(home), BASE_LISTEN="127.0.0.1")
        return subprocess.run(["bash", "-c", 'source "%s/lib/boot.sh"; boot_jupyter' % BASE],
                              capture_output=True, text=True, env=e, cwd=str(tmp_path))
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [l.split("=", 1)[1] for l in tf.read_text().splitlines() if l.startswith("JUPYTER_TOKEN=")]
    assert len(saved) == 1 and len(saved[0]) >= 32, saved
    assert oct(tf.stat().st_mode)[-3:] == "600", "the token file must be owner-only"
    # and it is NEVER printed. The boot log is a file on the volume; with a shared store that volume is mounted by
    # every machine, so a generated token in it is readable by every session on every one of them. The suite has
    # forbidden logging an operator-supplied token since 2.1.0 and a generated one is the same secret — the house
    # rule is that a rule applies to ALL instances of its pattern, not the instance that prompted it. What the log
    # carries instead is how to fetch it.
    assert saved[0] not in r.stdout, "the generated token was printed into the boot log"
    assert "ssh -L 8888:127.0.0.1:8888" in r.stdout and "podctl jupyter" in r.stdout, r.stdout
    # a second boot REUSES it and does not append: caught doing exactly that before the fix
    r2 = run()
    again = [l for l in tf.read_text().splitlines() if l.startswith("JUPYTER_TOKEN=")]
    assert len(again) == 1 and again[0].endswith(saved[0]), again
    # and it binds what BASE_LISTEN says, which is loopback on every host but RunPod
    assert "--ip" in (BASE / "lib" / "boot.sh").read_text()
    # 3.9.0: and on a PUBLIC bind too. 2.1.0 refused it because nobody had anywhere to read a generated token; now the
    # podctl verbs read it over ssh and, on RunPod, ONE line on the CONTAINER log (fd 3, the start command's own stdout:
    # RunPod's Logs tab, the owner's) names it for a pod with no ssh key. Never the boot log on the volume.
    pub = tmp_path / "public"; (pub / "comfy-base" / "tools" / "bin").mkdir(parents=True)
    (pub / "comfy-base" / "state").mkdir()
    jl2 = pub / "comfy-base" / "tools" / "bin" / "jupyter-lab"; jl2.write_text("#!/bin/sh\nexit 0\n"); jl2.chmod(0o755)
    e = {k: v for k, v in os.environ.items() if k not in ("JUPYTER_TOKEN", "JUPYTER_PASSWORD")}
    e.update(BOOT_HOME=str(pub / "comfy-base"), BASE_LISTEN="0.0.0.0", BASE_HOST="runpod", RUNPOD_POD_ID="podabc")
    console = tmp_path / "console.log"
    r3 = subprocess.run(["bash", "-c", 'source "%s/lib/boot.sh"; boot_jupyter 3>"%s"' % (BASE, console)],
                        capture_output=True, text=True, env=e, cwd=str(tmp_path))
    ptf = pub / "comfy-base" / "state" / "tokens.env"
    assert r3.returncode == 0 and "JupyterLab starting on 8888" in r3.stdout and ptf.exists(), r3.stdout + r3.stderr
    ptok = [l.split("=", 1)[1] for l in ptf.read_text().splitlines() if l.startswith("JUPYTER_TOKEN=")][0]
    assert oct(ptf.stat().st_mode)[-3:] == "600" and ptok not in r3.stdout + r3.stderr, "never in the boot log"
    assert console.read_text() == "Jupyter Terminal token: %s   (open https://podabc-8888.proxy.runpod.net/lab?token=%s)\n" % (ptok, ptok)
    # the pod's OWN token (podctl create sets one per pod) is used and never repeated, not even on the console
    console.write_text("")
    e2 = dict(e, JUPYTER_TOKEN="pod-own-token-value")
    r4 = subprocess.run(["bash", "-c", 'source "%s/lib/boot.sh"; boot_jupyter 3>"%s"' % (BASE, console)],
                        capture_output=True, text=True, env=e2, cwd=str(tmp_path))
    assert r4.returncode == 0 and console.read_text() == "" and "pod-own-token-value" not in r4.stdout + r4.stderr
    # and off RunPod (a VM's journald is not an owner-only page), the console gets nothing at all
    console.write_text("")
    r5 = subprocess.run(["bash", "-c", 'source "%s/lib/boot.sh"; boot_jupyter 3>"%s"' % (BASE, console)],
                        capture_output=True, text=True, env=dict(e, BASE_HOST="vm"), cwd=str(tmp_path))
    assert r5.returncode == 0 and console.read_text() == ""
