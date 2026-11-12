import io
import json
import urllib.error
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from mailing.ingress import get_ingress_config
from mailing.services import aisl_ses_forwarder as forwarder
from mailing.sqs_worker import SqsWorker

TOPIC = "arn:aws:sns:eu-west-1:387546586013:ses-bounces"


@pytest.fixture(autouse=True)
def config(settings, monkeypatch):
    settings.SQS_AISL_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/aisl"
    settings.AISL_SES_WEBHOOK_URL = "https://aishippinglabs.com/api/ses-events"
    settings.AISL_SES_WEBHOOK_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:387546586013:secret:test"
    settings.AISL_SES_TOPIC_ARNS = (TOPIC,)
    monkeypatch.setattr(forwarder, "_secret_cache", None)


def envelope():
    return {
        "Type": "Notification", "TopicArn": TOPIC, "MessageId": "sns-stable-id",
        "Message": json.dumps({"notificationType": "Bounce"}),
        "Signature": "original-signature", "SignatureVersion": "1",
        "SigningCertURL": "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-test.pem",
        "Timestamp": "2026-10-23T12:00:00Z",
    }


def run(monkeypatch, payload=None, status="ok", error=None):
    client = MagicMock()
    client.receive_message.return_value = {"Messages": [{
        "MessageId": "sqs-id", "ReceiptHandle": "receipt", "Body": json.dumps(payload or envelope()),
    }]}
    # SqsWorker deletes via delete_message_batch; the mock answers success.
    client.delete_message_batch.return_value = {"Successful": [{"Id": "msg-0"}], "Failed": []}
    opener = MagicMock()
    if error:
        opener.open.side_effect = error
    else:
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps({"status": status}).encode()
    monkeypatch.setattr(forwarder.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(forwarder, "_shared_secret", lambda: "test-shared-secret")
    result = SqsWorker(get_ingress_config("aisl-ses-webhooks"), client=client).run_once()
    return result, client, opener


@pytest.mark.parametrize("status", ["ok", "duplicate"])
def test_acknowledge_only_after_aisl_accepts_original_signed_envelope(monkeypatch, status):
    result, client, opener = run(monkeypatch, status=status)
    request = opener.open.call_args.args[0]
    assert json.loads(request.data) == envelope()
    assert request.get_header("X-ses-webhook-secret") == "test-shared-secret"
    assert opener.open.call_args.kwargs["timeout"] == 15
    assert result.deleted == 1
    client.delete_message_batch.assert_called_once_with(
        QueueUrl="https://sqs.example/aisl",
        Entries=[{"Id": "msg-0", "ReceiptHandle": "receipt"}],
    )
    client.delete_message.assert_not_called()


@pytest.mark.parametrize("error", [
    TimeoutError(),
    urllib.error.HTTPError("https://aishippinglabs.com/api/ses-events", 403, "Forbidden", {}, io.BytesIO()),
    urllib.error.HTTPError("https://aishippinglabs.com/api/ses-events", 503, "Unavailable", {}, io.BytesIO()),
])
def test_http_failure_retains_message_for_sqs_retry(monkeypatch, error):
    result, client, _ = run(monkeypatch, error=error)
    assert result.failed == 1
    assert result.deleted == 0
    client.delete_message.assert_not_called()


def test_unexpected_200_does_not_drop_event(monkeypatch):
    result, client, _ = run(monkeypatch, status="ignored")
    assert result.failed == 1
    client.delete_message.assert_not_called()


@pytest.mark.parametrize("field,value", [("TopicArn", "arn:unexpected"), ("Type", "SubscriptionConfirmation"), ("MessageId", "")])
def test_unexpected_envelope_never_posts_or_deletes(monkeypatch, field, value):
    payload = envelope() | {field: value}
    result, client, opener = run(monkeypatch, payload=payload)
    assert result.failed == 1
    opener.open.assert_not_called()
    client.delete_message.assert_not_called()


def test_redirect_is_refused_without_leaking_header():
    request = forwarder.urllib.request.Request("https://aishippinglabs.com/api/ses-events")
    assert forwarder._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.example") is None


def test_missing_auth_configuration_fails_before_poll(settings):
    settings.AISL_SES_WEBHOOK_SECRET_ARN = ""
    with pytest.raises(ImproperlyConfigured):
        get_ingress_config("aisl-ses-webhooks")


def test_secret_cache_refreshes_for_rotation(monkeypatch):
    secrets = MagicMock()
    secrets.get_secret_value.side_effect = [{"SecretString": "first"}, {"SecretString": "rotated"}]
    monkeypatch.setattr(forwarder, "aws_client", lambda service: secrets)
    clock = [10]
    monkeypatch.setattr(forwarder.time, "monotonic", lambda: clock[0])
    assert forwarder._shared_secret() == "first"
    assert forwarder._shared_secret() == "first"
    clock[0] += 301
    assert forwarder._shared_secret() == "rotated"
    assert secrets.get_secret_value.call_count == 2


def test_command_standby_does_not_create_a_consumer(settings, monkeypatch):
    settings.AISL_SES_FORWARD_ENABLED = False
    worker = MagicMock()
    monkeypatch.setattr("mailing.management.commands.drain_sqs_ingress.SqsWorker", worker)
    output = io.StringIO()
    call_command("drain_sqs_ingress", "aisl-ses-webhooks", "--once", stdout=output)
    worker.assert_not_called()
    assert "standby" in output.getvalue()
