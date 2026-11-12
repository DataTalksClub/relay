import pytest
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailLayout,
    EmailTemplate,
    EmailTemplateVersion,
    Organization,
    TransactionalMessage,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import create_client_api_key
from mailing.services.email_layouts import default_layouts

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
    spec = default_layouts()["dtc-courses"]
    EmailLayout.objects.create(
        client=client,
        sender_id="",
        brand_name=spec["brand_name"],
        website_url=spec["website_url"],
        layout_html=spec["layout_html"],
    )
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
    assert "DataTalks.Club" in message.html_body
    assert "Relay" not in message.html_body
    assert "<h1>Hi Ada</h1>" in message.html_body
    assert "email-footer" in message.html_body
    # Text part carries the substituted markdown source, not HTML.
    assert "# Hi Ada" in message.text_body
    assert "<h1>" not in message.text_body


def test_layout_context_opt_in_is_versioned_and_matches_preview_and_dry_run(
    client,
    api_client_record,
    markdown_template,
):
    layout = EmailLayout.objects.get(client=api_client_record, sender_id="")
    layout.layout_html = default_layouts()["aisl"]["layout_html"]
    layout.brand_name = "AI Shipping Labs"
    layout.save(update_fields=["layout_html", "brand_name", "updated_at"])
    markdown_template.markdown_layout_context = True
    markdown_template.save(update_fields=["markdown_layout_context", "updated_at"])
    published = publish_template(client, markdown_template.key)
    assert published.status_code == 201
    assert published.json()["version"]["markdown_layout_context"] is True

    markdown_template.markdown_body = "# Changed draft"
    markdown_template.markdown_layout_context = False
    markdown_template.save(
        update_fields=["markdown_body", "markdown_layout_context", "updated_at"]
    )
    context = CONTEXT | {
        "footer_note": "Example footer note",
        "verify_email_url": "https://example.com/verify?token=private",
        "unsubscribe_url": "https://example.com/unsubscribe?token=private",
        "unrelated_private_value": "must not reach the layout",
    }
    send_payload = {
        "email": "person@example.com",
        "template_key": markdown_template.key,
        "template_version": 1,
        "idempotency_key": "layout-parity-v1",
        "context": context,
        "dry_run": True,
    }
    dry_run = post_send(client, send_payload)
    preview = api(
        client,
        "api_transactional_template_preview",
        payload={"context": context, "template_version": 1},
        template_key=markdown_template.key,
    )

    assert dry_run.status_code == 202
    assert preview.status_code == 200
    rendered = dry_run.json()["rendered"]
    assert preview.json()["html_body"] == rendered["html_body"]
    assert preview.json()["text_body"] == rendered["text_body"]
    assert preview.json()["template_version"] == 1
    assert "Changed draft" not in rendered["html_body"]
    assert "must not reach the layout" not in rendered["html_body"]
    assert "must not reach the layout" not in rendered["text_body"]
    for name in ("footer_note", "verify_email_url", "unsubscribe_url"):
        assert context[name] in rendered["html_body"]
        assert context[name] in rendered["text_body"]
    assert "AI Shipping Labs" in rendered["text_body"]


def test_layout_context_opt_out_does_not_expose_reserved_context(
    client,
    markdown_template,
):
    assert publish_template(client, markdown_template.key).status_code == 201
    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "context": CONTEXT | {
                "footer_note": "Must remain private",
                "verify_email_url": "https://example.com/verify",
                "unsubscribe_url": "https://example.com/unsubscribe",
            },
            "dry_run": True,
        },
    )

    assert response.status_code == 202
    rendered = response.json()["rendered"]
    assert "Must remain private" not in rendered["html_body"]
    assert "https://example.com/verify" not in rendered["html_body"]
    assert "https://example.com/unsubscribe" not in rendered["text_body"]


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"footer_note": 7}, {"context.footer_note": "must_be_string"}),
        (
            {"verify_email_url": "javascript:alert(1)"},
            {"context.verify_email_url": "invalid_url"},
        ),
        (
            {"unsubscribe_url": "not-a-url"},
            {"context.unsubscribe_url": "invalid_url"},
        ),
    ],
)
def test_opted_in_layout_context_rejects_invalid_values_before_send_work(
    client,
    markdown_template,
    context,
    expected,
):
    markdown_template.markdown_layout_context = True
    markdown_template.save(update_fields=["markdown_layout_context", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201
    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "context": CONTEXT | context,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == expected
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0


def test_layout_context_html_escapes_footer_text(client, markdown_template):
    markdown_template.markdown_layout_context = True
    markdown_template.save(update_fields=["markdown_layout_context", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201
    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "context": CONTEXT | {"footer_note": "<script>alert(1)</script>"},
            "dry_run": True,
        },
    )

    assert response.status_code == 202
    html = response.json()["rendered"]["html_body"]
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_layout_autoescape_preserves_legacy_plain_body_html(
    client,
    api_client_record,
    markdown_template,
):
    layout = EmailLayout.objects.get(client=api_client_record, sender_id="")
    layout.layout_html = (
        "<html><body>{{ body_html }}"
        "<footer>{{ footer_note }}</footer></body></html>"
    )
    layout.save(update_fields=["layout_html", "updated_at"])
    markdown_template.markdown_layout_context = True
    markdown_template.save(update_fields=["markdown_layout_context", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201

    response = post_send(
        client,
        {
            "email": "person@example.com",
            "template_key": markdown_template.key,
            "template_version": 1,
            "context": CONTEXT | {"footer_note": "<script>alert(1)</script>"},
            "dry_run": True,
        },
    )

    assert response.status_code == 202
    html = response.json()["rendered"]["html_body"]
    assert "<h1>Hi Ada</h1>" in html
    assert "&lt;h1&gt;Hi Ada&lt;/h1&gt;" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_preview_explicit_draft_differs_from_published_version(
    client,
    markdown_template,
):
    markdown_template.markdown_body = "# Published v1 {{ user_name }}"
    markdown_template.save(update_fields=["markdown_body", "updated_at"])
    assert publish_template(client, markdown_template.key).status_code == 201
    markdown_template.markdown_body = "# Saved draft {{ user_name }}"
    markdown_template.save(update_fields=["markdown_body", "updated_at"])

    draft = api(
        client,
        "api_transactional_template_preview",
        payload={"context": CONTEXT, "draft": True},
        template_key=markdown_template.key,
    )
    published = api(
        client,
        "api_transactional_template_preview",
        payload={"context": CONTEXT, "template_version": 1},
        template_key=markdown_template.key,
    )

    assert draft.status_code == 200
    assert draft.json()["published"] is False
    assert draft.json()["template_version"] is None
    assert "Saved draft Ada" in draft.json()["html_body"]
    assert "Published v1" not in draft.json()["html_body"]
    assert published.status_code == 200
    assert published.json()["published"] is True
    assert published.json()["template_version"] == 1
    assert "Published v1 Ada" in published.json()["html_body"]
    assert "Saved draft" not in published.json()["html_body"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"draft": "true"}, {"draft": "must_be_boolean"}),
        (
            {"draft": True, "template_version": 1},
            {"draft": "conflicts_with_template_version"},
        ),
    ],
)
def test_preview_rejects_invalid_draft_selection(
    client,
    markdown_template,
    payload,
    expected,
):
    response = api(
        client,
        "api_transactional_template_preview",
        payload=payload,
        template_key=markdown_template.key,
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == expected
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0


def test_template_api_validates_layout_opt_in_and_supports_dotted_keys(
    client,
    api_client_record,
):
    key = "coursework.pool_ready"
    payload = {
        "name": "Coursework pool ready",
        "subject": "Pool ready",
        "markdown_body": "Your review pool is ready.",
        "markdown_layout_context": True,
    }
    saved = client.put(
        reverse("mailing:api_transactional_template", args=[key]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )
    detail = client.get(
        reverse("mailing:api_transactional_template", args=[key]),
        **auth_headers(),
    )
    published = publish_template(client, key)
    versions = api(client, "api_transactional_template_versions", template_key=key)
    preview = api(
        client,
        "api_transactional_template_preview",
        payload={"template_version": 1},
        template_key=key,
    )

    assert saved.status_code == 200
    assert saved.json()["template"]["markdown_layout_context"] is True
    assert detail.status_code == 200
    assert published.status_code == 201
    assert versions.status_code == 200
    assert versions.json()["versions"][0]["markdown_layout_context"] is True
    assert preview.status_code == 200
    assert preview.json()["template_key"] == key
    assert EmailTemplate.objects.get(client=api_client_record, key=key)

    invalid_type = client.put(
        reverse("mailing:api_transactional_template", args=[key]),
        data=payload | {"markdown_layout_context": "yes"},
        content_type="application/json",
        **auth_headers(),
    )
    assert invalid_type.status_code == 400
    assert invalid_type.json()["error"]["fields"] == {
        "markdown_layout_context": "must_be_boolean"
    }

    raw_html = client.put(
        reverse("mailing:api_transactional_template", args=[key]),
        data={
            "name": "Raw HTML",
            "subject": "Raw",
            "html_body": "<p>Raw</p>",
            "markdown_layout_context": True,
        },
        content_type="application/json",
        **auth_headers(),
    )
    assert raw_html.status_code == 400
    assert raw_html.json()["error"]["fields"] == {
        "markdown_layout_context": "requires_markdown_body"
    }
