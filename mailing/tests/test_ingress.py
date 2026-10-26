"""SES and inbound-mail queues hand off to the task system rather than
processing inline, so that work entering through an AWS door is as visible as
work enqueued by Django.
"""

import json
from pathlib import Path

import pytest
import yaml
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django_tasks_db.models import DBTaskResult

import mailing.management.commands.drain_sqs_ingress as drain_module
from mailing.ingress import INGRESS_WORKER_NAMES, get_ingress_config
from mailing.models import EmailEvent
from mailing.sqs_worker import SqsWorker, WorkerConfig, WorkerResult
from mailing.tasks import process_ses_webhook_event
from mailing.tests.test_ses_webhooks import ses_payload, sns_payload
from taskdeck.models import TaskRun

pytestmark = pytest.mark.django_db


class FakeSqsClient:
    def __init__(self, messages):
        self.messages = messages
        self.deleted_receipts = []

    def receive_message(self, **kwargs):
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted_receipts.append(kwargs["ReceiptHandle"])


def _message(message_id, body):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"receipt-{message_id}",
        "Body": json.dumps(body),
    }


def _ses_payload(mail_id="ses-1"):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "notification_type": "delivery",
        "provider_event_id": f"sns-{mail_id}",
        "ses_message_id": mail_id,
        "received_at": "2026-10-23T12:00:00Z",
    }


def test_ses_ingress_enqueues_a_task_per_message(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"
    client = FakeSqsClient([_message("m1", _ses_payload("a")), _message("m2", _ses_payload("b"))])

    worker = SqsWorker(get_ingress_config("ses-webhooks"), client=client)
    result = worker.run_once()

    assert result.received == 2
    assert result.deleted == 2
    runs = TaskRun.objects.filter(name="process_ses_webhook_event")
    assert runs.count() == 2, "each notification becomes a visible task run"
    queued = DBTaskResult.objects.filter(task_path__endswith="process_ses_webhook_event")
    assert queued.count() == 2
    assert set(queued.values_list("queue_name", flat=True)) == {"provider-events"}


def test_ses_ingress_does_not_process_inline(settings, monkeypatch):
    """The drain must not do the work itself.

    Processing inline would leave these notifications absent from the status
    contract, which is the blind spot this design exists to remove.
    """
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"

    def explode(*args, **kwargs):
        raise AssertionError("the ingress drain must enqueue, not process")

    monkeypatch.setattr("mailing.services.ses_webhooks.process_ses_webhook", explode)

    worker = SqsWorker(get_ingress_config("ses-webhooks"), client=FakeSqsClient([_message("m1", _ses_payload())]))
    assert worker.run_once().deleted == 1


def test_inbound_email_ingress_enqueues(settings):
    settings.SQS_INBOUND_EMAIL_QUEUE_URL = "https://sqs.example/inbound"
    body = {"Records": [{"s3": {"object": {"key": "inbound/x"}}}]}
    client = FakeSqsClient([_message("m1", body)])

    result = SqsWorker(get_ingress_config("inbound-email"), client=client).run_once()

    assert result.deleted == 1
    assert TaskRun.objects.filter(name="process_inbound_email").count() == 1
    inbound = DBTaskResult.objects.filter(task_path__endswith="process_inbound_email")
    assert inbound.count() == 1
    assert inbound.get().queue_name == "provider-events"


def test_missing_queue_url_is_a_configuration_error(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = ""
    with pytest.raises(ImproperlyConfigured):
        get_ingress_config("ses-webhooks")


@pytest.mark.parametrize("raw_sns", [True, False])
def test_ses_ingress_preserves_transport_timestamps_through_task_processing(settings, raw_sns):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"
    payload = sns_payload(ses_payload("Delivery", "ses-1")) if raw_sns else _ses_payload()
    message = _message("m1", payload)
    message["Attributes"] = {
        "SentTimestamp": "1792756799000",
        "ApproximateFirstReceiveTimestamp": "1792756800000",
    }
    result = SqsWorker(get_ingress_config("ses-webhooks"), client=FakeSqsClient([message])).run_once()

    assert result.deleted == 1
    queued = DBTaskResult.objects.get(task_path__endswith="process_ses_webhook_event")
    queued_payload = queued.args_kwargs["args"][0]
    # Processing later must retain the receipt time captured by ingress.
    received_at = queued_payload["received_at"]
    process_ses_webhook_event.func(queued_payload)
    metadata = EmailEvent.objects.get().metadata
    assert metadata["received_at"] == received_at
    assert metadata["sqs_sent_timestamp"] == "1792756799000"
    assert metadata["sqs_approximate_first_receive_timestamp"] == "1792756800000"
    if raw_sns:
        assert metadata["sns_timestamp"] == "2026-05-24T12:00:01Z"
    else:
        assert received_at == payload["received_at"]


def test_ses_ingress_retries_malformed_payload_without_deleting_it(settings, caplog):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"
    client = FakeSqsClient([_message("bad", {"Type": "Notification", "Message": "bad-json"})])

    with caplog.at_level("ERROR"):
        result = SqsWorker(get_ingress_config("ses-webhooks"), client=client).run_once()

    assert result.failed == 1
    assert result.deleted == 0
    assert client.deleted_receipts == []
    assert not DBTaskResult.objects.exists()
    assert "bad" in caplog.text
    assert "SesWebhookError" in caplog.text
    assert "invalid SES notification message" in caplog.text


def test_ses_ingress_processes_and_deletes_delivery_delay_notification(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"
    delay_payload = {
        "Type": "Notification",
        "MessageId": "sns-delay-1",
        "TopicArn": "arn:aws:sns:eu-west-1:387546586013:relay-production-ses-events",
        "Message": json.dumps(
            {
                "eventType": "DeliveryDelay",
                "mail": {
                    "timestamp": "2026-10-24T10:38:18.805Z",
                    "source": "newsletter@example.com",
                    "messageId": "ses-delay-msg-1",
                    "destination": ["recipient@example.com"],
                },
                "deliveryDelay": {
                    "timestamp": "2026-10-24T10:38:18.805Z",
                    "delayType": "TransientCommunicationFailure",
                    "expirationTime": "2026-10-24T18:07:05.177Z",
                    "delayedRecipients": [
                        {
                            "emailAddress": "recipient@example.com",
                            "status": "4.4.1",
                            "diagnosticCode": "smtp; 421 4.4.1 Unable to connect to remote host",
                        }
                    ],
                },
            }
        ),
    }
    client = FakeSqsClient([_message("m-delay", delay_payload)])

    worker = SqsWorker(get_ingress_config("ses-webhooks"), client=client)
    result = worker.run_once()

    assert result.failed == 0
    assert result.deleted == 1
    assert client.deleted_receipts == ["receipt-m-delay"]
    queued = DBTaskResult.objects.filter(task_path__endswith="process_ses_webhook_event")
    assert queued.count() == 1


def test_unknown_ingress_worker_is_rejected():
    with pytest.raises(ValueError):
        get_ingress_config("not-a-queue")


def test_every_active_ingress_worker_has_coolify_consumer():
    """Regression guard: ensure every active SQS ingress queue Relay owns
    (from INGRESS_WORKER_NAMES) has a running consumer in the Coolify stack
    template (deploy/coolify/docker-compose.staging.yml).

    Inbound mail (inbound-email, sandbox-inbound-email) is intentionally disabled
    in production (per docs/production-aux-ownership.md), so it is excluded from
    the required active consumers.
    """
    # Intentional production exceptions per docs/production-aux-ownership.md
    INTENTIONALLY_DISABLED = {"inbound-email", "sandbox-inbound-email"}

    required_workers = set(INGRESS_WORKER_NAMES) - INTENTIONALLY_DISABLED

    compose_path = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "coolify"
        / "docker-compose.staging.yml"
    )
    assert compose_path.exists(), f"compose file {compose_path} not found"

    with compose_path.open() as f:
        compose_data = yaml.safe_load(f)

    services = compose_data.get("services", {})

    drained_workers = set()
    for _service_name, svc in services.items():
        cmd = svc.get("command", [])
        if (
            isinstance(cmd, list)
            and len(cmd) >= 4
            and cmd[0] == "python"
            and cmd[1] == "manage.py"
            and cmd[2] == "drain_sqs_ingress"
        ):
            drained_workers.add(cmd[3])

    missing = required_workers - drained_workers
    assert not missing, (
        f"Active SQS ingress queues missing running consumer in Coolify stack: {missing}. "
        f"Every SQS ingress queue Relay owns must have a consumer in "
        f"deploy/coolify/docker-compose.staging.yml to prevent unconsumed backlogs."
    )



def test_drain_heartbeat_logs_every_n_idle_polls(monkeypatch, capsys):
    """F5: a silent-but-alive drain must be distinguishable from a stuck one."""
    idle_results = [WorkerResult(received=0, deleted=0, failed=0)] * 7
    active_results = [WorkerResult(received=1, deleted=1, failed=0)]

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        def run_forever(self, *, should_stop, idle_sleep=0):
            for result in idle_results[:3] + active_results + idle_results[3:]:
                yield result

    monkeypatch.setattr(drain_module, "SqsWorker", FakeWorker)
    monkeypatch.setattr(
        drain_module,
        "get_ingress_config",
        lambda name: WorkerConfig(name="test-ingress", queue_url="https://sqs.example/test", handler=lambda e, c: {}),
    )

    call_command("drain_sqs_ingress", "ses-webhooks", heartbeat_every=3)

    out = capsys.readouterr().out
    assert "Starting ses-webhooks ingress drain" in out
    assert "received=1 deleted=1 failed=0" in out
    assert out.count("heartbeat ses-webhooks idle_polls=3 received=0 deleted=0 failed=0") == 2
    assert "idle_polls=6" not in out, "counter must reset after an active poll"
    assert "Stopped ses-webhooks ingress drain" in out


def test_drain_once_reports_batch_result(monkeypatch, capsys):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        def run_once(self):
            return WorkerResult(received=2, deleted=2, failed=0)

    monkeypatch.setattr(drain_module, "SqsWorker", FakeWorker)
    monkeypatch.setattr(
        drain_module,
        "get_ingress_config",
        lambda name: WorkerConfig(name="test-ingress", queue_url="https://sqs.example/test", handler=lambda e, c: {}),
    )

    call_command("drain_sqs_ingress", "ses-webhooks", once=True)

    out = capsys.readouterr().out
    assert "received=2 deleted=2 failed=0" in out
