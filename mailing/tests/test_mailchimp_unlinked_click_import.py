import json
from datetime import datetime

import pytest
from django_tasks_db.models import DBTaskResult

from mailing.models import Audience, Campaign, CampaignStatus, Client, Contact, EmailEvent, OperatorAudit, Organization
from mailing.services.mailchimp_click_import import ClickImportError, import_mailchimp_clicks
from mailing.services.mailchimp_source_identity import source_identity_key
from mailing.tests.test_mailchimp_click_import import END, START, URL, archive

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope(tmp_path):
    organization = Organization.objects.create(name="Test", slug="test")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="App", slug="app")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="History", status=CampaignStatus.SENT)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"mc-campaign": campaign.pk}))
    return {"client": client, "audience": audience, "campaign": campaign,
            "mapping": mapping, "root": tmp_path / "archive"}


def run(scope, **kwargs):
    return import_mailchimp_clicks(archive_path=scope["root"], campaign_map=scope["mapping"], organization="test",
                                   client_slug="app", audience_slug="news", list_id="list-one", apply=True, **kwargs)


def test_default_unlinked_import_preserves_every_click_and_does_not_create_people_or_tasks(scope):
    rows = archive(scope, email="missing@example.com")
    before_campaign = Campaign.objects.values().get(pk=scope["campaign"].pk)
    before_tasks = DBTaskResult.objects.count()
    report = run(scope)
    assert report["unmatched_policy"] == "unlinked"
    assert report["counts"]["created"] == report["counts"]["unlinked_events"] == 3
    assert report["counts"]["unlinked_source_identities"] == 1
    assert report["counts"]["matched_events"] == 0
    assert not Contact.objects.exists()
    assert not scope["campaign"].recipients.exists()
    assert DBTaskResult.objects.count() == before_tasks
    assert Campaign.objects.values().get(pk=scope["campaign"].pk) == before_campaign
    events = {event.provider_event_id: event for event in EmailEvent.objects.all()}
    assert len(events) == 3
    for row in rows:
        event = events[row["source_event_id"]]
        assert event.contact_id is None
        assert event.url == URL
        assert event.created_at == datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        assert event.metadata["mailchimp"]["datacenter"] == "us19"
        assert event.metadata["mailchimp"]["identity_status"] == "unlinked"
        assert "missing@example.com" not in json.dumps(event.metadata)
    assert run(scope)["counts"]["existing"] == 3


def test_explicit_archive_policy_keeps_unmatched_history_archived(scope):
    archive(scope, email="missing@example.com")
    report = run(scope, unmatched_policy="archive")
    assert report["counts"]["unmatched"] == 3
    assert not EmailEvent.objects.exists()
    assert not Contact.objects.exists()


def test_archive_policy_verifies_existing_unlinked_clicks_and_archives_only_new_ids(scope):
    archive(scope, email="missing@example.com")
    run(scope)
    before = list(EmailEvent.objects.order_by("pk").values())
    archive(scope, email="missing@example.com", timestamps=(START, START, END, END))
    report = run(scope, unmatched_policy="archive")
    assert report["counts"]["existing"] == 3
    assert report["counts"]["unmatched"] == 1
    assert report["counts"]["unlinked_events"] == 3
    assert report["counts"]["unlinked_source_identities"] == 1
    assert report["reconciliation"]["by_contact"]["list-one:member-one"] == {
        "source": 4, "existing": 3, "unmatched": 1,
    }
    assert list(EmailEvent.objects.order_by("pk").values()) == before
    assert not Contact.objects.exists()


def test_archive_policy_does_not_skip_conflict_validation_of_existing_unlinked_event(scope):
    archive(scope, email="missing@example.com")
    run(scope)
    EmailEvent.objects.filter(pk=EmailEvent.objects.first().pk).update(url="https://conflicting.example/")
    with pytest.raises(ClickImportError, match="conflicts"):
        run(scope, unmatched_policy="archive")
    assert EmailEvent.objects.count() == 3
    assert not Contact.objects.exists()


def test_later_email_match_does_not_link_originally_unlinked_identity_even_for_new_click_ids(scope):
    archive(scope, email="missing@example.com")
    run(scope)
    Contact.objects.create(email="missing@example.com")
    archive(scope, email="missing@example.com", timestamps=(START, START, END, END))
    report = run(scope)
    assert report["counts"]["created"] == 1
    assert report["counts"]["existing"] == 3
    assert report["counts"]["unlinked_events"] == 4
    assert report["missing_contacts"] == []
    assert not EmailEvent.objects.filter(contact__isnull=False).exists()


def test_later_address_change_keeps_original_saved_person_including_new_clicks(scope):
    original = Contact.objects.create(email="One@example.com")
    archive(scope)
    run(scope)
    original.email = "renamed@example.com"
    original.save()
    Contact.objects.create(email="One@example.com")
    archive(scope, timestamps=(START, START, END, END))
    report = run(scope)
    assert report["counts"]["matched_events"] == 4
    assert report["counts"]["matched_source_identities"] == 1
    assert set(EmailEvent.objects.values_list("contact_id", flat=True)) == {original.pk}


def test_conflicting_saved_people_for_same_source_identity_abort_before_writes(scope):
    Contact.objects.create(email="One@example.com")
    archive(scope)
    run(scope)
    other = Contact.objects.create(email="other@example.com")
    event = EmailEvent.objects.first()
    EmailEvent.objects.filter(pk=event.pk).update(contact=other)
    archive(scope, timestamps=(START, START, END, END))
    with pytest.raises(ClickImportError, match="conflicting Relay contact linkage"):
        run(scope)
    assert EmailEvent.objects.count() == 3


def test_mixed_linked_and_unlinked_saved_decisions_abort(scope):
    Contact.objects.create(email="One@example.com")
    archive(scope)
    run(scope)
    event = EmailEvent.objects.first()
    event.contact = None
    event.metadata["mailchimp"]["identity_status"] = "unlinked"
    event.save(update_fields=["contact", "metadata"])
    with pytest.raises(ClickImportError, match="conflicting Relay contact linkage"):
        run(scope)
    assert EmailEvent.objects.count() == 3


def test_tombstoned_source_skipped_for_all_current_and_new_events_without_restoration(scope):
    archive(scope)
    run(scope)
    key = source_identity_key("us19", "list-one", "member-one")
    OperatorAudit.objects.create(action="mailchimp.source_history.erased", target_type="mailchimp_source_identity",
                                  target_id=0, metadata={"client_id": scope["client"].pk,
                                                         "audience_id": scope["audience"].pk,
                                                         "source_key_hash": key})
    erased_metadata = {"provider": "mailchimp", "historical_import": True, "erased": True,
                       "source_history_erased": True, "mailchimp_source_key_hash": key}
    EmailEvent.objects.update(contact=None, url="", metadata=erased_metadata)
    archive(scope, timestamps=(START, START, END, END))
    report = run(scope)
    assert report["counts"]["erased_source"] == 4
    assert report["counts"]["erased_source_identities"] == 1
    assert EmailEvent.objects.count() == 3
    assert all(event.metadata == erased_metadata and event.url == "" for event in EmailEvent.objects.all())


def test_erased_legacy_metadata_fails_closed_and_is_never_restored(scope):
    archive(scope)
    run(scope)
    EmailEvent.objects.update(metadata={"erased": True})
    with pytest.raises(ClickImportError, match="verifiable source tombstone"):
        run(scope)
    assert all(event.metadata == {"erased": True} for event in EmailEvent.objects.all())


def test_legacy_linked_provenance_keeps_contact_without_rewriting_metadata(scope):
    original = Contact.objects.create(email="One@example.com")
    archive(scope)
    run(scope)
    for event in EmailEvent.objects.all():
        event.metadata["mailchimp"].pop("datacenter")
        event.metadata["mailchimp"].pop("identity_status")
        event.save(update_fields=["metadata"])
    before = list(EmailEvent.objects.order_by("pk").values_list("metadata", flat=True))
    original.email = "renamed@example.com"
    original.save()
    assert run(scope)["counts"]["existing"] == 3
    assert list(EmailEvent.objects.order_by("pk").values_list("metadata", flat=True)) == before


def test_anonymized_contact_never_becomes_a_new_source_identity_decision(scope):
    Contact.objects.create(email="old@erased.invalid")
    archive(scope, email="old@erased.invalid")
    with pytest.raises(ClickImportError, match="anonymized"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_and_eraser_scope_lock_precedes_identity_preload(scope, monkeypatch):
    from mailing.services import mailchimp_click_import  # noqa: PLC0415

    archive(scope)
    original_lock = mailchimp_click_import.lock_source_history_scope
    original_keys = mailchimp_click_import.erased_source_keys
    calls = []

    def lock(client, audience):
        calls.append("scope_lock")
        return original_lock(client, audience)

    def keys(client, audience):
        calls.append("tombstones")
        return original_keys(client, audience)

    monkeypatch.setattr(mailchimp_click_import, "lock_source_history_scope", lock)
    monkeypatch.setattr(mailchimp_click_import, "erased_source_keys", keys)
    run(scope)
    assert calls == ["scope_lock", "tombstones"]
