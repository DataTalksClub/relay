"""Fault-injection harness for the claim -> send -> finalize send path (WP1).

Nothing here touches the network. ``FaultSes`` is a fake SES client with
latency and scripted faults; ``query_log`` records every SQL statement from
every thread; ``run_batch_subprocess`` runs a batch in a separate process so a
test can SIGKILL / SIGTERM it for real.

Fault vocabulary (``FaultSes(faults={call_number: fault})``):

- ``"throttle"``      ClientError Throttling (definite: nothing was sent)
- ``"reject"``        ClientError MessageRejected (definite rejection)
- ``"read_timeout"``  the message IS accepted, then the response is lost and
                      botocore's ReadTimeoutError is raised (ambiguous)
- ``"conn_reset"``    accepted, then ConnectionResetError (ambiguous)
- ``"crash_after_accept"``  accepted, then the process SIGKILLs itself
"""

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from botocore.exceptions import ClientError, ReadTimeoutError
from django.db import connection
from django.db.backends import utils as db_utils

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    Organization,
)

RUNNER = Path(__file__).with_name("claim_finalize_runner.py")


def client_error(code, status=400, operation="SendRawEmail"):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FaultSes:
    """Fake SES: latency, per-call scripted faults, and in-call DB assertions.

    ``accepted`` holds the message ids SES "delivered" (including calls whose
    response was lost), keyed by recipient address, so tests can compare what
    SES really did against what the database believes.
    """

    def __init__(self, latency=0.0, faults=None, assert_no_transaction=False, on_call=None):
        self.latency = latency
        self.faults = dict(faults or {})
        self.assert_no_transaction = assert_no_transaction
        self.on_call = on_call
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.accepted = {}
        self.transaction_violations = []
        self._lock = threading.Lock()

    def send_email(self, **params):
        return self._send(params)

    def send_raw_email(self, **params):
        return self._send(params)

    def _send(self, params):
        if self.assert_no_transaction and (connection.in_atomic_block or not connection.get_autocommit()):
            self.transaction_violations.append(threading.current_thread().name)
        with self._lock:
            self.calls += 1
            number = self.calls
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.on_call:
                self.on_call(number)
            time.sleep(self.latency)
            fault = self.faults.get(number)
            if fault == "throttle":
                raise client_error("Throttling")
            if fault == "reject":
                raise client_error("MessageRejected")
            message_id = f"ses-{number}"
            destinations = params.get("Destinations") or [params.get("Destination", {}).get("ToAddresses", [""])[0]]
            with self._lock:
                self.accepted[destinations[0]] = message_id
            if fault == "read_timeout":
                raise ReadTimeoutError(endpoint_url="https://email.eu-west-1.amazonaws.com")
            if fault == "conn_reset":
                raise ConnectionResetError("Connection reset by peer")
            if fault == "crash_after_accept":
                os.kill(os.getpid(), signal.SIGKILL)
            return {"MessageId": message_id}
        finally:
            with self._lock:
                self.in_flight -= 1


class _QueryLog(list):
    def matching(self, *needles):
        return [sql for sql in self if all(needle.lower() in sql.lower() for needle in needles)]


@contextmanager
def query_log():
    """Record every SQL statement executed by any thread (CaptureQueriesContext is per-connection)."""
    log = _QueryLog()
    lock = threading.Lock()
    original_execute = db_utils.CursorWrapper.execute
    original_executemany = db_utils.CursorWrapper.executemany

    def execute(self, sql, params=None):
        with lock:
            log.append(str(sql))
        return original_execute(self, sql, params)

    def executemany(self, sql, param_list):
        with lock:
            log.append(str(sql))
        return original_executemany(self, sql, param_list)

    db_utils.CursorWrapper.execute = execute
    db_utils.CursorWrapper.executemany = executemany
    try:
        yield log
    finally:
        db_utils.CursorWrapper.execute = original_execute
        db_utils.CursorWrapper.executemany = original_executemany


def big_html(links=400):
    rows = "".join(
        f'<p>Item {i} <a href="https://example.com/post/{i}?a=1&b=2&utm_source=n">read more about topic {i}</a></p>'
        + '<span style="color:#333;font-family:Arial">padding text padding text padding text</span>' * 2
        for i in range(links)
    )
    return f"<html><body>{rows}</body></html>"


def campaign_with_recipients(count, html_body="<p>Hi</p>", status="queued"):
    organization = Organization.objects.create(name="Org", slug="org")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status=status, html_body=html_body,
        text_body="Hi https://example.com/x",
    )
    contacts = [Contact.objects.create(email=f"person{i}@example.com") for i in range(count)]
    recipients = CampaignRecipient.objects.bulk_create(
        [CampaignRecipient(campaign=campaign, contact=c, email=c.email) for c in contacts]
    )
    payload = {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [r.id for r in recipients],
    }
    return campaign, payload


def statuses(campaign):
    counts = {}
    for status in CampaignRecipient.objects.filter(campaign=campaign).values_list("status", flat=True):
        counts[status] = counts.get(status, 0) + 1
    return counts


def database_url():
    cfg = connection.settings_dict
    auth = cfg["USER"] + (f":{cfg['PASSWORD']}" if cfg.get("PASSWORD") else "")
    return f"postgresql://{auth}@{cfg['HOST'] or 'localhost'}:{cfg['PORT'] or 5432}/{cfg['NAME']}"


def run_batch_subprocess(payload, *, latency=0.0, faults=None, concurrency=4, settings_env=None):
    """Start a send batch in its own process; returns the Popen (stdout = JSON call log on exit)."""
    import json

    env = {
        **os.environ,
        "DATABASE_URL": database_url(),
        "DJANGO_SETTINGS_MODULE": "relay.settings",
        "CF_PAYLOAD": json.dumps(payload),
        "CF_LATENCY": str(latency),
        "CF_FAULTS": json.dumps({str(k): v for k, v in (faults or {}).items()}),
        "CF_CONCURRENCY": str(concurrency),
        **(settings_env or {}),
    }
    return subprocess.Popen(
        [sys.executable, str(RUNNER)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
