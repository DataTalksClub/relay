import pytest
from django.contrib import admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Audience,
    Client,
    Contact,
    EmailEvent,
    EmailTemplate,
    Organization,
    RecipientList,
    RecipientListMember,
    TemplateContentFormat,
    TemplateVersion,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.api_docs import API_DOC_PATHS, build_openapi_spec
from mailing.services.auth import create_client_api_key
from mailing.services.transactional_markdown import TemplateMarkdownError, parse_frontmatter
from mailing.services.transactional_rendering import render_template_content
from mailing.services.transactional_versions import (
    latest_published_version_number,
    publish_template_draft,
    resolve_template_version,
)

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"

REGISTRATION_MARKDOWN = """Hi {{ user_name }},

You're registered for **{{ event_title }}**.

Join link: {{ join_url }}

{% if cancel_url %}- Need to cancel? [Cancel my registration]({{ cancel_url }})
{% else %}- This registration cannot be cancelled.
{% endif %}

<script>alert('nope')</script>
"""


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Transactional test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def other_client(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Newsletter",
        slug="dtc-newsletter",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Other test", raw_api_key="other-key")
    return client


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def create_markdown_template(api_client_record, *, key="event-registration", category="events", subject=None):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key=key,
        name=key,
        subject=subject or "You're registered: {{ event_title }}",
        content_format=TemplateContentFormat.MARKDOWN,
        markdown_body=REGISTRATION_MARKDOWN,
        category=category,
        required_context=[{"name": "event_title", "description": "Event title."}],
    )


def put_template(django_client, template_key, payload, raw_key=API_KEY):
    return django_client.put(
        reverse("mailing:api_transactional_template", args=[template_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_template_action(django_client, action, template_key, payload=None, raw_key=API_KEY):
    return django_client.post(
        reverse(f"mailing:api_transactional_template_{action}", args=[template_key]),
        data=payload or {},
        content_type="application/json",
        **auth_headers(raw_key),
    )


def get_template_versions(django_client, template_key, raw_key=API_KEY):
    return django_client.get(
        reverse("mailing:api_transactional_template_versions", args=[template_key]),
        **auth_headers(raw_key),
    )


def post_send(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


SEND_CONTEXT = {
    "user_name": "Ada",
    "event_title": "AI Shipping Workshop",
    "join_url": "https://example.com/join",
    "cancel_url": "https://example.com/cancel",
}


# --- Publish: snapshot, dedupe, immutability -------------------------------


def test_publish_snapshots_draft_as_version_one(api_client_record):
    template = create_markdown_template(api_client_record)

    version, created = publish_template_draft(template)

    assert created is True
    assert version.version == 1
    assert version.template == template
    assert version.subject == "You're registered: {{ event_title }}"
    assert version.content_format == TemplateContentFormat.MARKDOWN
    assert version.markdown_body == REGISTRATION_MARKDOWN
    assert version.html_body == ""
    assert version.category == "events"
    assert version.required_context == [{"name": "event_title", "description": "Event title."}]
    assert version.published_at is not None
    assert latest_published_version_number(template) == 1


def test_republish_identical_content_returns_existing_version(api_client_record):
    template = create_markdown_template(api_client_record)
    first, _ = publish_template_draft(template)

    second, created = publish_template_draft(template)

    assert created is False
    assert second.pk == first.pk
    assert TemplateVersion.objects.count() == 1


def test_republish_after_draft_edit_creates_next_version_and_keeps_old_one(api_client_record):
    template = create_markdown_template(api_client_record)
    first, _ = publish_template_draft(template)

    template.subject = "Renamed: {{ event_title }}"
    template.category = "workshops"
    template.save(update_fields=["subject", "category", "updated_at"])
    second, created = publish_template_draft(template)

    assert created is True
    assert second.version == 2
    assert second.subject == "Renamed: {{ event_title }}"
    assert second.category == "workshops"
    first.refresh_from_db()
    assert first.version == 1
    assert first.subject == "You're registered: {{ event_title }}"
    assert first.category == "events"
    assert TemplateVersion.objects.count() == 2


def test_html_draft_publishes_html_and_text_source(api_client_record):
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="verify",
        name="Verify",
        subject="Verify {{ product }}",
        html_body="<p>Verify at {{ verification_url }}</p>",
        text_body="Verify at {{ verification_url }}",
        required_context=["verification_url"],
    )

    version, _ = publish_template_draft(template)

    assert version.content_format == TemplateContentFormat.HTML
    assert version.html_body == "<p>Verify at {{ verification_url }}</p>"
    assert version.text_body == "Verify at {{ verification_url }}"
    assert version.markdown_body == ""
    assert version.required_context == [{"name": "verification_url", "description": ""}]


def test_published_versions_are_immutable_and_absent_from_admin(api_client_record):
    template = create_markdown_template(api_client_record)
    version, _ = publish_template_draft(template)

    version.subject = "tampered"
    with pytest.raises(ValueError):
        version.save()

    assert TemplateVersion not in admin.site._registry
    assert TemplateVersion.objects.get(pk=version.pk).subject == "You're registered: {{ event_title }}"


# --- Version resolution ----------------------------------------------------


def test_resolve_template_version_defaults_to_latest_and_rejects_unknown_or_missing(api_client_record):
    template = create_markdown_template(api_client_record)

    with pytest.raises(Exception) as none_published:
        resolve_template_version(template)
    assert none_published.value.errors == {"template_key": "no_published_version"}
    assert none_published.value.status_code == 409

    first, _ = publish_template_draft(template)
    template.subject = "Second"
    template.save(update_fields=["subject", "updated_at"])
    second, _ = publish_template_draft(template)

    assert resolve_template_version(template).pk == second.pk
    assert resolve_template_version(template, 1).pk == first.pk

    with pytest.raises(Exception) as unknown:
        resolve_template_version(template, 99)
    assert unknown.value.errors == {"template_version": "not_found"}
    assert unknown.value.status_code == 404


# --- Markdown format: frontmatter parsing and rendering --------------------


def test_parse_frontmatter_reads_required_and_optional_keys():
    metadata, body = parse_frontmatter(
        '---\nsubject: "Hello {{ name }}"\ncategory: events\nrequired_context:\n  - name: event_title\n'
        "    description: Title.\n---\n\nBody line one.\n\nBody line two.\n"
    )

    assert metadata == {
        "subject": "Hello {{ name }}",
        "category": "events",
        "required_context": [{"name": "event_title", "description": "Title."}],
    }
    assert body == "Body line one.\n\nBody line two."


def test_parse_frontmatter_supports_subject_only_and_rejects_malformed_sources():
    metadata, body = parse_frontmatter("---\nsubject: Reset\n---\n\nBody")
    assert metadata == {"subject": "Reset"}
    assert body == "Body"

    for source in [
        "No frontmatter at all",
        "---\nsubject: unbalanced\n\nBody without closing delimiter",
        "---\nsubject: [unclosed\n---\n\nBody",
        "---\n- a\n- b\n---\n\nBody",
    ]:
        with pytest.raises(TemplateMarkdownError):
            parse_frontmatter(source)


def test_markdown_content_renders_context_first_then_sanitized_wrapped_html():
    rendered = render_template_content(
        subject="You're registered: {{ event_title }}",
        context=SEND_CONTEXT,
        content_format=TemplateContentFormat.MARKDOWN,
        markdown_body=REGISTRATION_MARKDOWN,
        client_name="DTC Courses",
    )

    # Context substitution runs first, so Django {% if %} blocks and markdown
    # emphasis see real values instead of raw tags.
    assert "AI Shipping Workshop" in rendered["text"]
    assert "Need to cancel?" in rendered["text"]
    assert "Cancel my registration (https://example.com/cancel)" in rendered["text"]
    assert "{%" not in rendered["html"]

    # Markdown converts and the shared email chrome wraps it with the client
    # header and footer.
    assert "<!DOCTYPE html>" in rendered["html"]
    assert "<h1>DTC Courses</h1>" in rendered["html"]
    assert "<strong>AI Shipping Workshop</strong>" in rendered["html"]
    assert "https://example.com/join" in rendered["html"]
    assert "&copy; DTC Courses" in rendered["html"]

    # The sanitizer strips raw HTML injections from the template source.
    assert "<script>" not in rendered["html"]
    assert "alert('nope')" not in rendered["html"]

    assert rendered["subject"] == "You're registered: AI Shipping Workshop"


# --- Template draft API: markdown form and GET shape -----------------------


def test_put_and_get_template_support_markdown_draft_and_latest_published_version(client, api_client_record):
    put_response = put_template(
        client,
        "event-registration",
        {
            "name": "Event registration",
            "subject": "You're registered: {{ event_title }}",
            "content_format": "markdown",
            "markdown_body": "Hi {{ user_name }}, see you at {{ event_title }}.",
            "category": "events",
            "required_context": [{"name": "event_title"}],
        },
    )

    assert put_response.status_code == 200
    body = put_response.json()
    assert body["template"]["content_format"] == "markdown"
    assert body["template"]["markdown_body"] == "Hi {{ user_name }}, see you at {{ event_title }}."
    assert body["template"]["category"] == "events"
    assert body["template"]["latest_published_version"] is None

    get_response = client.get(
        reverse("mailing:api_transactional_template", args=["event-registration"]),
        **auth_headers(),
    )
    assert get_response.status_code == 200
    assert get_response.json()["latest_published_version"] is None

    publish_response = post_template_action(client, "publish", "event-registration")
    assert publish_response.status_code == 200

    get_response = client.get(
        reverse("mailing:api_transactional_template", args=["event-registration"]),
        **auth_headers(),
    )
    assert get_response.json()["latest_published_version"] == 1


def test_put_template_validates_markdown_form_and_content_format(client, api_client_record):
    response = put_template(
        client,
        "broken-markdown",
        {
            "name": "Broken",
            "subject": "Broken",
            "content_format": "markdown",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"markdown_body": "required"}

    response = put_template(
        client,
        "broken-format",
        {
            "name": "Broken",
            "subject": "Broken",
            "content_format": "rich-text",
            "markdown_body": "Body",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"content_format": "invalid"}


# --- Publish / versions endpoints ------------------------------------------


def test_publish_endpoint_creates_then_dedupes_and_is_client_scoped(client, api_client_record, other_client):
    create_markdown_template(api_client_record)

    first = post_template_action(client, "publish", "event-registration")
    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["latest_published_version"] == 1
    assert first.json()["version"]["is_latest"] is True
    assert first.json()["version"]["category"] == "events"

    second = post_template_action(client, "publish", "event-registration")
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["version"]["version"] == 1
    assert TemplateVersion.objects.count() == 1

    missing = post_template_action(client, "publish", "event-registration", raw_key="other-key")
    assert missing.status_code == 404
    assert missing.json()["error"]["fields"] == {"template_key": "not_found"}


def test_versions_endpoint_lists_versions_with_latest_flag(client, api_client_record):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)
    template.subject = "Second subject"
    template.save(update_fields=["subject", "updated_at"])
    publish_template_draft(template)

    response = get_template_versions(client, "event-registration")

    assert response.status_code == 200
    body = response.json()
    assert body["template_key"] == "event-registration"
    assert body["latest_published_version"] == 2
    assert [version["version"] for version in body["versions"]] == [2, 1]
    assert body["versions"][0]["subject"] == "Second subject"
    assert body["versions"][0]["is_latest"] is True
    assert body["versions"][1]["subject"] == "You're registered: {{ event_title }}"
    assert body["versions"][1]["is_latest"] is False
    assert all(version["published_at"] for version in body["versions"])


# --- Preview endpoint and preview/send render parity ------------------------


def test_preview_renders_draft_or_version_and_lists_missing_context(client, api_client_record):
    template = create_markdown_template(api_client_record)
    template.required_context = [
        {"name": "event_title"},
        {"name": "join_url"},
    ]
    template.save(update_fields=["required_context", "updated_at"])
    publish_template_draft(template)

    missing = post_template_action(
        client,
        "preview",
        "event-registration",
        {"context": {"user_name": "Ada"}},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["fields"] == {
        "context.event_title": "required",
        "context.join_url": "required",
    }

    draft_preview = post_template_action(
        client,
        "preview",
        "event-registration",
        {"context": SEND_CONTEXT},
    )
    assert draft_preview.status_code == 200
    assert draft_preview.json()["template_version"] is None
    assert draft_preview.json()["preview"]["subject"] == "You're registered: AI Shipping Workshop"
    assert "<h1>DTC Courses</h1>" in draft_preview.json()["preview"]["html"]
    assert "AI Shipping Workshop" in draft_preview.json()["preview"]["text"]

    version_preview = post_template_action(
        client,
        "preview",
        "event-registration",
        {"context": SEND_CONTEXT, "template_version": 1},
    )
    assert version_preview.status_code == 200
    assert version_preview.json()["template_version"] == 1

    unknown = post_template_action(
        client,
        "preview",
        "event-registration",
        {"context": SEND_CONTEXT, "template_version": 42},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["fields"] == {"template_version": "not_found"}


def test_preview_and_dry_run_send_share_one_render_path(client, api_client_record, monkeypatch):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)

    preview_response = post_template_action(
        client,
        "preview",
        "event-registration",
        {"context": SEND_CONTEXT, "template_version": 1},
    )

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "template_version": 1,
            "context": SEND_CONTEXT,
            "dry_run": True,
        },
    )

    assert preview_response.status_code == 200
    assert response.status_code == 202
    preview = preview_response.json()["preview"]
    rendered = response.json()["rendered"]
    assert rendered["subject"] == preview["subject"]
    assert rendered["html_body"] == preview["html"]
    assert rendered["text_body"] == preview["text"]
    assert response.json()["message"]["template_version"] == 1


# --- Send resolution: template_version on every send path -------------------


def test_send_without_template_version_uses_latest_published_version(client, api_client_record, monkeypatch):
    template = create_markdown_template(api_client_record)
    first, _ = publish_template_draft(template)
    template.subject = "Second: {{ event_title }}"
    template.save(update_fields=["subject", "updated_at"])
    second, _ = publish_template_draft(template)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "idempotency_key": "reg-1",
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template_version == second.version
    assert message.subject == "Second: AI Shipping Workshop"
    assert response.json()["message"]["template_version"] == second.version
    assert enqueued[0]["template_key"] == "event-registration"


def test_send_with_explicit_template_version_renders_that_version(client, api_client_record, monkeypatch):
    template = create_markdown_template(api_client_record)
    first, _ = publish_template_draft(template)
    template.subject = "Second: {{ event_title }}"
    template.save(update_fields=["subject", "updated_at"])
    publish_template_draft(template)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "template_version": 1,
            "idempotency_key": "reg-2",
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template_version == first.version
    assert message.subject == "You're registered: AI Shipping Workshop"


def test_send_with_unknown_version_returns_404_without_side_effects(client, api_client_record, monkeypatch):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "template_version": 99,
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_version": "not_found"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_send_without_published_version_returns_409_without_side_effects(client, api_client_record, monkeypatch):
    create_markdown_template(api_client_record)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"template_key": "no_published_version"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_send_rejects_non_integer_template_version(client, api_client_record):
    create_markdown_template(api_client_record)
    publish_template_draft(api_client_record.email_templates.get())

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "template_version": "one",
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"template_version": "must_be_integer"}


def test_recipient_list_send_accepts_template_version_and_validates_required_context(
    client,
    api_client_record,
    monkeypatch,
):
    audience = Audience.objects.create(organization=api_client_record.organization, name="Courses", slug="dtc-courses")
    recipient_list = RecipientList.objects.create(
        client=api_client_record,
        audience=audience,
        key="ml-zoomcamp-2026",
        name="ML Zoomcamp 2026",
    )
    contact = Contact.objects.create(email="learner@example.com")
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=contact,
        email=contact.normalized_email,
        source_object_key="registration:1",
    )
    template = create_markdown_template(api_client_record)
    first, _ = publish_template_draft(template)
    template.subject = "Second: {{ event_title }}"
    template.save(update_fields=["subject", "updated_at"])
    publish_template_draft(template)
    batches = []
    monkeypatch.setattr(
        "mailing.services.transactional.enqueue_transactional_email_batch",
        lambda message_ids, **kwargs: batches.append((list(message_ids), kwargs)),
    )

    send_response = client.post(
        reverse("mailing:api_recipient_list_transactional_send", args=[recipient_list.key]),
        data={
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": "event-registration",
            "template_version": 1,
            "idempotency_key": "list-send-1",
            "context": SEND_CONTEXT,
        },
        content_type="application/json",
        **auth_headers(),
    )

    assert send_response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template_version == first.version
    assert message.subject == "You're registered: AI Shipping Workshop"
    assert len(batches) == 1

    unknown = client.post(
        reverse("mailing:api_recipient_list_transactional_send", args=[recipient_list.key]),
        data={
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": "event-registration",
            "template_version": 99,
            "idempotency_key": "list-send-2",
            "context": SEND_CONTEXT,
        },
        content_type="application/json",
        **auth_headers(),
    )

    assert unknown.status_code == 404
    assert unknown.json()["error"]["fields"] == {"template_version": "not_found"}
    assert TransactionalMessage.objects.count() == 1


def test_required_context_validates_against_resolved_version_not_draft(client, api_client_record, monkeypatch):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)
    # The draft tightens its context contract after publishing; the published
    # version still renders with the old contract.
    template.required_context = [{"name": "event_title"}, {"name": "seat_number"}]
    template.save(update_fields=["required_context", "updated_at"])
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": "event-registration",
            "idempotency_key": "old-contract",
            "context": SEND_CONTEXT,
        },
    )

    assert response.status_code == 202
    assert len(enqueued) == 1


# --- Test send --------------------------------------------------------------


def test_test_send_denies_non_allowlisted_address_by_default_without_side_effects(
    client,
    api_client_record,
    monkeypatch,
):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_template_action(
        client,
        "test_send",
        "event-registration",
        {"email": "staff@example.com", "context": SEND_CONTEXT},
    )

    assert response.status_code == 403
    assert response.json()["error"]["fields"] == {"email": "not_allowlisted"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


@override_settings(TRANSACTIONAL_TEST_SEND_ADDRESSES=["Staff@Example.com", "qa@example.com"])
def test_test_send_allows_allowlisted_address_through_normal_enqueue_path(
    client,
    api_client_record,
    monkeypatch,
):
    template = create_markdown_template(api_client_record)
    publish_template_draft(template)
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_template_action(
        client,
        "test_send",
        "event-registration",
        {"email": "staff@example.com", "context": SEND_CONTEXT},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["test_send"] is True
    message = TransactionalMessage.objects.get()
    assert message.email == "staff@example.com"
    assert message.metadata == {"test_send": True}
    assert message.template_version == 1
    assert message.status == TransactionalMessageStatus.QUEUED
    assert EmailEvent.objects.get().event_type == "queued"
    assert len(enqueued) == 1
    assert enqueued[0]["transactional_message_id"] == message.id


@override_settings(TRANSACTIONAL_TEST_SEND_ADDRESSES=["staff@example.com"])
def test_test_send_is_scoped_to_authenticated_client_templates(client, api_client_record, other_client):
    create_markdown_template(other_client, key="other-registration")

    response = post_template_action(
        client,
        "test_send",
        "other-registration",
        {"email": "staff@example.com", "context": SEND_CONTEXT},
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    assert TransactionalMessage.objects.count() == 0


@override_settings(TRANSACTIONAL_TEST_SEND_ADDRESSES=["staff@example.com"])
def test_test_send_validates_email_and_requires_published_version(client, api_client_record):
    create_markdown_template(api_client_record)

    invalid = post_template_action(
        client,
        "test_send",
        "event-registration",
        {"email": "not-an-email", "context": SEND_CONTEXT},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["fields"] == {"email": "invalid"}

    missing = post_template_action(
        client,
        "test_send",
        "event-registration",
        {"email": {}, "context": SEND_CONTEXT},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["fields"] == {"email": "required"}

    no_version = post_template_action(
        client,
        "test_send",
        "event-registration",
        {"email": "staff@example.com", "context": SEND_CONTEXT},
    )
    assert no_version.status_code == 409
    assert no_version.json()["error"]["fields"] == {"template_key": "no_published_version"}


# --- Import command ----------------------------------------------------------


def write_template_file(directory, name, source):
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def test_import_templates_creates_idempotent_markdown_drafts(api_client_record, tmp_path):
    write_template_file(
        tmp_path,
        "event_registration.md",
        "---\nsubject: \"You're registered: {{ event_title }}\"\n"
        "category: events\n"
        "required_context:\n  - name: event_title\n    description: Event title.\n---\n\nHi {{ user_name }}!\n",
    )
    write_template_file(
        tmp_path,
        "password_reset.md",
        "---\nsubject: Reset your password\n---\n\nUse {{ reset_url }} to reset.\n",
    )

    call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    templates = {template.key: template for template in EmailTemplate.objects.all()}
    assert set(templates) == {"event_registration", "password_reset"}
    event_template = templates["event_registration"]
    assert event_template.content_format == TemplateContentFormat.MARKDOWN
    assert event_template.subject == "You're registered: {{ event_title }}"
    assert event_template.category == "events"
    assert event_template.required_context == [{"name": "event_title", "description": "Event title."}]
    assert event_template.markdown_body == "Hi {{ user_name }}!"
    assert event_template.is_transactional is True
    reset_template = templates["password_reset"]
    assert reset_template.subject == "Reset your password"
    assert reset_template.category == ""
    assert reset_template.required_context == []

    call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    assert EmailTemplate.objects.count() == 2
    assert TemplateVersion.objects.count() == 0


def test_import_templates_exits_non_zero_on_malformed_file(api_client_record, tmp_path):
    write_template_file(
        tmp_path,
        "good_template.md",
        "---\nsubject: Good\n---\n\nBody\n",
    )
    write_template_file(
        tmp_path,
        "bad_template.md",
        "---\ncategory: events\n---\n\nNo subject in frontmatter\n",
    )

    with pytest.raises(CommandError) as excinfo:
        call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")

    assert "bad_template.md" in str(excinfo.value)
    assert "created=1" in str(excinfo.value)
    keys = set(EmailTemplate.objects.values_list("key", flat=True))
    assert keys == {"good_template"}


def test_import_templates_rejects_unknown_client_and_empty_directory(api_client_record, tmp_path):
    with pytest.raises(CommandError) as client_error:
        call_command("import_templates", "--dir", str(tmp_path), "--client", "missing-client")
    assert "Client not found" in str(client_error.value)

    with pytest.raises(CommandError) as empty_error:
        call_command("import_templates", "--dir", str(tmp_path), "--client", "dtc-courses")
    assert "No *.md files" in str(empty_error.value)


# --- Docs stay in sync -------------------------------------------------------


def test_template_version_endpoints_are_registered_in_openapi_and_docs_map():
    spec = build_openapi_spec()
    for name in [
        "api_transactional_template_publish",
        "api_transactional_template_versions",
        "api_transactional_template_preview",
        "api_transactional_template_test_send",
    ]:
        path = API_DOC_PATHS[f"mailing:{name}"]
        assert path in spec["paths"], path
        assert path.endswith(("publish", "versions", "preview", "test-send"))
