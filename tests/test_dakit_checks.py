"""dakit CI gates: the vendored kit matches its lock, and relay's styles pass
the kit's border, colour and scale checkers (dakit docs/adoption.md)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "static/dakit"
SCALE_BASELINE = ROOT / "tests/dakit-scale-baseline.json"
# campaign-composer.js builds the email document itself: its inline styles go
# to recipients' mail clients, which have no CSS variables, so literal colours
# and px sizes are correct there. Borders are still checked everywhere.
EMAIL_DOCUMENT_SCRIPTS = {"campaign-composer.js"}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


def app_style_paths():
    scripts = sorted(path for path in (ROOT / "static/mailing/js").glob("*.js") if path.name not in EMAIL_DOCUMENT_SCRIPTS)
    return [ROOT / "templates", ROOT / "static/mailing/css", *scripts]


def run(*args):
    result = subprocess.run(["node", *map(str, args)], capture_output=True, text=True, cwd=ROOT)
    return result.returncode, result.stdout + result.stderr


def test_vendored_dakit_matches_its_lock():
    code, output = run(KIT / "sync.mjs", "--check", "--dest", KIT)
    assert code == 0, output


def test_no_accent_borders():
    code, output = run(KIT / "check-borders.mjs", ROOT / "templates", ROOT / "static/mailing")
    assert code == 0, output


def test_colours_come_from_dakit_roles():
    code, output = run(KIT / "check-colors.mjs", *app_style_paths())
    assert code == 0, output


def test_spacing_and_type_stay_on_the_dakit_scales():
    """New off-grid values fail; the baseline holds existing debt and may only shrink."""
    code, output = run(KIT / "check-scale.mjs", "--baseline", SCALE_BASELINE, *app_style_paths())
    assert code == 0, output


def test_scale_baseline_is_relative_and_bounded():
    baseline = json.loads(SCALE_BASELINE.read_text())
    assert baseline, "an empty baseline means the file can be deleted"
    assert all(not Path(key).is_absolute() for key in baseline)
