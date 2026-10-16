"""comfyui tier: needs the shared testbed and, for the live checks, a server of this checkout's own. Since 2.6.0 the
tree is <base>/testbed, provisioned by the testbed.sh that ships here; a brand repository has base/testbed beside
base/comfyui-base instead. Both are looked for, in that order, the same way lib/95-summary.sh does it - the runner
normally hands BASE_NODE_SRC down and this fallback is for a bare `pytest suite/`. Everything skips cleanly when
neither is present."""
import json, os, pathlib, re, subprocess, urllib.request, pytest
from pathlib import Path
pytestmark = pytest.mark.comfyui
BASE = pathlib.Path(__file__).resolve().parents[1]
def _find_testbed():
    for root in (BASE, BASE.parent):                      # in the base since 2.6.0, else beside it (a brand tree)
        if (root / "testbed" / "main.py").exists():
            return str(root / "testbed")
    return ""


def _owned_server():
    """3.16.0: a bare `pytest suite/` talks only to the server this checkout's testbed started (testbed.sh --owned), never
    to whatever answers on 8199: on a shared Mac that was another checkout's (error log row 84). 127.0.0.1:0 is none."""
    for root in (BASE, BASE.parent):
        if (root / "testbed.sh").exists():
            r = subprocess.run(["bash", str(root / "testbed.sh"), "--owned"], capture_output=True, text=True, timeout=60)
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "127.0.0.1:0"
    return "127.0.0.1:0"


NODE_SRC = os.environ.get("BASE_NODE_SRC") or _find_testbed()
SERVER = os.environ.get("BASE_SERVER") or _owned_server()


def _server_up():
    try:
        urllib.request.urlopen("http://%s/system_stats" % SERVER, timeout=2); return True
    except Exception:
        return False


def test_comfyui_loader_map_knows_every_core_loader_class_in_the_tree():
    if not NODE_SRC: pytest.skip("no testbed in or beside the base (bash testbed.sh --server)")
    if not (pathlib.Path(NODE_SRC) / "nodes.py").exists(): pytest.skip("%s is not a full ComfyUI tree (a fake pod's): no nodes.py to read" % NODE_SRC)   # 2.0.16
    cats = {ln.split("|")[0] for ln in re.findall(r'"([A-Za-z0-9_ ()]+\|[A-Za-z0-9_]+)"', (BASE / "lib" / "60-sync.sh").read_text())}
    core = (pathlib.Path(NODE_SRC) / "nodes.py").read_text()
    for cls in ("UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "LoraLoader", "LoraLoaderModelOnly", "CheckpointLoaderSimple", "ControlNetLoader", "CLIPVisionLoader", "StyleModelLoader"):
        assert ("class %s" % cls) in core, cls
        assert cls in cats, cls


def test_comfyui_smoke_tool_covers_every_type_including_subgraphs_against_the_live_server(tmp_path):
    if not _server_up(): pytest.skip("testbed server not running on " + SERVER)
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "VAELoader", "mode": 0, "widgets_values": ["x"]}, {"id": 2, "type": "NoSuchNodeXYZ", "mode": 0, "widgets_values": []},
                                        {"id": 3, "type": "Label (rgthree)", "mode": 0, "widgets_values": []}],
                              "definitions": {"subgraphs": [{"id": "abc", "nodes": [{"id": 4, "type": "UNETLoader", "mode": 0, "widgets_values": []}]}]}, "links": [], "extra": {}}))
    r = subprocess.run(["python3", str(BASE / "py" / "smoke.py"), str(wf), SERVER], capture_output=True, text=True)
    assert r.returncode != 0 and "NoSuchNodeXYZ" in r.stdout, r.stdout + r.stderr
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "VAELoader", "mode": 0, "widgets_values": ["x"]}], "definitions": {"subgraphs": [{"id": "abc", "nodes": [{"id": 4, "type": "UNETLoader", "mode": 0, "widgets_values": []}]}]}, "links": [], "extra": {}}))
    r = subprocess.run(["python3", str(BASE / "py" / "smoke.py"), str(wf), SERVER], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_comfyui_combofix_tool_handles_the_live_object_info_shape(tmp_path):
    if not _server_up(): pytest.skip("testbed server not running on " + SERVER)
    # a stored value the server does offer, plus a renamed one that shares its head with an offered option
    with urllib.request.urlopen("http://%s/object_info/KSampler" % SERVER, timeout=60) as r:
        obj = json.loads(r.read())["KSampler"]
    samplers = obj["input"]["required"]["sampler_name"]
    samplers = samplers[0] if isinstance(samplers[0], list) else samplers[1]["options"]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "KSampler", "mode": 0, "widgets_values": [1, "fixed", 20, 8.0, samplers[0], "simple", 1.0]}], "links": [], "extra": {}}, indent=2))
    r = subprocess.run(["python3", str(BASE / "py" / "combofix.py"), SERVER, str(wf)], capture_output=True, text=True)
    assert r.returncode == 0 and "COMBOFIX 0 repaired 0 unresolved" in r.stdout, r.stdout + r.stderr
