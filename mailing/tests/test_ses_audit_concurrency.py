"""Retries must serialize even when the audit and correlated message are absent."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections, transaction
from django.db.models.query import QuerySet

from mailing.models import OperatorAudit
from mailing.services.ses_webhooks import process_ses_webhook


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL transaction locks")
@pytest.mark.parametrize("notification_type", ["delivery_delay", "unsupported"])
def test_concurrent_unsupported_retries_create_one_audit(monkeypatch, notification_type):
    payload = {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": "concurrent-unsupported",
        "notification_type": notification_type,
        "received_at": "2026-10-28T11:00:00Z",
        "ses_message_id": "unknown-message",
        "metadata": {},
    }
    created = threading.Event()
    release = threading.Event()
    retry_missed = threading.Event()
    retry_done = threading.Event()
    retry_thread = []
    original_first = QuerySet.first

    def observe_first(query):
        result = original_first(query)
        if query.model is OperatorAudit and threading.get_ident() in retry_thread and result is None:
            retry_missed.set()
        return result

    monkeypatch.setattr(QuerySet, "first", observe_first)

    def first_delivery():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                audit = process_ses_webhook(payload)
                created.set()
                assert release.wait(10)
                return audit.pk
        finally:
            connections.close_all()

    def retry_delivery():
        retry_thread.append(threading.get_ident())
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                return process_ses_webhook(payload).pk
        finally:
            retry_done.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_delivery)
        try:
            assert created.wait(5)
            retry = pool.submit(retry_delivery)
            assert retry_missed.wait(5), "retry must observe the still-uncommitted audit as absent"
            assert not retry_done.wait(0.3), "retry must wait for the first transaction"
        finally:
            release.set()
        assert first.result(timeout=10) == retry.result(timeout=10)

    assert OperatorAudit.objects.filter(metadata__provider_event_id=payload["provider_event_id"]).count() == 1
