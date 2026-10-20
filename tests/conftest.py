import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Every test session gets its own throw-away app folder.
_home = tempfile.mkdtemp(prefix="astroflow-test-")
os.environ["ASTROFLOW_HOME"] = _home


@pytest.fixture()
def tmpdir_path(tmp_path: Path) -> Path:
    return tmp_path
