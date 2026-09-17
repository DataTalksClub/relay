import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db

API_KEY = "history-import-key"
OTHER_API_KEY = "other-history-import-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="AI Shipping Labs", slug="ai-shipping-labs")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="AI Shipping Labs", slug="aisl")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(organization=organization, name="AISL Website", slug="aisl-website")
    create_client_api_key(client=client, name="Import key", raw_api_key=API_KEY)
    return client


@pytest.fixture
def other_client():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")
    create_client_api_key(client=client, name="Other key", raw_api_key=OTHER_API_KEY)
    return client


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_json(client, url_name, payload, raw_key=API_KEY):
    return client.post(
        reverse(url_name),
        data=json.dumps(payload),
        content_type="application/json",
        **auth_headers(raw_key),
    )


def message_row(reference, *, email="learner@example.com", created_at="2024-02-03T09:00:00Z", **overrides):
    row = {
        "client_reference": reference,
        "email": email,
        "template_key": "welcome",
        "subject": "Welcome to AI Shipping Labs",
        "status": TransactionalMessageStatus.SENT,
        "created_at": created_at,
        "sent_at": created_at,
        "ses_message_id": f"ses-{reference}",
    }
    row.update(overrides)
    return row


def campaign_row(reference, *, created_at="2024-01-05T08:00:00Z", recipients=None):
    return {
        "client_reference": reference,
        "subject": "January newsletter",
        "status": CampaignStatus.SENT,
        "created_at": created_at,
        "sent_at": created_at,
        "recipients": recipients or [],
    }


def test_import_messages_creates_rows_with_their_original_timestamps(client, api_client_record):
    response = post_json(
        client,
        "mailing:api_import_messages",
        {
            "client": api_client_record.slug,
            "messages": [
                message_row(
                    "emaillog-1",
                    created_at="2023-11-02T10:15:00Z",
                    delivered_at="2023-11-02T10:15:30Z",
                    first_opened_at="2023-11-03T07:00:00Z",
                    open_count=2,
                )
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"total": 1, "created": 1, "skipped": 0}

    message = TransactionalMessage.objects.get(client=api_client_record, idempotency_key="emaillog-1")
    assert message.created_at == parse_datetime("2023-11-02T10:15:00Z")
    assert message.delivered_at == parse_datetime("2023-11-02T10:15:30Z")
    assert message.first_opened_at == parse_datetime("2023-11-03T07:00:00Z")
    assert message.open_count == 2
    assert message.status == TransactionalMessageStatus.SENT
    assert message.template_key == "welcome"
    assert message.email == "learner@example.com"
    assert message.contact.normalized_email == "learner@example.com"


def test_import_messages_is_idempotent_on_client_reference(client, api_client_record):
    payload = {
        "messages": [
            message_row("emaillog-1"),
            message_row("emaillog-2", email="second@example.com", created_at="2024-02-04T09:00:00Z"),
        ]
    }

    first = post_json(client, "mailing:api_import_messages", payload)
    rows_after_first = TransactionalMessage.objects.count()
    second = post_json(client, "mailing:api_import_messages", payload)

    assert first.json() == {"total": 2, "created": 2, "skipped": 0}
    assert second.json() == {"total": 2, "created": 0, "skipped": 2}
    assert rows_after_first == 2
    assert TransactionalMessage.objects.count() == 2


def test_import_messages_keeps_the_same_reference_separate_per_client(client, api_client_record, other_client):
    post_json(client, "mailing:api_import_messages", {"messages": [message_row("shared-reference")]})
    response = post_json(
        client,
        "mailing:api_import_messages",
        {"messages": [message_row("shared-reference")]},
        raw_key=OTHER_API_KEY,
    )

    assert response.json() == {"total": 1, "created": 1, "skipped": 0}
    assert TransactionalMessage.objects.filter(idempotency_key="shared-reference").count() == 2


def test_import_messages_creates_an_inactive_placeholder_template(client, api_client_record):
    post_json(client, "mailing:api_import_messages", {"messages": [message_row("emaillog-1")]})

    template = EmailTemplate.objects.get(client=api_client_record, key="welcome")
    assert template.is_active is False
    assert template.description == "Placeholder created by the history import."


def test_import_messages_reuses_an_existing_template(client, api_client_record):
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="welcome",
        name="Welcome",
        subject="Welcome",
        is_active=True,
    )

    post_json(client, "mailing:api_import_messages", {"messages": [message_row("emaillog-1")]})

    template.refresh_from_db()
    assert template.is_active is True
    assert EmailTemplate.objects.filter(client=api_client_record, key="welcome").count() == 1
    assert TransactionalMessage.objects.get(idempotency_key="emaillog-1").template_id == template.id


def test_import_messages_rejects_a_duplicate_reference_inside_one_batch(client, api_client_record):
    response = post_json(
        client,
        "mailing:api_import_messages",
        {"messages": [message_row("emaillog-1"), message_row("emaillog-1")]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"messages.1.client_reference": "duplicate_in_batch"}
    assert TransactionalMessage.objects.count() == 0


def test_import_messages_rejects_an_unsupported_status(client, api_client_record):
    response = post_json(
        client,
        "mailing:api_import_messages",
        {"messages": [message_row("emaillog-1", status="delivered")]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"messages.0.status": "unsupported"}
    assert TransactionalMessage.objects.count() == 0


def test_import_messages_rejects_another_clients_slug(client, api_client_record, other_client):
    response = post_json(
        client,
        "mailing:api_import_messages",
        {"client": other_client.slug, "messages": [message_row("emaillog-1")]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["fields"] == {"client": "forbidden"}
    assert TransactionalMessage.objects.count() == 0


def test_import_endpoints_require_authentication(client):
    for url_name, key in (
        ("mailing:api_import_messages", "messages"),
        ("mailing:api_import_events", "events"),
        ("mailing:api_import_campaigns", "campaigns"),
    ):
        response = client.post(
            reverse(url_name),
            data=json.dumps({key: []}),
            content_type="application/json",
        )
        assert response.status_code == 401, url_name
        assert response.json()["error"]["code"] == "missing_authorization"


def test_import_campaigns_creates_campaign_and_recipient_rows(client, api_client_record, audience):
    response = post_json(
        client,
        "mailing:api_import_campaigns",
        {
            "audience": audience.slug,
            "campaigns": [
                campaign_row(
                    "campaign-7",
                    recipients=[
                        {
                            "email": "learner@example.com",
                            "status": CampaignRecipientStatus.SENT,
                            "sent_at": "2024-01-05T08:01:00Z",
                            "delivered_at": "2024-01-05T08:01:20Z",
                            "open_count": 3,
                            "first_opened_at": "2024-01-05T09:00:00Z",
                        },
                        {
                            "email": "bounced@example.com",
                            "status": CampaignRecipientStatus.BOUNCED,
                            "sent_at": "2024-01-05T08:01:00Z",
                        },
                    ],
                )
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "total": 1,
        "created": 1,
        "skipped": 0,
        "recipients_total": 2,
        "recipients_created": 2,
        "recipients_skipped": 0,
    }

    campaign = Campaign.objects.get(client=api_client_record, external_key="campaign-7")
    assert campaign.audience_id == audience.id
    assert campaign.created_at == parse_datetime("2024-01-05T08:00:00Z")
    assert campaign.status == CampaignStatus.SENT
    assert campaign.recipient_count == 2
    assert campaign.delivered_count == 1
    assert campaign.unique_open_count == 1
    assert campaign.open_count == 3
    assert set(campaign.recipients.values_list("email", "status")) == {
        ("learner@example.com", CampaignRecipientStatus.SENT),
        ("bounced@example.com", CampaignRecipientStatus.BOUNCED),
    }


def test_import_campaigns_is_idempotent_for_campaigns_and_recipients(client, api_client_record, audience):
    payload = {
        "audience": audience.slug,
        "campaigns": [
            campaign_row(
                "campaign-7",
                recipients=[
                    {"email": "learner@example.com", "sent_at": "2024-01-05T08:01:00Z"},
                    {"email": "second@example.com", "sent_at": "2024-01-05T08:01:00Z"},
                ],
            )
        ],
    }

    first = post_json(client, "mailing:api_import_campaigns", payload)
    campaigns_after_first = Campaign.objects.count()
    recipients_after_first = CampaignRecipient.objects.count()
    second = post_json(client, "mailing:api_import_campaigns", payload)

    assert first.json()["created"] == 1
    assert first.json()["recipients_created"] == 2
    assert second.json()["created"] == 0
    assert second.json()["recipients_created"] == 0
    assert second.json()["recipients_skipped"] == 2
    assert (campaigns_after_first, recipients_after_first) == (1, 2)
    assert (Campaign.objects.count(), CampaignRecipient.objects.count()) == (1, 2)


def test_import_campaigns_requires_a_known_audience(client, api_client_record, audience):
    missing = post_json(client, "mailing:api_import_campaigns", {"campaigns": [campaign_row("campaign-7")]})
    unknown = post_json(
        client,
        "mailing:api_import_campaigns",
        {"audience": "not-an-audience", "campaigns": [campaign_row("campaign-7")]},
    )

    assert missing.status_code == 400
    assert missing.json()["error"]["fields"] == {"audience": "required"}
    assert unknown.status_code == 400
    assert unknown.json()["error"]["fields"] == {"audience": "not_found"}
    assert Campaign.objects.count() == 0


def test_import_events_links_messages_campaigns_and_recipients(client, api_client_record, audience):
    post_json(client, "mailing:api_import_messages", {"messages": [message_row("emaillog-1")]})
    post_json(
        client,
        "mailing:api_import_campaigns",
        {
            "audience": audience.slug,
            "campaigns": [
                campaign_row(
                    "campaign-7",
                    recipients=[{"email": "learner@example.com", "sent_at": "2024-01-05T08:01:00Z"}],
                )
            ],
        },
    )

    response = post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [
                {
                    "client_reference": "sesevent-1",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.BOUNCE,
                    "created_at": "2024-02-03T09:05:00Z",
                    "message_reference": "emaillog-1",
                    "metadata": {"bounce_type": "Permanent"},
                },
                {
                    "client_reference": "sesevent-2",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.CLICK,
                    "created_at": "2024-01-05T09:30:00Z",
                    "campaign_reference": "campaign-7",
                    "url": "https://aishippinglabs.test/course",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"total": 2, "created": 2, "skipped": 0}

    bounce = EmailEvent.objects.get(client_reference="sesevent-1")
    assert bounce.event_type == EmailEventType.BOUNCE
    assert bounce.created_at == parse_datetime("2024-02-03T09:05:00Z")
    assert bounce.transactional_message.idempotency_key == "emaillog-1"
    assert bounce.client_id == api_client_record.id
    assert bounce.audience_id == audience.id

    click = EmailEvent.objects.get(client_reference="sesevent-2")
    assert click.created_at == parse_datetime("2024-01-05T09:30:00Z")
    assert click.campaign.external_key == "campaign-7"
    assert click.campaign_recipient.email == "learner@example.com"
    assert click.url == "https://aishippinglabs.test/course"


def test_import_events_is_idempotent_on_client_reference(client, api_client_record, audience):
    payload = {
        "audience": audience.slug,
        "events": [
            {
                "client_reference": "sesevent-1",
                "email": "learner@example.com",
                "event_type": EmailEventType.COMPLAINT,
                "created_at": "2024-02-03T09:05:00Z",
            },
            {
                "client_reference": "sesevent-2",
                "email": "learner@example.com",
                "event_type": EmailEventType.OPEN,
                "created_at": "2024-02-03T09:06:00Z",
            },
        ],
    }

    first = post_json(client, "mailing:api_import_events", payload)
    rows_after_first = EmailEvent.objects.count()
    second = post_json(client, "mailing:api_import_events", payload)

    assert first.json() == {"total": 2, "created": 2, "skipped": 0}
    assert second.json() == {"total": 2, "created": 0, "skipped": 2}
    assert rows_after_first == 2
    assert EmailEvent.objects.count() == 2


def test_import_events_rejects_an_unknown_message_reference(client, api_client_record, audience):
    response = post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [
                {
                    "client_reference": "sesevent-1",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.BOUNCE,
                    "created_at": "2024-02-03T09:05:00Z",
                    "message_reference": "never-imported",
                }
            ],
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"events.0.message_reference": "not_found"}
    assert EmailEvent.objects.count() == 0


def test_import_events_rejects_a_provider_event_id_relay_already_holds(client, api_client_record, audience):
    def event(reference, provider_event_id):
        return {
            "client_reference": reference,
            "email": "learner@example.com",
            "event_type": EmailEventType.BOUNCE,
            "created_at": "2024-02-03T09:05:00Z",
            "provider_event_id": provider_event_id,
        }

    accepted = post_json(
        client,
        "mailing:api_import_events",
        {"audience": audience.slug, "events": [event("sesevent-1", "sns-abc")]},
    )
    reused = post_json(
        client,
        "mailing:api_import_events",
        {"audience": audience.slug, "events": [event("sesevent-2", "sns-abc")]},
    )
    duplicated = post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [event("sesevent-3", "sns-def"), event("sesevent-4", "sns-def")],
        },
    )

    assert accepted.json() == {"total": 1, "created": 1, "skipped": 0}
    assert reused.status_code == 400
    assert reused.json()["error"]["fields"] == {"events.0.provider_event_id": "already_recorded"}
    assert duplicated.status_code == 400
    assert duplicated.json()["error"]["fields"] == {"events.1.provider_event_id": "duplicate_in_batch"}
    assert EmailEvent.objects.count() == 1


def test_import_events_rejects_an_unsupported_event_type(client, api_client_record, audience):
    response = post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [
                {
                    "client_reference": "sesevent-1",
                    "email": "learner@example.com",
                    "event_type": "rendering_failure",
                    "created_at": "2024-02-03T09:05:00Z",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"events.0.event_type": "unsupported"}
    assert EmailEvent.objects.count() == 0


def test_contact_history_interleaves_imported_and_live_rows_in_time_order(client, api_client_record, audience):
    post_json(
        client,
        "mailing:api_import_campaigns",
        {
            "audience": audience.slug,
            "campaigns": [
                campaign_row(
                    "campaign-7",
                    created_at="2023-05-01T08:00:00Z",
                    recipients=[{"email": "learner@example.com", "sent_at": "2023-05-01T08:01:00Z"}],
                ),
                campaign_row(
                    "campaign-8",
                    created_at="2023-09-01T08:00:00Z",
                    recipients=[{"email": "learner@example.com", "sent_at": "2023-09-01T08:01:00Z"}],
                ),
            ],
        },
    )
    post_json(
        client,
        "mailing:api_import_messages",
        {
            "messages": [
                message_row("emaillog-old", created_at="2023-06-01T10:00:00Z"),
                message_row("emaillog-newer", created_at="2023-12-01T10:00:00Z"),
            ]
        },
    )
    post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [
                {
                    "client_reference": "sesevent-old",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.DELIVERED,
                    "created_at": "2023-06-01T10:00:30Z",
                    "message_reference": "emaillog-old",
                },
                {
                    "client_reference": "sesevent-newer",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.OPEN,
                    "created_at": "2023-12-01T11:00:00Z",
                    "message_reference": "emaillog-newer",
                },
            ],
        },
    )

    contact = Contact.objects.get(normalized_email="learner@example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
        verified_at=timezone.now(),
    )
    # A live event recorded now, after the import, must still sort above every
    # imported row even though the imported rows were inserted later.
    live_event = EmailEvent.objects.create(
        contact=contact,
        client=api_client_record,
        audience=audience,
        event_type=EmailEventType.CLICK,
        url="https://aishippinglabs.test/live",
    )

    response = client.get(
        reverse("mailing:api_contact_history", args=[contact.id]),
        {"audience": audience.slug, "client": api_client_record.slug, "limit": "50"},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()

    event_times = [item["created_at"] for item in body["events"]]
    assert event_times == sorted(event_times, reverse=True)
    assert body["events"][0]["id"] == live_event.id
    assert [item["event_type"] for item in body["events"]] == ["click", "open", "delivered"]

    message_times = [item["created_at"] for item in body["transactional_messages"]]
    assert message_times == sorted(message_times, reverse=True)
    assert [item["template_key"] for item in body["transactional_messages"]] == ["welcome", "welcome"]
    assert message_times == ["2023-12-01T10:00:00Z", "2023-06-01T10:00:00Z"]

    campaign_times = [item["created_at"] for item in body["campaign_recipients"]]
    assert campaign_times == sorted(campaign_times, reverse=True)
    assert [item["campaign"]["subject"] for item in body["campaign_recipients"]] == [
        "January newsletter",
        "January newsletter",
    ]
    assert campaign_times == ["2023-09-01T08:00:00Z", "2023-05-01T08:00:00Z"]


def test_contact_history_pages_events_in_time_order(client, api_client_record, audience):
    post_json(
        client,
        "mailing:api_import_events",
        {
            "audience": audience.slug,
            "events": [
                {
                    "client_reference": f"sesevent-{index}",
                    "email": "learner@example.com",
                    "event_type": EmailEventType.OPEN,
                    "created_at": f"2023-0{index}-01T10:00:00Z",
                }
                for index in (1, 2, 3)
            ],
        },
    )
    contact = Contact.objects.get(normalized_email="learner@example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
        verified_at=timezone.now(),
    )

    first_page = client.get(
        reverse("mailing:api_contact_history", args=[contact.id]),
        {"audience": audience.slug, "client": api_client_record.slug, "limit": "2"},
        **auth_headers(),
    ).json()
    second_page = client.get(
        reverse("mailing:api_contact_history", args=[contact.id]),
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "limit": "2",
            "cursor": first_page["next_cursor"],
        },
        **auth_headers(),
    ).json()

    assert [item["created_at"] for item in first_page["events"]] == [
        "2023-03-01T10:00:00Z",
        "2023-02-01T10:00:00Z",
    ]
    assert [item["created_at"] for item in second_page["events"]] == ["2023-01-01T10:00:00Z"]
    assert second_page["next_cursor"] is None


def test_import_history_command_imports_every_section_once(tmp_path, api_client_record, audience):
    document = {
        "client": api_client_record.slug,
        "audience": audience.slug,
        "campaigns": [
            campaign_row(
                "campaign-7",
                recipients=[{"email": "learner@example.com", "sent_at": "2024-01-05T08:01:00Z"}],
            )
        ],
        "messages": [message_row("emaillog-1")],
        "events": [
            {
                "client_reference": "sesevent-1",
                "email": "learner@example.com",
                "event_type": EmailEventType.DELIVERED,
                "created_at": "2024-02-03T09:00:30Z",
                "message_reference": "emaillog-1",
            }
        ],
    }
    path = tmp_path / "history.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    first_out = StringIO()
    call_command("import_history", "--from-json", str(path), stdout=first_out)
    counts_after_first = (
        Campaign.objects.count(),
        CampaignRecipient.objects.count(),
        TransactionalMessage.objects.count(),
        EmailEvent.objects.count(),
    )

    second_out = StringIO()
    call_command("import_history", "--from-json", str(path), stdout=second_out)

    first_summary = json.loads(first_out.getvalue())
    second_summary = json.loads(second_out.getvalue())

    assert first_summary["campaigns"]["created"] == 1
    assert first_summary["campaigns"]["recipients_created"] == 1
    assert first_summary["messages"] == {"total": 1, "created": 1, "skipped": 0}
    assert first_summary["events"] == {"total": 1, "created": 1, "skipped": 0}

    assert second_summary["campaigns"]["created"] == 0
    assert second_summary["campaigns"]["recipients_created"] == 0
    assert second_summary["messages"] == {"total": 1, "created": 0, "skipped": 1}
    assert second_summary["events"] == {"total": 1, "created": 0, "skipped": 1}

    assert counts_after_first == (1, 1, 1, 1)
    assert (
        Campaign.objects.count(),
        CampaignRecipient.objects.count(),
        TransactionalMessage.objects.count(),
        EmailEvent.objects.count(),
    ) == (1, 1, 1, 1)


def test_import_history_command_rejects_an_unknown_client(tmp_path, api_client_record):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"messages": [message_row("emaillog-1")]}), encoding="utf-8")

    with pytest.raises(CommandError, match="No client slug"):
        call_command("import_history", "--from-json", str(path))

    with pytest.raises(CommandError, match="Unknown client slug: ghost"):
        call_command("import_history", "--from-json", str(path), "--client", "ghost")

    assert TransactionalMessage.objects.count() == 0


def test_import_history_command_reports_validation_errors_and_writes_nothing(tmp_path, api_client_record):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "client": api_client_record.slug,
                "messages": [message_row("emaillog-1"), message_row("emaillog-2", created_at="not-a-date")],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="messages.1.created_at"):
        call_command("import_history", "--from-json", str(path))

    assert TransactionalMessage.objects.count() == 0
