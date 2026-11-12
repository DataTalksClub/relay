import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from mailing.models import EmailEvent
from mailing.services.ses_webhooks import normalize_ses_notification, normalize_ses_webhook_worker_payload
from mailing.tests.test_ses_webhooks import ses_payload, sns_payload

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("timestamp", [None, "", "malformed", "2026-10-23T12:00:00Z"])
def test_sns_timestamp_is_optional_and_preserved_without_changing_processing(timestamp):
    envelope = sns_payload(ses_payload("Delivery", "ses-test"))
    envelope["Timestamp"] = timestamp
    payload = normalize_ses_notification(envelope)

    assert payload["metadata"].get("sns_timestamp") == (timestamp or None)
    assert payload["notification_type"] == "delivery"
    assert normalize_ses_webhook_worker_payload(payload) == payload


def create_event(created_at, timestamp=None, provider="ses"):
    metadata = {"provider": provider}
    if timestamp is not None:
        metadata["sns_timestamp"] = timestamp
    event = EmailEvent.objects.create(event_type="delivered", metadata=metadata)
    EmailEvent.objects.filter(pk=event.pk).update(created_at=created_at)


def report(**options):
    output = StringIO()
    call_command("report_ses_ingress_lag", as_json=True, stdout=output, **options)
    return json.loads(output.getvalue())


def test_report_percentiles_window_and_invalid_samples(monkeypatch):
    now = timezone.now()
    monkeypatch.setattr("mailing.management.commands.report_ses_ingress_lag.timezone.now", lambda: now)
    created_at = now - timedelta(hours=1)
    for seconds in (0, 10, 20, 30, 40):
        # Equivalent non-UTC offsets must also yield correct elapsed seconds.
        timestamp = (created_at - timedelta(seconds=seconds)).astimezone(
            timezone.get_fixed_timezone(120)
        ).isoformat()
        create_event(created_at, timestamp)
    create_event(created_at)  # Old events remain auditable, never backfilled.
    for invalid in ("bad", "2026-99-99T00:00:00Z", "2026-10-23T12:00:00", 123):
        create_event(created_at, invalid)
    create_event(created_at, (created_at + timedelta(seconds=1)).isoformat())
    create_event(now - timedelta(hours=25), (created_at - timedelta(hours=25)).isoformat())
    create_event(now + timedelta(seconds=1), created_at.isoformat())
    create_event(created_at, created_at.isoformat(), provider="tracking")

    result = report()

    assert result == {
        "start": (now - timedelta(hours=24)).isoformat(), "end": now.isoformat(),
        "events": 11, "samples": 5, "missing_timestamp": 1,
        "invalid_timestamp": 4, "negative_lag": 1, "p50_seconds": 20.0, "p95_seconds": 38.0,
    }


def test_report_empty_and_single_sample():
    assert report()["p50_seconds"] is None
    assert report()["p95_seconds"] is None
    created_at = timezone.now() - timedelta(minutes=1)
    create_event(created_at, (created_at - timedelta(seconds=2)).isoformat())
    assert report(hours=1)["p95_seconds"] == 2.0


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf"), 1e100])
def test_report_rejects_invalid_windows(hours):
    with pytest.raises(CommandError):
        report(hours=hours)
