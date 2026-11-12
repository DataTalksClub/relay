import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_settings(extra_env, code):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
        "SECRET_KEY": "settings-test-key",
        "DATABASE_URL": "postgresql://relay:test@127.0.0.1/relay",
        "PUBLIC_BASE_URL": "http://localhost:8000",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_debug_defaults_off_and_production_debug_is_rejected():
    default = _run_settings({}, "import relay.settings; assert relay.settings.DEBUG is False")
    assert default.returncode == 0, default.stderr

    production_debug = _run_settings(
        {"DEBUG": "1", "PUBLIC_BASE_URL": "https://relay.datatalks.club"},
        "import relay.settings",
    )
    assert production_debug.returncode != 0
    assert "DEBUG must be disabled when PUBLIC_BASE_URL targets Relay production" in production_debug.stderr


def test_web_database_timeouts_are_applied_to_postgres_options():
    result = _run_settings(
        {"RELAY_WEB_DB_LOCK_TIMEOUT_MS": "2000", "RELAY_WEB_DB_STATEMENT_TIMEOUT_MS": "15000"},
        "from relay.settings import DATABASES; print(DATABASES['default']['OPTIONS']['options'])",
    )
    assert result.returncode == 0, result.stderr
    assert "lock_timeout=2000" in result.stdout
    assert "statement_timeout=15000" in result.stdout
