import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient

from mailing.models import Audience, Client, EmailTemplate, Organization
from mailing.services.admin_auth import create_admin_api_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    user = get_user_model().objects.create_user("template-api", is_staff=True)
    _, token = create_admin_api_key(user=user, name="template-api")
    organization = Organization.objects.create(name="Template Org", slug="template-org")
    client = Client.objects.create(organization=organization, name="Template Client", slug="template-client")
    Audience.objects.create(organization=organization, name="Audience", slug="audience")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    return http, client


def post(http, path, data):
    return http.post(path, data=json.dumps(data), content_type="application/json")


def test_admin_api_can_create_update_read_and_delete_email_templates(setup):
    http, client = setup
    payload = {
        "client_id": client.id,
        "key": "welcome",
        "name": "Welcome",
        "subject": "Hello",
        "text_body": "Welcome to the list",
        "required_context": ["name"],
        "example_context": {"name": "Reader"},
    }
    created = post(http, "/api/admin/templates", payload)
    assert created.status_code == 201
    template_id = created.json()["id"]
    assert created.json()["client_id"] == client.id
    assert http.get(f"/api/admin/templates/{template_id}").json()["key"] == "welcome"

    updated = http.patch(
        f"/api/admin/templates/{template_id}",
        data=json.dumps({"subject": "Hello there", "is_active": False}),
        content_type="application/json",
    )
    assert updated.status_code == 200
    assert updated.json()["subject"] == "Hello there"
    assert updated.json()["is_active"] is False
    assert EmailTemplate.objects.get(pk=template_id).subject == "Hello there"

    deleted = http.delete(f"/api/admin/templates/{template_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": template_id}
    assert not EmailTemplate.objects.filter(pk=template_id).exists()


def test_admin_template_api_rejects_invalid_field_types(setup):
    http, client = setup
    response = post(
        http,
        "/api/admin/templates",
        {"client_id": client.id, "key": "welcome", "name": "Welcome", "subject": "Hi", "is_active": "false"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"is_active": "invalid_type"}
