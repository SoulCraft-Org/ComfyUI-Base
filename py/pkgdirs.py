"""pkgdirs.py: where a consumer's packages are, in either of the two shapes a consumer can have (3.6.0).

    python3 py/pkgdirs.py [<root>]          # the package dirs of <root> (else the resolved consumer root), one per line
    python3 py/pkgdirs.py --root [<root>]   # the consumer root itself: <root>, else $BASE_WORKSPACE, else the submodule shape

Exit 0 with the answer (nothing printed by --root when there is no consumer root: a documented outcome); exit 1 with
"consumer root <X> does not exist" on stderr when a named root (the argument or $BASE_WORKSPACE) is not a directory.
The shell walkers treat any other non-zero exit as the helper failing and stop, never as "no packages".

A consumer keeps its packages in one of two shapes:

  - submodule:  <root>/base/comfyui-base is this base, and the packages are <root>/<brand>/packages/<name>/
  - workspace:  one repository per package, cloned side by side as <root>/<name>/, with the base read
                from $COMFY_BASE (a shared checkout that is NOT inside the workspace). $BASE_WORKSPACE names <root>.

Every cross-package walker (package.py --all, podctl's pin scan, verify.sh, testbed.sh, basetest.converted_packages)
asks this module, so the rule is written once. It used to be a `*/packages/*` glob in five places, each with its own
guard against a base that is its own repository, and none of them could see a workspace. Stdlib only, no side effects.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

BASE = Path(__file__).resolve().parents[1]


class NoSuchRoot(ValueError):
    """A named consumer root (explicit or $BASE_WORKSPACE) that is not a directory. A typo there must not read as an
    empty workspace: every walker would go green over zero packages."""


def consumer_root(base_dir, explicit: str | None, env: Mapping[str, str]) -> Path | None:
    """The consumer root to walk. An explicit root (a Python tool's --root, or pkgdirs.py's own argument) wins; then
    $BASE_WORKSPACE, the only override the shell walkers (verify.sh, testbed.sh) take; then <root> when base_dir sits
    at <root>/base/comfyui-base (the submodule shape); else None, and the caller walks nothing beyond what it was
    given. An empty value counts as unset. A named root that is not a directory raises NoSuchRoot."""
    named = explicit or env.get("BASE_WORKSPACE") or ""
    if named:
        root = Path(named).expanduser().resolve()
        if not root.is_dir():
            raise NoSuchRoot("consumer root %s does not exist" % named)
        return root
    base = Path(base_dir).resolve()
    if len(base.parents) > 1 and (base.parents[1] / "base" / "comfyui-base").resolve() == base:
        return base.parents[1]
    return None                        # a base that is its own repository: two levels up is whatever holds the checkout


def _is_package(d: Path, root: Path, base: Path) -> bool:
    """d holds d/<basename(d)>-script.sh, is not under a dot-directory (.git, .claude, .superpowers) and is not a
    ComfyUI Base checkout: the caller's base, or any comfyui-base/ (whose comfyui-base-script.sh passes the script
    test on its own)."""
    try:
        rel = d.relative_to(root)
    except ValueError:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    if d.name == "comfyui-base" or d.resolve() == base:
        return False
    return d.is_dir() and (d / f"{d.name}-script.sh").is_file()


def package_dirs(root, base_dir=None) -> list[Path]:
    """The package directories under root, both shapes: root/*/packages/*/ and root/*/. Resolved, deduplicated (a
    symlink to a package is the package), sorted by path. base_dir is the caller's base, never a package (default:
    the base this file belongs to). A root that does not exist has none."""
    root = Path(root).resolve()
    base = Path(base_dir).resolve() if base_dir is not None else BASE
    if not root.is_dir():
        return []
    seen = set()
    for d in [*root.glob("*/packages/*"), *root.iterdir()]:
        if _is_package(d, root, base):
            seen.add(d.resolve())
    return sorted(seen)


def main(argv: list[str]) -> int:
    want_root = bool(argv) and argv[0] == "--root"
    if want_root:
        argv = argv[1:]
    try:
        root = consumer_root(BASE, argv[0] if argv else None, os.environ)
    except NoSuchRoot as e:
        print(e, file=sys.stderr)
        return 1
    if root is None:
        if not want_root:
            print("no consumer root: nothing to walk", file=sys.stderr)
        return 0
    if want_root:
        print(root)
    else:
        for d in package_dirs(root, BASE):
            print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
