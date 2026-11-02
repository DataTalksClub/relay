"""Fast API-key auth regression tests (intake e5cd1579 follow-up).

Prod finding (VictoriaLogs, relay prod `oascx5kjeimc0ivpttag9z3q-*`):
every authenticated API request paid ~240 ms of PBKDF2 `check_password`
(`POST /api/contacts` p50 348/397 ms, `GET /api/contacts/preferences`
p50 308/352 ms, `POST /api/transactional/send` p50 358/408 ms, before/after
`858e94c`). High-entropy API keys must verify with HMAC-SHA256
(`key_fast_hash`); PBKDF2 `key_hash` stays as the legacy fallback with lazy
upgrade. These tests fail on the old per-request-PBKDF2 code path.
"""

import time
from contextlib import contextmanager
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from mailing.models import Audience, Client, ClientApiKey, Contact, Organization, Subscription, SubscriptionStatus
from mailing.services import auth as auth_service
from mailing.services.admin_auth import authenticate_admin_request, create_admin_api_key
from mailing.services.auth import authenticate_bearer_token, create_client_api_key, fast_hash_api_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def api_client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def raw_key(api_client_record):
    _, raw = create_client_api_key(client=api_client_record, name="fast-auth")
    return raw


def _forbid_pbkdf2():
    return mock.patch.object(
        auth_service,
        "check_password",
        side_effect=AssertionError("PBKDF2 must not run on the fast path"),
    )


@contextmanager
def _assert_max_queries(num):
    with CaptureQueriesContext(connection) as ctx:
        yield ctx
    assert len(ctx) <= num, f"expected at most {num} queries, ran {len(ctx)}:\n" + "\n".join(
        q["sql"][:200] for q in ctx.captured_queries
    )


def test_fast_path_authenticates_without_pbkdf2(raw_key, api_client_record):
    with _forbid_pbkdf2():
        result = authenticate_bearer_token(f"Bearer {raw_key}")
    assert result.is_authenticated
    assert result.client.pk == api_client_record.pk


def test_authenticate_wall_time_under_100ms(raw_key):
    start = time.perf_counter()
    try:
        result = authenticate_bearer_token(f"Bearer {raw_key}")
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert result.is_authenticated
    # Old code paid ~240 ms of PBKDF2 here; the fast path is a few ms.
    assert elapsed_ms < 100.0, f"authenticate took {elapsed_ms:.1f} ms"


def test_wrong_secret_rejected_without_match(raw_key):
    public_id = auth_service.public_id_from_raw_key(raw_key)
    with _forbid_pbkdf2():
        # Unknown public id: zero candidates, no hashing at all.
        unknown = authenticate_bearer_token("Bearer relay_nosuchid_abcdefghij123456")
        assert not unknown.is_authenticated
    # Right public id, wrong secret: fast compare fails, PBKDF2 fallback runs
    # (legacy safety net) and still rejects.
    result = authenticate_bearer_token(f"Bearer relay_{public_id}_wrongsecretwrongsecret")
    assert not result.is_authenticated
    assert result.error == "invalid_api_key"


def test_revoked_key_rejected(raw_key, api_client_record):
    key = ClientApiKey.objects.get(client=api_client_record)
    key.revoked_at = auth_service.timezone.now()
    key.save(update_fields=["revoked_at", "updated_at"])
    with _forbid_pbkdf2():
        result = authenticate_bearer_token(f"Bearer {raw_key}")
    assert not result.is_authenticated


def test_missing_and_malformed_headers_rejected():
    assert not authenticate_bearer_token("").is_authenticated
    assert not authenticate_bearer_token("Bearer").is_authenticated
    assert not authenticate_bearer_token("Token abc").is_authenticated


def test_legacy_pbkdf2_key_upgrades_to_fast_hash(api_client_record):
    _, raw = create_client_api_key(client=api_client_record, name="legacy")
    key = ClientApiKey.objects.get(client=api_client_record, name="legacy")
    key.key_fast_hash = ""
    key.save(update_fields=["key_fast_hash", "updated_at"])

    result = authenticate_bearer_token(f"Bearer {raw}")
    assert result.is_authenticated
    key.refresh_from_db()
    assert key.key_fast_hash == fast_hash_api_key(raw)

    # Second request must use the fast path (no PBKDF2 anymore).
    with _forbid_pbkdf2():
        again = authenticate_bearer_token(f"Bearer {raw}")
    assert again.is_authenticated


def test_admin_key_fast_path():
    user = get_user_model().objects.create_user(
        username="adminops", email="adminops@example.com", password="password", is_staff=True
    )
    _, token = create_admin_api_key(user=user, name="automation")
    factory = RequestFactory()

    def authed_request():
        return factory.get("/api/admin/inbound-domains", HTTP_AUTHORIZATION=f"Bearer {token}")

    with _forbid_pbkdf2():
        key = authenticate_admin_request(authed_request())
    assert key is not None
    assert key.user_id == user.pk


def _seed_preferences_contact(audience, api_client_record):
    contact = Contact.objects.create(email="Learner@Example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    return contact


def test_preferences_get_query_and_time_budget(client, audience, api_client_record, raw_key):
    _seed_preferences_contact(audience, api_client_record)
    url = reverse("mailing:api_contact_preferences")
    params = {"email": "learner@example.com", "audience": audience.slug, "client": api_client_record.slug}
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

    with _assert_max_queries(10):
        response = client.get(url, params, **headers)
    assert response.status_code == 200

    start = time.perf_counter()
    try:
        response = client.get(url, params, **headers)
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert response.status_code == 200
    assert elapsed_ms < 200.0, f"preferences GET took {elapsed_ms:.1f} ms"


def test_contacts_upsert_query_and_time_budget(client, audience, api_client_record, raw_key):
    url = reverse("mailing:api_contacts")
    payload = {"email": "new-reader@example.com", "audience": audience.slug, "client": api_client_record.slug}
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

    with CaptureQueriesContext(connection) as ctx:
        response = client.post(url, data=payload, content_type="application/json", **headers)
    assert response.status_code == 200
    # Measured budget on the seeded fixture (auth SELECT+UPDATE, scope,
    # contact+subscription upserts, payload, per-minute monitor). Guards N+1.
    assert len(ctx) <= 28, f"upsert took {len(ctx)} queries"

    start = time.perf_counter()
    try:
        response = client.post(
            url,
            data={"email": "second-reader@example.com", "audience": audience.slug, "client": api_client_record.slug},
            content_type="application/json",
            **headers,
        )
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert response.status_code == 200
    assert elapsed_ms < 200.0, f"contacts upsert took {elapsed_ms:.1f} ms"
