"""Transactional sends must not lock contact/client rows.

SQLite cannot reproduce PostgreSQL deadlocks, so this verifies the actual
evaluated ``FOR UPDATE`` queryset: the 2026-10-26 production deadlock was
``while locking tuple ... in relation "contacts"`` -- two concurrent sends
touching the same contact each held their message lock and waited on the
other's contact lock, because ``select_for_update().select_related(...)``
locks the joined rows too. ``of=("self",)`` keeps the lock on the message
table only.

Mirrors the interception style of ``test_campaign_lock_order.py``.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.db.models.query import QuerySet

import mailing.services.transactional_sender as sender
from mailing import db_retry
from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.transactional_retry import retry_transactional_message

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def records():
    organization = Organization.objects.create(name="Lock scope", slug="lock-scope")
    client = Client.objects.create(organization=organization, name="News", slug="news")
    contact = Contact.objects.create(email="person@example.invalid")
    template = EmailTemplate.objects.create(
        client=client,
        key="verify",
        name="Verify",
        subject="Subject",
        html_body="<p>Body</p>",
        text_body="Body",
    )
    message = TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="news",
        from_email="news@example.invalid",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="lock-scope-1",
        subject="Subject",
        html_body="<p>Body</p>",
        text_body="Body",
    )
    return client, contact, message


def _capture_lock_scopes(monkeypatch):
    scopes = []
    original = QuerySet._fetch_all

    def fetch(query):
        if (
            query._result_cache is None
            and query.query.select_for_update
            and query.model is TransactionalMessage
        ):
            scopes.append(set(query.query.select_for_update_of or ()))
        return original(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    return scopes


def test_claim_locks_only_the_message_row(monkeypatch, records):
    client, _contact, message = records
    scopes = _capture_lock_scopes(monkeypatch)

    claimed = sender._claim_message_for_send(
        {
            "transactional_message_id": message.id,
            "client_id": client.id,
            "idempotency_key": "lock-scope-1",
        }
    )

    assert claimed.status == TransactionalMessageStatus.SENDING
    assert scopes, "expected a FOR UPDATE query on TransactionalMessage"
    assert all(scope == {"self"} for scope in scopes)


def test_mark_sent_retries_a_single_deadlock(monkeypatch, records):
    """The retry must survive a deadlock aborting an in-flight transaction.

    The first attempt enters the atomic block and evaluates the queryset
    before Postgres aborts it; the retry then commits exactly one SENT
    row and event -- no duplicate send record.
    """
    _client, _contact, message = records
    message.status = TransactionalMessageStatus.SENDING
    message.save(update_fields=["status", "updated_at"])
    scopes = _capture_lock_scopes(monkeypatch)
    monkeypatch.setattr(db_retry, "sleep", lambda seconds: None)

    real_locked = sender._locked_messages
    state = {"aborted": False}

    def flaky_locked():
        queryset = real_locked()
        original_get = queryset.get

        def get_once(*args, **kwargs):
            if not state["aborted"]:
                state["aborted"] = True
                raise OperationalError("deadlock detected")
            return original_get(*args, **kwargs)

        queryset.get = get_once
        return queryset

    monkeypatch.setattr(sender, "_locked_messages", flaky_locked)

    marked = sender._mark_sent(message.id, "ses-message-1", {"sent_at": "2026-10-26T00:00:00+00:00"})

    assert state["aborted"] is True
    assert marked.status == TransactionalMessageStatus.SENT
    assert scopes, "expected a FOR UPDATE query on TransactionalMessage"
    assert all(scope == {"self"} for scope in scopes)
    assert (
        EmailEvent.objects.filter(
            transactional_message=message, event_type=EmailEventType.SENT
        ).count()
        == 1
    )


def test_operator_retry_locks_only_the_message_row(
    monkeypatch, records, django_capture_on_commit_callbacks
):
    """The operator retry path narrowed alongside the send path."""
    _client, _contact, message = records
    message.status = TransactionalMessageStatus.FAILED
    message.save(update_fields=["status", "updated_at"])
    operator = get_user_model().objects.create_user(username="lock-scope-op", is_staff=True)
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    scopes = _capture_lock_scopes(monkeypatch)

    with django_capture_on_commit_callbacks(execute=True):
        retry_transactional_message(message.pk, actor=operator, reason="lock scope check")

    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.QUEUED
    assert scopes, "expected a FOR UPDATE query on TransactionalMessage"
    assert all(scope == {"self"} for scope in scopes)
