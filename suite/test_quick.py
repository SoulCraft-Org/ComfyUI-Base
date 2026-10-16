"""unit tier: tier 0 (3.16.0), the checks any session runs in seconds before a commit: _build/objinfo.py (a package's API
prompts against its cached /object_info, the way ComfyUI's validator reads the schema) and _build/quick.sh. Offline,
on synthetic schemas; the tools are repository-only (_build/ is not in the zip), so from the zip these skip."""
import importlib.util
import json
import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "_build" / "objinfo.py"
QUICK = BASE / "_build" / "quick.sh"


def _load():
    if not TOOL.exists():
        pytest.skip("no _build/objinfo.py (not a repo checkout)")
    spec = importlib.util.spec_from_file_location("objinfo", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# /object_info as ComfyUI 0.41 serves it: the old combo shape (a list in the type slot), the 0.34+ one (COMBO + options),
# an upload combo, an autogrow and a dynamic combo (V3), an output node.
TYPES = {
    "Loader": {"input": {"required": {"name": [["a.safetensors", "b.safetensors"]], "folder": ["COMBO", {"options": []}]}},
               "output": ["MODEL"], "output_node": False},
    "LoadImage": {"input": {"required": {"image": [["x.png"], {"image_upload": True}]}}, "output": ["IMAGE", "MASK"], "output_node": False},
    "Sampler": {"input": {"required": {"model": ["MODEL"], "steps": ["INT", {"min": 1, "max": 100}], "cfg": ["FLOAT", {"min": 0.0, "max": 30.0}],
                                       "sampler": ["COMBO", {"options": ["euler", "er_sde · fast"]}]},
                          "optional": {"image": ["IMAGE,MASK"]}},
                "output": ["LATENT"], "output_node": False},
    "Format": {"input": {"required": {"f_string": ["STRING", {}], "values": ["COMFY_AUTOGROW_V3", {}]}}, "output": ["STRING"], "output_node": False},
    "Save": {"input": {"required": {"latent": ["LATENT"], "format": ["COMFY_DYNAMICCOMBO_V3", {"options": []}]},
                       "optional": {"name": ["STRING"]}}, "output": [], "output_node": True},
}


def _good():
    return {
        "1": {"class_type": "Loader", "inputs": {"name": "a.safetensors", "folder": "anything"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "customer-upload.png"}},
        "3": {"class_type": "Sampler", "inputs": {"model": ["1", 0], "steps": 8, "cfg": 1.0, "sampler": "euler", "image": ["2", 0]}},
        "4": {"class_type": "Format", "inputs": {"f_string": "{a}", "values.a": ["1", 0]}},
        "5": {"class_type": "Save", "inputs": {"latent": ["3", 0], "format": "mp4", "format.codec": "auto", "name": ["4", 0]}},
    }


def test_unit_objinfo_passes_a_prompt_comfyui_accepts_and_names_what_it_cannot_judge():
    oi = _load()
    problems, unjudged = oi.check_prompt(_good(), TYPES)
    assert problems == [], problems
    joined = " ".join(unjudged)
    assert "2.image: upload combo" in joined          # LoadImage's VALIDATE_INPUTS checks the disk, not the list
    assert "1.folder: empty option list" in joined    # a model folder on a machine without the weights
    assert "5.format: dynamic input" in joined and "4.values: dynamic input" in joined


@pytest.mark.parametrize("node, change, said", [
    ("3", lambda n: n["inputs"].pop("steps"), "required input 'steps' is missing"),
    ("3", lambda n: n["inputs"].update(steps=0), "below its min 1"),
    ("3", lambda n: n["inputs"].update(cfg="lots"), "is not a FLOAT"),
    ("3", lambda n: n["inputs"].update(sampler="er_sde"), "did it become 'er_sde · fast'"),
    ("1", lambda n: n["inputs"].update(name="c.safetensors"), "is not one of its 2 options"),
    ("3", lambda n: n["inputs"].update(model=["9", 0]), "which is not a node of this prompt"),
    ("3", lambda n: n["inputs"].update(model=["1", 1]), "which has 1"),
    ("3", lambda n: n["inputs"].update(model=["2", 0]), "wants MODEL"),
    ("4", lambda n: n.update(class_type="Gone"), "not in the cached /object_info"),
])
def test_unit_objinfo_refuses_what_comfyui_refuses(node, change, said):
    oi = _load()
    p = _good(); change(p[node])
    problems, _ = oi.check_prompt(p, TYPES)
    assert any(said in x for x in problems), problems


def test_unit_objinfo_judges_only_what_the_outputs_reach_and_types_by_comma_sets():
    oi = _load()
    p = _good()
    p["6"] = {"class_type": "Sampler", "inputs": {"model": ["1", 0], "steps": 999, "cfg": 1.0, "sampler": "euler"}}   # feeds nothing
    assert oi.check_prompt(p, TYPES)[0] == []         # ComfyUI never validates a node no output needs
    p["3"]["inputs"]["image"] = ["2", 1]              # MASK into "IMAGE,MASK"
    assert oi.check_prompt(p, TYPES)[0] == []
    p["5"]["class_type"] = "Format"; p["5"]["inputs"] = {"f_string": "x"}
    assert any("no output node" in x for x in oi.check_prompt(p, TYPES)[0])
    assert oi.types_fit("*", "MODEL") and oi.types_fit(["a", "b"], "COMBO") and not oi.types_fit("LATENT", "IMAGE")


def test_unit_objinfo_check_reads_every_api_prompt_and_says_when_there_is_no_cache(tmp_path, capsys):
    oi = _load()
    pkg = tmp_path / "pkg"; (pkg / "app" / "prompts").mkdir(parents=True); (pkg / ".git").mkdir()
    (pkg / "app" / "prompts" / "fast.api.json").write_text(json.dumps(_good()))
    (pkg / ".git" / "old.api.json").write_text("not json")                         # dot-folders are never read
    assert [p.name for p in oi.prompts_of(pkg)] == ["fast.api.json"]
    assert oi.check(pkg) == 0 and "NOT checked: no _build/object_info.json" in capsys.readouterr().out
    (pkg / "_build").mkdir()
    (pkg / "_build" / "object_info.json").write_text(json.dumps({"comfyui": "0.41.2", "server": "127.0.0.1:8188", "generated": "now", "types": TYPES}))
    assert oi.check(pkg) == 0 and ": OK" in capsys.readouterr().out
    bad = _good(); bad["3"]["inputs"]["steps"] = 500
    (pkg / "app" / "prompts" / "best.api.json").write_text(json.dumps(bad))
    assert oi.check(pkg) == 1
    out = capsys.readouterr().out
    assert "app/prompts/best.api.json 3 (Sampler): 'steps' = 500 is above its max 100" in out and "1 problem(s)" in out


def test_unit_objinfo_save_caches_the_workflow_and_prompt_classes_in_comfyuis_order(tmp_path):
    oi = _load()
    pkg = tmp_path / "pkg"; pkg.mkdir()
    (pkg / "pkg-script.sh").write_text('WF_NAME="Pkg.json"\n')
    (pkg / "Pkg.json").write_text(json.dumps({"nodes": [{"type": "Loader"}, {"type": "Note"}, {"type": "sub-1"}],
                                              "definitions": {"subgraphs": [{"id": "sub-1", "nodes": [{"type": "Sampler"}]}]}}))
    (pkg / "x.api.json").write_text(json.dumps({"9": {"class_type": "Save", "inputs": {}}}))
    assert oi.used_classes(pkg) == ["Loader", "Sampler", "Save"]   # no frontend-only Note, no subgraph instance id


def test_unit_quick_runs_four_steps_and_the_test_command_takes_quick():
    if not QUICK.exists():
        pytest.skip("no _build/quick.sh (not a repo checkout)")
    assert subprocess.run(["bash", "-n", str(QUICK)]).returncode == 0
    src = QUICK.read_text()
    for needle in ("package.py", "test quick", "objinfo.py\" check", "zip_guard"):
        assert needle in src, needle
    lib = (BASE / "lib" / "95-summary.sh").read_text()
    heavy = lib.split('BASE_HEAVY_TIERS="${BASE_HEAVY_TIERS:-', 1)[1].split("}", 1)[0].split()
    for t in ("install", "comfyui", "server", "snapshot", "runpod", "gpu", "bare"):   # every tier that needs a live machine
        assert t in heavy, t
    assert "unit" not in heavy and "graph" not in heavy and "contract" not in heavy


def test_unit_objinfo_leaves_input_files_and_self_validated_inputs_to_the_node(tmp_path):
    """Run 38934921493 (Video Replacer, 2026-10-16): ComfyUI accepted every lane, while this check refused VHS_LoadVideo's
    placeholder clip and ModelAttentionBackend's 'comfy kitchen attention' (a CPU server lists PyTorch only); both inputs
    are judged by the node's own VALIDATE_INPUTS, which /object_info does not show."""
    oi = _load()
    types = dict(TYPES, Video={"input": {"required": {"video": [["a.mp4"]]}}, "output": ["IMAGE"], "output_node": False},
                 Attn={"input": {"required": {"attention": ["COMBO", {"options": ["pytorch"]}]}}, "output": ["MODEL"], "output_node": False})
    p = _good()
    p["2"] = {"class_type": "Video", "inputs": {"video": "example.mp4"}}
    p["1"] = {"class_type": "Attn", "inputs": {"attention": "comfy kitchen attention"}}
    problems, unjudged = oi.check_prompt(p, types)
    assert [x for x in problems if "attention" in x] and not [x for x in problems if "video" in x], problems
    assert any("2.video: input file" in u for u in unjudged)
    problems, unjudged = oi.check_prompt(p, types, own={"Attn.attention"})
    assert problems == [] and any("validated by the node itself" in u for u in unjudged), problems
    pkg = tmp_path / "pkg"; (pkg / "_build").mkdir(parents=True)
    (pkg / "_build" / "objinfo-self-validated.txt").write_text("# the node's VALIDATE_INPUTS\nAttn.attention   # CPU lists pytorch only\n")
    assert oi.self_validated(pkg) == {"Attn.attention"}


def test_unit_the_testbed_takes_cpu_torch_on_linux_and_leaves_the_pod_alone():
    """3.18.2 (error log row 108): on Linux PyPI's torch is the CUDA build, a 7 GB venv every CI gate downloaded again;
    the testbed never sees a GPU. The CPU index on Linux only, set before the torch install, and never in the pod's venv."""
    tb = BASE / "testbed.sh"
    if not tb.exists():
        pytest.skip("no testbed.sh (not a repo checkout)")
    src = tb.read_text()
    i = src.index('if [ "$(uname -s)" = Linux ]; then export UV_TORCH_BACKEND=cpu; fi')
    assert i < src.index("torch torchvision torchaudio >/dev/null"), "the backend must be set before the torch install"
    assert "UV_TORCH_BACKEND=cpu" not in (BASE / "lib" / "30-venv.sh").read_text(), "the pod's venv picks its own backend"
