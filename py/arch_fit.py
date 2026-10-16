#!/usr/bin/env python3
"""Does a torch build carry kernels this GPU can run? (3.11.0)

torch lists its kernels as `sm_XY` (SASS for one compute capability) and `compute_XY` (PTX). The CUDA rule, from
NVIDIA's compatibility guide: SASS built for X.Y runs on a device of the SAME major X with a minor of Y or more, except
the arch-specific `sm_XYa` builds, which run on X.Y only; PTX built for X.Y is compiled by the driver for any device of
X.Y or newer. So torch 2.14.1 (CUDA 13.0)'s `sm_100` runs on a B300 (`sm_103`) and `sm_120` on an `sm_121`, and only a GPU
outside every family the build carries is missing. Comparing strings had a B300 rebuild the venv on every install.

    covering(want, archs) -> the listed arch that runs on `want` ("sm_103" -> "sm_100"), or "" when none does.
"""
import re

_ARCH = re.compile(r"^(sm|compute)_(\d+)(\d)([af]?)$")


def _parse(a):
    m = _ARCH.match((a or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)


def covering(want, archs):
    w = _parse(want)
    if not w or w[0] != "sm":
        return ""
    _, wmaj, wmin, _ = w
    sass, ptx = None, None                                      # the closest of each kind: (minor or (major, minor), name)
    for a in archs or ():
        p = _parse(a)
        if not p:
            continue
        kind, maj, mnr, suffix = p
        if kind == "sm" and maj == wmaj and mnr == wmin:
            return a                                            # an exact build is always the answer
        if kind == "sm" and suffix != "a" and maj == wmaj and mnr < wmin:
            if sass is None or mnr > sass[0]:
                sass = (mnr, a)                                 # same family, an older minor: SASS runs forward
        elif kind == "compute" and (maj, mnr) <= (wmaj, wmin):
            if ptx is None or (maj, mnr) > ptx[0]:
                ptx = ((maj, mnr), a)                           # PTX: the driver compiles it for this GPU
    return (sass or ptx or (None, ""))[1]

if __name__ == "__main__":
    import sys
    print(covering(sys.argv[1], sys.argv[2].split(",")) or "none")
