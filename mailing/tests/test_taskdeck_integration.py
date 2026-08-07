"""Integration of the task layer, and a regression guard on the enqueue bug.

Before this, `queue_campaign` enqueued its batches after the `atomic` block
with no on-commit hook. Called inside an outer transaction, that block is only
a savepoint, so batches were dispatched before the outer commit -- and a
rollback left sends in flight for a campaign that was never marked queued.
Email cannot be recalled, so this is guarded explicitly rather than trusted.
"""

import pytest
from django.db import transaction
from django.tasks import task
from django.utils import timezone

import taskdeck
from mailing import tasks as mailing_tasks
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.campaigns import queue_campaign
from taskdeck.models import TaskRun, TaskRunStatus

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
    assert run.project == "relay"
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


@pytest.fixture
def score_template(client_record):
    return EmailTemplate.objects.create(
        client=client_record,
        key="homework-score-notification",
        name="Homework score",
        subject="Score",
        text_body="Score",
    )


def _queued_message(client_record, contact, template, email):
    return TransactionalMessage.objects.create(
        client=client_record,
        contact=contact,
        email=email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        subject="Score",
        idempotency_key=f"homework-score:{email}",
    )


def test_bulk_send_batch_reports_progress_over_its_children(
    settings, monkeypatch, client_record, subscribed_contact, score_template
):
    """A scoring run must be answerable as "k of N sent", not as N loose rows.

    The recipient-list endpoints used to enqueue one send per member straight
    from the request. Those are siblings with no parent, so there was no
    denominator to show and no single row to watch. The linkage this depends on
    -- a child attaching to whichever run enqueued it -- only happens when the
    children are enqueued from inside the parent, which is why the batch task
    is driven here rather than the endpoint.
    """
    settings.TASKS = {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}

    messages = [
        _queued_message(client_record, subscribed_contact, score_template, f"s{i}@example.com")
        for i in range(3)
    ]

    # Stop at the point of dispatch: this is about the shape of the fan-out,
    # not about SES. The child still runs, so the parent's progress is real.
    sent = []
    monkeypatch.setattr(
        "mailing.tasks.send_transactional_email_from_queue",
        lambda payload, **kwargs: sent.append(payload),
    )

    result = mailing_tasks.send_transactional_email_batch.enqueue(
        [m.id for m in messages],
        list_key="homework-1-submitters",
        template_key=score_template.key,
        client_id=client_record.id,
    )

    parent = TaskRun.objects.get(result_id=str(result.id))
    assert parent.children.count() == 3, "each member send must hang off the batch"
    assert parent.resolved_progress() == (3, 3)
    assert parent.entity_type == "recipient_list"
    assert parent.entity_id == "homework-1-submitters"
    assert parent.owner_id == str(client_record.id)
    assert parent.message == "homework-score-notification: 3 recipients"
    assert len(sent) == 3


def test_bulk_send_batch_skips_a_message_deleted_before_pickup(
    settings, monkeypatch, client_record, subscribed_contact, score_template
):
    """The batch is enqueued on commit and runs later, so a row can vanish in
    between. The rest of the batch must still go out."""
    settings.TASKS = {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}

    kept = _queued_message(client_record, subscribed_contact, score_template, "kept@example.com")
    gone = _queued_message(client_record, subscribed_contact, score_template, "gone@example.com")
    gone_id = gone.id
    gone.delete()

    sent = []
    monkeypatch.setattr(
        "mailing.tasks.send_transactional_email_from_queue",
        lambda payload, **kwargs: sent.append(payload),
    )

    result = mailing_tasks.send_transactional_email_batch.enqueue([kept.id, gone_id])

    parent = TaskRun.objects.get(result_id=str(result.id))
    assert len(sent) == 1
    assert parent.children.count() == 1
    # The denominator still reflects what was asked for, so the gap is visible
    # rather than silently rounded away.
    assert parent.resolved_progress() == (1, 2)
