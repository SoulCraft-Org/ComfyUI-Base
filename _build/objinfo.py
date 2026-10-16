#!/usr/bin/env python3
"""objinfo.py: ComfyUI's own node schema, cached per package, and the package's API prompts checked against it offline.

    python3 "$COMFY_BASE/_build/objinfo.py" save <pkg dir> [--server HOST:PORT]    # from a live ComfyUI: the pod through
                                                                                  # `podctl tunnel` (127.0.0.1:8188), else the testbed
    python3 "$COMFY_BASE/_build/objinfo.py" check <pkg dir>...                      # offline, in seconds (tier 0: _build/quick.sh)

The cache is <pkg>/_build/object_info.json, the shape image-creator-krea-2's _build/object_info.py has committed since
2026-09: {comfyui, frontend, server, generated, missing, frontend_only, venv_gated, types: {class: /object_info[class]}},
for every class the package's workflow (subgraph interiors included) and its API prompts use. Key order is ComfyUI's own,
never sorted: the frontend restores widgets_values positionally into it. _build/ is not in any zip, so the cache never
ships and moving it needs no package version.

`check` reads every *.api.json under the package (dot-folders skipped) and does what ComfyUI's validator
(execution.validate_prompt / validate_inputs) does with the schema alone:
  - every class_type is in the cache (an unknown class fails the whole prompt there, reachable or not);
  - the prompt has an output node, and from those outputs back, for each node: every required input is present,
    a link names a node of the prompt and one of its outputs, and the output's type fits the input;
  - a literal value converts for INT / FLOAT, lies within min / max, and is one of a combo's options.
What it cannot see is a node's VALIDATE_INPUTS, which ComfyUI runs INSTEAD of the list check for the inputs it names
(LoadImage's file name is checked against the disk, not the list). So upload combos (image_upload, video_upload, ...)
and empty option lists (a model folder on a testbed without the weights) are not list-checked: they are counted as
"unjudged", never passed silently. A cache taken on the pod lists the pod's own files, so there a model name the pod
lacks fails here, before a render dies on it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "py"))
from canvas import FRONTEND_ONLY_TYPES                               # noqa: E402  the one list (canvas.py)

CACHE = pathlib.Path("_build") / "object_info.json"
UNJUDGED_KINDS = ("upload combo", "empty option list", "dynamic input")


def cache_path(pkg):
    return pathlib.Path(pkg) / CACHE


def prompts_of(pkg):
    """Every exported API prompt of the package: *.api.json anywhere under it, dot-folders skipped."""
    pkg = pathlib.Path(pkg)
    return sorted(p for p in pkg.rglob("*.api.json") if not any(part.startswith(".") for part in p.relative_to(pkg).parts))


def workflow_of(pkg):
    """The workflow the package's script names in WF_NAME, or None (no script, WF_NAME="", or the file is absent)."""
    pkg = pathlib.Path(pkg)
    for s in sorted(pkg.glob("*-script.sh")):
        m = re.search(r'^WF_NAME="([^"]*)"', s.read_text(encoding="utf-8"), re.M)
        if m and m.group(1) and (pkg / m.group(1)).is_file():
            return pkg / m.group(1)
    return None


def workflow_types(wf):
    """The server-side classes a UI workflow uses: root nodes and every subgraph's interior, minus subgraph instances
    (their type is the definition's id) and the frontend-only types."""
    defs = (wf.get("definitions") or {}).get("subgraphs") or []
    ids = {d.get("id") for d in defs}
    nodes = list(wf.get("nodes") or [])
    for d in defs:
        nodes += d.get("nodes") or []
    return {n["type"] for n in nodes if n.get("type") and n["type"] not in ids and n["type"] not in FRONTEND_ONLY_TYPES}


def used_classes(pkg):
    used = set()
    wf = workflow_of(pkg)
    if wf:
        used |= workflow_types(json.loads(wf.read_text(encoding="utf-8")))
    for p in prompts_of(pkg):
        used |= {v["class_type"] for v in json.loads(p.read_text(encoding="utf-8")).values() if isinstance(v, dict) and "class_type" in v}
    return sorted(used)


# ---------------------------------------------------------------- the schema, as ComfyUI's validator reads it

def spec_of(info, name):
    """(category, type, extra) for one declared input, or None. Both /object_info shapes: a list in the type slot (the
    old combo) and ["COMBO", {"options": [...]}] (0.34+)."""
    for cat in ("required", "optional"):
        sect = (info.get("input") or {}).get(cat) or {}
        if name in sect:
            v = sect[name]
            typ = v[0] if isinstance(v, (list, tuple)) and v else v
            extra = v[1] if isinstance(v, (list, tuple)) and len(v) > 1 and isinstance(v[1], dict) else {}
            return cat, typ, extra
    return None


def is_v3_template(typ):
    """A V3 io type whose real shape lives in its options (autogrow, dynamic combo, match type): judged by presence only."""
    return isinstance(typ, str) and typ.startswith("COMFY_") and typ.endswith("_V3")


def combo_options(typ, extra):
    if isinstance(typ, list):
        return typ
    if typ == "COMBO":
        return extra.get("options") or []
    return None


def types_fit(received, wanted):
    """ComfyUI's validate_node_input, not strict: equal, either side "*", or the comma-separated sets meet. A list type
    (an old-style combo output) or a V3 template on either side is not judged."""
    if not isinstance(received, str) or not isinstance(wanted, str) or is_v3_template(received) or is_v3_template(wanted):
        return True
    if received == wanted or "*" in (received, wanted):
        return True
    return bool({t.strip() for t in received.split(",")} & {t.strip() for t in wanted.split(",")})


def reachable(prompt, types):
    """The node ids ComfyUI validates: the output nodes and everything they link back to."""
    todo = [k for k, v in prompt.items() if (types.get(v.get("class_type")) or {}).get("output_node")]
    seen = set()
    while todo:
        k = todo.pop()
        if k in seen or k not in prompt:
            continue
        seen.add(k)
        for val in (prompt[k].get("inputs") or {}).values():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], (str, int)):
                todo.append(str(val[0]))
    return seen


def check_prompt(prompt, types, venv_gated=()):
    """(problems, unjudged) for one API prompt: problems are what ComfyUI would refuse; unjudged names what the schema
    alone cannot decide."""
    problems, unjudged = [], []
    for k, v in sorted(prompt.items(), key=lambda kv: str(kv[0])):
        c = v.get("class_type")
        if c not in types:
            problems.append("%s (%s): not in the cached /object_info%s" % (
                k, c, " (venv-gated where the cache was taken)" if c in venv_gated else ": re-save it where the package runs"))
    if problems:
        return problems, unjudged
    live = reachable(prompt, types)
    if not live:
        problems.append("no output node: ComfyUI refuses a prompt with nothing to run")
    for k in sorted(live, key=str):
        v = prompt[k]; c = v["class_type"]; info = types[c]; inputs = v.get("inputs") or {}
        declared = {**((info.get("input") or {}).get("optional") or {}), **((info.get("input") or {}).get("required") or {})}
        for name in declared:
            cat, typ, extra = spec_of(info, name)
            here = name in inputs or any(i.startswith(name + ".") for i in inputs)
            if not here:
                if cat == "required" and typ != "COMFY_AUTOGROW_V3":
                    problems.append("%s (%s): required input %r is missing" % (k, c, name))
                continue
            if name not in inputs:                                       # present only as its children (values.a, method.shrink)
                unjudged.append("%s.%s: dynamic input" % (k, name)); continue
            val = inputs[name]
            if isinstance(val, list):                                    # a link
                if len(val) != 2 or str(val[0]) not in prompt:
                    problems.append("%s (%s): %r links to %r, which is not a node of this prompt" % (k, c, name, val)); continue
                src = prompt[str(val[0])]; outs = (types.get(src["class_type"]) or {}).get("output") or []
                if not isinstance(val[1], int) or not 0 <= val[1] < len(outs):
                    problems.append("%s (%s): %r takes output %r of %s (%s), which has %d" % (k, c, name, val[1], val[0], src["class_type"], len(outs))); continue
                if not types_fit(outs[val[1]], typ):
                    problems.append("%s (%s): %r wants %s, %s (%s) output %d gives %s" % (k, c, name, typ, val[0], src["class_type"], val[1], outs[val[1]]))
                continue
            if is_v3_template(typ):
                unjudged.append("%s.%s: dynamic input" % (k, name)); continue
            if typ in ("INT", "FLOAT"):
                try:
                    num = int(val) if typ == "INT" else float(val)
                except (TypeError, ValueError):
                    problems.append("%s (%s): %r = %r is not a %s" % (k, c, name, val, typ)); continue
                if "min" in extra and isinstance(extra["min"], (int, float)) and num < extra["min"]:
                    problems.append("%s (%s): %r = %r is below its min %r" % (k, c, name, val, extra["min"]))
                if "max" in extra and isinstance(extra["max"], (int, float)) and num > extra["max"]:
                    problems.append("%s (%s): %r = %r is above its max %r" % (k, c, name, val, extra["max"]))
                continue
            opts = combo_options(typ, extra)
            if opts is None:
                continue
            if any(key.endswith("upload") and extra.get(key) for key in extra):
                unjudged.append("%s.%s: upload combo" % (k, name)); continue
            if not opts:
                unjudged.append("%s.%s: empty option list" % (k, name)); continue
            if val not in opts:
                near = [o for o in opts if isinstance(o, str) and isinstance(val, str) and o.split("·")[0].strip() == val.split("·")[0].strip()]
                problems.append("%s (%s): %r = %r is not one of its %d options%s" % (
                    k, c, name, val, len(opts), (" (did it become %r?)" % near[0]) if len(near) == 1 else ""))
    return problems, unjudged


# ---------------------------------------------------------------- the two commands

def get(server, path, timeout):
    with urllib.request.urlopen("http://%s%s" % (server, path), timeout=timeout) as r:
        return json.loads(r.read())


def save(pkg, server):
    pkg = pathlib.Path(pkg)
    try:
        stats = get(server, "/system_stats", 10)["system"]
    except Exception as e:
        print("objinfo: no ComfyUI answering at %s (%s): `podctl tunnel <pod>` for the pod, `testbed.sh --server` here" % (server, e), file=sys.stderr)
        return 2
    info = get(server, "/object_info", 180)
    used = used_classes(pkg)
    payload = {
        "comfyui": stats.get("comfyui_version"),
        "frontend": stats.get("required_frontend_version"),
        "server": server,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "missing": [c for c in used if c not in info],
        "frontend_only": [],
        "venv_gated": {},
        "types": {c: info[c] for c in used if c in info},
    }
    out = cache_path(pkg)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print("objinfo %s: %d of %d classes cached from ComfyUI %s at %s -> %s" % (pkg.name, len(payload["types"]), len(used), payload["comfyui"], server, out))
    if payload["missing"]:
        print("  MISSING there (a finding, not a number): %s" % ", ".join(payload["missing"]))
        return 1
    return 0


def check(pkg):
    pkg = pathlib.Path(pkg)
    prompts = prompts_of(pkg)
    if not prompts:
        print("objinfo %s: no *.api.json prompts: nothing to check" % pkg.name)
        return 0
    cp = cache_path(pkg)
    if not cp.is_file():
        print("objinfo %s: %d prompts NOT checked: no %s (save one: objinfo.py save %s --server <the pod through podctl tunnel>)" % (
            pkg.name, len(prompts), CACHE, pkg))
        return 0
    cache = json.loads(cp.read_text(encoding="utf-8"))
    types = cache.get("types") or {}
    bad = 0; nodes = 0; skipped = 0
    for p in prompts:
        prompt = json.loads(p.read_text(encoding="utf-8"))
        nodes += len(prompt)
        problems, unjudged = check_prompt(prompt, types, cache.get("venv_gated") or {})
        skipped += len(unjudged)
        for line in problems:
            print("  %s %s" % (p.relative_to(pkg), line))
        bad += len(problems)
    print("objinfo %s: %d prompts, %d nodes against ComfyUI %s (cached from %s, %s): %s%s" % (
        pkg.name, len(prompts), nodes, cache.get("comfyui"), cache.get("server"), cache.get("generated"),
        "%d problem(s)" % bad if bad else "OK", ", %d inputs unjudged (upload combos, empty lists, dynamic inputs)" % skipped if skipped else ""))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="objinfo.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("save", help="cache the classes the package uses from a live ComfyUI's /object_info")
    p.add_argument("pkg"); p.add_argument("--server", default=os.environ.get("BASE_SERVER") or "127.0.0.1:8188")
    p = sub.add_parser("check", help="every *.api.json of each package against its cache, offline")
    p.add_argument("pkgs", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "save":
        return save(a.pkg, a.server)
    return max(check(d) for d in a.pkgs)


if __name__ == "__main__":
    sys.exit(main())
