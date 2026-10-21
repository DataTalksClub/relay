import pytest
from django.urls import reverse

from mailing.models import Client, EmailTemplate, Organization
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
def other_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="Other",
        slug="other-client",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "other@example.com"}],
    )
    create_client_api_key(client=client, name="Other test", raw_api_key="other-client-key")
    return client


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def list_url():
    return reverse("mailing:api_transactional_templates")


def test_templates_list_requires_authentication(client, api_client_record):
    response = client.get(list_url())
    assert response.status_code == 401


def test_templates_list_rejects_non_get(client, api_client_record):
    for method in ("post", "put", "delete"):
        response = getattr(client, method)(
            list_url(),
            data={},
            content_type="application/json",
            **auth_headers(),
        )
        assert response.status_code == 405, method


def test_templates_list_empty(client, api_client_record):
    response = client.get(list_url(), **auth_headers())
    assert response.status_code == 200
    assert response.json() == {"templates": []}


def test_templates_list_returns_client_templates_ordered_by_key(client, api_client_record):
    EmailTemplate.objects.create(
        client=api_client_record,
        key="welcome-b",
        name="Welcome B",
        subject="Hi B",
        html_body="<p>B</p>",
    )
    EmailTemplate.objects.create(
        client=api_client_record,
        key="welcome-a",
        name="Welcome A",
        subject="Hi A",
        html_body="<p>A</p>",
    )

    response = client.get(list_url(), **auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert [item["key"] for item in body["templates"]] == ["welcome-a", "welcome-b"]
    assert body["templates"][0]["name"] == "Welcome A"
    assert body["templates"][0]["client"] == "dtc-courses"


def test_templates_list_scoped_to_client_and_transactional_only(
    client, api_client_record, other_client_record
):
    EmailTemplate.objects.create(
        client=api_client_record,
        key="mine",
        name="Mine",
        subject="Hi",
        html_body="<p>Hi</p>",
    )
    EmailTemplate.objects.create(
        client=api_client_record,
        key="campaign-only",
        name="Campaign",
        subject="Hi",
        html_body="<p>Hi</p>",
        is_transactional=False,
    )
    EmailTemplate.objects.create(
        client=other_client_record,
        key="mine",
        name="Other Mine",
        subject="Hi",
        html_body="<p>Hi</p>",
    )

    response = client.get(list_url(), **auth_headers())
    assert response.status_code == 200
    assert [item["key"] for item in response.json()["templates"]] == ["mine"]


def test_templates_list_includes_latest_version(client, api_client_record):
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="versioned",
        name="Versioned",
        subject="Hi",
        html_body="<p>Hi</p>",
    )
    first = client.get(list_url(), **auth_headers())
    assert first.json()["templates"][0]["latest_version"] is None

    publish = client.post(
        reverse("mailing:api_transactional_template_publish", args=[template.key]),
        data={},
        content_type="application/json",
        **auth_headers(),
    )
    assert publish.status_code == 201

    second = client.get(list_url(), **auth_headers())
    assert second.json()["templates"][0]["latest_version"] == 1


def test_publish_version_includes_template_key_for_catalog_client(client, api_client_record):
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="versioned-publish",
        name="Versioned",
        subject="Hi",
        html_body="<p>Hi</p>",
    )
    response = client.post(
        reverse("mailing:api_transactional_template_publish", args=[template.key]),
        data={},
        content_type="application/json",
        **auth_headers(),
    )
    assert response.status_code == 201
    version = response.json()["version"]
    assert version["template_key"] == template.key
    assert version["version"] == 1


def test_versions_list_includes_template_key_for_catalog_client(client, api_client_record):
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="versioned-list",
        name="Versioned",
        subject="Hi",
        html_body="<p>Hi</p>",
    )
    client.post(
        reverse("mailing:api_transactional_template_publish", args=[template.key]),
        data={},
        content_type="application/json",
        **auth_headers(),
    )
    response = client.get(
        reverse("mailing:api_transactional_template_versions", args=[template.key]),
        **auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["template_key"] == template.key
    assert len(body["versions"]) == 1
    assert body["versions"][0]["template_key"] == template.key
    assert body["versions"][0]["version"] == 1
