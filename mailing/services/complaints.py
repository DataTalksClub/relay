"""SES complaint reporting: per-campaign rate, recent list, threshold alert.

Complaints are stored by the SES webhook as ``EmailEvent(event_type=complaint)``.
This module only reads them. Nothing here swallows errors: a failing query must
surface as a 500 on the dashboard or a failing scheduler tick, not an empty panel.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from mailing.models import Campaign, CampaignRecipient, EmailEvent, EmailEventType, OperatorAudit
from mailing.services.transactional_monitor import send_monitor_alert

COMPLAINT_RATE_THRESHOLD = Decimal("0.001")  # 0.1% of sent recipients, per campaign
COMPLAINT_ALERT_ACTION = "monitor.complaint_rate.reported"
COMPLAINT_QUERY_TIMEOUT_MS = 5000
RECENT_LIMIT = 50
RATE_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class ComplaintRow:
    occurred_at: object
    recipient: str
    campaign_id: int | None
    campaign_name: str
    complaint_type: str


@dataclass(frozen=True)
class CampaignComplaintRate:
    campaign_id: int
    campaign_name: str
    complaints: int
    sent: int
    rate: Decimal
    over_threshold: bool

    @property
    def rate_display(self) -> str:
        return f"{self.rate * 100:.3f}%"


@contextmanager
def _bounded_queries():
    """Cap each query at COMPLAINT_QUERY_TIMEOUT_MS. Postgres only: SET LOCAL needs a transaction."""
    if connection.vendor != "postgresql":
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", [COMPLAINT_QUERY_TIMEOUT_MS])
        yield


def _events(client=None):
    events = EmailEvent.objects.filter(event_type=EmailEventType.COMPLAINT)
    return events.filter(client=client) if client is not None else events


def recent_complaints(client=None, *, limit=RECENT_LIMIT) -> list[ComplaintRow]:
    with _bounded_queries():
        events = list(
            _events(client).select_related("contact", "campaign").order_by("-created_at", "-id")[:limit]
        )
    return [
        ComplaintRow(
            occurred_at=event.metadata.get("event_timestamp") or event.created_at,
            recipient=event.contact.email if event.contact_id else "(unknown recipient)",
            campaign_id=event.campaign_id,
            campaign_name=event.campaign.subject if event.campaign_id else "(transactional / uncorrelated)",
            complaint_type=event.metadata.get("complaint_feedback_type") or "unspecified",
        )
        for event in events
    ]


def campaign_complaint_rates(client=None, *, days=RATE_LOOKBACK_DAYS) -> list[CampaignComplaintRate]:
    since = timezone.now() - timedelta(days=days)
    with _bounded_queries():
        complaints = dict(
            _events(client)
            .filter(campaign__isnull=False, created_at__gte=since)
            .values_list("campaign_id")
            .annotate(n=Count("contact_id", distinct=True))
        )
        if not complaints:
            return []
        campaigns = Campaign.objects.filter(pk__in=complaints)
        sent = dict(
            CampaignRecipient.objects.filter(campaign__in=campaigns, sent_at__isnull=False)
            .values_list("campaign_id")
            .annotate(n=Count("id"))
        )
        names = dict(campaigns.values_list("pk", "subject"))
    rows = []
    for campaign_id, count in complaints.items():
        total = sent.get(campaign_id, 0)
        rate = Decimal(count) / Decimal(total) if total else Decimal(1)
        rows.append(CampaignComplaintRate(
            campaign_id, names.get(campaign_id, f"campaign {campaign_id}"), count, total, rate,
            rate > COMPLAINT_RATE_THRESHOLD,
        ))
    return sorted(rows, key=lambda row: (-row.rate, row.campaign_id))


def check_complaint_rate(*, now=None, test=False):
    """Alert once per campaign (and again if its complaint count grows) above the threshold."""
    now = now or timezone.now()
    alerts = []
    for row in campaign_complaint_rates():
        if not row.over_threshold:
            continue
        last = (
            OperatorAudit.objects.filter(
                action=COMPLAINT_ALERT_ACTION, target_type="campaign", target_id=row.campaign_id
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if last is not None and (last.metadata or {}).get("complaints", 0) >= row.complaints:
            continue
        alert = {
            "kind": "campaign_complaint_rate",
            "client_id": None,
            "campaign_id": row.campaign_id,
            "campaign": row.campaign_name,
            "complaints": row.complaints,
            "sent": row.sent,
            "rate": row.rate_display,
            "threshold": f"{COMPLAINT_RATE_THRESHOLD * 100:.1f}%",
        }
        send_monitor_alert(alert, test=test)
        if not test:
            OperatorAudit.objects.create(
                action=COMPLAINT_ALERT_ACTION,
                target_type="campaign",
                target_id=row.campaign_id,
                metadata={"complaints": row.complaints, "sent": row.sent, "reported_at": now.isoformat()},
            )
        alerts.append(alert)
    return alerts
