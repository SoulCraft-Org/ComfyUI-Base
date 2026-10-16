#!/usr/bin/env bash
# _build/sage/build-wheel.sh: ONE SageAttention wheel for ONE GPU arch, compiled the way a pod compiles it
# (lib/40-packs.sh _base_sage_install), for .github/workflows/sage-wheels.yml. It runs inside an nvidia/cuda devel
# container whose CUDA minor is the torch backend's (cu130 -> 13.0.x), with uv mounted from the runner.
#
#   SRC      a SageAttention checkout at the commit being published (patched here, as a pod patches its clone)
#   ARCH     one TORCH_CUDA_ARCH_LIST entry: 9.0 | 10.0 | 10.3 | 12.0
#   TORCH    the torch version a pod would pick (py/torch_pick.py), e.g. 2.14.1
#   BACKEND  its CUDA build, e.g. cu130
#   PYMM     the Python minor, e.g. 3.14
#   OUT      where the wheel and key.txt go
#   MAX_JOBS, NVCC_THREADS   the runner's 4 vCPUs and 16 GB: 2 and 2 (upstream's --threads=8 is for a workstation)
#
# key.txt is the cache key a pod on that GPU computes for this build (sageattention-<sha12>-<cpXY>-torch<ver>-<sm>):
# the publish job checks every wheel against it, so a published wheel can only ever answer the key it was built for.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${SRC:?}" "${ARCH:?}" "${TORCH:?}" "${BACKEND:?}" "${PYMM:?}" "${OUT:?}"
# shellcheck source=../../lib/40-packs.sh
source "$here/lib/40-packs.sh"            # functions and a few assignments only: _base_sage_prepare_src, _base_sage_patch_setup

venv=/tmp/sage-venv
uv venv -q --python "$PYMM" "$venv"
uv pip install -q --python "$venv/bin/python" --torch-backend "$BACKEND" "torch==$TORCH"
uv pip install -q --python "$venv/bin/python" --upgrade pip setuptools wheel ninja packaging
PY="$venv/bin/python"
tv="$("$PY" -c 'import torch; print(torch.__version__.replace("+", "-"))')"
[ "$tv" = "$TORCH-$BACKEND" ] || { echo "::error::torch resolved to $tv, not $TORCH-$BACKEND"; exit 1; }
pytag="$("$PY" -c 'import sys; print("cp%d%d" % sys.version_info[:2])')"
sm="sm_${ARCH/./}"
sha="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || cat "$SRC/.sage-commit")"
key="sageattention-${sha:0:12}-${pytag}-torch${tv}-${sm}"

_base_sage_prepare_src "$SRC"
sed -i "s/--threads=8/--threads=${NVCC_THREADS:-2}/" "$SRC/setup.py"     # nvcc refuses a repeated --threads, so it is replaced
echo "building $key (TORCH_CUDA_ARCH_LIST=$ARCH, MAX_JOBS=${MAX_JOBS:-2}, nvcc --threads=${NVCC_THREADS:-2})"
mkdir -p "$OUT"
TORCH_CUDA_ARCH_LIST="$ARCH" MAX_JOBS="${MAX_JOBS:-2}" CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}" \
  "$PY" -m pip wheel --no-build-isolation --no-deps -w "$OUT" "$SRC"

whl="$(ls "$OUT"/sageattention-*.whl)"
[ "$(echo "$whl" | wc -l)" = 1 ] || { echo "::error::expected one wheel, got: $whl"; exit 1; }
# what the pod's import needs: _fused always (core imports it unconditionally), the qattn kernels upstream builds for this arch
"$PY" - "$whl" "$ARCH" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
need = ["_fused"] + (["_qattn_sm80", "_qattn_sm89", "_qattn_sm90"] if sys.argv[2] == "9.0" else ["_qattn_sm80", "_qattn_sm89"])
missing = [n for n in need if not any(x.startswith("sageattention/" + n) and x.endswith(".so") for x in names)]
print("\n".join("  " + x for x in names if x.endswith(".so")))
sys.exit("::error::the wheel lacks " + ", ".join(missing) if missing else 0)
PY
# it imports against this torch with no GPU (the driver stub stands in for libcuda): an ABI or link error stops here, not on a pod
stub=/tmp/cuda-stub; mkdir -p "$stub"; ln -sf "${CUDA_HOME:-/usr/local/cuda}/lib64/stubs/libcuda.so" "$stub/libcuda.so.1"
"$PY" -m pip install -q --no-deps "$whl"
LD_LIBRARY_PATH="$stub${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$PY" -c 'import sageattention, sageattention._fused; print("imports:", sageattention.__file__)'
echo "$key" > "$OUT/key.txt"
echo "built $(basename "$whl") for $key"
