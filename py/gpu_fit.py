#!/usr/bin/env python3
"""Is this GPU the one a package is tuned for? (3.8.0)

A package names the GPU classes it is built for, best first, in its script:  GPU_PREFER="B200 B300". This file is the
one table that says what each class is, and the one rule that says whether a GPU meets the preference. It never
blocks anything: a GPU below the preference gets a warning that names what it lacks, and the run goes on.

    python3 gpu_fit.py --prefer "B200 B300"                                # this machine, read from nvidia-smi
    python3 gpu_fit.py --prefer "B200 B300" --name N --cc 9.0 --mib 143771 # a GPU described by hand (the suite, podctl)
    python3 gpu_fit.py --check "B200 B300"                                 # exit 1 and name the unknown classes

Prints one line of TAB-separated fields:  fit=ok|below|unknown  gpu=<name>  sm=<sm_XY>  gb=<n>  want=<the preference>
(tabs, because a GPU's name and the preference both contain spaces).
`unknown` means nvidia-smi gave no answer (no GPU here, or the stand-in); it is never a warning. Exit 0 always, except
--check. Standalone: the standard library only, like gpu_facts.py beside it, so boot.sh can run it with any python3.

The rule. A GPU fits when its architecture (sm) is one a preferred class has, or when it is at least as new as the
oldest preferred class AND has at least 90 % of the smallest preferred class's memory (a newer chip with the memory,
before this table names it). Everything else is below. A MIG slice is never its parent class: RunPod sells
"NVIDIA B300 SXM6 AC MIG 1g.34gb", 34 GB of a B300.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# class → (compute capability, the memory the vendor states in GB, what to call it). First-party: NVIDIA's CUDA GPU
# compute-capability table (B200 10.0, B300 10.3) and the HGX B200 / B300 datasheets (180 GB / 288 GB HBM3e).
CLASSES = {
    "B200": ("10.0", 180, "NVIDIA B200"),
    "B300": ("10.3", 288, "NVIDIA B300"),
}
MEMORY_SHARE = 0.9          # nvidia-smi reports MiB, after the driver's own reservation: 180 GB reads as 183359 MiB


def sm_of(cc: str) -> str:
    """'10.3' → 'sm_103', the form lib/10-discover.sh and the SageAttention key use."""
    major, _, minor = cc.partition(".")
    return "sm_%s%s" % (major, minor or "0")


def _cc_key(cc: str) -> tuple[int, int]:
    major, _, minor = cc.partition(".")
    return (int(major), int(minor or 0))


def parse_prefer(text: str) -> list[str]:
    """'B200 B300' or 'B200,B300' → ['B200', 'B300'], in the package's order."""
    return [t for t in re.split(r"[\s,]+", (text or "").strip()) if t]


def unknown_classes(prefer: list[str]) -> list[str]:
    return [p for p in prefer if p not in CLASSES]


def class_of_name(name: str) -> str | None:
    """The class a GPU's NAME says it is: nvidia-smi's ('NVIDIA B200') or a host's type id ('NVIDIA B300 SXM6 AC').
    A MIG slice is a slice, never the class."""
    if not name or re.search(r"\bMIG\b", name, re.I):
        return None
    for key in CLASSES:
        if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(key), name, re.I):
            return key
    return None


def want_text(prefer: list[str]) -> str:
    """'B200 (sm_100, 180 GB) or B300 (sm_103, 288 GB)'."""
    return " or ".join("%s (%s, %d GB)" % (p, sm_of(CLASSES[p][0]), CLASSES[p][1]) for p in prefer if p in CLASSES)


def fits(prefer: list[str], cc: str | None, mib: int | None, name: str = "") -> str:
    """'ok' or 'below' for a GPU with this compute capability and total memory (MiB); 'unknown' with neither.
    The name decides alone only when the numbers are missing (a host's type id, read on the Mac)."""
    known = [p for p in prefer if p in CLASSES]
    if not known:
        return "ok"                                       # no preference declared: nothing to fall short of
    if not cc or not re.fullmatch(r"\d+(\.\d+)?", cc) or not mib:
        if name:
            return "ok" if class_of_name(name) in known else "below"
        return "unknown"
    if sm_of(cc) in {sm_of(CLASSES[p][0]) for p in known}:
        return "ok"
    oldest = min((CLASSES[p][0] for p in known), key=_cc_key)
    smallest_gb = min(CLASSES[p][1] for p in known)
    if _cc_key(cc) >= _cc_key(oldest) and mib * 1024 * 1024 >= MEMORY_SHARE * smallest_gb * 1e9:
        return "ok"
    return "below"


def _smi():
    """(name, cc, total MiB) of the first GPU, or None when nvidia-smi does not answer."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 3:
        return None
    return parts[0], parts[1], int(parts[2]) if parts[2].isdigit() else 0


def line(prefer: list[str], name: str, cc: str | None, mib: int | None) -> str:
    verdict = fits(prefer, cc, mib, name)
    sm = sm_of(cc) if cc and re.fullmatch(r"\d+(\.\d+)?", cc) else "?"
    gb = round(mib * 1024 * 1024 / 1e9) if mib else "?"
    return "\t".join(("fit=%s" % verdict, "gpu=%s" % (name or "?"), "sm=%s" % sm, "gb=%s" % gb, "want=%s" % want_text(prefer)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prefer", default="")
    ap.add_argument("--check", metavar="PREFER")
    ap.add_argument("--name"); ap.add_argument("--cc"); ap.add_argument("--mib", type=int)
    a = ap.parse_args(argv)
    if a.check is not None:
        bad = unknown_classes(parse_prefer(a.check))
        if bad:
            print("unknown GPU class %s (known: %s)" % (", ".join(bad), ", ".join(CLASSES)))
            return 1
        return 0
    prefer = parse_prefer(a.prefer)
    if a.name or a.cc or a.mib:
        print(line(prefer, a.name or "", a.cc, a.mib)); return 0
    got = _smi()
    if got is None:
        print(line(prefer, "", None, None)); return 0
    print(line(prefer, *got)); return 0


if __name__ == "__main__":
    sys.exit(main())
