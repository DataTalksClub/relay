import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from scripts.relay_db_preflight_host import run_preflight

ROOT = Path(__file__).resolve().parents[1]


def test_dev_and_prod_deploys_share_non_canceling_concurrency_group():
    workflows = [
        yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        for filename in ("deploy-dev.yml", "deploy-prod.yml")
    ]
    assert [workflow["concurrency"]["group"] for workflow in workflows] == ["relay-deploy", "relay-deploy"]
    assert all(workflow["concurrency"]["cancel-in-progress"] is False for workflow in workflows)


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


@pytest.mark.parametrize("failing_step, label", [("login", "docker login"), ("pull", "docker pull")])
def test_registry_failure_surfaces_exit_code_and_stderr_without_token(failing_step, label, capsys):
    web = {"NetworkSettings": {"Networks": {"coolify": {}}}, "Config": {"Env": ["DATABASE_URL=private"]}}

    def run(args, **kwargs):
        if "inspect" in args:
            return MagicMock(stdout=json.dumps([web]))
        if failing_step in args:
            raise subprocess.CalledProcessError(
                1, args, output="", stderr="Error response from daemon: no space left on device token=s3cr3t-token"
            )
        return MagicMock()

    with pytest.raises(subprocess.CalledProcessError):
        run_preflight("development 20261024-000000-abcdef0", {"username": "test", "token": "s3cr3t-token"}, run=run)
    err = capsys.readouterr().err
    assert f"{label} failed with exit code 1" in err
    assert "no space left on device" in err
    assert "s3cr3t-token" not in err
    assert "[redacted]" in err


@pytest.mark.parametrize(
    "transient_stderr",
    [
        "failed to copy: read tcp [2a02:c207:3021:5::1]:52044->[2606:50c0:8003::154]:443: read: connection reset by peer token=s3cr3t-token",
        "Get \"https://ghcr.io/v2/\": net/http: TLS handshake timeout",
        "dial tcp 140.82.121.34:443: i/o timeout",
        "received unexpected HTTP status: 502 Bad Gateway",
        "failed to copy: unexpected EOF",
    ],
)
def test_docker_pull_retries_transient_errors_then_succeeds_with_redaction(transient_stderr, capsys):
    web = {"NetworkSettings": {"Networks": {"coolify": {}}}, "Config": {"Env": ["DATABASE_URL=private"]}}
    pull_attempts = 0
    sleeps = []

    def run(args, **kwargs):
        nonlocal pull_attempts
        if "inspect" in args:
            return MagicMock(stdout=json.dumps([web]))
        if "pull" in args:
            pull_attempts += 1
            if pull_attempts < 3:
                raise subprocess.CalledProcessError(1, args, output="", stderr=transient_stderr)
        return MagicMock()

    run_preflight(
        "development 20261024-000000-abcdef0",
        {"username": "test", "token": "s3cr3t-token"},
        run=run,
        sleep=sleeps.append,
    )
    assert pull_attempts == 3
    assert sleeps == [5, 15]
    err = capsys.readouterr().err
    assert "docker pull attempt 1/4 failed with exit code 1 (retrying in 5s)" in err
    assert "docker pull attempt 2/4 failed with exit code 1 (retrying in 15s)" in err
    assert "s3cr3t-token" not in err


def test_docker_pull_stops_after_max_attempts_on_persistent_transient_error(capsys):
    web = {"NetworkSettings": {"Networks": {"coolify": {}}}, "Config": {"Env": ["DATABASE_URL=private"]}}
    pull_attempts = 0
    sleeps = []

    def run(args, **kwargs):
        nonlocal pull_attempts
        if "inspect" in args:
            return MagicMock(stdout=json.dumps([web]))
        if "pull" in args:
            pull_attempts += 1
            raise subprocess.CalledProcessError(
                1, args, output="", stderr="read: connection reset by peer token=s3cr3t-token"
            )
        return MagicMock()

    with pytest.raises(subprocess.CalledProcessError):
        run_preflight(
            "development 20261024-000000-abcdef0",
            {"username": "test", "token": "s3cr3t-token"},
            run=run,
            sleep=sleeps.append,
        )
    assert pull_attempts == 4
    assert sleeps == [5, 15, 30]
    err = capsys.readouterr().err
    assert "docker pull failed with exit code 1" in err
    assert "s3cr3t-token" not in err
    assert "[redacted]" in err


@pytest.mark.parametrize(
    "permanent_stderr",
    [
        "Error response from daemon: manifest for ghcr.io/datatalksclub/relay:bad not found: manifest unknown token=s3cr3t-token",
        "Error response from daemon: Head \"https://ghcr.io/v2/datatalksclub/relay/manifests/bad\": unauthorized: authentication required",
        "Error response from daemon: pull access denied for ghcr.io/datatalksclub/relay, repository does not exist or may require 'docker login': denied",
    ],
)
def test_docker_pull_fails_fast_on_non_transient_errors(permanent_stderr, capsys):
    web = {"NetworkSettings": {"Networks": {"coolify": {}}}, "Config": {"Env": ["DATABASE_URL=private"]}}
    pull_attempts = 0
    sleeps = []

    def run(args, **kwargs):
        nonlocal pull_attempts
        if "inspect" in args:
            return MagicMock(stdout=json.dumps([web]))
        if "pull" in args:
            pull_attempts += 1
            raise subprocess.CalledProcessError(1, args, output="", stderr=permanent_stderr)
        return MagicMock()

    with pytest.raises(subprocess.CalledProcessError):
        run_preflight(
            "development 20261024-000000-abcdef0",
            {"username": "test", "token": "s3cr3t-token"},
            run=run,
            sleep=sleeps.append,
        )
    assert pull_attempts == 1
    assert sleeps == []
    err = capsys.readouterr().err
    assert "docker pull failed with exit code 1" in err
    assert "s3cr3t-token" not in err
