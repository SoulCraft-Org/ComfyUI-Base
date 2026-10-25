"""unit tier, 3.25.0: the customer's own downloads, as fast as the source allows.

The promises: a Civitai file (and a big GitHub file) comes over many connections, its sha256 taken from the blocks as they
land, never from a second read; the token goes to civitai.com only, from a 0600 file, never to the storage it redirects to
and never into argv, a URL or a log; a wrong size stops before anything is written, a refusal and a wrong hash are said
once; an interrupted transfer resumes and is never moved into place by its size alone; every file leaves one line in the
run's per-file record; six files download at once on every machine, and the Hub's verify probe is asked once per run."""
import hashlib, http.server, json, os, pathlib, re, subprocess, sys, threading
import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]
HELPER = BASE / "py" / "fetch_ranges.py"
TOKEN = "civ-test-sekrit"
DATA = bytes(range(256)) * (40 * 4096 + 3)              # 40 MiB and 768 bytes: six 8 MiB blocks, the last one short
SHA = hashlib.sha256(DATA).hexdigest()


class _Server:
    """Civitai in miniature: /api/download/models/<id> wants the token and answers 307 to /blob?sig=… (the presigned
    storage); /api/v1/model-versions/<id> states sizeKB and SHA256; /blob serves ranges, or a whole 200 with ranges off.
    Every request is logged with the Authorization header it carried."""

    def __init__(self, data=DATA, sha=SHA, ranges=True, fail_from_block=None, need_token=True):
        self.data, self.sha, self.ranges, self.fail_from, self.need_token, self.log = data, sha, ranges, fail_from_block, need_token, []
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                srv.log.append((self.path, self.headers.get("Authorization"), self.headers.get("Range")))
                if self.path.startswith("/api/download/models/"):
                    if srv.need_token and self.headers.get("Authorization") != "Bearer " + TOKEN:
                        return self._send(401)
                    return self._send(307, headers=[("Location", "/blob?sig=presigned")])
                if self.path.startswith("/api/v1/model-versions/"):
                    body = json.dumps({"files": [{"id": 11, "sizeKB": len(srv.data) / 1024, "hashes": {"SHA256": srv.sha.upper()}}]}).encode()
                    return self._send(200, body)
                if self.path.startswith("/blob"):
                    m = re.match(r"bytes=(\d+)-(\d+)$", self.headers.get("Range") or "")
                    if m and srv.ranges:
                        a, b = int(m.group(1)), int(m.group(2))
                        if srv.fail_from is not None and a >= srv.fail_from * (8 << 20) and a > 0:
                            return self._send(404)
                        body = srv.data[a:b + 1]
                        return self._send(206, body, [("Content-Range", "bytes %d-%d/%d" % (a, a + len(body) - 1, len(srv.data)))])
                    return self._send(200, srv.data)
                return self._send(404)

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):   # a client that hung up mid-body is not the test's business
                pass

        self.httpd = S(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.t.join()

    def blob_auth(self):
        return [a for p, a, _ in self.log if p.startswith("/blob") and a]


@pytest.fixture
def server():
    made = []

    def make(**kw):
        s = _Server(**kw); made.append(s); return s
    yield make
    for s in made:
        s.close()


def _helper(*args):
    return subprocess.run([sys.executable, str(HELPER), *map(str, args)], capture_output=True, text=True, timeout=120)


def _auth(tmp_path):
    f = tmp_path / ".auth"; f.write_text("Authorization: Bearer %s\n" % TOKEN); f.chmod(0o600); return f


def _result(out):
    return json.loads(next(l for l in out.splitlines() if l.startswith("RESULT "))[7:])


# ---------------------------------------------------------------- the helper alone

def test_unit_a_civitai_file_comes_over_many_connections_verified_with_the_token_on_the_first_hop_only(server, tmp_path):
    s = server(); out = tmp_path / "f.safetensors"
    r = _helper("get", s.url + "/api/download/models/7", out, "--bytes", len(DATA), "--auth-file", _auth(tmp_path), "--conns", 4)
    assert r.returncode == 0, r.stdout + r.stderr
    res = _result(r.stdout)
    assert res["verified"] and res["sha256"] == SHA and res["conns"] == 4 and res["bytes"] == len(DATA), res
    assert hashlib.sha256(out.read_bytes()).hexdigest() == SHA and not (tmp_path / "f.safetensors.ranges").exists()
    assert s.blob_auth() == [] and any(a for p, a, _ in s.log if p.startswith("/api/download/"))   # the storage never saw it
    assert TOKEN not in r.stdout + r.stderr and "sig=" not in r.stdout + r.stderr                  # nor the output a URL's query
    ranges = [rg for p, _, rg in s.log if p.startswith("/blob")]
    assert ranges[0] == "bytes=0-0" and len(ranges) == 1 + 6, ranges                                # the size probe, then six blocks


def test_unit_a_wrong_size_a_refusal_and_a_wrong_hash_are_each_said_once(server, tmp_path):
    s = server()
    out = tmp_path / "f.safetensors"
    r = _helper("get", s.url + "/api/download/models/7", out, "--bytes", 5, "--auth-file", _auth(tmp_path))
    assert r.returncode == 3 and "the row says 5" in r.stdout and not out.exists(), r.stdout   # stopped before writing
    r = _helper("get", s.url + "/api/download/models/7", out, "--bytes", len(DATA))
    assert r.returncode == 4 and "(401)" in r.stdout and not out.exists(), r.stdout
    r = _helper("get", s.url + "/api/download/models/7", out, "--auth-file", _auth(tmp_path), "--sha256", "0" * 64)
    assert r.returncode == 2 and "deleted" in r.stdout and not out.exists(), r.stdout
    r = _helper("get", s.url + "/nothing", out)
    assert r.returncode == 5, r.stdout


def test_unit_civitai_s_stated_hash_is_the_one_checked_when_the_row_states_none(server, tmp_path):
    bad = server(sha="ab" * 32); out = tmp_path / "f.safetensors"          # Civitai states a hash these bytes do not have
    r = _helper("get", bad.url + "/api/download/models/7", out, "--bytes", len(DATA), "--auth-file", _auth(tmp_path))
    assert r.returncode == 2 and not out.exists(), r.stdout
    assert any(p.startswith("/api/v1/model-versions/7") for p, _, _ in bad.log)


def test_unit_an_interrupted_transfer_resumes_and_hashes_only_its_prefix_from_disk(server, tmp_path):
    s = server(); out = tmp_path / "f.safetensors"; blk = 8 << 20
    out.write_bytes(DATA[:2 * blk] + b"\0" * (len(DATA) - 2 * blk))      # what a run killed after two blocks leaves
    (tmp_path / "f.safetensors.ranges").write_text(json.dumps({"total": len(DATA), "block": blk, "hashed": 2}))
    r = _helper("get", s.url + "/api/download/models/7", out, "--bytes", len(DATA), "--auth-file", _auth(tmp_path))
    assert r.returncode == 0 and _result(r.stdout)["verified"], r.stdout + r.stderr
    starts = sorted(int(rg.split("=")[1].split("-")[0]) for p, _, rg in s.log if p.startswith("/blob") and rg != "bytes=0-0")
    assert starts == [i * blk for i in range(2, 6)], starts                 # blocks 3 to 6 only
    assert hashlib.sha256(out.read_bytes()).hexdigest() == SHA


def test_unit_a_source_without_ranges_is_read_through_in_one_stream(server, tmp_path):
    s = server(ranges=False, need_token=False); out = tmp_path / "f.safetensors"
    r = _helper("get", s.url + "/api/download/models/7", out, "--bytes", len(DATA))
    assert r.returncode == 0 and _result(r.stdout)["conns"] == 1, r.stdout + r.stderr
    assert hashlib.sha256(out.read_bytes()).hexdigest() == SHA
    assert sum(1 for p, _, _ in s.log if p.startswith("/blob")) == 1     # the probe's own 200 is the transfer


def test_unit_info_names_the_size_and_civitai_s_sha256(server, tmp_path):
    s = server()
    r = _helper("info", s.url + "/api/download/models/7", "--auth-file", _auth(tmp_path))
    assert r.returncode == 0 and _result(r.stdout)["bytes"] == len(DATA) and _result(r.stdout)["sha256"] == SHA, r.stdout


# ---------------------------------------------------------------- the model stage

def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_", "CIVITAI"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", 'source "%s/base.sh"; %s' % (BASE, snippet)], capture_output=True, text=True, env=e)


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text(""); return c


def _fake(tmp_path, origin, **extra):
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "500",
           "BASE_FAKE_DL": "real", "BASE_FAKE_ORIGIN": origin}
    env.update(extra)
    return env


CIV = "loras|SDXL|Style|civ.safetensors|https://civitai.com/api/download/models/7|%d|the buyer's own Civitai file|" % len(DATA)


def test_unit_civitai_rows_name_one_version_and_carry_no_token():
    good = ["https://civitai.com/api/download/models/7", "https://civitai.com/api/download/models/7?fileId=11",
            "https://civitai.com/api/download/models/7?type=Model&format=SafeTensor&size=pruned&fp=fp16"]
    bad = ["https://civitai.com/api/download/models/7?token=abc", "https://civitai.com/api/download/models/7?fileId=1&token=abc",
           "https://civitai.com/models/7", "https://civitai.com/api/download/models/x", "http://civitai.com/api/download/models/7",
           "https://civitai.com.evil.example/api/download/models/7", "https://civitai.com/api/download/models/7?redirect=https://x"]
    for u in good + bad:
        r = _bash('MODELS=("loras|SDXL|Style|a.safetensors|%s|4096||"); _base_model_rows >/dev/null; echo RC=$?' % u)
        assert ("RC=0" in r.stdout) is (u in good), (u, r.stdout, r.stderr)
    r = _bash('MODELS=("loras|SDXL|Style|a.safetensors|https://example.com/a|4096||"); _base_model_rows')
    assert "huggingface.co, github.com, civitai.com" in r.stderr


def test_unit_civitai_always_and_github_from_256_mib_take_the_ranged_leg():
    r = _bash('for a in "https://civitai.com/api/download/models/7 1" "https://github.com/o/r/releases/download/v/a 268435456"'
              ' "https://github.com/o/r/releases/download/v/a 268435455" "https://huggingface.co/o/r/resolve/main/a 999999999999"; do'
              ' set -- $a; _base_ranged "$1" "$2" && echo "$(_base_source "$1") ranged" || echo "$(_base_source "$1") not"; done;'
              ' BASE_RANGE_MIN_MB=1; _base_ranged https://github.com/o/r/raw/c/a 1048576 && echo small-ranged')
    assert r.stdout.split("\n")[:5] == ["civitai ranged", "github ranged", "github not", "hf not", "small-ranged"], r.stdout + r.stderr


def test_unit_a_civitai_row_downloads_verified_and_leaves_its_record(server, tmp_path):
    s = server(); c = _pod(tmp_path)
    r = _bash('MODELS=("%s"); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % CIV,
              env=_fake(tmp_path, s.url, CIVITAI_TOKEN=TOKEN))
    assert "DL=1" in r.stdout and "civitai, ranges x6" in r.stdout, r.stdout + r.stderr
    dest = c / "models/loras/SDXL/Style/civ.safetensors"
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == SHA
    assert TOKEN not in r.stdout + r.stderr and s.blob_auth() == []
    rec = list((tmp_path / "comfy-base/state/logs").glob("*.downloads.tsv"))
    assert len(rec) == 1, rec
    lines = rec[0].read_text().splitlines()
    assert lines[0].startswith("# start\tend\tseconds\tbytes\tMB_s\tsha256_s\tsource\tvia\tresult\tfile")
    row = lines[1].split("\t")
    assert len(lines) == 2 and row[3] == str(len(DATA)) and row[6:] == ["civitai", "ranges x6", "ok", "loras/SDXL/Style/civ.safetensors"], lines
    assert float(row[1]) >= float(row[0]) and float(row[2]) == pytest.approx(float(row[1]) - float(row[0]), abs=0.002)


def test_unit_without_a_token_civitai_s_refusal_is_said_plainly_and_once(server, tmp_path):
    s = server(); _pod(tmp_path)
    r = _bash('MODELS=("%s"); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % CIV, env=_fake(tmp_path, s.url))
    assert "DL=0" in r.stdout and "give the install a CIVITAI_TOKEN" in r.stdout and "download failed" in r.stdout, r.stdout
    assert sum(1 for p, _, _ in s.log if p.startswith("/api/download/")) == 1                      # no second or third attempt
    rec = next((tmp_path / "comfy-base/state/logs").glob("*.downloads.tsv")).read_text().splitlines()
    assert rec[1].split("\t")[6:9] == ["civitai", "ranges", "failed"], rec


def test_unit_a_broken_transfer_is_never_moved_in_by_its_size(server, tmp_path):
    """The ranged file has its full size from the first block (each block lands at its offset), so a size check would
    pass on a transfer that broke halfway. Only the helper's verdict moves it; a failed one stays staged, to resume."""
    s = server(fail_from_block=3); c = _pod(tmp_path)
    r = _bash('MODELS=("%s"); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % CIV,
              env=_fake(tmp_path, s.url, CIVITAI_TOKEN=TOKEN))
    assert "DL=0" in r.stdout and "the next run resumes it" in r.stdout, r.stdout + r.stderr
    assert not (c / "models/loras/SDXL/Style/civ.safetensors").exists()
    staged = [p for p in tmp_path.rglob("civ.safetensors") if ".comfy-base-staging" in str(p)]
    assert staged and staged[0].stat().st_size == len(DATA), staged                               # full size, and still not moved


def test_unit_gen_models_and_gen_hashes_read_civitai(server, tmp_path):
    s = server()
    r = _bash('_base_expected https://civitai.com/api/download/models/7; _base_hub_sha256 https://civitai.com/api/download/models/7',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_ORIGIN": s.url, "CIVITAI_TOKEN": TOKEN})
    assert r.stdout.split() == ["exact:%d" % len(DATA), SHA], r.stdout + r.stderr


# ---------------------------------------------------------------- the queue

def test_unit_six_downloads_at_once_on_every_machine():
    assert _bash('_base_fetch_jobs').stdout.strip() == "6"
    assert _bash('BASE_FETCH_JOBS=2 _base_fetch_jobs').stdout.strip() == "2"
    assert "MemTotal" not in (BASE / "lib/50-models.sh").read_text().split("_base_fetch_jobs(){", 1)[1].split("\n}\n", 1)[0]


def test_unit_the_hub_s_verify_probe_is_asked_once_for_the_whole_queue(tmp_path):
    _pod(tmp_path); bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "hf.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf $*" >> "%s"\n[ "$1" = cache ] && exit 0\n'
                             'f="$3"; d=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
                             'mkdir -p "$d"; python3 -c "import sys; open(sys.argv[1],\'wb\').truncate(4096)" "$d/$f"\n' % log)
    (bin_ / "hf").chmod(0o755)
    rows = " ".join('"vae|Example|Real|%s.safetensors|https://huggingface.co/x/y/resolve/main/%s.safetensors|4096|n|"' % (n, n) for n in "abc")
    r = _bash('MODELS=(%s); base_env_setup; base_discover quiet; base_models; echo DL=${#MODEL_DL[@]}' % rows,
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "500",
                   "BASE_FAKE_DL": "real", "BASE_FETCH_JOBS": "3", "PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    assert "DL=3" in r.stdout and "hf, hf-xet" in r.stdout, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    assert sum("cache verify --help" in l for l in calls) == 1 and sum(l.startswith("hf cache verify x/y") for l in calls) == 3, calls
    rec = next((tmp_path / "comfy-base/state/logs").glob("*.downloads.tsv")).read_text().splitlines()[1:]
    assert sorted(l.split("\t")[-1] for l in rec) == ["vae/Example/Real/%s.safetensors" % n for n in "abc"]
    assert {tuple(l.split("\t")[6:9]) for l in rec} == {("hf", "hf-xet", "ok")}, rec
