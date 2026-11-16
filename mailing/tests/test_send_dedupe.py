"""Duplicate-send guard: identical rendered content is sent once per (client, address).

Regression for AISL campaign 22 (2026-11-16): Relay campaigns 301 and 302 carried the same
content for the same 1,382 recipients and 1,343 people received it twice.
"""

from datetime import timedelta
from io import StringIO

import pytest
from botocore.exceptions import ClientError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignTaskAlert,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    SendDedupeOverride,
    SendFingerprint,
    SendFingerprintState,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services import send_dedupe
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.transactional_sender import send_transactional_email_from_queue

pytestmark = pytest.mark.django_db

SETTINGS = {"DEFAULT_FROM_EMAIL": "news@example.com", "PUBLIC_BASE_URL": "https://mail.example.com"}


class FakeSes:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def _send(self, **params):
        if self.error:
            raise self.error
        self.calls.append(params)
        return {"MessageId": f"ses-{len(self.calls)}"}

    send_email = send_raw_email = _send


def _world():
    organization = Organization.objects.create(name="DTC", slug="dtc")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="AISHL", slug="aishl")
    return audience, client


def _campaign(audience, client, emails, *, subject="Campaign 22", html="<p>Hello</p>"):
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject=subject, status="queued", html_body=html, text_body="Hello",
    )
    recipients = []
    for email in emails:
        contact, _ = Contact.objects.get_or_create(email=email.lower(), defaults={"verified_at": timezone.now()})
        Subscription.objects.get_or_create(
            contact=contact, audience=audience, client=client, defaults={"status": SubscriptionStatus.SUBSCRIBED},
        )
        recipients.append(CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=email))
    return campaign, recipients


def _send(campaign, recipients, ses):
    return send_campaign_batch(
        {
            "contract": "campaign-email", "version": 1, "campaign_id": campaign.id,
            "batch_id": f"campaign-{campaign.id}-batch-1", "campaign_recipient_ids": [r.id for r in recipients],
        },
        ses_client=ses,
    )


EMAILS = [f"person{i}@example.com" for i in range(5)]


@override_settings(**SETTINGS)
def test_c22_second_campaign_with_same_content_is_blocked_for_every_sent_recipient():
    """Relay 301 sends; Relay 302 (same client, same content, same people) must not reach SES."""
    audience, client = _world()
    first, first_recipients = _campaign(audience, client, EMAILS)
    second, second_recipients = _campaign(audience, client, EMAILS)  # identical subject/html/text
    ses = FakeSes()

    assert _send(first, first_recipients, ses).sent_count == 5
    assert len(ses.calls) == 5
    result = _send(second, second_recipients, ses)

    assert len(ses.calls) == 5  # nothing new reached SES
    assert result.sent_count == 0
    assert result.skipped_count == 5
    for recipient in CampaignRecipient.objects.filter(campaign=second):
        assert recipient.status == CampaignRecipientStatus.SKIPPED
        assert recipient.skip_reason == CampaignRecipientSkipReason.DUPLICATE_BLOCKED
        assert recipient.ses_message_id == ""
    assert EmailEvent.objects.filter(campaign=second, event_type=EmailEventType.SKIPPED).count() == 5
    alert = CampaignTaskAlert.objects.get(campaign=second)
    assert "duplicate_blocked: 5" in alert.message
    assert not CampaignTaskAlert.objects.filter(campaign=first).exists()


@override_settings(**SETTINGS)
def test_only_already_sent_recipients_are_blocked_new_ones_still_get_the_campaign():
    audience, client = _world()
    first, first_recipients = _campaign(audience, client, EMAILS[:3])
    second, second_recipients = _campaign(audience, client, EMAILS)
    ses = FakeSes()
    _send(first, first_recipients, ses)
    _send(second, second_recipients, ses)

    assert len(ses.calls) == 3 + 2
    statuses = {r.email: r.skip_reason for r in CampaignRecipient.objects.filter(campaign=second)}
    assert [e for e, reason in statuses.items() if reason == "duplicate_blocked"] == EMAILS[:3]


@override_settings(**SETTINGS)
def test_different_content_or_different_client_is_not_a_duplicate():
    audience, client = _world()
    other_client = Client.objects.create(organization=client.organization, name="Other", slug="other")
    a, ra = _campaign(audience, client, EMAILS[:2])
    b, rb = _campaign(audience, client, EMAILS[:2], subject="Campaign 23")
    c, rc = _campaign(audience, other_client, EMAILS[:2])
    ses = FakeSes()
    for campaign, recipients in ((a, ra), (b, rb), (c, rc)):
        _send(campaign, recipients, ses)
    assert len(ses.calls) == 6


@override_settings(**SETTINGS)
def test_whitespace_case_and_per_recipient_tokens_do_not_hide_a_duplicate():
    audience, client = _world()
    first, fr = _campaign(audience, client, ["Person@Example.com"], html="<p>Hello   there</p>")
    second, sr = _campaign(audience, client, ["person@example.com"], html="<p>Hello there</p>\n")
    ses = FakeSes()
    _send(first, fr, ses)
    _send(second, sr, ses)
    assert len(ses.calls) == 1


def test_normalisation_drops_tracking_unsubscribe_and_signature_but_keeps_destination():
    sig = "a" * 64
    one = f'<a href="https://m/t/c/TOKENone?u=https%3A%2F%2Fx.io&amp;s={sig}">x</a><img src="https://m/t/o/AAA.gif"> https://m/unsubscribe/UUU1'
    two = f'<a href="https://m/t/c/TOKENtwo?u=https%3A%2F%2Fx.io&amp;s={"b" * 64}">x</a><img src="https://m/t/o/BBB.gif"> https://m/unsubscribe/UUU2'
    other = one.replace("x.io", "y.io")
    assert send_dedupe.content_hash("s", one, "t") == send_dedupe.content_hash("s", two, "t")
    assert send_dedupe.content_hash("s", one, "t") != send_dedupe.content_hash("s", other, "t")


@override_settings(**SETTINGS)
def test_merge_values_make_per_recipient_content_distinct():
    audience, client = _world()
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", html_body="<p>x</p>", text_body="x",
    )
    one = send_dedupe.campaign_content_hash(campaign, {"name": "Ann"}, {})
    two = send_dedupe.campaign_content_hash(campaign, {"name": "Bob"}, {})
    assert one == two  # no {{name}} in the template: values do not matter
    campaign.html_body = "<p>{{name}}</p>"
    campaign.text_body = "{{name}}"
    assert send_dedupe.campaign_content_hash(campaign, {"name": "Ann"}, {}) != send_dedupe.campaign_content_hash(
        campaign, {"name": "Bob"}, {}
    )


@override_settings(**SETTINGS)
def test_transient_ses_failure_releases_the_slot_so_the_retry_is_not_a_duplicate():
    audience, client = _world()
    campaign, recipients = _campaign(audience, client, EMAILS[:1])
    throttle = ClientError({"Error": {"Code": "Throttling"}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "SendEmail")
    try:
        _send(campaign, recipients, FakeSes(error=throttle))
    except Exception:  # noqa: BLE001 - retryable error is raised to the task runner
        pass
    assert SendFingerprint.objects.get().state == SendFingerprintState.RELEASED
    ses = FakeSes()
    _send(campaign, recipients, ses)
    assert len(ses.calls) == 1
    assert SendFingerprint.objects.get().state == SendFingerprintState.SENT


@override_settings(**SETTINGS)
def test_permanent_rejection_releases_the_slot_but_unknown_outcome_never_does():
    audience, client = _world()
    campaign, recipients = _campaign(audience, client, EMAILS[:1])
    reject = ClientError({"Error": {"Code": "MessageRejected"}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "SendEmail")
    _send(campaign, recipients, FakeSes(error=reject))
    assert SendFingerprint.objects.get().state == SendFingerprintState.RELEASED

    other, other_recipients = _campaign(audience, client, EMAILS[1:2], subject="Other")
    unknown = ClientError({"Error": {"Code": "InternalFailure"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "SendEmail")
    _send(other, other_recipients, FakeSes(error=unknown))
    assert SendFingerprint.objects.get(email=EMAILS[1]).state == SendFingerprintState.RESERVED
    again, again_recipients = _campaign(audience, client, EMAILS[1:2], subject="Other")
    ses = FakeSes()
    _send(again, again_recipients, ses)
    assert ses.calls == []  # ambiguous first send: not auto-resent


@override_settings(**SETTINGS)
def test_window_expiry_allows_a_legitimate_repeat():
    audience, client = _world()
    first, fr = _campaign(audience, client, EMAILS[:1])
    second, sr = _campaign(audience, client, EMAILS[:1])
    ses = FakeSes()
    _send(first, fr, ses)
    SendFingerprint.objects.update(sent_at=timezone.now() - timedelta(days=31))
    _send(second, sr, ses)
    assert len(ses.calls) == 2
    assert SendFingerprint.objects.count() == 1  # row re-used (unique index), not duplicated


@override_settings(**SETTINGS)
def test_audited_campaign_override_allows_resend_and_counts_uses():
    audience, client = _world()
    first, fr = _campaign(audience, client, EMAILS[:2])
    second, sr = _campaign(audience, client, EMAILS[:2])
    ses = FakeSes()
    _send(first, fr, ses)
    out = StringIO()
    call_command(
        "allow_duplicate_send", client="aishl", campaign=second.id, by="alexey", reason="approved resend", stdout=out,
    )
    _send(second, sr, ses)
    assert len(ses.calls) == 4
    override = SendDedupeOverride.objects.get()
    assert (override.uses, override.created_by, override.reason) == (2, "alexey", "approved resend")


def test_override_is_never_client_wide():
    _world()
    with pytest.raises(CommandError):
        call_command("allow_duplicate_send", client="aishl", by="a", reason="x")


@override_settings(**SETTINGS)
def test_concurrent_reservation_has_exactly_one_winner():
    audience, client = _world()
    chash = send_dedupe.content_hash("s", "h", "t")
    candidates = [send_dedupe.Candidate(key=i, email="p@example.com", content_hash=chash) for i in (1, 2, 3)]
    reserved, blocked = send_dedupe.reserve(client.id, candidates, kind="campaign", window_seconds=None)
    assert reserved == [1] and set(blocked) == {2, 3}
    # A second sender racing for the same slot loses; the owner re-reserving itself is idempotent.
    again, blocked_again = send_dedupe.reserve(client.id, candidates[:1], kind="campaign", window_seconds=None)
    assert again == [1] and not blocked_again
    other, other_blocked = send_dedupe.reserve(client.id, candidates[1:2], kind="campaign", window_seconds=None)
    assert other == [] and set(other_blocked) == {2}


# --- transactional -------------------------------------------------------------------------------


def _tx_message(client, *, key, template, email="person@example.com", html="<p>Receipt #1</p>"):
    contact, _ = Contact.objects.get_or_create(email=email)
    return TransactionalMessage.objects.create(
        client=client, contact=contact, email=contact.normalized_email, template=template,
        template_key=template.key, status=TransactionalMessageStatus.QUEUED, idempotency_key=key,
        subject="Receipt", html_body=html, text_body="Receipt #1",
    )


def _payload(message):
    return {
        "transactional_message_id": message.id, "client_id": message.client_id,
        "idempotency_key": message.idempotency_key,
    }


@pytest.fixture
def tx_template():
    _, client = _world()
    return EmailTemplate.objects.create(client=client, key="receipt", name="Receipt", subject="s", html_body="h", text_body="t")


@override_settings(**SETTINGS)
def test_transactional_identical_content_inside_window_is_blocked_and_visible(tx_template):
    ses = FakeSes()
    one = _tx_message(tx_template.client, key="a", template=tx_template)
    two = _tx_message(tx_template.client, key="b", template=tx_template)
    send_transactional_email_from_queue(_payload(one), client=ses, source="news@example.com")
    result = send_transactional_email_from_queue(_payload(two), client=ses, source="news@example.com")

    assert len(ses.calls) == 1
    assert result.status == TransactionalMessageStatus.DUPLICATE_BLOCKED
    event = EmailEvent.objects.get(event_type=EmailEventType.DUPLICATE_BLOCKED)
    assert event.metadata["first_transactional_message_id"] == one.id
    # a redelivery of the blocked message is acknowledged, not re-evaluated
    send_transactional_email_from_queue(_payload(two), client=ses, source="news@example.com")
    assert EmailEvent.objects.filter(event_type=EmailEventType.DUPLICATE_BLOCKED).count() == 1


@override_settings(**SETTINGS)
def test_transactional_template_with_zero_window_allows_identical_repeats_like_otp(tx_template):
    tx_template.dedupe_window_seconds = 0
    tx_template.save()
    ses = FakeSes()
    for key in ("a", "b"):
        send_transactional_email_from_queue(
            _payload(_tx_message(tx_template.client, key=key, template=tx_template)), client=ses, source="news@example.com",
        )
    assert len(ses.calls) == 2
    assert SendFingerprint.objects.count() == 0


@override_settings(**SETTINGS)
def test_transactional_repeat_after_window_and_different_recipient_are_allowed(tx_template):
    ses = FakeSes()
    send_transactional_email_from_queue(
        _payload(_tx_message(tx_template.client, key="a", template=tx_template)), client=ses, source="news@example.com",
    )
    send_transactional_email_from_queue(
        _payload(_tx_message(tx_template.client, key="c", template=tx_template, email="other@example.com")),
        client=ses, source="news@example.com",
    )
    SendFingerprint.objects.update(sent_at=timezone.now() - timedelta(hours=1))
    send_transactional_email_from_queue(
        _payload(_tx_message(tx_template.client, key="b", template=tx_template)), client=ses, source="news@example.com",
    )
    assert len(ses.calls) == 3


@override_settings(**SETTINGS)
def test_transactional_retry_of_same_message_after_transient_failure_is_not_a_duplicate(tx_template):
    from mailing.services.transactional_sender import TransientSendFailure  # noqa: PLC0415

    message = _tx_message(tx_template.client, key="a", template=tx_template)
    boom = ClientError({"Error": {"Code": "InternalFailure"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "SendEmail")
    with pytest.raises(TransientSendFailure):
        send_transactional_email_from_queue(_payload(message), client=FakeSes(error=boom), source="news@example.com")
    ses = FakeSes()
    result = send_transactional_email_from_queue(_payload(message), client=ses, source="news@example.com")
    assert result.status == TransactionalMessageStatus.SENT
    assert len(ses.calls) == 1


@override_settings(**SETTINGS)
def test_bounce_frees_the_slot_for_a_resend(tx_template):
    from mailing.services.ses_webhooks import apply_transactional_message_update  # noqa: PLC0415

    ses = FakeSes()
    one = _tx_message(tx_template.client, key="a", template=tx_template)
    send_transactional_email_from_queue(_payload(one), client=ses, source="news@example.com")
    one.refresh_from_db()
    apply_transactional_message_update(one, "bounce", timezone.now(), {"bounce_type": "Transient"})
    two = _tx_message(tx_template.client, key="b", template=tx_template)
    result = send_transactional_email_from_queue(_payload(two), client=ses, source="news@example.com")
    assert result.status == TransactionalMessageStatus.SENT


@override_settings(**SETTINGS, DEDUPE_ENABLED=False)
def test_kill_switch_disables_the_guard(tx_template):
    ses = FakeSes()
    for key in ("a", "b"):
        send_transactional_email_from_queue(
            _payload(_tx_message(tx_template.client, key=key, template=tx_template)), client=ses, source="news@example.com",
        )
    assert len(ses.calls) == 2


@override_settings(**SETTINGS)
def test_test_sends_bypass_the_guard_and_do_not_consume_the_slot():
    from mailing.services.campaign_sender import send_campaign_test_message  # noqa: PLC0415

    audience, client = _world()
    campaign, recipients = _campaign(audience, client, EMAILS[:1])
    ses = FakeSes()
    send_campaign_test_message(campaign, EMAILS[0], ses_client=ses, source="news@example.com")
    assert SendFingerprint.objects.count() == 0
    _send(campaign, recipients, ses)
    assert len(ses.calls) == 2


def test_purge_removes_old_settled_rows_only():
    audience, client = _world()
    old = timezone.now() - timedelta(days=200)
    SendFingerprint.objects.create(client=client, email="a@x.io", content_hash="1" * 64, state="sent", reserved_at=old)
    SendFingerprint.objects.create(client=client, email="b@x.io", content_hash="2" * 64, state="reserved", reserved_at=old)
    SendFingerprint.objects.create(client=client, email="c@x.io", content_hash="3" * 64, state="sent", reserved_at=timezone.now())
    call_command("purge_send_fingerprints", stdout=StringIO())
    assert sorted(SendFingerprint.objects.values_list("email", flat=True)) == ["b@x.io", "c@x.io"]
