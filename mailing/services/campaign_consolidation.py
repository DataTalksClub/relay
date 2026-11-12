"""Consolidate several already-sent campaign batches into one canonical campaign.

A newsletter issue sent gradually as several batch campaigns becomes one
campaign: recipient rows and their email events move to the canonical
campaign; source campaign rows are kept (never deleted), marked
``merged_into`` and hidden from campaign lists, with their pre-merge
counters/status saved in ``merge_snapshot``.

Guarantees:

* No sending. Only ``campaign_id`` columns and campaign counters change, via
  plain ``UPDATE`` statements; no task is enqueued, no status that a sender
  or recovery job acts on is created. Merges refuse campaigns that still have
  pending/sending/unknown recipients or are not finished (sent/cancelled).
* No duplicate recipients. ``(campaign, contact)`` stays unique: when one
  contact appears in several batches (a skipped row later resent by another
  batch) the row that was actually sent moves; the unsent duplicate stays on
  its source campaign as history. Two *sent* rows for one contact abort.
* Reversible. Every moved recipient/event records ``merged_from_campaign_id``;
  ``rollback_merge`` moves them back (including events recorded after the
  merge, via their recipient) and restores the snapshots.
* Totals reconcile. Counters are recomputed from recipient rows, which is how
  the live counters are maintained anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, Max, OuterRef, Q, Subquery, Sum

from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    EmailEvent,
)

FINISHED_STATUSES = {CampaignStatus.SENT, CampaignStatus.CANCELLED}
UNSETTLED_RECIPIENT_STATUSES = [
    CampaignRecipientStatus.PENDING,
    CampaignRecipientStatus.SENDING,
    CampaignRecipientStatus.UNKNOWN,
]
SNAPSHOT_FIELDS = (
    "status",
    "sent_at",
    "recipient_count",
    "sent_count",
    "skipped_count",
    "delivered_count",
    "unique_open_count",
    "open_count",
    "unique_click_count",
    "click_count",
    "unsubscribe_count",
    "bounce_count",
    "complaint_count",
)
CHUNK = 2000


class CampaignMergeError(Exception):
    pass


@dataclass
class MergePlan:
    canonical_id: int
    source_ids: list[int]
    move_recipient_ids: dict[int, list[int]] = field(default_factory=dict)
    kept_duplicate_ids: dict[int, list[int]] = field(default_factory=dict)

    @property
    def moved_count(self):
        return sum(len(v) for v in self.move_recipient_ids.values())

    @property
    def kept_count(self):
        return sum(len(v) for v in self.kept_duplicate_ids.values())


def _snapshot(campaign):
    data = {}
    for name in SNAPSHOT_FIELDS:
        value = getattr(campaign, name)
        data[name] = value.isoformat() if hasattr(value, "isoformat") else value
    return data


def _validate(canonical, sources):
    if not sources:
        raise CampaignMergeError("no source campaigns given")
    if canonical.pk in {s.pk for s in sources}:
        raise CampaignMergeError("canonical campaign is also listed as a source")
    for campaign in [canonical, *sources]:
        if campaign.merged_into_id is not None:
            raise CampaignMergeError(f"campaign {campaign.pk} is already merged into {campaign.merged_into_id}")
        if campaign.status not in FINISHED_STATUSES:
            raise CampaignMergeError(f"campaign {campaign.pk} is {campaign.status}; only sent/cancelled can merge")
        if campaign.client_id != canonical.client_id or campaign.audience_id != canonical.audience_id:
            raise CampaignMergeError(f"campaign {campaign.pk} has a different client/audience than {canonical.pk}")
    unsettled = (
        CampaignRecipient.objects.filter(
            campaign_id__in=[canonical.pk, *[s.pk for s in sources]], status__in=UNSETTLED_RECIPIENT_STATUSES
        )
        .values("campaign_id")
        .annotate(n=Count("id"))
    )
    problems = {row["campaign_id"]: row["n"] for row in unsettled}
    if problems:
        raise CampaignMergeError(f"unsettled (pending/sending/unknown) recipients: {problems}")
    if Campaign.objects.filter(merged_into_id__in=[s.pk for s in sources]).exists():
        raise CampaignMergeError("a source campaign is itself a merge target")


def build_plan(canonical, sources) -> MergePlan:
    """Choose which source recipient rows move; never two rows for one contact."""
    _validate(canonical, sources)
    plan = MergePlan(canonical_id=canonical.pk, source_ids=[s.pk for s in sources])
    canonical_contacts = set(
        CampaignRecipient.objects.filter(campaign_id=canonical.pk).values_list("contact_id", flat=True)
    )
    by_contact: dict[int, list[tuple]] = {}
    rows = CampaignRecipient.objects.filter(campaign_id__in=plan.source_ids).values_list(
        "id", "campaign_id", "contact_id", "sent_at"
    )
    for rid, cid, contact_id, sent_at in rows.iterator(chunk_size=5000):
        by_contact.setdefault(contact_id, []).append((rid, cid, sent_at))
    for source_id in plan.source_ids:
        plan.move_recipient_ids[source_id] = []
        plan.kept_duplicate_ids[source_id] = []
    for contact_id, entries in by_contact.items():
        sent_entries = [e for e in entries if e[2] is not None]
        if contact_id in canonical_contacts:
            if sent_entries:
                raise CampaignMergeError(
                    f"contact {contact_id} was sent by source {sent_entries[0][1]} and is already in "
                    f"canonical {canonical.pk}; refusing to merge two sends"
                )
            winner = None
        elif len(sent_entries) > 1:
            raise CampaignMergeError(
                f"contact {contact_id} was sent by several sources {[e[1] for e in sent_entries]}"
            )
        elif sent_entries:
            winner = sent_entries[0]
        else:
            winner = min(entries, key=lambda e: e[0])
        for entry in entries:
            target = plan.move_recipient_ids if entry is winner else plan.kept_duplicate_ids
            target[entry[1]].append(entry[0])
    return plan


def recompute_counts(campaign_id):
    """Recompute every counter of one campaign from its recipient rows."""
    totals = CampaignRecipient.objects.filter(campaign_id=campaign_id).aggregate(
        recipients=Count("pk"),
        sent=Count("pk", filter=Q(sent_at__isnull=False)),
        skipped=Count("pk", filter=Q(status=CampaignRecipientStatus.SKIPPED)),
        delivered=Count("pk", filter=Q(delivered_at__isnull=False)),
        bounced=Count("pk", filter=Q(status=CampaignRecipientStatus.BOUNCED)),
        complained=Count("pk", filter=Q(status=CampaignRecipientStatus.COMPLAINED)),
        unsubscribed=Count("pk", filter=Q(status=CampaignRecipientStatus.UNSUBSCRIBED)),
        unique_opens=Count("pk", filter=Q(first_opened_at__isnull=False)),
        unique_clicks=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        opens=Sum("open_count"),
        clicks=Sum("click_count"),
        last_sent=Max("sent_at"),
    )
    counts = {
        "recipient_count": totals["recipients"],
        "sent_count": totals["sent"],
        "skipped_count": totals["skipped"],
        "delivered_count": totals["delivered"],
        "bounce_count": totals["bounced"],
        "complaint_count": totals["complained"],
        "unsubscribe_count": totals["unsubscribed"],
        "unique_open_count": totals["unique_opens"],
        "unique_click_count": totals["unique_clicks"],
        "open_count": totals["opens"] or 0,
        "click_count": totals["clicks"] or 0,
    }
    Campaign.objects.filter(pk=campaign_id).update(**counts)
    return counts, totals["last_sent"]


def campaign_totals(campaign_ids):
    """Row-level totals across campaigns (for reconciliation before/after)."""
    rec = CampaignRecipient.objects.filter(campaign_id__in=campaign_ids).aggregate(
        rows=Count("pk"),
        contacts=Count("contact_id", distinct=True),
        sent=Count("pk", filter=Q(sent_at__isnull=False)),
        delivered=Count("pk", filter=Q(delivered_at__isnull=False)),
        bounced=Count("pk", filter=Q(status=CampaignRecipientStatus.BOUNCED)),
        complained=Count("pk", filter=Q(status=CampaignRecipientStatus.COMPLAINED)),
        unsubscribed=Count("pk", filter=Q(status=CampaignRecipientStatus.UNSUBSCRIBED)),
        unique_opens=Count("pk", filter=Q(first_opened_at__isnull=False)),
        unique_clicks=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        opens=Sum("open_count"),
        clicks=Sum("click_count"),
    )
    events = dict(
        EmailEvent.objects.filter(campaign_id__in=campaign_ids)
        .values_list("event_type")
        .annotate(n=Count("id"))
        .order_by()
    )
    rec["opens"] = rec["opens"] or 0
    rec["clicks"] = rec["clicks"] or 0
    rec["events"] = {k: events[k] for k in sorted(events)}
    rec["click_urls"] = (
        EmailEvent.objects.filter(campaign_id__in=campaign_ids, event_type="click").exclude(url="").count()
    )
    return rec


def _move_events_to_canonical(canonical_id, source_ids):
    """Move source events whose recipient now lives on canonical (or with no recipient)."""
    moved = 0
    for source_id in source_ids:
        qs = EmailEvent.objects.filter(campaign_id=source_id).filter(
            Q(campaign_recipient__isnull=True) | Q(campaign_recipient__campaign_id=canonical_id)
        )
        moved += qs.update(campaign_id=canonical_id, merged_from_campaign_id=source_id)
    return moved


def merge_campaigns(canonical_id, source_ids, *, apply=False):
    """Plan (and with ``apply=True`` perform) the merge. Returns a report dict."""
    all_ids = [canonical_id, *source_ids]
    with transaction.atomic():
        locked = {c.pk: c for c in Campaign.objects.select_for_update().filter(pk__in=all_ids).order_by("pk")}
        missing = sorted(set(all_ids) - set(locked))
        if missing:
            raise CampaignMergeError(f"campaigns not found: {missing}")
        canonical = locked[canonical_id]
        sources = [locked[i] for i in source_ids]
        before = campaign_totals(all_ids)
        plan = build_plan(canonical, sources)
        report = {
            "canonical": {"id": canonical.pk, "external_key": canonical.external_key},
            "sources": [{"id": s.pk, "external_key": s.external_key, "status": s.status} for s in sources],
            "move_recipients": {str(k): len(v) for k, v in plan.move_recipient_ids.items()},
            "kept_unsent_duplicates": {str(k): len(v) for k, v in plan.kept_duplicate_ids.items() if v},
            "before": before,
            "applied": False,
        }
        if not apply:
            transaction.set_rollback(True)
            return report

        snapshots = {c.pk: _snapshot(c) for c in [canonical, *sources]}
        for source_id, ids in plan.move_recipient_ids.items():
            for start in range(0, len(ids), CHUNK):
                CampaignRecipient.objects.filter(pk__in=ids[start : start + CHUNK], campaign_id=source_id).update(
                    campaign_id=canonical_id, merged_from_campaign_id=source_id
                )
        report["moved_events"] = _move_events_to_canonical(canonical_id, source_ids)
        for source in sources:
            Campaign.objects.filter(pk=source.pk).update(
                merged_into_id=canonical_id,
                merge_snapshot={"before_merge": snapshots[source.pk], "merged_into": canonical_id},
            )
            recompute_counts(source.pk)
        _, last_sent = recompute_counts(canonical_id)
        canonical_snapshot = dict(canonical.merge_snapshot or {})
        canonical_snapshot["before_merge"] = snapshots[canonical_id]
        canonical_snapshot["merged_sources"] = list(source_ids)
        Campaign.objects.filter(pk=canonical_id).update(
            merge_snapshot=canonical_snapshot,
            status=CampaignStatus.SENT,
            sent_at=max(filter(None, [canonical.sent_at, last_sent]), default=canonical.sent_at),
        )
        report["after"] = campaign_totals(all_ids)
        report["after_canonical_only"] = campaign_totals([canonical_id])
        report["applied"] = True
        _assert_reconciled(report["before"], report["after"])
    return report


def _assert_reconciled(before, after):
    keys = ("rows", "contacts", "sent", "delivered", "bounced", "complained", "unsubscribed",
            "unique_opens", "unique_clicks", "opens", "clicks", "events", "click_urls")
    diff = {k: (before[k], after[k]) for k in keys if before[k] != after[k]}
    if diff:
        raise CampaignMergeError(f"totals changed across the merge set: {diff}")


def settle_merge(canonical_id):
    """Sweep events/counters written to source campaigns by in-flight requests during the merge."""
    source_ids = list(Campaign.objects.filter(merged_into_id=canonical_id).values_list("pk", flat=True))
    with transaction.atomic():
        moved = _move_events_to_canonical(canonical_id, source_ids)
        for source_id in source_ids:
            recompute_counts(source_id)
        recompute_counts(canonical_id)
    return {"canonical": canonical_id, "sources": source_ids, "moved_events": moved}


def rollback_merge(canonical_id):
    """Undo ``merge_campaigns``: move rows/events back and restore snapshots."""
    with transaction.atomic():
        canonical = Campaign.objects.select_for_update().get(pk=canonical_id)
        sources = list(Campaign.objects.select_for_update().filter(merged_into_id=canonical_id).order_by("pk"))
        if not sources:
            raise CampaignMergeError(f"campaign {canonical_id} has no merged sources")
        source_ids = [s.pk for s in sources]
        all_ids = [canonical_id, *source_ids]
        before = campaign_totals(all_ids)
        moved_back = 0
        for source_id in source_ids:
            moved_back += CampaignRecipient.objects.filter(
                campaign_id=canonical_id, merged_from_campaign_id=source_id
            ).update(campaign_id=source_id, merged_from_campaign_id=None)
        # Events follow their recipient (also events recorded after the merge).
        events_back = EmailEvent.objects.filter(
            campaign_id=canonical_id, campaign_recipient__campaign_id__in=source_ids
        ).update(campaign_id=Subquery(
            CampaignRecipient.objects.filter(pk=OuterRef("campaign_recipient_id")).values("campaign_id")[:1]
        ))
        for source_id in source_ids:
            events_back += EmailEvent.objects.filter(
                campaign_id=canonical_id, campaign_recipient__isnull=True, merged_from_campaign_id=source_id
            ).update(campaign_id=source_id)
        EmailEvent.objects.filter(merged_from_campaign_id__in=source_ids).update(merged_from_campaign_id=None)
        for campaign in [canonical, *sources]:
            snap = (campaign.merge_snapshot or {}).get("before_merge") or {}
            recompute_counts(campaign.pk)
            restore = {k: snap[k] for k in ("status", "sent_at", "recipient_count", "sent_count", "skipped_count")
                       if k in snap}
            Campaign.objects.filter(pk=campaign.pk).update(merged_into=None, merge_snapshot={}, **restore)
        after = campaign_totals(all_ids)
        _assert_reconciled(before, after)
    return {"canonical": canonical_id, "sources": source_ids, "recipients_moved_back": moved_back,
            "events_moved_back": events_back, "totals": after}
