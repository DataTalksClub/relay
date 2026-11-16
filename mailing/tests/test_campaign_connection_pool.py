"""Bound PostgreSQL sockets independently of in-flight SES requests."""

import threading
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test import override_settings

from mailing.db_pool import release_pooled_connection
from mailing.services import campaign_sender as sender
from mailing.tests.test_campaign_send_throughput import _campaign_with_recipients, _SlowSes


def test_pooled_concurrency_is_bounded_at_32(settings, monkeypatch):
    settings.CAMPAIGN_SEND_CONCURRENCY = 100
    monkeypatch.setitem(connection.settings_dict["OPTIONS"], "pool", {"max_size": 6})
    assert sender._configured_send_concurrency() == 32


def test_release_never_closes_an_atomic_transaction(monkeypatch):
    monkeypatch.setitem(connection.settings_dict["OPTIONS"], "pool", {"max_size": 6})
    monkeypatch.setattr(connection, "in_atomic_block", True)
    close = Mock()
    monkeypatch.setattr(connection, "close", close)
    release_pooled_connection()
    close.assert_not_called()


@pytest.mark.skipif(
    connection.vendor != "postgresql" or not connection.settings_dict["OPTIONS"].get("pool"),
    reason="run with PostgreSQL and RELAY_DB_POOL_MAX_SIZE=6",
)
@pytest.mark.django_db(transaction=True)
@override_settings(SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=240.0, CAMPAIGN_SEND_CONCURRENCY=32)
def test_32_sends_share_six_pg_sockets_across_repeated_batches():
    campaign, payload = _campaign_with_recipients(256)
    seen = set()
    lock = threading.Lock()

    class CheckedSes(_SlowSes):
        def _send(self):
            # The sender returned its checkout before waiting on the network.
            assert connection.connection is None
            with lock:
                seen.add(threading.get_ident())
            return super()._send()

    ses = CheckedSes(latency=0.1)
    ids = payload["campaign_recipient_ids"]
    try:
        for part in (ids[:128], ids[128:]):
            result = sender.send_campaign_batch({**payload, "campaign_recipient_ids": part}, ses_client=ses)
            assert result.sent_count == 128
        assert ses.calls == 256
        assert ses.max_in_flight > 12
        assert len(seen) > 12
        assert connection.pool.get_stats()["pool_size"] <= 6
        campaign.refresh_from_db()
        assert campaign.sent_count == 256
    finally:
        sender.shutdown_send_pool()
