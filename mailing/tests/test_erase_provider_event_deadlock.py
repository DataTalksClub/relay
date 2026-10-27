"""Contact erasure must take child row locks before the Contact row lock.

2026-10-27 12:49Z, production: the ``provider-events-worker`` aborted a task
with ``deadlock detected``. ``erase_contact_for_client`` held ``FOR UPDATE``
on the contact row and waited for a ``TransactionalMessage``/
``CampaignRecipient`` row, while ``process_ses_webhook`` held that child row
(correlation locks it first) and waited on the contact row -- the bounce's
``mark_hard_bounced`` update and, at COMMIT, the ``EmailEvent`` insert's
deferred foreign-key check both need the contact. The erase was the only
writer that locked the parent first; the fix makes it take the child rows
first (``_lock_contact_children_for_erase``) and the contact last, matching
every sender/webhook/tracking writer.

The first test asserts the lock order on any backend. The last three replay
the cycle with two real connections and only run on PostgreSQL (SQLite has
no row locks); run them with ``DATABASE_URL=postgres://...``.
"""

import threading

import pytest
from django.db import connection, connections, transaction
from django.db.models.query import QuerySet
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
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services import api as api_module
from mailing.services import ses_webhooks
from mailing.services.api import erase_contact_for_client
from mailing.services.ses_webhooks import process_ses_webhook

pytestmark = pytest.mark.django_db(transaction=True)

HARD_BOUNCE = {"bounce_type": "Permanent", "bounce_sub_type": "General"}


@pytest.fixture
def records():
    organization = Organization.objects.create(name="Erase deadlock", slug="erase-deadlock")
    client = Client.objects.create(organization=organization, name="News", slug="erase-news")
    contact = Contact.objects.create(email="erased@example.invalid")
    template = EmailTemplate.objects.create(
        client=client, key="verify", name="Verify", subject="S", html_body="<p>B</p>", text_body="B"
    )
    message = TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="news",
        from_email="news@example.invalid",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="erase-deadlock-1",
        subject="S",
        html_body="<p>B</p>",
        text_body="B",
        ses_message_id="erase-deadlock-ses-1",
        sent_at=timezone.now(),
    )
    audience = Audience.objects.create(organization=organization, name="A", slug="erase-a")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="Hello")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id="erase-deadlock-campaign-1",
        sent_at=timezone.now(),
    )
    return client, audience, contact, message, campaign, recipient


def _bounce(ses_message_id, provider_event_id):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": provider_event_id,
        "notification_type": "bounce",
        "received_at": "2026-10-27T12:49:07Z",
        "ses_message_id": ses_message_id,
        "mail_message_id": ses_message_id,
        "metadata": HARD_BOUNCE,
    }


def _erase_payload(client, audience, email):
    return {"email": email, "client": client.slug, "audience": audience.slug}


def _capture_lock_order(monkeypatch):
    """Record this thread's select_for_update querysets as they compile.

    Concurrent threads run their own locking queries; only the calling
    thread's entries are returned so the order assertions stay deterministic.
    """
    ident = threading.get_ident()
    order = []
    original = QuerySet._fetch_all

    def fetch(query):
        if query._result_cache is None and query.query.select_for_update:
            if threading.get_ident() == ident:
                order.append(
                    (
                        query.model,
                        bool(query.query.select_for_no_key_update),
                        set(query.query.select_for_update_of or ()),
                    )
                )
        return original(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    return order


def test_erase_locks_child_rows_before_the_contact_row(monkeypatch, records):
    client, audience, contact, _message, _campaign, _recipient = records
    order = _capture_lock_order(monkeypatch)

    result = erase_contact_for_client(_erase_payload(client, audience, contact.normalized_email), client)

    assert result["erased"] is True
    assert result["counts"]["transactional_messages_anonymized"] == 1
    assert result["counts"]["campaign_recipients_anonymized"] == 1
    # Exactly one locking query per pre-locked table plus the parent lock.
    assert [(model, no_key, of) for model, no_key, of in order] == [
        (EmailEvent, True, {"self"}),
        (TransactionalMessage, True, {"self"}),
        (CampaignRecipient, True, {"self"}),
        (Contact, False, set()),
    ]


def _run_both_sides(records, ses_side_body, monkeypatch):
    """Run the SES-side transaction and the erase on separate connections.

    The SES side runs on a worker thread and receives the two barrier
    events: webhook tests pause inside the service via the
    ``append_provider_event`` seam once it holds its child row locks;
    manual bodies signal and wait around their own statements. The erase
    runs on the calling thread and signals from inside
    ``contact_has_scoped_relationship`` -- the old parent-first code reached
    that call right after taking the Contact lock, the fixed child-first
    code reaches it before taking any row lock at all.
    """
    client, audience, contact, _message, _campaign, _recipient = records
    ses_holding_child = threading.Event()
    erase_past_seam = threading.Event()
    errors = []

    original_append = ses_webhooks.append_provider_event
    original_relationship = api_module.contact_has_scoped_relationship

    def append_at_insert(payload, source):
        ses_holding_child.set()
        assert erase_past_seam.wait(10)
        return original_append(payload, source)

    def relationship_then_signal(contact_arg, scope):
        result = original_relationship(contact_arg, scope)
        erase_past_seam.set()
        return result

    monkeypatch.setattr(ses_webhooks, "append_provider_event", append_at_insert)
    monkeypatch.setattr(api_module, "contact_has_scoped_relationship", relationship_then_signal)

    def ses_side():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET deadlock_timeout = '200ms'")
            ses_side_body(ses_holding_child, erase_past_seam)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertions below
            errors.append(("ses", exc))
        finally:
            connections.close_all()

    def erase_side():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET deadlock_timeout = '200ms'")
            return erase_contact_for_client(_erase_payload(client, audience, contact.normalized_email), client)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertions below
            errors.append(("erase", exc))
            return None
        finally:
            connections.close_all()

    ses_thread = threading.Thread(target=ses_side)
    ses_thread.start()
    assert ses_holding_child.wait(10), errors
    result = erase_side()
    ses_thread.join(30)
    assert not ses_thread.is_alive()
    return result, errors


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row-lock deadlocks need PostgreSQL")
def test_erase_and_transactional_bounce_do_not_deadlock(monkeypatch, records):
    _client, _audience, contact, message, _campaign, _recipient = records

    def ses_body(_holding, _wait_for_erase):
        process_ses_webhook(_bounce(message.ses_message_id, "erase-deadlock-bounce-1"))

    result, errors = _run_both_sides(records, ses_body, monkeypatch)

    assert errors == []
    assert result["erased"] is True
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.BOUNCED
    assert EmailEvent.objects.filter(provider_event_id="erase-deadlock-bounce-1").count() == 1
    contact.refresh_from_db()
    # The erase clears suppression flags after the bounce applied; the
    # erased alias proves the erase ran to completion after it.
    assert contact.normalized_email.startswith("erased-contact-")
    assert result["counts"]["transactional_messages_anonymized"] == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row-lock deadlocks need PostgreSQL")
def test_erase_and_campaign_bounce_do_not_deadlock(monkeypatch, records):
    _client, _audience, contact, _message, _campaign, recipient = records

    def ses_body(_holding, _wait_for_erase):
        process_ses_webhook(_bounce(recipient.ses_message_id, "erase-deadlock-bounce-2"))

    result, errors = _run_both_sides(records, ses_body, monkeypatch)

    assert errors == []
    assert result["erased"] is True
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.BOUNCED
    assert EmailEvent.objects.filter(provider_event_id="erase-deadlock-bounce-2").count() == 1
    assert result["counts"]["campaign_recipients_anonymized"] == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row-lock deadlocks need PostgreSQL")
def test_erase_and_marking_a_message_sent_do_not_deadlock(monkeypatch, records):
    """The 2026-10-26 shape: the sender's committed EmailEvent insert needs
    FOR KEY SHARE on the contact at COMMIT while the erase wants its message
    row; the child-first erase leaves only a plain wait."""
    _client, _audience, _contact, message, _campaign, _recipient = records

    def ses_body(holding, wait_for_erase):
        # Mirrors transactional_sender._mark_sent's statements; the helper
        # itself retries transient errors, which would mask the cycle.
        with transaction.atomic():
            locked = TransactionalMessage.objects.select_for_update(of=("self",)).get(pk=message.pk)
            locked.status = TransactionalMessageStatus.SENT
            locked.ses_message_id = "erase-deadlock-sent-1"
            locked.sent_at = timezone.now()
            locked.save(update_fields=["status", "ses_message_id", "sent_at", "updated_at"])
            holding.set()
            assert wait_for_erase.wait(10)
            EmailEvent.objects.create(
                event_type=EmailEventType.SENT,
                transactional_message=locked,
                contact=locked.contact,
                client=locked.client,
                metadata={"ses_message_id": "erase-deadlock-sent-1"},
            )

    result, errors = _run_both_sides(records, ses_body, monkeypatch)

    assert errors == []
    assert result["erased"] is True
    message.refresh_from_db()
    assert EmailEvent.objects.filter(campaign_recipient__isnull=True, transactional_message=message).count() == 1
