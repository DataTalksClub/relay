from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.campaign_test_tasks import enqueue_campaign_test, send_campaign_test
from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, CampaignRecipient, Client, Contact, EmailTemplate, Organization
from taskdeck.models import TaskRun

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    org = Organization.objects.create(name="Review", slug="review")
    integration = Client.objects.create(organization=org, name="Newsletter", slug="newsletter")
    audience = Audience.objects.create(organization=org, name="Members", slug="members")
    operator = get_user_model().objects.create_user(username="reviewer", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.pk
    session.save()
    campaign = Campaign.objects.create(client=integration, audience=audience, subject="Review me",
                                       html_body="<h1>Hello</h1>", text_body="Hello")
    return integration, campaign


def test_test_send_only_explicit_addresses_no_snapshot(setup, client):
    integration, campaign = setup
    with patch("mailing.campaign_test_tasks.send_campaign_test_message", return_value="test-id") as send:
        response = client.post(reverse("mailing:campaign_test_send", args=[campaign.pk]), {
            "test_emails": "Me@example.com, me@example.com\nother@example.com", "confirm_test": "1",
            "operator_client_id": integration.pk,
        })
    assert response.status_code == 302
    send.assert_not_called()
    task = DBTaskResult.objects.get()
    assert task.args_kwargs["args"][1] == ["me@example.com", "other@example.com"]
    assert task.args_kwargs["args"][0]["subject"] == "Review me"
    run = TaskRun.objects.get(result_id=str(task.pk))
    assert run.owner_id == str(integration.pk)
    assert run.entity_id == str(campaign.pk)
    assert run.status == "queued"
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert campaign.recipient_count == 0
    assert not campaign.recipients.exists()


@pytest.mark.parametrize("payload", [
    {"test_emails": "me@example.com"},
    {"test_emails": "bad-address", "confirm_test": "1"},
    {"test_emails": ",".join(f"user{i}@example.com" for i in range(26)), "confirm_test": "1"},
    {"test_emails": "me@example.com", "confirm_test": "1", "operator_client_id": "999"},
])
def test_test_send_rejects_unconfirmed_invalid_excess_or_stale(setup, client, payload):
    _, campaign = setup
    with patch("mailing.campaign_test_tasks.send_campaign_test_message") as send:
        response = client.post(reverse("mailing:campaign_test_send", args=[campaign.pk]), payload)
    assert response.status_code == 302
    send.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_partial_test_failure_reports_accepted_count(setup, client):
    _, campaign = setup
    error = ClientError({"Error": {"Code": "MessageRejected", "Message": "Rejected"}}, "SendEmail")
    with patch("mailing.campaign_test_tasks.send_campaign_test_message", side_effect=["id", error]) as send:
        response = client.post(reverse("mailing:campaign_test_send", args=[campaign.pk]), {
            "test_emails": "a@example.com,b@example.com,c@example.com", "confirm_test": "1",
        }, follow=True)
        send.assert_not_called()
        assert b"Test queued" in response.content
        call_command("db_worker", "--batch", "--verbosity", "0")
        assert send.call_count == 2
        # Finished jobs are not automatically requeued, including a partial test.
        call_command("db_worker", "--batch", "--verbosity", "0")
        assert send.call_count == 2
    outcome = DBTaskResult.objects.get().return_value
    assert outcome["status"] == "partial"
    assert outcome["accepted_count"] == 1
    assert outcome["unconfirmed_email"] == "b@example.com"
    assert outcome["not_attempted"] == ["c@example.com"]
    assert "1 provider accepted; 1 unconfirmed; 1 not attempted" in TaskRun.objects.get().message
    assert not campaign.recipients.exists()


@pytest.mark.parametrize("sample,expected", [
    ('{"name": "Updated"}', b"&lt;h1&gt;Updated&lt;/h1&gt;"),
    ('{}', b"Add a non-empty value"),
    ('[]', b"Use a JSON object"),
    ('not json', b"Enter valid JSON"),
])
def test_sample_context_validated_rendered_without_persistence(setup, client, sample, expected):
    integration, _ = setup
    template = EmailTemplate.objects.create(client=integration, name="Welcome", key="welcome",
                                           is_transactional=True, subject="Hi {{ name }}",
                                           html_body="<h1>{{ name }}</h1>", required_context=["name"],
                                           example_context={"name": "Original"})
    response = client.post(reverse("mailing:template_detail", args=[template.pk]), {
        "sample_context": sample, "operator_client_id": integration.pk,
    })
    assert response.status_code == 200
    assert expected in response.content
    template.refresh_from_db()
    assert template.example_context == {"name": "Original"}
    assert not template.transactional_messages.exists()


def test_custom_sample_remains_in_sandbox_and_preview_has_width_controls(setup, client):
    integration, campaign = setup
    template = EmailTemplate.objects.create(client=integration, name="Safe preview", key="safe",
                                           is_transactional=True, html_body="{{ html }}")
    response = client.post(reverse("mailing:template_detail", args=[template.pk]), {
        "sample_context": '{"html":"<script>alert(1)</script><h1>Safe</h1>"}',
    })
    assert b'sandbox=""' in response.content
    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content
    assert b"Mobile preview" in response.content
    detail = client.get(reverse("mailing:campaign_detail", args=[campaign.pk]))
    assert b"Mobile preview" in detail.content
    assert b"Send test email" in detail.content


@pytest.mark.parametrize("status", ["draft", "queued"])
def test_cancel_supported_lifecycle_without_external_key(setup, client, status):
    _, campaign = setup
    campaign.status = status
    campaign.save()
    response = client.post(reverse("mailing:campaign_cancel", args=[campaign.pk]), {"confirm_cancel": "1"})
    assert response.status_code == 302
    campaign.refresh_from_db()
    assert campaign.status == "cancelled"


@pytest.mark.parametrize("status", ["sending", "sent"])
def test_cancel_rejects_started_campaign(setup, client, status):
    _, campaign = setup
    campaign.status = status
    campaign.save()
    client.post(reverse("mailing:campaign_cancel", args=[campaign.pk]), {"confirm_cancel": "1"})
    campaign.refresh_from_db()
    assert campaign.status == status


def test_cancel_requires_confirmation(setup, client):
    _, campaign = setup
    client.post(reverse("mailing:campaign_cancel", args=[campaign.pk]), {})
    campaign.refresh_from_db()
    assert campaign.status == "draft"


def test_bad_template_syntax_is_visible_error_not_server_error(setup, client):
    integration, _ = setup
    template = EmailTemplate.objects.create(client=integration, name="Bad template", key="bad",
                                           is_transactional=True, html_body="{% definitely_invalid %}")
    response = client.get(reverse("mailing:template_detail", args=[template.pk]))
    assert response.status_code == 200
    assert b"could not be rendered" in response.content


@pytest.mark.parametrize("recipient_status,sent_at,cancelled", [
    ("pending", None, True), ("bounced", "sent", False),
])
def test_cancel_queued_preserves_historical_send_guard(setup, client, recipient_status, sent_at, cancelled):
    _, campaign = setup
    campaign.status = "queued"
    campaign.recipient_count = 1
    campaign.save()
    contact = Contact.objects.create(email="person@example.com", normalized_email="person@example.com")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email,
                                                status=recipient_status,
                                                sent_at=timezone.now() if sent_at else None)
    client.post(reverse("mailing:campaign_cancel", args=[campaign.pk]), {"confirm_cancel": "1"})
    campaign.refresh_from_db()
    recipient.refresh_from_db()
    assert (campaign.status == "cancelled") == cancelled
    if cancelled:
        assert recipient.status == "skipped"
        assert recipient.last_error == "campaign_cancelled"
    else:
        assert recipient.status == "bounced"


def test_get_test_send_and_cross_scope_post_never_send(setup, client):
    integration, campaign = setup
    other = Client.objects.create(organization=integration.organization, name="Other", slug="other")
    outside = Campaign.objects.create(client=other, audience=campaign.audience, subject="Other", html_body="Hi")
    with patch("mailing.campaign_test_tasks.send_campaign_test_message") as send:
        assert client.get(reverse("mailing:campaign_test_send", args=[campaign.pk])).status_code == 405
        assert client.post(reverse("mailing:campaign_test_send", args=[outside.pk]), {
            "test_emails": "me@example.com", "confirm_test": "1",
        }).status_code == 404
    send.assert_not_called()



def test_queued_test_and_projection_rollback_together(setup):
    _, campaign = setup
    with patch("mailing.campaign_test_tasks.send_campaign_test_message") as send:
        with pytest.raises(ValueError), transaction.atomic():
            enqueue_campaign_test(campaign, ["a@example.com"])
            assert DBTaskResult.objects.count() == 1
            raise ValueError("rollback")
    send.assert_not_called()
    assert not DBTaskResult.objects.exists()
    assert not TaskRun.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_worker_uses_confirmed_content_snapshot_and_persisted_replay_guard(setup, client):
    _, campaign = setup
    client.post(reverse("mailing:campaign_test_send", args=[campaign.pk]), {
        "test_emails": "a@example.com", "confirm_test": "1",
    })
    campaign.subject = "Edited after confirmation"
    campaign.html_body = "<p>Changed</p>"
    campaign.save()
    task = DBTaskResult.objects.get()
    with patch("mailing.campaign_test_tasks.send_campaign_test_message", return_value="provider-id") as send:
        call_command("db_worker", "--batch", "--verbosity", "0")
        assert send.call_args.args[0].subject == "Review me"
        assert send.call_args.args[0].html_body == "<h1>Hello</h1>"
        # Even re-entering the same confirmed job directly cannot repeat SES.
        repeat = send_campaign_test.call(*task.args_kwargs["args"])
        assert repeat["status"] == "already_attempted"
        send.assert_called_once()
    task.refresh_from_db()
    assert task.return_value["status"] == "provider_accepted"
    assert task.return_value["accepted"][0]["message_id"] == "provider-id"
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert not campaign.recipients.exists()


@override_settings(TASKS={"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}})
def test_immediate_backend_cannot_send_from_request(setup, client):
    _, campaign = setup
    with patch("mailing.campaign_test_tasks.send_campaign_test_message") as send:
        response = client.post(reverse("mailing:campaign_test_send", args=[campaign.pk]), {
            "test_emails": "a@example.com", "confirm_test": "1",
        }, follow=True)
    send.assert_not_called()
    assert b"Configure a deferred background worker" in response.content
    assert not DBTaskResult.objects.exists()


def test_claim_before_provider_call_prevents_retry_after_uncertain_worker_stop(setup):
    _, campaign = setup
    result = enqueue_campaign_test(campaign, ["a@example.com", "b@example.com"])
    task = DBTaskResult.objects.get(pk=result.id)
    # A worker could stop after provider acceptance but before saving its result.
    with patch("mailing.campaign_test_tasks.send_campaign_test_message", side_effect=KeyboardInterrupt), \
         pytest.raises(KeyboardInterrupt):
        send_campaign_test.call(*task.args_kwargs["args"])
    with patch("mailing.campaign_test_tasks.send_campaign_test_message") as send:
        repeat = send_campaign_test.call(*task.args_kwargs["args"])
    send.assert_not_called()
    assert repeat["status"] == "already_attempted"
    assert TaskRun.objects.get().progress_current == 0
