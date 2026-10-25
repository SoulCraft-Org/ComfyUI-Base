# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright"]
# ///
"""What does the REAL frontend queue? Dump app.graphToPrompt() for declared radio states.

    uv run --upgrade "base/comfyui-base/_build/canvas/probe.py" --pkg "<package dir>" [--states <json>] [--out <dir>]

Loads the package's workflow into the testbed's ComfyUI page and writes prompt-<state>.json for each state in
<pkg>/_build/snapshots/probe/states.json, so a suite's emulation of bypass/mute can be diffed against the
frontend's own resolver (the kit's port of ExecutableNodeDTO was proved 0-divergent this way).

A state is either the original flat mode map — {"name": {"<node id>": mode, ...}} — or, for a package whose
selection is widget-driven rather than a bypass panel, {"name": {"modes": {...}, "widgets": {"<node id>":
{"<widget name>": value}}}}. Both may appear in one file; the shipped file is state "default". Widgets are
set BEFORE graphToPrompt, so a lazy switch resolves the branch that state actually chose.

Read-only: never starts a server, never writes into the testbed."""
import argparse, json, os, sys, pathlib
from playwright.sync_api import sync_playwright
from pathlib import Path

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1] / "py"))
from snapshot import need_server, route_settings, ensure_browser          # noqa: E402
from canvas import load_spec                                # noqa: E402

from snapshot import JS_LOAD                             # noqa: E402 — one loader, and it clears extra.linearMode
JS_PROMPT = """async (state) => {
  const app = window.app, g = app.rootGraph;
  for (const [id, m] of Object.entries(state.modes || {})) { const n = g.getNodeById(Number(id)); if (n) n.mode = m; }
  // Widget-driven selection: set the value the way the frontend does, so a lazy switch and any
  // onWidgetChanged handler see it before graphToPrompt walks the graph.
  const missing = [];
  for (const [id, ws] of Object.entries(state.widgets || {})) {
    const n = g.getNodeById(Number(id));
    if (!n) { missing.push(id); continue; }
    for (const [name, v] of Object.entries(ws)) {
      const w = (n.widgets || []).find(w => w.name === name);
      if (!w) { missing.push(id + ':' + name); continue; }
      w.value = v; if (w.callback) w.callback(v, app.canvas, n);
    }
  }
  const p = await app.graphToPrompt();
  return {output: p.output, missing};
}"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()))
    ap.add_argument("--wf", default=None); ap.add_argument("--server", default=os.environ.get("BASE_SERVER") or f"127.0.0.1:{os.environ.get('TESTBED_PORT') or 8199}")
    ap.add_argument("--states", default=None, help="default <pkg>/_build/snapshots/probe/states.json"); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    spec, files = load_spec(a.pkg)
    wf_path = a.wf or os.environ.get("BASE_WF") or files["workflow"]
    out = pathlib.Path(a.out or os.path.join(files["snapshots"], "probe")); out.mkdir(parents=True, exist_ok=True)
    states_path = pathlib.Path(a.states or out / "states.json")
    states = json.loads(Path(states_path).read_text(encoding="utf-8")) if states_path.exists() else {"default": {}}
    wf = json.loads(Path(wf_path).read_text(encoding="utf-8"))
    need_server(a.server, wf, set(spec.cuda_only))
    ensure_browser()                                   # 3.0.0: the browser for this (newest) Playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); page = b.new_page(); route_settings(page)
        page.goto(f"http://{a.server}/", wait_until="load")
        page.wait_for_function("() => window.app && window.app.canvas && window.app.rootGraph", timeout=120_000)
        info = page.evaluate(JS_LOAD, wf)
        bad = sorted(set(info["missing"]) - set(spec.cuda_only))
        if bad: print(f"missing node types: {bad}", file=sys.stderr); sys.exit(3)
        for name, st in states.items():
            page.evaluate(JS_LOAD, wf)                                    # every state starts from the shipped file
            if not any(k in ("modes", "widgets") for k in st):            # the original flat form
                st = {"modes": st}
            r = page.evaluate(JS_PROMPT, {"modes": {str(k): v for k, v in (st.get("modes") or {}).items()},
                                          "widgets": {str(k): v for k, v in (st.get("widgets") or {}).items()}})
            if r["missing"]:
                print(f"state {name!r} names nodes or widgets the graph does not have: {r['missing']}", file=sys.stderr)
                sys.exit(5)
            prompt = r["output"]
            (out / f"prompt-{name}.json").write_text(json.dumps(prompt, indent=1, sort_keys=True), encoding="utf-8")
            print(f"{name}: {len(prompt)} nodes -> {out / f'prompt-{name}.json'}")
        b.close()

if __name__ == "__main__":
    main()
