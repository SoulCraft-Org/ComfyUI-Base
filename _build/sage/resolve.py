#!/usr/bin/env python3
"""What .github/workflows/sage-wheels.yml builds: the key a pod would compute today, and which arch wheels its release lacks.

A pod (lib/40-packs.sh _base_sage_install) keys SageAttention on the resolved upstream commit, the venv's Python, torch
and CUDA build, and the GPU's sm. This answers the same questions the same way, with no GPU:
  commit   git ls-remote of SAGE_REF (default main), as the pod resolves it
  Python   the newest stable minor uv provides on which py/torch_pick.py resolves the torch family
  torch    py/torch_pick.py for that minor; with no driver it takes the newest CUDA build uv knows that carries the whole
           family, which is what a pod with a current driver (every Hopper and Blackwell host) gets
  image    the newest nvidia/cuda <major>.<minor>.x-devel-ubuntu24.04 for that CUDA build (cu130 -> 13.0.x)
Each can be named instead (workflow_dispatch inputs). Prints KEY=VALUE lines for $GITHUB_OUTPUT, and the matrix
of archs whose wheel is not yet in the release (all of them with --all, a pull request's dry run). An arch counts as
published once its sm_<arch>.sha256 is there: the workflow uploads it after the wheel and never replaces either.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
ARCHS = ["9.0", "10.0", "10.3", "12.0"]


def run(*cmd, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, **kw)


def sage_commit(ref):
    r = run("git", "ls-remote", "https://github.com/thu-ml/SageAttention", ref)
    m = re.match(r"([0-9a-f]{40})\s", r.stdout)
    if not m:
        sys.exit("could not resolve SageAttention %s: %s" % (ref, r.stderr.strip()))
    return m.group(1)


def python_minors():
    out = run("uv", "python", "list").stdout
    return sorted({m for m in re.findall(r"^cpython-(3\.\d+)\.\d+-", out, re.M)}, key=lambda v: int(v.split(".")[1]), reverse=True)


def torch_for(pymm):
    r = run(sys.executable, os.path.join(BASE, "py", "torch_pick.py"), pymm)
    if r.returncode != 0:
        return None
    ver, backend = r.stdout.split()
    print("  Python %s: torch %s %s (%s)" % (pymm, ver, backend, r.stderr.strip()), file=sys.stderr)
    return ver, backend


def cuda_image(backend):
    m = re.fullmatch(r"cu(\d+)(\d)", backend)
    if not m:
        sys.exit("not a CUDA backend: %s" % backend)
    want = "%s.%s" % (m.group(1), m.group(2))
    url = "https://hub.docker.com/v2/repositories/nvidia/cuda/tags?page_size=100&name=%s." % want
    best = None
    while url:
        with urllib.request.urlopen(url, timeout=30) as f:
            d = json.load(f)
        for t in d.get("results", []):
            m2 = re.fullmatch(re.escape(want) + r"\.(\d+)-devel-ubuntu24\.04", t["name"])
            if m2 and (best is None or int(m2.group(1)) > best[0]):
                best = (int(m2.group(1)), t["name"])
        url = d.get("next")
    if not best:
        sys.exit("no nvidia/cuda %s.x-devel-ubuntu24.04 image" % want)
    return "nvidia/cuda:" + best[1]


def release_assets(repo, tag):
    r = run("gh", "release", "view", tag, "--repo", repo, "--json", "assets", "-q", ".assets[].name")
    return r.stdout.split() if r.returncode == 0 else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="main")
    ap.add_argument("--python", default="")
    ap.add_argument("--torch", default="", help="e.g. 2.14.1 (with --backend)")
    ap.add_argument("--backend", default="", help="e.g. cu130 (with --torch)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "SoulCraft-Org/ComfyUI-Base"))
    ap.add_argument("--all", action="store_true", help="every arch, published or not (a pull request builds them all, dry)")
    a = ap.parse_args()

    sha = sage_commit(a.ref) if not re.fullmatch(r"[0-9a-f]{40}", a.ref) else a.ref
    if a.torch and a.backend:
        pymm = a.python or "3.14"
        torch, backend = a.torch, a.backend
    else:
        picked = None
        for mm in ([a.python] if a.python else python_minors()):
            picked = torch_for(mm)
            if picked:
                pymm = mm
                break
            print("  Python %s: the torch family has no wheels for it" % mm, file=sys.stderr)
        if not picked:
            sys.exit("no Python minor resolves the torch family")
        torch, backend = picked
    pytag = "cp" + pymm.replace(".", "")
    tag = "sage-%s-torch%s-%s-%s" % (sha[:12], torch, backend, pytag)
    have = release_assets(a.repo, tag)
    todo = [x for x in ARCHS if a.all or "sm_%s.sha256" % x.replace(".", "") not in have]
    out = {"sha": sha, "sha12": sha[:12], "python": pymm, "pytag": pytag, "torch": torch, "backend": backend,
           "image": cuda_image(backend), "tag": tag, "archs": json.dumps(todo), "build": "true" if todo else "false"}
    for k, v in out.items():
        print("%s=%s" % (k, v))


if __name__ == "__main__":
    main()
