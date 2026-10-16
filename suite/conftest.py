import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / "py"))
# the runner loads the basetest plugin with -p basetest; direct runs: PYTHONPATH=py pytest suite -p basetest


import os, subprocess, shutil, tempfile  # noqa: E402
import pytest  # noqa: E402


# 3.19.0: no test ever reaches the operator's login Keychain. hosts/__init__.py's keychain() is off for the whole run
# (BASE_KEYCHAIN=0, which every subprocess built from os.environ inherits), and a TRIPWIRE `security` is first on PATH: it
# prints nothing, exits 44 and records its argv, and the run fails if anything called it. The tests of the lookup itself
# turn it on (BASE_KEYCHAIN=1) with their own stub first on PATH, never the real binary. Set at import, before any test
# module is collected, so nothing loads a provider first.
os.environ["BASE_KEYCHAIN"] = "0"
KEYCHAIN_TRIPWIRE = pathlib.Path(tempfile.mkdtemp(prefix="base-keychain-tripwire-"))
(KEYCHAIN_TRIPWIRE / "security").write_text('#!/bin/sh\nprintf \'%%s\\n\' "$*" >> "%s/calls"\nexit 44\n' % KEYCHAIN_TRIPWIRE)
(KEYCHAIN_TRIPWIRE / "security").chmod(0o755)
os.environ["PATH"] = str(KEYCHAIN_TRIPWIRE) + os.pathsep + os.environ.get("PATH", "")


def pytest_sessionfinish(session, exitstatus):
    calls = KEYCHAIN_TRIPWIRE / "calls"
    if calls.exists():
        print("\nKEYCHAIN TRIPWIRE: `security` was called during the run (argv per line):\n" + calls.read_text())
        session.exitstatus = 1
    shutil.rmtree(KEYCHAIN_TRIPWIRE, ignore_errors=True)


_KEYCHAIN_STUB = r"""#!/bin/sh
# the suite's `security`: records its argv, answers `-a <account>` from values (ACCOUNT=VALUE lines), else exits 44
# as the real one does for a missing item. A file named hang makes it hang (a keychain dialog nobody answers).
d="$(dirname "$0")"
printf '%s\n' "$*" >> "$d/argv"
[ -f "$d/hang" ] && exec sleep 30
acct=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-a" ] && [ $# -gt 1 ]; then acct="$2"; shift; fi
  shift
done
line="$(grep -m1 "^$acct=" "$d/values" 2>/dev/null)" || exit 44
printf '%s\n' "${line#*=}"
"""


class KeychainStub:
    def __init__(self, d):
        self.d = d

    def serve(self, values):
        """{account: value}: what the stub answers from now on. Any other account exits 44."""
        (self.d / "values").write_text("".join("%s=%s\n" % kv for kv in values.items()))

    def hang(self):
        (self.d / "hang").write_text("")

    def calls(self):
        f = self.d / "argv"
        return f.read_text().splitlines() if f.exists() else []


@pytest.fixture
def keychain_stub(tmp_path, monkeypatch):
    """3.19.0: the Keychain lookup turned ON (BASE_KEYCHAIN=1, sys.platform darwin even on the Linux runner) against a
    stub `security` first on PATH, ahead of the tripwire and of the real binary. It serves nothing until .serve()."""
    d = tmp_path / "keychain-stub"
    d.mkdir()
    (d / "security").write_text(_KEYCHAIN_STUB)
    (d / "security").chmod(0o755)
    monkeypatch.setenv("BASE_KEYCHAIN", "1")
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(sys, "platform", "darwin")
    stub = KeychainStub(d)
    stub.serve({})
    return stub


@pytest.fixture(autouse=True)
def _no_forwarder_outlives_the_test(tmp_path_factory):
    """3.10.1: py/app_proxy.py is started detached (the boot's nohup), so a test that boots a fake pod and does not stop
    it leaves a listener on this machine after the run (3 found on the Mac on 2026-10-16). Every such process under this
    run's temp tree is stopped here, and the test that left it fails, naming it. 3.15.1: one already signalled may still be
    listed for a moment (CI, error log row 78), so a process gets up to 3 s to go before it counts as left."""
    import time
    yield
    bases = {str(tmp_path_factory.getbasetemp()), os.path.realpath(str(tmp_path_factory.getbasetemp()))}

    def listed():
        ps = subprocess.run(["ps", "-ax", "-o", "pid=,stat=,args="], capture_output=True, text=True).stdout
        return [l.strip() for l in ps.splitlines() if "app_proxy.py" in l and any(b in l for b in bases)
                and not l.split()[1].startswith("Z")]                     # a zombie is gone, only not yet reaped
    left, end = listed(), time.monotonic() + 3
    while left and time.monotonic() < end:
        time.sleep(0.05); left = listed()
    for l in left:
        subprocess.run(["kill", l.split()[0]], capture_output=True)
    if left:
        pytest.fail("a forwarder outlived the test (stopped now): %s" % "; ".join(left), pytrace=False)
