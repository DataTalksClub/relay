"""Claim-send-finalize behaviour that runs on SQLite too (the Postgres fault tests live in
test_claim_finalize_harness.py): outcome classification, throttle/halt/SIGTERM release, lease
expiry, SES-event reconciliation, the operator list and the confirmed resend."""

import hashlib
from datetime import timedelta

import pytest
from botocore.exceptions import ConnectTimeoutError, ParamValidationError, ReadTimeoutError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import campaign_sender as sender
from mailing.services.auth import create_client_api_key
from mailing.services.send_reconcile import (
    ResendNotConfirmed,
    expire_stale_claims,
    recipient_hash,
    reconcile_unknown_from_event,
    reconcile_unknown_recipients,
    resend_unknown,
    unknown_recipients,
)
from mailing.tests.claim_finalize_harness import FaultSes, client_error

pytestmark = pytest.mark.django_db

SENDING = CampaignRecipientStatus.SENDING
UNKNOWN = CampaignRecipientStatus.UNKNOWN
PENDING = CampaignRecipientStatus.PENDING


def make_campaign(count, *, status="queued", tag="a", external_key=""):
    organization = Organization.objects.create(name="Org", slug=f"org-{tag}")
    audience = Audience.objects.create(organization=organization, name="News", slug=f"news-{tag}")
    client = Client.objects.create(organization=organization, name="Client", slug=f"client-{tag}")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status=status, html_body="<p>Hi</p>", text_body="Hi",
        external_key=external_key,
    )
    contacts = [Contact.objects.create(email=f"p{i}-{tag}@example.com", verified_at=timezone.now()) for i in range(count)]
    Subscription.objects.bulk_create(
        [Subscription(contact=c, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED) for c in contacts]
    )
    recipients = CampaignRecipient.objects.bulk_create(
        [CampaignRecipient(campaign=campaign, contact=c, email=c.email) for c in contacts]
    )
    payload = {"campaign_id": campaign.id, "campaign_recipient_ids": [r.id for r in recipients]}
    return campaign, recipients, payload


def counts(campaign):
    result = {}
    for status in CampaignRecipient.objects.filter(campaign=campaign).values_list("status", flat=True):
        result[status] = result.get(status, 0) + 1
    return result


@pytest.fixture(autouse=True)
def _drain_flag():
    sender.reset_sigterm_drain()
    yield
    sender.reset_sigterm_drain()


@pytest.mark.parametrize(
    "exc,expected",
    [
        (client_error("Throttling", 400), "retry"),
        (client_error("ServiceUnavailable", 503), "retry"),
        (ConnectTimeoutError(endpoint_url="https://ses"), "retry"),
        (client_error("MessageRejected"), "failed"),
        (client_error("InvalidParameterValue"), "failed"),
        (ParamValidationError(report="bad"), "failed"),
        (client_error("InternalFailure", 500), "unknown"),
        (client_error("RequestTimeout", 408), "unknown"),
        (ReadTimeoutError(endpoint_url="https://ses"), "unknown"),
        (ConnectionResetError("reset"), "unknown"),
        (TimeoutError("t"), "unknown"),
        (RuntimeError("adapter bug"), "failed"),
    ],
)
def test_classify_send_error(exc, expected):
    assert sender.classify_send_error(exc) == expected


@override_settings(CAMPAIGN_CLAIM_CHUNK=3, SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_throttle_returns_claimed_rows_to_pending_and_raises_retryable():
    campaign, _, payload = make_campaign(6)
    # mailing.ses re-permits and retries a Throttling response once, so throttle twice.
    ses = FaultSes(faults={2: "throttle", 3: "throttle"})
    with pytest.raises(sender.RetryableCampaignSendError):
        sender.send_campaign_batch(payload, ses_client=ses)
    assert counts(campaign) == {CampaignRecipientStatus.SENT: 1, PENDING: 5}
    throttled = CampaignRecipient.objects.exclude(status=CampaignRecipientStatus.SENT)
    assert not throttled.filter(claim_owner__gt="").exists()
    assert not throttled.filter(tracking_token_hash__isnull=False).exists()


@override_settings(CAMPAIGN_CLAIM_CHUNK=5, SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_halt_mid_chunk_returns_claimed_unstarted_rows_to_pending():
    campaign, _, payload = make_campaign(5)

    def halt_after_first(number):
        if number == 1:
            Campaign.objects.filter(pk=campaign.pk).update(send_halted_at=timezone.now())

    ses = FaultSes(on_call=halt_after_first)
    result = sender.send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == 1 and result.sent_count == 1
    assert counts(campaign) == {CampaignRecipientStatus.SENT: 1, PENDING: 4}


@override_settings(CAMPAIGN_CLAIM_CHUNK=5, SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_sigterm_drain_finalizes_in_flight_and_returns_unstarted_rows():
    campaign, _, payload = make_campaign(5)
    ses = FaultSes(on_call=lambda number: sender._shutdown.set() if number == 2 else None)
    with pytest.raises(sender.CampaignSendInterrupted):
        sender.send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == 2
    assert counts(campaign) == {CampaignRecipientStatus.SENT: 2, PENDING: 3}


@override_settings(CAMPAIGN_CLAIM_CHUNK=1, SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_unknown_is_never_resent_and_keeps_its_tokens():
    campaign, _, payload = make_campaign(3)
    ses = FaultSes(faults={2: "read_timeout"})
    result = sender.send_campaign_batch(payload, ses_client=ses)
    assert result.unknown_count == 1 and result.sent_count == 2
    unknown = CampaignRecipient.objects.get(status=UNKNOWN)
    assert unknown.tracking_token_hash and unknown.unsubscribe_token_hash  # the accepted copy's links
    assert unknown.last_error == "send_outcome_unknown:ReadTimeoutError"
    calls = ses.calls
    sender.send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == calls
    assert CampaignRecipient.objects.get(pk=unknown.pk).tracking_token_hash == unknown.tracking_token_hash


def test_sending_rows_keep_the_campaign_open_and_completed_batch_settles_it():
    campaign, recipients, _ = make_campaign(2)
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(status=CampaignRecipientStatus.SENT, sent_at=timezone.now())
    CampaignRecipient.objects.filter(pk=recipients[1].pk).update(status=SENDING, claimed_at=timezone.now())
    sender.refresh_campaign_send_counts(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == "sending" and campaign.sent_count == 1
    CampaignRecipient.objects.filter(pk=recipients[1].pk).update(status=UNKNOWN)
    sender.refresh_campaign_send_counts(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == "sent"


def test_refresh_does_not_overwrite_a_cancelled_campaign():
    campaign, recipients, _ = make_campaign(1, status="cancelled")
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(status=CampaignRecipientStatus.SENT, sent_at=timezone.now())
    sender.refresh_campaign_send_counts(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == "cancelled" and campaign.sent_count == 1


def test_expire_stale_claims_only_moves_rows_past_the_lease():
    campaign, recipients, _ = make_campaign(2)
    now = timezone.now()
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(status=SENDING, claimed_at=now - timedelta(minutes=11))
    CampaignRecipient.objects.filter(pk=recipients[1].pk).update(status=SENDING, claimed_at=now - timedelta(minutes=9))
    assert expire_stale_claims(now=now) == 1
    assert counts(campaign) == {UNKNOWN: 1, SENDING: 1}
    assert expire_stale_claims(now=now, lease_seconds=60) == 1


def _stored_event(ses_message_id, email, *, kind="send", when=None):
    return EmailEvent.objects.create(
        event_type=EmailEventType.SENT, provider_event_id=f"sns-{ses_message_id}-{kind}",
        metadata={
            "ses_message_id": ses_message_id, "notification_type": kind, "mail_destination_hash": recipient_hash(email),
            "mail_timestamp": (when or timezone.now()).isoformat(),
        },
    )


def test_reconcile_from_stored_ses_events_finalizes_unknown_as_sent_once():
    campaign, recipients, _ = make_campaign(2)
    now = timezone.now()
    CampaignRecipient.objects.filter(campaign=campaign).update(status=UNKNOWN, claimed_at=now - timedelta(minutes=1))
    _stored_event("ses-a", recipients[0].email)
    _stored_event("ses-a", recipients[0].email, kind="delivery")
    assert reconcile_unknown_recipients() == 1
    assert reconcile_unknown_recipients() == 0
    first = CampaignRecipient.objects.get(pk=recipients[0].pk)
    assert first.status == CampaignRecipientStatus.SENT and first.ses_message_id == "ses-a"
    assert first.delivered_at is not None  # the stored delivery event was correlated afterwards
    assert CampaignRecipient.objects.get(pk=recipients[1].pk).status == UNKNOWN
    campaign.refresh_from_db()
    assert campaign.sent_count == 1


def test_reconcile_leaves_ambiguous_and_out_of_window_events_for_the_operator():
    campaign, recipients, _ = make_campaign(1, tag="x")
    other, others, _ = make_campaign(1, tag="x2")
    shared = recipients[0].email
    CampaignRecipient.objects.filter(pk=others[0].pk).update(email=shared)
    now = timezone.now()
    CampaignRecipient.objects.filter(campaign__in=[campaign, other]).update(status=UNKNOWN, claimed_at=now)
    event = {"ses_message_id": "ses-amb", "recipient": shared, "mail_timestamp": now.isoformat()}
    assert reconcile_unknown_from_event(event) is False  # two UNKNOWN rows for the address
    assert not CampaignRecipient.objects.filter(status=CampaignRecipientStatus.SENT).exists()
    event = {**event, "campaign_id": campaign.pk, "mail_timestamp": (now - timedelta(days=2)).isoformat()}
    assert reconcile_unknown_from_event(event) is False  # sent long before the claim
    assert reconcile_unknown_from_event({**event, "mail_timestamp": now.isoformat()}) is True


def test_reconcile_refuses_a_message_id_that_already_belongs_to_another_row():
    campaign, recipients, _ = make_campaign(2, tag="y")
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(
        status=CampaignRecipientStatus.SENT, ses_message_id="ses-dup"
    )
    CampaignRecipient.objects.filter(pk=recipients[1].pk).update(status=UNKNOWN)
    assert reconcile_unknown_from_event({"ses_message_id": "ses-dup", "recipient": recipients[1].email}) is False


def test_webhook_metadata_carries_the_destination_hash_not_the_address():
    from mailing.services.ses_webhooks import build_ses_metadata  # noqa: PLC0415 - one-off

    metadata = build_ses_metadata(
        {"eventType": "Send", "mail": {"messageId": "m", "destination": ["Person@Example.com"]}}, {"MessageId": "s"}
    )
    assert metadata["mail_destination_hash"] == hashlib.sha256(b"person@example.com").hexdigest()
    assert "person@example.com" not in str(metadata).lower()


def test_resend_requires_confirmation_is_logged_and_enqueues(caplog):
    campaign, recipients, _ = make_campaign(3, tag="r")
    for index, row in enumerate(recipients):
        CampaignRecipient.objects.filter(pk=row.pk).update(
            status=UNKNOWN, claimed_at=timezone.now(), tracking_token_hash=f"h{index}", unsubscribe_token_hash=f"u{index}"
        )
    ids = [recipients[0].pk, recipients[1].pk]
    for bad in (False, None, "true", 1):
        with pytest.raises(ResendNotConfirmed, match="confirm"):
            resend_unknown(campaign.pk, ids, confirm=bad, actor="op@example.com")
    assert counts(campaign) == {UNKNOWN: 3}
    with caplog.at_level("WARNING", logger="mailing.services.send_reconcile"):
        assert resend_unknown(campaign.pk, ids, confirm=True, actor="op@example.com") == 2
    assert counts(campaign) == {UNKNOWN: 1, PENDING: 2}
    assert "campaign_unknown_resend" in caplog.text and "op@example.com" in caplog.text
    events = EmailEvent.objects.filter(metadata__action="operator_resend_unknown")
    assert events.count() == 2 and events.first().metadata["actor"] == "op@example.com"
    row = CampaignRecipient.objects.get(pk=recipients[0].pk)
    assert row.tracking_token_hash is None and row.claimed_at is None  # re-minted at the next claim
    # A second click cannot resend rows that are no longer UNKNOWN.
    assert resend_unknown(campaign.pk, ids, confirm=True, actor="op@example.com") == 0


def test_unknown_api_lists_counts_and_resend_needs_confirm_true(client):
    campaign, recipients, _ = make_campaign(3, tag="api", external_key="camp-1")
    create_client_api_key(client=campaign.client, name="t", raw_api_key="k-unknown")
    headers = {"HTTP_AUTHORIZATION": "Bearer k-unknown"}
    CampaignRecipient.objects.filter(campaign=campaign).update(status=UNKNOWN, claimed_at=timezone.now())
    scope = {"audience": campaign.audience.slug, "client": campaign.client.slug}
    listing = client.get(reverse("mailing:api_campaign_unknown_recipients", args=["camp-1"]), scope, **headers)
    assert listing.status_code == 200 and listing.json()["count"] == 3
    url = reverse("mailing:api_campaign_unknown_resend", args=["camp-1"])
    body = {"recipient_ids": [recipients[0].pk], **scope}
    refused = client.post(url, data=body, content_type="application/json", **headers)
    assert refused.status_code == 400 and "confirm" in str(refused.json())
    assert counts(campaign) == {UNKNOWN: 3}
    accepted = client.post(url, data={**body, "confirm": True}, content_type="application/json", **headers)
    assert accepted.status_code == 202 and accepted.json()["resent"] == 1
    assert counts(campaign) == {UNKNOWN: 2, PENDING: 1}
    assert client.get(url, scope, **headers).status_code == 405


def test_unknown_recipients_listing_shape():
    campaign, _, _ = make_campaign(2, tag="l")
    CampaignRecipient.objects.filter(campaign=campaign).update(status=UNKNOWN, claimed_at=timezone.now())
    listing = unknown_recipients(campaign.pk)
    assert listing["count"] == 2 and {row["email"] for row in listing["recipients"]} == {
        "p0-l@example.com", "p1-l@example.com",
    }
