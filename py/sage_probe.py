#!/usr/bin/env python3
"""Does SageAttention RUN on this GPU, mode by mode, called the way KJNodes' Patch Sage Attention calls it?

Usage (with the venv's own interpreter):  sage_probe.py

"import sageattention" succeeding proves nothing about the GPU. On a B200 (sm_100, 2026-10-16) the import worked, the
build held sm_100a code in _qattn_sm80 and _qattn_sm89, and the first sampling step still died: upstream's sageattn()
(what KJ's "auto" and ComfyUI's --use-sage-attention call) dispatches sm80/86/89/90/120/121 only and raises
`ValueError: Unsupported CUDA architecture: sm100`. Upstream withdrew its SM100 support in 2025-12 over accuracy
(thu-ml/SageAttention#322), and SageAttention3 refuses sm_100 at run time (#346). So each mode is called on a tiny
tensor and compared with PyTorch's own attention; a mode that raises, returns NaN or disagrees does not count.

Prints, and nothing else on stdout:
    SAGE_PROOF verdict=<ok|only|none|nogpu|noimport|unknown> sm=<sm100|-> use=<mode|-> version=<v|->
    SAGE_MODE <mode> <ok|fail> <detail>          one line per mode tried
verdict: ok = "auto" runs · only = "auto" fails and the modes marked ok run (use = the one to set) · none = nothing
runs · nogpu = no CUDA device to prove it on · noimport = sageattention does not import · unknown = the probe broke.
Exit 0 always: the verdict line is the answer.
"""
import sys

# KJNodes' sageattn_modes (nodes/model_optimization_nodes.py), each called with KJ's own arguments
MODES = ("auto", "sageattn_qk_int8_pv_fp16_cuda", "sageattn_qk_int8_pv_fp16_triton",
         "sageattn_qk_int8_pv_fp8_cuda", "sageattn_qk_int8_pv_fp8_cuda++", "sageattn3", "sageattn3_per_block_mean")
# when "auto" fails, the mode to name first: Triton is architecture-neutral and is the one a B200 render proved
PREFER = ("sageattn_qk_int8_pv_fp16_triton", "sageattn_qk_int8_pv_fp16_cuda",
          "sageattn_qk_int8_pv_fp8_cuda", "sageattn_qk_int8_pv_fp8_cuda++", "sageattn3", "sageattn3_per_block_mean")
MIN_COS = 0.99          # SageAttention 2's int8 kernels agree with full attention far above this; a broken kernel does not


def verdict(results):
    """results: {mode: (ok, detail)} for the modes tried → (verdict, use)."""
    if results.get("auto", (False, ""))[0]:
        return "ok", "auto"
    runs = [m for m in PREFER if results.get(m, (False, ""))[0]]
    if runs:
        return "only", runs[0]
    return "none", "-"


def _kj_call(mode, sa, q, k, v):
    if mode == "auto":
        return sa.sageattn(q, k, v, is_causal=False, attn_mask=None, tensor_layout="HND")
    if mode == "sageattn_qk_int8_pv_fp16_cuda":
        return sa.sageattn_qk_int8_pv_fp16_cuda(q, k, v, is_causal=False, attn_mask=None, pv_accum_dtype="fp32", tensor_layout="HND")
    if mode == "sageattn_qk_int8_pv_fp16_triton":
        return sa.sageattn_qk_int8_pv_fp16_triton(q, k, v, is_causal=False, attn_mask=None, tensor_layout="HND")
    if mode == "sageattn_qk_int8_pv_fp8_cuda":
        return sa.sageattn_qk_int8_pv_fp8_cuda(q, k, v, is_causal=False, attn_mask=None, pv_accum_dtype="fp32+fp32", tensor_layout="HND")
    if mode == "sageattn_qk_int8_pv_fp8_cuda++":
        return sa.sageattn_qk_int8_pv_fp8_cuda(q, k, v, is_causal=False, attn_mask=None, pv_accum_dtype="fp32+fp16", tensor_layout="HND")
    from sageattn3 import sageattn3_blackwell                       # KJ's sageattn3 modes; absent unless someone built it
    return sageattn3_blackwell(q, k, v, is_causal=False, attn_mask=None, per_block_mean=(mode == "sageattn3_per_block_mean"))


def _short(e):
    s = "%s: %s" % (type(e).__name__, e)
    return " ".join(s.split())[:160]


def probe(attempt, modes=MODES):
    """attempt(mode) → (ok, detail); an exception from it is a failed mode, never a broken probe."""
    results = {}
    for m in modes:
        try:
            results[m] = attempt(m)
        except Exception as e:                                       # noqa: BLE001 - every failure is a finding
            results[m] = (False, _short(e))
    return results


def report(v, sm, use, version, results):
    lines = ["SAGE_PROOF verdict=%s sm=%s use=%s version=%s" % (v, sm or "-", use or "-", version or "-")]
    lines += ["SAGE_MODE %s %s %s" % (m, "ok" if ok else "fail", d or "-") for m, (ok, d) in results.items()]
    return "\n".join(lines)


def main():
    try:
        import importlib.metadata as md
        import sageattention as sa
    except Exception as e:                                           # noqa: BLE001
        print(report("noimport", None, None, None, {"import": (False, _short(e))})); return 0
    try:
        version = md.version("sageattention")
    except Exception:                                                # noqa: BLE001 - SageAttention 2 may carry no metadata
        version = None
    try:
        import torch
        import torch.nn.functional as F
        if not torch.cuda.is_available():
            print(report("nogpu", None, None, version, {})); return 0
        major, minor = torch.cuda.get_device_capability(0)
        sm = "sm%d%d" % (major, minor)
        g = torch.Generator(device="cuda").manual_seed(0)
        # HND, the layout ComfyUI's attention hands KJ's patch: batch 1, 2 heads, 256 tokens, head_dim 64; bf16 as the models run
        q, k, v = (torch.randn(1, 2, 256, 64, device="cuda", dtype=torch.bfloat16, generator=g) for _ in range(3))
        ref = F.scaled_dot_product_attention(q, k, v).float().flatten()
    except Exception as e:                                           # noqa: BLE001
        print(report("unknown", None, None, version, {"setup": (False, _short(e))})); return 0

    def attempt(mode):
        if mode.startswith("sageattn3"):
            try:
                import sageattn3  # noqa: F401
            except Exception:                                        # noqa: BLE001
                return False, "sageattn3 not installed"
        out = _kj_call(mode, sa, q, k, v)
        torch.cuda.synchronize()
        out = out.float().flatten()
        if not torch.isfinite(out).all():
            return False, "non-finite output"
        cos = float(F.cosine_similarity(out, ref, dim=0))
        return (cos >= MIN_COS), "cos=%.4f" % cos

    results = probe(attempt)
    v, use = verdict(results)
    print(report(v, sm, use, version, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
