"""Integration of the task layer, and a regression guard on the enqueue bug.

Before this, `queue_campaign` enqueued its batches after the `atomic` block
with no on-commit hook. Called inside an outer transaction, that block is only
a savepoint, so batches were dispatched before the outer commit -- and a
rollback left sends in flight for a campaign that was never marked queued.
Email cannot be recalled, so this is guarded explicitly rather than trusted.
"""

import pytest
import taskdeck
from django.db import transaction
from django.tasks import task
from django.utils import timezone
from taskdeck.models import TaskRun, TaskRunStatus

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaigns import queue_campaign

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(
        organization=organization, name="DataTalksClub", slug="dtc"
    )


@pytest.fixture
def client_record(organization):
    return Client.objects.create(
        organization=organization, name="DTC Courses", slug="dtc-courses"
    )


@pytest.fixture
def subscribed_contact(audience, client_record):
    """A contact that actually survives snapshotting.

    Both the verified timestamp and the subscription's client are required --
    without them the campaign snapshots zero recipients and every assertion
    below passes vacuously.
    """
    contact = Contact.objects.create(
        email="person@example.com", verified_at=timezone.now()
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    return contact


@pytest.fixture
def campaign(audience, client_record, subscribed_contact):
    return Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Hello",
        html_body="<p>Hi</p>",
        status=CampaignStatus.DRAFT,
    )


class Rollback(Exception):
    pass


def test_rollback_after_queueing_enqueues_nothing(campaign, monkeypatch):
    """The regression guard: no sends may survive a rolled-back queueing."""
    enqueued = []
    monkeypatch.setattr(
        "mailing.services.campaigns.enqueue_campaign_email", enqueued.append
    )

    with pytest.raises(Rollback), transaction.atomic():
        queue_campaign(campaign)
        # Something later in the same request fails.
        raise Rollback

    assert enqueued == [], "a rolled-back queueing must dispatch no batches"
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT


def test_commit_enqueues_every_batch(campaign, monkeypatch, django_capture_on_commit_callbacks):
    enqueued = []
    monkeypatch.setattr(
        "mailing.services.campaigns.enqueue_campaign_email", enqueued.append
    )

    with django_capture_on_commit_callbacks(execute=True):
        result = queue_campaign(campaign)

    assert result.queued is True
    assert len(enqueued) == result.batch_count == 1
    assert enqueued[0]["contract"] == "campaign-email"


def test_batches_of_one_send_share_a_correlation_id(
    campaign, django_capture_on_commit_callbacks
):
    """Every batch of a send is one chain, so the fan-out reads as one thing."""
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign, batch_size=1)

    runs = TaskRun.objects.filter(name="send_campaign_email_batch")
    assert runs.exists()
    assert len(set(runs.values_list("correlation_id", flat=True))) == 1


def test_enqueue_creates_a_task_run_carrying_the_campaign(
    campaign, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign)

    run = TaskRun.objects.get(name="send_campaign_email_batch")
    assert run.project == "datamailer"
    assert run.status == TaskRunStatus.QUEUED


def test_recipient_status_is_the_source_of_campaign_progress(
    campaign, django_capture_on_commit_callbacks
):
    """Send progress comes from the domain model, not from counting tasks.

    Per-recipient rows survive a task being retried and are accurate even if a
    batch is redelivered, which a task-side counter would not be.
    """
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign)

    campaign.refresh_from_db()
    pending = campaign.recipients.filter(status=CampaignRecipientStatus.PENDING).count()
    assert campaign.recipient_count > 0, "fixture must produce an eligible recipient"
    assert pending == campaign.recipient_count


@task()
def _probe_child():
    return "child"


@task()
def _probe_parent():
    taskdeck.set_total(2)
    _probe_child.enqueue()
    _probe_child.enqueue()
    return "parent"


def test_fanout_children_attach_without_explicit_threading(settings):
    """Fan-out linkage works inside this project, not just in taskdeck's suite."""
    settings.TASKS = {
        "default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}
    }

    result = _probe_parent.enqueue()

    parent = TaskRun.objects.get(result_id=str(result.id))
    assert parent.children.count() == 2
    assert parent.resolved_progress() == (2, 2)
