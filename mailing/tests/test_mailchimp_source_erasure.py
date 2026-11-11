import json
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    OperatorAudit,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import mailchimp_source_identity
from mailing.services.mailchimp_source_erasure import MailchimpSourceErasureError, erase_mailchimp_source_clicks
from mailing.services.mailchimp_source_identity import (
    ERASURE_ACTION,
    ERASURE_TARGET,
    erased_source_keys,
    lock_source_history_scope,
    source_identity_key,
)

pytestmark = pytest.mark.django_db
SOURCE = {"datacenter": "us19", "list_id": "source-list-one", "email_id": "source-member-one"}


@pytest.fixture
def scope():
    organization = Organization.objects.create(name="Erasure", slug="erasure")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    contact = Contact.objects.create(email="one@example.org")
    Subscription.objects.create(client=client, audience=audience, contact=contact,
                                status=SubscriptionStatus.UNSUBSCRIBED)
    return {"client": client, "audience": audience, "contact": contact}


def event(scope, identity=None, **kwargs):
    defaults = {
        "client": scope["client"], "audience": scope["audience"], "contact": scope["contact"],
        "event_type": EmailEventType.CLICK, "url": "https://example.org/private?email=one@example.org",
        "provider_event_id": f"mailchimp:click:{EmailEvent.objects.count()}",
        "metadata": {"provider": "mailchimp", "historical_import": True,
                     "mailchimp": {**(identity or SOURCE), "raw_file": "raw/private.json", "timestamp": "2026-11-11T12:00:00Z"}},
    }
    defaults.update(kwargs)
    return EmailEvent.objects.create(**defaults)


def erase(scope, **kwargs):
    return erase_mailchimp_source_clicks(client=scope["client"], audience=scope["audience"], **SOURCE, **kwargs)


def test_default_dry_run_does_not_write(scope):
    selected = event(scope)
    before = EmailEvent.objects.values().get(pk=selected.pk)
    report = erase(scope)
    assert report == {"dry_run": True, "matched": 1, "erased": 0, "tombstoned": False}
    assert EmailEvent.objects.values().get(pk=selected.pk) == before
    assert not OperatorAudit.objects.exists()


def test_apply_erases_only_exact_source_historical_click_and_scope(scope):
    selected = event(scope)
    retained = []
    for field in SOURCE:
        retained.append(event(scope, {**SOURCE, field: "us20" if field == "datacenter" else "different-safe-id"}))
    retained.append(event(scope, event_type=EmailEventType.OPEN))
    retained.append(event(scope, metadata={"provider": "ses", "historical_import": True, "mailchimp": SOURCE}))
    retained.append(event(scope, metadata={"provider": "mailchimp", "historical_import": False, "mailchimp": SOURCE}))
    other_client = Client.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    other_audience = Audience.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    retained.append(event(scope, client=other_client))
    retained.append(event(scope, audience=other_audience))
    before = {row["id"]: row for row in EmailEvent.objects.filter(pk__in=[value.pk for value in retained]).values()}
    provider_id, created_at = selected.provider_event_id, selected.created_at
    report = erase(scope, apply=True)
    selected.refresh_from_db()
    assert report == {"dry_run": False, "matched": 1, "erased": 1, "tombstoned": True}
    assert selected.url == "" and selected.contact_id is None
    assert selected.provider_event_id == provider_id and selected.created_at == created_at
    assert selected.metadata["erased"] is True
    assert selected.metadata["source_history_erased"] is True
    assert "mailchimp" not in selected.metadata
    assert "raw_file" not in json.dumps(selected.metadata)
    assert {row["id"]: row for row in EmailEvent.objects.filter(pk__in=before).values()} == before
    assert erased_source_keys(other_client, scope["audience"]) == set()
    assert erased_source_keys(scope["client"], other_audience) == set()


def test_contact_consent_suppression_campaigns_and_recipients_are_unchanged(scope):
    event(scope)
    Contact.objects.filter(pk=scope["contact"].pk).update(
        hard_bounced_at=datetime(2026, 10, 1, tzinfo=UTC),
        complained_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    campaign = Campaign.objects.create(client=scope["client"], audience=scope["audience"], subject="Keep intact")
    CampaignRecipient.objects.create(campaign=campaign, contact=scope["contact"], email="one@example.org")
    models = (Contact, Subscription, Campaign, CampaignRecipient)
    before = {model: list(model.objects.values()) for model in models}
    erase(scope, apply=True)
    assert {model: list(model.objects.values()) for model in models} == before


def test_tombstone_retains_only_opaque_scoped_identity_and_prevents_reimport(scope):
    event(scope)
    erase(scope, apply=True)
    audit = OperatorAudit.objects.get()
    expected_hash = source_identity_key(**SOURCE)
    assert audit.action == ERASURE_ACTION and audit.target_type == ERASURE_TARGET and audit.target_id == 0
    assert audit.metadata == {"client_id": scope["client"].pk, "audience_id": scope["audience"].pk,
                              "source_key_hash": expected_hash}
    assert erased_source_keys(scope["client"], scope["audience"]) == {expected_hash}
    serialized = json.dumps(audit.metadata)
    assert all(value not in serialized for value in SOURCE.values())
    assert "one@example.org" not in serialized
    # Import uses this exact key membership to skip even newly discovered clicks.
    assert source_identity_key(**SOURCE) in erased_source_keys(scope["client"], scope["audience"])
    assert source_identity_key(**{**SOURCE, "email_id": "other-member"}) not in erased_source_keys(
        scope["client"], scope["audience"],
    )


def test_erasure_is_idempotent_and_can_tombstone_a_source_with_no_events(scope):
    first = erase(scope, apply=True)
    second = erase(scope, apply=True)
    assert first == second == {"dry_run": False, "matched": 0, "erased": 0, "tombstoned": True}
    assert OperatorAudit.objects.count() == 1
    assert not EmailEvent.objects.exists()


@pytest.mark.parametrize("value", [None, "", [], {}, 123, "unsafe/id", "has space", "x" * 256])
def test_identity_fields_reject_unbounded_or_nonscalar_values(value):
    with pytest.raises(ValueError, match="bounded"):
        source_identity_key("us19", "list-one", value)


def test_identity_hash_is_stable_namespace_sensitive_and_accepts_fixture_ids():
    value = source_identity_key("us19", "test-list", "test-member")
    assert len(value) == 64 and value == source_identity_key("us19", "test-list", "test-member")
    assert value != source_identity_key("us20", "test-list", "test-member")


def test_cross_organization_scope_is_rejected(scope):
    organization = Organization.objects.create(name="Other", slug="different")
    audience = Audience.objects.create(organization=organization, name="Other", slug="other")
    with pytest.raises(ValueError, match="scope"):
        erase_mailchimp_source_clicks(client=scope["client"], audience=audience, **SOURCE, apply=True)
    assert not OperatorAudit.objects.exists()


def test_command_defaults_to_dry_run_and_console_contains_counts_only(scope):
    selected = event(scope)
    output = StringIO()
    options = {"organization": "erasure", "client": "client", "audience": "news", **SOURCE}
    call_command("erase_mailchimp_source_clicks", **options, stdout=output)
    assert json.loads(output.getvalue()) == {"dry_run": True, "matched": 1, "erased": 0, "tombstoned": False}
    assert all(value not in output.getvalue() for value in SOURCE.values())
    assert not OperatorAudit.objects.exists()
    output = StringIO()
    call_command("erase_mailchimp_source_clicks", **options, apply=True, stdout=output)
    assert json.loads(output.getvalue())["erased"] == 1
    selected.refresh_from_db()
    assert selected.contact_id is None


def test_command_rejects_unknown_scope_without_exposing_source(scope):
    with pytest.raises(CommandError, match="Invalid Mailchimp source identity or Relay scope"):
        call_command("erase_mailchimp_source_clicks", organization="missing", client="client", audience="news", **SOURCE)


def test_history_lock_uses_only_namespaced_transaction_advisory_sql(monkeypatch):
    database = MagicMock(vendor="postgresql", in_atomic_block=True)
    monkeypatch.setattr(mailchimp_source_identity, "connection", database)
    client = SimpleNamespace(pk=7, organization_id=3)
    audience = SimpleNamespace(pk=11, organization_id=3)
    lock_source_history_scope(client, audience)
    cursor = database.cursor.return_value.__enter__.return_value
    sql, params = cursor.execute.call_args.args
    assert sql == "SELECT pg_advisory_xact_lock(%s)"
    assert len(params) == 1 and -(2**63) <= params[0] < 2**63
    # Fixed namespace contract shared by import and erasure across releases.
    assert params[0] == 5052483187076975535
    first = params[0]
    lock_source_history_scope(client, audience)
    assert cursor.execute.call_args.args[1] == [first]
    lock_source_history_scope(client, SimpleNamespace(pk=12, organization_id=3))
    assert cursor.execute.call_args.args[1] != [first]
    lock_source_history_scope(SimpleNamespace(pk=8, organization_id=3), audience)
    assert cursor.execute.call_args.args[1] != [first]


def test_history_lock_requires_atomic_scope_even_on_sqlite(monkeypatch, scope):
    database = MagicMock(vendor="sqlite", in_atomic_block=False)
    monkeypatch.setattr(mailchimp_source_identity, "connection", database)
    with pytest.raises(ValueError, match="atomic"):
        lock_source_history_scope(scope["client"], scope["audience"])
    database.cursor.assert_not_called()
    database.in_atomic_block = True
    lock_source_history_scope(scope["client"], scope["audience"])
    database.cursor.assert_not_called()


def test_history_lock_validates_scope_before_executing_sql(monkeypatch):
    database = MagicMock(vendor="postgresql", in_atomic_block=True)
    monkeypatch.setattr(mailchimp_source_identity, "connection", database)
    with pytest.raises(ValueError, match="scope"):
        lock_source_history_scope(SimpleNamespace(pk=7, organization_id=3),
                                  SimpleNamespace(pk=11, organization_id=4))
    database.cursor.assert_not_called()


def test_erasure_apply_uses_shared_advisory_lock_but_dry_run_does_not(monkeypatch, scope):
    lock = MagicMock()
    monkeypatch.setattr("mailing.services.mailchimp_source_erasure.lock_source_history_scope", lock)
    erase(scope)
    lock.assert_not_called()
    erase(scope, apply=True)
    lock.assert_called_once_with(scope["client"], scope["audience"])


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("source_dc", ["absent", None, "", "not-a-dc", 123])
def test_ambiguous_legacy_source_refuses_without_mutating_any_rows_or_tombstoning(scope, source_dc, linked, apply):
    legacy = event(scope, contact=scope["contact"] if linked else None)
    metadata = legacy.metadata
    if source_dc == "absent":
        metadata["mailchimp"].pop("datacenter")
    else:
        metadata["mailchimp"]["datacenter"] = source_dc
    legacy.metadata = metadata
    legacy.save(update_fields=["metadata"])
    event(scope)  # A valid exact-source event must also remain untouched on refusal.
    before = list(EmailEvent.objects.order_by("pk").values())
    with pytest.raises(MailchimpSourceErasureError, match="verified legacy source reconciliation is required"):
        erase(scope, apply=apply)
    assert list(EmailEvent.objects.order_by("pk").values()) == before
    assert not OperatorAudit.objects.exists()
    assert erased_source_keys(scope["client"], scope["audience"]) == set()


@pytest.mark.parametrize("datacenter", [None, "", "test-dc", "US19", "us", "us19/other", "us" + "1" * 31])
def test_datacenter_must_match_verified_archive_account_format(datacenter):
    with pytest.raises(ValueError, match="valid account datacenter"):
        source_identity_key(datacenter, "safe-test-list", "safe-test-member")


def test_ambiguous_legacy_rows_in_another_scope_do_not_block_exact_source_erasure(scope):
    other = Client.objects.create(organization=scope["client"].organization, name="Other", slug="legacy-other")
    unrelated = event(scope, client=other, metadata={"provider": "mailchimp", "historical_import": True,
                                                  "mailchimp": {"list_id": SOURCE["list_id"], "email_id": SOURCE["email_id"]}})
    selected = event(scope)
    assert erase(scope, apply=True)["erased"] == 1
    unrelated.refresh_from_db()
    selected.refresh_from_db()
    assert unrelated.url and unrelated.contact_id == scope["contact"].pk
    assert selected.url == "" and selected.contact_id is None
