import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.api import isoformat
from mailing.services.auth import create_client_api_key
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.campaigns import estimate_campaign_recipients

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )
    create_client_api_key(client=client, name="Campaign test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def audience(organization):
    return Audience.objects.create(
        organization=organization,
        name="DataTalksClub",
        slug="datatalks-club",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def put_campaign(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.put(
        reverse("mailing:api_campaign", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def get_campaign(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.get(
        reverse("mailing:api_campaign", args=[external_key]),
        payload,
        **auth_headers(raw_key),
    )


def queue_campaign_api(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_queue", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def cancel_campaign_api(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_cancel", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def preview_campaign_api(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_preview", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_campaign_test_send(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_test_send", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_campaign_recount(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_recount", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def get_campaign_recipients(django_client, external_key, query="", raw_key=API_KEY):
    return django_client.get(
        reverse("mailing:api_campaign_recipients", args=[external_key]) + query,
        **auth_headers(raw_key),
    )


def post_campaign_recipient_retry(django_client, external_key, recipient_id, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_recipient_retry", args=[external_key, recipient_id]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def campaign_payload(audience, api_client_record):
    return {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "subject": "Course starts tomorrow",
        "preview_text": "Start details",
        "html_body": "<p>Hello learner</p>",
        "text_body": "Hello learner",
        "category_tag": "course-reminders",
        "include_tags": ["python", "ml", "python"],
        "exclude_tags": ["inactive"],
        "metadata": {"course_slug": "ml-zoomcamp-2026"},
    }


def test_campaign_api_upserts_and_gets_by_external_key(client, audience, api_client_record):
    response = put_campaign(
        client,
        "cmp-course-start-2026",
        campaign_payload(audience, api_client_record) | {"recipient_list_key": "ml-zoomcamp-2026:@e"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    campaign = body["campaign"]
    assert campaign["external_key"] == "cmp-course-start-2026"
    assert campaign["audience"] == audience.slug
    assert campaign["client"] == api_client_record.slug
    assert campaign["status"] == CampaignStatus.DRAFT
    assert campaign["category_tag"] == "course-reminders"
    assert campaign["recipient_list_key"] == "ml-zoomcamp-2026:@e"
    assert campaign["include_tags"] == ["ml", "python"]
    assert campaign["exclude_tags"] == ["inactive"]
    assert campaign["metadata"] == {"course_slug": "ml-zoomcamp-2026"}

    update_payload = campaign_payload(audience, api_client_record) | {
        "subject": "Course starts today",
        "recipient_list_key": "ml-zoomcamp-2026:@e",
        "include_tags": ["ml"],
    }
    update = put_campaign(client, "cmp-course-start-2026", update_payload)

    assert update.status_code == 200
    assert update.json()["created"] is False
    assert update.json()["campaign"]["subject"] == "Course starts today"
    assert update.json()["campaign"]["include_tags"] == ["ml"]

    fetched = get_campaign(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert fetched.status_code == 200
    assert fetched.json()["campaign"]["subject"] == "Course starts today"
    assert fetched.json()["campaign"]["category_tag"] == "course-reminders"
    assert fetched.json()["campaign"]["recipient_list_key"] == "ml-zoomcamp-2026:@e"
    assert fetched.json()["campaign"]["metadata"] == {"course_slug": "ml-zoomcamp-2026"}
    assert Campaign.objects.count() == 1


def test_campaign_api_rejects_cross_audience_update(client, organization, audience, api_client_record):
    other_audience = Audience.objects.create(
        organization=organization,
        name="Other",
        slug="other",
    )
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="cmp-course-start-2026",
        subject="Existing",
        html_body="<p>Hello</p>",
    )
    payload = campaign_payload(other_audience, api_client_record)

    response = put_campaign(client, "cmp-course-start-2026", payload)

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"external_key": "audience_mismatch"}


def test_campaign_api_rejects_update_after_queue(client, audience, api_client_record):
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="queued-campaign",
        subject="Queued",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )

    response = put_campaign(
        client,
        "queued-campaign",
        campaign_payload(audience, api_client_record),
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"status": "not_editable"}


def test_campaign_api_rejects_non_object_metadata(client, audience, api_client_record):
    response = put_campaign(
        client,
        "cmp-course-start-2026",
        campaign_payload(audience, api_client_record) | {"metadata": ["invalid"]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"metadata": "must_be_object"}


def test_campaign_api_queue_snapshots_and_enqueues_pending_recipients(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    contact = Contact.objects.create(
        email="learner@example.com",
        verified_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    put = put_campaign(
        client,
        "cmp-course-start-2026",
        campaign_payload(audience, api_client_record) | {
            "include_tags": [],
            "exclude_tags": [],
        },
    )
    assert put.status_code == 201

    response = queue_campaign_api(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["batch_count"] == 1
    assert body["recipient_count"] == 1
    assert body["skipped_count"] == 0
    assert body["campaign"]["status"] == CampaignStatus.QUEUED
    assert len(enqueued) == 1
    assert enqueued[0]["contract"] == "campaign-email"
    assert enqueued[0]["campaign_recipient_ids"]

    replay = queue_campaign_api(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert replay.status_code == 202
    replay_body = replay.json()
    assert replay_body["queued"] is False
    assert replay_body["batch_count"] == 0
    assert replay_body["recipient_count"] == 1
    assert replay_body["skipped_count"] == 0
    assert replay_body["campaign"]["status"] == CampaignStatus.QUEUED
    assert len(enqueued) == 1


def test_campaign_api_cancels_draft_campaign(client, audience, api_client_record):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="draft-campaign",
        subject="Draft",
        html_body="<p>Hello</p>",
    )

    response = cancel_campaign_api(
        client,
        "draft-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.CANCELLED


def test_campaign_api_cancels_unsent_queued_campaign_and_stale_batch_does_not_send(
    client,
    audience,
    api_client_record,
):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="queued-campaign",
        subject="Queued",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
        recipient_count=1,
    )
    contact = Contact.objects.create(email="learner@example.com", verified_at=timezone.now())
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.PENDING,
    )

    response = cancel_campaign_api(
        client,
        "queued-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    campaign.refresh_from_db()
    recipient.refresh_from_db()
    assert campaign.status == CampaignStatus.CANCELLED
    assert campaign.recipient_count == 0
    assert campaign.skipped_count == 1
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.last_error == "campaign_cancelled"

    result = send_campaign_batch(
        {
            "campaign_id": campaign.id,
            "campaign_recipient_ids": [recipient.id],
        },
        ses_client=_FailingIfCalledSes(),
    )

    assert result.skipped_count == 1


def test_campaign_api_rejects_cancel_after_send_started(client, audience, api_client_record):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="sending-campaign",
        subject="Sending",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
        sent_count=1,
    )

    response = cancel_campaign_api(
        client,
        "sending-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"status": "not_cancellable"}
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.QUEUED


def test_campaign_api_preview_renders_campaign_body(client, audience, api_client_record, settings):
    settings.PUBLIC_BASE_URL = "https://mail.example.com"
    put = put_campaign(
        client,
        "preview-campaign",
        campaign_payload(audience, api_client_record)
        | {
            "html_body": '<p>Hello <a href="https://example.com/read">read</a></p>',
            "text_body": "Hello text",
        },
    )
    assert put.status_code == 201

    response = preview_campaign_api(
        client,
        "preview-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["subject"] == "Course starts tomorrow"
    assert "https://mail.example.com/t/c/preview-tracking-token" in preview["html_body"]
    assert "Unsubscribe or manage preferences" in preview["html_body"]
    assert "https://mail.example.com/unsubscribe/preview-unsubscribe-token" in preview["text_body"]


def test_campaign_api_test_send_sends_explicit_recipients_without_campaign_recipients(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    sent = []

    def fake_send(campaign, email):
        sent.append((campaign.external_key, email))
        return f"message-{len(sent)}"

    monkeypatch.setattr("mailing.services.api.send_campaign_test_message", fake_send)
    put = put_campaign(client, "test-send-campaign", campaign_payload(audience, api_client_record))
    assert put.status_code == 201

    response = post_campaign_test_send(
        client,
        "test-send-campaign",
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "emails": ["B@example.com", "a@example.com", "a@example.com"],
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["sent_count"] == 2
    assert body["recipients"] == [
        {"email": "a@example.com", "message_id": "message-1"},
        {"email": "b@example.com", "message_id": "message-2"},
    ]
    assert sent == [
        ("test-send-campaign", "a@example.com"),
        ("test-send-campaign", "b@example.com"),
    ]
    assert CampaignRecipient.objects.count() == 0


def campaign_recipient_row(campaign, email, *, contact_kwargs=None, **kwargs):
    contact = Contact.objects.create(email=email, **(contact_kwargs or {}))
    return CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=email, **kwargs)


def scope_query(audience, api_client_record, extra=""):
    return f"?audience={audience.slug}&client={api_client_record.slug}{extra}"


def test_campaign_api_recount_matches_send_estimate_without_exposing_identities(
    client,
    audience,
    api_client_record,
):
    now = timezone.now()
    for index, status in enumerate(
        [SubscriptionStatus.SUBSCRIBED, SubscriptionStatus.SUBSCRIBED, SubscriptionStatus.UNSUBSCRIBED]
    ):
        contact = Contact.objects.create(email=f"learner-{index}@example.com", verified_at=now)
        Subscription.objects.create(
            contact=contact,
            audience=audience,
            client=api_client_record,
            status=status,
        )
    tagged_contact = Contact.objects.create(email="tagged@example.com", verified_at=now)
    Subscription.objects.create(
        contact=tagged_contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    tag = Tag.objects.create(audience=audience, name="Python", slug="python")
    ContactTag.objects.create(contact=tagged_contact, tag=tag)

    unfiltered = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="recount-campaign",
        subject="Recount",
        html_body="<p>Hello</p>",
    )
    filtered = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="recount-tagged-campaign",
        subject="Recount tagged",
        html_body="<p>Hello</p>",
        include_tags=["python"],
    )

    response = post_campaign_recount(
        client,
        "recount-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 200
    body = response.json()
    estimate = estimate_campaign_recipients(unfiltered)
    assert body["campaign"]["external_key"] == "recount-campaign"
    assert body["total_candidates"] == estimate.total_candidates == 4
    assert body["tag_filtered_count"] == estimate.tag_filtered_count == 0
    assert body["recipient_count"] == estimate.recipient_count == 3
    assert body["skipped_count"] == estimate.skipped_count == 1
    assert body["skip_reason_counts"] == estimate.skip_reason_counts == {"client_unsubscribe": 1}
    assert "recipients" not in body
    assert "learner-0@example.com" not in response.content.decode()

    tagged_response = post_campaign_recount(
        client,
        "recount-tagged-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert tagged_response.status_code == 200
    tagged_body = tagged_response.json()
    tagged_estimate = estimate_campaign_recipients(filtered)
    assert tagged_body["total_candidates"] == tagged_estimate.total_candidates == 4
    assert tagged_body["tag_filtered_count"] == tagged_estimate.tag_filtered_count == 3
    assert tagged_body["recipient_count"] == tagged_estimate.recipient_count == 1
    assert tagged_body["skipped_count"] == tagged_estimate.skipped_count == 0
    assert "@example.com" not in tagged_response.content.decode()


def test_campaign_api_recount_requires_known_campaign(client, audience, api_client_record):
    response = post_campaign_recount(
        client,
        "missing-campaign",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"external_key": "not_found"}


def test_campaign_api_recipients_list_dispositions_and_status_filter(
    client,
    audience,
    api_client_record,
):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="recipients-campaign",
        subject="Recipients",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )
    sent_at = timezone.now() - timezone.timedelta(minutes=5)
    sent_recipient = campaign_recipient_row(
        campaign,
        "a-sent@example.com",
        status=CampaignRecipientStatus.SENT,
        ses_message_id="ses-1",
        sent_at=sent_at,
    )
    failed_recipient = campaign_recipient_row(
        campaign,
        "b-failed@example.com",
        status=CampaignRecipientStatus.FAILED,
        last_error="connection reset",
    )
    campaign_recipient_row(
        campaign,
        "c-skipped@example.com",
        status=CampaignRecipientStatus.SKIPPED,
        skip_reason=CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE,
    )

    response = get_campaign_recipients(
        client,
        "recipients-campaign",
        scope_query(audience, api_client_record),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["external_key"] == "recipients-campaign"
    assert body["count"] == 3
    assert body["status_counts"] == {"sent": 1, "failed": 1, "skipped": 1}
    assert [row["email"] for row in body["recipients"]] == sorted(row["email"] for row in body["recipients"])
    sent_row = next(row for row in body["recipients"] if row["id"] == sent_recipient.id)
    assert sent_row["status"] == CampaignRecipientStatus.SENT
    assert sent_row["ses_message_id"] == "ses-1"
    assert sent_row["sent_at"] == isoformat(sent_at)
    failed_row = next(row for row in body["recipients"] if row["id"] == failed_recipient.id)
    assert failed_row["status"] == CampaignRecipientStatus.FAILED
    assert failed_row["last_error"] == "connection reset"
    assert failed_row["ses_message_id"] == ""
    skipped_row = next(row for row in body["recipients"] if row["status"] == CampaignRecipientStatus.SKIPPED)
    assert skipped_row["skip_reason"] == CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE

    filtered = get_campaign_recipients(
        client,
        "recipients-campaign",
        scope_query(audience, api_client_record, "&status=failed"),
    )

    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["count"] == 1
    assert [row["id"] for row in filtered_body["recipients"]] == [failed_recipient.id]
    assert filtered_body["status_counts"] == {"sent": 1, "failed": 1, "skipped": 1}

    invalid = get_campaign_recipients(
        client,
        "recipients-campaign",
        scope_query(audience, api_client_record, "&status=not-a-status"),
    )

    assert invalid.status_code == 400
    assert invalid.json()["error"]["fields"] == {"status": "invalid"}


def test_campaign_api_retry_requeues_failed_recipient(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="retry-campaign",
        subject="Retry",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
        recipient_count=1,
    )
    recipient = campaign_recipient_row(
        campaign,
        "retry@example.com",
        status=CampaignRecipientStatus.FAILED,
        last_error="ses unavailable",
    )

    response = post_campaign_recipient_retry(
        client,
        "retry-campaign",
        recipient.id,
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["retried"] is True
    assert body["recipient"]["id"] == recipient.id
    assert body["recipient"]["status"] == CampaignRecipientStatus.PENDING
    assert body["campaign"]["status"] == CampaignStatus.QUEUED
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.PENDING
    assert recipient.last_error == "ses unavailable [retried from failed]"
    assert len(enqueued) == 1
    assert enqueued[0]["campaign_id"] == campaign.id
    assert enqueued[0]["campaign_recipient_ids"] == [recipient.id]
    assert enqueued[0]["idempotency_key"] == f"campaign-{campaign.id}-retry-{recipient.id}"
    event = EmailEvent.objects.get(campaign_recipient=recipient, event_type=EmailEventType.QUEUED)
    assert event.metadata == {"retry": True}


def test_campaign_api_retry_from_sent_campaign_reopens_sending(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="sent-campaign",
        subject="Sent",
        html_body="<p>Hello</p>",
        status=CampaignStatus.SENT,
    )
    recipient = campaign_recipient_row(
        campaign,
        "late-retry@example.com",
        status=CampaignRecipientStatus.FAILED,
        last_error="timeout",
    )

    response = post_campaign_recipient_retry(
        client,
        "sent-campaign",
        recipient.id,
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 202
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENDING


def test_campaign_api_retry_rejects_non_failed_recipient(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="sent-recipient-campaign",
        subject="Sent recipient",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )
    recipient = campaign_recipient_row(
        campaign,
        "already-sent@example.com",
        status=CampaignRecipientStatus.SENT,
        ses_message_id="ses-delivered",
    )

    response = post_campaign_recipient_retry(
        client,
        "sent-recipient-campaign",
        recipient.id,
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"recipient": "recipient_not_failed"}
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert enqueued == []


@pytest.mark.parametrize("campaign_status", [CampaignStatus.DRAFT, CampaignStatus.CANCELLED])
def test_campaign_api_retry_rejects_campaign_that_cannot_send(
    client,
    audience,
    api_client_record,
    monkeypatch,
    campaign_status,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key=f"blocked-{campaign_status}-campaign",
        subject="Blocked",
        html_body="<p>Hello</p>",
        status=campaign_status,
    )
    recipient = campaign_recipient_row(
        campaign,
        "blocked@example.com",
        status=CampaignRecipientStatus.FAILED,
        last_error="ses unavailable",
    )

    response = post_campaign_recipient_retry(
        client,
        f"blocked-{campaign_status}-campaign",
        recipient.id,
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"recipient": "campaign_cannot_send"}
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert enqueued == []


def test_campaign_api_retry_rejects_recipient_from_another_campaign(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="target-campaign",
        subject="Target",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )
    other_campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="other-campaign",
        subject="Other",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )
    recipient = campaign_recipient_row(
        other_campaign,
        "elsewhere@example.com",
        status=CampaignRecipientStatus.FAILED,
    )

    response = post_campaign_recipient_retry(
        client,
        "target-campaign",
        recipient.id,
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"recipient": "not_found"}
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert enqueued == []


class _FailingIfCalledSes:
    def send_email(self, **params):
        raise AssertionError("SES should not be called")
