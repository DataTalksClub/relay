"""Campaign writers acquire the parent before the recipient row lock.

Exceptions that lock only the recipient: SES webhooks and recipient tracking
(opens, clicks, unsubscribes); their counters refresh after commit.

SQLite cannot reproduce PostgreSQL deadlocks; this verifies the actual evaluated
FOR UPDATE queryset order shared by sender and event/operator writers.
"""
import pytest
from django.db import connection, transaction
from django.db.models.query import QuerySet

from mailing.models import Audience, Campaign, CampaignRecipient, CampaignRecipientStatus, Client, Contact, Organization
from mailing.services.click_security import sign_click_redirect
from mailing.services.operator_management import assume_recipient_sent
from mailing.services.ses_webhooks import correlate_ses_message
from mailing.services.tokens import ensure_campaign_recipient_tokens
from mailing.services.tracking import apply_tracking_event, apply_unsubscribe, record_click, record_open

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("operation", ["open", "click", "unsubscribe", "assume-sent", "provider"])
def test_campaign_writers_lock_parent_before_recipient(monkeypatch, operation):
    organization = Organization.objects.create(name="Lock contract", slug="lock-contract")
    audience = Audience.objects.create(organization=organization, name="Readers", slug="readers")
    client = Client.objects.create(organization=organization, name="News", slug="news")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="Lock ordering")
    contact = Contact.objects.create(email="synthetic@example.invalid")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email,
                                                status=CampaignRecipientStatus.FAILED,
                                                ses_message_id="synthetic-provider-id")
    tokens = ensure_campaign_recipient_tokens(recipient)
    locks = []
    original = QuerySet._fetch_all

    def fetch(query):
        if query._result_cache is None and query.query.select_for_update and query.model in {Campaign, CampaignRecipient}:
            locks.append(query.model)
            assert connection.in_atomic_block
        return original(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    if operation in {"open", "click"}:
        if operation == "open":
            recipient_id = record_open(tokens.tracking_token)
        else:
            destination = "https://example.invalid/read"
            recipient_id = record_click(
                tokens.tracking_token,
                destination,
                sign_click_redirect(tokens.tracking_token, destination),
            )
        assert recipient_id == recipient.pk
        assert locks == []  # the web path takes no row lock at all
        event = apply_tracking_event(
            {"kind": operation, "recipient_id": recipient.pk, "occurred_at": "2026-11-09T12:00:00+00:00",
             "url": "https://example.invalid/read"}
        )
        result = event.campaign_recipient
    elif operation == "unsubscribe":
        result, _outcome = apply_unsubscribe(tokens.unsubscribe_token)
    elif operation == "assume-sent":
        result = assume_recipient_sent(actor=None, campaign=campaign, recipient=recipient)
    else:
        with transaction.atomic():
            result = correlate_ses_message("synthetic-provider-id")
    assert result.pk == recipient.pk
    if operation in {"provider", "open", "click", "unsubscribe"}:
        # These lock only the recipient: a per-event campaign FOR UPDATE starved
        # the senders' FOR SHARE and pinned every web thread (2026-11-09).
        assert locks == [CampaignRecipient]
    else:
        assert locks[:2] == [Campaign, CampaignRecipient]
    recipient.refresh_from_db()
    if operation == "open":
        assert recipient.open_count == 1
    elif operation == "click":
        assert recipient.click_count == 1
    elif operation == "unsubscribe":
        assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED
    elif operation == "assume-sent":
        assert recipient.status == CampaignRecipientStatus.SENT
