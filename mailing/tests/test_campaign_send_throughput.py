"""Campaign batch send throughput.

A prod campaign sent ~4 msg/s against RELAY_SES_MAX_SEND_RATE=45: each batch
sent its recipients one at a time, and every send held the campaign row lock
across the ~240 ms SES call. These tests use a fake SES client with realistic
latency; nothing leaves the machine.

The concurrency tests need PostgreSQL (row locks, one connection per thread):
    DATABASE_URL=postgresql://... uv run pytest mailing/tests/test_campaign_send_throughput.py
"""

import logging
import threading
import time

import pytest
from botocore.exceptions import ClientError
from django.db import connection
from django.test import override_settings

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    Organization,
)
from mailing.services.campaign_sender import RetryableCampaignSendError, send_campaign_batch

SES_LATENCY_SECONDS = 0.2
needs_postgres = pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL")


class _SlowSes:
    def __init__(self, latency=SES_LATENCY_SECONDS, fail_on=None, error_code="Throttling"):
        self.latency = latency
        self.fail_on = fail_on
        self.error_code = error_code
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def send_email(self, **params):
        return self._send()

    def send_raw_email(self, **params):
        return self._send()

    def _send(self):
        with self._lock:
            self.calls += 1
            number = self.calls
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.latency)
            if self.fail_on and number == self.fail_on:
                raise ClientError({"Error": {"Code": self.error_code}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "SendRawEmail")
            return {"MessageId": f"ses-{number}"}
        finally:
            with self._lock:
                self.in_flight -= 1


def _campaign_with_recipients(count):
    organization = Organization.objects.create(name="Org", slug="org")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status="queued", html_body="<p>Hi</p>", text_body="Hi"
    )
    contacts = [Contact.objects.create(email=f"person{i}@example.com") for i in range(count)]
    recipients = CampaignRecipient.objects.bulk_create(
        [CampaignRecipient(campaign=campaign, contact=c, email=c.email) for c in contacts]
    )
    payload = {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [r.id for r in recipients],
    }
    return campaign, payload


@needs_postgres
@pytest.mark.django_db(transaction=True)
@override_settings(SES_MAX_SEND_RATE_PER_SECOND=45.0, CAMPAIGN_SEND_CONCURRENCY=16)
def test_batch_reaches_configured_rate_with_slow_ses():
    campaign, payload = _campaign_with_recipients(60)
    ses = _SlowSes()

    started = time.monotonic()
    result = send_campaign_batch(payload, ses_client=ses)
    elapsed = time.monotonic() - started

    assert result.sent_count == 60
    assert ses.max_in_flight > 1
    # Sequential sending at 200 ms/call is 5/s; the 45/s limiter caps the best case.
    assert 60 / elapsed >= 30, f"{60 / elapsed:.1f} msg/s"
    campaign.refresh_from_db()
    assert campaign.status == "sent"
    assert campaign.sent_count == 60
    assert CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SENT).count() == 60
    assert CampaignRecipient.objects.filter(campaign=campaign).exclude(ses_message_id="").count() == 60


@needs_postgres
@pytest.mark.django_db(transaction=True)
@override_settings(SES_MAX_SEND_RATE_PER_SECOND=45.0, CAMPAIGN_SEND_CONCURRENCY=8)
def test_two_batches_of_one_campaign_send_in_parallel_without_double_sending():
    campaign, payload = _campaign_with_recipients(40)
    ids = payload["campaign_recipient_ids"]
    first = {**payload, "campaign_recipient_ids": ids[:20]}
    second = {**payload, "campaign_recipient_ids": ids[20:]}
    ses = _SlowSes()
    errors = []

    def run(batch):
        try:
            send_campaign_batch(batch, ses_client=ses)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(batch,)) for batch in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert ses.calls == 40
    assert len(set(CampaignRecipient.objects.values_list("ses_message_id", flat=True))) == 40


@needs_postgres
@pytest.mark.django_db(transaction=True)
@override_settings(SES_MAX_SEND_RATE_PER_SECOND=45.0, CAMPAIGN_SEND_CONCURRENCY=8)
def test_retryable_error_stops_new_sends_keeps_finished_ones_and_reraises():
    campaign, payload = _campaign_with_recipients(40)
    ses = _SlowSes(fail_on=3)

    with pytest.raises(RetryableCampaignSendError):
        send_campaign_batch(payload, ses_client=ses)

    sent = CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SENT).count()
    pending = CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).count()
    assert 0 < sent < 40
    assert sent + pending == 40
    assert CampaignRecipient.objects.filter(campaign=campaign).exclude(last_error="").count() == 1
    campaign.refresh_from_db()
    assert campaign.sent_count == sent
    assert campaign.status == "sending"


@pytest.mark.django_db
@override_settings(SES_MAX_SEND_RATE_PER_SECOND=1000.0, CAMPAIGN_SEND_CONCURRENCY=1)
def test_throughput_far_below_configured_rate_is_logged_as_error(caplog):
    campaign, payload = _campaign_with_recipients(20)

    with caplog.at_level(logging.INFO, logger="mailing.services.campaign_sender"):
        send_campaign_batch(payload, ses_client=_SlowSes(latency=0.03))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "campaign_send_throughput_low" in r.getMessage()]
    assert len(errors) == 1
    assert f"campaign_id={campaign.id}" in errors[0].getMessage()


@pytest.mark.django_db
@override_settings(SES_MAX_SEND_RATE_PER_SECOND=10.0, CAMPAIGN_SEND_CONCURRENCY=1)
def test_throughput_near_configured_rate_logs_info_only(caplog):
    campaign, payload = _campaign_with_recipients(20)

    with caplog.at_level(logging.INFO, logger="mailing.services.campaign_sender"):
        send_campaign_batch(payload, ses_client=_SlowSes(latency=0.03))

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Campaign batch sent" in r.getMessage() for r in caplog.records)
