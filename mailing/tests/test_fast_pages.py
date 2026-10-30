"""Fast-pages regression tests: query-count caps per key page.

Covers intake e5cd1579 (server-side p95 < 200 ms / p50 < 100 ms):
every key page renders within a fixed query budget so N+1 regressions
fail here instead of in production. Public pages stay correct per token
(no shared page cache); the cacheable static doc fragments are covered
by the reference-operations tests below.
"""

import os
import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection as _connection
from django.test.utils import CaptureQueriesContext as _CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from jobs.models import Job, JobStatus, Schedule
from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.documentation_views import _reference_operations, documentation_context
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
    RecipientList,
    Subscription,
    SubscriptionStatus,
    TransactionalCampaign,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.operator_ui import (
    ContactExplorerFilters,
    _latest_event_by_audience,
    audience_queryset,
    campaign_progress_map,
    campaign_send_progress,
    contact_explorer_queryset,
)
from mailing.services.tokens import ensure_campaign_recipient_tokens

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        username="operator", email="operator@example.com", password="password", is_staff=True
    )


@pytest.fixture
def seeded(client_record, audience):
    """A small but joined dataset: campaigns x recipients + events + transactional."""
    campaigns = []
    recipients = []
    for index in range(3):
        campaign = Campaign.objects.create(
            audience=audience,
            client=client_record,
            subject=f"Weekly update {index}",
            status="sent",
            sent_at=timezone.now(),
            recipient_count=5,
            sent_count=5,
        )
        campaigns.append(campaign)
        for slot in range(5):
            contact = Contact.objects.create(email=f"reader-{index}-{slot}@example.com")
            Subscription.objects.create(
                contact=contact, audience=audience, client=client_record, status=SubscriptionStatus.SUBSCRIBED
            )
            recipient = CampaignRecipient.objects.create(
                campaign=campaign,
                contact=contact,
                email=contact.email,
                status=CampaignRecipientStatus.SENT,
                sent_at=timezone.now(),
            )
            recipients.append(recipient)
            EmailEvent.objects.create(
                campaign=campaign,
                campaign_recipient=recipient,
                contact=contact,
                client=client_record,
                audience=audience,
                event_type=EmailEventType.SENT,
            )
    template = EmailTemplate.objects.create(client=client_record, key="welcome", name="Welcome", subject="Welcome")
    contact = Contact.objects.create(email="transactional@example.com")
    Subscription.objects.create(
        contact=contact, audience=audience, client=client_record, status=SubscriptionStatus.SUBSCRIBED
    )
    message = TransactionalMessage.objects.create(
        client=client_record,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        subject="Welcome",
        status=TransactionalMessageStatus.SENT,
        sent_at=timezone.now(),
    )
    recipient_list = RecipientList.objects.create(
        client=client_record, audience=audience, key="course-2026", name="Course 2026"
    )
    tx_campaign = TransactionalCampaign.objects.create(
        client=client_record,
        recipient_list=recipient_list,
        template_key=template.key,
        idempotency_key="tx-campaign-1",
    )
    message.campaign = tx_campaign
    message.save(update_fields=["campaign"])
    tokens = ensure_campaign_recipient_tokens(recipients[0])
    return {
        "campaigns": campaigns,
        "recipients": recipients,
        "message": message,
        "tx_campaign": tx_campaign,
        "tracking_token": tokens.tracking_token,
        "unsubscribe_token": tokens.unsubscribe_token,
    }


def login_staff(test_client, staff_user, client_record) -> None:
    test_client.force_login(staff_user)
    session = test_client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()


def test_health_ok(client):
    with django_assert_num_queries(3):
        response = client.get(reverse("mailing:health"))
    assert response.status_code == 200
    assert response.json()["service"] == "relay"


def test_readiness_ok(client):
    with django_assert_num_queries(2):
        response = client.get(reverse("mailing:readiness"))
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_dashboard_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(20):
        response = client.get(reverse("mailing:dashboard"))
    assert response.status_code == 200


def test_campaign_list_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(10):
        response = client.get(reverse("mailing:campaign_list"))
    assert response.status_code == 200


def test_campaign_detail_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    campaign = seeded["campaigns"][0]
    with django_assert_num_queries(16):
        response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]))
    assert response.status_code == 200


def test_campaign_detail_draft_estimate_query_budget(client, staff_user, client_record, audience):
    login_staff(client, staff_user, client_record)
    campaign = Campaign.objects.create(
        audience=audience, client=client_record, subject="Draft estimate", status="draft"
    )
    contact = Contact.objects.create(email="estimate-me@example.com")
    Subscription.objects.create(
        contact=contact, audience=audience, client=client_record, status=SubscriptionStatus.SUBSCRIBED
    )
    with django_assert_num_queries(16):
        response = client.get(reverse("mailing:campaign_detail", args=[campaign.pk]))
    assert response.status_code == 200


def test_campaign_progress_map_matches_single(client_record, audience):
    campaign = Campaign.objects.create(audience=audience, client=client_record, subject="Map check")
    contact = Contact.objects.create(email="map-check@example.com")
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
    )
    single = campaign_send_progress(campaign)
    batched = campaign_progress_map([campaign.pk])[campaign.pk]
    assert (batched.queued_count, batched.processed_count, batched.sent_count) == (
        single.queued_count,
        single.processed_count,
        single.sent_count,
    )
    assert campaign_progress_map([]) == {}


def test_audience_list_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(20):
        response = client.get(reverse("mailing:audience_list"))
    assert response.status_code == 200


def test_audience_detail_query_budget(client, staff_user, client_record, audience):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(16):
        response = client.get(reverse("mailing:audience_detail", args=[audience.id]))
    assert response.status_code == 200


def test_contact_search_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(20):
        response = client.get(reverse("mailing:contact_search"))
    assert response.status_code == 200


def test_contact_detail_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    contact = seeded["recipients"][0].contact
    with django_assert_num_queries(30):
        response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))
    assert response.status_code == 200


def test_transactional_campaign_detail_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(8):
        response = client.get(reverse("mailing:transactional_campaign_detail", args=[seeded["tx_campaign"].pk]))
    assert response.status_code == 200


def test_transactional_message_detail_query_budget(client, staff_user, client_record, seeded):
    login_staff(client, staff_user, client_record)
    with django_assert_num_queries(14):
        response = client.get(reverse("mailing:transactional_message_detail", args=[seeded["message"].pk]))
    assert response.status_code == 200


def test_jobs_list_query_budget(client, staff_user, client_record):
    login_staff(client, staff_user, client_record)
    Job.objects.create(
        client=client_record,
        task_type="system.echo",
        task={},
        status=JobStatus.QUEUED,
        run_after=timezone.now(),
    )
    with django_assert_num_queries(12):
        response = client.get(reverse("jobs:job_list"))
    assert response.status_code == 200


def test_jobs_detail_query_budget(client, staff_user, client_record):
    login_staff(client, staff_user, client_record)
    job = Job.objects.create(
        client=client_record,
        task_type="system.echo",
        task={},
        status=JobStatus.QUEUED,
        run_after=timezone.now(),
    )
    with django_assert_num_queries(8):
        response = client.get(reverse("jobs:job_detail", args=[job.pk]))
    assert response.status_code == 200


def test_schedule_list_query_budget(client, staff_user, client_record):
    login_staff(client, staff_user, client_record)
    Schedule.objects.create(
        client=client_record,
        name="daily",
        cron="0 9 * * *",
        task_type="system.echo",
        task={},
        next_run_at=timezone.now(),
    )
    with django_assert_num_queries(10):
        response = client.get(reverse("jobs:schedule_list"))
    assert response.status_code == 200


def test_api_docs_pages_query_budget(client, staff_user):
    client.force_login(staff_user)
    # Reference rows are parsed once from the static spec: no DB at all.
    with django_assert_num_queries(0):
        _reference_operations()
        _reference_operations()
    with django_assert_num_queries(0):
        documentation_context()
    with django_assert_num_queries(8):
        assert client.get(reverse("mailing:api_docs")).status_code == 200
    with django_assert_num_queries(8):
        assert client.get(reverse("mailing:api_docs_reference")).status_code == 200
    with django_assert_num_queries(6):
        response = client.get(reverse("mailing:api_docs_json"))
        assert response.status_code == 200


def test_reference_operations_cached_and_independent():
    first = _reference_operations()
    second = _reference_operations()
    assert first is second
    assert len(first) > 10
    assert all("method" in op and "contract" in op for op in first)


def test_documentation_context_cached_base_independent():
    first = documentation_context()
    second = documentation_context()
    assert first is not second
    assert first["workflow_examples"] == second["workflow_examples"]
    first["workflow_examples"][0]["slug"] = "mutated"
    assert second["workflow_examples"][0]["slug"] != "mutated"


def test_tracking_open_pixel(client, seeded):
    response = client.get(reverse("mailing:tracking_open", args=[seeded["tracking_token"]]))
    assert response.status_code == 200
    assert response["Cache-Control"].startswith("no-store")


def test_tracking_click_redirect(client, seeded):
    url = reverse("mailing:tracking_click", args=[seeded["tracking_token"]])
    response = client.get(url + "?u=https://example.com/welcome")
    assert response.status_code in (301, 302)


def test_public_unsubscribe_get(client, seeded):
    response = client.get(reverse("mailing:public_unsubscribe", args=[seeded["unsubscribe_token"]]))
    assert response.status_code == 200


def test_oidc_callback_server_side_budget_under_300ms(client):
    """DB + session part of the OIDC callback, excluding provider round-trips."""
    claims = {
        "nonce": "n",
        "email": "Operator@Example.com",
        "email_verified": True,
    }
    session = SessionStore()
    session["oidc"] = {"state": "s", "verifier": "v", "nonce": "n", "return_to": "/"}
    session.save()
    client.cookies["sessionid"] = session.session_key
    url = reverse("oidc-callback")
    with (
        mock.patch("relay.oidc._exchange", return_value={"id_token": "token"}),
        mock.patch("relay.oidc._verify", return_value=claims),
    ):
        started = time.perf_counter()
        with django_assert_num_queries(16):
            response = client.get(url + "?code=c&state=s")
        elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code in (302, 303)
    assert elapsed_ms < 300, f"callback server-side took {elapsed_ms:.1f} ms"


def test_latest_event_by_audience_breaks_timestamp_ties_without_full_hydration(client_record, audience):
    contact = Contact.objects.create(email="tie-break@example.com")
    campaign = Campaign.objects.create(audience=audience, client=client_record, subject="Tie break")
    same_time = timezone.now()
    events = EmailEvent.objects.bulk_create(
        [
            EmailEvent(
                campaign=campaign,
                contact=contact,
                client=client_record,
                audience=audience,
                event_type=EmailEventType.QUEUED,
            )
            for _ in range(10)
        ]
    )
    EmailEvent.objects.filter(pk__in=[e.pk for e in events]).update(created_at=same_time)
    with django_assert_num_queries(2) as ctx:
        result = _latest_event_by_audience([audience.pk], client_record)
    assert result[audience.pk].pk == max(e.pk for e in events)
    assert f"IN ({result[audience.pk].pk})" in ctx.captured_queries[1]["sql"]


def test_explorer_and_audience_count_strip_subquery_annotations(client_record, audience, seeded):
    filters = ContactExplorerFilters(client_id=client_record.id, audience_id=audience.id)
    with django_assert_num_queries(1) as ctx:
        count = contact_explorer_queryset(filters).count()
    assert count == 16
    count_sql = ctx.captured_queries[0]["sql"].upper()
    assert "CAMPAIGN_RECIPIENTS" not in count_sql
    assert "TRANSACTIONAL_MESSAGES" not in count_sql

    with django_assert_num_queries(1) as aud_ctx:
        assert audience_queryset(client_record).count() == 1
    aud_count_sql = aud_ctx.captured_queries[0]["sql"].upper()
    assert "SUBSCRIPTIONS" not in aud_count_sql
    assert "CAMPAIGNS" not in aud_count_sql


class django_assert_num_queries:
    """Minimal assertNumQueries replacement usable as a context manager."""

    def __init__(self, max_queries):
        self.max_queries = max_queries

    def __enter__(self):
        self.ctx = _CaptureQueriesContext(_connection)
        self.ctx.__enter__()
        return self.ctx

    def __exit__(self, exc_type, exc, tb):
        self.ctx.__exit__(exc_type, exc, tb)
        count = len(self.ctx)
        report = os.environ.get("FAST_PAGES_REPORT")
        if report:
            with open(report, "a") as fh:
                fh.write(f"{os.environ.get('PYTEST_CURRENT_TEST', '?')}: {count}\n")
        assert count <= self.max_queries, f"expected at most {self.max_queries} queries, got {count}:\n" + "\n".join(
            q["sql"][:200] for q in self.ctx.captured_queries[:count]
        )
        return False
