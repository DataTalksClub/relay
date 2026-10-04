import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from mailing.models import Client, Contact, EmailTemplate, Organization, TransactionalMessage
from mailing.services.api_docs import build_openapi_spec, workflow_examples
from mailing.setup_views import SenderRowsClientForm, client_setup_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_client():
    org = Organization.objects.create(name="Example", slug="example")
    return Client.objects.create(organization=org, name="App", slug="app")


@pytest.fixture
def staff_browser(client):
    client.force_login(get_user_model().objects.create_user(username="setupstaff", is_staff=True))
    return client


def row_payload(client, **updates):
    payload = {
        "organization": str(client.organization_id),
        "name": client.name,
        "slug": client.slug,
        "is_active": "on",
        "sender_editor": "rows",
        "sender_count": "2",
        "sender-0-id": "courses",
        "sender-0-email": "Courses <courses@example.com>",
        "sender-1-id": "support",
        "sender-1-email": "support@example.com",
        "default_sender_id": "support",
    }
    payload.update(updates)
    return payload


def test_sender_rows_save_default_and_display_name(staff_browser, setup_client):
    response = staff_browser.post(reverse("mailing:client_edit", args=[setup_client.id]), row_payload(setup_client))
    assert response.status_code == 302
    setup_client.refresh_from_db()
    assert setup_client.default_sender_id == "support"
    assert setup_client.sender_emails == [
        {"id": "courses", "email": "Courses <courses@example.com>"},
        {"id": "support", "email": "support@example.com"},
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"sender-1-id": "courses"},
        {"sender-0-email": "invalid"},
        {"default_sender_id": "missing"},
        {"sender_count": "101"},
        {"sender-0-id": "bad=id"},
        {"sender-0-email": "a@example.com\nother=b@example.com"},
    ],
)
def test_sender_rows_reject_invalid_policy(setup_client, changes):
    form = SenderRowsClientForm(row_payload(setup_client, **changes), instance=setup_client)
    assert not form.is_valid()


def test_legacy_sender_import_keeps_existing_contract(setup_client):
    form = SenderRowsClientForm(
        row_payload(
            setup_client, sender_editor="import", sender_emails="news=News <news@example.com>", default_sender_id="news"
        ),
        instance=setup_client,
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["sender_emails"] == [{"id": "news", "email": "News <news@example.com>"}]


def test_setup_does_not_equate_configuration_to_delivery(setup_client):
    setup_client.sender_emails = [{"id": "courses", "email": "courses@example.com"}]
    setup_client.default_sender_id = "courses"
    setup_client.save()
    checks = client_setup_context(setup_client)["setup_checklist"]
    assert checks[0]["status"] == "Configured"
    assert checks[3]["status"] == "Not verified"
    assert "client=%s" % setup_client.id in checks[2]["url"]
    template = EmailTemplate.objects.create(
        client=setup_client, key="welcome", name="Welcome", subject="Hello", text_body="Hi"
    )
    contact = Contact.objects.create(email="recipient@example.com", normalized_email="recipient@example.com")
    message = TransactionalMessage.objects.create(
        client=setup_client,
        contact=contact,
        template=template,
        template_key="welcome",
        email=contact.email,
        subject="Hello",
        status="sent",
    )
    assert not client_setup_context(setup_client)["setup_checklist"][3]["done"]
    message.delivered_at = timezone.now()
    message.save()
    assert client_setup_context(setup_client)["setup_checklist"][3]["done"]
    another = Client.objects.create(organization=setup_client.organization, slug="other", name="Other")
    assert not client_setup_context(another)["setup_checklist"][3]["done"]


def test_docs_hub_short_and_legacy_anchor_destinations_present(staff_browser):
    response = staff_browser.get(reverse("mailing:api_docs"))
    assert response.status_code == 200
    assert b"Send a test email: quickstart" in response.content
    assert b"YOUR_CLIENT_API_KEY" in response.content
    assert b"dry_run" in response.content
    assert b"legacy-doc-anchors" in response.content
    assert b'"upsert-contact"' in response.content
    assert b"relay_dtccourses_demo_transactional_email_key" not in response.content
    assert len(response.content) < 60000


def test_all_workflow_examples_remain_available(staff_browser):

    for group in workflow_examples():
        response = staff_browser.get(reverse("mailing:api_docs_workflow", args=[slugify(group["section"])]))
        assert response.status_code == 200
        for example in group["items"]:
            assert f'id="{example["id"]}"'.encode() in response.content
            assert example["title"].encode() in response.content


def test_reference_search_uses_current_full_openapi(staff_browser):
    response = staff_browser.get(
        reverse("mailing:api_docs_reference"), {"q": "/api/transactional/send", "method": "POST"}
    )
    assert response.status_code == 200
    endpoints = list(response.context["page_obj"])
    expected = build_openapi_spec()["paths"]["/api/transactional/send"]["post"]
    assert any(
        item["path"] == "/api/transactional/send" and item["description"] == expected.get("description", "")
        for item in endpoints
    )
    assert all(item["method"] == "POST" and "/api/transactional/send" in item["path"] for item in endpoints)
    empty = staff_browser.get(reverse("mailing:api_docs_reference"), {"q": "not-an-endpoint"})
    assert b"No matching endpoints." in empty.content


def test_new_docs_destinations_are_staff_only(client):
    for name, args in [
        ("api_docs_reference", []),
        ("api_docs_legacy", []),
        ("api_docs_workflow", ["contact-workflows"]),
    ]:
        response = client.get(reverse("mailing:" + name, args=args))
        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]


def test_invalid_sender_rows_keep_policy_and_expose_accessible_errors(staff_browser, setup_client):
    setup_client.sender_emails = [{"id": "existing", "email": "existing@example.com"}]
    setup_client.default_sender_id = "existing"
    setup_client.save()
    response = staff_browser.post(
        reverse("mailing:client_edit", args=[setup_client.id]),
        row_payload(setup_client, **{"sender-0-email": "invalid"}),
    )
    assert response.status_code == 200
    assert b'id="sender-policy-errors" role="alert" tabindex="-1"' in response.content
    assert b'aria-invalid="true" aria-describedby="sender-policy-errors"' in response.content
    setup_client.refresh_from_db()
    assert setup_client.sender_emails == [{"id": "existing", "email": "existing@example.com"}]
    assert setup_client.default_sender_id == "existing"
