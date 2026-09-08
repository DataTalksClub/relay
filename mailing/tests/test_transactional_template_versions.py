import pytest
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailTemplate,
    EmailTemplateVersion,
    Organization,
    TransactionalMessage,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import create_client_api_key

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
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Transactional test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def markdown_template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="event_registration",
        name="Event registration",
        subject="You're registered: {{ event_title }}",
        markdown_body=(
            "# Hi {{ user_name }}\n\n"
            "You're registered for **{{ event_title }}**.\n\n"
            "Join link: {{ join_url }}"
        ),
        required_context=[
            {"name": "user_name", "description": "Recipient name."},
            {"name": "join_url", "description": "Join link."},
        ],
        category="events",
    )


@pytest.fixture
def html_template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify {{ product }}",
        html_body="<p>Verify at {{ verification_url }}</p>",
        text_body="Verify at {{ verification_url }}",
    )


CONTEXT = {
    "user_name": "Ada",
    "event_title": "Community Lunch",
    "join_url": "https://events.example.com/join",
}


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def api(client, name, payload=None, *, template_key=None, raw_key=API_KEY):
    args = [template_key] if template_key is not None else []
    method = "post" if payload is not None or name.endswith(("publish", "preview", "test-send")) else "get"
    data = payload if payload is not None else {}
    if method == "post":
        return client.post(
            reverse(f"mailing:{name}", args=args),
            data=data,
            content_type="application/json",
            **auth_headers(raw_key),
        )
    return client.get(reverse(f"mailing:{name}", args=args), **auth_headers(raw_key))


def publish_template(client, template_key):
    return api(client, "api_transactional_template_publish", template_key=template_key)


def post_send(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_publish_creates_monotonic_versions_and_refuses_mutation(client, api_client_record, markdown_template):
    first = publish_template(client, markdown_template.key)
    assert first.status_code == 201
    assert first.json()["latest_version"] == 1
    assert first.json()["version"]["markdown_body"] == markdown_template.markdown_body
    assert first.json()["version"]["category"] == "events"

    markdown_template.subject = "Changed draft subject"
    markdown_template.save(update_fields=["subject", "updated_at"])

    second = publish_template(client, markdown_template.key)
    assert second.status_code == 201
    assert second.json()["latest_version"] == 2

    versions = EmailTemplateVersion.objects.filter(template=markdown_template).order_by("version")
    assert versions[0].version == 1
    assert versions[0].subject == "You're registered: {{ event_title }}"
    assert versions[1].subject == "Changed draft subject"

    version_one = versions[0]
    with pytest.raises(ValueError, match="immutable"):
        version_one.subject = "mutated"
        version_one.save()
    with pytest.raises(ValueError, match="immutable"):
        version_one.delete()


def test_send_without_version_uses_latest_published_then_draft(
    client,
    api_client_record,
    markdown_template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    payload = {
        "email": "Person@Example.COM",
        "template_key": markdown_template.key,
        "idempotency_key": "reg-1",
        "context": CONTEXT,
    }

    draft_send = post_send(client, payload)
    assert draft_send.status_code == 202
    draft_message = TransactionalMessage.objects.get(idempotency_key="reg-1")
    assert draft_message.template_version is None
    assert draft_message.subject == "You're registered: Community Lunch"
    assert "email-header" in draft_message.html_body
    assert enqueued and "template_version" not in enqueued[0]

    markdown_template.markdown_body = "# v1 body {{ user_name }}"
    markdown_template.save(update_fields=["markdown_body", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201

    markdown_template.markdown_body = "# draft body {{ user_name }}"
    markdown_template.save(update_fields=["markdown_body", "updated_at"])

    published_send = post_send(client, {**payload, "idempotency_key": "reg-2"})
    assert published_send.status_code == 202
    published_message = TransactionalMessage.objects.get(idempotency_key="reg-2")
    assert published_message.template_version == 1
    assert "<h1>v1 body Ada</h1>" in published_message.html_body
    assert "draft body" not in published_message.html_body
    assert enqueued[-1]["template_version"] == 1
    assert validate_transactional_email_message(enqueued[-1]) == enqueued[-1]


def test_send_with_explicit_version_renders_that_snapshot(
    client,
    api_client_record,
    markdown_template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    assert publish_template(client, markdown_template.key).status_code == 201

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "idempotency_key": "reg-1",
            "context": CONTEXT,
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template_version == 1
    assert message.subject == "You're registered: Community Lunch"

    markdown_template.subject = "Draft-only subject"
    markdown_template.save(update_fields=["subject", "updated_at"])

    replay = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "idempotency_key": "reg-2",
            "context": CONTEXT,
        },
    )
    assert replay.status_code == 202
    second = TransactionalMessage.objects.get(idempotency_key="reg-2")
    assert second.subject == "You're registered: Community Lunch"
    assert second.template_version == 1


def test_send_missing_required_context_fails_before_any_work(client, api_client_record, markdown_template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "idempotency_key": "reg-1",
            "context": {"user_name": "Ada"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {
        "context.join_url": "required",
    }
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_published_version_context_requirements_apply_to_sends(
    client,
    api_client_record,
    html_template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    html_template.required_context = [{"name": "verification_url", "description": "Verify link."}]
    html_template.save(update_fields=["required_context", "updated_at"])
    assert publish_template(client, html_template.key).status_code == 201

    # The draft no longer requires the key; the published version still does.
    html_template.required_context = []
    html_template.save(update_fields=["required_context", "updated_at"])

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": html_template.key,
            "idempotency_key": "verify-1",
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"context.verification_url": "required"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


def test_preview_matches_dry_run_send_for_same_version_and_context(
    client,
    api_client_record,
    markdown_template,
):
    assert publish_template(client, markdown_template.key).status_code == 201

    markdown_template.subject = "Draft-only subject {{ user_name }}"
    markdown_template.save(update_fields=["subject", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201

    send_payload = {
        "email": "person@example.com",
        "template_key": markdown_template.key,
        "template_version": 1,
        "idempotency_key": "parity-1",
        "context": CONTEXT,
        "dry_run": True,
    }
    dry_run = post_send(client, send_payload)
    assert dry_run.status_code == 202

    preview = api(
        client,
        "api_transactional_template_preview",
        payload={"context": CONTEXT, "template_version": 1},
        template_key=markdown_template.key,
    )
    assert preview.status_code == 200
    body = preview.json()
    rendered = dry_run.json()["rendered"]
    assert body["subject"] == rendered["subject"]
    assert body["html_body"] == rendered["html_body"]
    assert body["text_body"] == rendered["text_body"]
    assert body["template_version"] == 1
    assert body["published"] is True
    assert body["missing_context"] == []


def test_preview_reports_missing_context_without_failing(
    client,
    api_client_record,
    markdown_template,
):
    preview = api(
        client,
        "api_transactional_template_preview",
        payload={"context": {"user_name": "Ada"}},
        template_key=markdown_template.key,
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["published"] is False
    assert body["template_version"] is None
    assert body["missing_context"] == ["join_url"]
    # Missing keys render empty (Django template semantics); missing_context
    # is what tells the preview caller which keys produced nothing.
    assert "{{ join_url }}" not in body["html_body"]
    assert body["text_body"].rstrip().endswith("Join link:")


def test_preview_and_send_reject_unknown_or_invalid_versions(client, api_client_record, html_template):
    send = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": html_template.key,
            "template_version": 7,
            "idempotency_key": "verify-1",
            "context": {},
        },
    )
    assert send.status_code == 404
    assert send.json()["error"]["fields"] == {"template_version": "not_found"}
    assert TransactionalMessage.objects.count() == 0

    preview = api(
        client,
        "api_transactional_template_preview",
        payload={"template_version": 7},
        template_key=html_template.key,
    )
    assert preview.status_code == 404

    for bad_version in (0, -1, "one", True):
        invalid = api(
            client,
            "api_transactional_template_preview",
            payload={"template_version": bad_version},
            template_key=html_template.key,
        )
        assert invalid.status_code == 400, bad_version
        assert invalid.json()["error"]["fields"] == {"template_version": "must_be_positive_int"}


def test_versions_endpoint_lists_newest_first(client, api_client_record, html_template):
    missing = api(client, "api_transactional_template_versions", template_key="does-not-exist")
    assert missing.status_code == 404

    empty = api(client, "api_transactional_template_versions", template_key=html_template.key)
    assert empty.status_code == 200
    assert empty.json() == {
        "template_key": html_template.key,
        "latest_version": None,
        "versions": [],
    }

    assert publish_template(client, html_template.key).status_code == 201
    assert publish_template(client, html_template.key).status_code == 201

    listed = api(client, "api_transactional_template_versions", template_key=html_template.key)
    assert listed.status_code == 200
    body = listed.json()
    assert body["template_key"] == html_template.key
    assert body["latest_version"] == 2
    assert [version["version"] for version in body["versions"]] == [2, 1]
    assert body["versions"][0]["subject"] == html_template.subject


def test_template_get_includes_markdown_category_and_latest_version(
    client,
    api_client_record,
    markdown_template,
):
    detail = client.get(
        reverse("mailing:api_transactional_template", args=[markdown_template.key]),
        **auth_headers(),
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["markdown_body"] == markdown_template.markdown_body
    assert body["category"] == "events"
    assert body["latest_version"] is None

    assert publish_template(client, markdown_template.key).status_code == 201

    detail = client.get(
        reverse("mailing:api_transactional_template", args=[markdown_template.key]),
        **auth_headers(),
    )
    assert detail.json()["latest_version"] == 1


def test_upsert_accepts_markdown_draft_fields(client, api_client_record):
    payload = {
        "name": "Event registration",
        "subject": "You're registered: {{ event_title }}",
        "markdown_body": "# Hi {{ user_name }}",
        "category": "events",
        "required_context": [{"name": "user_name", "description": "Recipient name."}],
    }
    response = client.put(
        reverse("mailing:api_transactional_template", args=["event_registration"]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )
    assert response.status_code == 200
    assert response.json()["created"] is True
    assert response.json()["template"]["markdown_body"] == "# Hi {{ user_name }}"
    assert response.json()["template"]["category"] == "events"

    template = EmailTemplate.objects.get(client=api_client_record, key="event_registration")
    assert template.markdown_body == "# Hi {{ user_name }}"
    assert template.category == "events"


def test_upsert_rejects_non_string_markdown_or_empty_category(client, api_client_record):
    response = client.put(
        reverse("mailing:api_transactional_template", args=["event_registration"]),
        data={"name": "Event registration", "subject": "s", "markdown_body": 5, "category": " "},
        content_type="application/json",
        **auth_headers(),
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {
        "markdown_body": "must_be_string",
        "category": "must_be_non_empty_string",
    }


@override_settings(TRANSACTIONAL_TEST_SEND_ALLOWLIST=[])
def test_test_send_disabled_when_allowlist_empty(client, api_client_record, markdown_template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = api(
        client,
        "api_transactional_template_test_send",
        payload={"email": "staff@example.com", "context": CONTEXT},
        template_key=markdown_template.key,
    )
    assert response.status_code == 403
    assert response.json()["error"]["fields"] == {"test_send": "allowlist_not_configured"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


@override_settings(TRANSACTIONAL_TEST_SEND_ALLOWLIST=["staff@example.com"])
def test_test_send_rejects_address_outside_allowlist(client, api_client_record, markdown_template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = api(
        client,
        "api_transactional_template_test_send",
        payload={"email": "learner@example.com", "context": CONTEXT},
        template_key=markdown_template.key,
    )
    assert response.status_code == 403
    assert response.json()["error"]["fields"] == {"email": "not_in_test_send_allowlist"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


@override_settings(TRANSACTIONAL_TEST_SEND_ALLOWLIST=["Staff@Example.com", "pm@example.com"])
def test_test_send_allowlisted_address_uses_durable_send_pipeline(
    client,
    api_client_record,
    markdown_template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    assert publish_template(client, markdown_template.key).status_code == 201

    response = api(
        client,
        "api_transactional_template_test_send",
        payload={"email": "staff@example.com", "template_version": 1, "context": CONTEXT},
        template_key=markdown_template.key,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["test_recipient"] == "staff@example.com"
    assert body["enqueued"] is True
    assert body["idempotent_replay"] is False

    message = TransactionalMessage.objects.get()
    assert message.email == "staff@example.com"
    assert message.template_version == 1
    assert message.metadata["test_send"] is True
    assert len(enqueued) == 1
    assert enqueued[0]["template_version"] == 1


def test_test_send_requires_email(client, api_client_record, markdown_template):
    response = api(
        client,
        "api_transactional_template_test_send",
        payload={"context": CONTEXT},
        template_key=markdown_template.key,
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"email": "required"}


def test_template_endpoints_require_authentication(client, api_client_record, markdown_template):
    for name in (
        "api_transactional_template_publish",
        "api_transactional_template_versions",
        "api_transactional_template_preview",
        "api_transactional_template_test_send",
    ):
        if name.endswith("versions"):
            response = client.get(reverse(f"mailing:{name}", args=[markdown_template.key]))
        else:
            response = client.post(
                reverse(f"mailing:{name}", args=[markdown_template.key]),
                data={},
                content_type="application/json",
            )
        assert response.status_code == 401, name


def test_markdown_send_wraps_body_in_email_shell_with_footer(
    client,
    api_client_record,
    markdown_template,
    settings,
):
    settings.RELAY_EMAIL_BRAND_NAME = "Datamailer"
    settings.RELAY_EMAIL_SITE_BASE_URL = "https://datamailer.example.com"

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "idempotency_key": "reg-1",
            "context": CONTEXT,
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.html_body.startswith("<!DOCTYPE html>")
    assert "Datamailer" in message.html_body
    assert "<h1>Hi Ada</h1>" in message.html_body
    assert "email-footer" in message.html_body
    # Text part carries the substituted markdown source, not HTML.
    assert "# Hi Ada" in message.text_body
    assert "<h1>" not in message.text_body
