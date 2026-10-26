"""Background bulk retry for transactional messages.

The admin bulk-retry endpoint used to loop up to 100 sequential retries on
the request thread (5-20 s). Now it enqueues this task and returns 202.
Idempotent: single-message retry guards (failed-only without force,
suppression blocks, same row/idempotency key) make re-entry safe — already
retried rows are skipped, never double-sent.
"""

import logging

from django.tasks import task

import taskdeck

logger = logging.getLogger(__name__)


def enqueue_bulk_retry(*, actor_id=None, reason="", force=False, limit=100,
                       client_id=None, client_slug="", status="failed",
                       created_after=None, created_before=None,
                       error_contains="", template_key=""):
    from mailing import transactional_retry_tasks as tasks  # noqa: PLC0415 - keeps enqueue import-light

    result = tasks.run_bulk_transactional_retry.enqueue(
        actor_id=actor_id, reason=reason, force=force, limit=limit,
        client_id=client_id, client_slug=client_slug, status=status,
        created_after=created_after.isoformat() if created_after is not None else None,
        created_before=created_before.isoformat() if created_before is not None else None,
        error_contains=error_contains, template_key=template_key,
    )
    taskdeck.stamp(result, message=f"Bulk retry queued (limit {limit})")
    return result


@task()
def run_bulk_transactional_retry(*, actor_id=None, reason="", force=False, limit=100,
                                 client_id=None, client_slug="", status="failed",
                                 created_after=None, created_before=None,
                                 error_contains="", template_key=""):
    from django.contrib.auth import get_user_model  # noqa: PLC0415 - keeps tasks import-light
    from django.utils.dateparse import parse_datetime  # noqa: PLC0415 - keeps tasks import-light

    from mailing.models import Client  # noqa: PLC0415 - keeps tasks import-light
    from mailing.services.transactional_retry import (  # noqa: PLC0415 - breaks an import cycle
        MAX_BULK_RETRY,
        bulk_retry_transactional_messages,
        collect_retry_candidates,
    )

    limit = max(1, min(int(limit or MAX_BULK_RETRY), MAX_BULK_RETRY))
    actor = None
    if actor_id is not None:
        actor = get_user_model().objects.filter(pk=actor_id).first()
    client = Client.objects.filter(pk=client_id).first() if client_id is not None else None
    after = parse_datetime(created_after) if created_after else None
    before = parse_datetime(created_before) if created_before else None

    if client_id:
        taskdeck.set_owner(client_id)
    taskdeck.set_total(min(limit, MAX_BULK_RETRY), message="Bulk transactional retry")

    qs = collect_retry_candidates(
        client=client, client_slug=client_slug or "", status=status,
        created_after=after, created_before=before,
        error_contains=error_contains or "", template_key=template_key or "",
    )
    result = bulk_retry_transactional_messages(qs, actor=actor, reason=reason or "", force=force, limit=limit)
    return result.to_dict()
