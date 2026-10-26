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

The rule. A GPU fits when its architecture (sm) is one a preferred class has AND it has at least 90 % of that class's
memory, or when it is at least as new as the oldest preferred class AND has at least 90 % of the smallest preferred
class's memory (a newer chip with the memory, before this table names it). Everything else is below. A MIG slice is never
its parent class: RunPod sells "NVIDIA B300 SXM6 AC MIG 1g.34gb", 34 GB of a B300. 3.28.0: the sm alone no longer
decides, since sm_120 is an RTX PRO 6000's (96 GB) and an RTX 5090's (32 GB) alike.

Which test lane (3.21.0). LANES lists the GPUs a smoke can run on, cheapest first (Kaggle, HF ZeroGPU, Modal's
credits, then RunPod, which is paid), and pick_lane(peak_gib, minutes, needs) returns the first whose card holds the
measured peak plus HEADROOM and whose own limit holds the run:

    python3 gpu_fit.py --peak-gib 35.7 --minutes 10 --needs "bf16"   # lane=zerogpu-large ... paid=no
    python3 gpu_fit.py --peak-gib 69 --minutes 60 --needs "B200"     # a GPU class is judged by fits(), as GPU_PREFER is

Exit 0 with a lane, 1 with lane=none, 2 for an unknown need.
"""
from __future__ import annotations

import argparse
import re
from collections import namedtuple
import subprocess
import sys

# class → (compute capability, the memory the vendor states in GB, what to call it). First-party: NVIDIA's CUDA GPU
# compute-capability table (B200 10.0, B300 10.3) and the HGX B200 / B300 datasheets (180 GB / 288 GB HBM3e).
# 3.28.0: the same table gives the GB300 10.3 and the RTX PRO 6000 Blackwell Server Edition 12.0; NVIDIA's GB300 NVL72
# page gives the GB300 288 GB of HBM3e (a Grace CPU host: aarch64), the RTX PRO 6000 Blackwell Server Edition's datasheet
# 96 GB of GDDR7 (nvidia-smi reads 97887 MiB). A class names a GPU for GPU_PREFER and the warnings; it never blocks.
CLASSES = {
    "B200": ("10.0", 180, "NVIDIA B200"),
    "B300": ("10.3", 288, "NVIDIA B300"),
    "GB300": ("10.3", 288, "NVIDIA GB300 (Grace, aarch64)"),
    "RTX-PRO-6000": ("12.0", 96, "NVIDIA RTX PRO 6000 Blackwell Server Edition"),
}
# a class whose GPU_PREFER word is not how the GPU names itself: the pattern its name is read by. "RTX PRO 6000" is
# Blackwell's alone (the Ada card is "RTX 6000 Ada Generation"); the Workstation Edition is the same chip and memory.
NAMES = {"RTX-PRO-6000": r"(?<![A-Za-z0-9])RTX\s+PRO\s+6000(?![0-9])"}
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
        pattern = NAMES.get(key) or r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(key)   # GB300 is never B300
        if re.search(pattern, name, re.I):
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
    if any(sm_of(cc) == sm_of(CLASSES[p][0]) and mib * 1024 * 1024 >= MEMORY_SHARE * CLASSES[p][1] * 1e9 for p in known):
        return "ok"
    oldest = min((CLASSES[p][0] for p in known), key=_cc_key)
    smallest_gb = min(CLASSES[p][1] for p in known)
    if _cc_key(cc) >= _cc_key(oldest) and mib * 1024 * 1024 >= MEMORY_SHARE * smallest_gb * 1e9:
        return "ok"
    return "below"


# 3.21.0: the test lanes, cheapest first. A smoke takes the first lane whose card holds its measured peak with HEADROOM
# to spare and whose own limit holds the run (pick_lane). gb is the memory the vendor states; minutes is the longest
# one run may take there, None for no limit of the lane's own; server says a ComfyUI server can stay up (ZeroGPU runs
# per-call Gradio functions, so no). First-party: Modal's pricing page (per-second GPU prices, $30 a month of credits)
# and timeouts guide (1 s to 24 h per call); HF's ZeroGPU docs (`large` is half an RTX PRO 6000, `xlarge` the whole card
# at twice the quota rate). A paid lane (RunPod) comes last and still needs its owner's yes.
HEADROOM = 0.10             # the Kaggle harness calls a peak past 95 % of the card "tight"; 10 % keeps clear of that
KAGGLE_SESSION_MIN = 12 * 60
ZEROGPU_CAP_MIN = 30        # our own cap on the PRO quota's 40 daily minutes; past the quota HF bills credits
MODAL_CREDITS_USD = 30.0
MODAL_MAX_MIN = 24 * 60
DTYPES = ("fp16", "bf16", "fp8", "fp4")

Lane = namedtuple("Lane", "name gpu cc gb minutes server paid")


def _modal_min(usd_s: float) -> int:
    """The minutes the month's credits buy at this GPU's list price, within Modal's 24 h per call."""
    return min(MODAL_MAX_MIN, int(MODAL_CREDITS_USD / usd_s / 60))


LANES = (
    Lane("kaggle-t4", "NVIDIA T4", "7.5", 16, KAGGLE_SESSION_MIN, True, False),
    Lane("kaggle-l4", "NVIDIA L4", "8.9", 24, KAGGLE_SESSION_MIN, True, False),
    Lane("zerogpu-large", "half an NVIDIA RTX PRO 6000", "12.0", 48, ZEROGPU_CAP_MIN, False, False),
    Lane("zerogpu-xlarge", "NVIDIA RTX PRO 6000", "12.0", 96, ZEROGPU_CAP_MIN // 2, False, False),
    Lane("modal-t4", "NVIDIA T4", "7.5", 16, _modal_min(0.000164), True, False),
    Lane("modal-l4", "NVIDIA L4", "8.9", 24, _modal_min(0.000222), True, False),
    Lane("modal-a10", "NVIDIA A10", "8.6", 24, _modal_min(0.000306), True, False),
    Lane("modal-l40s", "NVIDIA L40S", "8.9", 48, _modal_min(0.000542), True, False),
    Lane("modal-a100-80gb", "NVIDIA A100 80GB", "8.0", 80, _modal_min(0.000694), True, False),
    Lane("modal-rtx-pro-6000", "NVIDIA RTX PRO 6000", "12.0", 96, _modal_min(0.000842), True, False),
    Lane("modal-b200", "NVIDIA B200", CLASSES["B200"][0], CLASSES["B200"][1], _modal_min(0.001736), True, False),
    Lane("runpod-rtx-pro-6000", "NVIDIA RTX PRO 6000", "12.0", 96, None, True, True),
    Lane("runpod-b200", "NVIDIA B200", CLASSES["B200"][0], CLASSES["B200"][1], None, True, True),
    Lane("runpod-b300", "NVIDIA B300", CLASSES["B300"][0], CLASSES["B300"][1], None, True, True),
)


def fast_dtypes(cc: str) -> list[str]:
    """The dtypes this architecture's tensor cores run natively: bf16 from Ampere (8.0), fp8 from Ada (8.9), fp4 from
    Blackwell (10.0). A T4 (7.5) has fp16 alone."""
    k = _cc_key(cc)
    return [d for d, floor in zip(DTYPES, ((0, 0), (8, 0), (8, 9), (10, 0))) if k >= floor]


def pick_lane(peak_gib: float, minutes: float, needs=(), lanes=LANES) -> Lane | None:
    """The cheapest lane whose card holds `peak_gib` (measured, e.g. torch.cuda.max_memory_reserved) plus HEADROOM,
    whose limit holds a run of `minutes`, and which has every need: a fast dtype (fp16 bf16 fp8 fp4), `server`, or GPU
    classes from CLASSES, judged by fits() exactly as a package's GPU_PREFER is. None when no lane holds it.
    `needs` is a list or a string in GPU_PREFER's form ("bf16 server"); an unknown need is a ValueError."""
    needs = parse_prefer(needs) if isinstance(needs, str) else list(needs)
    bad = [n for n in needs if n not in DTYPES and n != "server" and n not in CLASSES]
    if bad:
        raise ValueError("unknown need %s (known: %s, server, %s)" % (", ".join(bad), " ".join(DTYPES), " ".join(CLASSES)))
    if peak_gib < 0 or minutes < 0:
        raise ValueError("peak_gib and minutes are measured amounts, never negative")
    classes = [n for n in needs if n in CLASSES]
    for lane in lanes:
        if peak_gib * (1 + HEADROOM) > lane.gb * 1e9 / 2 ** 30:
            continue
        if lane.minutes is not None and minutes > lane.minutes:
            continue
        if "server" in needs and not lane.server:
            continue
        if not {n for n in needs if n in DTYPES} <= set(fast_dtypes(lane.cc)):
            continue
        if classes and fits(classes, lane.cc, int(lane.gb * 1e9 / 2 ** 20), lane.gpu) != "ok":
            continue
        return lane
    return None


def lane_line(lane: Lane | None, peak_gib: float, minutes: float, needs: str) -> str:
    if lane is None:
        return "\t".join(("lane=none", "peak_gib=%g" % peak_gib, "minutes=%g" % minutes, "needs=%s" % needs))
    return "\t".join(("lane=%s" % lane.name, "gpu=%s" % lane.gpu, "sm=%s" % sm_of(lane.cc), "gb=%d" % lane.gb,
                      "minutes=%s" % ("none" if lane.minutes is None else lane.minutes), "paid=%s" % ("yes" if lane.paid else "no")))


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
    ap.add_argument("--peak-gib", type=float); ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--needs", default="")
    a = ap.parse_args(argv)
    if a.peak_gib is not None:
        try:
            lane = pick_lane(a.peak_gib, a.minutes, a.needs)
        except ValueError as e:
            print(e); return 2
        print(lane_line(lane, a.peak_gib, a.minutes, a.needs))
        return 0 if lane else 1
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
