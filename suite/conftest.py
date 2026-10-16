import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / "py"))
# the runner loads the basetest plugin with -p basetest; direct runs: PYTHONPATH=py pytest suite -p basetest


import os, subprocess  # noqa: E402
import pytest  # noqa: E402


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
