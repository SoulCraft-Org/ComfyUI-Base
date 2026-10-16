#!/usr/bin/env python3
"""Every node pack's remote HEAD, asked all at once (3.5.0).

    pack_heads.py [--jobs N] < keys        one "key<TAB>branch<TAB>sha" per remote that answered

A key is a url (its remote's HEAD is asked) or, for a PACKS row naming the branch to track (3.6.1), url#branch (that
branch's tip is asked, whatever the remote publishes as HEAD). The answer carries the key as it was asked.

A pack already at the commit its remote publishes as HEAD is not fetched at all (lib/40-packs.sh). Asking each remote
is one `git ls-remote --symref <url> HEAD`, about a second on the network; asked one after another that was ~33 seconds
before a single pack moved, so they are asked together. A remote that does not answer (offline, private, gone) prints
nothing: the pack then takes the full path, which fetches and reports exactly what git says.
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def head(key):
    url, _, track = key.partition("#")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")           # a private remote must fail, never wait for a password
    ask = [url, "refs/heads/" + track] if track else ["--symref", url, "HEAD"]
    try:
        r = subprocess.run(["git", "ls-remote", *ask], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    branch = sha = ""
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if track:
            if len(parts) == 2 and parts[1] == "refs/heads/" + track:
                branch, sha = track, parts[0]
        elif len(parts) >= 3 and parts[0] == "ref:" and parts[1].startswith("refs/heads/") and parts[2] == "HEAD":
            branch = parts[1][len("refs/heads/"):]
        elif len(parts) == 2 and parts[1] == "HEAD":
            sha = parts[0]
    return (key, branch, sha) if branch and sha else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=16)
    a = ap.parse_args(argv)
    keys = sorted({u.strip() for u in sys.stdin if u.strip()})
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for got in pool.map(head, keys):
            if got:
                print("\t".join(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
