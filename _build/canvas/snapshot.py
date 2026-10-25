# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright"]
# ///
"""Photograph a workflow package's canvas in the REAL frontend, and export every node's computeSize().

    uv run --upgrade "base/comfyui-base/_build/canvas/snapshot.py" --pkg "<package dir>"      # needs: bash testbed.sh --server
    uv run ... --pkg <dir> --viewport 1728x1117@2 --subgraphs --view band2=0.55,170,-1650

Loads the package's workflow into the testbed's ComfyUI page with Playwright, restores the saved view, screenshots
what a person sees at the display the canvas is designed for (CANVAS.display in the package's suite.py), writes
opening-<WxH@dpr>.png / full-*.png / view.json to <pkg>/_build/snapshots and <pkg>/_build/sizes.json (the size floor
layout.py sizes against, keyed to the graph's structure hash). Refuses loudly, never starts anything: the server must
answer and register every node type the workflow uses (except --allow-missing, default CANVAS.cuda_only)."""
import argparse, json, os, re, sys, time, pathlib, urllib.request, datetime
from pathlib import Path

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "py"))
from canvas import structure_sha, CHROME_LEFT, CHROME_TOP, FRONTEND_ONLY_TYPES, load_spec, package_files, text_host             # noqa: E402
from comfy_settings import SEED                                                                                      # noqa: E402


def ensure_browser():
    """3.0.0: Playwright is unpinned (always its newest, `uv run --upgrade`), and each release drives its own Chromium
    build. Installing it is idempotent and takes a second when that build is already present."""
    import subprocess
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

SELF_DRAWN = ("Label (rgthree)", "Bookmark (rgthree)")           # draw their own text; the size floor does not apply
SETTINGS = {                                                        # what the page believes its settings are
    "Comfy.TutorialCompleted": True, "Comfy.RightSidePanel.IsOpen": False, "Comfy.Minimap.Visible": False,
    "Comfy.Graph.CanvasInfo": False, "Comfy.EnableWorkflowViewRestore": True, "Comfy.UseNewMenu": "Top",
    "Comfy.ColorPalette": "dark", "Comfy.Workflow.WorkflowTabsPosition": "Topbar", "Comfy.Sidebar.Location": "left",
    **SEED,                                                         # 3.17.1: what the base seeds on every machine (row 81)
}

JS_LOAD = """async (wf) => {
  const app = window.app;
  // Every tool that shares this loader — snapshot, panels, probe — works on the CANVAS. A
  // workflow declaring extra.linearMode opens in App view instead, and there the graph
  // canvas element keeps its device pixels but reports a ZERO CSS rect: measured on
  // frontend 1.49.6, el.width 3024 with getBoundingClientRect().width 0, so the probe's
  // px-per-unit ratio came out Infinity and getImageData was handed a non-long. Load a
  // copy with the flag cleared. App Mode is a second view of the same graph; node
  // geometry, rgthree panel rows and graphToPrompt are all facts about the canvas, and a
  // package that ships a front door still has one behind it.
  const linear = !!(wf && wf.extra && wf.extra.linearMode);
  if (linear) wf = Object.assign({}, wf, {extra: Object.assign({}, wf.extra, {linearMode: false})});
  await app.loadGraphData(wf, true, true, null, {skipAssetScans: true, silentAssetErrors: true, deferWarnings: true});
  // "Missing" means the TYPE is not registered. `has_errors` is broader on 1.49.6: useNodeErrorFlagSync also
  // raises it from the missing-model and missing-media scans, so a loader whose file is deliberately absent on
  // this machine (a PUT-YOUR-… placeholder, an on-demand weight) wore the same red halo as an uninstalled pack
  // and refused the snapshot. The frontend marks a missing-TYPE placeholder in exactly one
  // other way: it keeps the serialised node on `last_serialization` (LGraph.ts configure/unpack, app.ts
  // loadApiJson -- the only three writers of that field), so that is the signal.
  const isMissing = (n) => !!n.has_errors && !!n.last_serialization;
  window.__baseIsMissingType = isMissing;
  const missing = [...new Set(app.rootGraph.nodes.filter(isMissing).map(n => n.type))];
  return {linear, missing, nodes: app.rootGraph.nodes.length, groups: app.rootGraph.groups.length};
}"""
JS_VIEW = """async (ds) => {
  const app = window.app, c = app.canvas;
  if (ds) { c.ds.offset[0] = ds.offset[0]; c.ds.offset[1] = ds.offset[1]; c.ds.scale = ds.scale; }
  c.setDirty(true, true); c.draw(true, true);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  c.ds.computeVisibleArea(c.viewport);
  const el = c.canvas, rect = el.getBoundingClientRect();
  return {ds: {scale: c.ds.scale, offset: [c.ds.offset[0], c.ds.offset[1]]},
          visible_area: Array.from(c.visible_area), canvas_px: [el.width, el.height],
          canvas_css: [rect.width, rect.height], canvas_at: [rect.left, rect.top],
          low_quality: !!c.low_quality, lod_threshold: c._lowQualityZoomThreshold, dpr: window.devicePixelRatio};
}"""
JS_PROBE = """(id) => {
  const app = window.app, c = app.canvas, n = app.rootGraph.getNodeById(id);
  if (!n) return null;
  const el = c.canvas, ratio = el.width / el.getBoundingClientRect().width, ctx = el.getContext('2d');
  const px = (x, y) => { const d = ctx.getImageData(Math.round(x * ratio), Math.round(y * ratio), 1, 1).data; return [d[0], d[1], d[2]]; };
  const X = u => (u + c.ds.offset[0]) * c.ds.scale, Y = u => (u + c.ds.offset[1]) * c.ds.scale;
  const title = [X(n.pos[0] + n.size[0] * 0.75), Y(n.pos[1] - 15)], body = [X(n.pos[0] + n.size[0] * 0.5), Y(n.pos[1] + n.size[1] * 0.5)];
  return {title_at: title, title_rgb: px(...title), body_at: body, body_rgb: px(...body), color: n.color || null, bgcolor: n.bgcolor || null, title: n.title};
}"""
JS_CHROME = """() => {
  const q = sel => { const e = document.querySelector(sel); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const out = {};
  for (const [k, sel] of Object.entries({left_bar: '.side-tool-bar-container, .comfyui-body-left', top_bar: '.comfyui-body-top', bottom_bar: '.comfyui-body-bottom',
        actionbar: '.actionbar', canvas_container: '.graph-canvas-container', topbar: '.comfyui-menu, .top-menubar'})) { try { out[k] = q(sel); } catch (e) { out[k] = null; } }
  return out;
}"""
JS_FIT = """async () => {
  const app = window.app, c = app.canvas, g = app.rootGraph;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x, y, w, h) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h); };
  for (const n of g.nodes) { const b = n.boundingRect; add(b[0], b[1], b[2], b[3]); }
  for (const gr of g.groups) { const b = gr._bounding; add(b[0], b[1], b[2], b[3]); }
  c.ds.fitToBounds([x0, y0, x1 - x0, y1 - y0], {zoom: 0.96});
  c.setDirty(true, true); c.draw(true, true);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return {bounds: [x0, y0, x1 - x0, y1 - y0], ds: {scale: c.ds.scale, offset: [c.ds.offset[0], c.ds.offset[1]]}};
}"""
JS_SIZES = """(graphKey) => {
  const app = window.app, c = app.canvas, g = c.graph;
  const str = v => { try { return v == null ? null : String(typeof v === 'object' ? JSON.stringify(v) : v).slice(0, 80); } catch (e) { return '?'; } };
  return g.nodes.map(n => ({
    key: graphKey === 'root' ? String(n.id) : graphKey + ':' + n.id,
    id: n.id, graph: graphKey, type: n.type, title: n.title, mode: n.mode, missing: (window.__baseIsMissingType ? window.__baseIsMissingType(n) : !!n.has_errors),
    collapsed: !!(n.flags && n.flags.collapsed), subgraph: (n.isSubgraphNode && n.isSubgraphNode()) ? n.subgraph.id : null,
    pos: [n.pos[0], n.pos[1]], size: [n.size[0], n.size[1]], computed: Array.from(n.computeSize()),
    inputs: (n.inputs || []).map(i => ({name: i.name, label: i.label ?? null, type: i.type, widget: !!i.widget, linked: i.link != null})),
    outputs: (n.outputs || []).map(o => ({name: o.name, type: o.type, links: (o.links || []).length})),
    widgets: (n.widgets || []).map(w => ({name: w.name, type: w.type, y: w.y ?? null,
      height: w.computedHeight ?? w.height ?? 20,
      min_h: (w.computeLayoutSize ? (w.computeLayoutSize(n) || {}).minHeight : (w.computeSize ? w.computeSize(n.size[0])[1] : null)) ?? null,
      dom: !!w.element, dom_scroll_h: w.element ? w.element.scrollHeight : null,
      hidden: n.isWidgetVisible ? !n.isWidgetVisible(w) : false, value: str(w.value)}))
  }));
}"""
JS_OPEN = """async (id) => {
  const app = window.app, c = app.canvas, n = app.rootGraph.getNodeById(id);
  if (!n || !n.isSubgraphNode || !n.isSubgraphNode()) return false;
  c.openSubgraph(n.subgraph, n);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return c.graph === n.subgraph;
}"""
JS_ROOT = """async () => { const app = window.app, c = app.canvas; c.setGraph(app.rootGraph);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return c.graph === app.rootGraph; }"""

def need_server(server, wf=None, allowed=()):
    """The server must answer, and register every node type the workflow uses (the testbed, not a bare ComfyUI)."""
    try:
        with urllib.request.urlopen(f"http://{server}/system_stats", timeout=3) as r: stats = json.load(r)
        with urllib.request.urlopen(f"http://{server}/object_info", timeout=120) as r: known = set(json.load(r))
    except Exception as e:
        print(f"testbed server is not answering at {server} ({e}) -- start it with:  bash testbed.sh --server", file=sys.stderr)
        sys.exit(2)
    if wf is not None:
        defs = {d["id"] for d in wf.get("definitions", {}).get("subgraphs", []) or []}
        used = {n["type"] for n in wf["nodes"]} | {n["type"] for d in wf.get("definitions", {}).get("subgraphs", []) or [] for n in d.get("nodes", []) or []}
        missing = sorted(t for t in used if t not in known and t not in FRONTEND_ONLY_TYPES and t not in defs and t not in allowed)
        if missing: print(f"the server at {server} does not register {missing}: not this package's testbed", file=sys.stderr); sys.exit(2)
    return stats

def parse_viewport(s):
    wh, _, dpr = s.partition("@"); w, _, h = wh.partition("x")
    return int(w), int(h), int(dpr or 1)

# Console errors the frontend raises on its own, each with the reason it is not ours. A line matching none is UNEXPLAINED,
# and the snapshot tier fails on it — a new frontend cannot slip a new error past a bare count (2026-09-06: "console errors: 15").
KNOWN_CONSOLE = [
    (re.compile(r"Failed to load resource: the server responded with a status of (404|400)"),
     "the frontend asks /api/userdata for per-user files (settings, templates) that a fresh user dir does not have yet"),
    (re.compile(r"violates the following Content Security Policy directive"),
     "the headless browser's CSP blocks the frontend's external template media (images, video) — not loaded, not needed"),
    (re.compile(r"ComfyApp graph accessed before initialization"),
     "the frontend's own init-order notice while extensions register, before the graph exists"),
    (re.compile(r"\[vite:preloadError\].*(already registered|Failed to fetch dynamically imported)"),
     "an extension registered twice, or a lazy chunk the frontend never uses here — the frontend's packaging, not the graph"),
    (re.compile(r"Cannot read properties of undefined \(reading 'all_connected_inputs'\)\s+at LinkRenderController\.highlight_subgraph_node_connections"),
     "cg-use-everywhere draws a subgraph's highlights before its own link list exists (use_everywhere_ui.js:194, this.ue_list undefined) — "
     "a race in that extension while the canvas is inside a subgraph; seen 1 run in 3 on the same graph (2026-09-08)"),
    (re.compile(r"Cannot read properties of undefined \(reading 'extra'\)\s+at app\.loadGraphData \(\S*/ComfyUI-Manager/components-manager\.js"),
     "comfyui-obvpm 0.2.16's loadGraphData wrapper (web/obvpm_migrate.js) forwards `orig.call(this, graphData, ...rest)`, "
     "so the frontend's no-argument startup call (resolveStartupOutcome: `loadGraphData()` once Comfy.TutorialCompleted) "
     "reaches ComfyUI-Manager's wrapper (js/components-manager.js) with ONE argument, undefined; Manager guards only "
     "`arguments.length == 0` and reads graphData.extra. Logged twice (initializeWorkflow's catch, then the same call retried inside it). Reproduced on "
     "frontend 1.55.17, 2026-10-16: the startup default graph does not load (0 nodes, 10 with either wrapper fixed) and "
     "the startup's later steps are skipped; this tool loads its workflow explicitly afterwards, so nothing it measures "
     "changes. Upstream: Comfy-Org/ComfyUI-Manager#1742 (the null variant); drop this row when either wrapper is fixed"),
]


def classify_console(lines):
    """[error] lines → (known: [(line, reason)], unexplained: [line]); other levels are ignored."""
    known, unexplained = [], []
    for line in lines:
        if not line.startswith("[error]"):
            continue
        for rx, reason in KNOWN_CONSOLE:
            if rx.search(line):
                known.append((line, reason)); break
        else:
            unexplained.append(line)
    return known, unexplained


def console_summary(lines):
    known, unexplained = classify_console(lines)
    reasons = {}
    for _line, reason in known:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {"errors": len(known) + len(unexplained), "known": len(known), "unexplained": unexplained, "reasons": reasons}


def slug(s): return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")[:40]

# 3.17.1 (error log row 81): ComfyUI_frontend v1.55.17's onboarding tour, by its own test ids. CoachmarkLanding.vue:
# coach-landing-overlay (the full-screen scrim) around coach-landing (the "Welcome to Apps" card, "Start tutorial" / "Skip");
# TourSpotlight.vue: coach-spotlight and coach-card (role=dialog, "Skip" / "Next" / "Done") for the later steps. Its own
# browser_tests/tests/tour.spec.ts: the landing's "Skip" ends it and marks it seen, and so does Escape.
COACH_IDS = ("coach-landing-overlay", "coach-landing", "coach-spotlight", "coach-card")
JS_COACH_SHOWN = """(ids) => { for (const id of ids) { const el = document.querySelector(`[data-testid="${id}"]`);
  if (el && el.getClientRects().length) return id; } return null; }"""
JS_COACH_SKIP = """() => { for (const id of ['coach-landing', 'coach-card']) {
  const box = document.querySelector(`[data-testid="${id}"]`); if (!box) continue;
  const b = [...box.querySelectorAll('button')].find(x => x.textContent.trim() === 'Skip');
  if (b) { b.click(); return id; } } return null; }"""


def coach_tour(page, wait_ms=1500):
    """None when no onboarding tour is on screen; else {"shown": <test id>, "dismissed": bool}, after clicking its exact
    "Skip" (the landing's, else the step card's) or, failing that, pressing Escape. The packages' App-view drivers call
    it instead of their own dismiss loops, and REPORT a result: on a server the base installed, or on the testbed, the
    base seeds "Comfy.OnboardingCoachmarks.Seen", so a tour there means the seed missed (a renamed key, a new tour).
    Only page.evaluate, page.wait_for_timeout and page.keyboard.press are used, so a fake page tests it."""
    shown = None; waited = 0
    while waited <= wait_ms:
        shown = page.evaluate(JS_COACH_SHOWN, list(COACH_IDS))
        if shown: break
        page.wait_for_timeout(250); waited += 250
    if not shown:
        return None
    if not page.evaluate(JS_COACH_SKIP):
        page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    return {"shown": shown, "dismissed": not page.evaluate(JS_COACH_SHOWN, list(COACH_IDS))}


def route_settings(page):
    def handler(route):
        req = route.request
        if req.method == "GET":
            tail = req.url.rstrip("/").split("/api/settings", 1)[1].strip("/")
            body = SETTINGS if not tail else SETTINGS.get(tail)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/api/settings*", handler)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()), help="the package folder (default: the cwd)")
    ap.add_argument("--wf", default=None, help="the workflow (default: the package's '* Workflow.json', or $BASE_WF)")
    ap.add_argument("--server", default=os.environ.get("BASE_SERVER") or f"127.0.0.1:{os.environ.get('TESTBED_PORT') or 8199}")
    ap.add_argument("--out", default=None, help="default <pkg>/_build/snapshots")
    ap.add_argument("--sizes-out", default=None, help="default <pkg>/_build/sizes.json")
    ap.add_argument("--viewport", action="append", help="WxH@dpr, repeatable; default CANVAS.display")
    ap.add_argument("--subgraphs", action="store_true", help="one fitted PNG per subgraph definition")
    ap.add_argument("--allow-missing", default=None, help="node types that may fail to register (default CANVAS.cuda_only)")
    ap.add_argument("--no-sizes", action="store_true")
    ap.add_argument("--keep-browser", action="store_true", help="headed, and leave it open for a look")
    ap.add_argument("--view", action="append", default=[], metavar="NAME=SCALE,X,Y",
                    help="an extra screenshot at ds {scale, offset [X, Y]} -> view-NAME-<WxH@dpr>.png (e.g. band2=0.55,170,-1400)")
    a = ap.parse_args()
    spec, files = load_spec(a.pkg)
    a.wf = a.wf or os.environ.get("BASE_WF") or files["workflow"]
    a.out = a.out or files["snapshots"]; a.sizes_out = a.sizes_out or files["sizes"]
    allowed_types = tuple(spec.cuda_only) if a.allow_missing is None else tuple(x for x in a.allow_missing.split(",") if x)
    wf = json.loads(Path(a.wf).read_text(encoding="utf-8"))
    viewports = [parse_viewport(v) for v in (a.viewport or [spec.display])]
    allowed = set(allowed_types)
    stats = need_server(a.server, wf, allowed)
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    import hashlib
    view = {"workflow": a.wf, "workflow_sha": hashlib.sha256(Path(a.wf).read_bytes()).hexdigest(),
            "workflow_structure_sha": structure_sha(wf), "server": a.server,
            "comfyui": stats.get("system", {}).get("comfyui_version"), "frontend": None,
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "ds_requested": wf.get("extra", {}).get("ds"), "viewports": [], "subgraphs": [], "files": []}
    console, errors = [], []
    sizes = None
    from playwright.sync_api import sync_playwright     # lazy: the module's pure helpers (classify_console) import without a browser stack
    ensure_browser()                                   # 3.0.0: the browser for this (newest) Playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.keep_browser)
        for (W, H, dpr) in viewports:
            ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=dpr, color_scheme="dark")
            page = ctx.new_page()
            page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: errors.append(str(e)))
            route_settings(page)
            page.goto(f"http://{a.server}/", wait_until="load")
            page.wait_for_function("() => window.app && window.app.canvas && window.app.rootGraph && document.fonts.status === 'loaded'", timeout=120_000)
            if view["frontend"] is None:
                view["frontend"] = page.evaluate("() => (window.__COMFYUI_FRONTEND_VERSION__ || (document.querySelector('meta[name=version]')||{}).content || null)")
            info = page.evaluate(JS_LOAD, wf)
            bad = sorted(set(info["missing"]) - allowed)
            if bad:
                print(f"missing node types (a red-halo canvas is a bug report, not a picture): {bad}", file=sys.stderr); sys.exit(3)
            v = page.evaluate(JS_VIEW, view["ds_requested"])
            tag = f"{W}x{H}@{dpr}"
            rec = {"viewport": [W, H], "dpr": dpr, "nodes": info["nodes"], "groups": info["groups"], "missing": info["missing"],
                   "linear_mode": info.get("linear", False),   # the file opens in App view; this picture is the canvas behind it

                   "ds_applied": v["ds"], "visible_area": v["visible_area"], "canvas_px": v["canvas_px"], "canvas_css": v["canvas_css"],
                   "canvas_at": v["canvas_at"], "low_quality": v["low_quality"], "lod_threshold": v["lod_threshold"]}
            req = view["ds_requested"]
            if req and (abs(v["ds"]["scale"] - req["scale"]) > 1e-9 or v["ds"]["offset"] != list(req["offset"])):
                print(f"frontend replaced the saved view: asked {req}, got {v['ds']} -- fix extra.ds", file=sys.stderr); sys.exit(4)
            f = out / f"opening-{tag}.png"; page.screenshot(path=str(f)); view["files"].append(f.name); rec["opening"] = f.name
            rec["probe"] = page.evaluate(JS_PROBE, spec.start_card) if spec.start_card else None
            rec["chrome"] = page.evaluate(JS_CHROME)
            for spec in a.view:                                   # extra views of the same page, then back to the saved one
                name, rest = spec.split("=", 1); sc, ox, oy = rest.split(",")
                page.evaluate(JS_VIEW, {"scale": float(sc), "offset": [float(ox), float(oy)]})
                f = out / f"view-{name}-{tag}.png"; page.screenshot(path=str(f)); view["files"].append(f.name)
            if a.view: page.evaluate(JS_VIEW, view["ds_requested"])
            # The frontend's visible_area is in device pixels / scale; what a person sees is CSS px / scale.
            sc, off = v["ds"]["scale"], v["ds"]["offset"]
            rec["visible_units"] = [-off[0], -off[1], v["canvas_css"][0] / sc, v["canvas_css"][1] / sc]
            # Overlays measured on 1.49.6 (left icon strip, workflow tab bar) sit ON the canvas element, not beside it.
            L, T = CHROME_LEFT, CHROME_TOP
            rec["usable_units"] = [L / sc - off[0], T / sc - off[1], (v["canvas_css"][0] - L) / sc, (v["canvas_css"][1] - T) / sc]
            fit = page.evaluate(JS_FIT); rec["full"] = fit
            f = out / f"full-{tag}.png"; page.screenshot(path=str(f)); view["files"].append(f.name); rec["full"]["png"] = f.name
            view["viewports"].append(rec)
            if sizes is None and not a.no_sizes:
                page.evaluate(JS_VIEW, view["ds_requested"])
                sizes = {r["key"]: r for r in page.evaluate(JS_SIZES, "root")}
                stored = {str(n["id"]): n["size"] for n in wf["nodes"]}
                for sg in wf.get("definitions", {}).get("subgraphs", []) or []:
                    for n in sg.get("nodes", []) or []: stored[f"{sg['id']}:{n['id']}"] = n["size"]
                def _st(k):
                    v = stored.get(k); return [float(v.get("0")), float(v.get("1"))] if isinstance(v, dict) else ([float(v[0]), float(v[1])] if v else None)
                defs = wf.get("definitions", {}).get("subgraphs", []) or []
                inst = {n["type"]: n["id"] for n in wf["nodes"] if any(n["type"] == sg["id"] for sg in defs)}
                for sg in defs:
                    iid = inst.get(sg["id"])
                    if iid is None: continue
                    if not page.evaluate(JS_OPEN, iid): errors.append(f"could not open subgraph {sg['name']} via {iid}"); continue
                    for r in page.evaluate(JS_SIZES, sg["id"]): sizes[r["key"]] = r
                    if a.subgraphs:
                        page.evaluate(JS_FIT)
                        f = out / f"subgraph-{sg['id'][:8]}-{slug(sg['name'])}.png"; page.screenshot(path=str(f)); view["files"].append(f.name)
                        view["subgraphs"].append({"id": sg["id"], "name": sg["name"], "instance": iid, "png": f.name, "nodes": len(sg.get("nodes", []) or [])})
                    page.evaluate(JS_ROOT)
            if sizes is not None:
                for k, r in sizes.items(): r["stored"] = _st(k)      # the JSON's size; `size` is what the page grew it to on load
            ctx.close()
        if a.keep_browser: input("browser open -- press return to close ")
        b.close()
    if sizes is not None:
        # SELF_DRAWN: labels and bookmarks draw their own text (module constant)
        short = [k for k, r in sizes.items() if not r["missing"] and not r["collapsed"] and r["type"] not in SELF_DRAWN and r.get("stored")
                 and (r["stored"][0] < r["computed"][0] - 0.5 or r["stored"][1] < r["computed"][1] - 0.5)]
        Path(a.sizes_out).write_text(json.dumps(
                  {"frontend": view["frontend"], "comfyui": view["comfyui"], "generated": view["generated"],
                   "platform": sys.platform,       # 3.8.2: the text metrics differ by OS; the cache is compared only where it was made
                   # 3.27.2: and by machine and fonts (py/canvas.py text_host): a Blacksmith ARM runner measured 17 nodes otherwise
                   "machine": text_host()["machine"], "fonts": text_host()["fonts"],
                   "workflow_structure_sha": view["workflow_structure_sha"],
                   "constants": {"NODE_TITLE_HEIGHT": 30, "NODE_SLOT_HEIGHT": 20, "NODE_WIDGET_HEIGHT": 20},
                   "nodes": sizes}, indent=1, ensure_ascii=False))
        view["sizes"] = {"file": a.sizes_out, "nodes": len(sizes), "shorter_than_computed": len(short), "short_keys": short[:50]}
    cs = console_summary(console)
    view["console_errors"] = cs["errors"]; view["console_known"] = cs["reasons"]; view["console_unexplained"] = cs["unexplained"]; view["page_errors"] = errors
    (out / "console.log").write_text("\n".join(console + [f"PAGEERROR {e}" for e in errors]))
    (out / "view.json").write_text(json.dumps(view, indent=1, ensure_ascii=False), encoding="utf-8")
    for rec in view["viewports"]:
        print(f"{rec['viewport'][0]}x{rec['viewport'][1]}@{rec['dpr']}: {rec['opening']}  ds {rec['ds_applied']['scale']:.3f} {rec['ds_applied']['offset']}  "
              f"canvas {rec['canvas_css']} css  visible {[round(x) for x in rec['visible_units']]}  usable {[round(x) for x in rec['usable_units']]}  low_quality {rec['low_quality']}  "
              f"probe title {rec['probe'] and rec['probe']['title_rgb']} for {rec['probe'] and rec['probe']['color']}, body {rec['probe'] and rec['probe']['body_rgb']} for {rec['probe'] and rec['probe']['bgcolor']}")
        print(f"   chrome: {rec['chrome']}")
    if sizes is not None: print(f"sizes: {len(sizes)} nodes, {len(short)} shorter than the frontend draws them -> {a.sizes_out}")
    print(f"missing: {view['viewports'][0]['missing']}   page errors: {len(errors)}   console errors: {cs['errors']} "
          f"({cs['known']} known: {'; '.join('%d× %s' % (n, r.split(' — ')[0].split(' (')[0][:48]) for r, n in cs['reasons'].items()) or 'none'}; "
          f"{len(cs['unexplained'])} unexplained)   -> {out / 'view.json'}")
    for line in cs["unexplained"]:
        print("   UNEXPLAINED " + line[:200])
    if errors: sys.exit(3)

if __name__ == "__main__": main()
