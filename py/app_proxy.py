#!/usr/bin/env python3
"""The app ports (3.9.0): one small forwarder that serves each customer app at the ROOT of its own port, so a host whose
console links ports, not paths (RunPod's Connect tab), can link an app by name. ComfyUI serves the apps itself, at
/apps/<slug>/ (a pack the package ships; GET /apps lists them); this file only moves bytes between a port and that path.

    app_proxy.py --state <comfy-base/state> [--upstream 127.0.0.1:8188] [--listen 0.0.0.0] [--front 8188]   every row of state/apps.d/*.list
    app_proxy.py --map 8189=<slug> [--map …] [--upstream …] [--listen …] [--front …]                      the rows given here instead

A row is `port|slug|name` (lib/75-boot.sh writes one file per package from its APP_PORTS). On port P a request for
/<path> goes to /apps/<slug>/<path>, except the ComfyUI routes an app calls at the server's root (CORE below: the
websocket, uploads, views, history, queue, interrupt), which go through unchanged. An app reaches those through
new URL("../../", its own URL), and from a port's root "../../" is "/", so the same app works at /apps/<slug>/ on 8188
and at / on its own port. An app must not name a top-level file or folder after a CORE route.

How it forwards, and why it is this small: every connection carries ONE request. The request head is rewritten (the
path, and Connection: close), the response head gets Connection: close, and from then on bytes are piped both ways
until either side closes. A body of any length or transfer coding, a streamed response and a websocket (Upgrade, then
101) all pass through without being parsed. Host and Origin are passed through untouched, so ComfyUI sees what the
browser sent. Standard library only: it runs on whatever python3 the machine has, beside ComfyUI, never on the GPU.
While ComfyUI does not answer yet, a page says so and reloads itself, instead of the browser's connection error.

The key (3.14.0): every link a public bind serves is locked by one per-machine secret, BASE_LINK_TOKEN (the environment,
never argv: `ps` shows argv to every user). The link podctl prints carries it once, `?token=<secret>`; the forwarder
answers that with a cookie (HttpOnly, Secure, SameSite=Lax, this host only) and a redirect to the same URL without it,
so the browser's address bar, history and the app never hold the key, and every later visit, the Connect tab's own
link included, opens straight away. A request with neither gets a plain 401 page. Compared in constant time; the key
is never printed and never reaches ComfyUI (the cookie is taken out of the Cookie header before it is forwarded). A
connection from the machine itself (an ssh tunnel, the install's own checks) is already inside and passes unasked.
JupyterLab is not behind it: it keeps its own token.

ComfyUI's own port is covered the same way. On a public bind ComfyUI listens on 127.0.0.1 only (85-launch.sh), and
`--front PORT` serves PORT on each of the machine's OTHER addresses (the container's eth0 is where a host's proxy
arrives), unlocked by the same key and forwarded unchanged. Loopback keeps ComfyUI itself, so the tunnel, the smoke
and every install check reach it exactly as before. A port that cannot be bound yet (an older ComfyUI still holding
0.0.0.0 until the install restarts it) is tried again every few seconds."""
import argparse, asyncio, glob, hmac, ipaddress, os, re, signal, socket, struct, sys
from urllib.parse import unquote

CORE = ("ws", "upload", "view", "history", "queue", "interrupt")   # the ComfyUI routes an app calls at the server's root
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED = {22, 8188, 8888, 9199, 11434}        # sshd, ComfyUI, JupyterLab, and the two ports podctl tunnel forwards besides
HEAD_MAX = 64 * 1024
CHUNK = 1 << 20                                 # 3.14.0: stream buffer and read size; 64 KiB throttled a 5 MB asset to a fifth (MEASURED)
HOP = {"connection", "keep-alive", "proxy-connection"}
COOKIE = "base_link"                            # the key's cookie: host-only, so each port's link carries the key once
COOKIE_AGE = 30 * 24 * 3600
RETRY = 5                                       # seconds between tries at a port that could not be bound
LOCKED = ("<!doctype html><meta charset=utf-8><title>Locked</title>"
          "<body style=\"font:16px system-ui;margin:3em\"><p>This link needs its key.</p>"
          "<p style=\"color:#666\">Open the full link: <code>podctl status</code> prints it, and on RunPod so does the pod's Logs tab.</p>")
WAITING = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=10><title>Starting</title>"
           "<body style=\"font:16px system-ui;margin:3em\"><p>ComfyUI is starting. This page reloads by itself.</p>")


def parse_row(line):
    """`port|slug|name` → (port, slug, name), or None for a blank or comment line. A bad row raises ValueError."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 3 or not parts[0].isdigit():
        raise ValueError("not port|slug|name: %r" % s)
    port, slug, name = int(parts[0]), parts[1], parts[2]
    if not 1024 <= port <= 65535 or port in RESERVED:
        raise ValueError("port %d is reserved or out of range (1024-65535, never %s)" % (port, sorted(RESERVED)))
    if not SLUG.match(slug):
        raise ValueError("slug %r is not lowercase kebab-case" % slug)
    if not name:
        raise ValueError("row %r has no name" % s)
    return port, slug, name


def read_rows(state):
    """Every row of <state>/apps.d/*.list, in file order. A bad row is reported and skipped; a port claimed twice keeps its first row."""
    rows, seen = [], {}
    for f in sorted(glob.glob(os.path.join(state, "apps.d", "*.list"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = parse_row(line)
                except ValueError as e:
                    print("app_proxy: %s: %s (skipped)" % (os.path.basename(f), e), file=sys.stderr)
                    continue
                if r is None:
                    continue
                if r[0] in seen:
                    print("app_proxy: port %d is claimed by %s and %s: keeping %s" % (r[0], seen[r[0]], r[1], seen[r[0]]), file=sys.stderr)
                    continue
                seen[r[0]] = r[1]
                rows.append(r)
    return rows


def target(path, slug):
    """The upstream request target for a request target on the app's port."""
    p, q, rest = path.partition("?")
    if not p.startswith("/"):
        return path                                          # absolute-form or '*': not ours to rewrite
    first = p[1:].split("/", 1)[0]
    if first in CORE:
        return path
    return "/apps/%s%s%s%s" % (slug, p, q, rest)


def split_token(path):
    """A request target → (the value of its first `token` query parameter or None, the target with every `token` removed).
    The rest of the query is kept byte for byte (no re-encoding), so an app's own parameters arrive exactly as sent."""
    p, q, rest = path.partition("?")
    if not q:
        return None, path
    tok, kept = None, []
    for part in rest.split("&"):
        if part.split("=", 1)[0] == "token":
            if tok is None:
                tok = unquote(part.split("=", 1)[1]) if "=" in part else ""
            continue
        kept.append(part)
    if tok is None:
        return None, path
    kept = [k for k in kept if k]
    return tok, p + ("?" + "&".join(kept) if kept else "")


def same(secret, offered):
    """Constant-time equality of two keys (bytes, so a non-ASCII guess is just wrong, never an error)."""
    return hmac.compare_digest(secret.encode("utf-8"), offered.encode("utf-8", "surrogateescape"))


def cookies(headers, name=COOKIE):
    """Every value the request's Cookie headers give `name`."""
    out = []
    for l in headers:
        k, _, v = l.partition(":")
        if k.strip().lower() != "cookie":
            continue
        for c in v.split(";"):
            n, eq, val = c.strip().partition("=")
            if eq and n == name:
                out.append(val.strip().strip('"'))
    return out


def loopback(peer):
    """True for a connection from the machine itself (127.0.0.0/8, ::1, and their IPv4-mapped forms)."""
    try:
        a = ipaddress.ip_address((peer or "").split("%", 1)[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback


OPEN = object()   # 3.19.1: the secret that opens every link (--open: the pod did not opt into BASE_LINK_LOCK=1)


def authorize(secret, method, path, headers, peer):
    """The key's verdict on one request → ("open", target) | ("set", location) | ("deny", None).

    open: forward it, to `target` (the key taken out of the query when it rode there). set: a GET or HEAD that carried
    the key: answer with the cookie and a redirect to `location`, the same URL without it. deny: the 401 page. With no
    secret at all every request from outside is denied: a public bind is never open for want of a key."""
    if secret is OPEN or loopback(peer):
        return "open", path
    if not secret:
        return "deny", None
    tok, clean = split_token(path)
    if tok is not None and same(secret, tok):
        return ("set", clean) if method in ("GET", "HEAD") else ("open", clean)
    if any(same(secret, c) for c in cookies(headers)):
        return "open", path                            # an app's own ?token= (not the key) passes untouched
    return "deny", None


def _drop_cookie(line, name=COOKIE):
    """A `Cookie:` header line without `name`, or None when nothing else is left."""
    k, _, v = line.partition(":")
    kept = [c.strip() for c in v.split(";") if c.strip() and c.strip().partition("=")[0] != name]
    return "%s: %s" % (k, "; ".join(kept)) if kept else None


def rewrite_head(head, fn):
    """Request or response head bytes → the same head with Connection: close (Upgrade kept), first line through fn,
    and the key's cookie taken out of any Cookie header (ComfyUI and its packs never see the key)."""
    lines = head.decode("latin-1").split("\r\n")
    first, headers = lines[0], [l for l in lines[1:] if l]
    upgrade = any(l.split(":", 1)[0].strip().lower() == "upgrade" for l in headers)
    headers = [l if l.split(":", 1)[0].strip().lower() != "cookie" else _drop_cookie(l) for l in headers]
    kept = [l for l in headers if l is not None and l.split(":", 1)[0].strip().lower() not in HOP]
    kept.append("Connection: Upgrade" if upgrade else "Connection: close")
    return ("\r\n".join([fn(first)] + kept) + "\r\n\r\n").encode("latin-1"), upgrade


async def _read_head(reader):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        return head if len(head) <= HEAD_MAX else None
    except asyncio.LimitOverrunError:
        return None
    except asyncio.IncompleteReadError:
        return None


async def _pipe(reader, writer):
    try:
        while True:
            b = await reader.read(CHUNK)
            if not b:
                break
            writer.write(b)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _simple(writer, status, body, ctype="text/plain; charset=utf-8", extra=(), cache="no-cache"):
    data = body.encode("utf-8")
    more = "".join("%s\r\n" % h for h in extra)
    writer.write(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: %s\r\n%sConnection: close\r\n\r\n"
                  % (status, ctype, len(data), cache, more)).encode("latin-1") + data)


def _unlock(writer, secret, location):
    """The cookie, and a redirect to the URL without the key. The cookie lives on this host only (no Domain), so no
    other pod behind the same proxy domain ever receives it. The key appears in this response's header and nowhere else."""
    _simple(writer, "303 See Other", "", extra=(
        "Location: %s" % location,
        "Set-Cookie: %s=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (COOKIE, secret, COOKIE_AGE),
        "Referrer-Policy: no-referrer"), cache="no-store")


def handler(slug, upstream, secret=None):
    """One port's connection handler. slug None is the front (ComfyUI's own port): the path goes through unchanged."""
    host, port = upstream

    async def handle(creader, cwriter):
        try:
            head = await _read_head(creader)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            m = re.match(r"^(\S+) (\S+) (HTTP/1\.[01])$", first)
            if not m:
                _simple(cwriter, "400 Bad Request", "bad request line\n"); return
            method, path, ver = m.groups()
            peer = (cwriter.get_extra_info("peername") or ("",))[0]
            verdict, path = authorize(secret, method, path, head.decode("latin-1").split("\r\n")[1:], peer)
            if verdict == "deny":
                _simple(cwriter, "401 Unauthorized", LOCKED, "text/html; charset=utf-8", cache="no-store"); return
            if verdict == "set":
                _unlock(cwriter, secret, path); return
            to = target(path, slug) if slug else path
            new, upgrade = rewrite_head(head, lambda _l: "%s %s %s" % (method, to, ver))
            try:
                ureader, uwriter = await asyncio.open_connection(host, port, limit=CHUNK)
            except OSError:
                _simple(cwriter, "503 Service Unavailable", WAITING, "text/html; charset=utf-8"); return
            uwriter.write(new)
            # the request body (if any) and, after a 101, the websocket's client half
            up = asyncio.ensure_future(_pipe(creader, uwriter))
            rhead = await _read_head(ureader)
            if not rhead:
                up.cancel(); uwriter.close(); return
            if upgrade and rhead.startswith(b"HTTP/1.1 101"):
                cwriter.write(rhead)
            else:
                cwriter.write(rewrite_head(rhead, lambda l: l)[0])
            await _pipe(ureader, cwriter)
            up.cancel()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                cwriter.close()
            except Exception:
                pass
    return handle


def front_addrs():
    """This machine's addresses other than loopback, where a host's proxy arrives: IPv4 per interface (SIOCGIFADDR) and
    global IPv6 (/proc/net/if_inet6) on Linux; elsewhere what the host name resolves to. Never 127.x or ::1."""
    out = []
    try:
        import fcntl
        for _i, nm in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", nm[:15].encode()))   # SIOCGIFADDR
                out.append(socket.inet_ntoa(raw[20:24]))
            except OSError:
                pass
            finally:
                s.close()
    except (ImportError, OSError, AttributeError):
        pass
    try:
        with open("/proc/net/if_inet6") as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 6 and f[3] == "00":                        # scope 00: global
                    out.append(str(ipaddress.ip_address(bytes.fromhex(f[0]))))
    except OSError:
        pass
    if not out:
        try:
            out = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP)]
        except OSError:
            out = []
    seen, addrs = set(), []
    for a in out:
        if a not in seen and not loopback(a):
            seen.add(a); addrs.append(a)
    return addrs


def public(listen):
    """True when the bind reaches beyond the machine (anything but loopback): the key is then required."""
    return listen not in ("", "localhost") and not loopback(listen)


async def serve(rows, listen, upstream, say=print, secret=None, front=None, front_listen=None, retry=RETRY):
    """Bind every row (and the front on each non-loopback address); a port that cannot be bound yet is said once and
    tried again every `retry` seconds. Returns the servers bound now; later ones join the same list."""
    servers, jobs = [], []
    for port, slug, name in rows:
        jobs.append((listen, port, handler(slug, upstream, secret),
                     "%s on %s:%d -> %s:%d/apps/%s/" % (name, listen, port, upstream[0], upstream[1], slug), "%s (%s)" % (name, slug)))
    if front:
        for addr in (front_listen if front_listen is not None else front_addrs()):
            jobs.append((addr, front, handler(None, upstream, secret),
                         "ComfyUI on %s:%d -> %s:%d (the key's front)" % (addr, front, upstream[0], upstream[1]), "ComfyUI"))

    async def bind(job, quiet):
        addr, port, h, ok, label = job
        try:
            srv = await asyncio.start_server(h, addr, port, limit=CHUNK, reuse_address=True)
        except OSError as e:
            if not quiet:
                say("app_proxy: %s NOT served on %s:%d yet: %s (trying again every %d s)" % (label, addr, port, e.strerror or e, retry))
            return False
        servers.append(srv)
        say("app_proxy: " + ok)
        return True

    async def again(job):
        while not await bind(job, True):
            await asyncio.sleep(retry)

    for job in jobs:
        if not await bind(job, False) and retry:
            asyncio.ensure_future(again(job))
    return servers


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="comfy-base/state: serve every row of its apps.d/*.list")
    ap.add_argument("--map", action="append", default=[], help="PORT=SLUG (repeatable), instead of --state")
    ap.add_argument("--upstream", default="127.0.0.1:8188", help="where ComfyUI answers (default 127.0.0.1:8188)")
    ap.add_argument("--listen", default="127.0.0.1", help="the bind address (BASE_LISTEN: 0.0.0.0 on RunPod)")
    ap.add_argument("--front", type=int, help="ComfyUI's own port: serve it, behind the key, on each non-loopback address (ComfyUI keeps 127.0.0.1)")
    ap.add_argument("--open", action="store_true", help="every link opens without the key (3.19.1: the default on a pod; "
                    "boot.sh drops it when the pod sets BASE_LINK_LOCK=1)")
    ap.add_argument("--front-listen", action="append", help="the addresses for --front (repeatable); default: every non-loopback one")
    ap.add_argument("--list", action="store_true", help="print the rows it would serve, one `port|slug|name` per line, and exit")
    a = ap.parse_args(argv)
    rows = []
    for m in a.map:
        p, _, s = m.partition("=")
        rows.append(parse_row("%s|%s|%s" % (p, s, s)))
    if a.state:
        rows += read_rows(a.state)
    if a.list:
        for r in rows:
            print("%d|%s|%s" % r)
        return 0
    front = a.front if a.front and public(a.listen) else None   # a loopback bind has no outside to lock out: no front
    if not rows and not front:
        print("app_proxy: no app ports declared: nothing to serve")
        return 0
    secret = OPEN if a.open else (os.environ.get("BASE_LINK_TOKEN", "").strip() or None)
    if public(a.listen):
        print("app_proxy: %s" % ("every link opens on one click (--open; BASE_LINK_LOCK=1 locks them)" if secret is OPEN else
                                 "every link needs the key (BASE_LINK_TOKEN)" if secret else
                                 "no BASE_LINK_TOKEN: every request from outside this machine is refused"), flush=True)
    h, _, p = a.upstream.rpartition(":")
    upstream = (h or "127.0.0.1", int(p))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(serve(rows, a.listen, upstream, say=lambda s: print(s, flush=True), secret=secret,
                                  front=front, front_listen=a.front_listen))
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        loop.run_forever()
    finally:
        for s in servers:
            s.close()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
