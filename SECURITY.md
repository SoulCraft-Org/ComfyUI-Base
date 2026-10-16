# Security

**Reporting.** Please report a vulnerability privately, not in a public issue: **Report a vulnerability** on this
repository's Security tab (GitHub private vulnerability reporting), or email support@soul-craft.com with the subject
"security". Say what is affected (the version is the handbook's first line), how to reproduce it, and what an attacker
gains. You get an answer within five working days; a fix ships as a new `base-vX.Y.Z` tag.

## What the installer downloads, and what it checks

ComfyUI Base runs as root on a machine you own or rent, so this is the whole list. The handbook (§0.3, §0.9, §0.15)
is the authority; this page is the summary.

| What | From | Checked how |
|---|---|---|
| ComfyUI, at its newest release | github.com (Comfy-Org/ComfyUI) | git, at the newest `vX.Y.Z` tag (or a newer `COMFY_REF` you name; never an older one) |
| Node packs a package declares (`PACKS`), and the base's six | github.com, each pack's own repository | git; the row's sha is the **last-tested record**, not a pin: the install takes the pack's newest commit and prints the sha it got |
| Python packages (ComfyUI's and every pack's requirements, uv, JupyterLab, comfy-cli, huggingface_hub, pytest) | pypi.org, pypi.nvidia.com, download.pytorch.org | uv's resolver, at the newest versions; no pack's `==` pin is honoured (handbook §0.3) |
| CPython | uv's managed builds (python-build-standalone on github.com) | uv |
| Model files a package declares (`MODELS`) | huggingface.co or github.com only; a row naming any other host is refused | the exact byte size on every row, after the download and on every later run; the Hub's own sha256 for each file is recorded in the package's `models.sha256` (`base.sh gen-hashes`) |
| OS packages and the NVIDIA driver (`nvidia-open`, `cuda-toolkit`) | the distribution's apt mirrors and developer.download.nvidia.com | apt's signed repositories (NVIDIA's added from its `cuda-keyring`) |
| SageAttention | github.com, built on the machine | built from source |
| The base library itself (`lib/`, `py/`, `suite/`, `hosts/`) | the zip you uploaded or this repository | every file against `MANIFEST.sha256`, on every run |
| A package zip | your machine, by `podctl upload` | size and sha256 compared on both sides |

**Newest by design.** The base installs the newest of everything on every run, so upstream code, including a node
pack's, reaches the machine without a review here. That is the maintainer's decision (handbook §0.3). A machine that
must not take untested upstream changes sets `BASE_RUNTIME`, which installs only a runtime that has passed its
maintainer's own sweep and changes nothing else.

**Nothing is uploaded.** comfy-cli's and huggingface_hub's telemetry are switched off before they run (`DO_NOT_TRACK`,
`COMFY_NO_TELEMETRY`, `HF_HUB_DISABLE_TELEMETRY`). ComfyUI is launched with `--disable-api-nodes`.

**Tokens.** `HF_TOKEN` (or its aliases) and provider keys come from the environment, the provider's own config, or
`state/tokens.env` (mode 0600). No token is ever printed, logged or put on a command line; the suite runs with every
token name stripped from its environment. JupyterLab starts only with a token or password of its own.

**Exposure.** On every host except RunPod ComfyUI and JupyterLab listen on loopback only (reach them over an ssh
tunnel). On RunPod they sit behind RunPod's proxy; a new pod is private unless `podctl create --public` (3.8.1).
ComfyUI has no login of its own: anyone who can reach its port can queue any workflow.
