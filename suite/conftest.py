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
    run's temp tree is stopped here, and the test that left it fails, naming it."""
    yield
    bases = {str(tmp_path_factory.getbasetemp()), os.path.realpath(str(tmp_path_factory.getbasetemp()))}
    ps = subprocess.run(["ps", "-ax", "-o", "pid=,args="], capture_output=True, text=True).stdout
    left = [l.strip() for l in ps.splitlines() if "app_proxy.py" in l and any(b in l for b in bases)]
    for l in left:
        subprocess.run(["kill", l.split()[0]], capture_output=True)
    if left:
        pytest.fail("a forwarder outlived the test (stopped now): %s" % "; ".join(left), pytrace=False)
