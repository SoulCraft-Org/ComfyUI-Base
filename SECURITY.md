# Security

**Reporting.** Please report a vulnerability privately, not in a public issue: **Report a vulnerability** on this
repository's Security tab (GitHub private vulnerability reporting), or email support@soulcraft.computer with the subject
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
| Model files a package declares (`MODELS`) | huggingface.co or github.com only; a row naming any other host is refused | the exact byte size on every row, after the download and on every later run; every Hub download (file or snapshot) is checked with `hf cache verify` against the hashes the Hub states for the row's revision (sha256 for LFS files) before it moves into place, and a file that fails is deleted; each file is hashed once, when it downloads, and later runs check its size and the recorded commit and hash, so an existing volume pays nothing (3.12.0); a row pinned to a commit (`resolve/<commit>/…`, `hf://owner/repo@<commit>`) downloads exactly that commit; the Hub's sha256 for each file is also recorded in the package's `models.sha256` (`base.sh gen-hashes`). A GitHub asset is checked by size only |
| OS packages and the NVIDIA driver (`nvidia-open`, `cuda-toolkit`) | the distribution's apt mirrors and developer.download.nvidia.com | apt's signed repositories (NVIDIA's added from its `cuda-keyring`) |
| SageAttention | github.com: the wheel this repository's `sage-wheels` workflow built and published for the machine's exact key (a `sage-*` release, write-once, its tag protected), else built on the machine from source | the wheel's sha256 against the `sm_<arch>.sha256` published beside it, before it is installed; then a run on the GPU (the per-GPU proof) |
| The base library itself (`lib/`, `py/`, `suite/`, `hosts/`) | the zip you uploaded or this repository | every file against `MANIFEST.sha256`, on every run |
| A package zip | your machine, by `podctl upload` | size and sha256 compared on both sides |

**Newest by design.** The base installs the newest of everything on every run, so upstream code, including a node
pack's, reaches the machine without a review here. That is the maintainer's decision (handbook §0.3). A machine that
must not take untested upstream changes sets `BASE_RUNTIME`, which installs only a runtime that has passed its
maintainer's own sweep and changes nothing else.

**Nothing is uploaded.** comfy-cli's and huggingface_hub's telemetry are switched off before they run (`DO_NOT_TRACK`,
`COMFY_NO_TELEMETRY`, `HF_HUB_DISABLE_TELEMETRY`). ComfyUI is launched with `--disable-api-nodes`.

**Tokens.** `HF_TOKEN` and `CIVITAI_TOKEN` (or their aliases) come from the environment, the machine's own environment
or `state/tokens.env` (mode 0600). Provider keys on the Mac come from the environment, else (3.19.0) the macOS login
Keychain item whose account is the variable's name, else the provider's own config; a key pair counts only when both
halves come from one of those. A token value starting with `{{` is an unresolved RunPod Secret reference and is ignored
as a placeholder. A RunPod pod made by `podctl create` references the account's RunPod Secrets `HF_TOKEN` and
`CIVITAI_TOKEN` by name when they exist (podctl lists names only and never sees a value); the reference resolves inside
the account that owns the pod, so a customer's pod never gets the operator's keys. No token is ever printed, logged or put on a command line (curl reads it from a 0600 header file, `hf` from its environment); the suite runs with every
token name stripped from its environment. The install only reads from the Hub: use a read-only or fine-grained read
token; a token whose role is `write` is accepted with a warning (3.12.0). JupyterLab starts only with a token or password of its own.

**Exposure.** On every host except RunPod ComfyUI and JupyterLab listen on loopback only (reach them over an ssh
tunnel). On RunPod they sit behind RunPod's proxy; a new pod is public with named links (3.14.0); since 3.19.1 those links are open by default (one click, no key, no Jupyter token, as RunPod's proxy URLs are), and with `BASE_LINK_LOCK=1` (`podctl create --lock`) every link but JupyterLab's (which has its own token) is locked by the pod's own key: `?token=` once, then an HttpOnly, Secure, SameSite=Lax cookie; `podctl create --private` is the opt-in for ssh only.
ComfyUI has no login of its own: anyone who can reach its port can queue any workflow, which is why on a public
bind it listens on 127.0.0.1 only and `py/app_proxy.py` holds its port, behind that key.
