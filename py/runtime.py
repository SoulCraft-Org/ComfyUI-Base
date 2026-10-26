#!/usr/bin/env python3
"""The proven runtime (3.1.0): what a buyer's machine installs instead of building.

A runtime is the part of a green machine that is the same for every buyer: the ComfyUI tree (minus models, inputs,
outputs, users and temp), the uv-managed Python, the venv inside the tree, every node pack that is a git checkout
(never a package's vendored packs, which come with its zip), the base's tools venv
(JupyterLab) and its wheel cache (the SageAttention build). It is captured on a machine after a green run, as a
gzip tar split into parts that fit a GitHub release asset, with a manifest (runtime.json) that is the ONLY place its
versions are written: the base's lib/ and py/ carry none.

    runtime.py build --root /workspace --comfy /workspace/ComfyUI --out DIR [--part-bytes N] [--url-base PREFIX]
                     [--torch-cuda V] [--base-version V] [--python V] [--sage-key K]
    runtime.py verify-parts MANIFEST DIR           every part's size and sha256; rc 1 naming the first that differs
    runtime.py sources MANIFEST LOCATION           "<name>\\t<where to fetch it>\\t<bytes>" per part, relative names resolved
    runtime.py check-tree MANIFEST --comfy DIR     ComfyUI's and every pack's HEAD equal the manifest's; rc 1 per difference
    runtime.py covers MANIFEST URL...              rc 1 naming every pack URL the runtime does not carry
    runtime.py field MANIFEST dotted.key           one value, for bash
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tarfile

FORMAT = 1
PART_BYTES = 1_900_000_000          # under GitHub's 2 GiB per release asset
PATHS = ["ComfyUI", "comfy-base/python", "comfy-base/bin", "comfy-base/tools", "comfy-base/state/wheels"]
EXCLUDE_TOP = {"models", "input", "output", "user", "temp"}      # under ComfyUI/: data, never runtime
EXCLUDE_NAMES = {"__pycache__"}
# The lowest NVIDIA driver major a CUDA major's wheels run on: NVIDIA's minor-version compatibility table (CUDA Toolkit
# release notes). It is what the runtime's WHEELS need, never what the capture machine happened to run.
DRIVER_FLOOR = {13: 580, 12: 525, 11: 450}


def driver_floor(cuda):
    """The driver floor of a torch CUDA build ("13.0" -> 580). A build that names no CUDA, or one this table does not
    know, raises: a runtime without a floor would be refused or accepted by accident."""
    m = (cuda or "").strip().split(".")[0]
    if not m.isdigit() or int(m) not in DRIVER_FLOOR:
        raise SystemExit("runtime: cannot derive the driver floor from torch CUDA %r (known majors: %s)" % (cuda, sorted(DRIVER_FLOOR)))
    return DRIVER_FLOOR[int(m)]


def _git(d, *args):
    r = subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _norm_url(u):
    u = (u or "").strip().lower().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _Parts:
    """A binary sink that cuts what it is given into numbered part files, hashing each as it goes."""

    def __init__(self, out, stem, limit):
        self.out, self.stem, self.limit = pathlib.Path(out), stem, limit
        self.parts, self._f, self._h, self._n = [], None, None, 0

    def _open(self):
        name = "%s.%03d" % (self.stem, len(self.parts))
        self._f, self._h, self._n = open(self.out / name, "wb"), hashlib.sha256(), 0
        self.parts.append({"name": name})

    def _close(self):
        if self._f:
            self._f.close()
            self.parts[-1].update(bytes=self._n, sha256=self._h.hexdigest())
            self._f = None

    def write(self, b):
        view = memoryview(b)
        while view:
            if self._f is None or self._n >= self.limit:
                self._close()
                self._open()
            take = view[: self.limit - self._n]
            self._f.write(take)
            self._h.update(take)
            self._n += len(take)
            view = view[len(take):]
        return len(b)

    def flush(self):
        pass

    def close(self):
        self._close()


def _paths(root):
    """PATHS plus every comfy-base/tools.<python>.<ts>: comfy-base/tools is a symlink to one of them."""
    extra = sorted(str(p.relative_to(root)) for p in (pathlib.Path(root) / "comfy-base").glob("tools.*") if p.is_dir())
    return PATHS + [x for x in extra if x not in PATHS]


def _members(root, comfy_rel):
    """(absolute path, archive name) for everything in the runtime set, in a stable order."""
    root = pathlib.Path(root)
    for rel in _paths(root):
        base = root / rel
        if not base.exists() and not base.is_symlink():
            continue
        yield base, rel
        if base.is_symlink() or not base.is_dir():
            continue
        for dp, ds, fs in os.walk(base, followlinks=False):
            d = pathlib.Path(dp)
            keep = []
            for x in sorted(ds):
                if x in EXCLUDE_NAMES:
                    continue
                if rel == comfy_rel and d == base and x in EXCLUDE_TOP:
                    continue
                # 3.1.1: only a pack that is a git checkout is runtime. A package's VENDORED packs (plain folders its
                # zip installs) are that package's own, often paid, code: they arrive with the zip on every install
                # and never ride a runtime that may be published.
                if rel == comfy_rel and d == base / "custom_nodes" and not (d / x).is_symlink() and not (d / x / ".git").exists():
                    continue
                keep.append(x)
            ds[:] = keep
            for x in keep:                         # a symlinked dir is listed here and never descended: the link itself is archived
                p = d / x
                yield p, str(p.relative_to(root))
            for x in sorted(fs):
                p = d / x
                yield p, str(p.relative_to(root))


def build(a):
    floor = driver_floor(a.torch_cuda)
    root = pathlib.Path(a.root)
    comfy = pathlib.Path(a.comfy)
    comfy_rel = str(comfy.relative_to(root)) if comfy.is_absolute() else str(comfy)
    if comfy_rel != "ComfyUI":
        PATHS[0] = comfy_rel
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sink = _Parts(out, "runtime.tar.gz", a.part_bytes)
    import gzip
    gz = gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=1, mtime=0)
    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for path, name in _members(root, comfy_rel):
            tar.add(str(path), arcname=name, recursive=False)
    gz.close()
    sink.close()
    cn = root / comfy_rel / "custom_nodes"
    packs = []
    if cn.is_dir():
        for d in sorted(cn.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                remote = _git(d, "remote", "get-url", "origin") or _git(d, "config", "--get", "remote.origin.url")
                packs.append({"dir": d.name, "url": remote, "commit": _git(d, "rev-parse", "HEAD")})
    version = ""
    vf = root / comfy_rel / "comfyui_version.py"
    if vf.exists():
        for ln in vf.read_text(encoding="utf-8").splitlines():
            if ln.startswith("__version__"):
                version = ln.split("=", 1)[1].strip().strip("\"'")
    url_base = (a.url_base or "").rstrip("/")
    for p in sink.parts:
        p["url"] = "%s/%s" % (url_base, p["name"]) if url_base else p["name"]
    manifest = {
        "format": FORMAT, "created": _utc(), "base": a.base_version or "",
        "cuda": a.torch_cuda, "driver_min": str(floor),
        "comfyui": {"dir": comfy_rel, "commit": _git(root / comfy_rel, "rev-parse", "HEAD"), "version": version},
        "python": a.python or "", "venv": "%s/.venv-cu130" % comfy_rel if (root / comfy_rel / ".venv-cu130").exists() else "",
        "packs": packs, "sageattention": {"key": a.sage_key or ""},
        "paths": _paths(root), "exclude": ["%s/%s" % (comfy_rel, x) for x in sorted(EXCLUDE_TOP)],
        "parts": sink.parts,
    }
    (out / "runtime.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print("runtime: %d part(s), %d bytes, %d pack(s) -> %s" % (len(sink.parts), sum(p["bytes"] for p in sink.parts), len(packs), out / "runtime.json"))
    return 0


def _load(p):
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))


def verify_parts(a):
    m, d = _load(a.manifest), pathlib.Path(a.dir)
    for p in m.get("parts", []):
        f = d / p["name"]
        if not f.is_file():
            print("missing part: %s" % p["name"], file=sys.stderr)
            return 1
        if f.stat().st_size != p["bytes"]:
            print("part %s is %d bytes, the manifest says %d" % (p["name"], f.stat().st_size, p["bytes"]), file=sys.stderr)
            return 1
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != p["sha256"]:
            print("part %s does not match its sha256" % p["name"], file=sys.stderr)
            return 1
    print("runtime parts verified: %d" % len(m.get("parts", [])))
    return 0


def sources(a):
    m, loc = _load(a.manifest), a.location
    for p in m.get("parts", []):
        u = p.get("url") or p["name"]
        if "://" not in u and not u.startswith("/"):
            if "://" in loc:
                u = loc.rsplit("/", 1)[0] + "/" + u
            else:
                u = str(pathlib.Path(loc).resolve().parent / u)
        print("%s\t%s\t%d" % (p["name"], u, p.get("bytes", 0)))
    return 0


def check_tree(a):
    m, comfy = _load(a.manifest), pathlib.Path(a.comfy)
    bad = 0
    want = m.get("comfyui", {}).get("commit", "")
    have = _git(comfy, "rev-parse", "HEAD")
    if want and have != want:
        print("ComfyUI is at %s, the runtime says %s" % (have or "no commit", want), file=sys.stderr)
        bad = 1
    for p in m.get("packs", []):
        d = comfy / "custom_nodes" / p["dir"]
        have = _git(d, "rev-parse", "HEAD") if d.is_dir() else ""
        if not d.is_dir():
            print("pack %s is missing from %s" % (p["dir"], d.parent), file=sys.stderr)
            bad = 1
        elif p.get("commit") and have != p["commit"]:
            print("pack %s is at %s, the runtime says %s" % (p["dir"], have or "no commit", p["commit"]), file=sys.stderr)
            bad = 1
    return bad


def covers(a):
    have = {_norm_url(p.get("url")) for p in _load(a.manifest).get("packs", [])}
    bad = 0
    for u in a.urls:
        if _norm_url(u) not in have:
            print("not in the runtime: %s" % u, file=sys.stderr)
            bad = 1
    return bad


def field(a):
    v = _load(a.manifest)
    for k in a.key.split("."):
        v = v.get(k, "") if isinstance(v, dict) else ""
    print(v if not isinstance(v, (dict, list)) else json.dumps(v))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="runtime.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--root", required=True)
    b.add_argument("--comfy", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--part-bytes", type=int, default=PART_BYTES)
    b.add_argument("--url-base", default="")
    b.add_argument("--torch-cuda", required=True)
    b.add_argument("--base-version", default="")
    b.add_argument("--python", default="")
    b.add_argument("--sage-key", default="")
    v = sub.add_parser("verify-parts")
    v.add_argument("manifest")
    v.add_argument("dir")
    s = sub.add_parser("sources")
    s.add_argument("manifest")
    s.add_argument("location")
    c = sub.add_parser("check-tree")
    c.add_argument("manifest")
    c.add_argument("--comfy", required=True)
    o = sub.add_parser("covers")
    o.add_argument("manifest")
    o.add_argument("urls", nargs="*")
    f = sub.add_parser("field")
    f.add_argument("manifest")
    f.add_argument("key")
    a = ap.parse_args(argv)
    return {"build": build, "verify-parts": verify_parts, "sources": sources, "check-tree": check_tree,
            "covers": covers, "field": field}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
