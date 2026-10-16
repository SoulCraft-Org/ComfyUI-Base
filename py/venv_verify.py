#!/usr/bin/env python3
"""Verify a freshly built venv from the inside. Prints one fact line, then every problem; exit 1 on any.

Run as: VENV=... BASE_PERSIST_ROOT=/workspace BASE_WANT_PY=3.14 BASE_WANT_BACKEND=cu132 BASE_WANT_SM=sm_120 BASE_EXTRA_IMPORTS="a b" $PY venv_verify.py

BASE_NO_GPU=1 (3.9.1, set by the install for BASE_SEED=1): an image build has no GPU, so "torch sees a CUDA device" is
not asked; the CUDA build, the archs and the closure still are. The machine the image serves asks it at its first install.

A non-zero exit fires the automatic rollback, and that rollback is what leaves the pod BOOTABLE instead
of bricked. So the persistence closure is fatal here, not a warning: the base interpreter, the pyvenv.cfg
home, every existing sys.path root and every absolute .pth target must live on the volume.
"""
import importlib
import os
import sys
import time

venv = os.environ["VENV"]
vol = os.path.realpath(os.environ["BASE_PERSIST_ROOT"]) if os.environ.get("BASE_PERSIST_ROOT") else ""
want_py = os.environ.get("BASE_WANT_PY") or ""
want_sm = os.environ.get("BASE_WANT_SM") or ""
want_backend = os.environ.get("BASE_WANT_BACKEND") or ""
extra = (os.environ.get("BASE_EXTRA_IMPORTS") or "").split()
t0 = time.time()
import torch  # noqa: E402
t_import = time.time() - t0
probs = []
cu = torch.version.cuda or "none"
archs = torch.cuda.get_arch_list()
have_py = "%d.%d" % sys.version_info[:2]
if want_py and have_py != want_py:
    probs.append("python is %s, not %s" % (sys.version.split()[0], want_py))
have_backend = "cu" + cu.replace(".", "") if cu != "none" else "cpu"
if want_backend and have_backend != want_backend:
    probs.append("torch is built for %s, not the picked %s" % (have_backend, want_backend))
elif not want_backend and cu == "none":
    probs.append("torch has no CUDA build")
if want_sm and want_sm not in archs:
    probs.append("%s not in torch arch list" % want_sm)
no_gpu = os.environ.get("BASE_NO_GPU") == "1"
if not no_gpu and not torch.cuda.is_available():
    probs.append("torch.cuda.is_available() is False")
if not os.path.realpath(torch.__file__).startswith(os.path.realpath(venv)):
    probs.append("torch imported from outside the venv: %s" % torch.__file__)
base = os.path.realpath(getattr(sys, "_base_executable", None) or sys.base_prefix)
cfg, home = os.path.join(venv, "pyvenv.cfg"), ""
if os.path.exists(cfg):
    for ln in open(cfg, encoding="utf-8", errors="replace"):
        if ln.split("=", 1)[0].strip() == "home":
            home = os.path.realpath(ln.split("=", 1)[1].strip())
            break
if vol and os.path.realpath(venv).startswith(vol + os.sep):
    off = sorted({p for p in (base, home, os.path.realpath(sys.executable)) if p and not p.startswith(vol + os.sep)})
    if off:
        probs.append("the base interpreter of this venv is outside %s (%s) - a pod stop/start wipes it, start.sh then "
                     "activates a dangling symlink and the pod never comes up" % (vol, ", ".join(off)))
    # the same question asked of every import root the interpreter will have at boot; the cwd belongs to
    # the caller, not to the venv, so it is out
    here, roots = os.path.realpath(os.getcwd()), []
    for p in sys.path:
        if not p:
            continue
        rp = os.path.realpath(p)
        if rp == here or not os.path.exists(rp):
            continue
        if not rp.startswith(vol + os.sep):
            roots.append(rp)
    # a .pth in site-packages can add roots the running interpreter has not been asked for yet
    for sp in [p for p in sys.path if p.endswith("site-packages") and os.path.isdir(p)]:
        for f in sorted(os.listdir(sp)):
            if not f.endswith((".pth", ".egg-link")):
                continue
            for ln in open(os.path.join(sp, f), encoding="utf-8", errors="replace"):
                ln = ln.strip()
                if not ln.startswith("/"):
                    continue
                rp = os.path.realpath(ln)
                if not rp.startswith(vol + os.sep):
                    roots.append("%s (via %s)" % (rp, f))
    if roots:
        probs.append("this venv imports from outside %s (%s) - a pod stop/start wipes those paths" % (vol, ", ".join(sorted(set(roots)))))
for m in extra:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        probs.append("import %s: %r" % (m, e))
print("python %s · torch %s · CUDA %s · %s · import torch %.1fs%s" % (
    sys.version.split()[0], torch.__version__, cu, (want_sm if want_sm in archs else "NO " + want_sm) if want_sm else ",".join(archs[-2:]), t_import,
    " · no GPU asked (seed build)" if no_gpu else ""))
for p in probs:
    print("  ‼ " + p)
sys.exit(1 if probs else 0)
