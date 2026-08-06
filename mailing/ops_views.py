"""The taskdeck status contract endpoint.

Read-only, machine-consumed, and deliberately separate from the operator UI's
own status page: this one is polled by the cross-project console and has to
keep a stable shape across releases. See docs/contract.md in the taskdeck repo.

There is no path from here to enqueueing or cancelling anything. A console that
can only observe cannot cause an outage.
"""

import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET
from taskdeck.collector import collect_status

from mailing.models import Campaign, TransactionalMessage
from mailing.services.worker_status import WORKER_DEFINITIONS, backlog_count

logger = logging.getLogger(__name__)

CACHE_KEY = "taskdeck:status"
CACHE_SECONDS = 30


def _authorised(request):
    """Constant-time bearer check.

    A bearer token is enough here: this is a read-only server-to-server GET
    over TLS, so the replay protection a signed request adds buys nothing.
    """
    expected = getattr(settings, "TASKDECK_STATUS_TOKEN", "")
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :].strip(), expected)


def _entity_resolver(entity_type, entity_id):
    """Turn a recorded entity reference into a label and a link.

    Kept in the project rather than the package: it needs this project's
    models, and every project's answer is different.
    """
    if entity_type == "campaign":
        campaign = Campaign.objects.filter(pk=entity_id).only("id", "subject").first()
        if campaign is None:
            return None
        return {
            "label": campaign.subject,
            "url": reverse("mailing:campaign_detail", args=[campaign.id]),
        }
    if entity_type == "transactional_message":
        message = (
            TransactionalMessage.objects.filter(pk=entity_id).only("id", "email").first()
        )
        if message is None:
            return None
        return {"label": message.email, "url": ""}
    return None


def _ingress_backlogs():
    """Report the queues AWS writes into directly, alongside task state.

    These are not task-system queues, so they are absent from the package's
    view. Leaving them out would mean a backed-up SES notification queue looked
    like a perfectly healthy system.
    """
    rows = []
    for definition in WORKER_DEFINITIONS:
        try:
            count = backlog_count(definition.key)
        except Exception:
            logger.exception("taskdeck: backlog probe failed for %s", definition.key)
            count = None
        if count is None:
            continue
        rows.append(
            {"name": definition.key, "label": definition.backlog_label, "count": count}
        )
    return rows


def build_payload():
    payload = collect_status(mode="sidecar", entity_resolver=_entity_resolver)
    payload["ingress_backlogs"] = _ingress_backlogs()
    return payload


@require_GET
def status(request):
    if not _authorised(request):
        # 404 rather than 401: an unauthenticated caller learns nothing about
        # whether this endpoint exists on this host.
        return JsonResponse({"detail": "not found"}, status=404)

    payload = cache.get(CACHE_KEY)
    if payload is None:
        payload = build_payload()
        cache.set(CACHE_KEY, payload, CACHE_SECONDS)
    return JsonResponse(payload)
