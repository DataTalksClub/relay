"""Fail CI on accent borders: runs the vendored dakit border checker."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_no_accent_borders():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "static/dakit/check-borders.mjs"),
            str(ROOT / "templates"),
            str(ROOT / "static/mailing"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
