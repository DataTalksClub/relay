"""SES and inbound-mail queues hand off to the task system rather than
processing inline, so that work entering through an AWS door is as visible as
work enqueued by Django.
"""

import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from mailing.ingress import get_ingress_config
from mailing.sqs_worker import SqsWorker
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
        "notification_type": "Delivery",
        "message_id": mail_id,
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


def test_ses_ingress_does_not_process_inline(settings, monkeypatch):
    """The drain must not do the work itself.

    Processing inline would leave these notifications absent from the status
    contract, which is the blind spot this design exists to remove.
    """
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"

    def explode(*args, **kwargs):
        raise AssertionError("the ingress drain must enqueue, not process")

    monkeypatch.setattr("mailing.services.ses_webhooks.process_ses_webhook", explode)

    worker = SqsWorker(
        get_ingress_config("ses-webhooks"), client=FakeSqsClient([_message("m1", _ses_payload())])
    )
    assert worker.run_once().deleted == 1


def test_inbound_email_ingress_enqueues(settings):
    settings.SQS_INBOUND_EMAIL_QUEUE_URL = "https://sqs.example/inbound"
    body = {"Records": [{"s3": {"object": {"key": "inbound/x"}}}]}
    client = FakeSqsClient([_message("m1", body)])

    result = SqsWorker(get_ingress_config("inbound-email"), client=client).run_once()

    assert result.deleted == 1
    assert TaskRun.objects.filter(name="process_inbound_email").count() == 1


def test_missing_queue_url_is_a_configuration_error(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = ""
    with pytest.raises(ImproperlyConfigured):
        get_ingress_config("ses-webhooks")


def test_unknown_ingress_worker_is_rejected():
    with pytest.raises(ValueError):
        get_ingress_config("not-a-queue")
