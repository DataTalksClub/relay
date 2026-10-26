"""Background snapshot + queue for campaigns.

The request path (operator UI, client API, admin API) only flips DRAFT ->
SNAPSHOTTING and enqueues this task. The heavy recipient snapshot and batch
fan-out run here, on the ``default`` db_worker lane. Idempotent: re-entry
while QUEUED/SENDING/SENT (or a second task for the same campaign) returns
without enqueueing again, so retries never double-send.
"""

import logging

from django.db import transaction
from django.tasks import task

import taskdeck
from mailing.models import Campaign, CampaignStatus

logger = logging.getLogger(__name__)


def enqueue_campaign_snapshot(campaign_id, *, expected_revision=None, batch_size=None):
    from mailing import campaign_queue_tasks as tasks  # noqa: PLC0415 - keeps enqueue import-light

    result = tasks.run_campaign_snapshot.enqueue(
        campaign_id, expected_revision=expected_revision, batch_size=batch_size
    )
    taskdeck.stamp(result, entity=("campaign", campaign_id), message="Snapshot queued")
    return result


@task()
def run_campaign_snapshot(campaign_id, *, expected_revision=None, batch_size=None):
    from mailing.services.campaigns import queue_campaign  # noqa: PLC0415 - breaks an import cycle

    try:
        campaign = Campaign.objects.select_related("client").get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.warning("campaign snapshot %s: campaign missing, skipping", campaign_id)
        return {"campaign_id": campaign_id, "queued": False, "reason": "missing"}

    taskdeck.set_entity("campaign", campaign_id)
    if campaign.client_id:
        taskdeck.set_owner(campaign.client_id)
    taskdeck.set_total(1, message="Snapshotting recipients")

    try:
        # queue_campaign accepts SNAPSHOTTING as resumable and treats any
        # further state as an idempotent no-op.
        result = queue_campaign(
            campaign,
            batch_size=batch_size,
            expected_revision=expected_revision,
        )
    except Exception as exc:
        with transaction.atomic():
            locked = Campaign.objects.select_for_update().get(pk=campaign_id)
            if locked.status == CampaignStatus.SNAPSHOTTING:
                locked.status = CampaignStatus.FAILED
                locked.metadata = dict(locked.metadata or {}) | {"snapshot_error": str(exc)[:2000]}
                locked.save(update_fields=["status", "metadata", "updated_at"])
        raise
    return {
        "campaign_id": campaign_id,
        "queued": result.queued,
        "scheduled": result.scheduled,
        "batch_count": result.batch_count,
        "recipient_count": result.recipient_count,
        "skipped_count": result.skipped_count,
    }
