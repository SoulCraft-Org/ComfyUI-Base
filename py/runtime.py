#!/usr/bin/env python3
"""The proven runtime (3.1.0; parts re-cut in 3.29.3): what a buyer's machine installs instead of building.

A runtime is the part of a green machine that is the same for every buyer: the ComfyUI tree (minus models, inputs,
outputs, users and temp), the uv-managed Python, the venv inside the tree, every node pack that is a git checkout
(never a package's vendored packs, which come with its zip), the base's tools venv
(JupyterLab) and its wheel cache (the SageAttention build). It is captured on a machine after a green run as
INDEPENDENT parts: each one its own zstd-compressed tar of a share of the files (plus the directories above them), so
the parts fetch in any order, unpack in any order and unpack at once. runtime.json is the ONLY place the runtime's
versions are written (the base's lib/ and py/ carry none) and lists every part's name, sha256 and bytes.

    runtime.py build --root /workspace --comfy /workspace/ComfyUI --out DIR --torch-cuda 13.0 [--part-bytes N]
                     [--url-base PREFIX] [--jobs N] [--level N] [--base-version V] [--python V] [--sage-key K]
    runtime.py apply MANIFEST LOCATION --root DIR --work DIR [--parts FILE] [--jobs 8] [--result FILE]
                                                   fetch parts --jobs at a time, verify each sha256, unpack each as it lands
    runtime.py verify-parts MANIFEST DIR           every part's size and sha256; rc 1 naming the first that differs
    runtime.py sources MANIFEST LOCATION [--parts FILE]   "<name>\t<where to fetch it>\t<bytes>" per part
    runtime.py check-tree MANIFEST --comfy DIR     ComfyUI's and every pack's HEAD equal the manifest's; rc 1 per difference
    runtime.py covers MANIFEST URL...              rc 1 naming every pack URL the runtime does not carry
    runtime.py field MANIFEST dotted.key           one value, for bash
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import time

FORMAT = 2                          # 3.29.3: independent zstd parts. Format 1 (gzip slices of one stream) was never published
PART_BYTES = 512 * 1024 * 1024      # of file content per part, before compression: about 250 MB of torch's CUDA libraries packed
JOBS = 8                            # parts fetched and unpacked at once
ZSTD_LEVEL = 3
TAR_HEADER = 512
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


def _zstd():
    z = shutil.which("zstd")
    if not z:
        raise SystemExit("runtime: zstd is not installed (apt-get install zstd); the runtime's parts are zstd tars")
    return z


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ancestors(name):
    parts = name.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _partition(members, limit):
    """members -> a list of parts, each a list of (path, archive name). Files and symlinks are cut in walk order at about
    `limit` bytes of content; every part also carries the directory entries above its own members, so it unpacks alone."""
    dirs, files = {}, []
    for path, name in members:
        if path.is_dir() and not path.is_symlink():
            dirs[name] = path
        else:
            size = path.lstat().st_size if path.is_file() and not path.is_symlink() else 0
            files.append((path, name, size + TAR_HEADER))
    cut, cur = [[]], 0
    for path, name, cost in files:
        if cut[-1] and cur + cost > limit:
            cut.append([])
            cur = 0
        cut[-1].append((path, name))
        cur += cost
    parts, covered = [], []
    for items in cut:
        need = {d for _, n in items for d in _ancestors(n) if d in dirs} | {n for _, n in items if n in dirs}
        covered.extend(need)
        parts.append([(dirs[d], d) for d in sorted(need)] + items)
    rest = [(dirs[d], d) for d in sorted(dirs) if d not in covered]      # empty subtrees go with the first part
    parts[0] = rest + parts[0]
    return parts


def _write_part(out, name, items, level):
    """One part: a tar of `items` streamed into zstd. A hard link can only point inside its own part."""
    dest = pathlib.Path(out) / name
    proc = subprocess.Popen([_zstd(), "-q", "-f", "-%d" % level, "-T1", "-o", str(dest)], stdin=subprocess.PIPE)
    raw = 0
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for path, arc in items:
                tar.add(str(path), arcname=arc, recursive=False)
                if path.is_file() and not path.is_symlink():
                    raw += path.stat().st_size
        proc.stdin.close()
    except BaseException:
        proc.kill()
        raise
    if proc.wait() != 0:
        raise SystemExit("runtime: zstd failed on %s" % name)
    return {"name": name, "bytes": dest.stat().st_size, "sha256": _sha_file(dest), "raw": raw}


def build(a):
    floor = driver_floor(a.torch_cuda)
    _zstd()
    root = pathlib.Path(a.root)
    comfy = pathlib.Path(a.comfy)
    comfy_rel = str(comfy.relative_to(root)) if comfy.is_absolute() else str(comfy)
    if comfy_rel != "ComfyUI":
        PATHS[0] = comfy_rel
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cut = _partition(list(_members(root, comfy_rel)), a.part_bytes)
    names = ["runtime.%03d.tar.zst" % i for i in range(len(cut))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(cut)))) as ex:
        made = list(ex.map(lambda t: _write_part(out, t[0], t[1], a.level), zip(names, cut)))
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
    for p in made:
        p["url"] = "%s/%s" % (url_base, p["name"]) if url_base else p["name"]
    manifest = {
        "format": FORMAT, "compression": "zstd", "created": _utc(), "base": a.base_version or "",
        "cuda": a.torch_cuda, "driver_min": str(floor),
        "comfyui": {"dir": comfy_rel, "commit": _git(root / comfy_rel, "rev-parse", "HEAD"), "version": version},
        "python": a.python or "", "venv": "%s/.venv-cu130" % comfy_rel if (root / comfy_rel / ".venv-cu130").exists() else "",
        "packs": packs, "sageattention": {"key": a.sage_key or ""},
        "paths": _paths(root), "exclude": ["%s/%s" % (comfy_rel, x) for x in sorted(EXCLUDE_TOP)],
        "parts": made,
    }
    (out / "runtime.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print("runtime: %d part(s), %d bytes packed (%d unpacked), %d pack(s) -> %s" % (
        len(made), sum(p["bytes"] for p in made), sum(p["raw"] for p in made), len(packs), out / "runtime.json"))
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


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _signed(parts_file, manifest_parts):
    """{name: url} from a list of per-part links given beside a local manifest: a JSON list (or {"parts": [...]}) of
    {name, url[, sha256, bytes]}. A sha256 or size it states must be the manifest's: the links never override what the
    manifest says the bytes are."""
    data = json.loads(pathlib.Path(parts_file).read_text(encoding="utf-8"))
    rows = data.get("parts", []) if isinstance(data, dict) else data
    by_name = {p["name"]: p for p in manifest_parts}
    out = {}
    for r in rows:
        n = r.get("name")
        if n not in by_name:
            continue
        for k in ("sha256", "bytes"):
            if k in r and r[k] != by_name[n].get(k):
                raise SystemExit("runtime: the link list says %s of %s is %s, the manifest says %s" % (k, n, r[k], by_name[n].get(k)))
        if not r.get("url"):
            raise SystemExit("runtime: the link list has no url for %s" % n)
        out[n] = r["url"]
    missing = [n for n in by_name if n not in out]
    if missing:
        raise SystemExit("runtime: the link list has no link for %d part(s), first %s" % (len(missing), missing[0]))
    return out


def _resolve(m, loc, parts_file=None):
    """Every part with where it comes from: a signed link, else the manifest's own url, else its name beside the manifest."""
    signed = _signed(parts_file, m.get("parts", [])) if parts_file else {}
    rows = []
    for p in m.get("parts", []):
        if not SAFE_NAME.match(p["name"]):
            raise SystemExit("runtime: a part named %r is not a plain file name" % p["name"])
        u = signed.get(p["name"]) or p.get("url") or p["name"]
        if "://" not in u and not u.startswith("/"):
            if "://" in loc:
                u = loc.rsplit("/", 1)[0] + "/" + u
            else:
                u = str(pathlib.Path(loc).resolve().parent / u)
        rows.append(dict(p, where=u))
    return rows


def sources(a):
    for r in _resolve(_load(a.manifest), a.location, a.parts):
        print("%s\t%s\t%d" % (r["name"], r["where"], r.get("bytes", 0)))
    return 0


def _scrub(text):
    return re.sub(r"\S+://\S+", "<url>", text or "")        # a signed link is a credential: it is never printed


def _fetch(where, dest):
    """One part to `dest`: a copy from a local path, else curl over https (resumable, retried)."""
    if "://" not in where:
        shutil.copyfile(where, dest)
        return
    if not (where.startswith("https://") or where.startswith(("http://127.0.0.1:", "http://localhost:"))):
        raise RuntimeError("only https links are fetched (http only from this machine's own loopback)")
    r = subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-delay", "2", "--retry-all-errors", "-C", "-", "--max-time", "1800",
                        "-o", str(dest), where], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("curl exit %d: %s" % (r.returncode, _scrub((r.stderr or "").strip().splitlines()[-1] if r.stderr.strip() else "")))


def _unpack(part, root):
    """zstd -dc part | tar -x -C root, both exit codes judged."""
    z = subprocess.Popen([_zstd(), "-dc", "-q", str(part)], stdout=subprocess.PIPE)
    t = subprocess.run(["tar", "-xf", "-", "-C", str(root)], stdin=z.stdout, capture_output=True, text=True)
    z.stdout.close()
    zrc = z.wait()
    if zrc != 0 or t.returncode != 0:
        raise RuntimeError("unpack failed (zstd %d, tar %d) %s" % (zrc, t.returncode, (t.stderr or "").strip()[-300:]))


def _one(part, root, work):
    """Fetch one part (skipped when a verified copy is already in `work`), check size and sha256, unpack it, drop the copy."""
    name, t0 = part["name"], time.time()
    local = pathlib.Path(part["where"]) if "://" not in part["where"] else pathlib.Path(work) / name
    if "://" in part["where"] and not (local.is_file() and local.stat().st_size == part.get("bytes")):
        _fetch(part["where"], local)
    t1 = time.time()
    if local.stat().st_size != part.get("bytes"):
        raise RuntimeError("%s is %d bytes, the manifest says %s" % (name, local.stat().st_size, part.get("bytes")))
    if _sha_file(local) != part["sha256"]:
        if "://" in part["where"]:
            local.unlink()                       # a bad copy is never resumed from
        raise RuntimeError("%s does not match its sha256" % name)
    t2 = time.time()
    _unpack(local, root)
    t3 = time.time()
    if "://" in part["where"]:
        local.unlink()
    return {"name": name, "fetch": t1 - t0, "verify": t2 - t1, "unpack": t3 - t2}


def apply(a):
    """Every part, `jobs` at a time: fetched, verified and unpacked as it lands, the largest first. Nothing is stamped
    here: the caller does that on a zero exit. A failed part stops the rest and is named (never its link)."""
    m = _load(a.manifest)
    if m.get("format") != FORMAT or m.get("compression") != "zstd":
        raise SystemExit("runtime: %s is runtime format %s, this base reads format %d (zstd parts): capture it again" % (a.manifest, m.get("format"), FORMAT))
    rows = sorted(_resolve(m, a.location, a.parts), key=lambda r: -r.get("bytes", 0))
    root, work = pathlib.Path(a.root), pathlib.Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    _zstd()
    t0 = time.time()
    done, failed = [], None
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(rows) or 1)))
    futs = {ex.submit(_one, r, root, work): r["name"] for r in rows}
    try:
        for f in concurrent.futures.as_completed(futs):
            try:
                done.append(f.result())
            except Exception as e:                # noqa: BLE001 - every cause is reported by part name
                failed = "%s: %s" % (futs[f], _scrub(str(e)))
                break
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    if failed:
        print("runtime: part %s" % failed, file=sys.stderr)
        return 1
    t1 = time.time()
    wall = max(t1 - t0, 1e-6)
    packed, raw = sum(r.get("bytes", 0) for r in rows), sum(r.get("raw", 0) for r in rows)
    res = {"t0": int(t0), "t1": int(t1 + 0.999), "seconds": round(wall, 2), "parts": len(rows), "jobs": a.jobs, "bytes": packed, "raw": raw,
           "gbps": round(packed / wall / 1e9, 3), "fetch_seconds": round(sum(d["fetch"] for d in done), 1),
           "verify_seconds": round(sum(d["verify"] for d in done), 1), "unpack_seconds": round(sum(d["unpack"] for d in done), 1)}
    if a.result:
        pathlib.Path(a.result).write_text(json.dumps(res) + "\n", encoding="utf-8")
    print("runtime: %d part(s), %.2f GB packed (%.2f GB unpacked) in %.1f s = %.2f GB/s, %d at once (busy: fetch %.0f s, verify %.0f s, unpack %.0f s)" % (
        len(rows), packed / 1e9, raw / 1e9, wall, res["gbps"], a.jobs, res["fetch_seconds"], res["verify_seconds"], res["unpack_seconds"]))
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
    b.add_argument("--part-bytes", type=int, default=PART_BYTES, help="bytes of file content per part, before compression")
    b.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 2)), help="parts compressed at once")
    b.add_argument("--level", type=int, default=ZSTD_LEVEL)
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
    s.add_argument("--parts")
    ap_ = sub.add_parser("apply")
    ap_.add_argument("manifest")
    ap_.add_argument("location")
    ap_.add_argument("--root", required=True)
    ap_.add_argument("--work", required=True)
    ap_.add_argument("--parts")
    ap_.add_argument("--jobs", type=int, default=JOBS)
    ap_.add_argument("--result")
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
    return {"build": build, "verify-parts": verify_parts, "sources": sources, "apply": apply, "check-tree": check_tree,
            "covers": covers, "field": field}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
