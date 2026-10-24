"""SES delivery and webhook gap monitoring using the ops alert transport.

Alerts when:
1. SES-sent messages older than 2 hours have no provider outcome event above a threshold.
2. CloudWatch SNS published events exceed Relay persisted events over a 30-minute window.
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from mailing.aws import aws_client
from mailing.models import EmailEvent, EmailEventType, OperatorAudit, TransactionalMessage, TransactionalMessageStatus
from mailing.services.transactional_monitor import send_monitor_alert

logger = logging.getLogger(__name__)

_last_alert_by_kind = {}


def get_sns_published_count(topic_name, start_time, end_time, *, client=None):
    """Query CloudWatch for SNS NumberOfMessagesPublished in the given time window."""
    try:
        cw = client or aws_client("cloudwatch")
        res = cw.get_metric_statistics(
            Namespace="AWS/SNS",
            MetricName="NumberOfMessagesPublished",
            Dimensions=[{"Name": "TopicName", "Value": topic_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=max(60, int((end_time - start_time).total_seconds())),
            Statistics=["Sum"],
        )
        datapoints = res.get("Datapoints", [])
        return int(sum(dp["Sum"] for dp in datapoints))
    except Exception as exc:
        logger.warning(
            "ses_monitor_cloudwatch_query_failed topic=%s error_type=%s",
            topic_name,
            type(exc).__name__,
        )
        return None


def check_ses_outcome_gap(now=None, *, threshold=None, lookback_days=None):
    """Check for SES-sent messages older than 2h that have no outcome event."""
    now = now or timezone.now()
    threshold = threshold if threshold is not None else getattr(settings, "RELAY_SES_OUTCOME_GAP_THRESHOLD", 1)
    lookback_days = (
        lookback_days if lookback_days is not None else getattr(settings, "RELAY_SES_OUTCOME_GAP_LOOKBACK_DAYS", 7)
    )

    cutoff = now - timedelta(hours=2)
    start_time = now - timedelta(days=lookback_days)

    unconfirmed = (
        TransactionalMessage.objects.filter(
            created_at__gte=start_time,
            created_at__lte=cutoff,
            status=TransactionalMessageStatus.SENT,
            delivered_at__isnull=True,
        )
        .exclude(
            events__event_type__in=[
                EmailEventType.DELIVERED,
                EmailEventType.BOUNCE,
                EmailEventType.COMPLAINT,
            ]
        )
        .order_by("created_at")
    )

    count = unconfirmed.count()
    if count >= threshold:
        return {
            "kind": "ses_outcome_gap",
            "count": count,
            "threshold": threshold,
            "threshold_hours": 2,
            "message_ids": list(unconfirmed.values_list("pk", flat=True)[:50]),
        }
    return None


def check_sns_persisted_gap(now=None, *, cw_client=None, topic_name=None, window_minutes=30):
    """Check if SNS published events exceed Relay persisted events over window."""
    now = now or timezone.now()
    topic_name = topic_name or getattr(settings, "RELAY_SES_EVENTS_TOPIC_NAME", "relay-production-ses-events")
    window_start = now - timedelta(minutes=window_minutes)
    window_end = now

    sns_published = get_sns_published_count(topic_name, window_start, window_end, client=cw_client)
    if sns_published is None:
        # CloudWatch metric unavailable or errored; skip gap alert.
        return None

    persisted_events = EmailEvent.objects.filter(
        created_at__gte=window_start,
        created_at__lte=window_end,
    ).count()

    persisted_audits = OperatorAudit.objects.filter(
        action__startswith="ses.",
        created_at__gte=window_start,
        created_at__lte=window_end,
    ).count()

    total_persisted = persisted_events + persisted_audits

    if sns_published > total_persisted:
        return {
            "kind": "ses_sns_persisted_gap",
            "window_minutes": window_minutes,
            "sns_published": sns_published,
            "relay_persisted": total_persisted,
            "topic": topic_name,
        }
    return None


def check_ses_delivery_gap(*, now=None, force=False, cw_client=None, test=False):
    """Run SES delivery guard checks and emit alerts via ops alert path."""
    global _last_alert_by_kind
    now = now or timezone.now()

    if not getattr(settings, "RELAY_SES_MONITOR_ENABLED", True) and not force:
        return []

    alerts = []
    outcome_gap = check_ses_outcome_gap(now)
    if outcome_gap:
        alerts.append(outcome_gap)

    sns_gap = check_sns_persisted_gap(now, cw_client=cw_client)
    if sns_gap:
        alerts.append(sns_gap)

    sent_alerts = []
    for alert in alerts:
        kind = alert["kind"]
        last_alert = _last_alert_by_kind.get(kind)
        if not force and last_alert and (now - last_alert) < timedelta(hours=1):
            continue

        logger.error("relay_ses_guard_alert %s", json.dumps(alert, sort_keys=True))
        try:
            send_monitor_alert(alert, test=test)
        except Exception as exc:
            logger.error(
                "relay_ses_guard_alert_delivery_failed kind=%s error_type=%s",
                kind,
                type(exc).__name__,
            )
        else:
            _last_alert_by_kind[kind] = now
            sent_alerts.append(alert)

    return sent_alerts
