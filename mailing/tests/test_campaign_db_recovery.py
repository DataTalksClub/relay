"""Mail-free outage/restart recovery and connection-budget regression tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import OperationalError, connection
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing import tasks
from mailing.enqueue import batch_campaign_enqueues, enqueue_campaign_email
from mailing.models import Audience, Campaign, CampaignRecipient, Client, Contact, Organization, Subscription
from mailing.services import campaign_sender as sender
from mailing.services.campaign_db_recovery import recover_campaign_db_batches
from mailing.services.task_leases import fail_stale_campaign_tasks
from mailing.tests.claim_finalize_harness import FaultSes
from relay.management.commands.db_worker import ResilientWorker

pytestmark = pytest.mark.django_db
SLOTS = "FATAL: remaining connection slots are reserved for roles with the SUPERUSER attribute"


@pytest.fixture
def batch():
    org = Organization.objects.create(name="Recovery", slug="recovery")
    audience = Audience.objects.create(organization=org, name="News", slug="news")
    client = Client.objects.create(organization=org, name="Client", slug="client")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status="sending", html_body="<p>Hi</p>", text_body="Hi",
    )
    rows = []
    for index, status in enumerate(["pending", "pending", "sent", "sending", "unknown", "skipped", "failed"]):
        contact = Contact.objects.create(email=f"recovery{index}@example.com", verified_at=timezone.now())
        Subscription.objects.create(contact=contact, audience=audience, client=client, status="subscribed")
        rows.append(CampaignRecipient.objects.create(
            campaign=campaign, contact=contact, email=contact.email, status=status,
        ))
    payload = {
        "contract": "campaign-email", "version": 1, "campaign_id": campaign.pk,
        "batch_id": "outage-batch", "campaign_recipient_ids": [row.pk for row in rows],
    }
    return campaign, rows, payload


def _worker():
    return ResilientWorker(
        queue_names=["campaign"], interval=0, batch=True, backend_name="default",
        startup_delay=False, max_tasks=1, worker_id="outage-test",
    )


def _failed(payload, *, opt_in=True, error=None):
    result = enqueue_campaign_email(payload) if opt_in else tasks.send_campaign_email_batch.enqueue(payload)
    row = DBTaskResult.objects.get(pk=result.id)
    row.set_failed(error or OperationalError(SLOTS))
    return row


@pytest.mark.django_db(transaction=True)
def test_outage_failed_batch_survives_worker_restart_and_sends_only_pending(batch, monkeypatch):
    campaign, rows, payload = batch
    enqueue_campaign_email(payload)
    monkeypatch.setattr(tasks, "send_campaign_batch", Mock(side_effect=OperationalError(SLOTS)))
    _worker().run()
    original = DBTaskResult.objects.get()
    assert original.status == "FAILED"

    # No in-memory retry callback survives. The durable failed task is enough.
    recovered = recover_campaign_db_batches()
    assert len(recovered) == 1
    retry = DBTaskResult.objects.get(pk=recovered[0])
    assert retry.args_kwargs["args"][0]["campaign_recipient_ids"] == [r.pk for r in rows[:2]]
    assert timezone.now() < retry.run_after <= timezone.now() + timedelta(seconds=38)
    assert recover_campaign_db_batches() == []

    # Exercise the real sender against a fake SES transport, never the network.
    ses = FaultSes()
    monkeypatch.setattr(tasks, "send_campaign_batch", lambda payload: sender.send_campaign_batch(payload, ses_client=ses))
    retry.run_after = timezone.now() - timedelta(seconds=1)
    retry.save(update_fields=["run_after"])
    _worker().run()
    retry.refresh_from_db()
    assert retry.status == "SUCCESSFUL"
    assert CampaignRecipient.objects.filter(pk__in=[r.pk for r in rows[:2]], status="pending").count() == 0
    assert list(CampaignRecipient.objects.filter(pk__in=[r.pk for r in rows[2:]]).order_by("id").values_list(
        "status", flat=True,
    )) == ["sent", "sending", "unknown", "skipped", "failed"]
    assert ses.calls == 2  # fake SES only; no network transport exists in this test


def test_recovery_enqueue_failure_rolls_back_receipt_and_can_retry(batch, monkeypatch):
    original = _failed(batch[2])
    monkeypatch.setattr(tasks.send_campaign_email_batch.__class__, "using", Mock(side_effect=OperationalError(SLOTS)))
    with pytest.raises(OperationalError):
        recover_campaign_db_batches()
    original.refresh_from_db()
    assert original.return_value is None
    assert DBTaskResult.objects.count() == 1
    monkeypatch.undo()
    assert len(recover_campaign_db_batches()) == 1


def test_receipt_failure_rolls_back_successor_insert(batch, monkeypatch):
    original = _failed(batch[2])
    save = DBTaskResult.save

    def fail_receipt(row, *args, **kwargs):
        if row.pk == original.pk:
            raise OperationalError(SLOTS)
        return save(row, *args, **kwargs)

    monkeypatch.setattr(DBTaskResult, "save", fail_receipt)
    with pytest.raises(OperationalError):
        recover_campaign_db_batches()
    assert DBTaskResult.objects.count() == 1
    original.refresh_from_db()
    assert original.return_value is None
    monkeypatch.undo()
    assert len(recover_campaign_db_batches()) == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires row locks")
@pytest.mark.django_db(transaction=True)
def test_concurrent_recovery_processes_create_exactly_one_successor(batch):
    _failed(batch[2])

    def recover():
        try:
            return recover_campaign_db_batches()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover(), range(2)))
    assert sum(map(len, results)) == 1
    assert DBTaskResult.objects.count() == 2


POOL_TIMEOUT = "couldn't get a connection after 10.00 sec"


@pytest.mark.parametrize(
    "message", [SLOTS, "sorry, too many clients already", "too many connections for role relay", POOL_TIMEOUT],
)
def test_connection_exhaustion_variants_are_recovered(batch, message):
    _failed(batch[2], error=OperationalError(message))
    assert len(recover_campaign_db_batches()) == 1


@pytest.mark.parametrize("error", [OperationalError("missing relation"), ValueError(SLOTS)])
def test_permanent_or_non_database_errors_are_not_retried(batch, error):
    _failed(batch[2], error=error)
    assert recover_campaign_db_batches() == []


def test_historical_campaigns_are_never_opted_in_by_deploy(batch):
    old = _failed(batch[2], opt_in=False)
    assert recover_campaign_db_batches() == []
    old.refresh_from_db()
    assert old.return_value is None
    assert DBTaskResult.objects.count() == 1


@pytest.mark.parametrize("halt", ["pause", "cancel", "done"])
def test_halted_cancelled_completed_campaigns_are_not_requeued(batch, halt):
    campaign, _, payload = batch
    _failed(payload)
    if halt == "pause":
        campaign.send_halted_at = timezone.now()
    else:
        campaign.status = "cancelled" if halt == "cancel" else "sent"
    campaign.save()
    assert recover_campaign_db_batches() == []
    assert DBTaskResult.objects.count() == 1


def test_stale_running_task_with_lost_result_write_has_one_successor(batch):
    result = enqueue_campaign_email(batch[2])
    DBTaskResult.objects.filter(pk=result.id).update(
        status="RUNNING", started_at=timezone.now() - timedelta(hours=1),
    )
    fail_stale_campaign_tasks(auto_reenqueue=True)
    assert DBTaskResult.objects.count() == 1
    assert len(recover_campaign_db_batches()) == 1
    assert recover_campaign_db_batches() == []


def test_bulk_enqueue_persists_recovery_marker(batch):
    with batch_campaign_enqueues(None):
        enqueue_campaign_email(batch[2])
    assert DBTaskResult.objects.get().args_kwargs["args"][0]["db_recovery"] == 1


def test_long_outage_has_capped_backoff_without_attempt_exhaustion(batch):
    original = _failed({**batch[2], "db_recovery_attempt": 1000})
    assert len(recover_campaign_db_batches()) == 1
    original.refresh_from_db()
    assert original.return_value["retry_in_seconds"] == 900


def test_sender_pool_cannot_exceed_twelve_database_connections(settings, monkeypatch):
    settings.CAMPAIGN_SEND_CONCURRENCY = 32
    monkeypatch.setattr(sender.connection, "vendor", "postgresql")
    assert sender._send_concurrency(500) == 12
    assert sender._configured_send_concurrency() == 12
    assert sender._send_concurrency(3) == 3


def test_sender_thread_releases_database_slot_even_on_failure(monkeypatch):
    run = object.__new__(sender._BatchRun)
    run.stop = Mock()
    run.stop.is_set.return_value = True
    close = Mock()
    monkeypatch.setattr(sender.connection, "close", close)
    run.drain(close_connections=True)
    close.assert_called_once()


def test_pool_timeout_failed_batch_requeues_only_pending_rows(batch):
    """Campaign 318: a batch killed by psycopg_pool PoolTimeout must not strand its PENDING rows."""
    campaign, rows, payload = batch
    original = _failed(payload, error=OperationalError(POOL_TIMEOUT))
    successors = recover_campaign_db_batches()
    assert len(successors) == 1
    successor = DBTaskResult.objects.get(pk=successors[0])
    assert successor.args_kwargs["args"][0]["campaign_recipient_ids"] == [rows[0].pk, rows[1].pk]
    original.refresh_from_db()
    assert original.return_value["db_retry_task_id"] == successors[0]
    assert recover_campaign_db_batches() == []
