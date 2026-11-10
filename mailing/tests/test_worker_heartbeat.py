from pathlib import Path
from types import SimpleNamespace

import yaml

from mailing.management.commands import drain_sqs_ingress
from mailing.services import task_leases


def test_campaign_task_heartbeat_refreshes_container_heartbeat(monkeypatch):

    task_beats = []
    file_beats = []
    monkeypatch.setattr(task_leases, "record_task_heartbeat", lambda *args: task_beats.append(args))
    monkeypatch.setattr(task_leases, "record_db_heartbeat", lambda: file_beats.append(True))

    task_leases.HeartbeatThread("task-1", "worker-1")._beat()

    assert task_beats == [("task-1", "worker-1")]
    assert file_beats == [True]


def test_sqs_ingress_refreshes_heartbeat_on_each_poll(monkeypatch):

    file_beats = []
    monkeypatch.setattr(drain_sqs_ingress.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(drain_sqs_ingress, "get_ingress_config", lambda _name: object())
    monkeypatch.setattr(
        drain_sqs_ingress,
        "record_db_heartbeat",
        lambda: file_beats.append(True),
    )

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_forever(self, **_kwargs):
            yield SimpleNamespace(received=0, deleted=0, failed=0)
            yield SimpleNamespace(received=1, deleted=1, failed=0)

    monkeypatch.setattr(drain_sqs_ingress, "SqsWorker", FakeWorker)

    drain_sqs_ingress.Command().handle(
        worker="ses-webhooks",
        once=False,
        batch_size=10,
        wait_time=20,
        idle_sleep=0,
        heartbeat_every=30,
    )

    # One startup touch and one for each completed poll result.
    assert len(file_beats) == 3


def test_dev_worker_healthchecks_are_shell_file_age_checks():

    compose_path = Path(__file__).parents[2] / "deploy" / "coolify" / "docker-compose.dev.yml"
    services = yaml.safe_load(compose_path.read_text())["services"]
    workers = (
        "worker",
        "campaign-worker",
        "provider-events-worker",
        "provider-events-worker-2",
        "tracking-worker",
        "scheduler",
        "cmp-callbacks",
        "client-callbacks",
        "recipient-imports",
        "inbound-deliveries",
    )

    for name in workers:
        service = services[name]
        healthcheck = service["healthcheck"]
        command = " ".join(healthcheck["test"])
        assert healthcheck["test"][0] == "CMD-SHELL"
        assert "worker_healthcheck" not in command
        assert "stat -c %Y /tmp/relay-db-heartbeat" in command
        assert healthcheck["interval"] == "15s"
        assert healthcheck["retries"] == 1

    assert services["campaign-worker"]["stop_grace_period"] == "40s"
    for name in ("worker", "provider-events-worker", "provider-events-worker-2", "tracking-worker"):
        assert services[name]["stop_grace_period"] == "20s"


def test_production_worker_heartbeats_and_resource_budget():
    services = yaml.safe_load((Path(__file__).parents[2] / "deploy/coolify/docker-compose.staging.yml").read_text())["services"]
    workers = {name: service for name, service in services.items() if name != "web"}
    assert len(workers) == 28
    assert sum(float(service["cpus"]) for service in workers.values()) <= 8
    for name, service in workers.items():
        assert service["mem_limit"] == "512m"
        probe = " ".join(service["healthcheck"]["test"])
        assert "stat -c %Y /tmp/relay-db-heartbeat" in probe
        assert "worker_healthcheck" not in probe
    assert "SQS_INBOUND_EMAIL_QUEUE_URL" in " ".join(workers["inbound-ingress"]["healthcheck"]["test"])
