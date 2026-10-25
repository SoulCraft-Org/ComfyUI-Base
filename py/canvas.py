"""ComfyUI Base — the canvas library: what every workflow package's suite and canvas tool share.

Each function was written against the real frontend (ComfyUI 1.49.6) and checked there. Nothing here knows a package:
ids, titles, budgets and the display come from the package's `CANVAS = CanvasSpec(...)` in its
suite.py, which `load_spec()` reads by ast so the tools in `_build/canvas/` see the same declaration
the tests do.

Contents
  graph helpers      wvals, _sg_defs, _all_nodes, byid (subgraph-aware), raw_links, links, LinkMap,
                     folds / inst_of / fold_map (declared definitions), eff_mode, push_instance_modes
  the bypass model   _valid, bypass_slot_index, _resolve_input_f, resolve_frontend, _lift, resolve, isrc, _in
                     -- the frontend's own semantics (executionUtil.ts + ExecutableNodeDTO.ts), 0-divergent
                     against app.graphToPrompt() in seven radio states
  geometry           TITLE_H, GRID, PREVIEW_RESERVE, CHROME_LEFT/TOP, nbound, gb, ov, contains, center_in, group,
                     membership (rgthree's centre rule), _extent, _canvas_links, _area, structure_sha, note_height
  budgets            Budgets (corpus-derived defaults), LOD cliff per dpr
  the declaration    CanvasSpec, load_spec(pkg_dir), package_files(pkg_dir)
"""
import ast, dataclasses, glob, hashlib, json, math, os, re
from pathlib import Path


def text_host():
    """3.27.2: what the frontend's text measurement depends on besides the frontend itself: the OS, the machine and the
    fonts it finds (Chromium asks fontconfig on Linux). The same frontend computed 17 of a package's nodes at other sizes
    on a Blacksmith ARM runner than on the HF runner that photographed them (2026-10-25). The fonts are a fingerprint of
    the sorted family names `fc-list` reports ("" where there is no fc-list)."""
    import platform, subprocess, sys
    try:
        out = subprocess.run(["fc-list", "--format", "%{family}\n"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    fams = sorted({f.strip() for line in out.splitlines() for f in line.split(",") if f.strip()})
    return {"platform": sys.platform, "machine": platform.machine(),
            "fonts": hashlib.sha256("\n".join(fams).encode()).hexdigest()[:16] if fams else ""}

# ---------------------------------------------------------------- graph helpers
def wvals(n):
    wv = n.get("widgets_values")
    return list(wv.values()) if isinstance(wv, dict) else (wv or [])
def _sg_defs(wf): return wf.get("definitions", {}).get("subgraphs", []) or []
def _all_nodes(wf):
    """Main canvas + every subgraph interior. A scan over wf["nodes"] alone now
    passes VACUOUSLY for anything that was encapsulated."""
    out = list(wf["nodes"])
    for sg in _sg_defs(wf): out.extend(sg.get("nodes", []) or [])
    return out
def byid(wf):
    """Node map that SEES THROUGH subgraphs (V5.6.0).

    Encapsulation moved 165 nodes into definitions.subgraphs. A map over
    wf["nodes"] alone would blind every wiring test that names a node by id --
    the tests would not be checking less, they would be unable to look.

    Interior nodes are also annotated with the CANVAS box of the subgraph instance
    that contains them. Their own `pos` is subgraph-LOCAL, so a geometry check
    against a main-canvas group box would silently compare two coordinate spaces.
    The annotation is written onto the node (not a copy) so callers that mutate
    `mode` -- apply_combo does -- still affect the graph they were handed."""
    d = {n["id"]: n for n in wf["nodes"]}
    inst = {}
    for n in wf["nodes"]:
        for sg in _sg_defs(wf):
            if n.get("type") == sg["id"]:
                inst[sg["id"]] = n
    for sg in _sg_defs(wf):
        holder = inst.get(sg["id"])
        for n in sg.get("nodes", []):
            if holder is not None:
                n["_canvas_pos"] = holder["pos"]
                n["_canvas_size"] = [80, 0] if holder.get("flags", {}).get("collapsed") else holder["size"]   # a collapsed instance is an 80px stub
            d.setdefault(n["id"], n)
    return d

def folds(wf, names):
    """{instance id: frozenset(interior ids)} for every definition named in `names` that is present, one instance each."""
    out = {}
    for sg in _sg_defs(wf):
        if sg["name"] not in names: continue
        inst = [n["id"] for n in wf["nodes"] if n.get("type") == sg["id"]]
        if len(inst) == 1: out[inst[0]] = frozenset(n["id"] for n in sg.get("nodes", []) or [])
    return out
def inst_of(wf, name):
    """The instance id of a declared fold (or any definition) by name."""
    sg = [d for d in _sg_defs(wf) if d["name"] == name]
    assert len(sg) == 1, f"definition {name!r}: {len(sg)} found"
    ids = [n["id"] for n in wf["nodes"] if n.get("type") == sg[0]["id"]]
    assert len(ids) == 1, f"definition {name!r}: {len(ids)} instances"
    return ids[0]
def fold_map(wf, names):
    """interior id -> instance id, so a declaration written in member ids still names the row."""
    return {m: i for i, ms in folds(wf, names).items() for m in ms}
def eff_mode(B, L, nid):
    """The mode the frontend acts on: an interior node inherits a non-live instance's mode."""
    inst = L.owner.get(nid)
    if inst is not None and inst in B and B[inst].get("mode", 0): return B[inst]["mode"]
    return B[nid].get("mode", 0)
def push_instance_modes(wf):
    """Mark the interior of a bypassed/muted instance as not-in-the-prompt.

    The frontend never expands such an instance (executionUtil.ts), so its interior nodes are
    not queued; pushing the mode down keeps membership walks honest. RESOLUTION does not go
    through the interior at all -- resolve_frontend answers at the instance's own slots --
    so this is about inclusion, not about what a consumer receives. Only a NON-ZERO instance
    mode is pushed: a live instance must leave its interior alone."""
    defs = {sg["id"]: sg for sg in _sg_defs(wf)}
    for n in wf["nodes"]:
        sg = defs.get(n.get("type"))
        if sg is not None and n.get("mode", 0):
            for x in sg.get("nodes", []) or []: x["mode"] = n["mode"]
    return wf
def raw_links(wf):
    """Links exactly as STORED, per graph, with no boundary resolution.

    Integrity tests must see the real structure: `links()` resolves subgraph
    boundaries so a wiring trace reads as one hop, which is right for tracing and
    wrong for asserting that every stored endpoint exists."""
    d = {l[0]: list(l) for l in wf["links"]}
    for sg in _sg_defs(wf):
        for lk in sg.get("links", []) or []:
            d[lk["id"]] = [lk["id"], lk["origin_id"], lk["origin_slot"],
                           lk["target_id"], lk["target_slot"], lk["type"]]
    return d
def links(wf):
    """Link map spanning the main graph AND every subgraph (V5.6.0).

    Encapsulation must be invisible to a wiring test. Four cases are resolved so
    that a chain crossing a boundary still reads as ONE hop between real nodes:

      inner link, origin -10   -> origin becomes the outer producer
      inner link, target -20   -> target becomes the outer consumer
      outer link from instance -> origin becomes the inner producer
      outer link into instance -> target becomes the inner consumer

    Inner links use the OBJECT form and are normalised to the array form. Inner
    and outer ids share one namespace (the builder mints both from the same
    sequence), so merging cannot collide."""
    d = {l[0]: list(l) for l in wf["links"]}
    for sg in _sg_defs(wf):
        inner = sg.get("links", []) or []
        insts = {n["id"] for n in wf["nodes"] if n.get("type") == sg["id"]}
        # inner view of the boundary
        prod, cons = {}, {}
        for lk in inner:
            if lk["target_id"] == -20: prod[lk["target_slot"]] = (lk["origin_id"], lk["origin_slot"])
            if lk["origin_id"] == -10: cons.setdefault(lk["origin_slot"], []).append((lk["target_id"], lk["target_slot"]))
        # outer view of the boundary
        in_src, out_dst = {}, {}
        for l in wf["links"]:
            if l[3] in insts: in_src[l[4]] = (l[1], l[2])
            if l[1] in insts: out_dst.setdefault(l[2], []).append((l[3], l[4]))
        # 1-2. inner links, boundaries resolved outward
        for lk in inner:
            o, os_, t, ts, ty = (lk["origin_id"], lk["origin_slot"],
                                 lk["target_id"], lk["target_slot"], lk["type"])
            if o == -10 and t == -20: continue
            if o == -10:
                src = in_src.get(os_)                      # None = the instance supplies a widget value, not a link
                d[lk["id"]] = [lk["id"], src[0] if src else None, src[1] if src else 0, t, ts, ty]
            elif t == -20:
                dst = out_dst.get(ts)                      # None = the subgraph output is unconsumed
                d[lk["id"]] = [lk["id"], o, os_, dst[0][0] if dst else None, dst[0][1] if dst else 0, ty]
            else:
                d[lk["id"]] = [lk["id"], o, os_, t, ts, ty]
        # 3-4. outer links, instance endpoints resolved inward
        for l in wf["links"]:
            e = d.get(l[0])
            if e is None: continue
            if l[1] in insts and l[2] in prod:
                e[1], e[2] = prod[l[2]]
            if l[3] in insts and l[4] in cons:
                e[3], e[4] = cons[l[4]][0]

    # A chain that leaves subgraph A and enters subgraph B needs BOTH ends
    # rewritten. One pass rewrites one of them and leaves the other pointing at an
    # instance id, so iterate to a fixed point.
    inst_ids = {n["id"] for n in wf["nodes"] if any(n.get("type") == sg["id"] for sg in _sg_defs(wf))}
    prod_all, cons_all = {}, {}
    for sg in _sg_defs(wf):
        ids = {n["id"] for n in wf["nodes"] if n.get("type") == sg["id"]}
        for lk in sg.get("links", []) or []:
            if lk["target_id"] == -20:
                for iid in ids: prod_all[(iid, lk["target_slot"])] = (lk["origin_id"], lk["origin_slot"])
            if lk["origin_id"] == -10:
                for iid in ids: cons_all.setdefault((iid, lk["origin_slot"]), []).append((lk["target_id"], lk["target_slot"]))
    for _ in range(8):
        changed = False
        for e in d.values():
            if e[1] in inst_ids and (e[1], e[2]) in prod_all:
                e[1], e[2] = prod_all[(e[1], e[2])]; changed = True
            if e[3] in inst_ids and (e[3], e[4]) in cons_all:
                e[3], e[4] = cons_all[(e[3], e[4])][0]; changed = True
        if not changed: break
    out = LinkMap(d)
    out.wf, out.raw = wf, raw_links(wf)
    out.defs = {sg["id"]: sg for sg in _sg_defs(wf)}
    out.inst = {n["id"]: out.defs[n["type"]] for n in wf["nodes"] if n.get("type") in out.defs}
    out.owner = {x["id"]: n["id"] for n in wf["nodes"] if n.get("type") in out.defs for x in out.defs[n["type"]].get("nodes", []) or []}
    return out
class LinkMap(dict):
    """What links() returns: the boundary-resolved map every wiring test indexes,
    plus -- as ATTRIBUTES, so `for lid, l in L.items()` never sees them -- what the
    frontend's own resolver needs: the links exactly as stored (`raw`), the
    instance -> definition map (`inst`) and interior node -> instance map (`owner`)."""
    wf = raw = defs = inst = owner = None
# ---------------------------------------------------------------- the FRONTEND's bypass model (V5.9.1)
# ComfyUI's frontend (executionUtil.ts + ExecutableNodeDTO.ts, 1.49.6) does NOT bypass a
# subgraph instance by bypassing its interior: a root node at mode 2/4 is never expanded,
# and a consumer's link into it resolves through the INSTANCE's own slots with
# _getBypassSlotIndex. The suite's older model (push a mode down, resolve node by node) was
# a different function and hid three defects (Handbook §16). The port below was diffed
# against app.graphToPrompt() in seven radio states (_build/probe.py): 0 divergent inputs.
# It is the suite's only model.

def _valid(a, b):
    """LiteGraph.isValidConnection: generic ('' / '*') matches anything; else case-insensitive
    equality, with comma-separated multi-type slots matched pairwise."""
    a = 0 if a in ("", "*", None) else a
    b = 0 if b in ("", "*", None) else b
    if not a or not b or a == b: return True
    a, b = str(a).lower(), str(b).lower()
    if "," not in a and "," not in b: return a == b
    return any(_valid(x, y) for x in a.split(",") for y in b.split(","))

def bypass_slot_index(n, slot, ctype):
    """ExecutableNodeDTO._getBypassSlotIndex: which INPUT a bypassed node's output `slot`
    passes through, for a consumer of type `ctype`. -1 = none (the output resolves to nothing)."""
    ins = n.get("inputs", []) or []; outs = n.get("outputs", []) or []
    otype = outs[slot]["type"] if slot < len(outs) else None
    if ctype in ("*", "", None):
        return slot if len(ins) > slot else (0 if ins else -1)     # frontend returns 0 and throws on a node with no inputs
    opp = ins[slot] if slot < len(ins) else None
    if opp is not None and _valid(opp["type"], otype) and _valid(opp["type"], ctype): return slot
    for i, inp in enumerate(ins):
        if inp["type"] == ctype: return i
    for i, inp in enumerate(ins):
        if _valid(inp["type"], otype) and _valid(inp["type"], ctype): return i
    return -1

def _resolve_input_f(B, L, nid, k, depth):
    """ExecutableNodeDTO.resolveInput: follow input k of node nid to a producer, through
    subgraph boundaries the way the frontend does (an inner -10 link becomes the owning
    instance's own input; an unlinked promoted input is a widget value, not a node)."""
    n = B[nid]; ins = n.get("inputs", []) or []
    if k >= len(ins): return None
    inp = ins[k]; lid = inp.get("link")
    if lid is None: return None
    raw = L.raw.get(lid)
    if raw is None: return None
    o, os_ = raw[1], raw[2]
    if o == -10:
        inst = L.owner.get(nid)
        if inst is None or inst not in B: return None
        return _resolve_input_f(B, L, inst, os_, depth + 1)
    return resolve_frontend(B, L, o, os_, inp.get("type"), depth + 1)

def resolve_frontend(B, L, nid, oslot, ctype=None, depth=0):
    """ExecutableNodeDTO.resolveOutput. Returns (node id, output slot) of the live producer
    the frontend would write into the prompt for output `oslot` of `nid`, or None."""
    if depth > 200 or nid not in B: return None
    n = B[nid]; mode = n.get("mode", 0)
    if mode == 2: return None
    if mode == 4:
        k = bypass_slot_index(n, oslot, ctype)
        return None if k == -1 else _resolve_input_f(B, L, nid, k, depth + 1)
    if nid in L.inst:                                   # a live instance: the -20 output node's inner link
        for lk in L.inst[nid].get("links", []) or []:
            if lk["target_id"] == -20 and lk["target_slot"] == oslot:
                if lk["origin_id"] == -10: return _resolve_input_f(B, L, nid, lk["origin_slot"], depth + 1)
                return resolve_frontend(B, L, lk["origin_id"], lk["origin_slot"], ctype, depth + 1)
        return None
    if n["type"] == "Any Switch (rgthree)":             # server-side: the first input that is not None
        for i, inp in enumerate(n.get("inputs", []) or []):
            if inp.get("link") is not None:
                r = _resolve_input_f(B, L, nid, i, depth + 1)
                if r: return r
        return None
    return (nid, oslot)

def _lift(B, L, nid, oslot):
    """A test that starts from an INTERIOR producer (L[...] is boundary-resolved) while the
    owning instance is not live is asking a question the frontend answers at the instance."""
    inst = L.owner.get(nid)
    if inst is None or inst not in B or B[inst].get("mode", 0) == 0: return nid, oslot, False
    for lk in L.inst[inst].get("links", []) or []:
        if lk["origin_id"] == nid and lk["origin_slot"] == oslot and lk["target_id"] == -20:
            return inst, lk["target_slot"], False
    return nid, oslot, True                             # feeds nothing outside: dead
def resolve(B, L, nid, oslot, depth=0, ctype=None):
    """What the frontend queues for output `oslot` of `nid`: (node, slot) of the live producer, or None.

    Callers may start from a boundary-resolved producer (an L[...] origin); _lift() turns that back into
    the question the frontend answers at the instance. A caller that names no consumer type is asking
    about a same-typed consumer, not a `*` one."""
    nid, oslot, dead = _lift(B, L, nid, oslot)
    if dead: return None
    if ctype is None:
        outs = B[nid].get("outputs", []) if nid in B else []
        ctype = outs[oslot]["type"] if oslot < len(outs) else None
    return resolve_frontend(B, L, nid, oslot, ctype)

def isrc(B, L, nid, name):
    """The live producer behind input `name` of `nid`, the way the frontend resolves it."""
    for i, inp in enumerate(B[nid]["inputs"]):
        if inp["name"] == name and inp.get("link") is not None:
            return _resolve_input_f(B, L, nid, i, 0)

def _in(B, node, name):
    return [i for i in B[node]["inputs"] if i["name"] == name][0]["link"]

# ---------------------------------------------------------------- geometry
NOTE_TYPES = ("MarkdownNote", "Label (rgthree)", "Bookmark (rgthree)")
TITLE_H = 30
PREVIEW_RESERVE = 200     # a LoadImage grows by this once a picture is loaded (useImagePreviewWidget); layout.py budgets it, so geometry counts it
def nbound(n):
    """Node rect on the MAIN canvas, including the 30px title bar.

    A node living inside a subgraph carries subgraph-local coordinates; byid()
    annotates it with its instance's box so group-membership geometry compares
    like with like. A LoadImage is measured with its preview reserve: the slot
    frames are sized for the loaded state, and that is the state you look at."""
    x, y = n.get("_canvas_pos", n["pos"])
    w, h = n.get("_canvas_size", n["size"])
    if "_canvas_pos" not in n and n.get("flags", {}).get("collapsed"): w, h = 80, 0
    if n.get("type") == "LoadImage": h += PREVIEW_RESERVE
    return (x, y - TITLE_H, x + w, y + h)
def gb(g): bx, by, bw, bh = g["bounding"]; return (bx, by, bx + bw, by + bh)
def ov(a, b): return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]
def contains(g, b): return g[0] <= b[0] and g[1] <= b[1] and g[2] >= b[2] and g[3] >= b[3]
def center_in(g, b):
    cx, cy = (b[0]+b[2])/2, (b[1]+b[3])/2
    return g[0] <= cx <= g[2] and g[1] <= cy <= g[3]
def group(wf, prefix): return [g for g in wf["groups"] if g["title"].startswith(prefix)][0]
CHROME_LEFT, CHROME_TOP = 76, 52   # css px the frontend (1.49.6) draws OVER the canvas: the left icon strip and the workflow tab bar (measured by snapshot.py)
GRID = 10
# Node types the server never registers: they are JS, resolved by the frontend before a prompt
# is built. THE canonical list — smoke.py imports it rather than keeping a second copy, because
# the two had already drifted: smoke.py learned about KJNodes' Get/Set and the LC123 bypassers
# in base 2.0.20 (a red pod run) and this copy never did, so every canvas tool refused a
# ProGrade-derived graph as "not this package's testbed".
FRONTEND_ONLY_TYPES = {"Label (rgthree)", "Bookmark (rgthree)", "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)",
                       "Fast Muter (rgthree)", "Fast Bypasser (rgthree)", "MarkdownNote", "Note", "Reroute", "PrimitiveNode",
                       "GetNode", "SetNode",                                    # KJNodes routing: web/js only (2.0.20)
                       "LC Bypasser", "LC Bypasser Panel", "LC Groups Bypasser"}  # LC123 panels: same

def node_box(n):
    """A root node's box as the frontend draws it: title bar included, a collapsed node an 80 px stub, no preview reserve."""
    x, y = n["pos"]; w, h = n["size"]
    if (n.get("flags") or {}).get("collapsed"): w, h = 80, 0
    return (x, y - TITLE_H, x + w, y + h)

def membership(wf, nodes=None):
    """{group title: [node ids]} by rgthree's rule: a node belongs to every frame the CENTRE of its drawn box lies in
    (fast_groups_service / LGraphGroup.recomputeInsideNodes) -- title bar included, a collapsed instance as its stub."""
    out = {g["title"]: [] for g in wf["groups"]}
    for n in (nodes if nodes is not None else wf["nodes"]):
        b = node_box(n); cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        for g in wf["groups"]:
            x, y, gw, gh = g["bounding"]
            if x <= cx <= x + gw and y <= cy <= y + gh: out[g["title"]].append(n["id"])
    return out
def _extent(wf):
    boxes = [nbound(n) for n in wf["nodes"]] + [gb(g) for g in wf["groups"]]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
def _canvas_links(wf):
    """Top-level (src, dst) pairs only.

    A link with an end inside a subgraph is dropped rather than guessed at. The instance stands
    in for its interior, and reading raw rows across a subgraph boundary makes this graph look
    like it contains a ten-node cycle through the analyzer, which it does not."""
    ids = {n["id"] for n in wf["nodes"] if n["type"] not in FRONTEND_ONLY_TYPES}; out = []      # a virtual node's wiring (the cuts muter) is not dataflow
    for row in wf.get("links") or []:
        if isinstance(row, list) and len(row) >= 4: s, t = row[1], row[3]
        elif isinstance(row, dict): s, t = row.get("origin_id"), row.get("target_id")
        else: continue
        if s in ids and t in ids and s != t: out.append((s, t))
    return out
def _area(r): return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])

def _idkey(nid):
    """Total order over a mixed int / "a:b" id space, integers first and numerically."""
    return (0, nid, "") if isinstance(nid, int) else (1, 0, str(nid))


def structure_sha(wf):
    """A hash of the graph's SHAPE -- per node: graph, id, type, title, input names and which are
    widgets, output names, widget count -- and deliberately NOT of pos/size/extra.ds. The frontend's
    computed sizes (_build/sizes.json) are keyed to it, so a relayout cannot invalidate the cache the
    relayout depends on, while a structural edit does."""
    import hashlib
    rows = []
    graphs = [("root", wf["nodes"])] + [(sg["id"], sg.get("nodes", []) or []) for sg in wf.get("definitions", {}).get("subgraphs", []) or []]
    for gkey, nodes in graphs:
        for n in nodes:
            wv = n.get("widgets_values_named") or n.get("widgets_values") or []
            rows.append((gkey, n["id"], n.get("type"), n.get("title") or "",
                         tuple((i.get("name"), "widget" in i) for i in n.get("inputs", []) or []),
                         tuple(o.get("name") for o in n.get("outputs", []) or []),
                         len(wv)))
    # A workflow derived from a subgraph-bearing file carries string node ids ("2682:0")
    # beside integers, and `(str(gkey), id)` raises TypeError on the mixed pair — which made
    # this function, and every test keyed to it, unreachable for any ProGrade-derived graph.
    #
    # Stringifying the id would fix the crash and REORDER every existing graph (ids would sort
    # "10" before "2"), silently invalidating every _build/sizes.json in the repo. So ints keep
    # their numeric order and strings sort after them: the hash of an all-integer graph is
    # unchanged, which is the whole point.
    return hashlib.sha256(repr(sorted(rows, key=lambda r: (str(r[0]), _idkey(r[1])))).encode("utf-8")).hexdigest()

def note_height(text, w, margin=20):
    """The wrap estimate the suite checks MarkdownNotes against (7.2 px per character, 18 px per line, 1.6 lines
    for a heading, 0.7 for a blank line) plus a margin, rounded up to the grid."""
    cpl = max(20, int((w - 24) / 7.2)); lines = 0
    for ln in text.split("\n"):
        s2 = ln.replace("**", "").replace("`", "")
        lines += 1.6 if ln.startswith("## ") else (0.7 if not s2.strip() else max(1, math.ceil(len(s2) / cpl)))
    return int(math.ceil((int(lines * 18 + 14) + margin) / GRID) * GRID)

# ---------------------------------------------------------------- budgets
def lod_cliff(dpr):
    """ComfyUI sets low_quality when ds.scale < min_font_size_for_lod / (NODE_TEXT_SIZE * sqrt(dpr)) -- 8 and 14 as shipped."""
    return 8 / (14 * dpr ** 0.5)

@dataclasses.dataclass(frozen=True)
class Budgets:
    """Thresholds from a 492-template corpus (the Comfy-Org templates): a package may override a field, never silently.
    Height is capped absolutely (5,770 px is the tallest official template); area per node (the corpus agrees on
    0.25-0.37 Mpx/node); fill and bloat are what a well-packed canvas reaches; flow is genuine back edges.

    `coord_max` is the odd one out and says so: it is a "nothing is stranded out in the void" guard, not a width
    budget, and the corpus has no width figure because height is what a reader pays for. On a canvas laid out from
    the origin it doubles as an undeclared width cap, and for a graph big enough that width and height trade against
    each other it can contradict `canvas_max_h`. Declare it rather than let two budgets fight."""
    canvas_max_h: int = 5000
    canvas_max_mpx_per_node: float = 0.30
    fill_min: float = 0.20
    group_bloat_max: float = 2.3            # every frame together, against the total node area
    leaf_bloat_max: float = 2.5             # ONE leaf frame, against the node area inside it
    coord_max: int = 20000                  # how far from the origin a node may sit — see the note below
    flow_max: float = 0.17
    title_max: int = 28
    notes_min: int = 5
    palette_max: int = 7
    grid: int = GRID

# ---------------------------------------------------------------- the declaration
@dataclasses.dataclass(frozen=True)
class CanvasSpec:
    """What a package declares once, in its suite.py, for the generic canvas tests and the canvas tools.

    Every field is a literal (tuples, strings, numbers, sets of ints) or a name bound to a literal in the same
    suite, so `load_spec()` can read it by ast without importing the suite (a suite may shell out at import)."""
    user_facing: tuple = ()                 # frame-title prefixes in the order a person works, row by row (the reading order)
    markers: tuple = ()                     # the glyphs that open a toggled row's title (rgthree panels match by title)
    cuda_only: tuple = ()                   # node types that cannot register on a Mac testbed; missing on load is not a defect
    start_card: int = 0                     # the MarkdownNote the file opens on (0 = none); the snapshot tier looks for it
    bookmarks: int = 0                      # rgthree Bookmarks that walk the path (keys 1/2/3); 0 = none
    cards_max: int = 8                      # MarkdownNotes allowed: section notes + one per text field
    display: str = "1512x982@2"             # the display the canvas is designed for: WxH css px @ dpr
    ungrouped_by_design: tuple = ()         # node ids allowed outside every frame
    tier_rules: tuple = ()                  # ((test name prefix, marker), ...): every tier test carries its gate
    version_key: str = "version"            # extra.<key> in the workflow JSON that carries the package version
    layout_env: str = "BASE_LAYOUT"         # env var; "0" skips geometry checks on a frontend-resaved copy
    nest: tuple = ()                        # (outer prefix, inner prefix) frame pairs that nest on purpose
    partial: tuple = ()                     # (prefix, prefix) frame pairs that overlap on purpose (a shared band of cells)
    preexisting_collisions: tuple = ()      # (id, id) node pairs allowed to overlap (a label over its own frame's title)
    opening_nodes: tuple = ()               # node ids that must be inside the usable opening area besides the start card
    tier_gates: tuple = ()                  # ((test name prefix, gate decorator), ...) for test_config_every_tier_test_carries_its_gate
    own_packs: int = 0                      # the package's own pack count (0 = not checked)
    extra_zip_members: tuple = ()           # files this package ships beyond the standard five (a Trainer, a models.py)
    stops: tuple = ()                       # ((name, frame-title prefix), ...): the stops of a render, for page.py's on-screen count
    budgets: Budgets = Budgets()

    @property
    def dpr(self): return int(self.display.rpartition("@")[2] or 1)
    @property
    def lod_cliff_design(self): return lod_cliff(self.dpr)
    @property
    def viewport(self):
        wh = self.display.partition("@")[0]; w, _, h = wh.partition("x"); return int(w), int(h)

def _workflow_of(pkg_dir):
    """The workflow the package's script names in WF_NAME (Title Case with the brand, spec 2026-09-12), or None."""
    import re as _re
    for s in sorted(glob.glob(os.path.join(pkg_dir, "*-script.sh"))):
        with open(s, encoding="utf-8") as fh:
            m = _re.search(r'^WF_NAME="([^"]+)"', fh.read(), _re.M)
        if m and os.path.exists(os.path.join(pkg_dir, m.group(1))):
            return os.path.join(pkg_dir, m.group(1))
    return None


def _zip_of(pkg_dir, one):
    """The package's zip, named for its brand's host (<name>-<host>.zip, py/brand.py). An extracted zip or a flat test
    tree has no brand.toml above it: then whichever host's zip is beside the workflow."""
    try:
        import brand
        host = brand.host_of(pkg_dir, "runpod")
    except Exception:                                    # no brand.py on the path, or a brand.toml naming an unknown host
        host = None
    if host:
        z = one("*-%s.zip" % host)
        if z: return z
    return one("*-runpod.zip") or one("*-verda.zip") or one("*-crusoe.zip") or one("*-local.zip")

def package_files(pkg_dir):
    """The package's four shipped files: the workflow its script names in WF_NAME (Title Case; '<name> Workflow.json' as the
    fallback), '<name>-script.sh', '<name>-handbook.md', suite.py; and its zip, <name>-<host>.zip for the brand's host."""
    pkg_dir = os.path.abspath(pkg_dir); name = os.path.basename(pkg_dir)
    one = lambda pat: (glob.glob(os.path.join(pkg_dir, pat)) or [None])[0]
    return {"dir": pkg_dir, "name": name, "workflow": _workflow_of(pkg_dir) or one("* Workflow.json"), "script": one("*-script.sh"),
            "handbook": one("*-handbook.md"), "suite": os.path.join(pkg_dir, "suite.py"), "pytest_ini": os.path.join(pkg_dir, "pytest.ini"),
            "zip": _zip_of(pkg_dir, one), "sizes": os.path.join(pkg_dir, "_build", "sizes.json"), "layout": os.path.join(pkg_dir, "_build", "layout.py"),
            "snapshots": os.path.join(pkg_dir, "_build", "snapshots")}

def load_spec(pkg_dir):
    """Read `CANVAS = CanvasSpec(...)` from the package's suite.py by ast. Names it uses must be literal assignments
    in the same file. Returns (spec, files)."""
    files = package_files(pkg_dir)
    tree = ast.parse(Path(files["suite"]).read_text(encoding="utf-8"))
    ns, canvas_node = {}, None
    for node in tree.body:
        if not isinstance(node, ast.Assign): continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                if t.id == "CANVAS": canvas_node = node.value; continue
                try: ns[t.id] = ast.literal_eval(node.value)
                except Exception: pass
    if canvas_node is None: raise SystemExit(f"{files['suite']}: no `CANVAS = CanvasSpec(...)` declaration")
    ns.update({"CanvasSpec": CanvasSpec, "Budgets": Budgets, "frozenset": frozenset, "set": set, "tuple": tuple})
    try:
        spec = eval(compile(ast.Expression(canvas_node), files["suite"], "eval"), {"__builtins__": {}}, ns)
    except NameError as e:
        raise SystemExit(f"{files['suite']}: CANVAS refers to {e} -- every name in the declaration must be a literal assignment in suite.py")
    return spec, files
