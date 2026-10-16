#!/usr/bin/env python3
"""Probe the venv this interpreter belongs to. Exit 0 = reusable, 1 = rebuild.

Run as: BASE_WANT_PY=3.14 BASE_WANT_TORCH=2.14.0 BASE_WANT_BACKEND=cu132 BASE_WANT_SM=sm_XY BASE_PERSIST_ROOT=/workspace VENV=... $PY venv_probe.py

HARD failures are the ones that would stop a render or mean this is not the venv the base maintains:
python minor is not the pick, torch is missing or behind the pick, torch's CUDA build is not the picked backend
(3.0.0: uv's --torch-backend=auto, never backwards on a shared venv), the device's sm is missing, or the venv boots
from a path a pod stop/start wipes. Everything else is a note.
"""
import os
import sys

hard, soft = [], []
want_py = os.environ.get("BASE_WANT_PY") or ""
want_torch = os.environ.get("BASE_WANT_TORCH") or ""
want_sm = os.environ.get("BASE_WANT_SM") or ""
want_backend = os.environ.get("BASE_WANT_BACKEND") or ""
have_py = "%d.%d" % (sys.version_info[0], sys.version_info[1])
if want_py and have_py != want_py:
    hard.append("python %s, want %s (newest that resolves)" % (have_py, want_py))
try:
    import torch
except Exception as e:  # noqa: BLE001
    print("    x torch does not import: %s" % e)
    sys.exit(1)
have_backend = "cu" + (torch.version.cuda or "").replace(".", "") if torch.version.cuda else "cpu"
if want_backend and have_backend != want_backend:
    hard.append("torch is built for %s, the pick is %s" % (have_backend, want_backend))
if want_sm:
    try:
        from arch_fit import covering                    # 3.11.0: a listed arch of the same family (or PTX) runs here too
        if not covering(want_sm, torch.cuda.get_arch_list()):
            hard.append("torch has no kernels that run on %s (this GPU)" % want_sm)
    except Exception as e:  # noqa: BLE001
        hard.append("arch list unavailable: %s" % e)
have_t = torch.__version__.split("+")[0]
if want_torch and have_t != want_torch:
    def key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:  # noqa: BLE001
            return (0,)
    if key(have_t) < key(want_torch):
        hard.append("torch %s, the newest for this Python on %s is %s" % (have_t, want_backend or "its backend", want_torch))
    else:
        soft.append("torch %s is ahead of the index pick %s" % (have_t, want_torch))
tp = os.path.realpath(os.path.dirname(torch.__file__))
if not tp.startswith(os.path.realpath(sys.prefix) + os.sep):
    soft.append("torch comes from outside the venv (normal on the stock RunPod image)")
# WHERE the base interpreter lives is the only property that outlives the boot. A venv whose interpreter
# is a symlink into $HOME passes every check above right up to the moment the pod is stopped. HARD, because
# a hard failure is what routes this venv to a rebuild instead of a reuse, and a poisoned venv that is
# reused is a pod that does not come back. Two kinds of path survive a restart: the volume, and anything
# the IMAGE provides (/usr/bin/python3.12 is restored every start). What does not come back is anything
# written at RUNTIME onto the container disk: $HOME, /root, /home, /tmp.
_vol = os.environ.get("BASE_PERSIST_ROOT") or ""
_venv = os.environ.get("VENV") or ""
if _vol and _venv and os.path.realpath(_venv).startswith(os.path.realpath(_vol) + os.sep):
    _vol = os.path.realpath(_vol)
    _base = os.path.realpath(getattr(sys, "_base_executable", None) or sys.base_prefix)
    _home = ""
    _cfg = os.path.join(_venv, "pyvenv.cfg")
    if os.path.exists(_cfg):
        for _ln in open(_cfg, encoding="utf-8", errors="replace"):
            if _ln.split("=", 1)[0].strip() == "home":
                _home = os.path.realpath(_ln.split("=", 1)[1].strip())
                break
    _ephemeral = (os.environ.get("HOME") or "/root", "/root", "/home", "/tmp", "/var/tmp")
    _doomed = sorted({p for p in (_base, _home) if p and not p.startswith(_vol + os.sep)
                      and any(p == e or p.startswith(e.rstrip("/") + os.sep) for e in _ephemeral)})
    if _doomed:
        hard.append("boots from %s, which a pod stop/start wipes - start.sh would then activate a dangling "
                    "symlink and the pod would not come up at all" % ", ".join(_doomed))
for m in hard:
    print("    x " + m)
for m in soft:
    print("    ~ " + m)
print("    python %s · torch %s · CUDA %s" % (sys.version.split()[0], torch.__version__, torch.version.cuda))
sys.exit(0 if not hard else 1)
