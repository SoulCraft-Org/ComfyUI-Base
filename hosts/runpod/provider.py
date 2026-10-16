"""hosts/runpod/provider.py: RunPod, the first host. REST v1, a container whose start command runs the base's boot, and
/workspace mounted from a network volume. This is the code that was podctl's RunPod half until base 2.2.0, moved here
verbatim; the driver reaches it through RunPodProvider, and the driver's own shared helpers (ssh, scp, the Host block) are
reached back through _c(), the driver module registered as comfyui_podctl.

The API key is read from RUNPOD_API_KEY, else from runpodctl's ~/.runpod/config.toml (`apiKey = "..."`). It is sent as a
bearer header and never printed, logged or placed in an argument.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

from comfyui_hosts import Address, PodctlError, Provider

VOLUME_ROOT = "/workspace"          # RunPod mounts the network volume here (volumeMountPath); the base's BASE_VOLUME on this host


def _c():
    """The driver module (podctl.py), for the shared helpers: write_ssh_config, ssh_banner, ssh_run, _sq, SSH_ALIAS."""
    return sys.modules["comfyui_podctl"]


def write_ssh_config(path, host, port):
    return _c().write_ssh_config(path, host, port)


def ssh_banner(host, port, timeout=10):
    return _c().ssh_banner(host, port, timeout)


def _ssh_hostname():
    return _c()._ssh_hostname()


def ssh_run(remote_cmd, env=None, timeout=None):
    return _c().ssh_run(remote_cmd, env=env, timeout=timeout)


def _sq(s):
    return _c()._sq(s)


DEFAULT_API_URL = "https://rest.runpod.io/v1"
DEFAULT_GRAPHQL_URL = "https://api.runpod.io/graphql"   # 3.7.3: stock per data centre lives only here (REST v1 has no stock call)


PLACEHOLDERS = {"token_here", "replace_with_ids", "changeme", ""}


SECRET_NAMES = re.compile(r"token|key|secret|password", re.I)


def load_api_key(env=None, config_path=None):
    """RUNPOD_API_KEY, else runpodctl's config file. Never echoed by anything in this module."""
    env = os.environ if env is None else env
    key = (env.get("RUNPOD_API_KEY") or "").strip()
    if key:
        return key
    path = pathlib.Path(config_path) if config_path else pathlib.Path.home() / ".runpod" / "config.toml"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            # runpodctl 2.x: apikey = 'x'; older: apiKey = "x"; hand-written: apikey = x
            m = re.match(r"""\s*api_?key\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*$""", line, re.I)
            if m:
                key = next((g for g in m.groups() if g), "")
                if key:
                    return key
                # 2.0.56: `apikey = ''` (what runpodctl writes before a key is set) matched with every group empty,
                # and next() over an empty generator raised StopIteration -- a traceback where the line below belongs.
    raise PodctlError("no RunPod API key: run `runpodctl config --apiKey=<your key>` once, or export RUNPOD_API_KEY")


def _base_version():
    try:
        return (pathlib.Path(__file__).resolve().parents[2] / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


BASE_VERSION_FOR_UA = _base_version()


class Api:
    """The few REST v1 calls podctl needs. JSON in, JSON out; errors carry the status and the body, never the key."""

    def __init__(self, key, base_url=None, graphql_url=None):
        self.key = key
        self.base = (base_url or os.environ.get("RUNPOD_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.graphql_url = graphql_url or os.environ.get("RUNPOD_GRAPHQL_URL") or DEFAULT_GRAPHQL_URL

    USER_AGENT = "podctl/%s (ComfyUI Base; RunPod REST v1 client)" % BASE_VERSION_FOR_UA

    def _call(self, method, path, body=None, _retry=True, url=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url or (self.base + path), data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self.USER_AGENT)   # 2.0.31: Cloudflare blocks urllib's default signature (error 1010)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode(errors="replace")
            finally:
                e.close()                                    # an unclosed HTTPError is a ResourceWarning on newer Pythons
            if e.code == 403 and "error-1010" in text and _retry:
                import time
                time.sleep(5)                               # one retry: the edge rule is per request signature, not per key
                return self._call(method, path, body, _retry=False, url=url)
            raise PodctlError("%s %s -> HTTP %s: %s" % (method, path, e.code, text[:300]))
        except urllib.error.URLError as e:
            raise PodctlError("%s %s -> %s" % (method, path, e.reason))
        return json.loads(raw) if raw else {}

    def get(self, path):
        return self._call("GET", path)

    def patch(self, path, body):
        return self._call("PATCH", path, body)

    def post(self, path, body=None):
        return self._call("POST", path, body)

    def delete(self, path):
        return self._call("DELETE", path)

    def graphql(self, query, variables=None):
        """RunPod's GraphQL API, for what REST v1 does not answer (stock per data centre). Same bearer header."""
        out = self._call("POST", "graphql", {"query": query, "variables": variables or {}}, url=self.graphql_url)
        if out.get("errors"):
            raise PodctlError("GraphQL: %s" % "; ".join(str(e.get("message", e)) for e in out["errors"])[:300])
        return out.get("data") or {}


def pod_image(pod):
    """RunPod's REST v1 returns `imageName` (its openapi says `image`); accept both."""
    return pod.get("imageName") or pod.get("image") or ""


def pod_volume_id(pod):
    """The NETWORK volume's id, in either shape REST v1 has answered with: a top-level `networkVolumeId` (pods read on
    2026-09-05) or only the `networkVolume` object, which the live openapi names and which comes back with
    `includeNetworkVolume=true` (pods created by runpodctl v3 or the console, read on 2026-10-15). "" for none."""
    return pod.get("networkVolumeId") or (pod.get("networkVolume") or {}).get("id") or ""


POD_READ = "?includeMachine=true&includeNetworkVolume=true"   # 3.7.3: every pod read asks for both, so either shape is complete


def pod_volume(pod):
    """What persists at volumeMountPath: {'kind': 'network' | 'pod' | 'none', 'id', 'size' (GB or None), 'dc'}.
    3.7.3: a pod volume (`volumeInGb` > 0, the pod's own disk on that machine) used to print as "volume NONE", which
    reads as "nothing persists". It persists across stop/start, but it is the machine's: terminate deletes it and a
    clone cannot share it, so the status line says which kind it is."""
    vid = pod_volume_id(pod)
    if vid:
        nv = pod.get("networkVolume") or {}
        size = nv.get("size")
        return {"kind": "network", "id": vid, "size": int(size) if size is not None else None, "dc": nv.get("dataCenterId") or ""}
    gb = int(pod.get("volumeInGb") or 0)
    if gb > 0:
        return {"kind": "pod", "id": "", "size": gb, "dc": (pod.get("machine") or {}).get("dataCenterId") or ""}
    return {"kind": "none", "id": "", "size": None, "dc": ""}


def volume_line(pod):
    v, at = pod_volume(pod), pod.get("volumeMountPath") or "?"
    if v["kind"] == "network":
        extra = ", ".join(x for x in (("%d GB" % v["size"]) if v["size"] is not None else "", v["dc"]) if x)
        return "network volume %s%s at %s" % (v["id"], (" (%s)" % extra) if extra else "", at)
    if v["kind"] == "pod":
        return ("%spod volume %d GB at %s (this machine's disk: kept across stop/start, deleted with the pod, "
                "not shareable by deploy --like)" % ("encrypted " if pod.get("volumeEncrypted") else "", v["size"], at))
    return "NONE at %s" % at


def env_verdicts(env):
    """{name: 'placeholder (<value>)' | 'set' | 'unset'}: the value of anything secret-looking is never shown."""
    out = {}
    for name, value in sorted((env or {}).items()):
        v = (value or "").strip()
        if v in PLACEHOLDERS:
            out[name] = "placeholder (%s)" % (v or "empty")
        elif SECRET_NAMES.search(name):
            out[name] = "set"
        else:
            out[name] = "set (%s)" % v
    return out


def pod_facts(pod):
    env = pod.get("env") or {}
    mapping = pod.get("portMappings") or {}
    lines = [
        "pod        %s (%s)" % (pod.get("id"), pod.get("name")),
        "image      %s" % (pod_image(pod) or "(unknown)"),
        "status     %s" % pod.get("desiredStatus"),
        "public ip  %s" % (pod.get("publicIp") or "(none yet)"),
        "ports      %s" % " ".join(pod.get("ports") or []) ,
        "ssh        %s" % ("%s:%s" % (pod.get("publicIp"), mapping["22"]) if mapping.get("22") else "port 22 not mapped"),
        "volume     %s" % volume_line(pod),
        "start cmd  %s" % (" ".join(pod.get("dockerStartCmd") or []) or "(image default)"),
    ]
    for name, verdict in env_verdicts(env).items():
        lines.append("env        %-22s %s" % (name, verdict))
    if "8888/http" not in (pod.get("ports") or []):
        lines.append("jupyter    not exposed (no 8888/http port): podctl tunnel reaches the machine's own ports")
    elif not (env.get("JUPYTER_TOKEN") or env.get("JUPYTER_PASSWORD")):
        # 3.9.0: never tokenless. With none in the env the boot generates one (state/tokens.env, and the Logs tab)
        lines.append("jupyter    token: generated by the boot (state/tokens.env): podctl jupyter prints the link")
    return "\n".join(lines)


# ---------------------------------------------------------------- the ssh config block

# ONE alias, one Mac, several sessions: and the block is a single pointer. On 2026-09-08 a third session
# ran `ssh-config` for its own pod and every other session's ssh, scp, install and lease silently began
# addressing that pod instead: same command, same output shape, different machine. `PODCTL_HOST` gives a
# session its own block (export PODCTL_HOST=runpod-<podid>) so nothing it does moves anyone else's pointer.
# The default stays `runpod`, so a single-session Mac and every existing habit are unchanged.


BOOT_GUARD = "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi"


def _fetch_json(url, headers=None):
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def image_config(ref, fetch=None):
    """{'entrypoint': [...], 'cmd': [...]} of a public Docker Hub image, from the registry's config blob."""
    fetch = fetch or _fetch_json
    name, _, tag = ref.partition(":")
    tag = tag or "latest"
    if "/" not in name:
        name = "library/" + name
    if name.count("/") > 1 or name.split("/")[0].count(".") or name.startswith("ghcr.io"):
        raise PodctlError("only public Docker Hub images can be read (%s); pass --entry '<the image's start command>'" % ref)
    try:
        token = fetch("https://auth.docker.io/token?service=registry.docker.io&scope=repository:%s:pull" % name)["token"]
        h = {"Authorization": "Bearer " + token,
             "Accept": ", ".join(["application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json",
                                  "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.index.v1+json"])}
        base = "https://registry-1.docker.io/v2/%s" % name
        man = fetch(base + "/manifests/" + tag, headers=h)
        if "manifests" in man:                                        # a list: pick linux/amd64
            pick = next((m for m in man["manifests"] if (m.get("platform") or {}).get("architecture") == "amd64"
                         and (m.get("platform") or {}).get("os", "linux") == "linux"), man["manifests"][0])
            man = fetch(base + "/manifests/" + pick["digest"], headers=h)
        cfg = fetch(base + "/blobs/" + man["config"]["digest"], headers=h).get("config") or {}
    except PodctlError:
        raise
    except Exception as e:                                            # network, JSON, key errors: one line, no traceback
        raise PodctlError("could not read the image config of %s (%s); pass --entry '<the image's start command>'" % (ref, e))
    return {"entrypoint": list(cfg.get("Entrypoint") or []), "cmd": list(cfg.get("Cmd") or [])}


def wrap_entry(entrypoint, cmd, bootstrap):
    """The pod fields that make the base's boot.sh PID 1 when it exists, else run the sshd bootstrap and the image's
    own entry. A CMD is replaced (the entrypoint, usually a pass-through, stays); an entrypoint-only image gets its
    entrypoint replaced and CMD emptied."""
    import shlex
    # bootstrap is None when the image already runs its own sshd: the boot guard is still installed (that is what
    # makes the base PID 1), but nothing needs to start sshd. An empty part would leave "guard; ; exec ...": a
    # bash syntax error: so the parts are joined, not interpolated.
    prefix = "; ".join([BOOT_GUARD] + ([bootstrap] if bootstrap else []))
    if cmd:
        return {"dockerStartCmd": ["bash", "-c", "%s; exec %s" % (prefix, " ".join(shlex.quote(w) for w in cmd))]}
    if entrypoint:
        return {"dockerEntrypoint": ["bash", "-c", "%s; exec %s" % (prefix, " ".join(shlex.quote(w) for w in entrypoint))],
                "dockerStartCmd": []}
    raise PodctlError("the image declares neither an entrypoint nor a command; pass --entry '<the image's start command>'")


def sshd_bootstrap_text():
    """The self-contained sshd snippet, from lib/boot.sh (one source for the start command and the boot)."""
    import subprocess
    boot = pathlib.Path(__file__).resolve().parents[2] / "lib" / "boot.sh"
    r = subprocess.run(["bash", str(boot), "--print-sshd-bootstrap"], capture_output=True, text=True)
    if r.returncode != 0 or "boot_sshd" not in r.stdout:
        raise PodctlError("could not read the sshd bootstrap from %s" % boot)
    return r.stdout.strip()


def wait_for(api, pod_id, ok, what, sleep, tries=90, every=5):
    for _ in range(tries):
        pod = api.get("/pods/" + pod_id)
        if ok(pod):
            return pod
        sleep(every)
    raise PodctlError("gave up waiting for %s (%ds)" % (what, tries * every))


FIRST_BOOT_WAIT = 15 * 60      # 3.10.1: the longest ensure waits for an image's own first boot before resetting the pod
FIRST_BOOT_MIN = 180           # a pod with no 8188/http to ask: this long after it started counts as its first boot done


def started_at(pod):
    """RunPod's `lastStartedAt` ("2026-10-16 07:52:37.993 +0000 UTC") as epoch seconds, or None. It moves on every
    reset, which is how ensure knows a reset has really happened (3.10.1)."""
    import datetime
    raw = (pod.get("lastStartedAt") or "").replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


def proxy_status(url, timeout=10):
    """The HTTP status RunPod's proxy gives for `url`, or None when nothing answers at all."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": Api.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code
    except Exception:
        return None


def first_boot_done(pod, probe, now):
    """3.10.1: has the IMAGE's own first boot finished? An image like runpod/comfyui copies ComfyUI and its venv onto the
    volume on its first start; a reset in the middle leaves no venv and the pod crash-loops (MEASURED 2026-10-16, pod
    wibxqf22v26shv). An image that ships ComfyUI (its name says comfy) and exposes 8188/http: done when that ComfyUI answers
    through the proxy (any status below 500; the proxy says 502 while nothing listens). Any other image (runpod/base serves
    nothing on 8188, so waiting for it would cost the whole FIRST_BOOT_WAIT): FIRST_BOOT_MIN seconds since it started;
    unknown start: done."""
    if "8188/http" in (pod.get("ports") or []) and "comfy" in pod_image(pod).lower():
        code = probe(proxy_url(pod, 8188) + "/")
        return code is not None and code < 500
    t = started_at(pod)
    return t is None or now() - t >= FIRST_BOOT_MIN


def ensure(api, pod_id, pubkey, ssh_config, bootstrap, image_config=image_config, entry=None, banner=ssh_banner,
           sleep=None, ssh_check=None, log=None, probe=None, now=None):
    """Make the pod reachable and hand its boot to the base. Idempotent: every step checks before it changes.
    Order: (the image's first boot, 3.10.1) → ports (22/tcp) → env.PUBLIC_KEY → wait RUNNING + mapping → ssh banner →
    (refused) start command → wait for the reset → banner on the CURRENT port → the `Host runpod` block → `ssh runpod hostname`."""
    import time
    sleep = sleep or time.sleep
    log = log or (lambda s: print(s))
    probe = probe or proxy_status
    now = now or time.time
    pod = api.get("/pods/" + pod_id)
    state = {"waited": False}

    def before_reset(p):
        # 3.10.1: never reset a pod whose image is still in its own first boot. Only a pod the base's boot does not own yet
        # can be in one (with the guard installed, every boot is the base's, which a reset does not harm).
        if state["waited"] or BOOT_GUARD in " ".join((p.get("dockerEntrypoint") or []) + (p.get("dockerStartCmd") or [])):
            return
        state["waited"] = True
        t0 = now()
        while not first_boot_done(p, probe, now):
            if now() - t0 >= FIRST_BOOT_WAIT:
                log("ensure: the image's first boot has not finished after %d s: going on (the pod resets)" % FIRST_BOOT_WAIT)
                return
            if now() - t0 < 1:
                log("ensure: the image is still in its first boot: waiting for it before anything resets the pod")
            sleep(10)
            p = api.get("/pods/" + pod_id)
        if now() - t0 >= 1:
            log("ensure: the image's first boot is done (%d s)" % (now() - t0))

    def reset(body, what):
        before_reset(pod)
        log(what)
        api.patch("/pods/" + pod_id, body)
        resets.append(pod.get("lastStartedAt"))

    resets = []
    ports = list(pod.get("ports") or [])
    if "22/tcp" not in ports:
        reset({"ports": ports + ["22/tcp"]}, "ensure: exposing 22/tcp (the pod resets)")
    env = dict(pod.get("env") or {})
    if (env.get("PUBLIC_KEY") or "").strip() != pubkey.strip():
        env["PUBLIC_KEY"] = pubkey.strip()
        reset({"env": env}, "ensure: setting PUBLIC_KEY to the Mac key (the pod resets)")

    def up(p):
        return p.get("desiredStatus") == "RUNNING" and bool(p.get("publicIp")) and bool((p.get("portMappings") or {}).get("22"))
    pod = after_reset(api, pod_id, resets[0] if resets else None, up, sleep) if resets else wait_for(api, pod_id, up, "RUNNING with port 22 mapped", sleep)
    host, port = pod["publicIp"], int(pod["portMappings"]["22"])
    # 2.0.52: the override is installed when it is ABSENT, not when sshd happens to be silent. It used to live
    # inside the "nothing answers" branch, so an image that runs its own sshd (runpod/comfyui:1.4.7-cuda13.0) never
    # got it: and that pod's next restart came up on the IMAGE's ComfyUI instead of the base's boot.sh, losing the
    # base's launch line (--preview-method auto --preview-size 1024 --disable-api-nodes). Everything installed still
    # worked, so the only symptom was the previews warning, once per restart. Found on the cold-install pod after a
    # restart, 2026-09-08. Reading the pod's own fields keeps this idempotent: a pod that already carries the guard
    # is not patched and its image is never fetched, so `ensure` still resets nothing on a second run.
    answers = banner(host, port)
    installed = BOOT_GUARD in " ".join((pod.get("dockerEntrypoint") or []) + (pod.get("dockerStartCmd") or []))
    if not installed:
        if answers:
            log("ensure: sshd answers, but the base's boot is not PID 1: installing it (the pod resets)")
        else:
            log("ensure: nothing answers on %s:%d: this image runs no sshd; wrapping its start command" % (host, port))
        if entry:
            fields = wrap_entry([], list(entry), bootstrap if not answers else None)
        else:
            try:
                cfg = image_config(pod_image(pod))
            except PodctlError as e:
                raise PodctlError("%s: or pass --entry '<the image's start command>'" % e)
            fields = wrap_entry(cfg["entrypoint"], cfg["cmd"], bootstrap if not answers else None)
        before_reset(pod)
        was = pod.get("lastStartedAt")
        api.patch("/pods/" + pod_id, fields)
        resets.append(was)
        after_reset(api, pod_id, was, up, sleep)
        host, port = ssh_after_reset(api, pod_id, up, banner, sleep, log)
    elif resets:                                         # the address read before a reset is not trusted after it
        host, port = ssh_after_reset(api, pod_id, up, banner, sleep, log)
    write_ssh_config(ssh_config, host, port)
    log("ensure: Host %s -> %s:%d" % (_c().SSH_ALIAS, host, port))
    hostname = (ssh_check or _ssh_hostname)()
    return "reachable: %s:%d (pod hostname %s)" % (host, port, hostname)


def after_reset(api, pod_id, before, up, sleep, tries=36, every=5):
    """3.10.1: the pod once its reset has really happened: `lastStartedAt` moved from `before`, RUNNING, 22 mapped. Right
    after the PATCH the API still answers with the OLD pod (RUNNING, the old port), which is how ensure came to poll the
    old port forever (MEASURED 2026-10-16: RunPod moved 22/tcp from 20424 to 36944). A pod whose start time never moves
    within tries*every seconds is taken as it reads then; ssh_after_reset re-reads the port on every try either way."""
    p = None
    for _ in range(tries):
        p = api.get("/pods/" + pod_id)
        if up(p) and (before is None or p.get("lastStartedAt") != before):
            return p
        sleep(every)
    return wait_for(api, pod_id, up, "RUNNING with port 22 mapped after the reset", sleep)


def ssh_after_reset(api, pod_id, up, banner, sleep, log, tries=60, every=5):
    """3.10.1: wait for sshd on the pod's CURRENT address, re-read on every try (a reset may move the port again); (host, port)."""
    last = None
    for _ in range(tries):
        p = api.get("/pods/" + pod_id)
        if up(p):
            cur = (p["publicIp"], int(p["portMappings"]["22"]))
            if last and cur != last:
                log("ensure: the pod's ssh port moved: %s:%d -> %s:%d" % (last + cur))
            last = cur
            if banner(*cur):
                return cur
        sleep(every)
    raise PodctlError("sshd never answered on %s after the pod reset: read the pod's log" % ("%s:%d" % last if last else "any port"))


def stop(api, pod_id, wait=True, sleep=None):
    import time
    sleep = sleep or time.sleep
    api.post("/pods/%s/stop" % pod_id)
    if wait:
        wait_for(api, pod_id, lambda p: p.get("desiredStatus") in ("EXITED", "STOPPED"), "the pod to stop", sleep, tries=60)
    return "stopped %s" % pod_id


def start(api, pod_id, wait=True, sleep=None, banner=ssh_banner, ssh_config=None):
    """Start (resume) the pod; with wait, block until port 22 is mapped AND answers with an ssh banner, then refresh
    the `Host runpod` block (the public port changes on every start)."""
    import time
    sleep = sleep or time.sleep
    api.post("/pods/%s/start" % pod_id)
    if not wait:
        return "started %s" % pod_id
    pod = wait_for(api, pod_id, lambda p: p.get("desiredStatus") == "RUNNING" and p.get("publicIp") and (p.get("portMappings") or {}).get("22"),
                   "RUNNING with port 22 mapped", sleep, tries=120)
    host, port = pod["publicIp"], int(pod["portMappings"]["22"])
    for _ in range(120):
        if banner(host, port):
            break
        sleep(5)
    else:
        raise PodctlError("started, but sshd never answered on %s:%d: read the pod's boot log" % (host, port))
    write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, port)
    return "started %s: ssh answers on %s:%d (Host %s updated)" % (pod_id, host, port, _c().SSH_ALIAS)


def restart(api, pod_id, wait=True, sleep=None, banner=ssh_banner, ssh_config=None):
    api.post("/pods/%s/restart" % pod_id)
    out = "restarted %s" % pod_id
    if wait:
        import time
        sleep = sleep or time.sleep
        pod = wait_for(api, pod_id, lambda p: p.get("desiredStatus") == "RUNNING" and p.get("publicIp") and (p.get("portMappings") or {}).get("22"),
                       "RUNNING with port 22 mapped", sleep, tries=120)
        host, port = pod["publicIp"], int(pod["portMappings"]["22"])
        for _ in range(120):
            if banner(host, port):
                break
            sleep(5)
        else:
            raise PodctlError("restarted, but sshd never answered on %s:%d" % (host, port))
        write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, port)
        out += ": ssh answers on %s:%d" % (host, port)
    return out


def deploy_body(src, name=None, gpu=None):
    """The create body that clones a pod onto ITS OWN network volume: same image, GPU type, disk, ports, env and start command
    (the base's boot override: the clone boots the base from its first start and the template's own downloader never runs);
    the volume's datacenter; secure cloud when the source is. Never a templateId: the template would re-apply its own env and
    start command over the clone's. The one reason to exist: a machine whose GPU is not ours (2.0.26's verdict): a stop/start
    lands on the same machine; a new pod lands wherever the datacenter has the GPU free."""
    vol = src.get("networkVolume") or {}
    vid = pod_volume_id(src)
    if not vid:
        why = (" (its %s is a %d GB pod volume, the machine's own disk)" % (src.get("volumeMountPath") or "/workspace", src["volumeInGb"])
               if int(src.get("volumeInGb") or 0) > 0 else "")
        raise PodctlError("%s has no network volume%s: nothing for a clone to share (a redeploy is for a pod on a network "
                          "volume; podctl create makes one)" % (src.get("id"), why))
    if not vol.get("dataCenterId"):
        raise PodctlError("%s: the API did not say which data centre volume %s is in (networkVolume missing)" % (src.get("id"), vid))
    machine = src.get("machine") or {}
    gpu = gpu or machine.get("gpuTypeId")
    if not gpu:
        raise PodctlError("%s: the API did not name the GPU type (machine facts missing): pass --gpu" % src.get("id"))
    body = {"name": name or "%s-2" % (src.get("name") or "pod"), "imageName": src["imageName"], "gpuTypeIds": [gpu],
            "gpuCount": int(src.get("gpuCount") or 1), "containerDiskInGb": int(src.get("containerDiskInGb") or 50),
            "networkVolumeId": vid, "volumeMountPath": src.get("volumeMountPath") or "/workspace",
            "ports": list(src.get("ports") or []), "env": dict(src.get("env") or {}),
            "cloudType": "SECURE" if machine.get("secureCloud", True) else "COMMUNITY", "dataCenterIds": [vol["dataCenterId"]],
            "supportPublicIp": True, "computeType": "GPU", "interruptible": bool(src.get("interruptible"))}
    for k in ("dockerStartCmd", "dockerEntrypoint"):
        if src.get(k):
            body[k] = list(src[k])
    if body["env"].get("JUPYTER_TOKEN"):            # 3.9.0: a token is per pod; the clone gets its own
        body["env"]["JUPYTER_TOKEN"] = jupyter_token()
    return body


def deploy(api, like, name=None, gpu=None, wait=True, sleep=None, banner=ssh_banner, ssh_config=None, say=print):
    """A NEW pod cloned from `like` onto the same volume. With wait: until sshd answers, then `Host runpod` → the new pod.
    Returns the new pod id. Prints counts and names, never an env value."""
    import time
    sleep = sleep or time.sleep
    src = api.get("/pods/%s%s" % (like, POD_READ))
    body = deploy_body(src, name=name, gpu=gpu)
    if src.get("templateId"):                       # 3.9.0: the names template is the one template a clone keeps
        try:
            t = api.get("/templates/%s" % src["templateId"]) or {}
        except Exception:                           # best-effort: a template we cannot read is not ours to keep
            t = {}
        if str(t.get("name") or "").startswith(NAMES_TEMPLATE_PREFIX):
            body["templateId"] = src["templateId"]
    new = api.post("/pods", body)
    pod_id = new["id"]
    say("deployed %s (%s): %s · %s × %d · %d GB container disk · volume %s at %s (%s) · %d ports · %d env vars · start command %s"
        % (pod_id, body["name"], body["imageName"], body["gpuTypeIds"][0], body["gpuCount"], body["containerDiskInGb"], body["networkVolumeId"],
           body["volumeMountPath"], body["dataCenterIds"][0], len(body["ports"]), len(body["env"]), "cloned (the base's boot)" if body.get("dockerStartCmd") else "(image default)"))
    if new.get("machineId") and new.get("machineId") == src.get("machineId"):
        say("  note: the SAME machine as %s (%s): the GPU verdict decides" % (like, new["machineId"]))
    if not wait:
        return pod_id
    pod = wait_for(api, pod_id, lambda p: p.get("desiredStatus") == "RUNNING" and p.get("publicIp") and (p.get("portMappings") or {}).get("22"),
                   "RUNNING with port 22 mapped", sleep, tries=120)
    host, port = pod["publicIp"], int(pod["portMappings"]["22"])
    for _ in range(120):
        if banner(host, port):
            break
        sleep(5)
    else:
        raise PodctlError("deployed %s, but sshd never answered on %s:%d: read the pod's boot log in the console" % (pod_id, host, port))
    write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, port)
    say("  ssh answers on %s:%d (Host %s → %s); machine %s" % (host, port, _c().SSH_ALIAS, pod_id, pod.get("machineId") or "?"))
    if pod.get("machineId") and pod.get("machineId") == src.get("machineId"):
        say("  note: the SAME machine as %s: the GPU verdict decides" % like)
    return pod_id


# Command sessions carry NO port forwards: Host runpod's LocalForward lines are for the tunnel, and a command session that
# tries them while a tunnel holds the ports prints "bind: Address already in use" three times per call (2026-09-06).


def volume_size_gb(api, pod):
    """The persistent volume's size in GB: a network volume's from RunPod's REST (or the embedded networkVolume), a pod
    volume's from `volumeInGb`; None for a pod without either."""
    v = pod_volume(pod)
    if v["kind"] == "pod" or (v["kind"] == "network" and v["size"] is not None):
        return v["size"]
    if v["kind"] != "network":
        return None
    nv = api.get("/networkvolumes/%s" % v["id"])
    size = nv.get("size") if isinstance(nv, dict) else None
    return int(size) if size is not None else None


def record_volume(api, pod, env=None):
    """Write /workspace/comfy-base/state/volume.env (VOLUME_GB, VOLUME_ID, RECORDED) on the pod: the base's disk gate
    reads it, because df on the pooled volume reports the pool, not the quota. Returns the line printed, or None."""
    import datetime
    size = volume_size_gb(api, pod)
    if size is None:
        return None
    vid = pod_volume_id(pod) or "pod-volume:%s" % pod.get("id")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = "mkdir -p /workspace/comfy-base/state && printf 'VOLUME_GB=%%s\\nVOLUME_ID=%%s\\nRECORDED=%%s\\n' %s %s %s > /workspace/comfy-base/state/volume.env" % (
        _sq(str(size)), _sq(vid), _sq(ts))
    r = ssh_run(cmd, env=env)
    if r.returncode != 0:
        raise PodctlError("could not record the volume size on the pod: %s" % r.stderr.strip()[-300:])
    return "volume  %d GB (%s) recorded in state/volume.env" % (size, vid)


# ---------------------------------------------------------------- 3.9.0: port names (the console's Connect tab)
# RunPod names a port only on a TEMPLATE: GraphQL PodTemplate.portsConfig [{port, name}] (graphql-spec.runpod.io; runpodctl
# v3.0.1 internal/api/template_port_labels.go, "a named port shown in the Runpod dashboard"). REST v1 and v2 have no
# label field, and a pod has no portsConfig of its own: its Connect tab shows its template's names, and a pod without a
# template shows "Port 8188 -> HTTP Service". So `create` puts each pod on a small PRIVATE template of this account,
# "comfy-base ports <sig>", one per set of names, that carries the names and nothing else (no env, no start command,
# no volume): every other field of the pod is the create body's own. saveTemplate REPLACES a template, so the names are
# written the way runpodctl writes them: read the whole template back, re-send every field with portsConfig set.

NAMES_TEMPLATE_PREFIX = "comfy-base ports "
TEMPLATE_GQL = ("id name imageName dockerArgs env { key value } ports volumeMountPath volumeInGb containerDiskInGb "
                "containerRegistryAuthId startJupyter startSsh startScript isServerless isPublic readme advancedStart "
                "category portsConfig { port name }")
SAVE_TEMPLATE = "mutation SaveTemplatePortNames($input: SaveTemplateInput) { saveTemplate(input: $input) { id } }"
NAMES_README = ("Port names for pods made by ComfyUI Base's `podctl create`. It carries the names of the pod's HTTP ports "
                "and nothing else: every other field of a pod made with it is that pod's own.")


def http_ports(names, tcp=("22/tcp",)):
    """[(port, name)] -> the pod's ports: each named one as http, then the tcp ones (sshd)."""
    return ["%d/http" % int(p) for p, _ in names] + list(tcp)


def names_signature(names):
    import hashlib
    return hashlib.sha1(";".join("%d=%s" % (int(p), n) for p, n in names).encode("utf-8")).hexdigest()[:10]


def _template_ports(value):
    """GraphQL answers ports as one string ("8188/http,22/tcp"), REST as a list: the bare numbers either way."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    return [i.strip().split("/")[0] for i in items if i and i.strip()]


def template_state(api, template_id, tries=10, sleep=None):
    """The whole template as saveTemplate wants it back, from GraphQL `myself { podTemplates }`. A template REST made a
    moment ago can be missing from GraphQL for a few hundred milliseconds (runpodctl retries 10 x 250 ms), so this does too."""
    import time
    sleep = sleep or time.sleep
    for i in range(tries):
        data = api.graphql("query PodctlTemplate { myself { podTemplates { %s } } }" % TEMPLATE_GQL)
        for t in ((data.get("myself") or {}).get("podTemplates") or []):
            if t.get("id") == template_id:
                return t
        if i + 1 < tries:
            sleep(0.25)
    raise PodctlError("template %s: not among this account's templates (GraphQL myself.podTemplates)" % template_id)


def save_port_names(api, template_id, names, sleep=None):
    """Write [(port, name)] as the template's port names, keeping every other field; returns the names it read back.
    A name for a port the template does not expose, or a blank name, is refused before anything is sent."""
    st = template_state(api, template_id, sleep=sleep)
    have = set(_template_ports(st.get("ports")))
    labels, seen = [], set()
    for port, name in names:
        port, name = str(int(port)), (name or "").strip()
        if not name:
            raise PodctlError("port %s: a port name may not be blank" % port)
        if port in seen:
            raise PodctlError("port %s is named twice" % port)
        if port not in have:
            raise PodctlError("port %s is not one of template %s's ports (%s)" % (port, template_id, ", ".join(sorted(have)) or "none"))
        seen.add(port)
        labels.append({"port": port, "name": name})
    ports = st.get("ports")
    inp = {"id": st["id"], "name": st.get("name") or "", "imageName": st.get("imageName") or "",
           "containerDiskInGb": int(st.get("containerDiskInGb") or 0), "containerRegistryAuthId": st.get("containerRegistryAuthId") or "",
           "dockerArgs": st.get("dockerArgs") or "", "env": st.get("env") or [],
           "ports": ports if isinstance(ports, str) else ",".join(ports or []), "portsConfig": labels,
           "volumeInGb": int(st.get("volumeInGb") or 0), "isPublic": bool(st.get("isPublic")), "isServerless": bool(st.get("isServerless")),
           "startJupyter": bool(st.get("startJupyter")), "startSsh": bool(st.get("startSsh")), "advancedStart": bool(st.get("advancedStart")),
           "readme": st.get("readme") or ""}
    for k in ("volumeMountPath", "startScript", "category"):
        if st.get(k):
            inp[k] = st[k]
    out = api.graphql(SAVE_TEMPLATE, {"input": inp})
    if not ((out.get("saveTemplate") or {}).get("id")):
        raise PodctlError("template %s: saveTemplate answered without an id (%r)" % (template_id, out))
    back = template_state(api, template_id, sleep=sleep).get("portsConfig") or []
    got = [(int(b["port"]), b["name"]) for b in back]
    if sorted(got) != sorted((int(l["port"]), l["name"]) for l in labels):
        raise PodctlError("template %s: the names read back as %s, not %s" % (template_id, got, [(l["port"], l["name"]) for l in labels]))
    return got


def names_template(api, names, image, say=print, sleep=None):
    """The id of this account's private template that names exactly these ports, made the first time it is needed."""
    tname = NAMES_TEMPLATE_PREFIX + names_signature(names)
    tid = None
    for t in (api.get("/templates") or []):
        if t.get("name") == tname:
            tid = t.get("id")
            break
    if tid:
        have = template_state(api, tid, sleep=sleep).get("portsConfig") or []
        if sorted((int(b["port"]), b["name"]) for b in have) == sorted((int(p), n) for p, n in names):
            return tid
    else:
        t = api.post("/templates", {"name": tname, "imageName": image, "isPublic": False, "isServerless": False, "category": "NVIDIA",
                                    "containerDiskInGb": 50, "volumeInGb": 0, "volumeMountPath": VOLUME_ROOT, "ports": http_ports(names),
                                    "env": {}, "dockerStartCmd": [], "dockerEntrypoint": [], "readme": NAMES_README})
        tid = t.get("id")
        if not tid:
            raise PodctlError("POST /templates answered without an id (%r)" % (t,))
        say("made template %s (%s): private, port names only" % (tid, tname))
    save_port_names(api, tid, names, sleep=sleep)
    return tid


def jupyter_token():
    """A fresh JupyterLab token for one pod: 48 hex characters from the OS's CSPRNG. Never printed by create."""
    import secrets
    return secrets.token_hex(24)


def proxy_url(pod, port):
    """RunPod's proxy URL for a port the pod exposes as http, else None (a --private pod exposes none)."""
    if "%d/http" % int(port) not in (pod.get("ports") or []):
        return None
    return "https://%s-%d.proxy.runpod.net" % (pod.get("id"), int(port))


# ---------------------------------------------------------------- 3.7.3: create, from nothing

STOCK_QUERY = "query { dataCenters { id name location gpuAvailability { gpuTypeId stockStatus available } } }"
CREATE_PORTS = ["8188/http", "8888/http", "22/tcp"]      # ComfyUI and JupyterLab through the proxy, sshd on a public port
PRIVATE_PORTS = ["22/tcp"]                               # 3.7.4 --private: sshd only; ComfyUI is reached through `podctl tunnel`
CUDA_VERSIONS = ("13.0", "12.9", "12.8", "12.7", "12.6", "12.5", "12.4", "12.3", "12.2", "12.1", "12.0", "11.8")  # the openapi's enum
# 3.8.0: with no --gpu, `create` asks for these in order (the video packages' GPU_PREFER, py/gpu_fit.py): B200 first,
# then B300. "NVIDIA B300 SXM6 AC MIG 1g.34gb" is a 34 GB slice of a B300 and is never on this list.
DEFAULT_GPUS = ("NVIDIA B200", "NVIDIA B300 SXM6 AC")
# The base installs a cu130 torch whatever the image was built for, so a host below CUDA 13.0 (a pre-580 driver) fails at
# the first kernel. Without --min-cuda the floor is never lower than this; an explicit --min-cuda still wins.
DEFAULT_MIN_CUDA = "13.0"


def stock(api, gpu):
    """[(data centre id, stock status)] where `gpu` can be rented NOW, in RunPod's order (GraphQL dataCenters; REST v1 has
    no stock call). `available` is the verdict; stockStatus (Low, Medium, High) is said beside it."""
    rows = []
    for dc in (api.graphql(STOCK_QUERY).get("dataCenters") or []):
        for g in dc.get("gpuAvailability") or []:
            if g.get("gpuTypeId") == gpu and g.get("available"):
                rows.append((dc.get("id"), g.get("stockStatus") or "?"))
    return rows


def cuda_floor(image, min_cuda=None):
    """The lowest host CUDA the pod accepts: --min-cuda, else what the image's tag says it was built for (cuda13.0,
    cu130), else None (any). A cu130 image on a 12.x driver starts and then fails at the first kernel."""
    if min_cuda:
        return str(min_cuda)
    tag = (image or "").rpartition(":")[2]
    m = re.search(r"cuda[-_]?(\d+)\.(\d+)", tag) or re.search(r"(?<![a-z])cu(\d{2})(\d)(?!\d)", tag)
    return "%s.%s" % (int(m.group(1)), m.group(2)) if m else None


def create_floor(image, min_cuda=None):
    """The floor `create` sends: --min-cuda as given, else the image tag's floor raised to DEFAULT_MIN_CUDA (3.8.0)."""
    if min_cuda:
        return str(min_cuda)
    key = lambda v: tuple(int(x) for x in v.split("."))
    tag = cuda_floor(image)
    return tag if tag and key(tag) > key(DEFAULT_MIN_CUDA) else DEFAULT_MIN_CUDA


def allowed_cuda(floor):
    if not floor:
        return None
    key = lambda v: tuple(int(x) for x in v.split("."))
    if floor not in CUDA_VERSIONS and not any(key(v) >= key(floor) for v in CUDA_VERSIONS):
        raise PodctlError("--min-cuda %s: RunPod offers %s" % (floor, ", ".join(CUDA_VERSIONS)))
    return [v for v in CUDA_VERSIONS if key(v) >= key(floor)]


PRIVATE_UNENCRYPTED = ("note    the pod volume will NOT be encrypted: RunPod's API cannot ask for that (only the console's "
                       "\"Encrypt volume\" box can, at create time). Private here means no public port: ssh only, ComfyUI "
                       "through podctl tunnel. The volume is this machine's: at a $0 account balance RunPod terminates the pod "
                       "and the volume with it (a network-volume pod is only stopped).")


def encryption_words(pod, gb=None):
    """3.9.1: the pod volume as the read-back describes it. True and False are RunPod's word; a missing field is said."""
    gb = int(gb if gb is not None else pod.get("volumeInGb") or 0)
    enc = pod.get("volumeEncrypted")
    kind = "ENCRYPTED pod volume" if enc is True else "pod volume"
    tail = "" if enc is True else (" (not encrypted)" if enc is False else " (encryption not reported)")
    return "%s %d GB at %s%s" % (kind, gb, VOLUME_ROOT, tail)


def create_body(name, gpu, dc, volume_id, image, container_gb, pubkey, cuda=None, spot=False, private_gb=None, names=None,
                template_id=None, token=None):
    """The POST /pods body for a pod on a network volume: Secure Cloud (network volumes exist only there), the volume's
    data centre, the ports, PUBLIC_KEY, the CUDA floor. No start command: `ensure` installs the base's boot guard from the
    image's own entry, as for every other pod; and no templateId but the port-names one (3.9.0), which carries nothing else.
    3.7.4 private_gb: a POD VOLUME of that size instead, 22/tcp the only port (no proxied HTTP), and
    BASE_LISTEN=127.0.0.1 so ComfyUI binds loopback and is reached through `podctl tunnel` alone.
    3.9.1: never `volumeEncrypted`. REST v1 POST /pods refuses it ("Extra input keys"): it is on the Pod read-back only,
    and no first-party create input (REST v1 PodCreateInput, REST v2, runpodctl 3.0.1) can ask for an encrypted volume.
    RunPod's storage docs name one way: the console's "Encrypt volume" box.
    3.9.0 names: [(port, name)], every one an http port (default the base's two); template_id: the template that names
    them in the console; token: the pod's own JUPYTER_TOKEN (JupyterLab is never tokenless). A private pod takes none of
    the three: no http port, so nothing to name, and the boot makes its token on the pod volume."""
    body = {"name": name, "imageName": image, "gpuTypeIds": [gpu], "gpuCount": 1, "cloudType": "SECURE", "computeType": "GPU",
            "dataCenterIds": [dc], "networkVolumeId": volume_id, "volumeMountPath": VOLUME_ROOT, "containerDiskInGb": int(container_gb),
            "ports": list(CREATE_PORTS), "env": {"PUBLIC_KEY": pubkey.strip()}, "supportPublicIp": True, "interruptible": bool(spot)}
    if not private_gb:
        body["ports"] = http_ports(names) if names else list(CREATE_PORTS)
        if token:
            body["env"]["JUPYTER_TOKEN"] = token
        if template_id:
            body["templateId"] = template_id
    if private_gb:
        del body["networkVolumeId"]
        body.update({"volumeInGb": int(private_gb), "ports": list(PRIVATE_PORTS),
                     "env": {"PUBLIC_KEY": pubkey.strip(), "BASE_LISTEN": "127.0.0.1"}})
    if cuda:
        body["allowedCudaVersions"] = list(cuda)
    return body


def create(api, name, gpu, dc, data_gb=None, volume=None, image=None, container_gb=100, pubkey=None, min_cuda=None, spot=False,
           wait=True, sleep=None, banner=ssh_banner, ssh_config=None, say=print, private=False, names=None, name_ports=True):
    """A pod from nothing: a network volume of `data_gb` in `dc` (or the existing `volume`), then the pod on it. A refused
    pod (sold out between the stock check and the create) takes a volume THIS call made with it, so nothing is left
    billing; an existing volume is never deleted. With wait: until sshd answers, then `Host runpod` -> the new pod.
    Returns the pod id. Prints names and counts, never the key."""
    import time
    sleep = sleep or time.sleep
    if not image:
        raise PodctlError("--image is required on RunPod (the pod's container image, e.g. a ComfyUI image built for CUDA 13)")
    if not pubkey:
        raise PodctlError("no public key for PUBLIC_KEY: pass --pubkey <file.pub>")
    cuda = allowed_cuda(create_floor(image, min_cuda))
    if private:
        return _create_private(api, name, gpu, dc, data_gb, volume, image, container_gb, pubkey, cuda, spot, wait, sleep,
                               banner, ssh_config, say, names=names)
    made = None
    if volume:
        vid = volume
    else:
        if not data_gb:
            raise PodctlError("--data-gb N (a new network volume) or --volume <id> (an existing one)")
        made = vid = api.post("/networkvolumes", {"name": "%s-data" % name, "size": int(data_gb), "dataCenterId": dc})["id"]
        say("created network volume %s (%s-data): %d GB in %s" % (vid, name, int(data_gb), dc))
    names = list(names or _c().BASE_PORT_NAMES)
    tid = None
    if name_ports:
        try:
            tid = names_template(api, names, image, say=say, sleep=sleep)
        except PodctlError as e:                # the names are the console's, never a reason to have no pod
            say("  !! the port names could not be written (%s): the Connect tab will say \"HTTP Service\"" % e)
    body = create_body(name, gpu, dc, vid, image, container_gb, pubkey, cuda=cuda, spot=spot, names=names, template_id=tid,
                       token=jupyter_token())     # printed once, as the Jupyter Terminal link; RunPod keeps it in the pod's env
    try:
        made_pod = api.post("/pods", body)
        pod_id = made_pod["id"]
    except Exception:
        if made:
            try:
                api.delete("/networkvolumes/%s" % made)
                say("  the pod was refused: network volume %s deleted again (nothing left billing)" % made)
            except Exception as e:  # the refusal is the news; a leftover volume is named so it can be removed
                say("  !! could not delete the new network volume %s after the refusal: %s" % (made, e))
        raise
    say("created %s (%s): %s · %s × 1 · %s · %d GB container disk · network volume %s at %s · ports %s · CUDA %s%s" % (
        pod_id, name, image, gpu, dc, int(container_gb), vid, VOLUME_ROOT, " ".join(body["ports"]),
        (">= %s" % cuda[-1] if cuda else "any"), " · spot" if spot else ""))
    say("  names    %s%s" % (" · ".join("%d %s" % (int(p), n) for p, n in names),
                             (" (template %s)" % tid) if tid else " (not in the console: no template)"))
    back = made_pod.get("ports")
    if back and sorted(back) != sorted(body["ports"]):
        say("  !! %s came back with ports %s, not %s" % (pod_id, " ".join(back), " ".join(body["ports"])))
    # 3.9.0 (simple first): the ready links, the terminal's with its token, so nothing has to be looked up. They answer
    # once the base is installed and has booted the pod (podctl ensure, then install); podctl status prints them again.
    for line in _c().links_lines(names, lambda p: proxy_url({"id": pod_id, "ports": body["ports"]}, p), body["env"].get("JUPYTER_TOKEN")):
        say("  " + line)
    if not wait:
        return pod_id
    pod = wait_for(api, pod_id, lambda p: p.get("desiredStatus") == "RUNNING" and p.get("publicIp") and (p.get("portMappings") or {}).get("22"),
                   "RUNNING with port 22 mapped", sleep, tries=120)
    host, port = pod["publicIp"], int(pod["portMappings"]["22"])
    for _ in range(120):
        if banner(host, port):
            break
        sleep(5)
    else:
        raise PodctlError("created %s, but sshd never answered on %s:%d: does the image run sshd from PUBLIC_KEY? "
                          "podctl ensure %s wraps its start command" % (pod_id, host, port, pod_id))
    write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, port)
    say("  ssh answers on %s:%d (Host %s -> %s)" % (host, port, _c().SSH_ALIAS, pod_id))
    return pod_id


SEED_TOKEN_CMD = ("umask 077; mkdir -p %s/comfy-base/state; f=%s/comfy-base/state/tokens.env; "
                  "if grep -q '^JUPYTER_TOKEN=' \"$f\" 2>/dev/null; then sed -n 's/^JUPYTER_TOKEN=//p' \"$f\" | tail -1; "
                  "else IFS= read -r l; printf '%%s\\n' \"$l\" >> \"$f\"; chmod 600 \"$f\"; echo SEEDED; fi" % (VOLUME_ROOT, VOLUME_ROOT))


def seed_jupyter_token(say=print):
    """3.9.0: a private pod's JupyterLab token, put in <volume>/comfy-base/state/tokens.env (0600) through ssh's stdin, where
    the boot reads it; one already there is kept and returned. None when ssh does not take it (the tunnel reads it later)."""
    tok = jupyter_token()
    try:
        r = _c().ssh_run(SEED_TOKEN_CMD, timeout=60, input="JUPYTER_TOKEN=%s\n" % tok)
    except Exception as e:
        say("  the terminal's token could not be handed over (%s): the boot makes one; podctl tunnel prints it" % e)
        return None
    if r.returncode != 0:
        say("  the terminal's token could not be handed over (ssh exit %d): the boot makes one; podctl tunnel prints it" % r.returncode)
        return None
    last = (r.stdout or "").strip().splitlines()[-1:] or [""]
    return tok if last[0] == "SEEDED" else (last[0] or None)


def _create_private(api, name, gpu, dc, data_gb, volume, image, container_gb, pubkey, cuda, spot, wait, sleep, banner, ssh_config, say,
                    names=None):
    """3.7.4 `create --private`: a pod volume, sshd only, ComfyUI on loopback.
    3.9.1: the volume is NOT encrypted, and that is said before the pod exists, never after: no create input RunPod
    documents can ask for encryption (only the console's "Encrypt volume" box can). The summary prints what the read-back
    says about the volume, never what was hoped for."""
    if volume:
        raise PodctlError("--private cannot use --volume: a private pod keeps its data on its own pod volume of --data-gb; "
                          "pass --public to put a pod on the network volume %s (with proxy URLs)" % volume)
    if not data_gb:
        raise PodctlError("--private needs --data-gb N: the size of the pod volume at %s" % VOLUME_ROOT)
    say(PRIVATE_UNENCRYPTED)
    body = create_body(name, gpu, dc, None, image, container_gb, pubkey, cuda=cuda, spot=spot, private_gb=data_gb)
    pod_id = api.post("/pods", body)["id"]
    back = api.get("/pods/%s%s" % (pod_id, POD_READ))
    ports = back.get("ports") or body["ports"]
    if sorted(ports) != sorted(PRIVATE_PORTS):
        say("  !! %s came back with ports %s, not %s" % (pod_id, " ".join(ports), " ".join(PRIVATE_PORTS)))
    say("created %s (%s): %s · %s × 1 · %s · %d GB container disk · %s · ports %s only · "
        "ComfyUI on loopback · CUDA %s%s" % (pod_id, name, image, gpu, dc, int(container_gb), encryption_words(back, data_gb),
                                              " ".join(PRIVATE_PORTS), (">= %s" % cuda[-1] if cuda else "any"), " · spot" if spot else ""))
    say("  private: no proxy URL; ComfyUI is http://localhost:8188 through podctl tunnel %s, which also names the Jupyter "
        "Terminal (its token made by the boot, on this volume) and each app port. The volume is this machine's (kept across "
        "stop/start, deleted with the pod)." % pod_id)
    if not wait:
        say("  podctl tunnel %s prints the links (ComfyUI, the Jupyter Terminal with its token, each app)" % pod_id)
        return pod_id
    pod = wait_for(api, pod_id, lambda p: p.get("desiredStatus") == "RUNNING" and p.get("publicIp") and (p.get("portMappings") or {}).get("22"),
                   "RUNNING with port 22 mapped", sleep, tries=120)
    host, port = pod["publicIp"], int(pod["portMappings"]["22"])
    for _ in range(120):
        if banner(host, port):
            break
        sleep(5)
    else:
        raise PodctlError("created %s, but sshd never answered on %s:%d: does the image run sshd from PUBLIC_KEY? "
                          "podctl ensure %s wraps its start command" % (pod_id, host, port, pod_id))
    write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, port)
    say("  ssh answers on %s:%d (Host %s -> %s)" % (host, port, _c().SSH_ALIAS, pod_id))
    # 3.9.0 (simple first, then private): the terminal's token is made here and handed to the pod over ssh's stdin, onto
    # the pod volume (never RunPod's env, which it keeps in the clear, never argv), so the links can be printed now
    tok = seed_jupyter_token(say=say)
    apps = [(p, "", n) for p, n in (names or []) if int(p) not in (8188, 8888)]
    for line in _c().tunnel_plan(_c().TUNNEL_PORTS, False, [], apps, tok)[1]:
        say("  " + line)
    say("  (through podctl tunnel %s; they answer once the base is installed and has booted the pod)" % pod_id)
    return pod_id


# ---------------------------------------------------------------- the provider the driver loads

class RunPodProvider(Provider):
    """RunPod behind the host interface. Everything above is reached through it by the driver; the functions stay
    importable on their own for the suite, which drives them against a fake REST server."""
    name = "runpod"
    volume_root = VOLUME_ROOT
    ssh_user = "root"
    ssh_alias = "runpod"
    key_path = pathlib.Path.home() / ".ssh" / "id_ed25519"
    experimental = False
    default_gpus = DEFAULT_GPUS                 # 3.8.0: `create` with no --gpu tries these, in order

    def __init__(self, api=None):
        self.api = api or Api(load_api_key())

    def pods(self):
        return self.api.get("/pods" + POD_READ) or []

    def pod(self, ident):
        return self.api.get("/pods/" + ident + POD_READ)

    def facts(self, pod):
        mapping = pod.get("portMappings") or {}
        status = {"RUNNING": "running", "EXITED": "stopped", "STOPPED": "stopped"}.get(pod.get("desiredStatus"), "transitional")
        return {"id": pod.get("id"), "name": pod.get("name"), "status": status, "host": pod.get("publicIp") or "",
                "port": int(mapping["22"]) if mapping.get("22") else 0, "user": self.ssh_user, "image": pod_image(pod),
                "gpu": (pod.get("machine") or {}).get("gpuTypeId", ""), "volumes": [pod_volume_id(pod)] if pod_volume_id(pod) else []}

    def address(self, pod):
        f = self.facts(pod)
        return Address(f["host"], f["port"], self.ssh_user) if f["host"] and f["port"] else None

    def status_text(self, pod):
        return pod_facts(pod)

    def pods_lines(self):
        out = []
        for p in self.pods():
            m = (p.get("portMappings") or {}).get("22")
            out.append("%-16s %-14s %-44s %-8s %-16s %s" % (p.get("id"), (p.get("name") or "")[:14], pod_image(p)[:44],
                                                             p.get("desiredStatus"), p.get("publicIp") or "-", ("22->%s" % m) if m else "22 unmapped"))
        return out

    def image_text(self, pod):
        cfg = image_config(pod_image(pod))
        return "image      %s\nentrypoint %s\ncmd        %s\nwrap       %s" % (
            pod_image(pod), cfg["entrypoint"], cfg["cmd"], json.dumps(wrap_entry(cfg["entrypoint"], cfg["cmd"], "<sshd bootstrap>")))

    def ensure(self, pod_id, pubkey, ssh_config, log=print, entry=None, **kw):
        return ensure(self.api, pod_id, pubkey=pubkey, ssh_config=ssh_config, bootstrap=sshd_bootstrap_text(), entry=entry, log=log)

    def start(self, pod_id, wait=True, ssh_config=None):
        return start(self.api, pod_id, wait=wait, ssh_config=ssh_config)

    def stop(self, pod_id, wait=True):
        return stop(self.api, pod_id, wait=wait)

    def restart(self, pod_id, wait=True, ssh_config=None):
        return restart(self.api, pod_id, wait=wait, ssh_config=ssh_config)

    def deploy_like(self, like, name=None, gpu=None, wait=True, ssh_config=None, say=print):
        return deploy(self.api, like, name=name, gpu=gpu, wait=wait, ssh_config=ssh_config, say=say)

    def record_volume(self, pod, env=None):
        return record_volume(self.api, pod, env=env)

    # 3.7.3: `podctl create` (the verb Verda already had) and the stock check behind it
    def availability(self, gpu, spot=False):
        """The data centres where `gpu` can be rented now, in RunPod's order (spot shares the on-demand stock view)."""
        return [dc for dc, _ in stock(self.api, gpu)]

    def stock_text(self, gpu):
        rows = stock(self.api, gpu)
        return ", ".join("%s (%s)" % r for r in rows) if rows else "nowhere"

    def volume_location(self, volume_id):
        v = self.api.get("/networkvolumes/%s" % volume_id)
        if not isinstance(v, dict) or not v.get("dataCenterId"):
            raise PodctlError("network volume %s: the API named no data centre (%r)" % (volume_id, v))
        return v["dataCenterId"]

    def create_machine(self, name, gpu, location, data_gb, os_gb=100, image=None, pubkey=None, spot=False, wait=True,
                       ssh_config=None, say=print, volume=None, min_cuda=None, private=False, names=None, name_ports=True):
        if not pubkey:
            pubkey = _c().mac_public_key(None)
        return create(self.api, name, gpu, location, data_gb=data_gb, volume=volume, image=image, container_gb=os_gb, pubkey=pubkey,
                      min_cuda=min_cuda, spot=spot, wait=wait, ssh_config=ssh_config, say=say, private=private, names=names,
                      name_ports=name_ports)

    def http_url(self, pod, port):
        """3.9.0: the URL a browser opens for one of the pod's ports, or None when the host does not proxy it."""
        return proxy_url(pod, port)

    def pod_token(self, pod):
        """3.9.0: the JupyterLab token the pod's own env carries (podctl create sets one per pod), else None."""
        return ((pod.get("env") or {}).get("JUPYTER_TOKEN") or "").strip() or None
