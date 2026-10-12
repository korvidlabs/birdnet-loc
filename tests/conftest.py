"""Test-wide environment: the package reads MAP_DIR/DATA_DIR at import time.

`birdnet_loc.web` builds its App (and therefore loads cameras.yaml and opens the
SQLite file) on import, so these must be set before any test imports it.
"""

import os
import pathlib
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

os.environ.setdefault("MAP_DIR", str(REPO / "example" / "map"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="birdnet-loc-test-"))
os.environ.setdefault("BANDS_YAML", str(REPO / "bands.yaml"))

# The floor the reference deployment ships (`CONFIRM_MIN_CONF=0.75`). The suite is
# written floor-relative, so this pins the tests to the number that actually
# ships rather than to `Config`'s older default.
os.environ.setdefault("CONFIRM_MIN_CONF", "0.75")
