import ast
import inspect
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from jobs import operator_views as job_operator_views
from jobs import views as job_views
from jobs.models import Job, Schedule
from jobs.urls import urlpatterns as job_patterns
from mailing import (
    activity_ui_views,
    admin_key_views,
    campaign_ui_views,
    contact_ui_views,
    documentation_views,
    setup_views,
    template_ui_views,
    views,
)
from mailing.admin_operations import READ_FIELDS
from mailing.admin_urls import OPERATOR_API_PARITY
from mailing.admin_urls import urlpatterns as admin_patterns
from mailing.models import (
    AdminApiKey,
    Audience,
    Client,
    Contact,
    InboundAddress,
    InboundMessage,
    OperatorAudit,
    Organization,
)
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.auth import authenticate_bearer_token, create_client_api_key
from mailing.urls import urlpatterns as client_patterns

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    user = get_user_model().objects.create_user("admin-api", is_staff=True)
    key, token = create_admin_api_key(user=user, name="automation")
    org = Organization.objects.create(name="Org", slug="org")
    relay_client = Client.objects.create(organization=org, name="Client", slug="client")
    audience = Audience.objects.create(organization=org, name="Audience", slug="audience")
    http = HttpClient(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {token}")
    return http, user, key, token, relay_client, audience


def send(http, path, data, method="post"):
    return getattr(http, method)("/api/admin/" + path, data=json.dumps(data), content_type="application/json")


def test_credentials_are_separate_and_sessions_do_not_authenticate(setup):
    http, user, key, token, client, _ = setup
    assert authenticate_bearer_token(f"Bearer {token}").error == "invalid_api_key"
    _, client_token = create_client_api_key(client=client, name="client")
    assert http.get("/api/contacts").status_code == 401
    unauth = HttpClient(HTTP_AUTHORIZATION=f"Bearer {client_token}")
    assert unauth.get("/api/admin/clients").status_code == 401
    unauth.force_login(user)
    assert unauth.get("/api/admin/clients").status_code == 401
    assert http.get("/api/admin/clients").status_code == 200
    key.refresh_from_db()
    assert key.last_used_at is not None
    for attr in ("is_staff", "is_active"):
        setattr(user, attr, False)
        user.save()
        assert http.get("/api/admin/clients").status_code == 401
        setattr(user, attr, True)
        user.save()


def test_key_lifecycle_and_audits_never_expose_hash_or_secret(setup):
    http, user, key, token, client, _ = setup
    response = send(http, "api-keys", {"name": "second"})
    assert response.status_code == 201
    raw = response.json()["api_key"]
    listed = http.get("/api/admin/api-keys")
    assert raw not in listed.content.decode()
    assert key.key_hash not in listed.content.decode()
    response = send(http, f"clients/{client.id}/api-keys", {"name": "integration"})
    assert response.status_code == 201
    client_token = response.json()["api_key"]
    client_key_id = response.json()["id"]
    assert authenticate_bearer_token(f"Bearer {client_token}").client == client
    assert send(http, f"clients/{client.id}/api-keys/{client_key_id}/revoke", {}).status_code == 200
    assert authenticate_bearer_token(f"Bearer {client_token}").error == "invalid_api_key"
    assert send(http, f"api-keys/{key.id}/revoke", {}).status_code == 200
    assert http.get("/api/admin/clients").status_code == 401
    audits = list(OperatorAudit.objects.values("actor_id", "metadata"))
    assert all(a["actor_id"] == user.id for a in audits)
    assert token not in str(audits) and raw not in str(audits) and client_token not in str(audits)


def test_bootstrap_command(setup):
    _, user, _, _, _, _ = setup
    out = io.StringIO()
    call_command("create_admin_api_key", user=user.username, name="cli", stdout=out)
    assert out.getvalue().startswith("relay_admin_")
    key = AdminApiKey.objects.get(name="cli")
    assert key.key_hash not in out.getvalue()
    with pytest.raises(CommandError):
        call_command("create_admin_api_key", user=user.username, name="cli")
    user.is_staff = False
    user.save()
    with pytest.raises(CommandError):
        call_command("create_admin_api_key", user=user.username, name="forbidden")


def test_resource_provisioning_validation_and_pagination(setup):
    http, _, _, _, client, _ = setup
    org = send(http, "organizations", {"name": "Second", "slug": "second"})
    assert org.status_code == 201
    org_id = org.json()["id"]
    response = send(http, "clients", {"name": "New", "slug": "new", "organization_id": org_id})
    assert response.status_code == 201
    response = send(http, f"clients/{client.id}", {"is_active": False}, "patch")
    assert response.json()["is_active"] is False
    assert (
        send(
            http, "clients", {"name": "Bad", "slug": "bad", "organization_id": org_id, "is_active": "false"}
        ).status_code
        == 400
    )
    assert send(http, f"clients/{client.id}", {"organization_id": org_id}, "patch").status_code == 400
    assert send(http, "organizations", {"name": "Bad", "slug": "second"}).status_code == 400
    assert send(http, "organizations", {"name": "Bad", "slug": "bad", "secret": "no"}).status_code == 400
    assert http.get("/api/admin/clients?limit=1").json()["total"] == 2
    assert http.get("/api/admin/clients?limit=201").status_code == 400
    assert send(http, "organizations", []).status_code == 400
    assert http.post("/api/admin/organizations", data="{", content_type="application/json").status_code == 400


def test_admin_import_export_uses_selected_client(setup):
    http, _, _, _, client, audience = setup
    response = send(
        http,
        f"clients/{client.id}/contacts/imports",
        {
            "audience": audience.slug,
            "client": client.slug,
            "contacts": [{"email": "one@example.com", "status": "subscribed"}],
        },
    )
    assert response.status_code == 200, response.content
    assert Contact.objects.filter(email="one@example.com").exists()
    response = http.get(f"/api/admin/clients/{client.id}/contacts.csv?audience={audience.slug}&client={client.slug}")
    assert response.status_code == 200, response.content
    assert "one@example.com" in response.content.decode()
    assert response["Cache-Control"] == "no-store"
    assert OperatorAudit.objects.filter(action="admin.api_contact_imports", metadata__admin_key_id=setup[2].id).exists()
    other = Client.objects.create(organization=client.organization, name="Other", slug="other")
    response = http.get(f"/api/admin/clients/{other.id}/contacts.csv?audience={audience.slug}&client={other.slug}")
    assert "one@example.com" not in response.content.decode()


@pytest.mark.parametrize("resource", list(READ_FIELDS))
def test_operator_read_routes_and_projection_fields(setup, resource):
    http = setup[0]
    model, fields = READ_FIELDS[resource]
    # Catch projection drift even when the collection is empty.
    instance = model()
    for field in fields.split():
        assert hasattr(instance, field), (resource, field)
    response = http.get("/api/admin/" + resource)
    assert response.status_code == 200, response.content
    assert "items" in response.json()


def test_client_settings_preserve_secrets_and_reject_scope_moves(setup):
    http, _, _, _, client, _ = setup
    client.cmp_webhook_token = "callback-secret"
    client.mailchimp_api_key = "mailchimp-secret-us1"
    client.save()
    response = send(http, f"clients/{client.id}/settings", {"name": "Renamed"}, "patch")
    assert response.status_code == 200, response.content
    client.refresh_from_db()
    assert client.name == "Renamed" and client.cmp_webhook_token == "callback-secret"
    assert "callback-secret" not in response.content.decode()
    assert "mailchimp-secret" not in response.content.decode()
    other = Organization.objects.create(name="Other", slug="other")
    assert send(http, f"clients/{client.id}/settings", {"organization": other.id}, "patch").status_code == 400
    client.refresh_from_db()
    assert client.organization_id != other.id


def test_campaign_forms_queue_estimate_and_edit_guard(setup):
    http, _, _, _, client, audience = setup
    response = send(
        http,
        f"clients/{client.id}/campaign-drafts",
        {
            "audience": audience.id,
            "client": client.id,
            "subject": "Hello",
            "html_body": "<p>Hello</p>",
            "text_body": "Hello",
        },
    )
    assert response.status_code == 201, response.content
    campaign_id = response.json()["id"]
    assert http.get(f"/api/admin/campaigns/{campaign_id}/queue").status_code == 200
    assert send(http, f"campaigns/{campaign_id}/queue", {}).status_code == 400
    response = send(http, f"clients/{client.id}/campaign-drafts/{campaign_id}", {"subject": "Updated"}, "patch")
    assert response.status_code == 200, response.content
    assert response.json()["subject"] == "Updated"


def test_contact_and_inbound_operator_actions(setup):
    http, _, _, _, client, audience = setup
    contact = Contact.objects.create(email="person@example.com")
    response = send(
        http,
        f"contacts/{contact.id}/state",
        {
            "verified_state": "verified",
            "email_validation_status": "valid",
            "hard_bounced": True,
        },
    )
    assert response.status_code == 200 and response.json()["hard_bounced_at"]
    response = send(
        http,
        f"clients/{client.id}/contacts/{contact.id}/subscriptions",
        {
            "audience": audience.id,
            "status": "subscribed",
            "verified": True,
        },
    )
    assert response.status_code == 200, response.content
    tag = send(http, "tags", {"audience_id": audience.id, "name": "Group", "slug": "group"})
    assert tag.status_code == 201
    assert http.get(f"/api/admin/tags/{tag.json()['id']}").status_code == 200
    response = send(http, "inbound-addresses", {"local_part": "inbox", "domain": "example.com"})
    assert response.status_code == 201
    address_id = response.json()["id"]
    assert send(http, f"inbound-addresses/{address_id}", {"is_active": False}, "patch").status_code == 200
    assert not InboundAddress.objects.get(pk=address_id).is_active
    message = InboundMessage.objects.create(sender_address="spam@example.com", sender_domain="example.com")
    assert send(http, f"inbound-messages/{message.id}/block", {"scope": "address"}).status_code == 200
    assert http.get(f"/api/admin/inbound-messages/{message.id}").status_code == 200
    assert send(http, f"inbound-messages/{message.id}/unblock", {}).status_code == 200


def test_client_api_operations_have_admin_counterparts():
    for pattern in client_patterns:
        if str(pattern.pattern).startswith("api/") and pattern.name != "api_worker_status":
            # Reversing with positional placeholder values tests route coverage.
            values = [
                1 if converter.__class__.__name__ == "IntConverter" else "test"
                for converter in pattern.pattern.converters.values()
            ]
            assert reverse("admin_api:scoped_" + pattern.name, args=[1, *values]).startswith("/api/admin/clients/1/")


def test_staff_ui_capabilities_have_admin_api_routes():
    staff_views = set()
    modules = (views, admin_key_views, job_views, job_operator_views, activity_ui_views,
               campaign_ui_views, contact_ui_views, documentation_views, setup_views, template_ui_views)
    for module in modules:
        declared = {
            node.name for node in ast.parse(inspect.getsource(module)).body
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Name) and d.id == "staff_member_required" for d in node.decorator_list
            )
        }
        for pattern in [*client_patterns, *job_patterns]:
            if pattern.callback.__module__ == module.__name__ and pattern.callback.__name__ in declared:
                staff_views.add(pattern.name)
    assert staff_views == set(OPERATOR_API_PARITY)
    api_names = {p.name for p in admin_patterns}
    assert set(OPERATOR_API_PARITY.values()) <= api_names


def test_admin_key_ui_creates_and_revokes_without_leaking_secret(setup):
    _, user, _, _, _, _ = setup
    http = HttpClient()
    assert http.get("/admin-api-keys/").status_code == 302
    http.force_login(user)
    response = http.post("/admin-api-keys/", {"name": "ui"})
    assert response.status_code == 200
    raw = response.context["raw_key"]
    assert raw.startswith("relay_admin_")
    assert raw in response.content.decode()
    assert raw not in http.get("/admin-api-keys/").content.decode()
    key = AdminApiKey.objects.get(name="ui")
    assert key.key_hash not in response.content.decode()
    assert http.post(f"/admin-api-keys/{key.id}/revoke/").status_code == 302
    assert HttpClient(HTTP_AUTHORIZATION=f"Bearer {raw}").get("/api/admin/clients").status_code == 401


def test_endpoint_discovery_and_json_not_found(setup):
    http = setup[0]
    response = http.get("/api/admin/")
    assert response.status_code == 200
    assert any(e["path"].endswith("contacts/imports") for e in response.json()["endpoints"])
    assert http.get("/api/admin/clients/99999").json()["error"]["code"] == "not_found"


def test_status_endpoints_use_operator_data(setup, monkeypatch):
    monkeypatch.setattr("mailing.services.operator_ui.sandbox_worker_statuses", lambda: [])
    monkeypatch.setattr("mailing.services.worker_status.sandbox_worker_statuses", lambda: [])
    http = setup[0]
    response = http.get("/api/admin/dashboard")
    assert response.status_code == 200 and "summary_stats" in response.json()
    assert http.get("/api/admin/workers").status_code == 200


def test_csv_import_dry_run_and_method_guards(setup):
    http, _, _, _, client, audience = setup
    data = {
        "audience": audience.slug,
        "client": client.slug,
        "dry_run": True,
        "csv": "email,subscription_status\ncsv@example.com,subscribed\n",
    }
    response = send(http, f"clients/{client.id}/contacts/imports/csv", data)
    assert response.status_code == 200, response.content
    assert not Contact.objects.filter(email="csv@example.com").exists()
    data["dry_run"] = False
    assert send(http, f"clients/{client.id}/contacts/imports/csv", data).status_code == 200
    assert Contact.objects.filter(email="csv@example.com").exists()
    assert http.delete("/api/admin/clients").status_code == 405
    assert http.post("/api/admin/organizations", data={"name": "x"}).status_code == 415


def test_dead_letter_retry_is_audited_and_enqueued_after_commit(setup, monkeypatch, django_capture_on_commit_callbacks):
    from jobs.models import Job, JobStatus  # noqa: PLC0415

    http, _, key, _, client, _ = setup
    job = Job.objects.create(client=client, task_type="test", task={}, status=JobStatus.FAILED)
    enqueued = []
    monkeypatch.setattr("mailing.admin_operations.enqueue_job", enqueued.append)
    with django_capture_on_commit_callbacks(execute=True):
        response = send(http, f"dead-letters/{job.id}/retry", {})
        assert response.status_code == 200, response.content
        assert enqueued == []
    assert enqueued == [job.id]
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED
    entry = OperatorAudit.objects.get(action="admin.dead_letter.retry")
    assert entry.metadata == {"task_id": str(job.id), "admin_key_id": key.id}


def test_operator_overviews_and_contact_filters(setup):
    http, _, _, _, client, audience = setup
    contact = Contact.objects.create(email="metrics@example.com")
    send(
        http,
        f"clients/{client.id}/contacts/{contact.id}/subscriptions",
        {"audience": audience.id, "status": "subscribed"},
    )
    response = http.get(f"/api/admin/contacts/{contact.id}/overview?client_id={client.id}")
    assert response.status_code == 200, response.content
    assert "metrics" in response.json()["overview"]
    response = http.get(f"/api/admin/audiences/{audience.id}/overview?client_id={client.id}")
    assert response.status_code == 200 and "breakdowns" in response.json()["overview"]
    response = http.get(f"/api/admin/contacts?client_id={client.id}&subscription_status=subscribed")
    assert response.json()["total"] == 1
    response = http.get(f"/api/admin/contacts?client_id={client.id}&subscription_status=unsubscribed")
    assert response.json()["total"] == 0
    assert http.get("/api/admin/contacts?client_id=invalid").status_code == 400


def test_full_data_transfer_uses_admin_auth(setup, settings):
    settings.RELAY_TRANSFER_TOKEN = ""
    http, _, _, _, _, _ = setup
    response = http.get("/api/admin/transfer/export")
    assert response.status_code == 200 and "resources" in response.json()
    response = http.get("/api/admin/transfer/export?resource=organizations")
    assert response.status_code == 200 and response.json()["rows"]
    result = send(http, "transfer/load", response.json())
    assert result.status_code == 200 and result.json()["upserted"] == 1
    assert HttpClient().get("/api/admin/transfer/export").status_code == 401
    assert http.get("/internal/transfer/export").status_code == 404
def test_jobs_admin_recovery_requires_fresh_confirmation(setup):
    http, _, _, _, client, _ = setup
    job = Job.objects.create(client=client, task_type="webhook", idempotency_key="review-admin", status="failed", error="timeout")
    detail = http.get(f"/api/admin/jobs/{job.id}")
    assert detail.status_code == 200
    assert detail.json()["error"] == "timeout"
    assert "task" not in detail.json()
    url = f"jobs/{job.id}/retry"
    assert send(http, url, {"client_id": client.id, "revision": job.updated_at.isoformat()}).status_code == 409
    assert send(http, url, {"client_id": client.id, "revision": "stale", "confirmed": True}).status_code == 409
    job.refresh_from_db()
    assert job.status == "failed"
    response = send(http, url, {"client_id": client.id, "revision": job.updated_at.isoformat(), "confirmed": True})
    assert response.status_code == 200
    job.refresh_from_db()
    assert job.status == "queued"
    assert response.json()["error"] == ""


def test_schedule_admin_pause_resume_uses_existing_future_policy(setup):
    http, _, _, _, client, _ = setup
    schedule = Schedule.objects.create(client=client, name="daily", task_type="webhook", cron="0 9 * * *", next_run_at=timezone.now())
    response = http.get(f"/api/admin/schedules/{schedule.id}")
    assert response.status_code == 200
    assert response.json()["cron"] == "0 9 * * *"
    url = f"schedules/{schedule.id}/action"
    assert send(http, url, {"action": "pause", "confirmed": True, "client_id": client.id, "revision": "stale"}).status_code == 409
    assert send(http, url, {"action": "pause", "confirmed": True, "client_id": client.id, "revision": schedule.updated_at.isoformat()}).status_code == 200
    schedule.refresh_from_db()
    assert not schedule.enabled
    assert send(http, url, {"action": "resume", "confirmed": True, "client_id": client.id, "revision": schedule.updated_at.isoformat()}).status_code == 200
    schedule.refresh_from_db()
    assert schedule.enabled and schedule.next_run_at > timezone.now()
    assert not schedule.jobs.exists()
