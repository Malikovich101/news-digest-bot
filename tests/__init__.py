"""Keep unit tests isolated from the repository production state file."""

import os
import tempfile

import digest


_TEST_STATE_DIR = tempfile.TemporaryDirectory(prefix="news-digest-tests-")
digest.STATE_FILE = os.path.join(_TEST_STATE_DIR.name, "state.json")
