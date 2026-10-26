"""Async request-path proof: slow work leaves the request immediately.

Covers acceptance (a)/(c)/(e): campaign queue snapshot moves to the
db_worker `default` lane, the request returns in SNAPSHOTTING with progress
visible on the campaign, retries are idempotent, and the worker completes
the snapshot without double-enqueueing.
"""

import importlib.util

import pytest
from django.core.management import call_command
from django.utils import timezone

from mailing.campaign_queue_tasks import run_campaign_snapshot
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaigns import queue_campaign, request_campaign_queue
from mailing.services.contact_import_export import (
    bulk_import_contacts_for_client,
    export_contacts_csv_for_client,
)
from mailing.transactional_retry_tasks import run_bulk_transactional_retry

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="Async team", slug="async-team")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="Async client", slug="async-client")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="Async readers", slug="async-readers")


@pytest.fixture
def campaign(audience, client_record):
    return Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Weekly update",
        html_body="<p>Hello</p>",
        text_body="Hello",
    )


@pytest.fixture
def subscribed_contact(audience, client_record):
    contact = Contact.objects.create(email="eligible@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact, audience=audience, client=client_record, status=SubscriptionStatus.SUBSCRIBED
    )
    return contact


def test_request_returns_immediately_and_worker_completes_idempotently(
    campaign, subscribed_contact, monkeypatch, django_capture_on_commit_callbacks
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    snapshots = []
    monkeypatch.setattr(
        "mailing.campaign_queue_tasks.enqueue_campaign_snapshot",
        lambda campaign_id, **kwargs: snapshots.append(campaign_id),
    )

    with django_capture_on_commit_callbacks(execute=True):
        result = request_campaign_queue(campaign)
    assert result.accepted is True
    assert result.snapshotting is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SNAPSHOTTING
    # Request returned before any snapshot work or SES.
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 0
    assert enqueued == []
    assert snapshots == [campaign.id]

    # Idempotent: a second request while snapshotting enqueues nothing new.
    with django_capture_on_commit_callbacks(execute=True):
        again = request_campaign_queue(campaign)
    assert again.accepted is False
    assert again.already_queued is True
    assert snapshots == [campaign.id]

    # Worker completes the snapshot (what run_campaign_snapshot calls).
    with django_capture_on_commit_callbacks(execute=True):
        worker_result = queue_campaign(campaign)
    assert worker_result.queued is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.QUEUED
    assert campaign.recipient_count == 1
    assert len(enqueued) == worker_result.batch_count == 1

    # Retry after completion never double-enqueues.
    with django_capture_on_commit_callbacks(execute=True):
        retry = queue_campaign(campaign)
    assert retry.queued is False
    assert len(enqueued) == 1


def test_snapshot_task_is_registered_on_default_queue():
    assert run_campaign_snapshot.queue_name == "default"


def test_worker_healthcheck_reports_alive():
    call_command("worker_healthcheck")


def test_gunicorn_config_uses_threads():
    spec = importlib.util.spec_from_file_location("gunicorn_conf", "gunicorn.conf.py")
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert conf.worker_class == "gthread"
    assert conf.workers >= 2
    assert conf.threads >= 2


def test_bulk_retry_task_is_registered_on_default_queue():
    assert run_bulk_transactional_retry.queue_name == "default"


def test_contact_sync_caps_are_enforced(client_record):
    with pytest.raises(ApiValidationError):
        bulk_import_contacts_for_client(
            {"contacts": [{"email": f"u{i}@example.com"} for i in range(201)]}, client_record
        )
    with pytest.raises(ApiValidationError):
        export_contacts_csv_for_client({"limit": "2001"}, client_record)
