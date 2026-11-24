"""values_update (UPDATE ... FROM VALUES) and the claim guards built on it."""

import pytest
from django.db import DataError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    Organization,
    SendFingerprint,
    SendFingerprintState,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import send_dedupe
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.db_commit import set_local_async_commit, set_local_sync_commit
from mailing.services.values_update import values_update

pytestmark = pytest.mark.django_db

postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-specific SQL")


def _campaign(count=3):
    organization = Organization.objects.create(name="DTC", slug="dtc")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="DM", slug="dm")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status="queued",
        html_body="<p>Hello</p>", text_body="Hello",
    )
    recipients = []
    for i in range(count):
        contact = Contact.objects.create(email=f"p{i}@example.com", verified_at=timezone.now())
        Subscription.objects.create(
            contact=contact, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED,
        )
        recipients.append(CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email))
    return campaign, recipients


def test_updates_each_row_with_its_own_values():
    _, recipients = _campaign()
    now = timezone.now()
    objs = [
        CampaignRecipient(pk=r.pk, status=CampaignRecipientStatus.SENT, ses_message_id=f"m-{i}", sent_at=now)
        for i, r in enumerate(recipients)
    ]
    assert values_update(CampaignRecipient, objs, ["status", "ses_message_id", "sent_at"]) == 3
    rows = {r.pk: r for r in CampaignRecipient.objects.all()}
    for i, r in enumerate(recipients):
        assert rows[r.pk].status == CampaignRecipientStatus.SENT
        assert rows[r.pk].ses_message_id == f"m-{i}"
        assert rows[r.pk].sent_at == now


def test_where_filter_limits_matched_rows_and_supports_in_and_null_values():
    _, recipients = _campaign()
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(status=CampaignRecipientStatus.SENT)
    objs = [CampaignRecipient(pk=r.pk, status=CampaignRecipientStatus.SENDING, claimed_at=None) for r in recipients]
    matched = values_update(
        CampaignRecipient, objs, ["status", "claimed_at"],
        where={"status": [CampaignRecipientStatus.PENDING, CampaignRecipientStatus.UNKNOWN]},
    )
    assert matched == 2
    assert CampaignRecipient.objects.get(pk=recipients[0].pk).status == CampaignRecipientStatus.SENT
    assert values_update(CampaignRecipient, objs, ["status"], where={"status": []}) == 0
    assert values_update(CampaignRecipient, [], ["status"]) == 0


def test_rejects_duplicate_primary_keys():
    _, recipients = _campaign(1)
    objs = [CampaignRecipient(pk=recipients[0].pk, status="sent")] * 2
    if connection.vendor == "postgresql":
        with pytest.raises(ValueError):
            values_update(CampaignRecipient, objs, ["status"])


@postgres_only
def test_overlong_text_fails_instead_of_truncating():
    _, recipients = _campaign(1)
    with pytest.raises(DataError), transaction.atomic():
        values_update(CampaignRecipient, [CampaignRecipient(pk=recipients[0].pk, status="x" * 500)], ["status"])


@postgres_only
def test_set_local_commit_mode_is_transaction_scoped():
    with transaction.atomic():
        assert set_local_async_commit() is True
        with connection.cursor() as cursor:
            cursor.execute("SHOW synchronous_commit")
            assert cursor.fetchone()[0] == "off"
        assert set_local_sync_commit() is True
        with connection.cursor() as cursor:
            cursor.execute("SHOW synchronous_commit")
            assert cursor.fetchone()[0] == "on"


class _Ses:
    def __init__(self):
        self.calls = 0

    def send_email(self, **params):
        self.calls += 1
        return {"MessageId": f"ses-{self.calls}"}

    send_raw_email = send_email


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_pending_row_with_ses_message_id_is_never_claimed_again():
    """Recovery safety: a row SES already accepted (message id recorded) is not re-sent."""
    campaign, recipients = _campaign(2)
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(ses_message_id="already-accepted")
    ses = _Ses()
    send_campaign_batch(
        {
            "contract": "campaign-email", "version": 1, "campaign_id": campaign.id,
            "batch_id": f"campaign-{campaign.id}-batch-1",
            "campaign_recipient_ids": [r.pk for r in recipients],
        },
        ses_client=ses,
    )
    assert ses.calls == 1
    first = CampaignRecipient.objects.get(pk=recipients[0].pk)
    assert first.status == CampaignRecipientStatus.PENDING
    assert first.ses_message_id == "already-accepted"
    assert CampaignRecipient.objects.get(pk=recipients[1].pk).status == CampaignRecipientStatus.SENT


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_claim_and_fingerprint_commit_together(monkeypatch):
    """If the reservation fails, the claim rolls back with it: no SENDING row without a fingerprint."""
    campaign, recipients = _campaign(2)
    monkeypatch.setattr(send_dedupe, "dedupe_enabled", lambda: True)

    def boom(*args, **kwargs):
        raise RuntimeError("reserve failed")

    monkeypatch.setattr(send_dedupe, "reserve", boom)
    ses = _Ses()
    with pytest.raises(Exception):  # noqa: B017 - the batch surfaces the internal error
        send_campaign_batch(
            {
                "contract": "campaign-email", "version": 1, "campaign_id": campaign.id,
                "batch_id": f"campaign-{campaign.id}-batch-1",
                "campaign_recipient_ids": [r.pk for r in recipients],
            },
            ses_client=ses,
        )
    assert ses.calls == 0
    statuses = set(CampaignRecipient.objects.values_list("status", flat=True))
    assert statuses == {CampaignRecipientStatus.PENDING}
    assert not SendFingerprint.objects.filter(state=SendFingerprintState.RESERVED).exists()
    assert set(CampaignRecipient.objects.values_list("claim_owner", flat=True)) == {""}
