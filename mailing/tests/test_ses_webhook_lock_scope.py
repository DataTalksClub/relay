"""SES webhook correlation must lock only the message/recipient row.

2026-10-27 10:43Z, production: ``correlate_ses_message`` ran
``TransactionalMessage.objects.select_for_update().select_related("client",
"contact")``. Without ``of=`` Postgres emits a bare ``FOR UPDATE`` that also
locks the joined ``clients`` and ``contacts`` rows. A concurrent send for the
same contact holds ``FOR KEY SHARE`` on those rows (every ``EmailEvent``
insert referencing them takes one), so the two transactions waited on each
other and Postgres aborted the SES side with ``deadlock detected``. The
provider-events task was marked FAILED and the SES event was lost.

The first tests assert the lock scope on any backend. The last one reproduces
the deadlock with two real connections and only runs on PostgreSQL (SQLite has
no row locks); run it with ``DATABASE_URL=postgres://...``.
"""

import threading
import time

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
from mailing.services.ses_webhooks import correlate_ses_message, process_ses_webhook

pytestmark = pytest.mark.django_db(transaction=True)


def _message(client, contact, n):
    template, _ = EmailTemplate.objects.get_or_create(
        client=client,
        key="verify",
        defaults={"name": "Verify", "subject": "Subject", "html_body": "<p>Body</p>", "text_body": "Body"},
    )
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="news",
        from_email="news@example.invalid",
        template=template,
        template_key="verify",
        status=TransactionalMessageStatus.SENT,
        idempotency_key=f"ses-lock-{n}",
        subject="Subject",
        html_body="<p>Body</p>",
        text_body="Body",
        ses_message_id=f"ses-lock-message-{n}",
        sent_at=timezone.now(),
    )


@pytest.fixture
def records():
    organization = Organization.objects.create(name="SES lock", slug="ses-lock")
    client = Client.objects.create(organization=organization, name="News", slug="news")
    contact = Contact.objects.create(email="shared@example.invalid")
    return client, contact, _message(client, contact, 1), _message(client, contact, 2)


def _capture_lock_scopes(monkeypatch):
    scopes = []
    original = QuerySet._fetch_all

    def fetch(query):
        if query._result_cache is None and query.query.select_for_update:
            scopes.append((query.model, set(query.query.select_for_update_of or ())))
        return original(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    return scopes


def _delivery(ses_message_id, provider_event_id):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": provider_event_id,
        "notification_type": "delivery",
        "received_at": "2026-10-27T10:43:07Z",
        "ses_message_id": ses_message_id,
        "mail_message_id": ses_message_id,
        "metadata": {},
    }


def test_transactional_correlation_locks_only_the_message_row(monkeypatch, records):
    _client, _contact, message, _other = records
    scopes = _capture_lock_scopes(monkeypatch)

    with transaction.atomic():
        assert correlate_ses_message(message.ses_message_id) == message

    assert (TransactionalMessage, {"self"}) in scopes
    assert all(of == {"self"} for model, of in scopes if model is TransactionalMessage)


def test_campaign_correlation_locks_campaign_then_only_the_recipient_row(monkeypatch, records):
    client, contact, _message_1, _message_2 = records
    audience = Audience.objects.create(organization=client.organization, name="A", slug="a")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="Hello")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id="ses-lock-campaign",
        sent_at=timezone.now(),
    )
    scopes = _capture_lock_scopes(monkeypatch)

    with transaction.atomic():
        assert correlate_ses_message("ses-lock-campaign") == recipient

    recipient_scopes = [of for model, of in scopes if model is CampaignRecipient]
    assert recipient_scopes == [{"self"}]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row-lock deadlocks need PostgreSQL")
def test_ses_event_and_concurrent_send_for_the_same_contact_do_not_deadlock(records):
    """Two real connections replay the 2026-10-27 lock cycle.

    Send side (T1): commits a send for message 1. Django creates Postgres
    foreign keys ``DEFERRABLE INITIALLY DEFERRED``, so the ``EmailEvent``
    insert takes ``FOR KEY SHARE`` on the shared client and contact rows at
    COMMIT (the "other side was a COMMIT" in the incident log);
    ``SET CONSTRAINTS ALL IMMEDIATE`` takes those same locks deterministically
    here. T1 then claims message 2 (a sibling message of the same contact).
    SES side (T2): applies a delivery for message 2 through the real
    ``process_ses_webhook``. With the old bare ``FOR UPDATE`` T2 locked message
    2 and then waited for the client/contact rows T1 holds, while T1 waited
    for message 2: ``deadlock detected``. Locking ``of=("self",)`` leaves only
    a plain wait on message 2, which resolves when T2 commits.
    """
    client, contact, message_1, message_2 = records
    send_holds_contact = threading.Event()
    errors = []

    def send_side():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '10s'")
                EmailEvent.objects.create(
                    event_type=EmailEventType.SENT,
                    transactional_message=message_1,
                    contact=contact,
                    client=client,
                )
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                send_holds_contact.set()
                # Let the SES side reach its row locks first.
                time.sleep(1.0)
                TransactionalMessage.objects.select_for_update(of=("self",)).get(pk=message_2.pk)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(("send", exc))
        finally:
            connections.close_all()

    def ses_side():
        try:
            assert send_holds_contact.wait(5)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            process_ses_webhook(_delivery(message_2.ses_message_id, "ses-lock-delivery-2"))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(("ses", exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=send_side), threading.Thread(target=ses_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    message_2.refresh_from_db()
    assert message_2.delivered_at is not None
    assert EmailEvent.objects.filter(provider_event_id="ses-lock-delivery-2").count() == 1
