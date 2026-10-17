#!/usr/bin/env python3
"""A package's own node packs (VENDORED_PACKS, 3.2.0): the folders beside its script that ship in its zip.

    own_packs.py digest <dir>                          sha256 of the pack's content (paths and bytes)
    own_packs.py version <dir>                         the pyproject.toml [project] version, or "-"
    own_packs.py newer <a> <b>                         exit 0 when version a is newer than b
    own_packs.py newest <dir>...                       one folder per pack name, the newest version of each (3.20.2)
    own_packs.py mirror <src> <dst> <stage> <prev>     make <dst> exactly <src>: copy into <stage>, move the live
                                                       folder to <prev>, move the copy into place

The digest ignores __pycache__, .DS_Store and the base's own stamp (.comfy-base-own), so the same pack reads the same
wherever it sits. The mirror never builds anything inside custom_nodes: ComfyUI imports every folder there that is not
__pycache__ or *.disabled, dot-folders included, so the copy is made in <stage> (on the same volume, so each move is a
rename) and the live folder is replaced in two moves.
"""
import hashlib
import os
import re
import shutil
import sys

STAMP = ".comfy-base-own"
SKIP = {"__pycache__", ".DS_Store", STAMP}


def _files(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for f in sorted(files):
            if f not in SKIP and not f.endswith(".pyc"):
                yield os.path.join(d, f)


def digest(root):
    h = hashlib.sha256()
    for p in _files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        with open(p, "rb") as fh:
            body = hashlib.sha256(fh.read()).hexdigest()
        h.update(("%s\0%s\0" % (rel, body)).encode("utf-8"))
    return h.hexdigest()


def version(root):
    p = os.path.join(root, "pyproject.toml")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return "-"
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "-"


def _key(v):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", v))


def newer(a, b):
    try:
        return _key(a) > _key(b)
    except TypeError:          # 1.0 vs 1.0rc1: a numeric part against a text part; compare as text
        return a > b


def newest(dirs, note=None):
    """One folder per pack name (its basename), the newest version of each, in the order the names first appear: the
    testbed links the copy a pod would keep (lib/62-own-packs.sh: newest wins, never moved backwards), never just the
    last package walked. A copy with no version loses to any copy that has one; on the same version the first given
    stays, and two copies of one version with different content are named, since one of them missed a bump."""
    pick, order = {}, []
    for d in dirs:
        name = os.path.basename(d.rstrip("/"))
        if name not in pick:
            pick[name] = d
            order.append(name)
            continue
        have, cand = version(pick[name]), version(d)
        if cand != "-" and (have == "-" or newer(cand, have)):
            kept, dropped = d, pick[name]
            pick[name] = d
        else:
            kept, dropped = pick[name], d
            if cand == have:                  # the same version: the first stays; silent when the copies are identical
                if note and digest(d) != digest(pick[name]):
                    note("%s: %s and %s both say version %s with different content; one of them missed a version bump"
                         % (name, pick[name], d, cand))
                continue
        if note:
            note("%s: %s %s kept over %s %s" % (name, version(kept), kept, version(dropped), dropped))
    return [pick[n] for n in order]


def mirror(src, dst, stage, prev):
    name = os.path.basename(dst.rstrip("/"))
    new = os.path.join(stage, name)
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    shutil.copytree(src, new, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc", STAMP), symlinks=False)
    if os.path.lexists(dst):
        shutil.rmtree(prev, ignore_errors=True)
        os.makedirs(os.path.dirname(prev), exist_ok=True)
        shutil.move(dst, prev)
    shutil.move(new, dst)


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "digest":
        print(digest(argv[2]))
    elif cmd == "version":
        print(version(argv[2]))
    elif cmd == "newer" and len(argv) == 4:
        return 0 if newer(argv[2], argv[3]) else 1
    elif cmd == "newest":
        for d in newest(argv[2:], note=lambda m: print("  ·· " + m, file=sys.stderr)):
            print(d)
    elif cmd == "mirror" and len(argv) == 6:
        mirror(*argv[2:6])
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
