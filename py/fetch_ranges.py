#!/usr/bin/env python3
"""One file over many connections, hashed as it lands (3.25.0). The Civitai leg, and GitHub's for a big file.

    fetch_ranges.py get  <url> <out> --bytes N [--sha256 HEX] [--auth-file F] [--conns 16] [--block-mib 8]
    fetch_ranges.py info <url> [--auth-file F] [--bytes N]        → {"bytes": N, "sha256": "<hex>"|null}

Standard library only: the system Python runs it, so it works before any venv exists and on any image.

  redirects   followed here, not by a library: the auth header (a 0600 file holding "Authorization: Bearer …", never
              argv) goes to the first host only. Civitai answers its download URL with a 307 to a presigned storage URL
              (24 h); the token never reaches that storage, its logs, or this output, which names hosts and never a query.
  ranges      the first request asks for byte 0 only. A 206 gives the total size (Content-Range) and proves ranges: the
              file is then fetched as fixed blocks over up to --conns kept-alive connections, each block written where
              it belongs (pwrite). A 200 means no ranges: that same response is read through, one stream. A storage HEAD
              is refused (presigned for GET only), which is why the size is asked this way.
  sha256      taken from the blocks in memory, in order, as they arrive: nothing is read back from disk. Workers stay
              at most --conns + 4 blocks ahead of the hash, so memory is bounded ((16 + 4) × 8 MiB per file). The expected
              hash is --sha256, else, for a Civitai URL, the one Civitai states for that file (/api/v1/model-versions/<id>:
              the fileId the URL names, else the one file whose sizeKB is exactly the size, else none, and the size stands).
  resume      <out>.ranges records how many blocks are written and hashed; a rerun hashes that prefix from disk once (an
              interrupted run only) and fetches the rest. A size that is not the row's stops before anything is written.

Exit codes: 0 verified and in place; 1 transient (the next attempt resumes); 2 sha256 mismatch (the file is deleted);
3 the source's size is not the row's; 4 refused (401/403: the token, a licence, early access); 5 not found (404).
On success the last line is "RESULT {json}": bytes, seconds, sha256, verified, conns, host.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit

UA = "comfyui-base-fetch/1"
WINDOW_EXTRA = 4


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _host(url):
    return urlsplit(url).hostname or "?"


def _auth_header(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        line = f.readline().strip()
    name, _, value = line.partition(":")
    return (name.strip(), value.strip()) if name and value else None


def _conn(url, timeout=60):
    p = urlsplit(url)
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    return cls(p.hostname, p.port, timeout=timeout)


def _target(url):
    p = urlsplit(url)
    return (p.path or "/") + ("?" + p.query if p.query else "")


def _status_fail(status, host):
    if status in (401, 403):
        return Fail(4, "%s refused the request (%d)" % (host, status))
    if status == 404:
        return Fail(5, "%s has no such file (404)" % host)
    return Fail(1, "%s answered %d" % (host, status))


def open_url(url, auth=None, headers=None, max_hops=10):
    """GET url, following redirects by hand; auth only to the first host. → (final url, connection, response)."""
    origin = _host(url)
    for _ in range(max_hops):
        conn = _conn(url)
        h = {"User-Agent": UA, **(headers or {})}
        if auth and _host(url) == origin:
            h[auth[0]] = auth[1]
        try:
            conn.request("GET", _target(url), headers=h)
            r = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise Fail(1, "%s: %s" % (_host(url), e.__class__.__name__)) from None
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader("Location")
            r.read()
            conn.close()
            if not loc:
                raise Fail(1, "%s redirected without a Location" % _host(url))
            url = urljoin(url, loc)
            continue
        return url, conn, r
    raise Fail(1, "too many redirects from %s" % origin)


def probe(url, auth=None):
    """→ (final url, total bytes, ranged, open 200 response or None, its connection or None)."""
    final, conn, r = open_url(url, auth, {"Range": "bytes=0-0"})
    if r.status == 206:
        m = re.match(r"bytes \d+-\d+/(\d+)$", r.getheader("Content-Range") or "")
        r.read()
        conn.close()
        if not m:
            raise Fail(1, "%s gave no total size in Content-Range" % _host(final))
        return final, int(m.group(1)), True, None, None
    if r.status == 200:
        cl = r.getheader("Content-Length")
        if cl is None or not cl.isdigit():
            r.close(); conn.close()
            raise Fail(1, "%s gave neither ranges nor a Content-Length" % _host(final))
        return final, int(cl), False, r, conn
    r.read(); conn.close()
    raise _status_fail(r.status, _host(final))


# ---------------------------------------------------------------- Civitai: the sha256 it states for the file
_CIVITAI_DL = re.compile(r"^/api/download/models/(\d+)$")


def civitai_sha256(url, size, auth=None):
    p = urlsplit(url)
    m = _CIVITAI_DL.match(p.path)
    if not m:
        return None
    api = "%s://%s/api/v1/model-versions/%s" % (p.scheme, p.netloc, m.group(1))
    try:
        final, conn, r = open_url(api, auth, {"Accept": "application/json"})
        body = r.read()
        conn.close()
        if r.status != 200:
            return None
        files = json.loads(body).get("files") or []
    except (Fail, ValueError, OSError):
        return None
    fid = (parse_qs(p.query).get("fileId") or [None])[0]
    pick = [f for f in files if fid and str(f.get("id")) == fid]
    if not pick and size:
        pick = [f for f in files if round(float(f.get("sizeKB") or 0) * 1024) == size]
    if len(pick) != 1:
        return None
    h = str((pick[0].get("hashes") or {}).get("SHA256") or "").lower()
    return h if re.fullmatch(r"[0-9a-f]{64}", h) else None


# ---------------------------------------------------------------- the transfer
class Ranged:
    def __init__(self, src, auth, final, total, out, block, conns):
        self.src, self.auth, self.final = src, auth, final
        self.total, self.out, self.block = total, out, block
        self.n = max(1, -(-total // block))
        self.conns = max(1, min(conns, self.n))
        self.window = self.conns + WINDOW_EXTRA
        self.cond = threading.Condition()
        self.done = {}
        self.cursor = self.next = 0
        self.error = None
        self.gen = 0                                   # bumped when the presigned URL is asked for again

    def _side(self):
        return self.out + ".ranges"

    def _save(self):
        tmp = self._side() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"total": self.total, "block": self.block, "hashed": self.cursor}, f)
        os.replace(tmp, self._side())

    def _resume(self, h, fd):
        """hash the prefix an interrupted run wrote; → blocks already in place"""
        try:
            with open(self._side()) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return 0
        if s.get("total") != self.total or s.get("block") != self.block or os.fstat(fd).st_size != self.total:
            return 0
        k = max(0, min(int(s.get("hashed") or 0), self.n))
        left = min(k * self.block, self.total)
        with open(self.out, "rb") as f:
            while left > 0:
                b = f.read(min(1 << 24, left))
                if not b:
                    return 0
                h.update(b)
                left -= len(b)
        return k

    def _refresh(self, seen):
        with self.cond:
            if self.gen != seen:
                return
            final, _, _, r, conn = probe(self.src, self.auth)
            if r is not None:
                r.close(); conn.close()
            self.final, self.gen = final, self.gen + 1

    def _fetch(self, i, state):
        a = i * self.block
        b = min(self.total, a + self.block) - 1
        for attempt in range(6):
            final, gen = self.final, self.gen
            try:
                if state.get("conn") is None or state.get("url") != final:
                    if state.get("conn") is not None:
                        state["conn"].close()
                    state["conn"], state["url"] = _conn(final), final
                c = state["conn"]
                c.request("GET", _target(final), headers={"User-Agent": UA, "Range": "bytes=%d-%d" % (a, b)})
                r = c.getresponse()
                data = r.read()
                if r.status == 206 and len(data) == b - a + 1:
                    return data
                if r.status in (401, 403) and attempt < 5:
                    self._refresh(gen)                 # an expired presigned URL: asked for again, once per expiry
                elif r.status in (401, 403, 404):
                    raise _status_fail(r.status, _host(final))
            except Fail:
                raise
            except (OSError, http.client.HTTPException):
                pass
            if state.get("conn") is not None:
                state["conn"].close()
            state["conn"] = None
            time.sleep(min(16, 2 ** attempt))
        raise Fail(1, "%s: block %d of %d failed six times" % (_host(self.final), i + 1, self.n))

    def _worker(self, fd):
        state = {}
        try:
            while True:
                with self.cond:
                    while self.error is None and self.next < self.n and self.next >= self.cursor + self.window:
                        self.cond.wait()
                    if self.error is not None or self.next >= self.n:
                        return
                    i, self.next = self.next, self.next + 1
                data = self._fetch(i, state)
                os.pwrite(fd, data, i * self.block)
                with self.cond:
                    self.done[i] = data
                    self.cond.notify_all()
        except BaseException as e:                     # noqa: BLE001 - every failure reaches the hashing thread
            with self.cond:
                if self.error is None:
                    self.error = e if isinstance(e, Fail) else Fail(1, "%s: %s" % (_host(self.final), e))
                self.cond.notify_all()
        finally:
            if state.get("conn") is not None:
                state["conn"].close()

    def run(self, h):
        fd = os.open(self.out, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            k = self._resume(h, fd)
            if k == 0:
                os.ftruncate(fd, 0)
            os.ftruncate(fd, self.total)
            self.cursor = self.next = k
            ts = [threading.Thread(target=self._worker, args=(fd,), daemon=True) for _ in range(self.conns)]
            for t in ts:
                t.start()
            while self.cursor < self.n:
                with self.cond:
                    while self.error is None and self.cursor not in self.done:
                        self.cond.wait()
                    if self.error is not None:
                        break
                    data = self.done.pop(self.cursor)
                h.update(data)                         # hashlib lets the workers run meanwhile
                with self.cond:
                    self.cursor += 1
                    self.cond.notify_all()
                if self.cursor % 32 == 0:
                    self._save()
            for t in ts:
                t.join()
            if self.error is not None:
                self._save()
                raise self.error
        finally:
            os.close(fd)
        try:
            os.remove(self._side())
        except OSError:
            pass


def stream(r, conn, out, total, h):
    got = 0
    try:
        with open(out, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                h.update(b)
                got += len(b)
    except (OSError, http.client.HTTPException) as e:
        raise Fail(1, "the stream broke after %d of %d bytes (%s)" % (got, total, e.__class__.__name__)) from None
    finally:
        conn.close()
    if got != total:
        raise Fail(1, "the stream ended after %d of %d bytes" % (got, total))


def get(a):
    auth = _auth_header(a.auth_file)
    t0 = time.monotonic()
    final, total, ranged, r, conn = probe(a.url, auth)
    if a.bytes and total != a.bytes:
        if r is not None:
            r.close(); conn.close()
        raise Fail(3, "%s serves %d bytes, the row says %d (the file changed at the source: pin or regenerate the row)"
                   % (_host(final), total, a.bytes))
    want = (a.sha256 or "").lower() or civitai_sha256(a.url, total, auth)
    h = hashlib.sha256()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if ranged:
        job = Ranged(a.url, auth, final, total, a.out, a.block_mib << 20, a.conns)
        job.run(h)
        conns = job.conns
    else:
        stream(r, conn, a.out, total, h)
        conns = 1
    got = h.hexdigest()
    if want and got != want:
        os.remove(a.out)
        raise Fail(2, "sha256 %s is not the %s stated for it: deleted" % (got[:16], want[:16]))
    return {"bytes": total, "seconds": round(time.monotonic() - t0, 3), "sha256": got, "verified": bool(want),
            "conns": conns, "host": _host(final)}


def info(a):
    auth = _auth_header(a.auth_file)
    final, total, _, r, conn = probe(a.url, auth)
    if r is not None:
        r.close(); conn.close()
    return {"bytes": total, "sha256": civitai_sha256(a.url, total, auth), "host": _host(final)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fetch_ranges.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("url"); g.add_argument("out")
    g.add_argument("--bytes", type=int, default=0)
    g.add_argument("--sha256", default="")
    g.add_argument("--auth-file", default="")
    g.add_argument("--conns", type=int, default=16)
    g.add_argument("--block-mib", type=int, default=8)
    i = sub.add_parser("info")
    i.add_argument("url")
    i.add_argument("--auth-file", default="")
    a = ap.parse_args(argv)
    try:
        res = get(a) if a.cmd == "get" else info(a)
    except Fail as e:
        print("  !! %s" % e, flush=True)
        return e.code
    except KeyboardInterrupt:
        return 1
    print("RESULT " + json.dumps(res, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
