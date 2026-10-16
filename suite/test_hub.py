"""unit tier, 3.12.0: the Hub, pinned and checked.

A row's revision is what downloads (resolve/<commit>/… and hf://owner/repo@<commit>; the file leg used to drop it and
fetch main), every Hub file is checked against the hashes the Hub states for that revision (`hf cache verify`) before it
moves into place, an error retrying cannot fix (gated, token refused, not found) is said once and not retried, no token
reaches a process list, a token that can write is named as more than the install needs, and `gen-models --pin` prints a
package's rows pinned to the commits they are at now."""
import json, os, pathlib, re, shutil, stat, subprocess, textwrap, threading, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _bash(snippet, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_"))}
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e)


def _exe(p, body):
    p.write_text("#!/bin/bash\n" + textwrap.dedent(body)); p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _hf(tmp_path, verify_rc=0, fail_with=""):
    """An hf stand-in: logs argv; `download` writes each named file (4096 bytes) under --local-dir, or prints `fail_with`
    and exits 1; `cache verify --help` answers; `cache verify` exits `verify_rc`."""
    b = tmp_path / "bin"; b.mkdir(exist_ok=True); log = tmp_path / "hf.log"
    _exe(b / "hf", f"""\
        echo "hf $*" >> "{log}"
        if [ "$1" = cache ]; then case " $* " in *" --help "*) exit 0;; esac; exit {verify_rc}; fi
        {"echo " + repr(fail_with) + "; exit 1" if fail_with else ":"}
        shift; repo="$1"; shift; d=""; files=()
        while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; --revision) shift;; --*) ;; *) files+=("$1");; esac; shift; done
        [ "${{#files[@]}}" -gt 0 ] || files=(model.safetensors)
        for f in "${{files[@]}}"; do mkdir -p "$d/$(dirname "$f")"; python3 -c "open('$d/$f','wb').truncate(4096)"; done
        """)
    return {"PATH": f"{b}:{os.environ['PATH']}"}, log


def _dl(tmp_path, env, url, extra=""):
    dest = tmp_path / "lib" / "vae" / "x.safetensors"
    return _bash(f'VENV=/nonexistent; BASE_NO_NET=0; STAGING="{tmp_path}/staging"; {extra}'
                 f'_base_download "{url}" "{dest}" 4096; echo "RC=$?"', env=env), dest


def test_unit_the_rows_accept_a_pinned_snapshot_and_refuse_a_branch_after_the_at():
    for url, good in ((f"hf://owner/repo@{COMMIT}", True), ("hf://owner/repo", True), ("hf://owner/repo@main", False),
                      ("hf://owner/repo@0123abc", False), ("hf://owner/repo/sub", False)):
        r = _bash(f'MODELS=("LLM|||Snap/|{url}|4096||"); _base_model_rows; echo "RC=$?"')
        assert ("RC=0" in r.stdout) is good, (url, r.stdout, r.stderr)
        if not good:
            assert "hf://owner/repo@<40-hex commit>" in r.stderr


def test_unit_the_hub_parser_reads_repo_revision_and_path_from_both_forms():
    r = _bash(f'_base_hf_ref https://huggingface.co/o/r/resolve/{COMMIT}/split/vae/a.safetensors; _base_hf_ref hf://o/r@{COMMIT};'
              ' _base_hf_ref hf://o/r; _base_hf_ref https://github.com/o/r/releases/x || echo NOTHUB')
    assert r.stdout.splitlines() == [f"o/r|{COMMIT}|split/vae/a.safetensors", f"o/r|{COMMIT}|", "o/r||", "NOTHUB"], r.stdout


def test_unit_a_pinned_file_row_downloads_that_commit_and_is_checked_before_it_moves(tmp_path):
    env, log = _hf(tmp_path)
    r, dest = _dl(tmp_path, env, f"https://huggingface.co/o/r/resolve/{COMMIT}/vae/x.safetensors")
    calls = log.read_text()
    assert "RC=0" in r.stdout and dest.stat().st_size == 4096, r.stdout + r.stderr
    assert f"hf download o/r vae/x.safetensors --revision {COMMIT} --local-dir" in calls, calls      # the pin is honoured
    assert re.search(rf"hf cache verify o/r --revision {COMMIT} --local-dir \S+", calls), calls      # and checked


def test_unit_a_file_the_hub_hashes_disagree_with_is_deleted_and_fails(tmp_path):
    env, log = _hf(tmp_path, verify_rc=1)
    r, dest = _dl(tmp_path, env, "https://huggingface.co/o/r/resolve/main/vae/x.safetensors")
    assert "RC=1" in r.stdout and not dest.exists(), r.stdout
    assert "does not match the sha256 the Hub states for main" in r.stdout
    assert not (tmp_path / "staging" / "x.safetensors.d").exists()                 # never resumed: a bad file is not a partial


@pytest.mark.parametrize("out,said", [
    ("Error: Access denied. Model 'o/r' requires approval.", "is gated: accept its licence at https://huggingface.co/o/r"),
    ("401 Client Error: Unauthorized for url", "refused HF_TOKEN"),
    ("Error: Revision not found in model 'o/r'.", "the row's pin is wrong"),
    ("Error: File not found in model 'o/r'.", "not on the Hub at the row's revision"),
])
def test_unit_an_error_retrying_cannot_fix_is_said_once_and_not_retried(tmp_path, out, said):
    env, log = _hf(tmp_path, fail_with=out)
    r, dest = _dl(tmp_path, env, "https://huggingface.co/o/r/resolve/main/vae/x.safetensors")
    assert "RC=1" in r.stdout and said in r.stdout, r.stdout
    assert log.read_text().count("hf download") == 1 and "retrying" not in r.stdout


def test_unit_a_transient_error_is_still_retried_three_times(tmp_path):
    env, log = _hf(tmp_path, fail_with="ConnectionError: read timed out")
    r, _ = _dl(tmp_path, env, "https://huggingface.co/o/r/resolve/main/vae/x.safetensors", extra="sleep(){ :; }; ")
    assert "RC=1" in r.stdout and log.read_text().count("hf download") == 3, r.stdout


def test_unit_a_pinned_snapshot_downloads_that_commit_and_every_file_is_checked(tmp_path):
    env, log = _hf(tmp_path)
    dest = tmp_path / "LLM" / "Snap"
    r = _bash(f'VENV=/nonexistent; BASE_NO_NET=0; _base_snapshot hf://o/r@{COMMIT} "{dest}" 4096; echo "RC=$?"', env=env)
    calls = log.read_text()
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert f"hf download o/r --revision {COMMIT} --local-dir {dest}" in calls
    assert f"hf cache verify o/r --revision {COMMIT} --local-dir {dest} --fail-on-missing-files" in calls, calls


def test_unit_a_snapshot_that_fails_the_hub_hashes_fails_and_says_so(tmp_path):
    env, log = _hf(tmp_path, verify_rc=1)
    r = _bash(f'VENV=/nonexistent; BASE_NO_NET=0; _base_snapshot hf://o/r "{tmp_path / "S"}" 4096; echo "RC=$?"', env=env)
    assert "RC=1" in r.stdout and "does not match the hashes the Hub states for main" in r.stdout, r.stdout


def test_unit_the_size_and_sha256_heads_keep_the_token_off_the_process_list(tmp_path):
    b = tmp_path / "bin"; b.mkdir(); log = tmp_path / "curl.log"
    _exe(b / "curl", f"""\
        echo "curl $*" >> "{log}"
        for a in "$@"; do case "$a" in @*) echo "file ${{a#@}} $(python3 -c 'import os,sys; print(format(os.stat(sys.argv[1]).st_mode & 0o777, "o"))' "${{a#@}}")" >> "{log}";; esac; done
        printf 'HTTP/2 200\\r\\nx-linked-size: 4096\\r\\nx-linked-etag: "{"a" * 64}"\\r\\n\\r\\n'
        """)
    r = _bash('_base_expected https://huggingface.co/o/r/resolve/main/x.safetensors; _base_hub_sha256 https://huggingface.co/o/r/resolve/main/x.safetensors',
              env={"PATH": f"{b}:{os.environ['PATH']}", "HF_TOKEN": "hf_secret_value"})
    calls = log.read_text()
    assert "exact:4096" in r.stdout and "a" * 64 in r.stdout, r.stdout + r.stderr
    assert "hf_secret_value" not in calls and calls.count("-H @") == 2, calls
    assert all(l.endswith(" 600") for l in calls.splitlines() if l.startswith("file ")), calls


def test_unit_gen_models_pin_prints_every_hub_row_at_its_commit_and_leaves_the_rest(tmp_path):
    if not STUB.exists():
        pytest.skip("_build/stub is repo tooling, not in the shipped base")
    pkg = tmp_path / "stub"; shutil.copytree(STUB, pkg)
    s = pkg / "stub-script.sh"
    s.write_text(s.read_text().replace(' "loras|SDXL|Detailers|stub_local.safetensors|LOCAL|0|a file that only exists on the pod|"',
                                       ' "loras|SDXL|Detailers|stub_local.safetensors|LOCAL|0|a file that only exists on the pod|"\n'
                                       ' "LLM|||Snap/|hf://o/snap|4096|a snapshot|"'))
    b = tmp_path / "bin"; b.mkdir(); log = tmp_path / "curl.log"
    _exe(b / "curl", f"""\
        echo "curl $*" >> "{log}"
        case "$*" in
          */api/models/o/snap/revision/main*) echo '{{"sha": "{COMMIT}"}}';;
          *) printf 'HTTP/2 302\\r\\nx-repo-commit: {COMMIT}\\r\\nlocation: https://cas-bridge.xethub.hf.co/x\\r\\n\\r\\n'
             printf 'HTTP/2 200\\r\\nx-linked-size: 4096\\r\\n\\r\\n';;
        esac
        """)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "HF_"))}
    e.update({"PATH": f"{b}:{os.environ['PATH']}", "COMFY_BASE": str(BASE), "HF_TOKEN": "hf_secret_value"})
    r = subprocess.run(["bash", str(BASE / "base.sh"), "gen-models", str(pkg), "--pin"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"|https://huggingface.co/x/y/resolve/{COMMIT}/stub_vae.safetensors|4096|" in r.stdout
    assert f"|hf://o/snap@{COMMIT}|4096|" in r.stdout and "|LOCAL|0|" in r.stdout
    calls = log.read_text()
    assert "hf_secret_value" not in calls
    for f in re.findall(r"-H @(\S+)", calls):
        assert not pathlib.Path(f).exists(), "the auth file outlived the run: " + f
    r2 = subprocess.run(["bash", str(BASE / "base.sh"), "gen-models", str(pkg), "--pinned"], capture_output=True, text=True, env=e)
    assert r2.returncode != 0 and "[--pin]" in r2.stderr


@pytest.mark.parametrize("role,warned", [("write", True), ("read", False), ("fineGrained", False)])
def test_unit_a_token_that_can_write_is_named_as_more_than_the_install_needs(tmp_path, role, warned):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    body = json.dumps({"name": "someone", "auth": {"type": "access_token", "accessToken": {"displayName": "t", "role": role}}}).encode()
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = _bash('export HF_TOKEN=hf_secret_value; _base_token_validate HF_TOKEN env; ls -A "$BASE_TMPD"; rm -rf "$BASE_TMPD"',
                  env={"BASE_TOKEN_CHECK_URL_HF": "http://127.0.0.1:%d/whoami-v2" % srv.server_address[1]})
    finally:
        srv.shutdown(); srv.server_close()
    assert "accepted" in r.stdout and f"(role: {role})" in r.stdout, r.stdout + r.stderr
    assert ("can WRITE" in r.stdout) is warned
    assert "someone" not in r.stdout and "hf_secret_value" not in r.stdout + r.stderr          # the account is nobody's business
    assert ".whoami-" not in r.stdout                                                           # the answer is not kept


def test_unit_stamp_models_links_a_pinned_snapshot_to_its_tree():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stamp_models", BASE / "py" / "stamp_models.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    text = f'MODELS=(\n "LLM|||Snap/|hf://o/r@{COMMIT}|4096||"\n "LLM|||Main/|hf://o/m|4096||"\n)\n'
    urls = {e["name"]: e["url"] for e in mod.entries(text)}
    assert urls == {"Snap": f"https://huggingface.co/o/r/tree/{COMMIT}", "Main": "https://huggingface.co/o/m"}


# ---------------------------------------------------------------- hashed once (3.12.0): the record, and a pin that moves
OLD = "f" * 40
ETAG = "e" * 64


def test_unit_a_download_records_its_commit_and_etag_from_hfs_own_metadata(tmp_path):
    b = tmp_path / "bin"; b.mkdir(); log = tmp_path / "hf.log"
    _exe(b / "hf", f"""\
        echo "hf $*" >> "{log}"
        if [ "$1" = cache ]; then exit 0; fi
        d=""; f="$3"; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done
        mkdir -p "$d/$(dirname "$f")" "$d/.cache/huggingface/download/$(dirname "$f")"
        python3 -c "open('$d/$f','wb').truncate(4096)"
        printf '{COMMIT}\\n{ETAG}\\n1.0\\n' > "$d/.cache/huggingface/download/$f.metadata"
        """)
    state = tmp_path / "state"
    r, dest = _dl(tmp_path, {"PATH": f"{b}:{os.environ['PATH']}"}, "https://huggingface.co/o/r/resolve/main/vae/x.safetensors",
                  extra=f'BASE_STATE="{state}"; ')
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert (state / "verified.tsv").read_text() == f"{dest}\t{COMMIT}\t{ETAG}\n"        # the commit main resolved to, not "main"


def _moved(tmp_path, record, url, hub_etag=ETAG):
    b = tmp_path / "bin"; b.mkdir(parents=True, exist_ok=True); log = tmp_path / "curl.log"
    _exe(b / "curl", f"""\
        echo "curl $*" >> "{log}"
        printf 'HTTP/2 200\\r\\nx-linked-size: 4096\\r\\nx-linked-etag: "{hub_etag}"\\r\\n\\r\\n'
        """)
    state = tmp_path / "state"; state.mkdir(parents=True, exist_ok=True)
    if record:
        (state / "verified.tsv").write_text("/lib/x.safetensors\t%s\t%s\n" % record)
    r = _bash(f'BASE_STATE="{state}"; BASE_NO_NET=0; if _base_pin_moved /lib/x.safetensors "{url}"; then echo MOVED; else echo KEPT; fi',
              env={"PATH": f"{b}:{os.environ['PATH']}"})
    calls = log.read_text() if log.exists() else ""
    return r.stdout.strip().splitlines()[-1], calls, (state / "verified.tsv").read_text() if record else ""


def test_unit_a_file_in_place_is_never_hashed_or_asked_about_unless_its_pin_moved(tmp_path):
    pinned = f"https://huggingface.co/o/r/resolve/{COMMIT}/x.safetensors"
    assert _moved(tmp_path / "a", None, pinned)[:2] == ("KEPT", "")                        # never recorded: the size is the check
    assert _moved(tmp_path / "b", (COMMIT, ETAG), pinned)[:2] == ("KEPT", "")              # verified at this very commit
    assert _moved(tmp_path / "c", (OLD, ETAG), "https://huggingface.co/o/r/resolve/main/x.safetensors")[:2] == ("KEPT", "")  # unpinned


def test_unit_a_moved_pin_with_the_same_file_costs_one_head_and_updates_the_record(tmp_path):
    verdict, calls, rec = _moved(tmp_path, (OLD, ETAG), f"https://huggingface.co/o/r/resolve/{COMMIT}/x.safetensors")
    assert verdict == "KEPT" and calls.count("curl ") == 1, calls
    assert rec.splitlines()[-1] == f"/lib/x.safetensors\t{COMMIT}\t{ETAG}"                 # no HEAD next time


def test_unit_a_moved_pin_whose_file_differs_is_fetched_again(tmp_path):
    verdict, calls, _ = _moved(tmp_path, (OLD, ETAG), f"https://huggingface.co/o/r/resolve/{COMMIT}/x.safetensors", hub_etag="d" * 64)
    assert verdict == "MOVED"
    verdict, calls, _ = _moved(tmp_path / "s", (OLD, "-"), f"hf://o/r@{COMMIT}")
    assert verdict == "MOVED" and calls == ""                                                # a snapshot: hf fetches what changed


def test_unit_base_models_refetches_a_file_whose_pin_moved_and_keeps_one_whose_did_not(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    env, log = _hf(tmp_path)
    b = tmp_path / "bin"; clog = tmp_path / "curl.log"
    _exe(b / "curl", f"""\
        echo "curl $*" >> "{clog}"
        case "$*" in *resolve/{COMMIT}/a.safetensors*) e="{"d" * 64}";; *) e="{ETAG}";; esac
        printf 'HTTP/2 200\\r\\nx-linked-size: 4096\\r\\nx-linked-etag: "%s"\\r\\n\\r\\n' "$e"
        """)
    rows = " ".join('"vae|Example|Real|%s.safetensors|https://huggingface.co/x/y/resolve/%s/%s.safetensors|4096|n|"' % (n, COMMIT, n) for n in "ab")
    env.update({"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_DL": "real"})
    r = _bash(f'MODELS=({rows}); base_env_setup; base_discover quiet; d="$M/vae/Example/Real"; mkdir -p "$d" "$BASE_STATE";'
              f' for n in a b; do python3 -c "open(\'$d/$n.safetensors\',\'wb\').truncate(4096)";'
              f' printf "%s\\t{OLD}\\t{ETAG}\\n" "$d/$n.safetensors" >> "$BASE_STATE/verified.tsv"; done;'
              f' BASE_NO_NET=0 base_models; echo DL=${{#MODEL_DL[@]}} OK=${{#MODEL_OK[@]}}', env=env)
    out = r.stdout
    assert "a.safetensors: its row now pins a commit whose file differs" in out and "DL=1 OK=1" in out, out + r.stderr
    assert f"hf download x/y a.safetensors --revision {COMMIT}" in log.read_text() and "b.safetensors" not in log.read_text()
