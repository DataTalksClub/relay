"""Generic tenant-scoped client callbacks (plan issue R1.4, relay issue #17).

Covers: the redacted versioned event payload, event-type mapping, row
deduplication under repeated transition processing, the timestamped HMAC
header contract (no Bearer credential), the deterministic contract fixture,
retry classification with bounded backoff, terminal failures (permanent 4xx,
redirects, retry exhaustion, endpoint disablement), per-message sequencing,
and proof that callback failure never regresses transport state.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    CallbackEndpoint,
    Campaign,
    CampaignRecipient,
    Client,
    ClientCallback,
    ClientCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.client_callbacks import (
    callback_event_id,
    callback_signature,
    canonical_callback_body,
    dispatch_client_callback,
    emit_client_callback,
    process_due_client_callbacks,
)
from mailing.tests.callback_helpers import (
    DEFAULT_CALLBACK_SECRET,
    DEFAULT_CALLBACK_URL,
    create_callback_endpoint,
    http_error,
    install_fake_opener,
)

pytestmark = pytest.mark.django_db

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "client_callback_contract_v1.json"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def app_client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def other_client(organization):
    return Client.objects.create(organization=organization, name="Newsletter", slug="dtc-newsletter")


@pytest.fixture
def contact():
    return Contact.objects.create(email="learner@example.com")


@pytest.fixture
def template(app_client):
    return EmailTemplate.objects.create(
        client=app_client,
        key="registration-welcome",
        name="Registration welcome",
        subject="Welcome",
        is_transactional=True,
    )


@pytest.fixture
def transactional_message(app_client, contact, template):
    return TransactionalMessage.objects.create(
        client=app_client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="registration-user-123",
        subject="Welcome to the course",
        html_body="<p>Welcome to the course</p>",
        text_body="Welcome to the course",
    )


def callback_endpoint(app_client):
    return create_callback_endpoint(app_client)


def transition_event(transactional_message, event_type=EmailEventType.BOUNCE, metadata=None):
    return EmailEvent.objects.create(
        transactional_message=transactional_message,
        contact=transactional_message.contact,
        client=transactional_message.client,
        event_type=event_type,
        metadata=metadata or {},
    )


# --- Event payload: mapping, redaction, dedup -------------------------------


@pytest.mark.parametrize(
    ("email_event_type", "callback_event_type", "reason_code"),
    [
        (EmailEventType.QUEUED, "delivery.accepted", None),
        (EmailEventType.SENT, "delivery.accepted", None),
        (EmailEventType.SKIPPED, "delivery.suppressed", "suppressed"),
        (EmailEventType.DELIVERED, "delivery.delivered", None),
        (EmailEventType.BOUNCE, "delivery.bounced", "soft_bounce"),
        (EmailEventType.COMPLAINT, "delivery.complained", "complaint"),
        (EmailEventType.OPEN, "engagement.opened", None),
        (EmailEventType.CLICK, "engagement.clicked", None),
        (EmailEventType.SUBSCRIBE, "subscription.changed", "subscribed"),
        (EmailEventType.UNSUBSCRIBE, "subscription.changed", "unsubscribed"),
    ],
)
def test_every_transition_maps_to_one_versioned_callback_event(
    transactional_message,
    app_client,
    email_event_type,
    callback_event_type,
    reason_code,
):
    callback_endpoint(app_client)
    event = transition_event(transactional_message, email_event_type)

    callback = emit_client_callback(event)

    assert callback is not None
    assert callback.event_type == callback_event_type
    assert callback.contract_version == 1
    assert callback.payload["event_type"] == callback_event_type
    if reason_code is None:
        assert "reason_code" not in callback.payload
    else:
        assert callback.payload["reason_code"] == reason_code


def test_payload_carries_identifiers_and_never_redaction_canaries(
    transactional_message,
    app_client,
):
    callback_endpoint(app_client)
    event = transition_event(
        transactional_message,
        EmailEventType.BOUNCE,
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General", "diagnostic": "smtp: quit whining"},
    )

    callback = emit_client_callback(event)
    body = callback.body

    assert set(callback.payload) == {
        "bounce_type",
        "client_reference",
        "contract_version",
        "event_id",
        "event_type",
        "reason_code",
        "sequence",
        "message_id",
        "template_key",
        "timestamp",
    }
    assert callback.payload["message_id"] == str(transactional_message.pk)
    assert callback.payload["client_reference"] == "registration-user-123"
    assert callback.payload["template_key"] == "registration-welcome"
    assert callback.payload["bounce_type"] == "hard"
    assert callback.payload["reason_code"] == "hard_bounce"
    assert parse_datetime(callback.payload["timestamp"]) is not None
    for canary in (
        "learner@example.com",
        "Welcome to the course",
        "smtp: quit whining",
        "diagnostic",
    ):
        assert canary not in body
    assert callback.body_hash == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert callback.body == canonical_callback_body(callback.payload).decode("utf-8")


def test_soft_bounce_and_omitted_bounce_type_metadata_report_soft(transactional_message, app_client):
    callback_endpoint(app_client)
    soft = emit_client_callback(transition_event(transactional_message, metadata={"bounce_type": "Transient"}))
    bare = emit_client_callback(transition_event(transactional_message))

    assert soft.payload["bounce_type"] == "soft"
    assert soft.payload["reason_code"] == "soft_bounce"
    assert bare.payload["bounce_type"] == "soft"


def test_contact_level_subscription_event_has_no_message_identity(app_client, contact):
    callback_endpoint(app_client)
    event = EmailEvent.objects.create(
        contact=contact,
        client=app_client,
        event_type=EmailEventType.UNSUBSCRIBE,
        metadata={"source": "api"},
    )

    callback = emit_client_callback(event)

    assert callback is not None
    assert callback.event_type == "subscription.changed"
    assert callback.payload["message_id"] is None
    assert callback.payload["client_reference"] is None
    assert callback.payload["template_key"] == ""


def test_campaign_and_send_failure_transitions_create_no_callback(
    transactional_message,
    app_client,
    organization,
    contact,
):
    callback_endpoint(app_client)
    audience = Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    campaign = Campaign.objects.create(audience=audience, client=app_client, subject="Weekly update")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        ses_message_id="ses-campaign-1",
    )
    campaign_event = EmailEvent.objects.create(
        campaign_recipient=recipient,
        client=app_client,
        event_type=EmailEventType.DELIVERED,
        metadata={},
    )

    assert emit_client_callback(campaign_event) is None
    assert emit_client_callback(transition_event(transactional_message, EmailEventType.FAILED)) is None
    assert ClientCallback.objects.count() == 0


def test_emit_is_deduplicated_per_client_and_event(transactional_message, app_client):
    callback_endpoint(app_client)
    event = transition_event(transactional_message)

    first = emit_client_callback(event)
    second = emit_client_callback(event)

    assert first.pk == second.pk
    assert ClientCallback.objects.count() == 1


def test_transitions_without_a_client_or_endpoint_create_no_callback(
    transactional_message,
    app_client,
    other_client,
    contact,
):
    callback_endpoint(app_client)
    event = transition_event(transactional_message)

    # A transition owned by another client must not deliver to this endpoint.
    other_event = EmailEvent.objects.create(
        contact=contact,
        client=other_client,
        event_type=EmailEventType.DELIVERED,
        metadata={},
    )

    assert emit_client_callback(event) is not None

    endpoint_disabled = CallbackEndpoint.objects.get(client=app_client)
    endpoint_disabled.disable("rotation test", disabled_at=timezone.now())
    orphan = transition_event(transactional_message)
    assert emit_client_callback(orphan) is None
    assert emit_client_callback(other_event) is None
    assert ClientCallback.objects.count() == 1


def test_sequence_is_monotonic_per_message(transactional_message, app_client):
    callback_endpoint(app_client)

    first = emit_client_callback(transition_event(transactional_message, EmailEventType.QUEUED))
    second = emit_client_callback(transition_event(transactional_message, EmailEventType.SENT))
    third = emit_client_callback(transition_event(transactional_message, EmailEventType.DELIVERED))

    assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]


def test_event_id_is_stable_for_one_transition(transactional_message, app_client):
    callback_endpoint(app_client)
    event = transition_event(transactional_message)

    assert str(callback_event_id(event)) == emit_client_callback(event).payload["event_id"]


# --- Signing and dispatch ---------------------------------------------------


def signed_request(opener):
    request = opener.requests[0]
    return (
        request,
        json.loads(request["body"].decode("utf-8")),
        request["body"].decode("utf-8"),
    )


def test_dispatch_posts_the_canonical_body_with_timestamped_hmac(
    transactional_message,
    app_client,
    monkeypatch,
):
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    opener = install_fake_opener(monkeypatch)

    assert dispatch_client_callback(callback) is True

    request, body, raw_body = signed_request(opener)
    assert request["url"] == DEFAULT_CALLBACK_URL
    assert "authorization" not in request["headers"]
    assert request["headers"]["content-type"] == "application/json"
    assert body == callback.payload
    assert raw_body == callback.body
    expected = callback_signature(DEFAULT_CALLBACK_SECRET, request["headers"]["x-relay-timestamp"], request["body"])
    assert request["headers"]["x-relay-signature"] == expected
    assert request["headers"]["x-relay-event-id"] == str(callback.event_id)
    assert request["headers"]["x-relay-contract-version"] == "1"
    assert request["headers"]["x-relay-attempt"] == "1"

    callback.refresh_from_db()
    assert callback.status == ClientCallbackStatus.DELIVERED
    assert callback.attempt_count == 1
    assert callback.response_status_class == "2xx"
    assert callback.delivered_at is not None


def test_retry_after_transport_failure_then_success_keeps_one_identical_body(
    transactional_message,
    app_client,
    monkeypatch,
):
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    opener = install_fake_opener(monkeypatch, outcomes=[TimeoutError("timed out"), 200])

    assert dispatch_client_callback(callback) is False
    callback.refresh_from_db()
    first_body = callback.body
    assert callback.status == ClientCallbackStatus.PENDING
    assert callback.attempt_count == 1
    assert callback.last_error_code == "timeout"
    assert callback.next_attempt_at > callback.created_at

    callback.next_attempt_at = timezone.now()
    callback.save(update_fields=["next_attempt_at", "updated_at"])
    assert dispatch_client_callback(callback) is True

    callback.refresh_from_db()
    assert callback.status == ClientCallbackStatus.DELIVERED
    assert callback.attempt_count == 2
    assert callback.body == first_body
    request = opener.requests[1]
    expected = callback_signature(DEFAULT_CALLBACK_SECRET, request["headers"]["x-relay-timestamp"], request["body"])
    assert request["headers"]["x-relay-signature"] == expected
    assert request["headers"]["x-relay-attempt"] == "2"


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_class", "retryable"),
    [
        (http_error(429), "rate_limited", "4xx", True),
        (http_error(500), "http_5xx", "5xx", True),
        (http_error(503), "http_5xx", "5xx", True),
        (http_error(404), "http_4xx", "4xx", False),
        (http_error(410), "http_4xx", "4xx", False),
        (http_error(302), "redirect_not_allowed", "3xx", False),
    ],
)
def test_http_outcome_classification(
    transactional_message,
    app_client,
    monkeypatch,
    outcome,
    expected_code,
    expected_class,
    retryable,
):
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    install_fake_opener(monkeypatch, outcomes=[outcome])

    assert dispatch_client_callback(callback) is False

    callback.refresh_from_db()
    assert callback.last_error_code == expected_code
    assert callback.response_status_class == expected_class
    if retryable:
        assert callback.status == ClientCallbackStatus.PENDING
        assert callback.next_attempt_at > callback.last_attempt_at
    else:
        assert callback.status == ClientCallbackStatus.FAILED


def test_retry_exhaustion_becomes_a_terminal_failure(transactional_message, app_client, monkeypatch):
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    callback.max_attempts = 2
    callback.save(update_fields=["max_attempts", "updated_at"])
    install_fake_opener(monkeypatch, outcomes=[http_error(500), http_error(500)])

    assert dispatch_client_callback(callback) is False
    callback.refresh_from_db()
    assert callback.status == ClientCallbackStatus.PENDING
    assert callback.attempt_count == 1

    assert dispatch_client_callback(callback) is False
    callback.refresh_from_db()
    assert callback.status == ClientCallbackStatus.FAILED
    assert callback.attempt_count == 2
    assert callback.last_error_code == "http_5xx"


def test_disabled_endpoint_fails_pending_callbacks_without_posting(
    transactional_message,
    app_client,
    monkeypatch,
):
    endpoint = callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    opener = install_fake_opener(monkeypatch)
    endpoint.disable("revoked", disabled_at=timezone.now())

    assert dispatch_client_callback(callback) is False

    assert opener.requests == []
    callback.refresh_from_db()
    assert callback.status == ClientCallbackStatus.FAILED
    assert callback.last_error_code == "endpoint_disabled"


def test_rotation_signs_with_the_new_secret_and_keeps_the_previous_one(
    transactional_message,
    app_client,
    monkeypatch,
):
    endpoint = callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    rotated_at = timezone.now()
    endpoint.rotate_secret("rotated-callback-secret", rotated_at=rotated_at)
    opener = install_fake_opener(monkeypatch)

    assert dispatch_client_callback(callback) is True

    request = opener.requests[0]
    expected = callback_signature("rotated-callback-secret", request["headers"]["x-relay-timestamp"], request["body"])
    assert request["headers"]["x-relay-signature"] == expected
    endpoint.refresh_from_db()
    assert endpoint.previous_signing_secret == DEFAULT_CALLBACK_SECRET
    assert endpoint.secret_rotated_at is not None


def test_callback_failure_never_regresses_transport_state(transactional_message, app_client, monkeypatch):
    transactional_message.status = TransactionalMessageStatus.SENT
    transactional_message.delivered_at = timezone.now()
    transactional_message.save(update_fields=["status", "delivered_at", "updated_at"])
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message, EmailEventType.DELIVERED))
    install_fake_opener(monkeypatch, outcomes=[http_error(500)])

    assert dispatch_client_callback(callback) is False

    transactional_message.refresh_from_db()
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.delivered_at is not None


def test_backoff_grows_bounded_and_deterministic(transactional_message, app_client):
    callback_endpoint(app_client)
    callback = emit_client_callback(transition_event(transactional_message))
    callback.attempt_count = 1

    delays = []
    for _ in range(6):
        delays.append(dispatch_retry_delay(callback))
        callback.attempt_count += 1

    assert delays[0] < delays[-1]
    assert max(delays) <= settings.CLIENT_CALLBACK_RETRY_MAX_DELAY_SECONDS
    # Deterministic: recomputing the schedule for the same attempt is stable.
    callback.attempt_count = 3
    assert dispatch_retry_delay(callback) == dispatch_retry_delay(callback)


def dispatch_retry_delay(callback):
    from mailing.services.client_callbacks import retry_delay  # noqa: PLC0415 - test-local import

    return retry_delay(callback).total_seconds()


# --- Batch dispatcher --------------------------------------------------------


def test_process_due_dispatches_only_due_rows(transactional_message, app_client, monkeypatch):
    callback_endpoint(app_client)
    due = emit_client_callback(transition_event(transactional_message, EmailEventType.QUEUED))
    future = emit_client_callback(transition_event(transactional_message, EmailEventType.SENT))
    future.next_attempt_at = timezone.now() + timedelta(hours=1)
    future.save(update_fields=["next_attempt_at", "updated_at"])
    opener = install_fake_opener(monkeypatch)

    result = process_due_client_callbacks()

    assert result == {"processed": 1, "delivered": 1, "failed": 0}
    assert len(opener.requests) == 1
    due.refresh_from_db()
    future.refresh_from_db()
    assert due.status == ClientCallbackStatus.DELIVERED
    assert future.status == ClientCallbackStatus.PENDING


# --- Deterministic contract fixture -----------------------------------------


def load_contract_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def test_contract_fixture_signature_matches_the_relay_signing_function():
    fixture = load_contract_fixture()

    signature = callback_signature(
        fixture["signing_secret"],
        fixture["timestamp"],
        fixture["canonical_body"].encode("utf-8"),
    )

    assert signature == fixture["expected_signature"]
    assert json.loads(fixture["canonical_body"]) == fixture["event"]


def reference_receiver_verify(body, headers, secrets, *, now, replay_window=300):
    """Verbatim mirror of the reference receiver documented in docs/api.md.

    ``secrets`` is the list of accepted signing secrets: the current one and,
    during a rotation, the previous one for the bounded overlap.
    """
    timestamp = headers.get("X-Relay-Timestamp", "")
    signature = headers.get("X-Relay-Signature", "")
    try:
        stamp = int(timestamp)
    except ValueError:
        raise ReceiverRejection("missing or malformed timestamp") from None
    if abs(now - stamp) > replay_window:
        raise ReceiverRejection("timestamp outside replay window")
    expected = [
        "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        for secret in secrets
    ]
    if not any(hmac.compare_digest(candidate, signature) for candidate in expected):
        raise ReceiverRejection("bad signature")


class ReceiverRejection(Exception):
    pass


def test_reference_receiver_accepts_the_contract_fixture_and_rotation_overlap():
    fixture = load_contract_fixture()
    body = fixture["canonical_body"].encode("utf-8")
    now = int(fixture["timestamp"])
    current_headers = {
        "X-Relay-Timestamp": fixture["timestamp"],
        "X-Relay-Signature": fixture["rotated_expected_signature"],
    }
    previous_headers = {
        "X-Relay-Timestamp": fixture["timestamp"],
        "X-Relay-Signature": fixture["expected_signature"],
    }
    overlap_secrets = [fixture["rotated_signing_secret"], fixture["signing_secret"]]

    # Before rotation: only the current secret verifies.
    reference_receiver_verify(body, previous_headers, [fixture["signing_secret"]], now=now)
    with pytest.raises(ReceiverRejection, match="bad signature"):
        reference_receiver_verify(body, current_headers, [fixture["signing_secret"]], now=now)
    # During the bounded rotation overlap: either secret verifies.
    reference_receiver_verify(body, current_headers, overlap_secrets, now=now)
    reference_receiver_verify(body, previous_headers, overlap_secrets, now=now)


def test_reference_receiver_rejects_tampered_expired_and_forged_callbacks():
    fixture = load_contract_fixture()
    body = fixture["canonical_body"].encode("utf-8")
    now = int(fixture["timestamp"])
    headers = {
        "X-Relay-Timestamp": fixture["timestamp"],
        "X-Relay-Signature": fixture["expected_signature"],
    }

    tampered = json.loads(fixture["canonical_body"])
    tampered["event_id"] = "00000000-0000-5000-8000-000000000009"
    tampered_body = canonical_callback_body(tampered)
    with pytest.raises(ReceiverRejection, match="bad signature"):
        reference_receiver_verify(tampered_body, headers, [fixture["signing_secret"]], now=now)

    with pytest.raises(ReceiverRejection, match="replay window"):
        reference_receiver_verify(body, headers, [fixture["signing_secret"]], now=now + 301)

    forged = dict(headers, **{"X-Relay-Signature": "sha256=" + "0" * 64})
    with pytest.raises(ReceiverRejection, match="bad signature"):
        reference_receiver_verify(body, forged, [fixture["signing_secret"]], now=now)

    missing = {"X-Relay-Signature": headers["X-Relay-Signature"]}
    with pytest.raises(ReceiverRejection, match="malformed timestamp"):
        reference_receiver_verify(body, missing, [fixture["signing_secret"]], now=now)
