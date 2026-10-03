"""Runner test package.

Every local-backend preflight takes one advisory lock file per reserved port, in a directory
shared by every run on the machine (``/tmp`` by default; ``oodbench.ports.PORT_LOCK_DIR_ENV``
overrides it). The suite must never write there: it would leave files in the real lock
directory, and two suites running at once on one machine -- or a suite next to a real sweep --
would refuse each other's port blocks. So, before any test module is imported, each test
process points the override at a private directory of its own, removed when the process exits.
pytest and ``unittest discover -t .`` both import this package first. A test that needs a
particular lock directory sets the variable itself.
"""

import atexit
import os
import shutil
import tempfile

_LOCK_DIR = tempfile.mkdtemp(prefix="oodbench-test-port-locks-")
os.environ["OODPB_PORT_LOCK_DIR"] = _LOCK_DIR
atexit.register(shutil.rmtree, _LOCK_DIR, True)
