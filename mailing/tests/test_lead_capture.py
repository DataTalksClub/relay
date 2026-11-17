import io
import re

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from mailing.lead_capture_models import LeadEvent, LeadMaterial
from mailing.models import (
    Campaign,
    Client,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
)
from mailing.services import lead_capture
from mailing.services.campaigns import _candidate_contacts

pytestmark = pytest.mark.django_db(transaction=True)

PDF = b"%PDF-1.4 test kit"


@pytest.fixture
def enqueued(monkeypatch):
    sent = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", sent.append)
    return sent


@pytest.fixture
def event():
    Organization.objects.create(name="DataTalks.Club", slug="datatalksclub")
    call_command("provision_lead_capture", stdout=io.StringIO())
    material = LeadMaterial.objects.create(slug="kit", title="The 28 prompts kit", filename="kit.pdf", data=PDF)
    return LeadEvent.objects.create(
        slug="test-talk", title="Test talk", offer="Slides and the kit", material=material,
        thank_you_template_key="lead-thank-you",
    )


def submit(client, slug="test-talk", **data):
    payload = {"email": "dev+lead@dtcdev.click", "name": "Ada", "consent": "yes"} | data
    return client.post(f"/m/{slug}", data=payload, HTTP_X_FORWARDED_FOR="203.0.113.7, 198.51.100.1")


def confirm_url(message):
    url = message.context["confirm_url"]
    assert url.startswith("https://materials.dtcdev.click/test-talk/confirm?t=")
    return url.split("?t=", 1)[1]


def test_provision_creates_scope_and_keeps_edited_template():
    Organization.objects.create(name="DataTalks.Club", slug="datatalksclub")
    call_command("provision_lead_capture", stdout=io.StringIO())
    client = Client.objects.get(slug="talk-leads")
    assert client.sender_emails == [{"id": "alexey", "email": "Alexey Grigorev <alexey@datatalks.club>"}]
    template = EmailTemplate.objects.get(client=client, key="lead-confirm")
    template.subject = "Edited in Relay"
    template.save()

    call_command("provision_lead_capture", stdout=io.StringIO())

    assert EmailTemplate.objects.get(pk=template.pk).subject == "Edited in Relay"


def test_page_renders_form_with_required_consent(client, event):
    response = client.get("/m/test-talk")

    assert response.status_code == 200
    body = response.content.decode()
    assert "Test talk" in body and "Slides and the kit" in body
    assert 'name="consent"' in body and "required" in body
    assert "I agree that Alexey Grigorev may contact me and send me materials." in body
    assert response["X-Robots-Tag"] == "noindex"


def test_unknown_event_is_404(client, event):
    assert client.get("/m/nope").status_code == 404


def test_signup_without_consent_is_rejected(client, event, enqueued):
    response = submit(client, consent="")

    assert response.status_code == 400
    assert "tick the box" in response.content.decode()
    assert not Contact.objects.exists()
    assert enqueued == []


def test_invalid_email_is_rejected(client, event, enqueued):
    response = submit(client, email="not-an-email")

    assert response.status_code == 400
    assert not Contact.objects.exists()


def test_honeypot_looks_successful_and_does_nothing(client, event, enqueued):
    response = submit(client, company="Acme Bots")

    assert response.status_code == 200
    assert "Check your inbox" in response.content.decode()
    assert not Contact.objects.exists()
    assert enqueued == []


def test_signup_sends_one_confirmation_and_stores_pending_lead_with_consent(client, event, enqueued):
    response = submit(client)

    assert response.status_code == 200
    assert "Check your inbox" in response.content.decode()
    message = TransactionalMessage.objects.get()
    assert message.email == "dev+lead@dtcdev.click"
    assert message.template_key == "lead-confirm"
    assert message.from_email == "Alexey Grigorev <alexey@datatalks.club>"
    assert "Test talk" in message.subject
    assert "Hi Ada" in message.text_body and "Slides and the kit" in message.text_body
    assert message.context["confirm_url"] in message.html_body
    assert len(enqueued) == 1
    subscription = Subscription.objects.get()
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.audience.slug == "talk-leads"
    record = ContactSourceMetadata.objects.get(source="lead:test-talk")
    assert record.metadata["name"] == "Ada"
    assert record.metadata["consent_text"] == event.consent_text
    assert record.metadata["consent_at"]
    assert record.metadata["last_submit_ip"] == "203.0.113.7"
    assert "confirmed_at" not in record.metadata
    assert not ContactTag.objects.exists()


def test_viewer_ip_header_from_the_edge_wins(client, event, enqueued):
    client.post(
        "/m/test-talk",
        data={"email": "dev+ip@dtcdev.click", "consent": "yes"},
        HTTP_X_FORWARDED_FOR="3.172.103.105, 198.51.100.1",
        HTTP_X_MATERIALS_CLIENT_IP="192.0.2.44",
    )

    assert ContactSourceMetadata.objects.get().metadata["last_submit_ip"] == "192.0.2.44"


def test_double_submit_is_idempotent(client, event, enqueued):
    submit(client)
    again = submit(client, email="DEV+lead@dtcdev.click")

    assert again.status_code == 200
    assert TransactionalMessage.objects.count() == 1
    assert Contact.objects.count() == 1


def test_resubmit_after_window_sends_a_distinct_email(client, event, enqueued):
    with override_settings(RELAY_LEADS_RESEND_SECONDS=0):
        submit(client)
        submit(client)

    first, second = TransactionalMessage.objects.order_by("id")
    assert first.context["confirm_url"] != second.context["confirm_url"]
    assert first.html_body != second.html_body


def test_email_rate_limit(client, event, enqueued):
    with override_settings(RELAY_LEADS_RESEND_SECONDS=0, RELAY_LEADS_EMAIL_LIMIT=2):
        assert submit(client).status_code == 200
        assert submit(client).status_code == 200
        assert submit(client).status_code == 429
    assert TransactionalMessage.objects.count() == 2


def test_ip_rate_limit(client, event, enqueued):
    with override_settings(RELAY_LEADS_IP_LIMIT=1):
        assert submit(client, email="dev+a@dtcdev.click").status_code == 200
        assert submit(client, email="dev+b@dtcdev.click").status_code == 429


def test_inactive_event_accepts_no_signups(client, event, enqueued):
    event.is_active = False
    event.save()

    assert "closed" in client.get("/m/test-talk").content.decode()
    assert submit(client).status_code == 410
    assert enqueued == []


def test_confirm_get_does_not_confirm_post_confirms_tags_and_unlocks_download(client, event, enqueued):
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())

    shown = client.get("/m/test-talk/confirm", {"t": token})
    assert shown.status_code == 200
    assert Subscription.objects.get().status == SubscriptionStatus.PENDING
    blocked = client.get("/m/test-talk/download", {"t": token})
    assert blocked.status_code == 403

    confirmed = client.post("/m/test-talk/confirm", {"t": token})
    assert confirmed.status_code == 200
    assert "/test-talk/download?t=" in confirmed.content.decode()
    subscription = Subscription.objects.get()
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.verified_at is not None
    assert Contact.objects.get().verified_at is not None
    tag = ContactTag.objects.select_related("tag").get().tag
    assert (tag.slug, tag.audience.slug) == ("test-talk", "talk-leads")

    download = client.get("/m/test-talk/download", {"t": token})
    assert download.status_code == 200
    assert download.content == PDF
    assert download["Content-Type"] == "application/pdf"
    assert 'filename="kit.pdf"' in download["Content-Disposition"]
    assert ContactSourceMetadata.objects.get().metadata["download_count"] == 1

    # Confirming twice is harmless.
    assert client.post("/m/test-talk/confirm", {"t": token}).status_code == 200
    assert ContactTag.objects.count() == 1


def test_confirmed_leads_are_campaign_recipients_by_event_tag(client, event, enqueued):
    submit(client, email="dev+one@dtcdev.click")
    submit(client, email="dev+two@dtcdev.click")
    token = confirm_url(TransactionalMessage.objects.get(email="dev+one@dtcdev.click"))
    client.post("/m/test-talk/confirm", {"t": token})
    scope = lead_capture.leads_scope()
    campaign = Campaign(client=scope.client, audience=scope.audience, subject="Slides")

    recipients = list(_candidate_contacts(campaign, ["test-talk"], []))

    assert [c.normalized_email for c in recipients] == ["dev+one@dtcdev.click"]


def test_token_for_another_event_is_rejected(client, event, enqueued):
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    LeadEvent.objects.create(slug="other", title="Other")

    assert client.post("/m/other/confirm", {"t": token}).status_code == 400
    assert client.post("/m/test-talk/confirm", {"t": token + "x"}).status_code == 400
    assert Subscription.objects.get().status == SubscriptionStatus.PENDING


def test_material_url_redirects_after_confirmation(client, event, enqueued):
    event.material = None
    event.material_url = "https://example.com/slides.pdf"
    event.save()
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    client.post("/m/test-talk/confirm", {"t": token})

    response = client.get("/m/test-talk/download", {"t": token})

    assert response.status_code == 302
    assert response["Location"] == "https://example.com/slides.pdf"


def test_suppressed_contact_gets_generic_page_and_no_send(client, event, enqueued):
    Contact.objects.create(email="dev+bounced@dtcdev.click", hard_bounced_at=timezone.now())

    response = submit(client, email="dev+bounced@dtcdev.click")

    assert response.status_code == 200
    assert "Check your inbox" in response.content.decode()
    assert enqueued == []


def test_qr_codes(client, event):
    svg = client.get("/m/test-talk/qr.svg")
    png = client.get("/m/test-talk/qr.png", {"download": "1"})

    assert svg.status_code == 200 and svg["Content-Type"] == "image/svg+xml"
    assert b"<svg" in svg.content
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    assert 'filename="test-talk-qr.png"' in png["Content-Disposition"]
    assert client.get("/m/test-talk/qr.gif").status_code == 404


def test_lead_event_command_creates_material_event_and_qr(tmp_path, event):
    pdf = tmp_path / "kit2.pdf"
    pdf.write_bytes(PDF + b"2")
    out = io.StringIO()
    call_command("lead_event", "material", "kit2", "--file", str(pdf), "--title", "Kit 2", stdout=out)
    call_command("lead_event", "set", "talk-two", "--title", "Talk two", "--material", "kit2", stdout=out)
    call_command("lead_event", "qr", "talk-two", "--out", str(tmp_path), stdout=out)
    call_command("lead_event", "list", stdout=out)

    created = LeadEvent.objects.get(slug="talk-two")
    assert created.material.filename == "kit2.pdf"
    assert (tmp_path / "talk-two-qr.svg").exists() and (tmp_path / "talk-two-qr.png").exists()
    assert "https://materials.dtcdev.click/talk-two" in out.getvalue()


def test_thank_you_after_confirm_once_with_pdf_and_event_copy(client, event, enqueued):
    event.thank_you_template_key = "lead-thank-you"
    event.consent_text = "I agree to be contacted by Alexey Grigorev"
    event.save()
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    assert client.get("/m/test-talk/confirm", {"t": token}).status_code == 200
    assert TransactionalMessage.objects.count() == 1

    assert client.post("/m/test-talk/confirm", {"t": token}).status_code == 200
    message = TransactionalMessage.objects.get(template_key="lead-thank-you")
    assert message.from_email == "Alexey Grigorev <alexey@datatalks.club>"
    assert "Thank you for attending Test talk" in message.text_body
    assert "Within a few days I'll send you all the materials I used today." in message.text_body
    assert "https://www.linkedin.com/in/agrigorev/" in message.text_body
    assert "https://aishippingblog.com/" in message.text_body
    assert "where we met" in message.text_body
    assert "agentic engineering" in message.text_body
    assert message.context["download_url"].endswith("/test-talk/download?t=" + token)
    assert client.get("/m/test-talk/download", {"t": token}).content == PDF
    assert ContactSourceMetadata.objects.get().metadata["consent_text"] == event.consent_text
    assert ContactSourceMetadata.objects.get().metadata["thank_you_message_id"] == message.id

    # Both the original token and a freshly issued one share the durable receipt.
    assert client.post("/m/test-talk/confirm", {"t": token}).status_code == 200
    another_token = lead_capture.issue_token(Contact.objects.get().id, event.id)
    assert client.post("/m/test-talk/confirm", {"t": another_token}).status_code == 200
    assert TransactionalMessage.objects.filter(template_key="lead-thank-you").count() == 1


def test_event_uses_editable_thank_you_template(client, event, enqueued):
    template = EmailTemplate.objects.get(key="lead-thank-you")
    template.subject = "Workshop follow-up: {{ event_title }}"
    template.markdown_body = "Custom event materials: {{ download_url }}"
    template.save()
    event.thank_you_template_key = template.key
    event.save()
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())

    assert client.post("/m/test-talk/confirm", {"t": token}).status_code == 200
    message = TransactionalMessage.objects.get(template_key=template.key)
    assert message.subject == "Workshop follow-up: Test talk"
    assert "Custom event materials:" in message.text_body


def test_confirmation_email_only_asks_to_confirm(client, event, enqueued):
    submit(client)
    body = TransactionalMessage.objects.get().text_body
    assert "Confirm and get the materials" in body
    assert "stays valid" not in body


def test_confirmed_page_says_the_guide_was_emailed(client, event, enqueued):
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    page = client.post("/m/test-talk/confirm", {"t": token}).content.decode()
    assert "I also sent the download link to your email" in page
    assert "keeps working" not in page


def test_verified_contact_skips_confirmation_for_another_event(client, event, enqueued):
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    client.post("/m/test-talk/confirm", {"t": token})
    second = LeadEvent.objects.create(
        slug="second-talk", title="Second talk", material=event.material, thank_you_template_key="lead-thank-you",
        consent_text="I agree to be contacted by Alexey Grigorev",
    )

    response = submit(client, slug="second-talk", name="")
    assert response.status_code == 200
    page = response.content.decode()
    assert "You're confirmed" in page
    assert "/second-talk/download?t=" in page
    assert "Check your inbox" not in page
    # No confirmation email for the second event; one thank-you for it.
    assert not TransactionalMessage.objects.filter(
        template_key="lead-confirm", context__event_title="Second talk"
    ).exists()
    thank_you = TransactionalMessage.objects.get(template_key="lead-thank-you", context__event_title="Second talk")
    page_token = re.search(r"/second-talk/download\?t=([^\"&<\s]+)", page).group(1)
    assert thank_you.context["download_url"].endswith("/second-talk/download?t=" + page_token)
    contact = Contact.objects.get()
    assert set(ContactTag.objects.filter(contact=contact).values_list("tag__slug", flat=True)) == {
        "test-talk", "second-talk",
    }
    record = ContactSourceMetadata.objects.get(contact=contact, source=second.source)
    assert record.metadata["consent_text"] == "I agree to be contacted by Alexey Grigorev"
    assert record.metadata["skipped_confirmation"] is True

    # Submitting again sends nothing more and still shows the materials.
    count = TransactionalMessage.objects.count()
    assert "You're confirmed" in submit(client, slug="second-talk").content.decode()
    assert TransactionalMessage.objects.count() == count


def test_unsubscribed_contact_still_needs_confirmation(client, event, enqueued):
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    client.post("/m/test-talk/confirm", {"t": token})
    Subscription.objects.update(status=SubscriptionStatus.UNSUBSCRIBED)
    LeadEvent.objects.create(slug="second-talk", title="Second talk", material=event.material)
    assert "Check your inbox" in submit(client, slug="second-talk").content.decode()


def test_confirmation_failure_is_visible_and_retryable(client, event, monkeypatch):
    token = lead_capture.issue_token(123, event.id)
    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(lead_capture, "confirm_lead", fail)
    response = client.post("/m/test-talk/confirm", {"t": token})
    assert response.status_code == 503
    assert "Please try confirming again" in response.content.decode()
    assert "Confirm and get the materials" in response.content.decode()


def test_other_events_remain_download_only_by_default(client, event, enqueued):
    event.thank_you_template_key = ""
    event.save()
    submit(client)
    token = confirm_url(TransactionalMessage.objects.get())
    response = client.post("/m/test-talk/confirm", {"t": token})
    assert TransactionalMessage.objects.count() == 1
    assert "sent the download link" not in response.content.decode()
