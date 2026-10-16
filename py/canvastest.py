"""ComfyUI Base — the generic canvas tests every workflow package runs against its own declaration.

In a package's suite.py, after its constants:

    from canvas import CanvasSpec, Budgets
    CANVAS = CanvasSpec(user_facing=USER_FACING, markers=MARKERS, cuda_only=CUDA_ONLY, start_card=992, ...)
    from canvastest import *          # pytest collects these test_* functions in the suite's module

The tests read the declaration through the `canvas` fixture (request.module.CANVAS) and the workflow through
the suite's own `wf` fixture (a module-scoped dict; one is provided here if the suite defines none). Every
number they assert is either the frontend's own (the level-of-detail cliff, computeSize() from sizes.json)
or a corpus threshold from canvas.Budgets, which a package may override field by field in its declaration.

Tiers (markers the suite's pytest.ini must declare): uiux, snapshot, config. The suite's marker loop gives
each test its tier by name prefix; the four legacy names (test_no_node_collisions, test_spacing_rules,
test_toggle_panels_fit_rows, test_note_boxes_fit_content) are uiux.

Each test was written against a real defect; the docstrings keep that history because it says why the
number is what it is."""
import hashlib, json, os, re, shutil, subprocess, sys, types, urllib.request, zipfile
import pytest
from pathlib import Path
from canvas import (wvals, _sg_defs, _all_nodes, byid, TITLE_H, GRID, PREVIEW_RESERVE, NOTE_TYPES, FRONTEND_ONLY_TYPES,
                    nbound, gb, ov, contains, center_in, _extent, _canvas_links, _area, structure_sha, package_files)

__all__ = ["canvas", "geometry", "snap", "server_answering",          # `wf` is deliberately not exported: a suite with its own keeps it; one without imports it by name
           "test_no_node_collisions", "test_spacing_rules", "test_toggle_panels_fit_rows", "test_note_boxes_fit_content",
           "test_uiux_primitive_titles_tell_the_truth", "test_uiux_group_titles_unique_and_sized", "test_uiux_every_node_is_readable",
           "test_uiux_every_toggle_row_can_show_selected", "test_uiux_opens_above_the_lod_cliff", "test_uiux_the_user_path_runs_along_the_top",
           "test_uiux_labels_fit_their_text", "test_uiux_the_user_path_reads_in_order", "test_uiux_bookmarks_walk_the_path",
           "test_uiux_canvas_within_budget", "test_uiux_canvas_is_not_mostly_empty", "test_uiux_groups_fit_their_contents",
           "test_uiux_links_flow_forward", "test_uiux_every_node_is_in_a_group", "test_uiux_the_ungrouped_pair_is_still_ungrouped",
           "test_uiux_positions_on_grid", "test_uiux_group_titles_are_short", "test_uiux_sections_are_documented", "test_uiux_group_palette_is_small",
           "test_uiux_every_node_is_at_least_its_computed_size", "test_uiux_cards_are_capped",
           "test_snapshot_opens_at_the_saved_view", "test_snapshot_is_above_the_lod_cliff_live", "test_snapshot_opening_screen_shows_the_start_card",
           "test_snapshot_no_node_is_smaller_than_the_frontend_draws_it", "test_snapshot_only_cuda_types_are_missing",
           "test_snapshot_loads_without_page_errors", "test_snapshot_console_errors_are_all_explained", "test_snapshot_sizes_cache_is_current",
           "test_config_sizes_cache_matches_the_workflow", "test_config_no_leftovers_from_packs_that_are_not_installed",
           "test_config_every_node_carries_a_properties_map", "test_config_the_shipping_zip_is_the_shipped_pair", "test_config_versions_agree",
           "test_config_the_layout_tool_reproduces_itself", "test_config_every_tier_test_carries_its_gate",
           "test_config_handbook_numbers_match_reality", "test_config_every_pack_is_pinned"]


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def canvas(request):
    """The package's declaration plus its files, resolved from the suite module that imported these tests."""
    spec = getattr(request.module, "CANVAS", None)
    if spec is None: pytest.skip("this suite declares no CANVAS = CanvasSpec(...)")
    files = package_files(os.path.dirname(os.path.abspath(request.module.__file__)))
    wf_path = getattr(request.module, "WF_PATH", None)                     # a suite may honour BASE_WF; follow it
    if wf_path: files["workflow"] = wf_path
    pkg = getattr(request.module, "PKG", None)
    server = os.environ.get("BASE_SERVER") or (os.environ.get(f"{pkg.id.upper()}_SERVER") if pkg is not None and getattr(pkg, "id", None) else None)
    return types.SimpleNamespace(spec=spec, files=files, pkg=pkg, module=request.module, server=server or "127.0.0.1:8188")


@pytest.fixture(scope="module")
def wf(canvas):
    """The workflow, once per module. A suite that defines its own `wf` shadows this one."""
    with open(canvas.files["workflow"], encoding="utf-8") as f: return json.load(f)


@pytest.fixture(scope="module")
def geometry(canvas):
    """Geometry checks apply to the shipped JSON, not to a copy the frontend re-saved (it reorders widgets and
    moves boxes). `<layout_env>=0` (default BASE_LAYOUT) skips them."""
    for var in {canvas.spec.layout_env, "BASE_LAYOUT"}:
        if os.environ.get(var, "1") == "0": pytest.skip(f"{var}=0: layout checks skipped (run against the pristine JSON for full coverage)")
    return True


@pytest.fixture(scope="module")
def server_answering(canvas):
    try:
        with urllib.request.urlopen(f"http://{canvas.server}/system_stats", timeout=5) as r: return json.loads(r.read())
    except Exception:
        return None


def _unregistered_types(canvas):
    """The workflow's node types the answering server does not register (frontend-only types, subgraph ids and the
    declared cuda-only types excepted): the same rule as snapshot.need_server, so the skip and the tool agree."""
    from canvas import FRONTEND_ONLY_TYPES
    try:
        with urllib.request.urlopen(f"http://{canvas.server}/object_info", timeout=120) as r: known = set(json.loads(r.read()))
    except Exception as e:
        pytest.skip(f"no /object_info from the ComfyUI at {canvas.server} ({e})")
    wf = json.loads(Path(canvas.files["workflow"]).read_text(encoding="utf-8"))
    subgraphs = wf.get("definitions", {}).get("subgraphs", []) or []
    defs = {d["id"] for d in subgraphs}
    used = {n["type"] for n in wf["nodes"]} | {n["type"] for d in subgraphs for n in d.get("nodes", []) or []}
    allowed = set(canvas.spec.cuda_only or ())
    return sorted(t for t in used if t not in known and t not in FRONTEND_ONLY_TYPES and t not in defs and t not in allowed)


def _snapshot_tool():
    """The base's photograph tool, when this is a repo checkout (the base zip ships py/, not _build/)."""
    from basetest import BASE_LIB
    p = os.path.join(str(BASE_LIB), "_build", "canvas", "snapshot.py")
    return p if os.path.exists(p) else None


@pytest.fixture(scope="module")
def snap(canvas, server_answering, tmp_path_factory):
    """One run of the photograph tool for the whole tier (about ten seconds), at the display the workflow is designed for.
    Needs the testbed server and uv (which resolves Playwright); without either, the tier skips."""
    tool = _snapshot_tool()
    if server_answering is None: pytest.skip(f"no ComfyUI answering at {canvas.server} (bash testbed.sh --server)")
    if shutil.which("uv") is None: pytest.skip("needs uv to run the photograph tool")
    if tool is None: pytest.skip("no base/comfyui-base/_build/canvas/snapshot.py (not a repo checkout)")
    # 2.0.56: NOTHING CONTROLS WHICH ComfyUI ANSWERS. The shared testbed is a long-lived process and can predate a
    # package's own node types; then the photograph tool exits 2 with "does not register [...]: not this package's
    # testbed" and this fixture used to turn that into an ERROR, which says "this package is broken" when the truth is
    # "the wrong server answered". A wrong server is a skip that names the types and the
    # server, exactly the gate snapshot.need_server applies, asked here first so the tier skips instead of erroring.
    missing = _unregistered_types(canvas)
    if missing:
        pytest.skip(f"the ComfyUI at {canvas.server} does not register {missing}: not this package's testbed "
                    f"(a private instance: BASE_SERVER=127.0.0.1:<port>)")
    out = tmp_path_factory.mktemp("snapshot")
    env = dict(os.environ, BASE_SERVER=canvas.server)
    r = subprocess.run(["uv", "run", "--quiet", tool, "--pkg", canvas.files["dir"], "--wf", canvas.files["workflow"], "--out", str(out),
                        "--sizes-out", str(out / "sizes.json"), "--viewport", canvas.spec.display, "--allow-missing", ",".join(canvas.spec.cuda_only)],
                       capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    view = json.loads(Path(out / "view.json").read_text(encoding="utf-8")); sizes = json.loads(Path(out / "sizes.json").read_text(encoding="utf-8"))
    return {"view": view, "sizes": sizes, "vp": view["viewports"][0], "out": out}


def _pair_in(pair, S):
    a, b = pair
    return any((a.startswith(x) and b.startswith(y)) or (b.startswith(x) and a.startswith(y)) for x, y in S)


def _is_user(canvas, t): return any(t.startswith(p) for p in canvas.spec.user_facing)


# ---------------------------------------------------------------- uiux: local hygiene
def test_no_node_collisions(wf, canvas, geometry):
    col = {(a["id"], b["id"]) for i, a in enumerate(wf["nodes"]) for b in wf["nodes"][i+1:] if ov(nbound(a), nbound(b))}
    assert col - set(map(tuple, canvas.spec.preexisting_collisions)) == set()


def test_spacing_rules(wf, canvas, geometry):
    """Every node sits inside its frame with the frontend's own margins; frames overlap only where the declaration
    says they nest (`nest`) or share a band on purpose (`partial`); unrelated frames keep 18 px apart."""
    NEST, PARTIAL = canvas.spec.nest, canvas.spec.partial
    for g in wf["groups"]:
        G = gb(g)
        for n in wf["nodes"]:
            b = nbound(n)
            if not ov(G, b): continue
            assert contains(G, b), f"node {n['id']} clipped by '{g['title']}'"
            p = (b[0]-G[0], b[1]-G[1], G[2]-b[2], G[3]-b[3])
            assert p[0] >= 10 and p[2] >= 10 and p[1] >= 28 and p[3] >= 5, f"'{g['title']}' node {n['id']} pads {p}"
    for i, a in enumerate(wf["groups"]):
        for b2 in wf["groups"][i+1:]:
            A, B = gb(a), gb(b2); pair = (a["title"], b2["title"])
            nested, partial = _pair_in(pair, NEST), _pair_in(pair, PARTIAL)
            if ov(A, B):
                assert nested or partial, f"unexpected overlap {pair}"
                if nested:
                    outer, inner = (A, B) if contains(A, B) else (B, A)
                    assert contains(outer, inner), f"NEST pair not nested {pair}"
                    assert min(inner[0]-outer[0], inner[1]-outer[1], outer[2]-inner[2], outer[3]-inner[3]) >= 14, f"tight nest {pair}"
            elif not (nested or partial):
                d = max(max(A[0]-B[2], B[0]-A[2]), max(A[1]-B[3], B[1]-A[3]))
                assert d >= 18, f"groups too close {pair} ({d}px)"


def test_toggle_panels_fit_rows(wf, canvas, geometry):
    """An rgthree Fast Groups panel lists one row per matched frame at 24 px each over a 34 px header (the frontend's
    computeSize for 2/3/4/5 rows is 82/106/130/154); a shorter box clips its rows."""
    for p in (n for n in wf["nodes"] if n["type"] in ("Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)")):
        pr = p.get("properties") or {}; mt, mc = pr.get("matchTitle", ""), pr.get("matchColors", "")
        rows = [g for g in wf["groups"] if (mc == "RED" and g.get("color") == "#A88") or (mt and mt in g["title"])]
        assert p["size"][1] >= 34 + len(rows) * 24, f"panel '{p.get('title')}' clips its rows ({len(rows)})"


def test_note_boxes_fit_content(wf, canvas, geometry):
    """A MarkdownNote box must hold its text at 7.2 px per character and 18 px per line (the wrap estimate
    canvas.note_height sizes new notes with)."""
    import math
    for n in wf["nodes"]:
        if n["type"] != "MarkdownNote": continue
        txt = n["widgets_values"][0]; w, h = n["size"]
        cpl = max(20, int((w - 24) / 7.2)); lines = 0
        for ln in txt.split("\n"):
            s2 = ln.replace("**", "").replace("`", "")
            lines += 1.6 if ln.startswith("## ") else (0.7 if not s2.strip() else max(1, math.ceil(len(s2) / cpl)))
        assert int(lines * 18 + 14) <= h, f"note {n['id']} '{n.get('title','')}' clips its text"


def test_uiux_primitive_titles_tell_the_truth(wf, canvas):
    """A primitive titled 'Steps · 8' must hold 8 — the title is what the user reads on the canvas."""
    for n in wf["nodes"]:
        if n["type"] in ("PrimitiveInt", "PrimitiveFloat") and " · " in n.get("title", ""):
            m = re.search(r" · (−?-?[0-9.]+)", n["title"])
            if m: assert float(m.group(1).replace("−", "-")) == float(wvals(n)[0]), (n["id"], n["title"], wvals(n))


def test_uiux_group_titles_unique_and_sized(wf, canvas, geometry):
    # font_size is OPTIONAL in the workflow schema — LiteGraph's LGraphGroup defaults it to 24, and a graph
    # inherited from an author who never touched it carries no key at all (most frames of a real graph). Reading it
    # unguarded turned a passing canvas into a KeyError, which tells the reader nothing about their layout.
    titles = [g["title"] for g in wf["groups"]]; assert len(titles) == len(set(titles)), "two frames share a title"
    for g in wf["groups"]:
        assert g.get("font_size", 24) >= 18 and g["bounding"][2] >= 150 and g["bounding"][3] >= 80, g["title"]


def test_uiux_every_node_is_readable(wf, canvas, geometry):
    for n in wf["nodes"]:
        assert n["size"][0] >= 60 and n["size"][1] >= 26, (n["id"], n["size"])
        cap = canvas.spec.budgets.coord_max
        assert abs(n["pos"][0]) < cap and abs(n["pos"][1]) < cap, (
            "node %s sits at %s, outside the declared %d-unit coordinate budget" % (n["id"], n["pos"][:2], cap))
        if n["type"] in ("PrimitiveString", "PrimitiveStringMultiline", "PrimitiveInt", "PrimitiveFloat", "PrimitiveBoolean"):
            assert n.get("title") and n["title"] != n["type"], f"primitive {n['id']} has no human title"


def test_uiux_every_toggle_row_can_show_selected(wf, canvas, geometry):
    """Every frame a panel can flip holds at least one node. Two reasons; the second one bit.

    An empty toggle has nothing to switch. And rgthree decides whether a row draws itself lit by
    asking `getGroupNodes(g).some(n => n.mode === ALWAYS)` (fast_groups_service.js) -- an empty
    group answers false for ever, so a row that IS the current selection draws itself off. Three
    rows once shipped empty, and all three were shipped DEFAULTS, so three of the seven panels
    looked unselected on every fresh load. A node in the group is the only lever, and it may be an
    inert one -- a Label is frontend-only and never executes.

    The markers are not uniform -- 🎚️ and 🖼️ carry a VS16 variation selector, 🎬 and 🔗 do not --
    which is exactly what made a fixed-width slice of the title look reasonable and be wrong; match
    by startswith. There is deliberately no exemption list: a row that genuinely has to be empty
    should fail here and be argued for."""
    for g in wf["groups"]:
        t = g["title"]
        if not any(t.startswith(m) for m in canvas.spec.markers): continue
        inside = [n["id"] for n in wf["nodes"] if contains(gb(g), nbound(n))]
        assert inside, "toggle row %r is empty: it cannot be switched, and rgthree will draw it unlit even when it is the selected row" % t


# ---------------------------------------------------------------- uiux: the canvas as an interface
# Every budget below is measured, never chosen. Two sources: the frontend's own level-of-detail cliff
# (low_quality <=> ds.scale < min_font_size_for_lod / (NODE_TEXT_SIZE * sqrt(dpr)), 8 and 14 as shipped:
# 0.571 at dpr 1, 0.404 at dpr 2 -- below it ComfyUI draws no widget text and hides DOM widgets), and the
# 492 official Comfy-Org templates (height max 5,770 px, area max 24.6 Mpx, fill min 0.192 / median 0.602,
# backward-link fraction median 0.000 / p90 0.053, saved scale median 0.59). See canvas.Budgets.

def test_uiux_opens_above_the_lod_cliff(wf, canvas):
    """The saved view decides whether the workflow opens legible or blank on the display it is designed for."""
    ds = (wf.get("extra") or {}).get("ds") or {}
    assert ds.get("scale") is not None, "no saved view: the workflow opens wherever ComfyUI last was"
    cliff = canvas.spec.lod_cliff_design
    assert ds["scale"] >= cliff, ("opens at scale %s, under the %.3f level-of-detail cliff at dpr %d: every widget renders blank"
                                  % (ds["scale"], cliff, canvas.spec.dpr))


def test_uiux_the_user_path_runs_along_the_top(wf, canvas, geometry):
    """In every column of the canvas, what you touch sits above what runs itself (declared by `user_facing`)."""
    gs = [(g["title"], gb(g)) for g in wf["groups"]]
    bad = []
    for ta, A in gs:
        if not _is_user(canvas, ta): continue
        for tb, B in gs:
            if _is_user(canvas, tb) or ta == tb: continue
            same_column = A[0] < B[2] and A[2] > B[0]
            if same_column and B[1] < A[1] and not contains(B, A) and not contains(A, B):
                bad.append("%r sits below %r" % (ta[:30], tb[:30]))
    assert not bad, "automatic frames above ones you use: %s" % sorted(set(bad))[:6]


def test_uiux_labels_fit_their_text(wf, canvas, geometry):
    """A Label paints its text at its own fontSize and ignores its node box; Arial averages 0.58 of the font
    size per character, so this checks against a deliberately forgiving 0.5."""
    bad = []
    for n in wf["nodes"]:
        if n["type"] != "Label (rgthree)": continue
        f = float((n.get("properties") or {}).get("fontSize", 14))
        need_w, need_h = len(n.get("title", "")) * f * 0.5, f * 1.2
        if n["size"][0] < need_w or n["size"][1] < need_h:
            bad.append("%s: %dx%d for %d chars at %dpx (needs %dx%d)" % (n["title"][:24], n["size"][0], n["size"][1], len(n["title"]), f, need_w, need_h))
    assert not bad, "labels whose text will not fit their box: %s" % bad


def test_uiux_the_user_path_reads_in_order(wf, canvas, geometry):
    """Walking the frames in the declared order (`user_facing`), every step goes right or down; a step that goes
    up AND left is the path doubling back on itself."""
    seq = []
    for g in wf["groups"]:
        for i, pref in enumerate(canvas.spec.user_facing):
            if g["title"].startswith(pref):
                seq.append((i, (gb(g)[1], gb(g)[0]), g["title"])); break
    seq.sort()
    worst = [(a, b) for (_, (ay, ax), a), (_, (by, bx), b) in zip(seq, seq[1:]) if bx < ax and by < ay]
    assert not worst, "the path doubles back on itself: %s" % [f"{a[:22]} then {b[:22]}" for a, b in worst[:5]]


def test_uiux_bookmarks_walk_the_path(wf, canvas, geometry):
    """The declared number of rgthree Bookmarks, on distinct spots, the first at the start of the path."""
    if not canvas.spec.bookmarks: pytest.skip("the declaration expects no bookmarks")
    marks = [n for n in wf["nodes"] if n["type"] == "Bookmark (rgthree)"]
    assert len(marks) == canvas.spec.bookmarks, [n.get("title") for n in marks]
    boxes = [nbound(n) for n in wf["nodes"]]
    x0 = min(b[0] for b in boxes); x1 = max(b[2] for b in boxes)
    xs = sorted(n["pos"][0] for n in marks)
    assert xs[0] - x0 < 0.15 * (x1 - x0), "the first bookmark is not at the start of the path"
    assert len({tuple(n["pos"]) for n in marks}) == len(marks), "two bookmarks land on the same spot"


def test_uiux_canvas_within_budget(wf, canvas, geometry):
    b = canvas.spec.budgets; x0, y0, x1, y1 = _extent(wf); w, h = x1 - x0, y1 - y0
    assert h <= b.canvas_max_h, ("canvas is %.0f px tall; the tallest of 492 official templates is 5,770" % h)
    per = w * h / 1e6 / len(wf["nodes"])
    assert per <= b.canvas_max_mpx_per_node, ("%.3f Mpx of canvas per node; official templates run 0.25 to 0.37" % per)


def test_uiux_canvas_is_not_mostly_empty(wf, canvas, geometry):
    x0, y0, x1, y1 = _extent(wf)
    fill = sum(_area(nbound(n)) for n in wf["nodes"]) / ((x1 - x0) * (y1 - y0))
    assert fill >= canvas.spec.budgets.fill_min, ("%.3f of the canvas is node; the emptiest of 492 official templates is 0.192" % fill)


def test_uiux_groups_fit_their_contents(wf, canvas, geometry):
    """A leaf frame four times the size of what it holds reads as a region, not a box; frames as a whole may cover
    the node area at most `group_bloat_max` times, counting shared ground once."""
    def _union(rects):
        xs = sorted({v for r in rects for v in (r[0], r[2])}); tot = 0.0
        for i in range(len(xs) - 1):
            x0, x1 = xs[i], xs[i + 1]
            spans = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
            cy, h = None, 0.0
            for a0, b0 in spans:
                if cy is None or a0 > cy: h += b0 - a0; cy = b0
                elif b0 > cy: h += b0 - cy; cy = b0
            tot += (x1 - x0) * h
        return tot
    tot_g = _union([gb(g) for g in wf["groups"]])
    for g in wf["groups"]:
        G = gb(g); a = sum(_area(nbound(n)) for n in wf["nodes"] if contains(G, nbound(n)))
        parent = any(o is not g and contains(G, gb(o)) for o in wf["groups"])
        span = any(o is not g and ov(G, gb(o)) and not contains(G, gb(o)) and not contains(gb(o), G) for o in wf["groups"])
        if a >= 20000 and not parent and not span:
            # The one budget that had no override, which read as an oversight rather than a principle:
            # pack_grid gives a column the width of its widest node, so a frame holding nodes of very
            # different widths loses area it can do nothing about. A package declares its own with a
            # measured reason, the way it declares fill_min and group_bloat_max.
            assert _area(G) <= canvas.spec.budgets.leaf_bloat_max * a, (
                "group %r is %.1fx the area it holds" % (g["title"], _area(G) / a))
    node = sum(_area(nbound(n)) for n in wf["nodes"])
    assert tot_g <= canvas.spec.budgets.group_bloat_max * node, ("group frames cover %.2fx the node area" % (tot_g / node))


def test_uiux_links_flow_forward(wf, canvas, geometry):
    """Left to right is how a ComfyUI graph is read -- but only where an order exists. A backward link between
    two frames with a one-way dependency is a placement defect; a link between a user-facing frame and the
    machinery runs whichever way the reading order dictates and is not counted (the cockpit gutter)."""
    b = canvas.spec.budgets
    pos = {n["id"]: n["pos"][0] for n in wf["nodes"]}
    owner, links = {}, _canvas_links(wf)
    for n in wf["nodes"]:
        holding = [g for g in wf["groups"] if contains(gb(g), nbound(n))]
        if holding: owner[n["id"]] = min(holding, key=lambda g: g["bounding"][2] * g["bounding"][3])["title"]
    edges = {(owner[s], owner[t]) for s, t in links if s in owner and t in owner and owner[s] != owner[t]}
    genuine_all = [(s, t) for s, t in links if pos[s] > pos[t] and s in owner and t in owner and owner[s] != owner[t] and (owner[t], owner[s]) not in edges]
    genuine = [(s, t) for s, t in genuine_all if _is_user(canvas, owner[s]) == _is_user(canvas, owner[t])]
    if not links: pytest.skip("no top-level links")
    frac = len(genuine) / len(links)
    assert frac <= b.flow_max, ("%d of %d links run right-to-left between groups that HAVE an order on one side of the cockpit gutter (%.1f%%; "
                                "%d = %.1f%% counting gutter crossings); official p90 is 5.3%%" % (len(genuine), len(links), 100 * frac, len(genuine_all), 100 * len(genuine_all) / len(links)))


def test_uiux_every_node_is_in_a_group(wf, canvas, geometry):
    """A node in no group belongs to no step of the flow, and no panel can ever reach it. The exemption is a list
    of ids in the declaration, not a predicate."""
    loose = sorted(n["id"] for n in wf["nodes"] if n["id"] not in canvas.spec.ungrouped_by_design and not any(contains(gb(g), nbound(n)) for g in wf["groups"]))
    assert not loose, "nodes in no group: %s" % loose


def test_uiux_the_ungrouped_pair_is_still_ungrouped(wf, canvas, geometry):
    """The other half of the exemption: a licence for exactly the declared nodes, not a loophole."""
    if not canvas.spec.ungrouped_by_design: pytest.skip("no ungrouped-by-design nodes declared")
    B = byid(wf)
    for nid in canvas.spec.ungrouped_by_design:
        assert not any(ov(gb(g), nbound(B[nid])) for g in wf["groups"]), ("node %d must touch no group: inside a toggled one it is bypassed with it" % nid)


def test_uiux_positions_on_grid(wf, canvas, geometry):
    """Off-grid positions are the fingerprint of hand-dragging."""
    grid = canvas.spec.budgets.grid
    off = sorted(n["id"] for n in wf["nodes"] if n["pos"][0] % grid or n["pos"][1] % grid)
    assert not off, "%d nodes off the %dpx grid: %s" % (len(off), grid, off[:12])


def test_uiux_group_titles_are_short(wf, canvas, geometry):
    """A title is a label, not a paragraph."""
    tm = canvas.spec.budgets.title_max
    long = [(len(g["title"]), g["title"]) for g in wf["groups"] if len(g["title"]) > tm]
    assert not long, "group titles over %d chars (documentation belongs in a MarkdownNote): %s" % (tm, sorted(long, reverse=True)[:4])


def test_uiux_sections_are_documented(wf, canvas, geometry):
    """Official templates carry a median of 1 note for 6 nodes; every note sits beside the section it explains."""
    notes = [n for n in wf["nodes"] if n["type"] in ("MarkdownNote", "Note")]
    assert len(notes) >= canvas.spec.budgets.notes_min, "only %d explanatory notes for %d nodes" % (len(notes), len(wf["nodes"]))
    loose = [n["id"] for n in notes if not any(contains(gb(g), nbound(n)) for g in wf["groups"])]
    assert not loose, "notes belong beside the section they explain; these sit in no group: %s" % loose


def test_uiux_group_palette_is_small(wf, canvas, geometry):
    colours = {g.get("color") for g in wf["groups"]}
    assert len(colours) <= canvas.spec.budgets.palette_max, "%d distinct group colours" % len(colours)


def test_uiux_cards_are_capped(wf, canvas):
    """The canvas is not the handbook: at most `cards_max` MarkdownNotes (section notes + one per text field)."""
    cards = sorted(n.get("title", "") for n in wf["nodes"] if n["type"] == "MarkdownNote")
    assert len(cards) <= canvas.spec.cards_max, "the canvas is not the handbook: %s" % cards


def test_uiux_every_node_is_at_least_its_computed_size(wf, canvas, geometry):
    """Offline twin of the snapshot check: stored size >= the frontend's computeSize() from _build/sizes.json, for
    every node the cache knows and the frontend draws itself (rgthree's labels and bookmarks draw their own)."""
    sizes = canvas.files["sizes"]
    if not os.path.exists(sizes): pytest.skip("no _build/sizes.json beside the workflow")
    cache = json.loads(Path(sizes).read_text(encoding="utf-8"))["nodes"]
    self_drawn = {"Label (rgthree)", "Bookmark (rgthree)"}
    bad = []
    graphs = [("root", wf["nodes"])] + [(sg["id"], sg.get("nodes", []) or []) for sg in _sg_defs(wf)]
    for gkey, nodes in graphs:
        for n in nodes:
            key = str(n["id"]) if gkey == "root" else f"{gkey}:{n['id']}"
            rec = cache.get(key)
            if rec is None or rec.get("missing") or n["type"] in self_drawn or (n.get("flags") or {}).get("collapsed"): continue
            s = n["size"]; s = [s.get("0"), s.get("1")] if isinstance(s, dict) else s
            if float(s[0]) < rec["computed"][0] - 0.5 or float(s[1]) < rec["computed"][1] - 0.5: bad.append((key, n["type"], [float(s[0]), float(s[1])], rec["computed"]))
    assert bad == [], bad[:10]


# ---------------------------------------------------------------- snapshot: the real frontend
def test_snapshot_opens_at_the_saved_view(wf, canvas, snap):
    """The frontend restores extra.ds only when a node overlaps the viewport; otherwise it silently fits the
    view and the opening screen is not the one the file describes."""
    want, got = wf["extra"]["ds"], snap["vp"]["ds_applied"]
    assert abs(got["scale"] - want["scale"]) < 1e-9 and [float(v) for v in got["offset"]] == [float(v) for v in want["offset"]], (want, got)


def test_snapshot_is_above_the_lod_cliff_live(canvas, snap):
    """canvas.low_quality is the frontend's own verdict on whether widget text is drawn."""
    assert snap["vp"]["low_quality"] is False, snap["vp"]


def test_snapshot_opening_screen_shows_the_start_card(wf, canvas, snap):
    """What the first screen must hold on the display the canvas is designed for: the declared opening nodes (the
    start card, the first bookmark), inside the area the frontend's own chrome does not cover."""
    ids = tuple(i for i in (canvas.spec.start_card,) + tuple(canvas.spec.opening_nodes) if i)
    if not ids: pytest.skip("no start card declared")
    B = byid(wf); u = snap["vp"]["usable_units"]
    area = (u[0], u[1], u[0] + u[2], u[1] + u[3])
    for nid in ids:
        assert contains(area, nbound(B[nid])), f"node {nid} {nbound(B[nid])} is not inside the usable opening area {area}"


def test_snapshot_no_node_is_smaller_than_the_frontend_draws_it(canvas, snap):
    """The frontend grows a node on load to computeSize(); a stored size below that is a box the layout placed
    smaller than the pod shows. Root and interior alike."""
    assert snap["view"]["sizes"]["shorter_than_computed"] == 0, snap["view"]["sizes"]["short_keys"]


def test_snapshot_only_cuda_types_are_missing(canvas, snap):
    assert set(snap["vp"]["missing"]) <= set(canvas.spec.cuda_only), snap["vp"]["missing"]


def test_snapshot_loads_without_page_errors(canvas, snap):
    assert snap["view"]["page_errors"] == [], snap["view"]["page_errors"]


def test_snapshot_console_errors_are_all_explained(canvas, snap):
    """Every console error the load raised matches a KNOWN pattern with a written reason (snapshot.py); an unexplained one
    is a finding — the frontend changed, or the graph did — never a number to shrug at."""
    view = snap["view"]
    assert "console_unexplained" in view, "re-run the photograph tool (snapshot.py) — this view.json predates the classifier"
    assert view["console_unexplained"] == [], view["console_unexplained"]


def test_snapshot_sizes_cache_is_current(canvas, snap):
    """_build/sizes.json is what the layout tool sizes against. A fresh run must compute the same sizes for every
    key, or the cache is from another frontend or another graph. 3.8.2: only on the OS that photographed it. The
    frontend measures text with the system's fonts, so a Linux runner computes other widths than the Mac that made
    the cache (CI, 2026-10-16: 22 of a package's nodes off on Linux, 1 on the Mac, same frontend). A cache without
    the field predates 3.8.2 and was made on the Mac, where the layout tool runs."""
    sizes = canvas.files["sizes"]
    if not os.path.exists(sizes): pytest.skip("no _build/sizes.json beside the workflow")
    fresh = snap["sizes"]["nodes"]; kept = json.loads(Path(sizes).read_text(encoding="utf-8"))
    made_on = kept.get("platform", "darwin")
    if made_on != sys.platform:
        pytest.skip(f"sizes.json was photographed on {made_on}; this is {sys.platform}, whose fonts measure text differently")
    assert kept["workflow_structure_sha"] == snap["view"]["workflow_structure_sha"], "sizes.json was made from a different graph: rerun the photograph tool"
    off = [k for k, r in fresh.items() if k not in kept["nodes"] or any(abs(a - b) > 0.5 for a, b in zip(r["computed"], kept["nodes"][k]["computed"]))]
    assert off == [], f"{len(off)} nodes compute a different size than sizes.json holds (frontend {snap['view']['frontend']} vs {kept.get('frontend')}): {off[:8]}"


# ---------------------------------------------------------------- config: shape guards
def test_config_sizes_cache_matches_the_workflow(wf, canvas):
    """The size cache carries a hash of the graph's shape (not its geometry); a structural edit without a new
    photograph leaves the layout sizing against the wrong graph."""
    sizes = canvas.files["sizes"]
    if not os.path.exists(sizes): pytest.skip("no _build/sizes.json beside the workflow")
    assert json.loads(Path(sizes).read_text(encoding="utf-8"))["workflow_structure_sha"] == structure_sha(wf), "rerun the photograph tool after a structural edit"


def test_config_no_leftovers_from_packs_that_are_not_installed(wf, canvas):
    """Metadata for a pack the installer does not install is dead weight, and it lies (118 nodes once carried
    cg-use-everywhere's `ue_properties` while the pack was not installed)."""
    if canvas.pkg is None: pytest.skip("no PKG in this suite")
    installed = " ".join(r["url"] for r in canvas.pkg.packs)
    if "cg-use-everywhere" in installed: pytest.skip("cg-use-everywhere is installed; its metadata belongs")
    carriers = [n["id"] for n in _all_nodes(wf) if "ue_properties" in (n.get("properties") or {})]
    assert carriers == [], "ue_properties on %d node(s) but cg-use-everywhere is not installed: %s" % (len(carriers), carriers[:8])
    for k in ("ue_links", "links_added_by_ue"):
        assert k not in (wf.get("extra") or {}), "extra.%s remains but cg-use-everywhere is not installed" % k


def test_config_every_node_carries_a_properties_map(wf, canvas):
    """`properties` is where cnr_id/aux_id live, so a node without one cannot say which pack installs it."""
    bare = [(n["id"], n["type"]) for n in _all_nodes(wf) if "properties" not in n]
    assert bare == [], "nodes with no properties map: %s" % bare


def test_config_the_shipping_zip_is_the_shipped_pair(canvas):
    """The quick start unzips the package's zip and runs what comes out: lock its members to the files beside it."""
    f = canvas.files; zp = f["zip"]
    if not zp or not os.path.exists(zp): pytest.skip("no package zip beside the workflow")
    want = [os.path.basename(p) for p in (f["workflow"], f["script"], f["handbook"], f["suite"], f["pytest_ini"]) if p and os.path.exists(p)]
    want += [m for m in canvas.spec.extra_zip_members if os.path.exists(os.path.join(f["dir"], m))]
    with zipfile.ZipFile(zp) as z:
        assert sorted(z.namelist()) == sorted(want), z.namelist()
        for m in want:
            src = os.path.join(f["dir"], m)
            a, b = hashlib.sha256(z.read(m)).hexdigest(), hashlib.sha256(Path(src).read_bytes()).hexdigest()
            assert a == b, f"{m}: the zip ships a different file than the one beside it ({a[:12]} vs {b[:12]})"


def test_config_versions_agree(wf, canvas):
    """One version string: the script's PKG_VERSION, extra.<version_key> in the JSON, the script header, the handbook
    title and the suite's docstring."""
    key = canvas.spec.version_key; v = wf["extra"][key]
    if canvas.pkg is not None:
        assert canvas.pkg.version == v, f"PKG_VERSION {canvas.pkg.version} ≠ workflow {key} {v}"
        assert canvas.pkg.wf_version_key == key, f"the base must read the version from extra.{key}"
    f = canvas.files
    if f["script"]: assert f"V{v}" in Path(f["script"]).read_text(encoding="utf-8").splitlines()[2], "script header ≠ workflow version"
    if f["handbook"] and os.path.exists(f["handbook"]): assert f"V{v}" in Path(f["handbook"]).read_text(encoding="utf-8").splitlines()[0], "handbook title ≠ workflow version"
    assert f"(V{v})" in Path(f["suite"]).read_text(encoding="utf-8").splitlines()[0], "the suite's docstring announces a different version"


def test_config_the_layout_tool_reproduces_itself(canvas):
    """`_build/layout.py --verify` must pass on the shipped file: a run changes nothing outside geometry, and running
    the tool on its own output moves nothing at all (string hashing is seeded per process; an ordering pass that
    read a set once produced a different canvas every run)."""
    tool = canvas.files["layout"]
    if not os.path.exists(tool): pytest.skip("no _build/layout.py beside the workflow")
    r = subprocess.run([sys.executable, tool, "--verify", "--wf", canvas.files["workflow"]], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_config_every_tier_test_carries_its_gate(canvas):
    """A test that needs a server, a GPU or pack source on disk must SAY so: a gate decorator within four lines above
    it (declared in `tier_gates`) or a gating fixture in its signature (`snap`, `server_answering`)."""
    if not canvas.spec.tier_gates: pytest.skip("no tier gates declared")
    bad = []
    for path in (canvas.files["suite"], __file__):
        text = Path(path).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(text):
            if not line.startswith("def test_"): continue
            for pfx, mark in canvas.spec.tier_gates:
                if not line.startswith("def " + pfx): continue
                gated = any(mark in text[j] for j in range(max(0, i - 4), i)) or re.search(r"\b(snap|server_answering)\b", line)
                if not gated: bad.append("%s is missing %s" % (line[4:line.index("(")], mark))
    assert not bad, "\n".join(bad)


def test_config_handbook_numbers_match_reality(canvas):
    """The handbook quotes figures about the suite. They drift every time the suite grows, so the suite counts itself:
    the tests the module collects (its own and the generic ones) and the tiers pytest.ini declares."""
    hb = canvas.files["handbook"]
    if not hb or not os.path.exists(hb): pytest.skip("no handbook beside the workflow")
    text = Path(hb).read_text(encoding="utf-8")
    n_fn = len([k for k, v in vars(canvas.module).items() if k.startswith("test_") and callable(v)])
    ini = Path(canvas.files["pytest_ini"]).read_text(encoding="utf-8")
    n_tier = len(re.findall(r"^\s+([a-z0-9]+):", re.split(r"^markers\s*=", ini, flags=re.M)[1], re.M))
    assert "**%d test functions**" % n_fn in text, "the handbook says a different number of test functions; the suite collects %d" % n_fn
    assert "**%d tiers**" % n_tier in text, "the handbook says a different number of tiers; pytest.ini has %d" % n_tier


def test_config_every_pack_is_pinned(canvas):
    """Unpinned packs are the largest single risk to "it works first try"; every pack of the package is a full commit sha."""
    if canvas.pkg is None: pytest.skip("no PKG in this suite")
    from basetest import base_packs
    base_dirs = {p["dir"] for p in base_packs()}
    own = [p for p in canvas.pkg.packs if p["dir"] not in base_dirs]
    if canvas.spec.own_packs: assert len(own) == canvas.spec.own_packs, "expected %d packs, found %d" % (canvas.spec.own_packs, len(own))
    for row in own:
        assert re.fullmatch(r"[0-9a-f]{40}", row["sha"]), "pack %s: %r is not a full commit sha" % (row["dir"], row["sha"])
    shas = [r["sha"] for r in canvas.pkg.packs]
    assert len(set(shas)) == len(shas), "two packs share a pinned commit"
