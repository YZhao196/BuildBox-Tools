"""Wire the two libraries together for the repository's own tests.

`buildbox_management` does not depend on `buildbox`, and `pip install
buildbox-management` does not bring it in. This suite is the one place they
meet: the device package is on the path only so the end-to-end test can drive a
real client against a real receiver over a real socket.

If someone installs this package on its own and runs the suite, the device is
absent and that one test reports itself skipped rather than quietly passing.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEVICE_PACKAGE = Path(__file__).resolve().parents[2] / "python"

if DEVICE_PACKAGE.is_dir() and str(DEVICE_PACKAGE) not in sys.path:
    sys.path.insert(0, str(DEVICE_PACKAGE))
