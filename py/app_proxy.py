#!/usr/bin/env python3
"""The app ports (3.9.0): one small forwarder that serves each customer app at the ROOT of its own port, so a host whose
console links ports, not paths (RunPod's Connect tab), can link an app by name. ComfyUI serves the apps itself, at
/apps/<slug>/ (a pack the package ships; GET /apps lists them); this file only moves bytes between a port and that path.

    app_proxy.py --state <comfy-base/state> [--upstream 127.0.0.1:8188] [--listen 0.0.0.0]   every row of state/apps.d/*.list
    app_proxy.py --map 8189=<slug> [--map …] [--upstream …] [--listen …]                      the rows given here instead

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
While ComfyUI does not answer yet, a page says so and reloads itself, instead of the browser's connection error."""
import argparse, asyncio, glob, os, re, signal, sys

CORE = ("ws", "upload", "view", "history", "queue", "interrupt")   # the ComfyUI routes an app calls at the server's root
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED = {22, 8188, 8888, 9199, 11434}        # sshd, ComfyUI, JupyterLab, and the two ports podctl tunnel forwards besides
HEAD_MAX = 64 * 1024
HOP = {"connection", "keep-alive", "proxy-connection"}
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


def rewrite_head(head, fn):
    """Request or response head bytes → the same head with Connection: close (Upgrade kept), first line through fn."""
    lines = head.decode("latin-1").split("\r\n")
    first, headers = lines[0], [l for l in lines[1:] if l]
    upgrade = any(l.split(":", 1)[0].strip().lower() == "upgrade" for l in headers)
    kept = [l for l in headers if l.split(":", 1)[0].strip().lower() not in HOP]
    kept.append("Connection: Upgrade" if upgrade else "Connection: close")
    return ("\r\n".join([fn(first)] + kept) + "\r\n\r\n").encode("latin-1"), upgrade


async def _read_head(reader):
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        return None
    except asyncio.IncompleteReadError:
        return None


async def _pipe(reader, writer):
    try:
        while True:
            b = await reader.read(65536)
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


def _simple(writer, status, body, ctype="text/plain; charset=utf-8"):
    data = body.encode("utf-8")
    writer.write(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"
                  % (status, ctype, len(data))).encode("latin-1") + data)


def handler(slug, upstream):
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
            new, upgrade = rewrite_head(head, lambda _l: "%s %s %s" % (method, target(path, slug), ver))
            try:
                ureader, uwriter = await asyncio.open_connection(host, port, limit=HEAD_MAX)
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


async def serve(rows, listen, upstream, say=print):
    servers = []
    for port, slug, name in rows:
        try:
            srv = await asyncio.start_server(handler(slug, upstream), listen, port, limit=HEAD_MAX, reuse_address=True)
        except OSError as e:
            say("app_proxy: %s (%s) NOT served on %s:%d: %s" % (name, slug, listen, port, e.strerror or e))
            continue
        servers.append(srv)
        say("app_proxy: %s on %s:%d -> %s:%d/apps/%s/" % (name, listen, port, upstream[0], upstream[1], slug))
    return servers


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="comfy-base/state: serve every row of its apps.d/*.list")
    ap.add_argument("--map", action="append", default=[], help="PORT=SLUG (repeatable), instead of --state")
    ap.add_argument("--upstream", default="127.0.0.1:8188", help="where ComfyUI answers (default 127.0.0.1:8188)")
    ap.add_argument("--listen", default="127.0.0.1", help="the bind address (BASE_LISTEN: 0.0.0.0 on RunPod)")
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
    if not rows:
        print("app_proxy: no app ports declared: nothing to serve")
        return 0
    h, _, p = a.upstream.rpartition(":")
    upstream = (h or "127.0.0.1", int(p))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    servers = loop.run_until_complete(serve(rows, a.listen, upstream, say=lambda s: print(s, flush=True)))
    if not servers:
        print("app_proxy: no port could be bound", flush=True)
        return 1
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
