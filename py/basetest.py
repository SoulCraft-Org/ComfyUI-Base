"""basetest — the pytest plugin every package suite (and the base's own) loads with `-p basetest`.

Helpers: code_only, fnbody, load_package, loader_cats, active_loaders, manifest_covers_active_loaders,
no_shared_basename, script_syntax_ok, fake_pod, run_script, tree_hash. Plus a per-tier summary table.
"""
import collections
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

BASE_LIB = pathlib.Path(os.environ.get("BASE_LIB") or pathlib.Path(__file__).resolve().parents[1])
MODEL_EXTS = (".safetensors", ".pt", ".pth", ".gguf", ".ckpt", ".bin", ".onnx")
_LEAK = {"HF_TOKEN", "HF_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_API_KEY", "COMFY_DIR", "BASE_NODE_SRC", "BASE_SERVER", "BASE_SEARCH_ROOTS", "BASE_YES", "BASE_RESTART", "BASE_DRY",
         "BASE_FAKE_ROOT", "BASE_NO_NET", "BASE_FAKE_FREE_GB", "BASE_FAKE_DL", "BASE_PERSIST_ROOT_FORCE", "BASE_LATEST", "BASE_DECLARE_ONLY",
         "BASE_FAKE_PID1_ENV", "BASE_FAKE_IMAGE_ROOT",
         "BASE_HOST", "BASE_VOLUME", "BASE_VOLUME_KIND", "BASE_LISTEN", "BASE_LIBRARY", "PODCTL_PROVIDER", "PODCTL_HOST",
         # the base DERIVES these from BASE_VOLUME with ${X:-default}, so an ambient value wins over the test's
         # volume and the test then measures the machine rather than what it set up. They only leak on a machine,
         # where a previous base run exported them, which is why this passed on every Mac and failed on the first
         # live install (2.4.0).
         "UV_CACHE_DIR", "PIP_CACHE_DIR", "HF_HOME", "HF_XET_CACHE"}


def code_only(text):
    """Drop full-line comments and trailing '  # ...' comments so a word in a comment never satisfies a test."""
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        out.append(re.sub(r"\s{2,}#.*$", "", ln))
    return "\n".join(out)


def fnbody(src, name):
    """The text of a bash function from its `name(){` line to the next top-level function definition."""
    m = re.search(r"^%s\(\)\s*\{" % re.escape(name), src, re.M)
    if not m:
        raise KeyError(name)
    rest = src[m.end():]
    nxt = re.search(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _declare(script):
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env={**{k: v for k, v in os.environ.items() if k not in _LEAK},
                       "BASE_DECLARE_ONLY": "1", "COMFY_BASE": str(BASE_LIB)})
    packs = [ln[8:].split("|") for ln in r.stdout.splitlines() if ln.startswith("PACKROW ")]
    models = [ln[9:].split("|") for ln in r.stdout.splitlines() if ln.startswith("MODELROW ")]
    later = [ln[11:] for ln in r.stdout.splitlines() if ln.startswith("MODELLATER ")]
    return r, packs, models, later


def load_package(pkg_dir):
    """Everything a suite needs to know about a package: parsed through the base, not re-implemented."""
    pkg_dir = pathlib.Path(pkg_dir)
    scripts = sorted(pkg_dir.glob("*-script.sh"))
    if not scripts:
        raise FileNotFoundError("no '*-script.sh' in %s" % pkg_dir)
    script = scripts[0]
    text = script.read_text(encoding="utf-8")
    r, packs, models, later = _declare(script)
    if r.returncode != 0:
        raise RuntimeError("declarations invalid:\n" + r.stderr)
    def grab(var, default=""):
        m = re.search(r'(?:^|;)\s*%s="?([^";\n]*)"?' % var, text, re.M)   # declarations may share a line, semicolon-separated
        return m.group(1) if m else default
    wf_name = grab("WF_NAME")
    wf = json.loads((pkg_dir / wf_name).read_text(encoding="utf-8")) if wf_name and (pkg_dir / wf_name).exists() else None
    sup = re.search(r"^SUPERSEDED=\(([^)]*)\)", text, re.M)
    lcats = [ln.strip().strip('"') for ln in re.findall(r'"([A-Za-z0-9_ ()]+\|[A-Za-z0-9_]+)"', fnbody_or_all(text, "LOADER_CATS"))]
    return types.SimpleNamespace(
        dir=pkg_dir, script=script, text=text, name=grab("PKG_NAME"), id=grab("PKG_ID"), version=grab("PKG_VERSION"),
        wf_name=wf_name, wf=wf, wf_version_key=grab("WF_VERSION_KEY", "package_version"),
        packs=[dict(zip(("dir", "url", "sha", "cnr_id", "why", "branch"), p + [""] * (6 - len(p)))) for p in packs],
        models=[dict(zip(("category", "family", "purpose", "file", "url", "bytes", "note", "alts"), m + [""] * (8 - len(m)))) for m in models],
        superseded=[t.strip("\"'") for t in sup.group(1).split()] if sup else [], loader_cats_extra=lcats,
        models_later=later)   # a package may quote its globs


def fnbody_or_all(text, var):
    m = re.search(r"^%s=\((.*?)\)" % var, text, re.M | re.S)
    return m.group(1) if m else ""


def base_packs():
    """The base's own BASE_PACKS rows (lib/40-packs.sh), in the shape load_package().packs uses."""
    src = (BASE_LIB / "lib" / "40-packs.sh").read_text(encoding="utf-8")
    body = re.search(r"^BASE_PACKS=\((.*?)^\)", src, re.M | re.S).group(1)
    return [dict(zip(("dir", "url", "sha", "cnr_id", "why", "branch"), (r.split("|") + [""] * 6)[:6])) for r in re.findall(r'"([^"]+)"', body)]


def loader_cats(pkg=None):
    """node type -> library category: the base's BASE_LOADER_CATS plus the package's LOADER_CATS."""
    src = (BASE_LIB / "lib" / "60-sync.sh").read_text(encoding="utf-8")
    body = re.search(r"^BASE_LOADER_CATS=\((.*?)^\)", src, re.M | re.S).group(1)
    cats = dict(pair.split("|", 1) for pair in re.findall(r'"([^"]+)"', body))
    if pkg is not None:
        for row in getattr(pkg, "loader_cats_extra", []):
            t, c = row.split("|", 1)
            cats[t] = c
    return cats


def active_loaders(wf, cats):
    """{(category, lower basename)} for every ACTIVE loader value: mode 0 nodes (root + subgraph definitions) and ON Power Lora rows."""
    out = set()
    def scan(nodes):
        for n in nodes:
            active = n.get("mode", 0) == 0
            t = str(n.get("type"))
            wv = n.get("widgets_values")
            items = wv if isinstance(wv, list) else list(wv.values()) if isinstance(wv, dict) else []
            for v in items:
                if isinstance(v, dict) and isinstance(v.get("lora"), str) and v["lora"].lower().endswith(MODEL_EXTS):
                    if active and v.get("on"):
                        out.add(("loras", os.path.basename(v["lora"].replace("\\", "/")).lower()))
                elif isinstance(v, str) and v.lower().endswith(MODEL_EXTS) and active and t in cats:
                    out.add((cats[t], os.path.basename(v.replace("\\", "/").replace("[local] ", "")).lower()))
    scan(wf.get("nodes", []))
    for sg in wf.get("definitions", {}).get("subgraphs", []):
        scan(sg.get("nodes", []))
    return out


def manifest_covers_active_loaders(pkg):
    """-> the (category, basename) pairs the shipped workflow loads while active that no MODELS row covers."""
    if pkg.wf is None:
        return []
    rows = {(m["category"], m["file"].lower()) for m in pkg.models}
    names = {m["file"].lower() for m in pkg.models} | {a.lower() for m in pkg.models for a in m["alts"].split()}
    missing = []
    for cat, base in sorted(active_loaders(pkg.wf, loader_cats(pkg))):
        if (cat, base) not in rows and base not in names:
            missing.append((cat, base))
    return missing


def no_shared_basename(rows):
    """-> basenames that appear in more than one row (a package must not declare one file twice)."""
    seen = collections.Counter(m["file"].lower() for m in rows)
    return sorted(k for k, v in seen.items() if v > 1)


def script_syntax_ok(path):
    return subprocess.run(["bash", "-n", str(path)]).returncode == 0


def brand_families(path):
    """The brand's families.txt beside its brand.toml (one family per line, # comments), for a path inside that brand:
    a set of spellings, or None when the path is not under a brand folder that ships one. The base itself lists no
    families (2.3.0): one spelling per family across a brand's packages is the brand's rule, checked from its repository."""
    sys.path.insert(0, str(BASE_LIB / "py"))
    import brand as _brand
    b = _brand.brand_dir_of(path)
    if b is None or not (b / "families.txt").is_file():
        return None
    return {ln.strip() for ln in (b / "families.txt").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def converted_packages(root):
    """Package dirs under root whose script sources the base (contains base_main), in either shape py/pkgdirs.py knows:
    <root>/<brand>/packages/<name>/ beside a base submodule, or <root>/<name>/ side by side (a workspace of one repository
    per package, a test's tmp tree, an extracted zip). The base's own dir is not a package."""
    here = str(pathlib.Path(__file__).resolve().parent)       # pkgdirs.py ships beside this file
    if here not in sys.path:
        sys.path.insert(0, here)
    import pkgdirs                             # 3.6.0: the one package-discovery rule, shared with every other walker
    out = []
    for d in pkgdirs.package_dirs(root, BASE_LIB):
        s = d / (d.name + "-script.sh")
        if "base_main" in s.read_text(encoding="utf-8", errors="replace"):
            out.append(d)
    return out


FRONTEND_ONLY_TYPES = {"Note", "MarkdownNote", "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)", "Label (rgthree)", "Bookmark (rgthree)",
                       "PrimitiveNode", "Reroute"}


def type_index(node_src):
    """node type -> "ComfyUI" or the custom_nodes/<dir> that declares it, by reading the SOURCE of a ComfyUI tree (the testbed
    or the pod). Four declaration forms: NODE_CLASS_MAPPINGS = {"X": Cls}, V3 schemas (node_id="X"), rgthree's
    NAME = get_name("X") (-> "X (rgthree)"), and class names. A suite asks it: which pack does this graph's node come from,
    and is that pack one this package or the base installs."""
    idx = {}
    roots = [(node_src, "ComfyUI")]
    cn = os.path.join(node_src, "custom_nodes")
    if os.path.isdir(cn):
        roots += [(os.path.join(cn, d), d) for d in sorted(os.listdir(cn)) if os.path.isdir(os.path.join(cn, d))]
    for root, name in roots:
        skip = {"custom_nodes"} if name == "ComfyUI" else set()
        for dp, ds, fs in os.walk(root):
            ds[:] = [d for d in ds if d not in skip and not d.startswith(".") and d not in ("node_modules", "web", "tests", "docs", "__pycache__", "models", "example_workflows", "examples")]
            for f in fs:
                if not f.endswith(".py"):
                    continue
                try:
                    txt = Path(os.path.join(dp, f)).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for t in re.findall(r'["\']([A-Za-z0-9_ ()|.-]{3,60})["\']\s*:\s*[A-Za-z_][A-Za-z0-9_.]*\s*[,}\n]', txt):
                    idx.setdefault(t, name)
                for t in re.findall(r'node_id\s*=\s*["\']([^"\']+)["\']', txt):
                    idx.setdefault(t, name)
                for t in re.findall(r'get_name\(["\']([^"\']+)["\']\)', txt):
                    idx.setdefault(t + " (rgthree)", name)
                for t in re.findall(r'class\s+([A-Za-z0-9_]+)\b', txt):
                    idx.setdefault(t, name)
    return idx


# ---------------------------------------------------------------- tiers by name prefix
def pytest_collection_modifyitems(config, items):
    """test_<tier>_<name> carries the <tier> marker when <tier> is one of the suite's ini markers, so a converted
    suite keeps its naming and `-m <tier>` works without decorating every function."""
    tiers = {ln.split(":")[0].strip() for ln in config.getini("markers")}
    for item in items:
        m = re.match(r"test_([a-z0-9]+)_", item.name)
        if m and m.group(1) in tiers and not any(mk.name == m.group(1) for mk in item.iter_markers()):
            item.add_marker(getattr(pytest.mark, m.group(1)))


PID1_ENV = "PUBLIC_KEY=ssh-ed25519 AAAAC3fake mac@key\nHF_TOKEN=token_here\nSOME_IDS=replace_with_ids\nJUPYTER_PASSWORD=x\ndownload_example=true\nRUNPOD_POD_ID=fakepod\n"
PID1_ENV_VM = PID1_ENV.replace("RUNPOD_POD_ID=fakepod\n", "")      # a VM host: no RunPod variable, so BASE_HOST resolves to vm (2.2.0)


def _fake_code_tree(c, comfy_version):
    """A ComfyUI code tree: main.py, requirements, the version module, comfy_extras."""
    for d in ("custom_nodes", "comfy_extras"):
        (c / d).mkdir(parents=True, exist_ok=True)
    (c / "main.py").write_text(""); (c / "requirements.txt").write_text(""); (c / "comfyui_version.py").write_text('__version__ = "%s"\n' % comfy_version)


def _pkg_comfy_min(pkg_dir, floor="0.34.7"):
    """The fake machine's ComfyUI: the package's own COMFY_MIN (3.0.0), never below the fixture's historic 0.34.7. A package
    needing a newer ComfyUI (Qwen Image 2.1: 0.37.0) otherwise stopped at the version gate before a pack or row was placed."""
    import re
    best = floor
    for sc in pathlib.Path(pkg_dir).glob("*-script.sh"):
        m = re.search(r'COMFY_MIN="([0-9][0-9.]*)"', sc.read_text(encoding="utf-8", errors="replace"))
        if m and tuple(int(x) for x in m.group(1).split(".")) > tuple(int(x) for x in best.split(".")):
            best = m.group(1)
    return best


def fake_pod(tmp_path, pkg_dir, strays=False, duplicates=False, foreign=False, local_present=True, poison_venv=False, dangling_venv=False, comfy_version=None, layout="official"):
    """A machine-shaped tree under <tmp_path>, in one of four layouts:
      official  — RunPod's ComfyUI image: the tree at <root>/ComfyUI with a template venv and runpod-slim/comfyui_args.txt
      community — a community template's runtime: code on the container disk (<root>/image/ComfyUI, /opt/venv, the runtime and
                  template dirs), the persist dirs models/user/output/input/custom_nodes on the volume at <root>/ComfyUI
                  WITHOUT main.py, one of the template's own models in place
      bare      — an empty volume (a plain GPU image on RunPod)
      volume    — 2.2.0: an empty volume on a VM host (Verda, Crusoe, an owned box): no RunPod variable in PID 1's env and
                  the driver's state/host.env naming the host, so the base resolves BASE_HOST=vm and binds loopback
    The package is copied to <root>/pkg (so the committed fixture is never rewritten); PID 1's env is <root>/pid1.env.
    Returns the canonical ComfyUI dir (<root>/ComfyUI — for community/bare/volume the place the base materialises it)."""
    comfy_version = comfy_version or _pkg_comfy_min(pkg_dir)
    root = pathlib.Path(tmp_path); root.mkdir(parents=True, exist_ok=True); c = root / "ComfyUI"; m = c / "models"
    (root / "pid1.env").write_text(PID1_ENV_VM if layout == "volume" else PID1_ENV)
    if layout == "volume":
        st = root / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
        (st / "host.env").write_text("BASE_HOST=vm\nBASE_VOLUME=%s\n" % root)
    elif layout == "community":
        img = root / "image"
        _fake_code_tree(img / "ComfyUI", comfy_version)
        (img / "opt" / "venv" / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("python3", "python3.12"):
            p = img / "opt" / "venv" / "bin" / name
            p.write_text('#!/bin/bash\necho "%s $*" >> "%s"\n' % (name, img / "calls.log")); p.chmod(0o755)
        (img / "comfyui-runtime" / "src").mkdir(parents=True, exist_ok=True); (img / "comfyui-runtime" / "src" / "start.sh").write_text("#!/bin/bash\n# the runtime\n")
        (img / "comfyui-template").mkdir(exist_ok=True)
        (img / "comfyui-template" / "template.json").write_text('{"comfy_extra_args": "--disable-dynamic-vram", "sage": true, "custom_nodes": {"target": "image", "repos": []}, "models_symlink": false}\n')
        for d in ("models/diffusion_models", "user/default/workflows", "output", "input", "custom_nodes"):
            (c / d).mkdir(parents=True, exist_ok=True)
        with open(c / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors", "wb") as f:
            f.truncate(4096)
    elif layout == "bare":
        pass
    elif layout == "official":
        (c / "models").mkdir(parents=True, exist_ok=True); (c / "user" / "default" / "workflows").mkdir(parents=True, exist_ok=True)
        _fake_code_tree(c, comfy_version)
        v = c / ".venv-cu130"; (v / "bin").mkdir(parents=True, exist_ok=True)
        if dangling_venv:
            (v / "bin" / "python").symlink_to(root / "nonexistent" / "python3")
            b = c / ".venv-cu130.pre-comfy-base-20260101-000000"; (b / "bin").mkdir(parents=True)
            (b / "bin" / "python").symlink_to(sys.executable)
        else:
            (v / "bin" / "python").symlink_to(sys.executable)
        if poison_venv:
            (v / "pyvenv.cfg").write_text("home = /tmp/uv-python/cpython-3.13.1/bin\n"); (v / ".comfy-base-venv").write_text("python=3.13.1 torch=fake base=1.0.0 ts=x by=stub\n")
        (root / "runpod-slim").mkdir(exist_ok=True); (root / "runpod-slim" / "comfyui_args.txt").write_text("# template args\n--use-sage-attention\n")
        mgr = c / "custom_nodes" / "ComfyUI-Manager"; mgr.mkdir(exist_ok=True); (mgr / "config.ini").write_text("[default]\nnetwork_mode = public\nsecurity_level = weak\n")
    else:
        raise ValueError("layout must be official, community, bare or volume (got %r)" % layout)
    def mk(rel, size, base=m):
        p = base / rel; p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.truncate(size)
        return p
    if strays:
        mk("stub_vae.safetensors", 4096, base=root / "somewhere")
    if duplicates:
        mk("stub_vae.safetensors", 4096, base=root / "dup")
        mk("vae/Example/stub_old.safetensors", 77)                       # superseded, inside the stub's family folder
    if foreign:
        mk("my_private_lora.safetensors", 1000, base=root / "foreign")
        mk("vae/stub_legacy/unknown.safetensors", 500)                 # unknown file in a declared legacy folder: reported, kept
    if local_present:
        mk("stub_local.safetensors", 12, base=root / "local")
    pkg = root / "pkg"; pkg.mkdir(exist_ok=True)
    import shutil
    for f in pathlib.Path(pkg_dir).iterdir():
        if f.is_file():
            (pkg / f.name).write_bytes(f.read_bytes()); (pkg / f.name).chmod(f.stat().st_mode)
        elif f.is_dir() and (f / "__init__.py").exists() and not f.name.startswith(("_", ".")):
            # 3.0.0: a package's vendored node packs ship in its zip, so the fake machine carries them too (they were
            # missing, and every suite test that reads a pack file failed inside the rehearsal while green elsewhere)
            shutil.copytree(f, pkg / f.name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    (root / "home").mkdir(exist_ok=True)
    return c


def boot_stubs(root, torch_ok=True):
    """What a fake pod needs to 'boot' through comfy-base/boot.sh on a workstation: sshd / ssh-keygen / pgrep / curl stubs
    on PATH (each logging to <root>/calls.log), a JupyterLab stub at the tools venv path, and the fake venv's python
    replaced by a stub that logs argv and answers `-c 'import torch'` as told. Returns (bin_dir, log)."""
    root = pathlib.Path(root); bin_ = root / "bin"; bin_.mkdir(parents=True, exist_ok=True); log = root / "calls.log"
    for name in ("sshd", "ssh-keygen"):
        (bin_ / name).write_text('#!/bin/bash\necho "%s $*" >> "%s"\n[ "%s" = sshd ] && touch "%s/sshd.running"\nexit 0\n' % (name, log, name, root))
    (bin_ / "pgrep").write_text('#!/bin/bash\necho "pgrep $*" >> "%s"\n[ -e "%s/sshd.running" ]\n' % (log, root))
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl $*" >> "%s"\nexit 0\n' % log)
    for p in bin_.iterdir():
        p.chmod(0o755)
    jl = root / "comfy-base" / "tools" / "bin" / "jupyter-lab"; jl.parent.mkdir(parents=True, exist_ok=True)
    jl.write_text('#!/bin/bash\necho "jupyter-lab $* JUPYTER_TOKEN=${JUPYTER_TOKEN-unset}" >> "%s"\nsleep 0.2\n' % log); jl.chmod(0o755)
    py = root / "ComfyUI" / ".venv-cu130" / "bin" / "python"
    if py.exists() or py.is_symlink():
        py.unlink()
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text('#!/bin/bash\necho "python $*" >> "%s"\ncase "$*" in *"import torch"*) exit %d;; esac\nexit 0\n' % (log, 0 if torch_ok else 1))
    py.chmod(0o755)
    return bin_, log


def boot_fake_pod(root, bin_, extra_env=None):
    """Run <root>/comfy-base/boot.sh once (BOOT_ONCE) with the stubs; returns (CompletedProcess, the boot log text)."""
    root = pathlib.Path(root)
    env = {**os.environ, "PATH": "%s:/usr/bin:/bin" % bin_, "BOOT_HOME": str(root / "comfy-base"), "BOOT_ROOT": str(root / "fs"),
           "BOOT_ONCE": "1", "BASE_START_TRIES": "1", "PUBLIC_KEY": "ssh-ed25519 AAAAC3fake mac@key", "RUNPOD_POD_ID": "fakepod"}
    for k in ("JUPYTER_TOKEN", "JUPYTER_PASSWORD"):
        env.pop(k, None)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(root / "comfy-base" / "boot.sh")], capture_output=True, text=True, env=env, timeout=120)
    logs = sorted((root / "comfy-base" / "state" / "logs").glob("boot_*.log"))
    return r, (logs[-1].read_text() if logs else "")


def run_script(script, *args, env=None, fake_root=None, inp="", timeout=600):
    """Run a package script against a fake pod. `script` may be the committed fixture: with fake_root set, the copy
    under <root>/pkg is run instead so the fixture stays pristine."""
    script = pathlib.Path(script)
    if fake_root is not None and (pathlib.Path(fake_root) / "pkg" / script.name).exists():
        script = pathlib.Path(fake_root) / "pkg" / script.name
    e = {k: v for k, v in os.environ.items() if k not in _LEAK}
    # PYTHONDONTWRITEBYTECODE: with HOME inside the fake tree, Apple's python would drop .pyc caches under
    # <root>/home/Library/Caches and every "--check changed nothing" hash would lie
    e.update({"COMFY_BASE": str(BASE_LIB), "BASE_INNER": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    if fake_root is not None:
        fr = pathlib.Path(fake_root)
        e.update({"BASE_FAKE_ROOT": str(fr), "BASE_SEARCH_ROOTS": str(fr), "BASE_NO_NET": "1", "HOME": str(fr / "home"), "BASE_FAKE_FREE_GB": "500"})
        if (fr / "pid1.env").exists():
            e["BASE_FAKE_PID1_ENV"] = str(fr / "pid1.env")          # what /proc/1/environ says on the pod
        if (fr / "image").is_dir():
            e["BASE_FAKE_IMAGE_ROOT"] = str(fr / "image")            # the container disk: never adopted, never scanned
    e.update(env or {})
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=e, input=inp, timeout=timeout)


def tree_hash(root, ignore=()):
    """A digest of every file's relative path and size under root, skipping any path with a component in `ignore`.
    The install tests pass ("logs", "sizes.tsv"): both are runtime records, not the store. sizes.tsv (3.5.0) caches a
    directory's size by path and ctime, and a second run may add a row for a directory the first run never measured."""
    h = hashlib.sha1()
    for p in sorted(pathlib.Path(root).rglob("*")):
        if any(part in ignore for part in p.relative_to(root).parts):
            continue
        if p.is_file() or p.is_symlink():
            h.update(str(p.relative_to(root)).encode()); h.update(str(p.lstat().st_size).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- the per-tier table
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tiers = [ln.split(":")[0].strip() for ln in config.getini("markers")]
    counts = collections.defaultdict(collections.Counter)
    for outcome in ("passed", "failed", "skipped", "error"):
        for rep in terminalreporter.stats.get(outcome, []):
            marks = getattr(rep, "keywords", {})
            tier = next((t for t in tiers if t in marks), "other")
            counts[tier][outcome] += 1
    if not counts:
        return
    terminalreporter.write_sep("=", "test tiers")
    for t in tiers + ["other"]:
        c = counts.get(t)
        if not c:
            continue
        state = "FAIL" if c["failed"] or c["error"] else ("skip" if not c["passed"] else "ok")
        terminalreporter.write_line("  %-12s %-5s passed %3d  failed %2d  skipped %2d" % (t, state, c["passed"], c["failed"], c["skipped"]))


# ---------------------------------------------------------------- foreign source (the packs' own .py files)
FOREIGN_SYNTAX_WARNINGS = []     # (file, message) — Python's SyntaxWarnings about THEIR code, recorded, never ours to fix


def parse_foreign_source(path):
    """ast.parse a file we only read (a pack's node source). Python warns about invalid escape sequences in such files
    at compile time, and those warnings are theirs: they are recorded here (a test asserts none is ours) instead of
    surfacing as the suite's. None on a missing file or a SyntaxError."""
    import ast, warnings
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:     # closed: an unclosed file is a ResourceWarning, and warnings are what this helper is about
            text = fh.read()
    except OSError:
        return None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return None
    for w in caught:
        if issubclass(w.category, SyntaxWarning):
            FOREIGN_SYNTAX_WARNINGS.append((str(path), str(w.message)))
    return tree
