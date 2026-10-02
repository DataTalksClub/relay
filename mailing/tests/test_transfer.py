"""Token-gated copy of mailing rows between hosts."""

import json
from datetime import UTC, datetime

import pytest
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    CategoryPreference,
    Client,
    ClientApiKey,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.transfer import MAX_PAGE, RESOURCE_ORDER

pytestmark = pytest.mark.django_db

TOKEN = "transfer-test-token"
CREATED = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def _auth():
    return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}


def _export(client, resource=None, **params):
    query = {}
    if resource is not None:
        query["resource"] = resource
    query.update(params)
    return client.get(reverse("mailing:transfer_export"), query, **_auth())


def _load(client, payload):
    return client.post(
        reverse("mailing:transfer_load"),
        data=json.dumps(payload),
        content_type="application/json",
        **_auth(),
    )


def _stamp(model, pk):
    fields = {"created_at": CREATED}
    if any(field.name == "updated_at" for field in model._meta.fields):
        fields["updated_at"] = CREATED
    model.objects.filter(pk=pk).update(**fields)


@pytest.fixture
def token(settings):
    settings.RELAY_TRANSFER_TOKEN = TOKEN


def test_unset_token_hides_both_routes(client, settings):
    settings.RELAY_TRANSFER_TOKEN = ""
    export = client.get(reverse("mailing:transfer_export"), HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    load = client.post(reverse("mailing:transfer_load"), data="{}", content_type="application/json")
    assert export.status_code == 404
    assert load.status_code == 404
    assert "resources" not in export.json()


def test_wrong_token_is_rejected(client, token):
    response = client.get(reverse("mailing:transfer_export"))
    assert response.status_code == 401
    denied = client.get(reverse("mailing:transfer_export"), HTTP_AUTHORIZATION="Bearer no")
    assert denied.status_code == 401


def test_wrong_method_is_rejected(client, token):
    export = client.post(reverse("mailing:transfer_export"), **_auth())
    load = client.get(reverse("mailing:transfer_load"), **_auth())
    assert export.status_code == 405
    assert load.status_code == 405


def test_invalid_page_and_resource_are_rejected(client, token):
    bad_page = _export(client, "organizations", offset="-1")
    bad_limit = _export(client, "organizations", limit="0")
    unknown = _export(client, "campaigns")
    assert bad_page.status_code == 400
    assert bad_limit.status_code == 400
    assert unknown.status_code == 400
    oversized = _load(client, {"version": 1, "resource": "organizations", "rows": [{}] * (MAX_PAGE + 1)})
    assert oversized.status_code == 400


def test_manifest_lists_every_resource_in_load_order(client, token):
    Organization.objects.create(name="PocketShell", slug="pocketshell")
    body = _export(client).json()
    assert body["version"] == 1
    assert [item["name"] for item in body["resources"]] == list(RESOURCE_ORDER)
    counts = {item["name"]: item["count"] for item in body["resources"]}
    assert counts["organizations"] == 1
    assert counts["contacts"] == 0


def test_round_trip_replays_by_natural_key_without_duplicating(client, token):
    organization = Organization.objects.create(name="PocketShell", slug="pocketshell")
    audience = Audience.objects.create(organization=organization, name="List", slug="list")
    record = Client.objects.create(
        organization=organization,
        name="PocketShell",
        slug="pocketshell",
        cmp_webhook_token="hook-secret",
        sender_emails=[{"id": "hello", "email": "hello@pocketshell.io"}],
        is_active=True,
    )
    api_key = ClientApiKey.objects.create(
        client=record,
        name="site",
        public_id="pub123",
        key_hash="hash-value",
    )
    contact = Contact.objects.create(email="Person@example.com", verified_at=CREATED)
    tag = Tag.objects.create(audience=audience, name="Early", slug="early")
    ContactTag.objects.create(contact=contact, tag=tag)
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=record,
        status=SubscriptionStatus.SUBSCRIBED,
        verified_at=CREATED,
    )
    CategoryPreference.objects.create(
        contact=contact,
        audience=audience,
        client=record,
        tag="newsletter",
        label="Newsletter",
        enabled=True,
    )
    ContactSourceMetadata.objects.create(
        contact=contact,
        audience=audience,
        client=record,
        source="landing",
        external_id="form-1",
        metadata={"path": "/"},
    )
    template = EmailTemplate.objects.create(
        client=record,
        key="confirm-signup",
        name="Confirm",
        subject="Confirm your email",
        text_body="Click",
    )
    blank = TransactionalMessage.objects.create(
        client=record,
        contact=contact,
        email=contact.normalized_email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="",
        subject="Confirm your email",
        sent_at=CREATED,
    )
    keyed = TransactionalMessage.objects.create(
        client=record,
        contact=contact,
        email=contact.normalized_email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="signup-1",
        subject="Confirm your email",
    )
    linked = EmailEvent.objects.create(
        client=record,
        audience=audience,
        contact=contact,
        transactional_message=keyed,
        event_type=EmailEventType.DELIVERED,
        provider_event_id="ses-1",
        metadata={"source": "sandbox"},
    )
    EmailEvent.objects.create(event_type=EmailEventType.OPEN, provider_event_id="")
    for model, pk in (
        (Organization, organization.pk),
        (Audience, audience.pk),
        (Client, record.pk),
        (ClientApiKey, api_key.pk),
        (Contact, contact.pk),
        (EmailTemplate, template.pk),
        (TransactionalMessage, blank.pk),
        (TransactionalMessage, keyed.pk),
        (EmailEvent, linked.pk),
    ):
        _stamp(model, pk)

    pages = {}
    for name in RESOURCE_ORDER:
        response = _export(client, name, limit="500")
        assert response.status_code == 200
        pages[name] = response.json()
        assert pages[name]["total"] == len(pages[name]["rows"])

    blank_row = next(row for row in pages["transactional_messages"]["rows"] if row["source_id"] == blank.pk)
    assert blank_row["idempotency_key"] == ""
    linked_row = next(row for row in pages["email_events"]["rows"] if row["provider_event_id"] == "ses-1")
    assert linked_row["transactional_idempotency_key"] == "signup-1"
    orphan_row = next(row for row in pages["email_events"]["rows"] if row["provider_event_id"] != "ses-1")
    assert orphan_row["provider_event_id"] == f"transfer:event:{orphan_row['source_id']}"
    assert pages["clients"]["rows"][0]["cmp_webhook_token"] == "hook-secret"
    assert pages["client_api_keys"]["rows"][0]["key_hash"] == "hash-value"

    EmailEvent.objects.all().delete()
    TransactionalMessage.objects.all().delete()
    EmailTemplate.objects.all().delete()
    CategoryPreference.objects.all().delete()
    ContactSourceMetadata.objects.all().delete()
    Subscription.objects.all().delete()
    ContactTag.objects.all().delete()
    Contact.objects.all().delete()
    ClientApiKey.objects.all().delete()
    Tag.objects.all().delete()
    Client.objects.all().delete()
    Audience.objects.all().delete()
    Organization.objects.all().delete()

    for name in RESOURCE_ORDER:
        loaded = _load(client, {"version": 1, "resource": name, "rows": pages[name]["rows"]})
        assert loaded.status_code == 200, loaded.content
        assert loaded.json()["upserted"] == len(pages[name]["rows"])

    assert Organization.objects.get(slug="pocketshell").name == "PocketShell"
    restored_client = Client.objects.get(slug="pocketshell")
    assert restored_client.cmp_webhook_token == "hook-secret"
    assert restored_client.sender_emails == [{"id": "hello", "email": "hello@pocketshell.io"}]
    assert ClientApiKey.objects.get(public_id="pub123").key_hash == "hash-value"
    restored_contact = Contact.objects.get(normalized_email="person@example.com")
    assert restored_contact.verified_at == CREATED
    assert restored_contact.created_at == CREATED
    subscription = Subscription.objects.get(contact=restored_contact)
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.verified_at == CREATED
    assert CategoryPreference.objects.get(contact=restored_contact, tag="newsletter").enabled is True
    assert ContactSourceMetadata.objects.get(source="landing").metadata == {"path": "/"}
    assert ContactTag.objects.filter(contact=restored_contact, tag__slug="early").exists()
    restored_blank = TransactionalMessage.objects.get(idempotency_key=f"transfer:{blank.pk}")
    assert restored_blank.status == TransactionalMessageStatus.SENT
    assert restored_blank.sent_at == CREATED
    restored_keyed = TransactionalMessage.objects.get(idempotency_key="signup-1")
    restored_event = EmailEvent.objects.get(provider_event_id="ses-1")
    assert restored_event.transactional_message_id == restored_keyed.pk
    assert restored_event.created_at == CREATED
    assert EmailEvent.objects.filter(provider_event_id=orphan_row["provider_event_id"]).count() == 1

    renamed = dict(pages["organizations"]["rows"][0])
    renamed["name"] = "PocketShell list"
    again = _load(client, {"version": 1, "resource": "organizations", "rows": [renamed]})
    assert again.status_code == 200
    for name in RESOURCE_ORDER:
        repeated = _load(client, {"version": 1, "resource": name, "rows": pages[name]["rows"]})
        assert repeated.status_code == 200
    assert Organization.objects.count() == 1
    assert Organization.objects.get(slug="pocketshell").name == "PocketShell"
    assert Client.objects.count() == 1
    assert Contact.objects.count() == 1
    assert Subscription.objects.count() == 1
    assert TransactionalMessage.objects.count() == 2
    assert EmailEvent.objects.count() == 2


def test_export_pages_past_the_first_window(client, token):
    for slug in ("a", "b", "c"):
        Organization.objects.create(name=slug, slug=slug)
    first = _export(client, "organizations", offset="0", limit="2")
    second = _export(client, "organizations", offset="2", limit="2")
    assert [row["slug"] for row in first.json()["rows"]] == ["a", "b"]
    assert [row["slug"] for row in second.json()["rows"]] == ["c"]
    assert first.json()["total"] == 3


def test_load_rejects_a_bad_document(client, token):
    broken = client.post(
        reverse("mailing:transfer_load"),
        data="{",
        content_type="application/json",
        **_auth(),
    )
    version = _load(client, {"version": 2, "resource": "organizations", "rows": []})
    missing = _load(client, {"version": 1, "resource": "organizations", "rows": [{"slug": "x"}]})
    assert broken.status_code == 400
    assert version.status_code == 400
    assert missing.status_code == 400
    assert Organization.objects.count() == 0


def test_loaded_timestamps_keep_their_offset(client, token):
    Organization.objects.create(name="PocketShell", slug="pocketshell")
    _stamp(Organization, Organization.objects.get().pk)
    page = _export(client, "organizations").json()
    Organization.objects.all().delete()
    assert _load(client, page).status_code == 200
    assert parse_datetime(page["rows"][0]["created_at"]) == CREATED
    assert Organization.objects.get().created_at == CREATED
