"""Exercise the shared workflow gate with Docker evidence and bounded polling."""

import io
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import coolify_verify as verify
from scripts.relay_db_preflight_host import inspect_runtime

TAG = "20261026-080000-abcdef0"
SHA = "abcdef01234567890123456789012345678901234"
IMAGE = f"ghcr.io/datatalksclub/relay:{TAG}"
BASELINE = {"observed_at": "2026-10-26T08:00:00+00:00", "containers": [{"id": "old"}]}
RUNTIME = {"containers": [
    {"name": name, "id": name, "image": IMAGE, "image_id": "sha256:abc",
     "created": "2026-10-26T08:00:01.123456789Z", "status": "running", "health": health}
    for name, health in [("web", "healthy"), ("worker", None)]
]}
PAYLOAD = {"status": "ready", "image_tag": TAG, "source_sha": SHA}
SERVICE = {"status": "running:unknown", "applications": [
    {"name": name, "image": IMAGE, "status": f"running:{health or 'unknown'}"}
    for name, health in [("web", "healthy"), ("worker", None)]
]}


def test_actual_new_containers_and_public_release_are_required():
    assert verify.runtime_ready(RUNTIME, BASELINE, {"web", "worker"}, IMAGE)[0]
    assert verify.service_ready(SERVICE, IMAGE, PAYLOAD, TAG, SHA)[0]


@pytest.mark.parametrize("change", ["old_id", "old_created", "old_image", "missing_worker", "missing_web", "extra"])
def test_metadata_and_new_public_web_cannot_hide_old_or_missing_workers(change):
    runtime = deepcopy(RUNTIME)
    item = runtime["containers"][1]
    if change == "old_id":
        item["id"] = "old"
    elif change == "old_created":
        item["created"] = "2026-10-26T07:59:59Z"
    elif change == "old_image":
        item["image"] = "ghcr.io/datatalksclub/relay:old"
    elif change == "missing_worker":
        runtime["containers"].pop()
    elif change == "missing_web":
        runtime["containers"].pop(0)
    else:
        runtime["containers"].append({**item, "name": "extra"})
    assert verify.service_ready(SERVICE, IMAGE, PAYLOAD, TAG, SHA)[0]  # stale API can look ready
    assert verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)[0] is False


@pytest.mark.parametrize("state,health", [("exited", None), ("dead", None), ("running", "unhealthy")])
def test_failed_new_worker_fails_even_when_public_web_is_new(state, health):
    runtime = deepcopy(RUNTIME)
    # Snapshot taken long after creation: well past the startup grace.
    runtime["observed_at"] = "2026-10-26T08:10:00+00:00"
    runtime["containers"][1].update(status=state, health=health)
    ready, _, fatal = verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)
    assert not ready and fatal


@pytest.mark.parametrize("state", ["exited", "dead"])
def test_crashed_new_worker_fails_fast_even_inside_startup_grace(state):
    runtime = deepcopy(RUNTIME)
    runtime["observed_at"] = "2026-10-26T08:00:30+00:00"
    runtime["containers"][1].update(status=state, health=None)
    ready, _, fatal = verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)
    assert not ready and fatal


@pytest.mark.parametrize("observed,fatal", [
    ("2026-10-26T08:02:30+00:00", False),   # 149s old: inside start period
    ("2026-10-26T08:04:59+00:00", False),   # just under the 300s grace
    ("2026-10-26T08:05:02+00:00", True),    # still unhealthy after grace
])
def test_unhealthy_new_worker_is_fatal_only_after_startup_grace(observed, fatal):
    # Prod run 45195819190: new workers reported unhealthy for ~2 minutes
    # during the rolling restart and were healthy a minute later.
    runtime = deepcopy(RUNTIME)
    runtime["observed_at"] = observed
    runtime["containers"][1].update(status="running", health="unhealthy")
    ready, reason, actual_fatal = verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)
    assert not ready and actual_fatal == fatal
    assert ("after startup grace" if fatal else "within 300s startup grace") in reason


def test_unhealthy_with_unknown_age_fails_closed():
    runtime = deepcopy(RUNTIME)
    runtime.pop("observed_at", None)
    runtime["containers"][1].update(status="running", health="unhealthy")
    assert verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)[2] is True


def test_starting_unhealthy_does_not_hide_crashed_worker():
    runtime = deepcopy(RUNTIME)
    runtime["observed_at"] = "2026-10-26T08:01:00+00:00"
    runtime["containers"][0].update(health="unhealthy")
    runtime["containers"][1].update(status="exited", health=None)
    assert verify.runtime_ready(runtime, BASELINE, {"web", "worker"}, IMAGE)[2] is True


def test_failed_new_container_is_not_hidden_by_another_old_or_missing_container():
    runtime = deepcopy(RUNTIME)
    runtime["containers"][0]["id"] = "old"
    runtime["containers"][1]["status"] = "exited"
    assert verify.runtime_ready(runtime, BASELINE, {"web", "worker", "missing"}, IMAGE)[2]


@pytest.mark.parametrize("status,fatal", [("starting:unhealthy", False), ("failed", True),
                                         ("exited", True), ("dead", True), ("running:unhealthy", False)])
def test_unfinished_or_failed_service_cannot_pass(status, fatal):
    service = {**SERVICE, "status": status}
    ready, _, actual_fatal = verify.service_ready(service, IMAGE, PAYLOAD, TAG, SHA)
    assert not ready and actual_fatal == fatal


@pytest.mark.parametrize("sha", [None, "", "unknown", "wrong-sha"])
def test_both_baked_identity_fields_must_match(sha):
    assert verify.release_matches({**PAYLOAD, "source_sha": sha}, TAG, SHA)[0] is False


def test_host_inspection_is_scoped_and_does_not_return_environment():
    uuid = "t9uudl5ik40webxrzamrm8qy"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "ps":
            return subprocess.CompletedProcess(command, 0, f"web-{uuid}\nweb-other\nworker-other\n")
        assert command == ["docker", "inspect", f"web-{uuid}"]
        return subprocess.CompletedProcess(command, 0, json.dumps([{
            "Name": f"/web-{uuid}", "Id": "id", "Image": "digest", "Created": "now",
            "Config": {"Image": IMAGE, "Env": ["SECRET_KEY=never-return-this"]},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        }]))

    result = inspect_runtime("development", run)
    assert result["containers"][0]["name"] == "web"
    assert "never-return-this" not in json.dumps(result)
    assert len(calls) == 2


@pytest.fixture
def poll_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp/relay-container-baseline.json").write_text(json.dumps(BASELINE))
    for name, value in {
        "COOLIFY_URL": "https://coolify.test", "COOLIFY_TOKEN": "private-token",
        "COOLIFY_SERVICE_UUID": "resource", "IMAGE_TAG": TAG, "SOURCE_SHA": SHA,
        "IMAGE_REF": "ghcr.io/datatalksclub/relay", "PUBLIC_READY_URL": "https://relay.test/health/ready?existing=1",
        "EXPECTED_SERVICES": '["web", "worker"]',
    }.items():
        monkeypatch.setenv(name, value)
    clock = [0.0]
    monkeypatch.setattr(verify.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(verify.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(verify, "inspect_runtime", lambda **kwargs: RUNTIME)
    return clock


def mock_responses(monkeypatch, public, service=SERVICE):
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        value = service if request.full_url.startswith("https://coolify.test") else public()
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(verify.urllib.request, "urlopen", urlopen)
    return requests


def test_poll_holds_old_release_then_passes_new_release(monkeypatch, poll_environment, capsys):
    values = iter([{**PAYLOAD, "image_tag": "old"}, PAYLOAD])
    requests = mock_responses(monkeypatch, lambda: next(values))
    assert verify.verify(timeout=30) == 0
    assert poll_environment[0] == 5
    public_requests = [r for r in requests if r.full_url.startswith("https://relay.test")]
    assert all("relay_verify=" in r.full_url and "existing=1" in r.full_url for r in public_requests)
    assert all(r.headers["Cache-control"] == "no-cache, no-store" for r in public_requests)
    assert public_requests[0].full_url != public_requests[1].full_url
    assert "elapsed_s=5.0" in capsys.readouterr().out


def test_old_version_fails_at_deadline(monkeypatch, poll_environment, capsys):
    mock_responses(monkeypatch, lambda: {**PAYLOAD, "image_tag": "old"})
    assert verify.verify(timeout=12) == 1
    assert poll_environment[0] == 12
    assert "ready=False" in capsys.readouterr().out


def test_empty_api_application_list_never_passes(monkeypatch, poll_environment):
    mock_responses(monkeypatch, lambda: PAYLOAD, {**SERVICE, "applications": []})
    assert verify.verify(timeout=5) == 1


def test_failed_deployment_fails_fast(monkeypatch, poll_environment):
    mock_responses(monkeypatch, lambda: PAYLOAD, {**SERVICE, "status": "failed"})
    assert verify.verify(timeout=30) == 1
    assert poll_environment[0] == 0


def test_late_success_after_network_request_is_rejected(monkeypatch, poll_environment):
    def public():
        poll_environment[0] = 340
        return PAYLOAD
    mock_responses(monkeypatch, public)
    assert verify.verify(timeout=300) == 1


def test_workers_in_health_start_period_wait_then_pass(monkeypatch, poll_environment, capsys):
    """Replays run 45195819190: transient unhealthy must not fail the deploy."""
    unhealthy_service = {"status": "running:unhealthy", "applications": [
        {**SERVICE["applications"][0]},
        {**SERVICE["applications"][1], "status": "running:unhealthy"},
    ]}
    services = iter([unhealthy_service] * 3 + [SERVICE] * 10)
    mock_responses(monkeypatch, lambda: PAYLOAD)
    monkeypatch.setattr(verify.urllib.request, "urlopen", _urlopen_with(lambda: next(services)))

    def runtime(**kwargs):
        snap = deepcopy(RUNTIME)
        # Host clock advances with the fake monotonic clock; worker created at 08:00:01.
        snap["observed_at"] = f"2026-10-26T08:02:{int(poll_environment[0]):02d}+00:00"
        if poll_environment[0] < 15:
            snap["containers"][1]["health"] = "unhealthy"
        return snap

    monkeypatch.setattr(verify, "inspect_runtime", runtime)
    assert verify.verify(timeout=60) == 0
    out = capsys.readouterr().out
    assert "startup grace" in out and "ready=True" in out
    assert poll_environment[0] == 15


def test_worker_still_unhealthy_after_grace_fails_fast(monkeypatch, poll_environment, capsys):
    monkeypatch.setenv("RELAY_VERIFY_UNHEALTHY_GRACE_SECONDS", "130")
    mock_responses(monkeypatch, lambda: PAYLOAD, {**SERVICE, "status": "running:unhealthy"})

    def runtime(**kwargs):
        snap = deepcopy(RUNTIME)
        snap["observed_at"] = f"2026-10-26T08:02:{int(poll_environment[0]):02d}+00:00"
        snap["containers"][1]["health"] = "unhealthy"
        return snap

    monkeypatch.setattr(verify, "inspect_runtime", runtime)
    assert verify.verify(timeout=600) == 1
    out = capsys.readouterr().out
    assert "failed fast" in out and "after startup grace" in out
    # 08:02:10 is 129s after creation; 08:02:15 is past the 130s grace.
    assert poll_environment[0] == 15


def _urlopen_with(service):
    def urlopen(request, **kwargs):
        value = service() if request.full_url.startswith("https://coolify.test") else PAYLOAD
        return io.BytesIO(json.dumps(value).encode())
    return urlopen


def test_same_tag_failed_replacement_does_not_pass(monkeypatch, poll_environment):
    mock_responses(monkeypatch, lambda: PAYLOAD)
    runtime = deepcopy(RUNTIME)
    runtime["containers"][1]["id"] = "old"
    monkeypatch.setattr(verify, "inspect_runtime", lambda **kwargs: runtime)
    assert verify.verify(timeout=5) == 1


def test_workflows_run_shared_gate_and_snapshot_before_deploy():
    root = Path(__file__).resolve().parents[1]
    for name in ["deploy-dev.yml", "deploy-prod.yml"]:
        source = (root / ".github/workflows" / name).read_text()
        verifier = ".deploy-verifier/scripts" if name == "deploy-prod.yml" else "scripts"
        assert f"run: bash {verifier}/run_runtime_verify.sh" in source
        assert source.index(f"bash {verifier}/run_runtime_verify.sh snapshot") < source.index('"PATCH",')
        assert "def evaluate_coolify" not in source
        assert 'output.write("EXPECTED_SERVICES="' in source
    result = subprocess.run([sys.executable, "-m", "scripts.coolify_verify", "bad-argument"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 1 and "Expected no arguments" in result.stderr


def test_promotion_uses_current_gate_while_preserving_historical_compose():
    root = Path(__file__).resolve().parents[1]
    source = yaml.safe_load((root / ".github/workflows/deploy-prod.yml").read_text())
    checkouts = [step["with"] for step in source["jobs"]["promote"]["steps"]
                 if step.get("uses") == "actions/checkout@v5"]
    assert checkouts[0]["ref"] == "${{ needs.select.outputs.source_sha }}"
    assert checkouts[1] == {"ref": "${{ github.sha }}", "path": ".deploy-verifier", "sparse-checkout": "scripts"}
