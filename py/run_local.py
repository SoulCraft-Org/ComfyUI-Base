#!/usr/bin/env python3
"""ComfyUI's code on the machine's own disk (3.15.0): the volume stays the truth, the server runs from a local copy.

    run_local.py sync --root R --tree COMFY --venv VENV --state STATE [--python-root P] [--jobs N] [--big BYTES]
                      [--headroom BYTES] [--auto] [--check-import MOD]
    run_local.py warm --tree COMFY --venv VENV [--python-root P] [--jobs N] [--big BYTES] [--auto]
                                      read every file the copy would hold, in parallel, so the volume's own page cache
                                      has it: what a start from the VOLUME (no copy) imports then reads from memory
    run_local.py prewarm <sync's arguments> [--warm-jobs N]
                                      sync, then the warm read when no copy came of it (no room, an error): the head
                                      start the boot and an install run in the background, as ONE process
    run_local.py fstype <path>        the filesystem type of the mount holding <path> (/proc/self/mountinfo)
    run_local.py status --root R      the current generation and its numbers

WHY. On a RunPod network volume every file access is a FUSE round trip. The venv is tens of thousands of small files
and torch's libraries are read page by page through mmap, so `main.py --quick-test-for-ci` MEASURED 5+ minutes at
near-zero CPU, 262 of 265 threads waiting and one in request_wait_answer. A copy pays each file's latency once, in
parallel, and every start after that reads the container disk.

WHAT. A generation is `R/<gen>/<absolute source path>`: the ComfyUI tree minus its data (models/ input/ output/ user/
temp/ become symlinks to the volume's), venv backups left out, every `.git` a symlink to the volume's own, a file of
--big bytes or more outside the venv (weights a pack keeps beside its code) a symlink to the volume's; the venv whole;
the uv Python whole. A file whose size and mtime match the previous generation's manifest is HARDLINKED from it, so an
incremental sync costs one parallel stat walk plus the bytes that changed, and nothing is ever written into a
generation a server may be running from. The venv's own paths are rewritten in the copy (bin/ symlinks, pyvenv.cfg,
shebangs, *.pth, editable finders), and the copy's python must import --check-import with sys.prefix inside the
generation before `R/current` points at it.

FRESH WITHOUT A WALK. The fingerprint is state/run.gen (the install bumps it whenever the code may have changed) plus
the mtimes of the tree, custom_nodes and each pack folder, extra_model_paths.yaml, the venv, its stamp, pyvenv.cfg and
site-packages, and the Python root. Same fingerprint, complete generation: nothing is read beyond those stats.

The last stdout line is the verdict for the shell: `RUN_LOCAL <verdict> <tree> <python> <seconds> [reason]`, verdict one
of fresh | synced | skip | noroom | failed; tree and python are `-` unless the verdict is fresh or synced.
Standard library only: the boot runs this with the machine's own python3, before any venv is trusted.
"""
import argparse
import concurrent.futures as cf
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

FORMAT = 1
DATA = ("models", "input", "output", "user", "temp")           # the tree's data: on the volume, always
REMOTE = ("fuse", "nfs", "cifs", "smb", "ceph", "glusterfs", "lustre", "9p", "afs", "beegfs", "gpfs", "wekafs")
META = ".run-local.json"
MANIFEST = ".manifest.tsv"


def say(msg):
    print("  " + msg, flush=True)


LAST = {"verdict": ""}


def verdict(v, tree="-", py="-", secs=0.0, why=""):
    LAST["verdict"] = v
    print("RUN_LOCAL %s %s %s %.1f%s" % (v, tree, py, secs, (" " + why) if why else ""), flush=True)


# ---------------------------------------------------------------------------------------------------- the filesystem
def fstype(path, mountinfo="/proc/self/mountinfo"):
    """The fstype of the longest mount point holding path; '' where /proc/self/mountinfo is absent (macOS)."""
    try:
        real = os.path.realpath(path)
        best, kind = "", ""
        with open(mountinfo) as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                f = left.split()
                if len(f) < 5 or not right:
                    continue
                mp = f[4].replace("\\040", " ")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, kind = mp, right.split()[0]
        return kind
    except OSError:
        return ""


def is_remote(kind):
    """fuse and fuse.<daemon> are remote (RunPod's network volume is FUSE); fuseblk is a local disk through FUSE (ntfs-3g)."""
    head = kind.split(".", 1)[0]
    return head in REMOTE or head.startswith(("nfs", "smb"))


def free_bytes(path):
    s = os.statvfs(path)
    return s.f_bavail * s.f_frsize


# ---------------------------------------------------------------------------------------------------- the sources
class Plan:
    """The mirrored roots and the rules for each path under them."""

    def __init__(self, tree, venv, python_root, big):
        self.tree = tree.rstrip("/")
        self.venv = venv.rstrip("/")
        self.big = big
        self.roots = [self.tree]
        if not self._under(self.venv, self.tree):
            self.roots.append(self.venv)
        self.python_root = self._python_root(python_root)
        if self.python_root and not any(self._under(self.python_root, r) for r in self.roots):
            self.roots.append(self.python_root)
        self.venv_name = os.path.basename(self.venv)

    @staticmethod
    def _under(p, root):
        return p == root or p.startswith(root + "/")

    def _python_root(self, given):
        """The interpreter install the venv runs on, when it is on the volume: the root given (the base's uv
        Python dir) when pyvenv.cfg's home is under it; nothing when home is the machine's own (already local)."""
        home = ""
        try:
            with open(os.path.join(self.venv, "pyvenv.cfg")) as fh:
                for line in fh:
                    k, _, v = line.partition("=")
                    if k.strip() == "home":
                        home = v.strip()
        except OSError:
            return ""
        given = (given or "").rstrip("/")
        if given and home and self._under(home, given):
            return given
        return ""

    def mapped(self, p):
        """True when p is something the generation holds a copy of (not the tree's data, not a mirrored root's outside)."""
        if not any(self._under(p, r) for r in self.roots):
            return False
        if self._under(p, self.tree):
            rel = p[len(self.tree) + 1:]
            if rel.split("/", 1)[0] in DATA:
                return False
        return True

    def rule(self, path, is_dir, size):
        """'copy' | 'skip' | 'srclink' (a symlink to the volume's own path in the copy)."""
        name = os.path.basename(path)
        if name == ".git":
            return "srclink"
        if self._under(path, self.tree) and os.path.dirname(path) == self.tree:
            if name in DATA:
                return "srclink"
            if name.startswith(".venv") and path != self.venv:
                return "skip"                       # another venv or our own backup ($VENV.pre-<who>-<ts>)
        in_venv = self._under(path, self.venv)
        if not is_dir and not in_venv and size >= self.big:
            return "srclink"                        # a model file a pack keeps beside its code
        return "copy"

    def fixup(self, path):
        """The files whose CONTENT names the venv's own paths: always copied fresh, then rewritten."""
        if not self._under(path, self.venv):
            return False
        rel = path[len(self.venv) + 1:]
        if rel == "pyvenv.cfg":
            return True
        if rel.startswith("bin/") and rel.count("/") == 1:
            return True                             # shebangs (checked by content)
        m = re.match(r"lib/python[0-9.]+t?/site-packages/([^/]+)$", rel)
        return bool(m and (m.group(1).endswith(".pth") or m.group(1).startswith("__editable__")))


def walk(plan, jobs):
    """Every entry under every root: {abs path: (kind, size, mtime_ns, mode, link)}; kind d|f|l|s (s = srclink)."""
    out = {}
    errors = []

    def scan(d):
        subs, rows = [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    p = e.path
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError as ex:
                        errors.append("%s: %s" % (p, ex))
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        r = plan.rule(p, False, 0)
                        if r == "skip":
                            continue
                        rows.append((p, ("s" if r == "srclink" else "l"), 0, st.st_mtime_ns, 0, os.readlink(p)))
                    elif stat.S_ISDIR(st.st_mode):
                        r = plan.rule(p, True, 0)
                        if r == "skip":
                            continue
                        if r == "srclink":
                            rows.append((p, "s", 0, st.st_mtime_ns, 0, ""))
                        else:
                            rows.append((p, "d", 0, st.st_mtime_ns, stat.S_IMODE(st.st_mode), ""))
                            subs.append(p)
                    elif stat.S_ISREG(st.st_mode):
                        r = plan.rule(p, False, st.st_size)
                        if r == "skip":
                            continue
                        rows.append((p, ("s" if r == "srclink" else "f"), st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode), ""))
                    # sockets, fifos, devices: nothing a server imports
        except OSError as ex:
            errors.append("%s: %s" % (d, ex))
        return subs, rows

    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        pending = set()
        for r in plan.roots:
            st = os.stat(r)
            out[r] = ("d", 0, st.st_mtime_ns, stat.S_IMODE(st.st_mode), "")
            pending.add(ex.submit(scan, r))
        while pending:
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for f in done:
                subs, rows = f.result()
                for row in rows:
                    out[row[0]] = row[1:]
                for s in subs:
                    pending.add(ex.submit(scan, s))
    if errors:
        raise RuntimeError("unreadable on the volume: " + "; ".join(errors[:5]))
    return out


# ---------------------------------------------------------------------------------------------------- the fingerprint
def fingerprint(plan, state):
    items = {"format": FORMAT, "roots": plan.roots}
    try:
        with open(os.path.join(state, "run.gen")) as fh:
            items["gen"] = fh.read().strip()
    except OSError:
        items["gen"] = ""
    probes = [plan.tree, os.path.join(plan.tree, "custom_nodes"), os.path.join(plan.tree, "extra_model_paths.yaml"),
              os.path.join(plan.tree, "main.py"), plan.venv, os.path.join(plan.venv, "pyvenv.cfg"),
              os.path.join(plan.venv, ".comfy-base-venv"), os.path.join(plan.venv, "bin")]
    lib = os.path.join(plan.venv, "lib")
    try:
        for d in sorted(os.listdir(lib)):
            probes.append(os.path.join(lib, d, "site-packages"))
    except OSError:
        pass
    try:
        with os.scandir(os.path.join(plan.tree, "custom_nodes")) as it:
            for e in it:
                probes.append(e.path)
                probes.append(os.path.join(e.path, ".git", "HEAD"))
    except OSError:
        pass
    if plan.python_root:
        probes.append(plan.python_root)
    stamps = []
    for p in sorted(probes):
        try:
            st = os.lstat(p)
            stamps.append("%s %d %d" % (p, st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append("%s -" % p)
    items["stats"] = stamps
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------- generations
def read_meta(gdir):
    try:
        with open(os.path.join(gdir, META)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_manifest(gdir):
    rows = {}
    try:
        with open(os.path.join(gdir, MANIFEST)) as fh:
            for line in fh:
                p, kind, size, mt = line.rstrip("\n").split("\t")[:4]
                rows[p] = (kind, int(size), int(mt))
    except (OSError, ValueError):
        return {}
    return rows


def current(root):
    try:
        g = os.readlink(os.path.join(root, "current"))
    except OSError:
        return ""
    gdir = os.path.join(root, g)
    return gdir if os.path.isdir(gdir) else ""


def in_use(root):
    """Generations a process runs from (its cwd or its executable): never deleted under it."""
    used = set()
    root = root.rstrip("/") + "/"
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None                                                  # cannot tell: delete nothing but what we know
    for pid in pids:
        for what in ("cwd", "exe"):
            try:
                t = os.readlink("/proc/%s/%s" % (pid, what))
            except OSError:
                continue
            if t.startswith(root):
                used.add(t[len(root):].split("/", 1)[0])
    return used


def prune(root, keep):
    used = in_use(root)
    gone = 0
    for g in os.listdir(root):
        gdir = os.path.join(root, g)
        if g in keep or not g.startswith("g") or not os.path.isdir(gdir) or os.path.islink(gdir):
            continue
        if used is None or g in used:
            continue
        shutil.rmtree(gdir, ignore_errors=True)
        gone += 1
    return gone


# ---------------------------------------------------------------------------------------------------- the copy
class Relocator:
    """Source paths to the generation's, in symlink targets and in the fix-up files' text."""

    def __init__(self, plan, gdir):
        self.plan, self.gdir = plan, gdir
        alts = "|".join(re.escape(r) for r in sorted(plan.roots, key=len, reverse=True))
        # a root only where its name ends: /workspace/ComfyUI never matches inside /workspace/ComfyUI-old
        self.rx = re.compile(r"(?<![\w./-])((?:%s)(?=[/\s'\"\n:;]|$)(?:/[^\s'\"\n:;]*)?)" % alts)

    def local(self, p):
        return self.gdir + p

    def target(self, t):
        return self.local(t) if os.path.isabs(t) and self.plan.mapped(os.path.normpath(t)) else t

    def text(self, s):
        return self.rx.sub(lambda m: self.local(m.group(1)) if self.plan.mapped(m.group(1).rstrip("/")) else m.group(1), s)


def build(plan, entries, gdir, prev, prev_rows, jobs):
    """Make the generation: dirs, then files and links in parallel. Returns (copied files, copied bytes, linked)."""
    rel = Relocator(plan, gdir)
    dirs = sorted((p for p, e in entries.items() if e[0] == "d"), key=lambda p: p.count("/"))
    for p in dirs:
        os.makedirs(rel.local(p), exist_ok=True)
    counts = {"copied": 0, "bytes": 0, "linked": 0}

    def one(p):
        kind, size, mt, mode, link = entries[p]
        dst = rel.local(p)
        if kind == "s":
            os.symlink(p, dst)
        elif kind == "l":
            os.symlink(rel.target(link), dst)
        elif kind == "f":
            fix = plan.fixup(p)
            old = prev_rows.get(p)
            if prev and not fix and old and old[0] == "f" and old[1] == size and old[2] == mt:
                try:
                    os.link(prev + p, dst)
                    return "linked", 0
                except OSError:
                    pass                                             # gone or another device: copy it
            shutil.copyfile(p, dst)
            os.chmod(dst, mode | stat.S_IRUSR | stat.S_IWUSR)
            if fix:
                fixup_file(rel, dst)
            os.utime(dst, ns=(mt, mt))
            return "copied", size
        return "", 0

    # the largest files first, so the long reads (torch's libraries) start at once and the small ones fill the gaps
    work = sorted((p for p, e in entries.items() if e[0] != "d"), key=lambda p: -entries[p][1])
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        for what, n in ex.map(one, work):
            if what:
                counts[what] += 1
                counts["bytes"] += n
    for p in reversed(dirs):                                         # the source's modes, once nothing more is written
        os.chmod(rel.local(p), entries[p][3] | stat.S_IRWXU)
    return counts


def fixup_file(rel, dst):
    try:
        with open(dst, "rb") as fh:
            head = fh.read(2)
            if os.path.basename(dst) != "pyvenv.cfg" and not dst.endswith((".pth", ".py")) and head != b"#!":
                return                                               # a binary in bin/: nothing to rewrite
            fh.seek(0)
            raw = fh.read(4 << 20)
        s = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return
    new = rel.text(s)
    if new != s:
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(new)


def check_import(py, venv_local, mod):
    if mod == "-":
        return ""                                                    # the suite's fake interpreters only: never on a machine
    code = "import sys\nimport %s\nprint(sys.prefix)" % mod if mod else "import sys\nprint(sys.prefix)"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    try:
        r = subprocess.run([py, "-I", "-c", code], capture_output=True, text=True, timeout=600, env=env)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return "could not run %s: %s" % (py, ex)
    if r.returncode != 0:
        return "the copy cannot import %s: %s (%s)" % (mod or "sys", (r.stderr.strip().splitlines() or ["?"])[-1][:200], py)
    prefix = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if os.path.realpath(prefix) != os.path.realpath(venv_local):
        return "the copy's python reports sys.prefix %s, not %s" % (prefix, venv_local)
    return ""


def sync(a):
    t0 = time.time()
    root = a.root.rstrip("/")
    if a.auto:
        kind = fstype(a.tree)
        if not is_remote(kind):
            verdict("skip", why="%s is on %s, not a network filesystem: it runs in place" % (a.tree, kind or "a local disk"))
            return 0
    plan = Plan(os.path.realpath(a.tree), os.path.realpath(a.venv) if os.path.exists(a.venv) else a.venv, a.python_root, a.big)
    if not os.path.isfile(os.path.join(plan.venv, "bin", "python")) and not os.path.islink(os.path.join(plan.venv, "bin", "python")):
        verdict("skip", why="no venv at %s yet" % plan.venv)
        return 0
    os.makedirs(root, exist_ok=True)
    lock = open(os.path.join(root, ".lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        fp = fingerprint(plan, a.state)
        cur = current(root)
        meta = read_meta(cur) if cur else {}
        if cur and meta.get("complete") and meta.get("fingerprint") == fp:
            vpy = cur + plan.venv + "/bin/python"
            if os.path.exists(vpy):
                verdict("fresh", cur + plan.tree, vpy, time.time() - t0)
                return 0
        say("copying ComfyUI's code to this machine's disk (%s): %s" % (root, " ".join(plan.roots)))
        entries = walk(plan, a.jobs)
        tw = time.time() - t0
        prev_rows = read_manifest(cur) if cur and meta.get("complete") else {}
        need = sum(e[1] for p, e in entries.items() if e[0] == "f" and not (
            p in prev_rows and prev_rows[p][1:] == (e[1], e[2]) and not plan.fixup(p)))
        free = free_bytes(root)
        if need + a.headroom > free:
            prune(root, {os.path.basename(cur)} if cur else set())
            free = free_bytes(root)
        if need + a.headroom > free:
            verdict("noroom", secs=time.time() - t0, why="%.1f GB to copy + %.1f GB headroom, %.1f GB free on %s" % (
                need / 1e9, a.headroom / 1e9, free / 1e9, root))
            return 0
        g = "g%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())
        gdir = os.path.join(root, g)
        try:
            n = build(plan, entries, gdir, cur if prev_rows else "", prev_rows, a.jobs)
            vpy = gdir + plan.venv + "/bin/python"
            why = check_import(vpy, gdir + plan.venv, a.check_import)
            if why:
                raise RuntimeError(why)
            with open(os.path.join(gdir, MANIFEST), "w") as fh:
                for p, e in entries.items():
                    fh.write("%s\t%s\t%d\t%d\n" % (p, e[0], e[1], e[2]))
            secs = time.time() - t0
            with open(os.path.join(gdir, META), "w") as fh:
                json.dump({"format": FORMAT, "complete": True, "fingerprint": fp, "roots": plan.roots,
                           "files": len(entries), "copied": n["copied"], "copied_bytes": n["bytes"], "linked": n["linked"],
                           "walk_s": round(tw, 1), "seconds": round(secs, 1), "made": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
            tmp = os.path.join(root, ".current.tmp")
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(g, tmp)
            os.replace(tmp, os.path.join(root, "current"))
        except BaseException:
            shutil.rmtree(gdir, ignore_errors=True)
            raise
        gone = prune(root, {g})
        say("%d entries walked in %.1f s; %d files copied (%.2f GB), %d linked from the previous copy; %.1f s in all%s" % (
            len(entries), tw, n["copied"], n["bytes"] / 1e9, n["linked"], secs, (", %d old copy(ies) removed" % gone) if gone else ""))
        verdict("synced", gdir + plan.tree, vpy, secs)
        return 0
    except Exception as ex:                                          # never fatal: the caller runs from the volume
        verdict("failed", secs=time.time() - t0, why=str(ex).replace("\n", " ")[:600])
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def warm(a):
    """MEASURED by the platform orchestrator on a RunPod pod: reading the whole venv and custom_nodes 64 files at a time
    took 18 s, and the imports after it took seconds instead of minutes. For a start that runs from the volume."""
    t0 = time.time()
    if a.auto:
        kind = fstype(a.tree)
        if not is_remote(kind):
            print("WARM skip %s is on %s" % (a.tree, kind or "a local disk"), flush=True)
            return 0
    try:
        plan = Plan(os.path.realpath(a.tree), os.path.realpath(a.venv), a.python_root, a.big)
        entries = walk(plan, a.jobs)
    except Exception as ex:
        print("WARM failed %s" % str(ex).replace("\n", " ")[:300], flush=True)
        return 0
    files = [p for p, e in entries.items() if e[0] == "f"]
    local = threading.local()
    total = [0]
    lock = threading.Lock()

    def one(p):
        buf = getattr(local, "buf", None)
        if buf is None:
            buf = local.buf = bytearray(1 << 20)
        n = 0
        try:
            with open(p, "rb", buffering=0) as fh:
                while True:
                    k = fh.readinto(buf)
                    if not k:
                        break
                    n += k
        except OSError:
            return
        with lock:
            total[0] += n

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(one, files))
    print("WARM done %d files %.2f GB %.1f s" % (len(files), total[0] / 1e9, time.time() - t0), flush=True)
    return 0


def prewarm(a):
    """One process, so a background head start is never a forked shell carrying its parent's command line (a bash
    subshell shows its parent's argv to ps, and the suite's guard against a stray forwarder matched one in CI)."""
    print("%s head start" % time.strftime("%H:%M:%S"), flush=True)
    sync(a)
    v = LAST["verdict"]
    if v not in ("fresh", "synced", "skip"):                         # skip: the volume is a local disk, nothing to warm
        a.jobs = a.warm_jobs
        a.auto = True
        warm(a)
    print("%s head start done: %s" % (time.strftime("%H:%M:%S"), v or "?"), flush=True)
    return 0


def status(a):
    cur = current(a.root)
    if not cur:
        print("no local copy at %s" % a.root)
        return 0
    m = read_meta(cur)
    print("local copy %s: %s files, %s copied (%.2f GB), %s linked, %s s, made %s" % (
        cur, m.get("files", "?"), m.get("copied", "?"), (m.get("copied_bytes") or 0) / 1e9, m.get("linked", "?"),
        m.get("seconds", "?"), m.get("made", "?")))
    return 0


def _sync_args(s):
    s.add_argument("--root", required=True)
    s.add_argument("--tree", required=True)
    s.add_argument("--venv", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--python-root", default="")
    s.add_argument("--jobs", type=int, default=32)
    s.add_argument("--big", type=int, default=64 << 20)
    s.add_argument("--headroom", type=int, default=4 << 30)
    s.add_argument("--auto", action="store_true")
    s.add_argument("--check-import", default="torch")


def main(argv):
    ap = argparse.ArgumentParser(prog="run_local.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    pw = sub.add_parser("prewarm")
    pw.add_argument("--warm-jobs", type=int, default=64)
    for p_ in (s, pw):
        _sync_args(p_)
    w = sub.add_parser("warm")
    w.add_argument("--tree", required=True)
    w.add_argument("--venv", required=True)
    w.add_argument("--python-root", default="")
    w.add_argument("--jobs", type=int, default=64)
    w.add_argument("--big", type=int, default=64 << 20)
    w.add_argument("--auto", action="store_true")
    f = sub.add_parser("fstype")
    f.add_argument("path")
    st = sub.add_parser("status")
    st.add_argument("--root", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fstype":
        print(fstype(a.path))
        return 0
    if a.cmd == "status":
        return status(a)
    if a.cmd == "warm":
        return warm(a)
    if a.cmd == "prewarm":
        return prewarm(a)
    return sync(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
