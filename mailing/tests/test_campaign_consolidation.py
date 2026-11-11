import json
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
)
from mailing.services import operator_ui
from mailing.services.campaign_consolidation import (
    CampaignMergeError,
    campaign_totals,
    merge_campaigns,
    rollback_merge,
)

pytestmark = pytest.mark.django_db
NOW = timezone.now()


@pytest.fixture
def scope():
    org = Organization.objects.create(name="DTC", slug="dtc-org")
    client = Client.objects.create(organization=org, name="Newsletter", slug="dtc-newsletter")
    audience = Audience.objects.create(organization=org, name="DTC", slug="dtc")
    return client, audience


def _campaign(scope, key, status=CampaignStatus.SENT):
    client, audience = scope
    return Campaign.objects.create(client=client, audience=audience, external_key=key, subject="Weekly", status=status)


def _contact(n):
    email = f"user{n}@example.com"
    return Contact.objects.get_or_create(normalized_email=email, defaults={"email": email})[0]


def _recipient(campaign, n, *, sent=True, status=None, opened=False, clicked=False):
    r = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=_contact(n),
        email=f"user{n}@example.com",
        status=status or (CampaignRecipientStatus.SENT if sent else CampaignRecipientStatus.SKIPPED),
        sent_at=NOW - timedelta(hours=1) if sent else None,
        delivered_at=NOW if sent else None,
        first_opened_at=NOW if opened else None,
        open_count=1 if opened else 0,
        first_clicked_at=NOW if clicked else None,
        click_count=1 if clicked else 0,
    )
    if sent:
        EmailEvent.objects.create(campaign=campaign, campaign_recipient=r, contact=r.contact,
                                  event_type=EmailEventType.SENT)
    if clicked:
        EmailEvent.objects.create(campaign=campaign, campaign_recipient=r, contact=r.contact,
                                  event_type=EmailEventType.CLICK, url="https://example.com/course")
    return r


@pytest.fixture
def batches(scope):
    canonical = _campaign(scope, "weekly-302")
    b2 = _campaign(scope, "weekly-302-b2")
    b3 = _campaign(scope, "weekly-302-b3", status=CampaignStatus.CANCELLED)
    b4a = _campaign(scope, "weekly-302-b4a")
    other = _campaign(scope, "unrelated")
    _recipient(canonical, 1, opened=True)
    _recipient(canonical, 2, clicked=True)
    _recipient(b2, 3, status=CampaignRecipientStatus.COMPLAINED)
    _recipient(b2, 4, clicked=True)
    _recipient(b3, 5)
    _recipient(b3, 6, sent=False)  # skipped by the hard stop, resent by b4a
    _recipient(b4a, 6, status=CampaignRecipientStatus.UNSUBSCRIBED)
    _recipient(other, 1)
    return canonical, [b2, b3, b4a], other


def test_dry_run_writes_nothing(batches):
    canonical, sources, _ = batches
    report = merge_campaigns(canonical.pk, [s.pk for s in sources])
    assert report["applied"] is False
    assert report["move_recipients"] == {str(sources[0].pk): 2, str(sources[1].pk): 1, str(sources[2].pk): 1}
    assert report["kept_unsent_duplicates"] == {str(sources[1].pk): 1}
    assert Campaign.objects.filter(merged_into__isnull=False).count() == 0
    assert CampaignRecipient.objects.filter(campaign=canonical).count() == 2


def test_merge_moves_rows_preserves_totals_and_never_sends(batches):
    canonical, sources, other = batches
    ids = [canonical.pk, *[s.pk for s in sources]]
    before = campaign_totals(ids)
    other_before = campaign_totals([other.pk])
    tasks_before = DBTaskResult.objects.count()
    with mock.patch("mailing.services.campaign_sender.send_campaign_batch", side_effect=AssertionError("send")):
        report = merge_campaigns(canonical.pk, [s.pk for s in sources], apply=True)
    assert DBTaskResult.objects.count() == tasks_before  # nothing enqueued: no send, requeue or task
    assert report["applied"] is True
    assert campaign_totals(ids) == before
    assert campaign_totals([other.pk]) == other_before

    canonical.refresh_from_db()
    rows = CampaignRecipient.objects.filter(campaign=canonical)
    assert rows.count() == 6
    assert rows.values("contact").distinct().count() == 6
    assert canonical.recipient_count == 6
    assert canonical.sent_count == 6
    assert canonical.complaint_count == 1
    assert canonical.unsubscribe_count == 1
    assert canonical.unique_click_count == 2
    assert canonical.merged_into_id is None
    assert EmailEvent.objects.filter(campaign=canonical, event_type=EmailEventType.CLICK).count() == 2
    assert EmailEvent.objects.filter(campaign=canonical, event_type=EmailEventType.SENT).count() == 6
    # The unsent duplicate stays on its source as history; nothing deleted.
    b3 = Campaign.objects.get(pk=sources[1].pk)
    assert b3.merged_into_id == canonical.pk
    assert list(b3.recipients.values_list("status", flat=True)) == ["skipped"]
    assert b3.merge_snapshot["before_merge"]["status"] == "cancelled"
    # Sources disappear from campaign lists; the canonical and unrelated campaigns stay.
    listed = set(operator_ui.campaign_queryset().values_list("pk", flat=True))
    assert listed == {canonical.pk, other.pk}
    # No recipient got a sendable status.
    assert not CampaignRecipient.objects.filter(status__in=["pending", "sending"]).exists()


def test_rollback_restores_original_layout(batches):
    canonical, sources, _ = batches
    layout = sorted(CampaignRecipient.objects.values_list("id", "campaign_id"))
    events = sorted(EmailEvent.objects.values_list("id", "campaign_id"))
    snapshot = {c.pk: (c.status, c.recipient_count, c.sent_count) for c in Campaign.objects.all()}
    merge_campaigns(canonical.pk, [s.pk for s in sources], apply=True)
    # An event recorded after the merge for a moved recipient goes back with its recipient.
    moved = CampaignRecipient.objects.get(contact__normalized_email="user4@example.com")
    late = EmailEvent.objects.create(campaign=canonical, campaign_recipient=moved, event_type=EmailEventType.OPEN)
    rollback_merge(canonical.pk)
    assert sorted(CampaignRecipient.objects.values_list("id", "campaign_id")) == layout
    late.refresh_from_db()
    assert late.campaign_id == sources[0].pk
    assert sorted(EmailEvent.objects.exclude(pk=late.pk).values_list("id", "campaign_id")) == events
    assert {c.pk: (c.status, c.recipient_count, c.sent_count) for c in Campaign.objects.all()} == snapshot
    assert not Campaign.objects.filter(merged_into__isnull=False).exists()
    assert not CampaignRecipient.objects.filter(merged_from_campaign_id__isnull=False).exists()


def test_refuses_two_sends_to_one_contact(scope):
    canonical = _campaign(scope, "a")
    source = _campaign(scope, "b")
    _recipient(canonical, 1)
    _recipient(source, 1)
    with pytest.raises(CampaignMergeError, match="refusing to merge two sends"):
        merge_campaigns(canonical.pk, [source.pk], apply=True)
    assert CampaignRecipient.objects.filter(campaign=source).count() == 1


def test_refuses_unfinished_or_unsettled(scope):
    canonical = _campaign(scope, "a")
    sending = _campaign(scope, "b", status=CampaignStatus.SENDING)
    with pytest.raises(CampaignMergeError, match="only sent/cancelled"):
        merge_campaigns(canonical.pk, [sending.pk], apply=True)
    source = _campaign(scope, "c")
    _recipient(source, 9, sent=False, status=CampaignRecipientStatus.PENDING)
    with pytest.raises(CampaignMergeError, match="unsettled"):
        merge_campaigns(canonical.pk, [source.pk], apply=True)


def test_command_requires_confirmation_and_reports_json(batches):
    canonical, sources, _ = batches
    ids = ",".join(str(s.pk) for s in sources)
    with pytest.raises(CommandError, match="confirm-sources"):
        call_command("merge_campaigns", canonical=canonical.pk, sources=ids, apply=True)
    out = StringIO()
    call_command("merge_campaigns", canonical=canonical.pk, sources=ids, apply=True, confirm_sources=ids, stdout=out)
    report = json.loads(out.getvalue())
    assert report["applied"] is True
    assert report["after_canonical_only"]["rows"] == 6
    out = StringIO()
    call_command("merge_campaigns", canonical=canonical.pk, rollback=True, stdout=out)
    assert json.loads(out.getvalue())["recipients_moved_back"] == 4
