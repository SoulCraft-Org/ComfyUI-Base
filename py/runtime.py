#!/usr/bin/env python3
"""The proven runtime (3.1.0; parts re-cut in 3.29.3): what a buyer's machine installs instead of building.

A runtime is the part of a green machine that is the same for every buyer: the ComfyUI tree (minus models, inputs,
outputs, users and temp), the uv-managed Python, the venv inside the tree, every node pack that is a git checkout
(never a package's vendored packs, which come with its zip), the base's tools venv
(JupyterLab) and its wheel cache (the SageAttention build). It is captured on a machine after a green run as
INDEPENDENT parts: each one its own zstd-compressed tar of a share of the files (plus the directories above them), so
the parts fetch in any order, unpack in any order and unpack at once. runtime.json is the ONLY place the runtime's
versions are written (the base's lib/ and py/ carry none) and lists every part's name, sha256 and bytes.

3.30.3 (format 3): a file larger than --part-bytes is cut into chunks of that size, each chunk its own tar member in a
"chunks" part of its own, so no part is larger than --part-bytes plus headers and one big library is fetched as several
parts at once. runtime.json's `files` lists each split file (path, mode, mtime, bytes, sha256) with its chunks
[{part, member, offset, bytes}]. `apply` creates the file at its full size, every chunk's part writes its bytes at its offset
(pwrite) as it lands, in any order, and the whole file's sha256 is checked against the manifest when its last chunk is in.

    runtime.py build --root /workspace --comfy /workspace/ComfyUI --out DIR --torch-cuda 13.0 [--part-bytes N]
                     [--url-base PREFIX] [--jobs N] [--level N] [--max-packed N] [--base-version V] [--python V] [--sage-key K] [--apt a,b,c]
    runtime.py apply MANIFEST LOCATION --root DIR --work DIR [--parts FILE] [--jobs 8] [--result FILE]
                     [--zstd-wait S --zstd-gate FILE]   fetch parts --jobs at a time, verify each sha256, unpack each as it lands
    runtime.py apt MANIFEST                        the apt packages the runtime needs, one per line (zstd first)
    runtime.py result-add FILE KEY VALUE           add one field (VALUE as JSON when it parses) to an apply result
    runtime.py ledger-line FILE [--label TEXT]     "<t0>\\t<t1>\\t<seconds>\\t<text>": the apply as one row of the timing ledger
    runtime.py verify-parts MANIFEST DIR           every part's size and sha256; rc 1 naming the first that differs
    runtime.py sources MANIFEST LOCATION [--parts FILE]   "<name>\\t<where to fetch it>\\t<bytes>" per part
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

FORMAT = 3                          # 3.30.3: split files. Format 2 (3.29.3: independent zstd parts, no split file) is still read; format 1 never was published
ACCEPTED = (2, 3)
CHUNKS = "chunks"                   # a part's `kind` when it holds only chunks of split files
BLOCK = 8 << 20
PART_BYTES = 256 * 1024 * 1024      # of file content per part, before compression: 268 MB at most (plus tar headers) even if nothing compresses,
                                    # under the 315 MB a single-request object upload takes (Cloudflare's API); a file larger than this is
                                    # cut into chunks of this size (format 3), and the build fails when a part passes --max-packed (OBJECT_CAP)
OBJECT_CAP = 315_000_000
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


ZSTD_WAIT = (0, None)               # (seconds, gate file): an apt install of zstd may still be running beside the fetch


def _zstd(wait=0, gate=None):
    """The zstd binary. With `wait`, polls for it while an install of it runs, until the gate file (written when that install
    failed) appears or the time is up."""
    deadline = time.time() + wait
    while True:
        z = shutil.which("zstd")
        if z:
            return z
        if time.time() >= deadline or (gate and os.path.exists(gate)):
            raise SystemExit("runtime: zstd is not installed (apt-get install zstd); the runtime's parts are zstd tars")
        time.sleep(0.2)


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BLOCK), b""):
            h.update(chunk)
    return h.hexdigest()


class _Range:
    """A read-only file object over bytes [offset, offset + length) of a file: one chunk, for tarfile.addfile."""

    def __init__(self, path, offset, length):
        self.f = open(path, "rb")
        self.f.seek(offset)
        self.left = length

    def read(self, n=-1):
        if n is None or n < 0 or n > self.left:
            n = self.left
        b = self.f.read(n)
        self.left -= len(b)
        return b

    def close(self):
        self.f.close()


def _ancestors(name):
    parts = name.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _greedy(items, limit):
    """Consecutive items cut into groups of about `limit` bytes (cost = content + one tar header); an item over it is a group alone."""
    cut, cur = [[]], 0
    for item, cost in items:
        if cut[-1] and cur + cost > limit:
            cut.append([])
            cur = 0
        cut[-1].append(item)
        cur += cost
    return cut


def _partition(members, limit):
    """members -> (parts, chunk parts, split files). Files and symlinks are cut in walk order at about `limit` bytes of content,
    and every part carries the directory entries above its own members, so it unpacks alone. A regular file larger than
    `limit` is not in them: it is cut into chunks of `limit`, and the chunks are grouped into parts of their own (item
    ("chunk", path, member, offset, bytes)). The split files come back as dicts without their chunks' part names."""
    dirs, small, big = {}, [], []
    for path, name in members:
        if path.is_dir() and not path.is_symlink():
            dirs[name] = path
        elif path.is_file() and not path.is_symlink() and path.lstat().st_size > limit:
            big.append((path, name))
        else:
            size = path.lstat().st_size if path.is_file() and not path.is_symlink() else 0
            small.append(((path, name), size + TAR_HEADER))
    cut = _greedy(small, limit)
    parts, covered = [], []
    for items in cut:
        need = {d for _, n in items for d in _ancestors(n) if d in dirs} | {n for _, n in items if n in dirs}
        covered.extend(need)
        parts.append([(dirs[d], d) for d in sorted(need)] + items)
    rest = [(dirs[d], d) for d in sorted(dirs) if d not in covered]      # empty subtrees, and the directories above split files, go with the first part
    parts[0] = rest + parts[0]
    files, chunks = [], []
    for i, (path, name) in enumerate(big):
        st = path.lstat()
        files.append({"path": name, "mode": st.st_mode & 0o7777, "mtime": int(st.st_mtime), "bytes": st.st_size, "src": path})
        for k, off in enumerate(range(0, st.st_size, limit)):
            n = min(limit, st.st_size - off)
            chunks.append((("chunk", path, "f%04d.c%03d" % (i, k), off, n, i), n + TAR_HEADER))
    return parts, _greedy(chunks, limit) if chunks else [], files


def _write_part(out, name, items, level):
    """One part: a tar of `items` streamed into zstd. A hard link can only point inside its own part."""
    dest = pathlib.Path(out) / name
    proc = subprocess.Popen([_zstd(), "-q", "-f", "-%d" % level, "-T1", "-o", str(dest)], stdin=subprocess.PIPE)
    raw = 0
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for item in items:
                if item[0] == "chunk":
                    _, path, member, off, n, _fi = item
                    ti = tarfile.TarInfo(member)
                    ti.size, ti.mode = n, 0o600
                    src = _Range(path, off, n)
                    try:
                        tar.addfile(ti, src)
                    finally:
                        src.close()
                    raw += n
                    continue
                path, arc = item
                tar.add(str(path), arcname=arc, recursive=False)
                if path.is_file() and not path.is_symlink():
                    raw += path.stat().st_size
        proc.stdin.close()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.wait() != 0:
        raise SystemExit("runtime: zstd failed on %s" % name)
    return {"name": name, "bytes": dest.stat().st_size, "sha256": _sha_file(dest), "raw": raw}


def oversize(parts, cap=OBJECT_CAP):
    """The parts whose packed size passes `cap` (0: no cap). With split files only a --part-bytes above the cap can make one."""
    return [p["name"] for p in parts if cap and p["bytes"] > cap]


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
    cut, chunk_cut, files = _partition(list(_members(root, comfy_rel)), a.part_bytes)
    allcut = cut + chunk_cut
    names = ["runtime.%03d.tar.zst" % i for i in range(len(allcut))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(allcut)))) as ex:
        hashes = list(ex.map(lambda f: _sha_file(f["src"]), files))                  # the whole split file's sha256: apply's judge
        made = list(ex.map(lambda t: _write_part(out, t[0], t[1], a.level), zip(names, allcut)))
    for p in made[len(cut):]:
        p["kind"] = CHUNKS
    for f, h in zip(files, hashes):
        f["sha256"], f["chunks"] = h, []
        del f["src"]
    for name, items in zip(names[len(cut):], chunk_cut):
        for _, _path, member, off, n, fi in items:
            files[fi]["chunks"].append({"part": name, "member": member, "offset": off, "bytes": n})
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
        "cuda": a.torch_cuda, "driver_min": str(floor), "apt": [x for x in (a.apt or "").split(",") if x],
        "comfyui": {"dir": comfy_rel, "commit": _git(root / comfy_rel, "rev-parse", "HEAD"), "version": version},
        "python": a.python or "", "venv": "%s/.venv-cu130" % comfy_rel if (root / comfy_rel / ".venv-cu130").exists() else "",
        "packs": packs, "sageattention": {"key": a.sage_key or ""},
        "paths": _paths(root), "exclude": ["%s/%s" % (comfy_rel, x) for x in sorted(EXCLUDE_TOP)],
        "parts": made, "files": files,
    }
    (out / "runtime.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print("runtime: %d part(s) (%d of chunks, %d split file(s)), %d bytes packed (%d unpacked), %d pack(s) -> %s" % (
        len(made), len(chunk_cut), len(files), sum(p["bytes"] for p in made), sum(p["raw"] for p in made), len(packs), out / "runtime.json"))
    big = oversize(made, a.max_packed)
    if big:
        raise SystemExit("runtime: %d part(s) pass %d bytes packed (--max-packed): %s. Lower --part-bytes (now %d)." % (len(big), a.max_packed, ", ".join(big), a.part_bytes))
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
    """{name: url} from the per-part links given beside a local manifest: the site's entitlement `runtime` object as it
    comes ({id, manifest: {url, sha256, bytes}, parts: [{name, url, sha256, bytes}]}) or a bare list of parts. A sha256 or
    size the links state is only a cross-check: it must be the manifest's, and the manifest's values judge the bytes."""
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


def _check_links_manifest(manifest, parts_file):
    """The entitlement names the manifest's own sha256 and size: the file in hand must be that one."""
    data = json.loads(pathlib.Path(parts_file).read_text(encoding="utf-8"))
    ref = data.get("manifest") if isinstance(data, dict) else None
    if not isinstance(ref, dict):
        return
    raw = pathlib.Path(manifest).read_bytes()
    if ref.get("sha256") and ref["sha256"] != hashlib.sha256(raw).hexdigest():
        raise SystemExit("runtime: the manifest is not the one the link list names (sha256 differs)")
    if ref.get("bytes") and ref["bytes"] != len(raw):
        raise SystemExit("runtime: the manifest is not the one the link list names (%d bytes, it names %s)" % (len(raw), ref["bytes"]))


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
        why = _scrub((r.stderr or "").strip().splitlines()[-1] if r.stderr.strip() else "")
        hint = " (a signed link lasts about ten minutes: it may have expired)" if r.returncode == 22 and ("403" in why or "401" in why) else ""
        raise RuntimeError("curl exit %d: %s%s" % (r.returncode, why, hint))


def _unpack(part, root):
    """zstd -dc part | tar -x -C root, both exit codes judged."""
    try:
        zbin = _zstd(*ZSTD_WAIT)
    except SystemExit as e:
        raise RuntimeError(str(e))
    z = subprocess.Popen([zbin, "-dc", "-q", str(part)], stdout=subprocess.PIPE)
    try:
        t = subprocess.run(["tar", "-xf", "-", "-C", str(root)], stdin=z.stdout, capture_output=True, text=True)
    finally:
        z.stdout.close()
        zrc = z.wait()
    if zrc != 0 or t.returncode != 0:
        raise RuntimeError("unpack failed (zstd %d, tar %d) %s" % (zrc, t.returncode, (t.stderr or "").strip()[-300:]))


def _plan(m, names):
    """The manifest's split files, checked: -> (files, chunk map {(part, member): (file index, offset, bytes)}, chunks per file)."""
    files = m.get("files", []) if m.get("format") == 3 else []
    cmap, count = {}, []
    for i, f in enumerate(files):
        path = f.get("path", "")
        if not path or os.path.isabs(path) or ".." in pathlib.PurePosixPath(path).parts:
            raise SystemExit("runtime: a split file has the path %r, which is not inside the machine's root" % path)
        total = 0
        for c in sorted(f.get("chunks", []), key=lambda c: c["offset"]):
            if c["part"] not in names:
                raise SystemExit("runtime: file %s has a chunk in part %s, which the manifest does not list" % (path, c["part"]))
            if c["offset"] != total:
                raise SystemExit("runtime: file %s: its chunks leave a gap or overlap at byte %d" % (path, total))
            total += c["bytes"]
            cmap[(c["part"], c["member"])] = (i, c["offset"], c["bytes"])
        if total != f.get("bytes") or not f.get("chunks"):
            raise SystemExit("runtime: file %s: its chunks add up to %d bytes, the manifest says %s" % (path, total, f.get("bytes")))
        count.append(len(f["chunks"]))
    return files, cmap, count


def _prepare_files(files, root):
    """Every split file created at its full size, empty, before any chunk lands (each chunk part writes its own range)."""
    for f in files:
        target = pathlib.Path(root) / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        with open(target, "wb") as fh:
            fh.truncate(f["bytes"])


def _unpack_chunks(local, part_name, cmap, files, root):
    """A chunks part: every member written into its split file at its offset (pwrite), no copy to join them. -> the file index of each chunk."""
    try:
        zbin = _zstd(*ZSTD_WAIT)
    except SystemExit as e:
        raise RuntimeError(str(e))
    z = subprocess.Popen([zbin, "-dc", "-q", str(local)], stdout=subprocess.PIPE)
    seen, unknown = [], []
    try:
        with tarfile.open(fileobj=z.stdout, mode="r|") as tar:
            for member in tar:
                key = (part_name, member.name)
                if key not in cmap:
                    unknown.append(member.name)      # reported after the missing chunk it usually means, which names the file
                    continue
                i, off, n = cmap[key]
                path = files[i]["path"]
                if member.size != n:
                    raise RuntimeError("file %s: chunk %s is %d bytes, the manifest says %d" % (path, member.name, member.size, n))
                src = tar.extractfile(member)
                fd = os.open(str(pathlib.Path(root) / path), os.O_WRONLY)
                try:
                    pos = 0
                    while pos < n:
                        b = src.read(min(BLOCK, n - pos))
                        if not b:
                            raise RuntimeError("file %s: chunk %s ends after %d of %d bytes" % (path, member.name, pos, n))
                        os.pwrite(fd, b, off + pos)
                        pos += len(b)
                finally:
                    os.close(fd)
                seen.append(key)
    finally:
        z.stdout.close()
        zrc = z.wait()
    if zrc != 0:
        raise RuntimeError("unpack failed (zstd %d)" % zrc)
    missing = [k for k in cmap if k[0] == part_name and k not in seen]
    if missing:
        raise RuntimeError("file %s: chunk %s is not in this part" % (files[cmap[missing[0]][0]]["path"], missing[0][1]))
    if unknown:
        raise RuntimeError("holds a member %r that no file's chunk list names" % unknown[0])
    return [cmap[k][0] for k in seen]


def _finish_file(f, root):
    """A split file whose chunks are all in: its sha256 against the manifest's, then its mode and mtime. -> seconds."""
    t0 = time.time()
    target = pathlib.Path(root) / f["path"]
    if _sha_file(target) != f["sha256"]:
        raise RuntimeError("does not match its sha256 once its %d chunk(s) are written" % len(f["chunks"]))
    os.chmod(target, f["mode"])
    os.utime(target, (f["mtime"], f["mtime"]))
    return time.time() - t0


def _one(part, root, work, files, cmap):
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
    done = []
    if part.get("kind") == CHUNKS:
        done = _unpack_chunks(local, name, cmap, files, root)
    else:
        _unpack(local, root)
    t3 = time.time()
    if "://" in part["where"]:
        local.unlink()
    return {"name": name, "fetch": t1 - t0, "verify": t2 - t1, "unpack": t3 - t2, "chunks": done}


def apply(a):
    """Every part, `jobs` at a time: fetched, verified and unpacked as it lands, the largest first. A split file's chunks are
    written into it by whichever parts hold them, and the file is checked whole once its last chunk is in. Nothing is stamped
    here: the caller does that on a zero exit. A failed part or file stops the rest and is named (never its link)."""
    m = _load(a.manifest)
    if a.parts:
        _check_links_manifest(a.manifest, a.parts)
    if m.get("format") not in ACCEPTED or m.get("compression") != "zstd":
        raise SystemExit("runtime: %s is runtime format %s, this base reads formats 2 and 3 (zstd parts): capture it again" % (a.manifest, m.get("format")))
    rows = sorted(_resolve(m, a.location, a.parts), key=lambda r: -r.get("bytes", 0))
    files, cmap, remaining = _plan(m, {r["name"] for r in rows})
    root, work = pathlib.Path(a.root), pathlib.Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    global ZSTD_WAIT
    ZSTD_WAIT = (a.zstd_wait, a.zstd_gate)
    if not a.zstd_wait:
        _zstd()
    t0 = time.time()
    root.mkdir(parents=True, exist_ok=True)
    _prepare_files(files, root)
    done, failed, file_secs = [], None, 0.0
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(rows) or 1)))
    futs = {ex.submit(_one, r, root, work, files, cmap): ("part", r["name"]) for r in rows}
    try:
        while futs and not failed:
            finished, _ = concurrent.futures.wait(list(futs), return_when=concurrent.futures.FIRST_COMPLETED)
            for f in finished:
                kind, name = futs.pop(f)
                try:
                    res = f.result()
                except Exception as e:            # noqa: BLE001 - every cause is reported by part or file name
                    failed = "%s %s: %s" % (kind, name, _scrub(str(e)))
                    break
                if kind == "file":
                    file_secs += res
                    continue
                done.append(res)
                for i in res["chunks"]:
                    remaining[i] -= 1
                    if remaining[i] == 0:        # the last chunk is in: the whole file is judged at once, beside the other parts
                        futs[ex.submit(_finish_file, files[i], root)] = ("file", files[i]["path"])
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    if failed:
        print("runtime: %s" % failed, file=sys.stderr)
        return 1
    t1 = time.time()
    wall = max(t1 - t0, 1e-6)
    packed, raw = sum(r.get("bytes", 0) for r in rows), sum(r.get("raw", 0) for r in rows)
    res = {"t0": int(t0), "t1": int(t1 + 0.999), "seconds": round(wall, 2), "parts": len(rows), "jobs": a.jobs, "bytes": packed, "raw": raw,
           "gbps": round(packed / wall / 1e9, 3), "fetch_seconds": round(sum(d["fetch"] for d in done), 1),
           "verify_seconds": round(sum(d["verify"] for d in done), 1), "unpack_seconds": round(sum(d["unpack"] for d in done), 1),
           "split_files": len(files), "file_seconds": round(file_secs, 1)}
    if a.result:
        pathlib.Path(a.result).write_text(json.dumps(res) + "\n", encoding="utf-8")
    print("runtime: %d part(s), %.2f GB packed (%.2f GB unpacked) in %.1f s = %.2f GB/s, %d at once (busy: fetch %.0f s, verify %.0f s, unpack %.0f s%s)" % (
        len(rows), packed / 1e9, raw / 1e9, wall, res["gbps"], a.jobs, res["fetch_seconds"], res["verify_seconds"], res["unpack_seconds"],
        ("; %d split file(s) joined and verified, %.0f s" % (len(files), file_secs)) if files else ""))
    return 0


def apt(a):
    for name in _load(a.manifest).get("apt", []):
        print(name)
    return 0


def result_add(a):
    f = pathlib.Path(a.file)
    r = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    try:
        r[a.key] = json.loads(a.value)
    except ValueError:
        r[a.key] = a.value
    f.write_text(json.dumps(r) + "\n", encoding="utf-8")
    return 0


def ledger_line(a):
    r = json.loads(pathlib.Path(a.file).read_text(encoding="utf-8"))
    extra = ""
    if "apt_seconds" in r:
        extra = "; apt %ss%s" % (r["apt_seconds"], (": " + " ".join(r["apt_installed"])) if r.get("apt_installed") else ", nothing missing")
    if r.get("split_files"):
        extra = "; %d split file(s) verified, %ss%s" % (r["split_files"], r.get("file_seconds", 0), extra)
    print("%s\t%s\t%s\t%s: %d parts, %.2f GB packed (%.2f GB unpacked), %.2f GB/s, %d at once (busy: fetch %ss, verify %ss, unpack %ss%s)" % (
        r["t0"], r["t1"], round(r["seconds"]), a.label, r["parts"], r["bytes"] / 1e9, r["raw"] / 1e9, r["gbps"], r["jobs"],
        r["fetch_seconds"], r["verify_seconds"], r["unpack_seconds"], extra))
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
    b.add_argument("--max-packed", type=int, default=OBJECT_CAP, help="fail when a packed part passes this many bytes (0: no limit)")
    b.add_argument("--url-base", default="")
    b.add_argument("--torch-cuda", required=True)
    b.add_argument("--apt", default="", help="comma-separated apt packages the runtime needs on a machine that skips the OS stage")
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
    ap_.add_argument("--zstd-wait", type=int, default=0)
    ap_.add_argument("--zstd-gate")
    t_ = sub.add_parser("apt")
    t_.add_argument("manifest")
    r_ = sub.add_parser("result-add")
    r_.add_argument("file")
    r_.add_argument("key")
    r_.add_argument("value")
    l_ = sub.add_parser("ledger-line")
    l_.add_argument("file")
    l_.add_argument("--label", default="RUNTIME fetch and unpack")
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
    return {"build": build, "verify-parts": verify_parts, "sources": sources, "apply": apply, "apt": apt, "result-add": result_add, "ledger-line": ledger_line, "check-tree": check_tree,
            "covers": covers, "field": field}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
