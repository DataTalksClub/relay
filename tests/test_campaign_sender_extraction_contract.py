"""Public campaign bytes and locked effect ordering before/after extraction."""

from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.models.query import QuerySet

from mailing.models import Audience, Campaign, CampaignRecipient, Client, Contact, EmailEvent, Organization
from mailing.services import campaign_sender as sender

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def settings_for_campaign(settings):
    settings.PUBLIC_BASE_URL = "https://relay.example.invalid"
    settings.DEFAULT_FROM_EMAIL = "global@relay.example.invalid"
    settings.AWS_SES_CONFIGURATION_SET = "synthetic-events"
    settings.SES_MAX_SEND_RATE_PER_SECOND = 0


@pytest.fixture
def pending():
    organization = Organization.objects.create(name="Synthetic", slug="synthetic")
    audience = Audience.objects.create(organization=organization, name="Audience", slug="audience")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    campaign = Campaign.objects.create(
        client=client, audience=audience, subject="Café", html_body="<p>Café</p>", text_body="Café"
    )
    contact = Contact.objects.create(email="recipient@example.invalid")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    return campaign, recipient


class Provider:
    def __init__(self, message_id="synthetic-id"):
        self.message_id = message_id
        self.calls = []
        self.atomic = []

    def send_email(self, **parameters):
        self.calls.append(parameters)
        self.atomic.append(connection.in_atomic_block)
        return {"MessageId": self.message_id}


def payload(pending):
    campaign, recipient = pending
    return {"campaign_id": campaign.pk, "campaign_recipient_ids": [recipient.pk]}


def test_public_rendering_preserves_exact_markup_and_actions():
    authored = '<!DOCTYPE html><p data-label="café &amp; tea">café &amp; &#169;<!--keep--><a href="https://example.invalid/read?a=1&amp;b=2">read</a><a href="/relative">rel</a><a href="mailto:help@example.invalid">mail</a><br/></p>'
    expected = '<!DOCTYPE html><p data-label="café &amp; tea">café &amp; &#169;<!--keep--><a href="https://relay.example.invalid/t/c/track?u=https%3A%2F%2Fexample.invalid%2Fread%3Fa%3D1%26b%3D2">read</a><a href="/relative">rel</a><a href="mailto:help@example.invalid">mail</a><br /></p>'
    footer = '<p><a href="https://relay.example.invalid/unsubscribe/unsub">Unsubscribe or manage preferences</a></p>'
    pixel = '<img src="https://relay.example.invalid/t/o/track.gif" width="1" height="1" alt="" />'
    campaign = SimpleNamespace(subject="Café", preview_text="Preview", html_body=authored, text_body="Café  \n\n")
    result = sender.render_campaign_message(campaign, tracking_token="track", unsubscribe_token="unsub")
    assert result == {
        "subject": "Café",
        "preview_text": "Preview",
        "html_body": f"{expected}\n{footer}\n{pixel}",
        "text_body": "Café\n\nUnsubscribe or manage preferences: https://relay.example.invalid/unsubscribe/unsub",
    }
    assert sender.rewrite_html_links(authored, "track") == expected


@pytest.mark.parametrize("body", [None, "", "  \n"])
def test_empty_plain_rendering_retains_existing_unsubscribe_only_policy(body):
    assert (
        sender.build_campaign_text_body(body, "unsub")
        == "Unsubscribe or manage preferences: https://relay.example.invalid/unsubscribe/unsub"
    )


def test_empty_html_retains_footer_and_pixel_bytes():
    assert (
        sender.build_campaign_html_body(None, "track", "unsub")
        == '\n<p><a href="https://relay.example.invalid/unsubscribe/unsub">Unsubscribe or manage preferences</a></p>\n<img src="https://relay.example.invalid/t/o/track.gif" width="1" height="1" alt="" />'
    )


@pytest.mark.parametrize("message_id", ["synthetic-id", ""])
def test_provider_return_including_empty_id_keeps_state_events_counts_and_lock(pending, monkeypatch, message_id):
    provider = Provider(message_id)
    steps = []
    original = QuerySet.select_for_update

    def locking(query, *args, **kwargs):
        if query.model is CampaignRecipient:
            steps.append(("lock", connection.in_atomic_block))
        return original(query, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", locking)
    monkeypatch.setattr(sender, "default_ses_client", lambda: provider)
    result = sender.send_campaign_batch(payload(pending))
    campaign, recipient = pending
    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert (result.sent_count, result.skipped_count, result.failed_count) == (1, 0, 0)
    assert steps and all(atomic for _, atomic in steps)
    assert provider.atomic == [True]
    assert recipient.status == "sent" and recipient.ses_message_id == message_id
    assert recipient.sent_at is not None and recipient.last_error == ""
    assert campaign.sent_count == 1
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == "sent" and event.metadata == {"ses_message_id": message_id}
    assert provider.calls[0]["Source"] == "global@relay.example.invalid"


def test_cancelled_claim_records_skip_before_tokens_or_provider(pending, monkeypatch):
    campaign, recipient = pending
    campaign.status = "cancelled"
    campaign.save(update_fields=["status"])

    def forbidden(*args):
        raise AssertionError("Cancelled claim must not prepare tokens")

    monkeypatch.setattr(sender, "_send_tokens_for_pending_recipient", forbidden)
    provider = Provider()
    result = sender.send_campaign_batch(payload(pending), ses_client=provider)
    recipient.refresh_from_db()
    assert result.skipped_count == 1 and provider.calls == []
    assert recipient.status == "skipped" and recipient.last_error == "campaign_cancelled"
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == "skipped" and event.metadata == {"reason": "campaign_cancelled"}


def test_audit_exception_rolls_back_claim_but_not_recorded_provider_effect(pending, monkeypatch):
    provider = Provider()
    original_tokens = (pending[1].tracking_token_hash, pending[1].unsubscribe_token_hash)

    def fail_event(*args, **kwargs):
        assert connection.in_atomic_block
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(sender, "_create_campaign_event", fail_event)
    assert not connection.in_atomic_block
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        sender.send_campaign_batch(payload(pending), ses_client=provider)
    campaign, recipient = pending
    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert len(provider.calls) == 1 and provider.atomic == [True]
    assert recipient.status == "pending" and recipient.ses_message_id == ""
    assert (recipient.tracking_token_hash, recipient.unsubscribe_token_hash) == original_tokens
    assert campaign.sent_count == 0 and EmailEvent.objects.count() == 0


def test_preview_calls_injectable_sender_and_keeps_token_order(pending, monkeypatch):
    campaign, recipient = pending
    tokens = iter(["track", "unsub"])
    captured = []
    provider = Provider()

    def capture(**values):
        captured.append(values)
        return "preview-id"

    monkeypatch.setattr(sender, "generate_raw_token", lambda: next(tokens))
    monkeypatch.setattr(sender, "send_email", capture)
    result = sender.send_campaign_test_message(
        campaign, "preview@example.invalid", ses_client=provider, source="explicit@example.invalid"
    )
    assert result == "preview-id" and provider.calls == []
    assert captured[0]["ses_client"] is provider
    assert captured[0]["source"] == "explicit@example.invalid"
    assert captured[0]["to_email"] == "preview@example.invalid"
    assert "/t/o/track.gif" in captured[0]["html_body"] and "/unsubscribe/unsub" in captured[0]["html_body"]
    assert captured[0]["text_body"].endswith("/unsubscribe/unsub")
    recipient.refresh_from_db()
    assert recipient.status == "pending" and recipient.ses_message_id == ""
    assert EmailEvent.objects.count() == 0
