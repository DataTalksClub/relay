"""SES delivery and webhook gap monitoring using the ops alert transport.

Alerts when:
1. SES-sent messages older than 2 hours have no provider outcome event above a threshold.
2. CloudWatch SNS published events exceed Relay persisted events over a 30-minute window.
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce
from django.utils import timezone

from mailing.aws import aws_client
from mailing.models import EmailEvent, EmailEventType, OperatorAudit, TransactionalMessage, TransactionalMessageStatus
from mailing.services.transactional_monitor import send_monitor_alert

logger = logging.getLogger(__name__)

_last_alert_by_kind = {}
_pending_sns_gap = None


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


OUTCOME_GAP_REPORTED = "monitor.ses_outcome_gap.reported"


def unreported_ses_outcomes(now, lookback_days):
    reported = OperatorAudit.objects.annotate(send_id=KT("metadata__ses_message_id")).filter(
        action=OUTCOME_GAP_REPORTED,
        target_type="transactionalmessage",
        target_id=OuterRef("pk"),
        send_id=OuterRef("ses_message_id"),
    )
    return (
        TransactionalMessage.objects.annotate(outcome_sent_at=Coalesce("sent_at", "created_at"))
        .filter(
            outcome_sent_at__gte=now - timedelta(days=lookback_days),
            outcome_sent_at__lte=now - timedelta(hours=2),
            status=TransactionalMessageStatus.SENT,
            delivered_at__isnull=True,
        )
        .exclude(ses_message_id="")
        .exclude(events__event_type__in=[EmailEventType.DELIVERED, EmailEventType.BOUNCE, EmailEventType.COMPLAINT])
        .filter(~Exists(reported))
        .order_by("pk")
    )


def check_ses_outcome_gap(now=None, *, threshold=None, lookback_days=None):
    """Find overdue SES sends that have neither an outcome nor a successful gap report.

    The durable receipt is scoped to the SES send ID: restarting the scheduler
    never repeats the same incident, while a later send can raise a new one.
    """
    now = now or timezone.now()
    threshold = threshold if threshold is not None else settings.RELAY_SES_OUTCOME_GAP_THRESHOLD
    lookback_days = lookback_days if lookback_days is not None else settings.RELAY_SES_OUTCOME_GAP_LOOKBACK_DAYS
    unconfirmed = unreported_ses_outcomes(now, lookback_days)
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


def report_ses_outcome_gap(now, *, test=False):
    # Lock real message rows, including across scheduler/CLI processes. A second
    # checker skips a report in flight; failed transport rolls back its receipt.
    with transaction.atomic():
        alert = check_ses_outcome_gap(now)
        if not alert:
            return None
        messages = list(
            unreported_ses_outcomes(now, settings.RELAY_SES_OUTCOME_GAP_LOOKBACK_DAYS)
            .filter(pk__in=alert["message_ids"])
            .select_for_update(skip_locked=True)
        )
        if not messages:
            return None
        alert["message_ids"] = [message.pk for message in messages]
        logger.error("relay_ses_guard_alert %s", json.dumps(alert, sort_keys=True))
        send_monitor_alert(alert, test=test)
        if not test:
            OperatorAudit.objects.bulk_create([
                OperatorAudit(
                    action=OUTCOME_GAP_REPORTED,
                    target_type="transactionalmessage",
                    target_id=message.pk,
                    metadata={"ses_message_id": message.ses_message_id, "reported_at": now.isoformat()},
                )
                for message in messages
            ])
        return alert


def check_sns_persisted_gap(now=None, *, cw_client=None, topic_name=None, window_minutes=30):
    """Check if SNS published events exceed Relay persisted events over window.

    The SNS window ends two minutes ago (minute-aligned) to allow normal
    ingest to settle. A single boundary-straddling persist (e.g. created
    0.5s after window_end) still shows as a gap here; callers must confirm
    persistence before paging (see check_ses_delivery_gap).
    """
    now = now or timezone.now()
    topic_name = topic_name or getattr(settings, "RELAY_SES_EVENTS_TOPIC_NAME", "relay-production-ses-events")
    window_end = (now - timedelta(minutes=2)).replace(second=0, microsecond=0)
    window_start = window_end - timedelta(minutes=window_minutes)

    sns_published = get_sns_published_count(topic_name, window_start, window_end, client=cw_client)
    if sns_published is None:
        # CloudWatch metric unavailable or errored; skip gap alert.
        return None

    persisted_events = EmailEvent.objects.filter(
        created_at__gte=window_start,
        created_at__lt=window_end,
        metadata__sns_topic_arn__endswith=":" + topic_name,
    ).exclude(provider_event_id="").count()

    persisted_audits = OperatorAudit.objects.filter(
        action__startswith="ses.",
        metadata__sns_topic_arn__endswith=":" + topic_name,
        created_at__gte=window_start,
        created_at__lt=window_end,
    ).count()

    total_persisted = persisted_events + persisted_audits

    if sns_published > total_persisted:
        return {
            "kind": "ses_sns_persisted_gap",
            "window_minutes": window_minutes,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "sns_published": sns_published,
            "relay_persisted": total_persisted,
            "topic": topic_name,
        }
    return None


def check_ses_delivery_gap(*, now=None, force=False, cw_client=None, test=False):
    """Run SES delivery guard checks and emit alerts via ops alert path.

    The SNS persisted gap pages only after confirmation: a first sighting
    starts a pending window (no page) and the alert fires only if the gap
    is still present after RELAY_SNS_GAP_CONFIRM_MINUTES (default 5).
    Normal ingest lag (including a persist landing just after the
    minute-aligned window_end) resolves before confirmation; a real loss
    persists for the full 30-minute window and still pages, only delayed.
    force=True bypasses the confirmation for manual verification.
    """
    global _last_alert_by_kind, _pending_sns_gap
    now = now or timezone.now()

    if not getattr(settings, "RELAY_SES_MONITOR_ENABLED", True) and not force:
        return []

    alerts = []
    sent_alerts = []
    try:
        outcome_gap = report_ses_outcome_gap(now, test=test)
    except Exception as exc:
        logger.error("relay_ses_guard_alert_delivery_failed kind=ses_outcome_gap error_type=%s", type(exc).__name__)
    else:
        if outcome_gap:
            sent_alerts.append(outcome_gap)

    sns_gap = check_sns_persisted_gap(now, cw_client=cw_client)
    if sns_gap:
        if force:
            alerts.append(sns_gap)
            _pending_sns_gap = None
        else:
            confirm_minutes = getattr(settings, "RELAY_SNS_GAP_CONFIRM_MINUTES", 5)
            if _pending_sns_gap is None:
                _pending_sns_gap = {"first_seen": now, "gap": sns_gap}
                logger.warning(
                    "relay_ses_gap_pending kind=%s sns_published=%s relay_persisted=%s",
                    sns_gap["kind"],
                    sns_gap["sns_published"],
                    sns_gap["relay_persisted"],
                )
            elif (now - _pending_sns_gap["first_seen"]) >= timedelta(minutes=confirm_minutes):
                alerts.append(sns_gap)
                _pending_sns_gap = None
            else:
                _pending_sns_gap["gap"] = sns_gap
                logger.info(
                    "relay_ses_gap_confirming kind=%s sns_published=%s relay_persisted=%s",
                    sns_gap["kind"],
                    sns_gap["sns_published"],
                    sns_gap["relay_persisted"],
                )
    else:
        if _pending_sns_gap is not None:
            logger.info("relay_ses_gap_resolved kind=ses_sns_persisted_gap")
        _pending_sns_gap = None

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
