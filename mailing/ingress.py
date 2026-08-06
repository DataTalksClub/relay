"""Draining the queues AWS writes into directly.

Two queues cannot move onto the in-application task system, because their
producers are AWS services rather than Django: SES delivery notifications
arrive via an SNS subscription, and inbound mail arrives via object-storage
event notifications. Both write to SQS natively.

So SQS stays for these, reduced to a thin ingress adapter: poll, hand the
payload to the same task the rest of the system uses, delete. The work itself
then runs on one execution path, appears in the status contract, and is
retried by one retry policy -- rather than being invisible because it happened
to enter the system through a different door.

This deliberately does not process inline. Doing so would leave these two
workloads absent from the console, which is precisely the blind spot the whole
exercise exists to remove.
"""

import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from mailing import tasks
from mailing.sqs import process_sqs_event
from mailing.sqs_worker import WorkerConfig

logger = logging.getLogger(__name__)

INGRESS_WORKER_NAMES = ("ses-webhooks", "inbound-email")


def _handle_ses_webhook_record(payload, record):
    tasks.process_ses_webhook_event.enqueue(payload)


def _handle_inbound_email_record(payload, record):
    tasks.process_inbound_email.enqueue(payload)


def ses_webhooks_ingress_handler(event, context=None):
    return process_sqs_event(event, _handle_ses_webhook_record)


def inbound_email_ingress_handler(event, context=None):
    return process_sqs_event(event, _handle_inbound_email_record)


def get_ingress_config(name):
    configs = {
        "ses-webhooks": WorkerConfig(
            name="ses-webhooks-ingress",
            queue_url=settings.SQS_SES_WEBHOOKS_QUEUE_URL,
            handler=ses_webhooks_ingress_handler,
        ),
        "inbound-email": WorkerConfig(
            name="inbound-email-ingress",
            queue_url=settings.SQS_INBOUND_EMAIL_QUEUE_URL,
            handler=inbound_email_ingress_handler,
        ),
    }
    if name not in configs:
        raise ValueError(f"unknown ingress worker: {name}")
    config = configs[name]
    if not config.queue_url:
        raise ImproperlyConfigured(f"SQS queue URL is required for the {name} ingress worker.")
    return config
