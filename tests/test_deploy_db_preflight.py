import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from scripts.relay_db_preflight_host import run_preflight

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["deploy-dev.yml", "deploy-prod.yml"])
def test_candidate_preflight_failure_prevents_compose_update_and_deploy(workflow, tmp_path):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = document["jobs"]["deploy" if workflow == "deploy-dev.yml" else "promote"]
    steps = job["steps"]
    preflight = next(i for i, step in enumerate(steps) if step.get("run") == "bash scripts/run_db_preflight.sh")
    switch = next(i for i, step in enumerate(steps) if "Apply Coolify" in step.get("name", ""))
    assert preflight < switch
    assert not steps[preflight].get("continue-on-error")
    assert "if" not in steps[switch]  # Default success() prevents mutation after failure.

    # Execute the actual shell wrapper with a failing remote runner. No later
    # replacement can run in a successful workflow step after this exit code.
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\ncat >/dev/null\nexit 42\n")
    ssh.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/run_db_preflight.sh")],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "PREFLIGHT_SSH_KEY": "test-only",
             "PREFLIGHT_KNOWN_HOSTS": "test-only", "PREFLIGHT_TARGET": "test-only",
             "PREFLIGHT_ENVIRONMENT": "development", "IMAGE_TAG": "20261024-000000-abcdef0",
             "GH_TOKEN": "test-private", "GITHUB_ACTOR": "test"},
        capture_output=True, text=True,
    )
    assert result.returncode == 42
    assert "test-private" not in result.stdout + result.stderr


def test_host_runs_candidate_with_only_db_config_and_no_startup_migration(tmp_path):
    web = {"NetworkSettings": {"Networks": {"coolify": {}}},
           "Config": {"Env": ["DATABASE_URL=private", "SECRET_KEY=private", "AWS_SECRET_ACCESS_KEY=excluded"]}}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return MagicMock(stdout=json.dumps([web]))
        if "run" in args:
            env_file = Path(args[args.index("--env-file") + 1])
            assert env_file.read_text().splitlines() == ["DATABASE_URL=private", "SECRET_KEY=private"]
            assert env_file.stat().st_mode & 0o777 == 0o600
        return MagicMock()

    run_preflight("production 20261024-000000-abcdef0", {"username": "test", "token": "private"}, run=run)
    assert calls[-1][-5:] == ["db_preflight", "--expected-role", "relay_app", "--expected-database", "relay"]
    assert calls[-1][calls[-1].index("--entrypoint") + 1] == "python"
    assert all("stop" not in c and "restart" not in c for c in calls)


@pytest.mark.parametrize("selection", ["production latest", "production x; rm -rf /", "other 20261024-000000-abcdef0"])
def test_forced_command_rejects_arbitrary_commands(selection):
    run = MagicMock()
    with pytest.raises(ValueError):
        run_preflight(selection, {}, run=run)
    run.assert_not_called()
