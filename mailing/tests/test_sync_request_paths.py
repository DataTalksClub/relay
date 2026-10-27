"""Synchronous request behavior and preserved resource limits."""

import importlib.util

import pytest
from django.core.management import call_command
from django.utils import timezone

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
from mailing.services.campaigns import request_campaign_queue
from mailing.services.contact_import_export import (
    bulk_import_contacts_for_client,
    export_contacts_csv_for_client,
)

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


def test_request_completes_synchronously_and_is_idempotent(campaign, subscribed_contact):
    result = request_campaign_queue(campaign)
    assert result.accepted and not result.snapshotting
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.QUEUED
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 1
    assert not request_campaign_queue(campaign).accepted


def test_worker_healthcheck_reports_alive():
    call_command("worker_healthcheck")


def test_gunicorn_config_uses_threads():
    spec = importlib.util.spec_from_file_location("gunicorn_conf", "gunicorn.conf.py")
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert conf.worker_class == "gthread"
    assert conf.workers >= 2
    assert conf.threads >= 2


def test_contact_sync_caps_are_enforced(client_record):
    with pytest.raises(ApiValidationError):
        bulk_import_contacts_for_client(
            {"contacts": [{"email": f"u{i}@example.com"} for i in range(201)]}, client_record
        )
    with pytest.raises(ApiValidationError):
        export_contacts_csv_for_client({"limit": "2001"}, client_record)


def test_snapshot_queries_do_not_grow_per_contact(campaign, django_assert_num_queries):
    from django.db import connection  # noqa: PLC0415
    from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

    from mailing.services.campaigns import snapshot_campaign_recipients  # noqa: PLC0415

    now = timezone.now()
    contacts = Contact.objects.bulk_create([
        Contact(email=f"snapshot-{i}@example.com", normalized_email=f"snapshot-{i}@example.com", verified_at=now)
        for i in range(1000)
    ])
    Subscription.objects.bulk_create([
        Subscription(contact=contact, audience=campaign.audience, client=campaign.client,
                     status=SubscriptionStatus.SUBSCRIBED) for contact in contacts
    ])
    with CaptureQueriesContext(connection) as queries:
        result = snapshot_campaign_recipients(campaign)
    assert result.recipient_count == 1000
    assert len(queries) < 35
    inserts = [query['sql'] for query in queries if query['sql'].startswith('INSERT INTO \"campaign_recipients\"')]
    assert len(inserts) == 1 and 'SELECT' in inserts[0]


def test_queue_bulk_inserts_existing_send_tasks(campaign, subscribed_contact):
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    from taskdeck.models import TaskRun  # noqa: PLC0415

    request_campaign_queue(campaign)
    task = DBTaskResult.objects.get(task_path="mailing.tasks.send_campaign_email_batch")
    assert task.args_kwargs["args"][0]["campaign_id"] == campaign.pk
    assert TaskRun.objects.get(result_id=str(task.pk)).entity_id == str(campaign.pk)
    assert not DBTaskResult.objects.filter(task_path__contains="snapshot").exists()


def test_retired_jobs_preserve_campaign_recipients(campaign, subscribed_contact):
    from importlib import import_module  # noqa: PLC0415

    from django.apps import apps  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    from mailing.services.campaigns import snapshot_campaign_recipients  # noqa: PLC0415

    snapshot_campaign_recipients(campaign)
    campaign.status = "snapshotting"
    campaign.save()
    task = DBTaskResult.objects.create(
        task_path="mailing.campaign_queue_tasks.run_campaign_snapshot", args_kwargs={"args": [campaign.pk], "kwargs": {}},
        backend_name="default",
    )
    migration = import_module("mailing.migrations.0042_retire_request_jobs")
    with connection.schema_editor() as editor:
        migration.recover_pending(apps, editor)
    campaign.refresh_from_db()
    task.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 1
    assert task.status == "FAILED"
