#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright"]
# ///
"""Click every row of every rgthree panel in the REAL frontend and report what lights.

    uv run --upgrade "base/comfyui-base/_build/canvas/panels.py" --pkg "<package dir>"      # needs: bash testbed.sh --server

The suite emulates rgthree (membership by node centre, "always one" = others off then the chosen
on, muters recurse into subgraph nodes). This runs the real thing: loads the workflow into the
testbed's page, scrolls each panel into view at scale 1, clicks each row with the mouse, and reads
every node's mode -- root and interior -- afterwards. It checks, per panel:

  group panels: the page matches rgthree's own rule (fast_groups_muter.js): a row is lit when ANY node in
      its frame is active; a click turns a lit row off and a dark row on; turning on under "max one" /
      "always one" turns every other row off first. Rows that light together because their frames share
      nodes (🎚️2/🎚️3, 🔗2/🔗3, 🎧2/🎧3) are reported as HAZARDs, not failures -- they are the design.
  Fast Muter (node-level): a row mutes that one node and nothing else; clicking again unmutes it.

Exit 0 when the page matches the model everywhere. A package's pod acceptance list is this, plus the queue."""
import argparse, json, os, pathlib, sys
from playwright.sync_api import sync_playwright
from pathlib import Path

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "py"))
from snapshot import JS_LOAD, need_server, route_settings, ensure_browser      # noqa: E402
from canvas import membership, load_spec                                       # noqa: E402

PANEL_TYPES = ("Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)", "Fast Muter (rgthree)")

JS_FOCUS = """async (id) => {
  const app = window.app, c = app.canvas, n = app.rootGraph.getNodeById(id);
  c.ds.scale = 1; c.ds.offset[0] = -n.pos[0] + 300; c.ds.offset[1] = -n.pos[1] + 200;
  const tick = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  c.setDirty(true, true); c.draw(true, true); await tick();
  if (n.refreshWidgets) n.refreshWidgets();
  c.setDirty(true, true); c.draw(true, true); await tick();
  // rgthree's group rows are RgthreeToggleAndNavWidget: name is the constant, the group title is the label
  return (n.widgets || []).map((w, i) => ({i, name: String((w.name && w.name !== 'RGTHREE_TOGGLE_AND_NAV') ? w.name : (w.label ?? w.group?.title ?? w.name ?? '')), value: w.value, y: w.last_y ?? w.y ?? null, type: w.type}));
}"""
JS_ROW_XY = """async ([id, i]) => {
  const app = window.app, c = app.canvas, n = app.rootGraph.getNodeById(id), w = n.widgets[i];
  const wy = w.last_y ?? w.y ?? 0;
  // scroll so THIS row sits 300px below the top: an 860px muter has rows below the viewport otherwise
  c.ds.offset[1] = -(n.pos[1] + wy) + 300; c.setDirty(true, true); c.draw(true, true);
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const wy2 = w.last_y ?? w.y ?? wy;
  const off = [n.pos[0] + n.size[0] * 0.5, n.pos[1] + wy2 + 10];
  const p = [(off[0] + c.ds.offset[0]) * c.ds.scale, (off[1] + c.ds.offset[1]) * c.ds.scale];
  const r = c.canvas.getBoundingClientRect();
  return [r.left + p[0], r.top + p[1]];
}"""
JS_MODES = """() => {
  const out = {};
  for (const n of window.app.rootGraph.nodes) { out[n.id] = n.mode; if (n.subgraph) for (const m of n.subgraph.nodes) out[n.id + ':' + m.id] = m.mode; }
  return out;
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()), help="the package folder (default: the cwd)")
    ap.add_argument("--wf", default=None, help="the workflow (default: the package's '* Workflow.json', or $BASE_WF)")
    ap.add_argument("--server", default=os.environ.get("BASE_SERVER") or f"127.0.0.1:{os.environ.get('TESTBED_PORT') or 8199}")
    ap.add_argument("--out", default=None, help="default <pkg>/_build/snapshots/panels.json")
    a = ap.parse_args()
    spec, files = load_spec(a.pkg)
    a.wf = a.wf or os.environ.get("BASE_WF") or files["workflow"]; a.out = a.out or os.path.join(files["snapshots"], "panels.json")
    wf = json.loads(Path(a.wf).read_text(encoding="utf-8"))
    need_server(a.server, wf, set(spec.cuda_only))
    by = {n["id"]: n for n in wf["nodes"]}
    mem = {t: set(ids) for t, ids in membership(wf).items()}
    panels = [n for n in wf["nodes"] if n["type"] in PANEL_TYPES]
    links_in = {}
    for l in wf["links"]:
        links_in.setdefault(l[3], []).append(l[1])
    report, failures = [], []

    def off_mode(p): return 2 if "Muter" in p["type"] else 4

    ensure_browser()                                   # 3.0.0: the browser for this (newest) Playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1512, "height": 982}, device_scale_factor=1, color_scheme="dark")
        page = ctx.new_page()
        errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        route_settings(page)
        page.goto(f"http://{a.server}/", wait_until="load")
        page.wait_for_function("() => window.app && window.app.canvas && window.app.rootGraph && document.fonts.status === 'loaded'", timeout=120_000)
        info = page.evaluate(JS_LOAD, wf)
        bad = sorted(set(info["missing"]) - set(spec.cuda_only))
        if bad: print(f"missing node types: {bad}", file=sys.stderr); sys.exit(3)
        base = page.evaluate(JS_MODES)

        def click_row(pid, i):
            x, y = page.evaluate(JS_ROW_XY, [pid, i]); page.mouse.click(x, y); page.wait_for_timeout(150)
            return page.evaluate(JS_MODES)

        for pn in sorted(panels, key=lambda n: n["id"]):
            pid = pn["id"]; rows = page.evaluate(JS_FOCUS, pid)
            rec = {"id": pid, "title": pn.get("title"), "type": pn["type"], "rows": [r["name"] for r in rows], "checks": [], "hazards": []}
            restriction = (pn.get("properties") or {}).get("toggleRestriction", "default")
            if pn["type"] == "Fast Muter (rgthree)":
                linked = [src for src in links_in.get(pid, [])]
                for r in rows:
                    hits = [i for i in linked if by[i].get("title") and by[i]["title"] in r["name"]]
                    target = max(hits, key=lambda i: len(by[i]["title"])) if hits else None      # 'AUDIO 1' is inside 'VID AUDIO 1': longest title wins
                    if target is None: rec["checks"].append(f"row '{r['name']}': no linked node matches"); failures.append((pid, r["name"], "unmatched")); continue
                    before = page.evaluate(JS_MODES); after = click_row(pid, r["i"])
                    changed = {k for k in after if after[k] != before.get(k)}
                    ok = changed == {str(target)} and after[str(target)] == 2
                    again = click_row(pid, r["i"]); ok2 = again == before
                    rec["checks"].append(f"{'ok' if ok and ok2 else 'FAIL'} row '{r['name']}' -> {target} mode {after.get(str(target))}, changed {sorted(changed)}, restored {ok2}")
                    if not (ok and ok2): failures.append((pid, r["name"], sorted(changed)))
                report.append(rec); continue
            # Group panels. Model rgthree exactly (fast_groups_muter.js doModeChange): a row is LIT when any node
            # in its frame is active; a click sets newValue = !lit; turning ON under "max one"/"always one" turns
            # every other row OFF first; turning OFF always succeeds ("always one" compares an object and never
            # holds); the frame's nodes then take modeOn/modeOff. Compare the page against that model, and report
            # rows that light together (frames sharing nodes) as hazards rather than failures.
            def group_of(name):
                cands = [t for t in mem if t == name or t in name or name in t]
                return max(cands, key=len) if cands else None
            row_groups = {r["i"]: group_of(r["name"]) for r in rows}
            if None in row_groups.values():
                missing = [r["name"] for r in rows if row_groups[r["i"]] is None]
                rec["checks"].append(f"rows without a group: {missing}"); failures.append((pid, "rows", missing)); report.append(rec); continue
            off = off_mode(pn)
            model = {int(k): v for k, v in page.evaluate(JS_MODES).items() if ":" not in k}
            def lit(g): return any(model.get(i) == 0 for i in mem[g])
            def set_group(g, on):
                for i in mem[g]: model[i] = 0 if on else off
            for r in rows:
                g = row_groups[r["i"]]
                was_lit = lit(g); new = not was_lit
                if new and " one" in restriction:
                    for q in rows: set_group(row_groups[q["i"]], False)
                set_group(g, new)
                actual = {int(k): v for k, v in click_row(pid, r["i"]).items() if ":" not in k}
                diff = sorted(i for i in model if actual.get(i) != model[i])
                lit_now = [row_groups[q["i"]] for q in rows if lit(row_groups[q["i"]])]
                ok = not diff
                rec["checks"].append(f"{'ok' if ok else 'FAIL'} row '{r['name']}' ({'lit' if was_lit else 'dark'} -> {'on' if new else 'off'}): lit now {[t[:14] for t in lit_now]}" + ("" if ok else f" model≠page at {diff[:8]}"))
                if not ok: failures.append((pid, r["name"], diff[:8]))
                if new and " one" in restriction and len(lit_now) > 1:
                    rec["hazards"].append(f"choosing '{g}' also lights {[t for t in lit_now if t != g]} (shared nodes: {sorted(set().union(*(mem[t] for t in lit_now if t != g)) & mem[g])})")
                if not new and " one" in restriction and not lit_now:
                    rec["hazards"].append(f"clicking the chosen '{g}' switched it off: no row selected")
            # leave the panel as we found it: click the row that was lit at the start, if it is dark now
            report.append(rec)
        b.close()
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"workflow": a.wf, "panels": report, "failures": failures, "page_errors": errors}, open(a.out, "w"), indent=1, ensure_ascii=False)
    for rec in report:
        print(f"{rec['id']} {rec['title']}  [{rec['type'].split(' (')[0]}]  {len(rec['rows'])} rows")
        for c in rec["checks"]: print("   ", c)
        for h in rec.get("hazards", []): print("    HAZARD", h)
    print(f"\n{len(failures)} failures, {len(errors)} page errors -> {a.out}")
    sys.exit(1 if failures or errors else 0)


if __name__ == "__main__":
    main()
