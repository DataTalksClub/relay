"""Transactional mail must not wait behind campaign batches (queue split).

`send_campaign_email_batch` runs on the `campaign` queue consumed by exactly
one `campaign-worker`; `send_transactional_email[_batch]` run on the
`transactional` queue consumed by the `worker` service, which never touches
the campaign lane. These tests pin that split: the declared queue names, the
queues rows actually land on, claim isolation while a batch is RUNNING, and
the migration that moves pre-split READY rows off `default`.
"""

from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.tasks import TaskResultStatus
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing import tasks as mailing_tasks
from relay.management.commands.db_worker import ResilientWorker

pytestmark = pytest.mark.django_db(transaction=True)


def test_send_tasks_declare_split_queues():
    assert mailing_tasks.send_campaign_email_batch.queue_name == "campaign"
    assert mailing_tasks.send_transactional_email.queue_name == "transactional"
    assert mailing_tasks.send_transactional_email_batch.queue_name == "transactional"
    # Unchanged lanes: SES/inbound, tracking, and the default-queue no-op.
    assert mailing_tasks.process_ses_webhook_event.queue_name == "provider-events"
    assert mailing_tasks.process_inbound_email.queue_name == "provider-events"
    assert mailing_tasks.process_tracking_event.queue_name == "tracking"
    assert mailing_tasks.process_email_event.queue_name == "default"


def test_enqueue_routes_each_send_to_its_own_lane():
    campaign = mailing_tasks.send_campaign_email_batch.enqueue(
        {"contract": "campaign-email", "version": 1}
    )
    transactional = mailing_tasks.send_transactional_email.enqueue(
        {"contract": "transactional-email", "version": 1}
    )
    fanout = mailing_tasks.send_transactional_email_batch.enqueue(
        [], list_key="l", template_key="t", client_id=None
    )
    assert DBTaskResult.objects.get(pk=campaign.id).queue_name == "campaign"
    assert DBTaskResult.objects.get(pk=transactional.id).queue_name == "transactional"
    assert DBTaskResult.objects.get(pk=fanout.id).queue_name == "transactional"


def _worker_for(*queues):
    return ResilientWorker(
        queue_names=list(queues),
        interval=0,
        batch=True,
        backend_name="default",
        startup_delay=False,
        max_tasks=None,
        worker_id="send-queue-split-test",
    )


def test_transactional_claimable_while_campaign_batch_running():
    """AC1 at the claim layer: a RUNNING campaign batch holds only the
    campaign lane; the transactional worker still claims its own task."""
    campaign = mailing_tasks.send_campaign_email_batch.enqueue(
        {"contract": "campaign-email", "version": 1}
    )
    transactional = mailing_tasks.send_transactional_email.enqueue(
        {"contract": "transactional-email", "version": 1}
    )

    campaign_worker = _worker_for("campaign")
    claimed_batch = campaign_worker._claim_next_task()
    assert claimed_batch is not None
    assert str(claimed_batch.pk) == str(campaign.id)
    assert DBTaskResult.objects.get(pk=campaign.id).status == TaskResultStatus.RUNNING

    # The campaign lane is now busy, but the transactional worker (which
    # consumes transactional + the legacy default drain, never campaign)
    # still picks up the transactional task.
    transactional_worker = _worker_for("transactional", "default")
    claimed_txn = transactional_worker._claim_next_task()
    assert claimed_txn is not None
    assert str(claimed_txn.pk) == str(transactional.id)

    # And the campaign worker cannot steal transactional work: nothing else
    # sits on its lane.
    assert campaign_worker._claim_next_task() is None


def _ready_row(*, task_path, queue_name="default", status=TaskResultStatus.READY, run_after=None):
    return DBTaskResult.objects.create(
        args_kwargs={"args": [{}], "kwargs": {}},
        priority=0,
        task_path=task_path,
        queue_name=queue_name,
        backend_name="default",
        run_after=run_after or timezone.now(),
        status=status,
        exception_class_path="",
        traceback="",
    )


def test_split_migration_moves_only_ready_send_tasks():
    migration = import_module("mailing.migrations.0049_send_queue_split")

    batch = _ready_row(task_path="mailing.tasks.send_campaign_email_batch")
    delayed_txn = _ready_row(
        task_path="mailing.tasks.send_transactional_email",
        run_after=timezone.now() + timezone.timedelta(hours=1),
    )
    fanout = _ready_row(task_path="mailing.tasks.send_transactional_email_batch")
    running_batch = _ready_row(
        task_path="mailing.tasks.send_campaign_email_batch",
        status=TaskResultStatus.RUNNING,
    )
    other = _ready_row(task_path="jobs.tasks.execute_job")
    placeholder = _ready_row(task_path="mailing.tasks.process_email_event")

    migration.move_send_tasks_to_split_queues(django_apps, None)

    assert DBTaskResult.objects.get(pk=batch.pk).queue_name == "campaign"
    assert DBTaskResult.objects.get(pk=delayed_txn.pk).queue_name == "transactional"
    assert DBTaskResult.objects.get(pk=fanout.pk).queue_name == "transactional"
    # RUNNING rows are claimed by a live worker: hands off.
    assert DBTaskResult.objects.get(pk=running_batch.pk).queue_name == "default"
    # Non-send tasks keep the default consumer.
    assert DBTaskResult.objects.get(pk=other.pk).queue_name == "default"
    assert DBTaskResult.objects.get(pk=placeholder.pk).queue_name == "default"
