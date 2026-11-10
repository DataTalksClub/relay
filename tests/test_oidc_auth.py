import logging
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.contrib.auth import get_user_model

from relay import oidc

AUTH_SETTINGS = {
    "AUTH_BASE_URL": "https://auth.example.test",
    "AUTH_CLIENT_ID": "datamailer-client",
    "AUTH_CALLBACK_URL": "https://relay.example.test/auth/callback",
    "AUTH_LOGOUT_URL": "https://relay.example.test/",
    "AUTH_ISSUER": "https://issuer.example.test/pool",
    "AUTH_JWKS_URL": "https://issuer.example.test/pool/.well-known/jwks.json",
}


@pytest.mark.django_db
def test_oidc_login_uses_pkce_and_verified_callback_creates_staff_session(client, settings, monkeypatch):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    settings.SESSION_COOKIE_SECURE = True
    start = client.get("/auth/login?return_to=/admin/")
    assert start.status_code == 302
    authorize = urlparse(start["Location"])
    query = parse_qs(authorize.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    nonce = client.session["oidc"]["nonce"]
    monkeypatch.setattr(oidc, "_exchange", lambda code, verifier: {"id_token": "signed-token"})
    monkeypatch.setattr(
        oidc,
        "_verify",
        lambda token: {
            "sub": "person-1",
            "email": "Person@DataTalks.Club",
            "email_verified": True,
            "nonce": nonce,
        },
    )
    callback = client.get(f"/auth/callback?code=valid&state={query['state'][0]}")
    assert callback.status_code == 302
    assert callback["Location"] == "/admin/"
    assert callback.cookies["sessionid"]["secure"] is True
    assert callback.cookies["sessionid"]["httponly"] is True
    user = get_user_model().objects.get(email="person@datatalks.club")
    assert user.is_staff is True
    assert client.get("/admin/").status_code == 200


@pytest.mark.django_db
def test_oidc_callback_rejects_invalid_state(client, settings):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    response = client.get("/auth/callback?code=valid&state=wrong")
    assert response.status_code == 303
    assert response["Location"] == "/auth/error?reason=session"
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response.content == b""


@pytest.mark.django_db
def test_oidc_workspace_policy_error_is_visible_and_logged_safely(client, settings, caplog):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    start = client.get("/auth/login?return_to=/admin/")
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    description = "PreSignUp failed with error Sign-in is restricted to a verified account in the approved Workspace domain."
    query = urlencode({"error": "invalid_request", "error_description": description, "state": state})

    with caplog.at_level(logging.WARNING, logger="relay.oidc"):
        response = client.get(f"/auth/callback?{query}")

    assert response.status_code == 303
    assert response["Location"] == "/auth/error?reason=workspace"
    assert "relay_oidc_provider_error" in caplog.text
    assert "code=invalid_request" in caplog.text
    assert "category=workspace" in caplog.text
    assert description not in caplog.text

    error_page = client.get(response["Location"])
    assert error_page.status_code == 403
    assert b"verified Google Workspace account ending in @datatalks.club" in error_page.content
    assert b"PreSignUp" not in error_page.content


@pytest.mark.django_db
def test_oidc_token_failure_logs_type_without_exception_details(client, settings, monkeypatch, caplog):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    start = client.get("/auth/login?return_to=/admin/")
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]

    def fail_exchange(_code, _verifier):
        raise RuntimeError("private token exchange detail")

    monkeypatch.setattr(oidc, "_exchange", fail_exchange)
    with caplog.at_level(logging.ERROR, logger="relay.oidc"):
        response = client.get(f"/auth/callback?code=valid&state={state}")

    assert response.status_code == 303
    assert response["Location"] == "/auth/error?reason=provider"
    assert "relay_oidc_token_verification_failed exception_type=RuntimeError" in caplog.text
    assert "private token exchange detail" not in caplog.text


def test_oidc_jwks_lookup_has_a_bounded_timeout(settings, monkeypatch):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    calls = {}

    class FakeJwksClient:
        def __init__(self, url, *, timeout):
            calls["url"] = url
            calls["timeout"] = timeout

        def get_signing_key_from_jwt(self, _id_token):
            return type("SigningKey", (), {"key": "public-key"})()

    monkeypatch.setattr(oidc.jwt, "PyJWKClient", FakeJwksClient)
    monkeypatch.setattr(oidc.jwt, "decode", lambda *_args, **_kwargs: {"sub": "person-1"})

    assert oidc._verify("signed-token") == {"sub": "person-1"}
    assert calls == {"url": AUTH_SETTINGS["AUTH_JWKS_URL"], "timeout": 15}


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example",
        "/\\evil.example",
        "/%5cevil.example",
        "/%255cevil.example",
        "https://evil.example/",
        "/%2f%2fevil.example",
        "/auth/callback",
        "/auth/error",
        "/ok%0d%0aLocation:https://evil.example",
        "/bad%escape",
    ],
)
def test_oidc_return_to_rejects_unsafe_targets(value):
    assert oidc._safe_return_to(value) == "/admin/"


def test_oidc_return_to_accepts_clean_local_path_and_query():
    assert oidc._safe_return_to("/admin/mailing/campaign/?page=2#ignored") == "/admin/mailing/campaign/?page=2"


def test_oidc_error_page_is_clean_and_hardened(client):
    response = client.get("/auth/error?code=not-rendered&state=not-rendered&reason=%3Cscript%3Ealert(1)%3C/script%3E")
    assert response.status_code == 403
    assert b"not-rendered" not in response.content
    assert b"Sign-in error" in response.content
    # Kit auth page: styled by the vendored dakit bundle in both themes, no
    # inline light-only colours.
    assert b'class="dk-auth-page"' in response.content
    assert b"/static/dakit/dakit.css" in response.content
    assert b"/static/mailing/js/theme-boot.js" in response.content
    assert b"<style" not in response.content
    assert b"#f5f7fa" not in response.content
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in response["Content-Security-Policy"]


def test_admin_login_redirects_to_shared_auth_when_configured(client, settings):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    response = client.get("/admin/login/")
    assert response.status_code == 302
    assert response["Location"] == "/auth/login?return_to=/admin/"


@pytest.fixture
def signed_in():
    from django.test import Client  # noqa: PLC0415

    user = get_user_model().objects.create_user("signout", "signout@example.com", "x", is_staff=True)
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    return browser


@pytest.mark.django_db
def test_logout_get_never_ends_the_session(signed_in):
    response = signed_in.get("/auth/logout", HTTP_ORIGIN="https://evil.example")
    assert response.status_code == 405
    assert "_auth_user_id" in signed_in.session


@pytest.mark.django_db
def test_logout_post_without_csrf_token_is_refused(signed_in):
    response = signed_in.post("/auth/logout")
    assert response.status_code == 403
    assert "_auth_user_id" in signed_in.session


@pytest.mark.django_db
def test_logout_post_with_csrf_token_signs_out_and_hands_off(signed_in, settings):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    page = signed_in.get("/")
    token = page.cookies["csrftoken"].value if "csrftoken" in page.cookies else signed_in.cookies["csrftoken"].value
    response = signed_in.post("/auth/logout", {"csrfmiddlewaretoken": token})
    assert response.status_code == 302
    assert response["Location"].startswith("https://auth.example.test/logout?")
    assert "_auth_user_id" not in signed_in.session
