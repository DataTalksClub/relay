"""Per-email DB work and per-phase batch timings of campaign sends."""

import logging
import re

import pytest
from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaign_sender import send_campaign_batch

pytestmark = pytest.mark.django_db


SEND_PHASES = ("lock_sql", "render_mime", "limiter_wait", "ses_call", "commit")
# SQLite: 9 after, 10 before (the second recipient SELECT ... FOR UPDATE in tokens.py).
# PostgreSQL adds the one FOR SHARE campaign lock query per email to both.
# The hard-stop halt flag adds one indexed PK SELECT per email (the drain-loop
# check); the pre-send suppression recheck adds one subscription query (both
# scopes; contacts that fail it are skipped, never sent).
# WP2's global SES bucket adds exactly 2 per email on top: one SELECT of the
# bucket row plus one conditional UPDATE that consumes the permit (shortage
# performs no write; BEGIN/COMMIT are gone — autocommit statements only).
EXPECTED_QUERIES_PER_EMAIL = 13 + (1 if connection.vendor == "postgresql" else 0)


class _FakeSes:
    def __init__(self):
        self.calls = 0

    def send_email(self, **params):
        self.calls += 1
        return {"MessageId": f"ses-{self.calls}"}

    send_raw_email = send_email


def _batch(count, tag="a"):
    organization = Organization.objects.create(name="Org", slug=f"org-{tag}")
    audience = Audience.objects.create(organization=organization, name="News", slug=f"news-{tag}")
    client = Client.objects.create(organization=organization, name="Client", slug=f"client-{tag}")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status="queued", html_body="<p>Hi</p>", text_body="Hi"
    )
    contacts = [Contact.objects.create(email=f"person{i}-{tag}@example.com", verified_at=timezone.now()) for i in range(count)]
    # The pre-send suppression recheck skips anything that is not subscribed.
    Subscription.objects.bulk_create(
        [
            Subscription(contact=c, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED)
            for c in contacts
        ]
    )
    recipients = CampaignRecipient.objects.bulk_create(
        [CampaignRecipient(campaign=campaign, contact=c, email=c.email) for c in contacts]
    )
    return {"campaign_id": campaign.id, "campaign_recipient_ids": [r.id for r in recipients]}


def _queries(count, tag):
    payload = _batch(count, tag)
    with CaptureQueriesContext(connection) as ctx:
        send_campaign_batch(payload, ses_client=_FakeSes())
    return [q["sql"] for q in ctx.captured_queries]


def test_queries_per_email_marginal_cost_and_no_duplicate_work():
    # Marginal queries per email = (N=12 total - N=2 total) / 10; per-batch fixed
    # cost cancels out.
    small = _queries(2, "s")
    large = _queries(12, "l")
    per_email = (len(large) - len(small)) / 10
    print(f"queries_per_email={per_email}")
    assert per_email <= EXPECTED_QUERIES_PER_EMAIL, per_email

    # Claim-send-finalize: recipients are claimed in chunks (one SELECT per chunk, not per
    # email), no query locks a row, and the campaign body is loaded once per batch.
    claim_selects = [q for q in large if 'FROM "campaign_recipients"' in q and '"claim_owner" = ' in q]
    assert len(claim_selects) == -(-12 // settings.CAMPAIGN_CLAIM_CHUNK), len(claim_selects)
    assert not [q for q in large if "FOR UPDATE" in q or "FOR SHARE" in q], "send path must not lock rows"
    full_campaign_loads = [q for q in large if 'FROM "campaigns"' in q and '"campaigns"."html_body"' in q.split("FROM")[0]]
    assert len(full_campaign_loads) == 1, len(full_campaign_loads)  # the batch context; independent of N


@override_settings(SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_batch_log_has_phase_breakdown_and_cpu_without_pii(caplog):
    payload = _batch(25)
    with caplog.at_level(logging.INFO, logger="mailing.services.campaign_sender"):
        send_campaign_batch(payload, ses_client=_FakeSes())
    lines = [r.getMessage() for r in caplog.records if "Campaign batch sent" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    for phase in SEND_PHASES:
        assert re.search(rf"{phase}_ms=\d+/[\d.]+/[\d.]+", line), (phase, line)
    assert "emails=25" in line
    assert re.search(r"worker_cpu_s=\d+\.\d+", line)
    assert re.search(r"cpu_ms_per_email=\d+\.\d", line)
    assert "@" not in line
    assert "example.com" not in line
    assert "person" not in line
