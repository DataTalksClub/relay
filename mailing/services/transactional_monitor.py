"""Privacy-preserving acceptance and API failure monitoring; never resends mail."""
import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Max, Sum
from django.utils import timezone

from mailing.aws import ses_client_for_source
from mailing.models import Client, TransactionalMessage, TransactionalMonitorState, TransactionalRequestMinute
from mailing.ses import send_email

logger = logging.getLogger(__name__)
_last_check = None


def policy_for(client_id):
    return {
        "min_daily": 10, "timezone": "Europe/Berlin", "quiet_hours": [],
        "error_percent": 20, "min_requests": 5,
        **settings.RELAY_TRANSACTIONAL_MONITOR_CLIENTS.get(str(client_id), {}),
    }


def normal_hour(now, policy):
    zone = ZoneInfo(policy["timezone"])
    # Require an entire active hour, avoiding a page immediately after quiet hours.
    return all((now - timedelta(minutes=m)).astimezone(zone).hour not in policy["quiet_hours"]
               for m in range(61))


def client_alerts(client, now):
    policy = policy_for(client.pk)
    if policy.get("enabled", True) is False:
        return []
    cutoff = now - timedelta(hours=1)
    messages = TransactionalMessage.objects.filter(client=client, created_at__lte=now)
    baseline = messages.filter(created_at__gte=now - timedelta(days=7), created_at__lte=cutoff).count()
    latest = messages.aggregate(latest=Max("created_at"))["latest"]
    alerts = []
    if (baseline >= max(1, policy["min_daily"] * 7) and latest is not None
            and latest <= cutoff and normal_hour(now, policy)):
        alerts.append({"kind": "acceptance_gap", "client_id": client.pk,
                       "baseline_count": baseline, "gap_minutes": int((now - latest).total_seconds() / 60)})
    counts = TransactionalRequestMinute.objects.filter(
        client=client, minute__gt=now - timedelta(minutes=15), minute__lte=now,
    ).aggregate(total=Sum("total"), errors=Sum("errors"))
    total, errors = counts["total"] or 0, counts["errors"] or 0
    if total >= max(1, policy["min_requests"]) and errors * 100 >= total * policy["error_percent"]:
        alerts.append({"kind": "api_errors", "client_id": client.pk,
                       "requests": total, "errors": errors, "error_percent": round(errors * 100 / total, 1)})
    return alerts


def send_monitor_alert(payload, *, test=False):
    # Test delivery is deliberately fixed to the operator mailbox.
    destination = "dev@dtcdev.click" if test else settings.RELAY_TRANSACTIONAL_MONITOR_TO
    source = settings.RELAY_TRANSACTIONAL_MONITOR_FROM
    body = json.dumps(payload, sort_keys=True)
    return send_email(ses_client=ses_client_for_source(source), source=source,
                      to_email=destination, subject="[Relay transactional monitor] " + payload["kind"],
                      text_body=body, html_body="",)


def check_transactional_health(*, now=None, force=False):
    global _last_check
    now = now or timezone.now()
    if not settings.RELAY_TRANSACTIONAL_MONITOR_ENABLED:
        return []
    if not force and _last_check and now - _last_check < timedelta(seconds=15):
        return []
    _last_check = now
    found = []
    try:
        for client in Client.objects.filter(is_active=True).only("id"):
            for payload in client_alerts(client, now):
                found.append(payload)
                state, _ = TransactionalMonitorState.objects.get_or_create(client=client, kind=payload["kind"])
                if state.last_alert_at and now - state.last_alert_at < timedelta(hours=1):
                    continue
                logger.error("relay_transactional_alert %s", json.dumps(payload, sort_keys=True))
                try:
                    send_monitor_alert(payload)
                except Exception as exc:
                    logger.error("relay_transactional_alert_delivery_failed client_id=%s error_type=%s",
                                 client.pk, type(exc).__name__)
                else:
                    state.last_alert_at = now
                    state.save(update_fields=["last_alert_at"])
        TransactionalRequestMinute.objects.filter(minute__lt=now - timedelta(days=1)).delete()
        logger.info("relay_transactional_monitor_heartbeat checked_at=%s alerts=%s", now.isoformat(), len(found))
    except Exception as exc:
        # Monitoring must not stop the scheduler. Never log request bodies or exception text.
        logger.error("relay_transactional_monitor_failed error_type=%s", type(exc).__name__)
    return found
