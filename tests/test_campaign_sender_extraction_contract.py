"""Public campaign bytes and locked effect ordering before/after extraction."""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from django.db import connection
from django.db.models.query import QuerySet
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import campaign_sender as sender
from mailing.services.public_urls import click_redirect_url

pytestmark = pytest.mark.django_db(transaction=True)

PRIVATE_CANARIES = ("private-recipient", "private-body", "private-context", "private-token", "private-credential")


@pytest.fixture(autouse=True)
def settings_for_campaign(settings):
    settings.PUBLIC_BASE_URL = "https://relay.example.invalid"
    settings.DEFAULT_FROM_EMAIL = "global@relay.example.invalid"
    settings.AWS_SES_CONFIGURATION_SET = "synthetic-events"
    settings.SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND = 0


@pytest.fixture
def pending():
    organization = Organization.objects.create(name="Synthetic", slug="synthetic")
    audience = Audience.objects.create(organization=organization, name="Audience", slug="audience")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    campaign = Campaign.objects.create(
        client=client, audience=audience, subject="Café", html_body="<p>Café</p>", text_body="Café",
        status=CampaignStatus.QUEUED,
    )
    contact = Contact.objects.create(email="recipient@example.invalid", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED,
    )
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

    def send_raw_email(self, **parameters):
        return self.send_email(**parameters)


def payload(pending):
    campaign, recipient = pending
    return {"campaign_id": campaign.pk, "campaign_recipient_ids": [recipient.pk]}


def test_public_rendering_preserves_exact_markup_and_actions():
    authored = '<!DOCTYPE html><p data-label="café &amp; tea">café &amp; &#169;<!--keep--><a href="https://example.invalid/read?a=1&amp;b=2">read</a><a href="/relative">rel</a><a href="mailto:help@example.invalid">mail</a><br/></p>'
    click_href = click_redirect_url("track", "https://example.invalid/read?a=1&b=2").replace("&", "&amp;")
    expected = f'<!DOCTYPE html><p data-label="café &amp; tea">café &amp; &#169;<!--keep--><a ses:no-track href="{click_href}">read</a><a href="/relative">rel</a><a href="mailto:help@example.invalid">mail</a><br /></p>'
    footer = '<p><a ses:no-track href="https://relay.example.invalid/unsubscribe/unsub">Unsubscribe or manage preferences</a></p>'
    pixel = '<img src="https://relay.example.invalid/t/o/track.gif" width="1" height="1" alt="" />'
    campaign = SimpleNamespace(subject="Café", preview_text="Preview", html_body=authored, text_body="Café  \n\n", unsubscribe_url_template="")
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
        == '\n<p><a ses:no-track href="https://relay.example.invalid/unsubscribe/unsub">Unsubscribe or manage preferences</a></p>\n<img src="https://relay.example.invalid/t/o/track.gif" width="1" height="1" alt="" />'
    )


@pytest.mark.parametrize("message_id", ["synthetic-id", ""])
def test_provider_return_including_empty_id_keeps_state_events_counts_and_no_row_locks(
    pending, monkeypatch, message_id
):
    provider = Provider(message_id)
    locks = []
    original = QuerySet.select_for_update

    def locking(query, *args, **kwargs):
        locks.append(query.model)
        return original(query, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", locking)
    monkeypatch.setattr(sender, "default_ses_client", lambda: provider)
    result = sender.send_campaign_batch(payload(pending))
    campaign, recipient = pending
    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert (result.sent_count, result.skipped_count, result.failed_count) == (1, 0, 0)
    # Claim-send-finalize: the send path takes no row lock at all (a claim is a conditional UPDATE).
    assert locks == []
    assert len(provider.calls) == 1
    assert recipient.status == "sent" and recipient.ses_message_id == message_id
    assert recipient.sent_at is not None and recipient.last_error == ""
    assert recipient.claimed_at is not None and recipient.claim_owner
    assert recipient.tracking_token_hash and recipient.unsubscribe_token_hash
    assert campaign.sent_count == 1
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == "sent" and event.metadata == {"ses_message_id": message_id}
    assert provider.calls[0]["Source"] == "global@relay.example.invalid"


def test_cancelled_campaign_records_skip_and_never_calls_provider(pending):
    campaign, recipient = pending
    campaign.status = "cancelled"
    campaign.save(update_fields=["status"])
    provider = Provider()
    result = sender.send_campaign_batch(payload(pending), ses_client=provider)
    recipient.refresh_from_db()
    assert result.skipped_count == 1 and provider.calls == []
    assert recipient.status == "skipped" and recipient.last_error == "campaign_cancelled"
    # The claim minted tokens; a skipped row drops them since no message carries them.
    assert recipient.tracking_token_hash is None and recipient.unsubscribe_token_hash is None
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == "skipped" and event.metadata == {"reason": "campaign_cancelled"}


def test_failed_finalize_leaves_row_sending_and_fails_loudly(pending, monkeypatch, caplog):
    provider = Provider()

    def fail_write(*args, **kwargs):
        raise RuntimeError("synthetic finalize failure")

    monkeypatch.setattr(sender, "_write_outcomes", fail_write)
    monkeypatch.setattr(sender.time, "sleep", lambda seconds: None)
    with pytest.raises(sender.CampaignSenderError, match="could not record 1 send outcomes"):
        sender.send_campaign_batch(payload(pending), ses_client=provider)
    campaign, recipient = pending
    recipient.refresh_from_db()
    # The message was accepted and is never resent: the row waits for lease expiry -> UNKNOWN.
    assert len(provider.calls) == 1
    assert recipient.status == "sending" and recipient.ses_message_id == ""
    assert EmailEvent.objects.count() == 0
    assert "campaign_finalize_lost" in caplog.text and f"{recipient.pk}=synthetic-id" in caplog.text


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


class FailingProvider(Provider):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def send_email(self, **parameters):
        super().send_email(**parameters)
        try:
            raise RuntimeError(PRIVATE_CANARIES[-1])
        except RuntimeError as cause:
            raise self.error from cause


def provider_error(kind):
    message = " ".join(PRIVATE_CANARIES)
    if kind == "timeout":
        return TimeoutError(message)
    if kind == "unexpected":
        return RuntimeError(message)
    return ClientError({"Error": {"Code": kind, "Message": message}}, "SendEmail")


@pytest.mark.parametrize(
    "kind,outcome",
    [("timeout", "unknown"), ("MessageRejected", "permanent"), ("unexpected", "permanent")],
)
def test_provider_failure_logs_safe_stack_and_preserves_outcome(pending, monkeypatch, caplog, kind, outcome):
    campaign, recipient = pending
    campaign.html_body = PRIVATE_CANARIES[1]
    campaign.save(update_fields=["html_body"])
    monkeypatch.setattr(sender, "generate_raw_token", lambda: PRIVATE_CANARIES[3])
    error = provider_error(kind)
    provider = FailingProvider(error)
    if outcome == "retryable":
        with pytest.raises(sender.RetryableCampaignSendError) as caught:
            sender.send_campaign_batch(payload(pending), ses_client=provider)
        assert caught.value.__cause__ is error and str(caught.value) == str(error)
    elif outcome == "unknown":
        result = sender.send_campaign_batch(payload(pending), ses_client=provider)
        assert (result.sent_count, result.skipped_count, result.failed_count, result.unknown_count) == (0, 0, 0, 1)
    else:
        result = sender.send_campaign_batch(payload(pending), ses_client=provider)
        assert (result.sent_count, result.skipped_count, result.failed_count) == (0, 0, 1)
    assert len(provider.calls) == 1
    assert_failure_state(campaign, recipient, error, outcome)
    assert_safe_diagnostic(caplog, campaign, recipient, outcome)


def test_throttling_gets_exactly_one_repermitted_retry(pending, settings, caplog):
    """WP2: our layer re-acquires a permit before retrying a Throttling error."""
    settings.SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND = 45.0
    campaign, recipient = pending
    error = provider_error("Throttling")
    provider = FailingProvider(error)
    with pytest.raises(sender.RetryableCampaignSendError):
        sender.send_campaign_batch(payload(pending), ses_client=provider)
    assert len(provider.calls) == 2
    assert_failure_state(campaign, recipient, error, "retryable")
    assert_safe_diagnostic(caplog, campaign, recipient, "retryable")
    feedback = [r for r in caplog.records if r.name == "mailing.ses_rate_limiter"]
    assert any("ses_rate_limit_throttling_feedback" in r.getMessage() for r in feedback)


def assert_failure_state(campaign, recipient, error, outcome):
    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert campaign.sent_count == 0 and recipient.ses_message_id == "" and recipient.sent_at is None
    if outcome == "unknown":
        # Ambiguous: never retried, tokens kept (the copy SES may have accepted carries them).
        assert recipient.status == "unknown" and recipient.last_error == "send_outcome_unknown:TimeoutError"
        assert recipient.tracking_token_hash and recipient.unsubscribe_token_hash
        assert EmailEvent.objects.count() == 0
        return
    assert recipient.last_error == str(error)
    if outcome == "retryable":
        assert recipient.status == "pending" and EmailEvent.objects.count() == 0
        assert recipient.tracking_token_hash is None and recipient.unsubscribe_token_hash is None
        assert recipient.claimed_at is None and recipient.claim_owner == ""
        return
    assert recipient.status == "failed"
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == "failed" and event.metadata == {"error": str(error)}


def assert_safe_diagnostic(caplog, campaign, recipient, outcome):
    delivery = [r for r in caplog.records if "Campaign delivery failed" in r.getMessage()]
    assert len(delivery) == 1
    record = delivery[0]
    rendered = caplog.text
    assert record.name == sender.__name__ and record.levelname == "ERROR"
    assert f"campaign_id={campaign.pk} recipient_id={recipient.pk}" in rendered
    assert f"classification={outcome}" in rendered
    assert "Traceback (most recent call last):" in rendered
    assert "campaign_sender.py" in rendered and "_deliver_campaign_recipient" in rendered
    assert "ses.py" in rendered and "send_email" in rendered
    assert "test_campaign_sender_extraction_contract.py" in rendered
    assert "RuntimeError: SES delivery error details omitted" in rendered
    assert not any(canary in rendered for canary in PRIVATE_CANARIES)
    assert recipient.email not in rendered and record.exc_info is None
    assert "direct cause" not in rendered and "During handling" not in rendered
    assert "raise self.error" not in rendered and "parameters =" not in rendered
