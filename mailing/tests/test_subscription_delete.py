"""Operator path that deletes explicitly listed Subscription rows (dry run by default)."""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient
from django.urls import reverse

from mailing.models import Audience, Client, Contact, OperatorAudit, Organization, Subscription, SubscriptionStatus
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.operator_management import SubscriptionDeleteRejected, delete_subscriptions

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    user = get_user_model().objects.create_user("ops", is_staff=True)
    key, token = create_admin_api_key(user=user, name="ops")
    org = Organization.objects.create(name="Org", slug="org")
    courses = Client.objects.create(organization=org, name="Courses", slug="courses")
    other = Client.objects.create(organization=org, name="Other", slug="other")
    audience = Audience.objects.create(organization=org, name="Aud", slug="aud")
    subs = []
    for index in range(4):
        contact = Contact.objects.create(email=f"person{index}@example.com")
        subs.append(
            Subscription.objects.create(
                contact=contact, audience=audience, client=courses, status=SubscriptionStatus.SUBSCRIBED
            )
        )
    foreign = Subscription.objects.create(
        contact=subs[0].contact, audience=audience, client=other, status=SubscriptionStatus.SUBSCRIBED
    )
    http = HttpClient(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {token}")
    return {
        "http": http,
        "user": user,
        "key": key,
        "courses": courses,
        "other": other,
        "subs": subs,
        "foreign": foreign,
    }


def post(world, payload, client=None):
    url = reverse("admin_api:subscriptions_delete", args=[(client or world["courses"]).id])
    return world["http"].post(url, data=json.dumps(payload), content_type="application/json")


def remaining_ids():
    return set(Subscription.objects.values_list("id", flat=True))


def test_dry_run_is_default_and_deletes_nothing(world):
    before = remaining_ids()
    target = [world["subs"][0].id, world["subs"][1].id]

    response = post(world, {"ids": target})

    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True
    assert data["deleted"] == 0
    assert data["matched"] == 2
    assert [row["id"] for row in data["subscriptions"]] == sorted(target)
    assert set(data["subscriptions"][0]) == {"id", "audience_id", "client_id", "status", "verified_at"}
    assert "example.com" not in response.content.decode()
    assert remaining_ids() == before

    audit = OperatorAudit.objects.get(action="subscription.delete.dry_run")
    assert audit.actor == world["user"]
    assert audit.target_type == "client" and audit.target_id == world["courses"].id
    assert audit.metadata["ids"] == sorted(target)
    assert audit.metadata["dry_run"] is True and audit.metadata["confirm"] is False
    assert audit.metadata["requested"] == 2 and audit.metadata["deleted"] == 0
    assert audit.metadata["admin_key_id"] == world["key"].id


def test_explicit_dry_run_true_deletes_nothing(world):
    before = remaining_ids()
    response = post(world, {"ids": [world["subs"][2].id], "dry_run": True})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert remaining_ids() == before


def test_confirm_deletes_exactly_listed_ids_and_leaves_others(world):
    before = remaining_ids()
    target = {world["subs"][0].id, world["subs"][2].id}

    response = post(world, {"ids": sorted(target), "confirm": True})

    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is False
    assert data["deleted"] == 2
    assert remaining_ids() == before - target
    # Other subscriptions of the same contacts/clients and contacts themselves survive.
    assert Subscription.objects.filter(pk=world["foreign"].pk).exists()
    assert Contact.objects.filter(pk=world["subs"][0].contact_id).exists()

    audit = OperatorAudit.objects.get(action="subscription.delete")
    assert audit.metadata["ids"] == sorted(target)
    assert audit.metadata["confirm"] is True and audit.metadata["dry_run"] is False
    assert audit.metadata["requested"] == 2 and audit.metadata["deleted"] == 2
    assert "example.com" not in json.dumps(audit.metadata)


def test_dry_run_false_without_confirm_is_refused(world):
    before = remaining_ids()
    response = post(world, {"ids": [world["subs"][0].id], "dry_run": False})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirm_required"
    assert remaining_ids() == before
    assert not OperatorAudit.objects.filter(action__startswith="subscription.delete").exists()


def test_confirm_false_is_dry_run_never_delete(world):
    before = remaining_ids()
    response = post(world, {"ids": [world["subs"][0].id], "confirm": False})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert remaining_ids() == before


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"ids": [], "confirm": True}, "ids_required"),
        ({"confirm": True}, "ids_required"),
        ({"ids": "1,2", "confirm": True}, "ids_required"),
        ({"ids": ["1"], "confirm": True}, "malformed_ids"),
        ({"ids": [1.5], "confirm": True}, "malformed_ids"),
        ({"ids": [0], "confirm": True}, "malformed_ids"),
        ({"ids": [True], "confirm": True}, "malformed_ids"),
        ({"ids": [1], "confirm": "yes"}, "validation_error"),
        ({"ids": [1], "confirm": True, "dry_run": True}, "validation_error"),
        ({"ids": [1], "email": "x"}, "unknown_fields"),
    ],
)
def test_malformed_requests_are_rejected(world, payload, code):
    before = remaining_ids()
    response = post(world, payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert remaining_ids() == before


def test_duplicate_ids_rejected(world):
    sub_id = world["subs"][0].id
    response = post(world, {"ids": [sub_id, sub_id], "confirm": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "duplicate_ids"
    assert Subscription.objects.filter(pk=sub_id).exists()


def test_any_missing_id_refuses_whole_request(world):
    before = remaining_ids()
    missing = max(before) + 1000
    response = post(world, {"ids": [world["subs"][0].id, missing], "confirm": True})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "ids_not_deletable"
    assert error["fields"] == {"missing": [missing]}
    assert remaining_ids() == before
    assert not OperatorAudit.objects.filter(action__startswith="subscription.delete").exists()


def test_other_client_id_refuses_whole_request(world):
    before = remaining_ids()
    response = post(world, {"ids": [world["subs"][1].id, world["foreign"].id], "confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"out_of_scope": [world["foreign"].id]}
    assert remaining_ids() == before


def test_unknown_client_is_not_found(world):
    url = reverse("admin_api:subscriptions_delete", args=[999999])
    response = world["http"].post(
        url, data=json.dumps({"ids": [world["subs"][0].id], "confirm": True}), content_type="application/json"
    )
    assert response.status_code == 404


def test_requires_admin_key_and_post(world):
    url = reverse("admin_api:subscriptions_delete", args=[world["courses"].id])
    assert HttpClient().post(url, data="{}", content_type="application/json").status_code == 401
    assert world["http"].get(url).status_code == 405


def test_service_rejects_missing_without_side_effects(world):
    with pytest.raises(SubscriptionDeleteRejected) as excinfo:
        delete_subscriptions(actor=world["user"], client=world["courses"], ids=[world["foreign"].id], confirm=True)
    assert excinfo.value.code == "ids_not_deletable"
    assert Subscription.objects.filter(pk=world["foreign"].pk).exists()
