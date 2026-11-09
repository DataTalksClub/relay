"""Hard-stop: pause / resume / stop for SENDING campaigns + throttle requeue.

Covers the acceptance criteria:
1. zero SES calls after stop/pause is acknowledged (flag set mid-batch),
2. stop latency <= one SES call (only in-flight calls finish),
3. resume sends remaining recipients with no duplicates,
4. halt path uses no row locks (plain UPDATE / plain SELECT proof),
5. API endpoints + UI buttons exist for SENDING, UI shows errors,
6. retryable SES throttling requeues remaining PENDING with backoff.
"""

import inspect

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing import tasks
from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import api as api_services
from mailing.services import campaign_sender as sender
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-hard-stop-key"


@pytest.fixture
def org():
    return Organization.objects.create(name="HardStop", slug="hardstop")


@pytest.fixture
def audience(org):
    return Audience.objects.create(organization=org, name="Members", slug="members")


@pytest.fixture
def api_client_record(org):
    client = Client.objects.create(organization=org, name="Sender", slug="sender")
    create_client_api_key(client=client, name="hard-stop", raw_api_key=API_KEY)
    return client


@pytest.fixture
def campaign_setup(audience, api_client_record):
    campaign = Campaign.objects.create(
        audience=audience,
        client=api_client_record,
        external_key="hard-stop-campaign",
        subject="Stop me",
        status=CampaignStatus.SENDING,
        html_body="<p>Hello</p>",
        text_body="Hello",
    )
    recipients = []
    for index in range(3):
        # Verified + subscribed, so the pre-send suppression recheck lets them through.
        contact = Contact.objects.create(email=f"halt-user-{index}@example.com", verified_at=timezone.now())
        Subscription.objects.create(
            contact=contact,
            audience=audience,
            client=api_client_record,
            status=SubscriptionStatus.SUBSCRIBED,
        )
        recipients.append(CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email))
    return campaign, recipients


def _payload(campaign, recipients):
    return {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id for recipient in recipients],
    }


class _CountingSes:
    """Fake SES that halts the campaign mid-batch, like an operator pause."""

    def __init__(self, campaign, *, halt_after=1, halt_reason="paused"):
        self.campaign = campaign
        self.halt_after = halt_after
        self.halt_reason = halt_reason
        self.calls = []

    def _record(self, params):
        to = params.get("Destinations", [None])[0]
        self.calls.append(to)
        if len(self.calls) == self.halt_after:
            # Plain UPDATE, exactly like pause/stop: no row lock is taken.
            Campaign.objects.filter(pk=self.campaign.pk).update(
                send_halted_at=timezone.now(),
                send_halt_reason=self.halt_reason,
                updated_at=timezone.now(),
            )
        return {"MessageId": f"ses-message-{len(self.calls)}"}

    def send_raw_email(self, **params):
        return self._record(params)

    def send_email(self, **params):
        to = params.get("Destination", {}).get("ToAddresses", [None])[0]
        return self._record({"Destinations": [to], **params})


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_zero_ses_calls_after_pause_acknowledged(campaign_setup):
    campaign, recipients = campaign_setup
    ses = _CountingSes(campaign, halt_after=1)

    result = sender.send_campaign_batch(_payload(campaign, recipients), ses_client=ses)

    # AC1 + AC2: the in-flight call finished, no new call started.
    assert len(ses.calls) == 1
    assert result.sent_count == 1
    statuses = {r.email: CampaignRecipient.objects.get(pk=r.pk).status for r in recipients}
    assert statuses[recipients[0].email] == CampaignRecipientStatus.SENT
    assert statuses[recipients[1].email] == CampaignRecipientStatus.PENDING
    assert statuses[recipients[2].email] == CampaignRecipientStatus.PENDING


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_stop_latency_is_one_in_flight_call(campaign_setup):
    campaign, recipients = campaign_setup
    ses = _CountingSes(campaign, halt_after=1, halt_reason="stopped")

    sender.send_campaign_batch(_payload(campaign, recipients), ses_client=ses)

    assert len(ses.calls) == 1
    campaign.refresh_from_db()
    assert campaign.send_halted_at is not None
    assert campaign.send_halt_reason == "stopped"
    sent = CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SENT).count()
    pending = CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).count()
    assert sent == 1
    assert pending == 2


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_resume_sends_remaining_with_no_duplicates(campaign_setup, monkeypatch):
    campaign, recipients = campaign_setup
    ses = _CountingSes(campaign, halt_after=1)
    sender.send_campaign_batch(_payload(campaign, recipients), ses_client=ses)
    assert len(ses.calls) == 1

    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    result = api_services.resume_campaign_instance(campaign)

    assert result["resumed"] is True
    assert result["recipient_count"] == 2
    assert len(enqueued) >= 1
    requeued_ids = sorted(rid for payload in enqueued for rid in payload["campaign_recipient_ids"])
    remaining_ids = sorted(
        CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).values_list(
            "id", flat=True
        )
    )
    assert requeued_ids == remaining_ids

    campaign.refresh_from_db()
    assert campaign.send_halted_at is None
    second_ses = _CountingSes(campaign, halt_after=10**9)
    sender.send_campaign_batch(_payload(campaign, recipients), ses_client=second_ses)

    all_calls = ses.calls + second_ses.calls
    assert len(all_calls) == 3
    assert sorted(all_calls) == sorted(recipient.email for recipient in recipients)
    assert CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SENT).count() == 3
    assert CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).count() == 0


def test_halt_paths_take_no_row_locks():
    """AC4 code proof: the halt write and read paths never select_for_update."""
    halt_sources = [
        inspect.getsource(api_services.pause_campaign_instance),
        inspect.getsource(api_services.resume_campaign_instance),
        inspect.getsource(api_services.stop_campaign_instance),
        inspect.getsource(sender._campaign_send_halted),
    ]
    for source in halt_sources:
        assert "select_for_update" not in source
        assert "FOR UPDATE" not in source
        assert "FOR SHARE" not in source
    assert ".update(" in inspect.getsource(api_services.pause_campaign_instance)
    assert ".update(" in inspect.getsource(api_services.stop_campaign_instance)


def _api_post(django_client, name, external_key, payload):
    return django_client.post(
        reverse(name, args=[external_key]),
        data=payload,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )


def _scope(audience, api_client_record):
    return {"audience": audience.slug, "client": api_client_record.slug}


def test_api_pause_resume_stop_for_sending_campaign(client, audience, api_client_record):
    scope = _scope(audience, api_client_record)
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="live-campaign",
        subject="Live",
        html_body="<p>Hello</p>",
        status=CampaignStatus.SENDING,
    )

    paused = _api_post(client, "mailing:api_campaign_pause", "live-campaign", scope)
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert paused.json()["campaign"]["send_halted"] is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENDING

    paused_again = _api_post(client, "mailing:api_campaign_pause", "live-campaign", scope)
    assert paused_again.status_code == 200
    assert paused_again.json()["paused"] is False

    contact = Contact.objects.create(email="resume-me@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact, audience=audience, client=api_client_record, status=SubscriptionStatus.SUBSCRIBED,
    )
    CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    resumed = _api_post(client, "mailing:api_campaign_resume", "live-campaign", scope)
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["recipient_count"] == 1

    resume_again = _api_post(client, "mailing:api_campaign_resume", "live-campaign", scope)
    assert resume_again.status_code == 409

    stopped = _api_post(client, "mailing:api_campaign_stop", "live-campaign", scope)
    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.CANCELLED
    assert campaign.send_halt_reason == "stopped"
    # Sent rows are kept; the pending row is left untouched, not skipped.
    assert CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).count() == 1


def test_api_pause_returns_false_for_non_sendable_campaign(client, audience, api_client_record):
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="draft-campaign",
        subject="Draft",
        html_body="<p>Hello</p>",
        status=CampaignStatus.DRAFT,
    )
    response = _api_post(client, "mailing:api_campaign_pause", "draft-campaign", _scope(audience, api_client_record))
    assert response.status_code == 200
    assert response.json()["paused"] is False


@pytest.fixture
def staff_client(client, api_client_record):
    operator = get_user_model().objects.create_user(username="halter", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = api_client_record.pk
    session.save()
    return client


def test_ui_buttons_exist_for_sending_and_failure_is_visible(staff_client, audience, api_client_record):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        subject="UI halt",
        html_body="<p>Hello</p>",
        status=CampaignStatus.SENDING,
    )
    detail = staff_client.get(reverse("mailing:campaign_detail", args=[campaign.pk]))
    assert detail.status_code == 200
    assert b"Pause sending" in detail.content
    assert b"Stop sending" in detail.content

    paused = staff_client.post(
        reverse("mailing:campaign_pause", args=[campaign.pk]),
        {"confirm_pause": "1", "operator_client_id": api_client_record.pk},
        follow=True,
    )
    assert paused.status_code == 200
    assert b"Campaign paused" in paused.content
    assert b"Resume sending" in paused.content

    # Missing confirmation: visible error, no state change is fine.
    bad = staff_client.post(
        reverse("mailing:campaign_resume", args=[campaign.pk]),
        {"operator_client_id": api_client_record.pk},
        follow=True,
    )
    assert bad.status_code == 200
    assert b"Confirm resuming" in bad.content

    resumed = staff_client.post(
        reverse("mailing:campaign_resume", args=[campaign.pk]),
        {"confirm_resume": "1", "operator_client_id": api_client_record.pk},
        follow=True,
    )
    assert b"Campaign resumed" in resumed.content

    stopped = staff_client.post(
        reverse("mailing:campaign_stop", args=[campaign.pk]),
        {"confirm_stop": "1", "operator_client_id": api_client_record.pk},
        follow=True,
    )
    assert b"Campaign stopped" in stopped.content
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.CANCELLED


def test_ui_stop_failure_is_visible_error(staff_client, audience, api_client_record):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        subject="Draft UI",
        html_body="<p>Hello</p>",
        status=CampaignStatus.DRAFT,
    )
    response = staff_client.post(
        reverse("mailing:campaign_stop", args=[campaign.pk]),
        {"confirm_stop": "1", "operator_client_id": api_client_record.pk},
        follow=True,
    )
    assert response.status_code == 200
    assert b"cannot be stopped" in response.content


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_retryable_throttling_requeues_remaining_with_backoff(campaign_setup, settings, monkeypatch):
    settings.RELAY_CAMPAIGN_THROTTLE_MAX_ATTEMPTS = 5
    settings.RELAY_CAMPAIGN_THROTTLE_BASE_SECONDS = 30
    settings.RELAY_CAMPAIGN_THROTTLE_MAX_SECONDS = 900
    campaign, recipients = campaign_setup

    class ThrottlingSes:
        def __init__(self):
            self.calls = []

        def send_raw_email(self, **params):
            self.calls.append(params.get("Destinations", [None])[0])
            if len(self.calls) == 2:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": f"ses-{len(self.calls)}"}

        def send_email(self, **params):
            return self.send_raw_email(**params)

    ses = ThrottlingSes()
    payload = _payload(campaign, recipients)
    monkeypatch.setattr(tasks, "send_campaign_batch", lambda message: sender.send_campaign_batch(message, ses_client=ses))
    outcome = tasks.send_campaign_email_batch.call(payload, attempt=1)

    # The batch did not fail: remaining PENDING requeued with backoff.
    assert outcome["deferred"] is True
    assert outcome["remaining"] == 2
    assert outcome["retry_in_seconds"] == 30
    assert outcome["next_attempt"] == 2
    retry = DBTaskResult.objects.filter(task_path="mailing.tasks.send_campaign_email_batch").get()
    assert retry.run_after > timezone.now()
    assert retry.args_kwargs["kwargs"] == {"attempt": 2}
    retry_ids = retry.args_kwargs["args"][0]["campaign_recipient_ids"]
    pending_ids = list(
        CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING)
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert sorted(retry_ids) == sorted(pending_ids)
    assert CampaignRecipient.objects.get(pk=recipients[0].pk).status == CampaignRecipientStatus.SENT
    assert "Throttling" in CampaignRecipient.objects.get(pk=recipients[1].pk).last_error


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_retryable_throttling_exhaustion_fails_loudly(campaign_setup, settings, monkeypatch):
    settings.RELAY_CAMPAIGN_THROTTLE_MAX_ATTEMPTS = 1
    campaign, recipients = campaign_setup
    monkeypatch.setattr(
        tasks,
        "send_campaign_batch",
        lambda message: (_ for _ in ()).throw(sender.RetryableCampaignSendError("Throttling")),
    )
    with pytest.raises(sender.RetryableCampaignSendError):
        tasks.send_campaign_email_batch.call(_payload(campaign, recipients), attempt=1)
