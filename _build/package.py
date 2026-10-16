#!/usr/bin/env python3
"""The one packager: builds every package's zip for its brand's host, and the base's own, byte-reproducibly.

    python3 "base/comfyui-base/_build/package.py" "<pkg dir>" ...    # rebuild those packages' zips
    python3 "base/comfyui-base/_build/package.py" --all               # every package of the consumer root (py/pkgdirs.py)
    python3 "$COMFY_BASE/_build/package.py" --all --root <workspace>  # ... of a named root ($BASE_WORKSPACE when not given)
    python3 "base/comfyui-base/_build/package.py" --base              # the base zip (writes MANIFEST.sha256 first)
    python3 "base/comfyui-base/_build/package.py" --host "<pkg dir>"  # print the host the package's zip is named for
    ... --check                                                  # verify without writing; exit 1 if stale

A package zip is flat: the workflow the script names in WF_NAME (644; none when WF_NAME=""), <name>-script.sh (700),
<name>-trainer.sh (700, if present), suite.py, pytest.ini, models.py and models.sha256 (644, if present), <name>-handbook.md (644, if present),
plus every file named in the script's ZIP_EXTRA=( ... ). It is named <name>-<host>.zip, where the host is the one the
package's brand declares in <brand>/brand.toml (py/brand.py; "runpod" when no brand.toml is above the package).

The base zip is comfyui-base.zip, one artefact for every host, carrying a top-level "comfyui-base/" folder with
lib/ py/ suite/ hosts/ walked recursively (hosts/<host>/*.sh at 700, everything else 644).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shlex
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "py"))
import brand                                                        # noqa: E402
import pkgdirs                                                      # noqa: E402  3.6.0: the one package-discovery rule

STAMP = (2026, 9, 4, 0, 0, 0)       # zip stores a local datetime, not an epoch; fixed so two builds give the same bytes
# LICENSE ships (2.12.2): MIT asks that the notice travel with every copy, and this zip is the copy the projects
# that build on the base hand to their own users.
BASE_MEMBERS = ["base.sh", "comfyui-base-script.sh", "comfyui-base-handbook.md", "VERSION", "pytest.ini", "LICENSE"]
BASE_DIRS = ["lib", "py", "suite", "hosts"]
BASE_ZIP = BASE / "comfyui-base.zip"


def workflow_name(pkg: Path, name: str) -> str | None:
    """The shipped workflow's file name: the script's WF_NAME (Title Case with the brand); None when
    the script declares WF_NAME="" (a package with no workflow yet); the old '<Name> Workflow.json' pattern only when
    the script declares no WF_NAME line at all."""
    m = re.search(r'^WF_NAME="([^"]*)"', (pkg / f"{name}-script.sh").read_text(encoding="utf-8"), re.M)
    if m is None:
        return f"{name} Workflow.json"
    return m.group(1) or None


def zip_host(pkg: Path) -> str:
    """Which venue a package's zip is for: the host its brand declares in brand.toml, else runpod."""
    return brand.host_of(pkg, "runpod")


def package_members(pkg: Path) -> list[tuple[str, int]]:
    name = pkg.name
    script = pkg / f"{name}-script.sh"
    if not script.exists():
        sys.exit(f"{pkg}: no '{name}-script.sh'")
    members = []
    wf = workflow_name(pkg, name)
    if wf:
        members.append((wf, 0o644))
    members.append((f"{name}-script.sh", 0o700))
    for fn, mode in ((f"{name}-trainer.sh", 0o700), ("suite.py", 0o644), ("pytest.ini", 0o644), ("models.py", 0o644), ("models.sha256", 0o644), (f"{name}-handbook.md", 0o644)):
        if (pkg / fn).exists():
            members.append((fn, mode))
    m = re.search(r"^ZIP_EXTRA=\((.*?)\)", script.read_text(encoding="utf-8"), re.M | re.S)
    if m:
        # shlex, not .split(): every other filename in this project has spaces in it, and a bare
        # whitespace split turned `ZIP_EXTRA=( "A B.json" )` into two members that do not exist —
        # the packager then failed with `missing: .../A`, naming a file nobody wrote. shlex is how
        # the shell itself reads that array, so a package's ZIP_EXTRA now means what it looks like.
        # Unquoted single-word entries parse identically, so every existing package is unaffected.
        try:
            extras = shlex.split(m.group(1), comments=False)
        except ValueError as e:                       # an unbalanced quote is a typo, not a filename
            sys.exit(f"{script}: ZIP_EXTRA is not parseable ({e})")
        for extra in extras:
            if (extra, 0o644) not in members:
                members.append((extra, 0o644))
    return members


def build(root: Path, members: list[tuple[str, int]], prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, mode in members:
            src = root / rel
            if not src.exists():
                sys.exit(f"missing: {src}")
            zi = zipfile.ZipInfo(prefix + rel, date_time=STAMP)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = mode << 16
            zf.writestr(zi, src.read_bytes())
    return buf.getvalue()


def _shippable(p: Path, top: Path) -> bool:
    """A file under one of BASE_DIRS that ships: no dotfile or dot-directory on its path, no __pycache__, no *.pyc."""
    if not p.is_file() or p.suffix == ".pyc":
        return False
    return not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(top).parts)


def base_members() -> list[tuple[str, int]]:
    out = []
    for f in BASE_MEMBERS:
        out.append((f, 0o700 if f.endswith(".sh") else 0o644))
    for d in BASE_DIRS:
        top = BASE / d
        if not top.is_dir():
            continue
        for p in sorted(top.rglob("*")):
            if _shippable(p, top):
                mode = 0o700 if d == "hosts" and p.suffix == ".sh" else 0o644
                out.append((str(p.relative_to(BASE)), mode))
    return out


def write_manifest(members: list[tuple[str, int]]) -> str:
    lines = []
    for rel, _ in members:
        lines.append("%s  %s" % (hashlib.sha256((BASE / rel).read_bytes()).hexdigest(), rel))
    return "\n".join(lines) + "\n"


def do(root: Path, members: list[tuple[str, int]], zip_path: Path, check: bool, prefix: str = "") -> int:
    fresh = build(root, members, prefix)
    if check:
        if not zip_path.exists():
            print(f"  {zip_path.name}: no zip on disk"); return 1
        if zip_path.read_bytes() != fresh:
            print(f"  {zip_path.name} is stale: rebuild it with package.py"); return 1
        print(f"  {zip_path.name} is current ({len(fresh) / 1000:.0f} KB, {len(members)} files)"); return 0
    zip_path.write_bytes(fresh)
    print(f"  wrote {zip_path.name}  ({len(fresh) / 1000:.0f} KB, {len(members)} files)")
    dirty = _uncommitted(root, members, prefix)
    if dirty:
        print("  ‼ built from UNCOMMITTED changes in %d packed file(s): %s" % (len(dirty), ", ".join(dirty[:6])))
        print("    Commit them with this zip, or rebuild after they land: a zip carrying a change whose source")
        print("    is uncommitted cannot be reproduced from a checkout. (Several sessions share this tree.)")
    return 0


def _uncommitted(root: Path, members, prefix: str = ""):
    """Packed files that differ from HEAD. A zip is a SHARED artefact built from a SHARED working tree: two
    zips were once committed carrying another session's in-flight edits, so the committed source and
    the committed artefact disagreed and no checkout could rebuild either. The rule "rebuild only from a clean
    tree" is one a person has to remember at the wrong moment; this is the tool saying it instead."""
    import subprocess
    try:
        repo = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
        if repo.returncode != 0:
            return []
        top = Path(repo.stdout.strip())
        names = subprocess.run(["git", "status", "--porcelain", "--", str(root)],
                               cwd=top, capture_output=True, text=True).stdout.splitlines()
    except Exception:                                    # pragma: no cover - a checkout without git still builds
        return []
    changed = set()
    for line in names:
        f = line[3:].strip().strip('"')
        if f:
            changed.add(str((top / f).resolve()))
    out = []
    for rel, _mode in members:
        src = (root / rel[len(prefix):]) if prefix and rel.startswith(prefix) else (root / rel)
        if str(src.resolve()) in changed:
            out.append(src.name)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", metavar="DIR", help="the consumer root --all walks (default: $BASE_WORKSPACE, else <root> of <root>/base/comfyui-base)")
    ap.add_argument("--base", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--host", metavar="PKG_DIR", help="print the host a package's zip is named for, and exit")
    a = ap.parse_args()
    if a.host:
        print(zip_host(Path(a.host).resolve()))
        return 0
    if not (a.dirs or a.all or a.base):
        print("usage: package.py <pkg dir>... | --all [--root <dir>] | --base [--check] | --host <pkg dir>", file=sys.stderr)
        return 2
    rc = 0
    if a.base:
        members = base_members()
        manifest = write_manifest(members)
        mf = BASE / "MANIFEST.sha256"
        if a.check:
            if not mf.exists() or mf.read_text() != manifest:
                print("  MANIFEST.sha256 is stale: rebuild with package.py --base"); rc = 1
        else:
            mf.write_text(manifest)
        members = members + [("MANIFEST.sha256", 0o644)]
        rc = max(rc, do(BASE, members, BASE_ZIP, a.check, prefix="comfyui-base/"))
    dirs = [Path(d).resolve() for d in a.dirs]
    if a.all:
        # 3.6.0: both shapes, <root>/<brand>/packages/<name>/ and <root>/<name>/, from the root the caller names
        try:
            root = pkgdirs.consumer_root(BASE, a.root, os.environ)
        except pkgdirs.NoSuchRoot as e:                 # a mistyped --root / $BASE_WORKSPACE is an error, not zero packages
            print("  %s" % e, file=sys.stderr)
            return 1
        if root is not None:
            dirs += [p for p in pkgdirs.package_dirs(root, BASE) if p not in dirs]
        else:
            print("  no consumer root (no --root, no $BASE_WORKSPACE, not at <root>/base/comfyui-base): nothing to walk")
    for pkg in dirs:
        rc = max(rc, do(pkg, package_members(pkg), pkg / f"{pkg.name}-{zip_host(pkg)}.zip", a.check))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
