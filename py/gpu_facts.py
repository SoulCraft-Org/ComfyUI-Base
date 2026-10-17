#!/usr/bin/env python3
"""The GPU must be ours. nvidia-smi inside a container reports the whole card's memory but lists only THIS container's
processes: memory used that no visible process holds belongs to another container, or to a context leaked on the host.
Renders then die at their first node with the card idle — 2026-09-06, pod fakepod0000003: 95.3 GB of 97.9 used, 0 %
utilisation, our ComfyUI holding 1.7 GB, 93.5 GB held outside (2.0.26). Every check before that had passed.

Prints one line:  total=<MiB> used=<MiB> ours=<MiB> foreign=<MiB> procs=<n> [gpus=<n>] verdict=ours|NOT-OURS|no-gpu
3.20.0: on several GPUs (one ComfyUI on each) every card is judged by ITS OWN processes (nvidia-smi's uuid ties a process
to its card), the numbers are the cards' sums, and one card with foreign memory makes the verdict NOT-OURS.
Exit 0 always; the caller reads the verdict. Standalone: no torch, no imports beyond the standard library."""
import subprocess, sys

FOREIGN_LIMIT_MIB = 4096   # the driver keeps ~600 MiB for itself on this card; past 4 GiB it is someone else's memory


def verdict(total, used, apps):
    """(total MiB, used MiB, [(pid, MiB)…] visible here) → (ours MiB, foreign MiB, verdict)."""
    ours = sum(m for _, m in apps)
    foreign = max(0, used - ours)
    return ours, foreign, ("ours" if foreign <= FOREIGN_LIMIT_MIB else "NOT-OURS")


def verdict_many(cards, apps):
    """3.20.0: ([(total, used, uuid)…], [(pid, MiB, uuid)…]) → (total, used, ours, foreign, verdict) summed over the cards,
    each card judged by its own processes. One card, or an nvidia-smi that names no uuid: verdict() over every process."""
    if len(cards) <= 1 or not all(c[2] for c in cards):
        total, used = (cards[0][0], cards[0][1]) if cards else (0, 0)
        return (total, used) + verdict(total, used, [(p, m) for p, m, _ in apps])
    sums, bad = [0, 0, 0, 0], False
    for total, used, uuid in cards:
        ours, foreign, v = verdict(total, used, [(p, m) for p, m, u in apps if u == uuid])
        for i, n in enumerate((total, used, ours, foreign)):
            sums[i] += n
        bad = bad or v != "ours"
    return tuple(sums) + ("NOT-OURS" if bad else "ours",)


def _query(args):
    try:
        r = subprocess.run(["nvidia-smi"] + args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_int(s):
    s = s.strip()
    return int(s) if s.isdigit() else 0


def facts():
    mem = _query(["--query-gpu=memory.total,memory.used,uuid", "--format=csv,noheader,nounits"])
    if not mem or not mem.strip():
        return None
    cards = []
    for line in mem.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            cards.append((parse_int(parts[0]), parse_int(parts[1]), parts[2] if len(parts) > 2 else ""))
    apps = []
    for line in (_query(["--query-compute-apps=pid,used_memory,gpu_uuid", "--format=csv,noheader,nounits"]) or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            apps.append((int(parts[0]), parse_int(parts[1]), parts[2] if len(parts) > 2 else ""))
    total, used, ours, foreign, v = verdict_many(cards, apps)
    return {"total": total, "used": used, "ours": ours, "foreign": foreign, "procs": len(apps), "gpus": len(cards), "verdict": v}


def main():
    f = facts()
    if f is None:
        print("verdict=no-gpu"); return 0
    print("total=%d used=%d ours=%d foreign=%d procs=%d%s verdict=%s" % (f["total"], f["used"], f["ours"], f["foreign"], f["procs"],
                                                                        (" gpus=%d" % f["gpus"]) if f["gpus"] > 1 else "", f["verdict"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
