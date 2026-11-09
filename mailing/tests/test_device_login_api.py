import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from mailing.models import AdminApiKey, DeviceLogin
from mailing.services.admin_auth import create_admin_api_key

pytestmark = pytest.mark.django_db


def start_device(client, remote_addr="192.0.2.10"):
    response = client.post(
        "/api/agent/device/start",
        data=json.dumps({"label": "workstation"}),
        content_type="application/json",
        REMOTE_ADDR=remote_addr,
    )
    assert response.status_code == 200
    return response.json()


def test_device_login_uses_shared_auth_contract_and_issues_one_admin_key():
    http = Client()
    start = start_device(http)
    assert start["user_code"][:4] + start["user_code"][5:] == start["user_code"].replace("-", "")
    assert start["interval"] == 5
    assert start["expires_in"] == 600
    assert start["verification_uri_complete"].endswith(f"?code={start['user_code']}")

    pending = http.post(
        "/api/agent/device/token",
        data=json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"]}),
        content_type="application/json",
    )
    assert pending.json() == {"error": "authorization_pending"}

    user = get_user_model().objects.create_user("device-operator", email="operator@example.test", is_staff=True)
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    page = browser.get(f"/device?code={start['user_code']}")
    assert page.status_code == 200
    assert b"workstation" in page.content
    csrf = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
    approved = browser.post(
        "/device/decide",
        {"code": start["user_code"], "approve": "true", "csrfmiddlewaretoken": csrf},
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert approved.status_code == 200

    token_response = http.post(
        "/api/agent/device/token",
        data=json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"]}),
        content_type="application/json",
    )
    token = token_response.json()["token"]
    assert token.startswith("relay_admin_")
    assert AdminApiKey.objects.filter(user=user, revoked_at__isnull=True).count() == 1
    assert DeviceLogin.objects.filter(admin_api_key__user=user, status="consumed").exists()
    reused = http.post(
        "/api/agent/device/token",
        data=json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"]}),
        content_type="application/json",
    )
    assert reused.json() == {"error": "expired_token"}

    revoked = http.post("/api/agent/device/revoke", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert revoked.json() == {"revoked": True}
    assert not AdminApiKey.objects.filter(user=user, revoked_at__isnull=True).exists()


def test_device_approval_requires_staff_and_csrf():
    start = start_device(Client(), remote_addr="192.0.2.11")
    anon = Client()
    assert anon.get(f"/device?code={start['user_code']}").status_code == 302

    user = get_user_model().objects.create_user("staff", email="staff@example.test", is_staff=True)
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    page = browser.get(f"/device?code={start['user_code']}")
    assert page.status_code == 200
    denied = browser.post("/device/decide", {"code": start["user_code"], "approve": "true"})
    assert denied.status_code == 403


def test_device_revoke_cannot_revoke_a_regular_admin_key():
    user = get_user_model().objects.create_user("regular-admin", email="admin@example.test", is_staff=True)
    key, token = create_admin_api_key(user=user, name="manual")
    response = Client().post("/api/agent/device/revoke", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 403
    key.refresh_from_db()
    assert key.revoked_at is None
