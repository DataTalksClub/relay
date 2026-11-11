import json
from io import StringIO

import pytest
from django.core.management import call_command

from mailing.models import Audience, Campaign, CampaignStatus, Client, Organization
from mailing.services.mailchimp_click_campaigns import prepare_mailchimp_click_campaigns
from mailing.services.mailchimp_click_import import ClickImportError, encoded
from mailing.tests.test_mailchimp_click_import import archive as write_click_archive
from mailing.tests.test_mailchimp_click_import import rewrite_artifact

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope(tmp_path):
    organization = Organization.objects.create(name="Test", slug="test")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="App", slug="app")
    result = {"root": tmp_path / "archive", "client": client, "audience": audience}
    write_click_archive(result)
    manifest_path = result["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["datacenter"] = "us19"
    manifest_path.write_bytes(encoded(manifest))
    report = {"id": "mc-campaign", "list_id": "list-one", "campaign_title": "Historical newsletter",
              "subject_line": "History subject", "send_time": "2025-10-01T09:00:00Z",
              "clicks": {"clicks_total": 99999}}
    rewrite_artifact(result, "campaigns.json", encoded([report]))
    result["mapping"] = tmp_path / "mapping.json"
    return result


def run(scope, **kwargs):
    return prepare_mailchimp_click_campaigns(archive_path=scope["root"], organization="test", client_slug="app",
                                            audience_slug="news", list_id="list-one", **kwargs)


def campaign(scope, **kwargs):
    return Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                   status=CampaignStatus.SENT, subject="Existing", **kwargs)


def test_default_plans_missing_campaign_without_creating(scope):
    mapping, report = run(scope)
    assert mapping == {}
    assert report["counts"] == {"missing": 1}
    assert report["campaigns"][0]["source_click_count"] == 3
    assert not Campaign.objects.exists()


def test_allow_create_dry_run_never_mutates(scope):
    mapping, report = run(scope, allow_create=True)
    assert mapping == {}
    assert report["counts"] == {"would_create": 1}
    assert not Campaign.objects.exists()


def test_apply_without_allow_create_never_creates(scope):
    mapping, report = run(scope, apply=True)
    assert mapping == {}
    assert report["counts"] == {"missing": 1}
    assert not Campaign.objects.exists()


def test_creation_is_inert_preserves_source_time_counts_and_rerun_identity(scope):
    mapping, report = run(scope, apply=True, allow_create=True)
    created = Campaign.objects.get(pk=mapping["mc-campaign"])
    assert report["counts"] == {"created": 1}
    assert created.external_key == "mailchimp-us19-mc-campaign"
    assert created.status == CampaignStatus.SENT
    assert created.subject == "History subject"
    assert created.sent_at.isoformat() == "2025-10-01T09:00:00+00:00"
    assert created.click_count == 3
    assert created.unique_click_count == 1
    assert created.sent_count == created.recipient_count == 0
    assert created.metadata["historical_import"] is True
    assert not created.recipients.exists()
    assert not created.scheduled_at
    assert not created.sender_id
    assert not created.html_body
    second_mapping, second_report = run(scope, apply=True, allow_create=True)
    assert second_mapping == mapping
    assert second_report["counts"] == {"existing": 1}
    assert Campaign.objects.count() == 1


def test_existing_exact_external_key_is_reused_without_field_changes(scope):
    existing = campaign(scope, external_key="mailchimp-us19-mc-campaign", metadata={"manual": "preserve"})
    before = Campaign.objects.values().get(pk=existing.pk)
    mapping, report = run(scope, apply=True, allow_create=True)
    assert mapping == {"mc-campaign": existing.pk}
    assert report["counts"] == {"existing": 1}
    assert Campaign.objects.values().get(pk=existing.pk) == before


def test_exact_metadata_provenance_is_reused(scope):
    existing = campaign(scope, metadata={"mailchimp": {"campaign_id": "mc-campaign", "datacenter": "us19",
                                                       "list_id": "list-one"}})
    assert run(scope)[0] == {"mc-campaign": existing.pk}


def test_ambiguous_source_mapping_fails_without_create(scope):
    campaign(scope, external_key="mailchimp-us19-mc-campaign")
    campaign(scope, metadata={"mailchimp": {"campaign_id": "mc-campaign", "datacenter": "us19",
                                           "list_id": "list-one"}})
    with pytest.raises(ClickImportError, match="More than one"):
        run(scope, apply=True, allow_create=True)
    assert Campaign.objects.count() == 2


def test_external_key_and_metadata_conflict_rejected(scope):
    campaign(scope, external_key="mailchimp-us19-mc-campaign", metadata={"mailchimp": {"list_id": "another"}})
    with pytest.raises(ClickImportError, match="conflicts"):
        run(scope, apply=True, allow_create=True)


def test_explicit_mapping_without_provenance_is_valid_and_never_changes_existing(scope):
    existing = campaign(scope, external_key="weekly-302")
    before = Campaign.objects.values().get(pk=existing.pk)
    scope["mapping"].write_text(json.dumps({"mc-campaign": existing.pk}))
    mapping, _ = run(scope, mapping_path=scope["mapping"], apply=True, allow_create=True)
    assert mapping == {"mc-campaign": existing.pk}
    assert Campaign.objects.values().get(pk=existing.pk) == before


def test_explicit_mapping_cannot_override_an_existing_exact_source_claim(scope):
    campaign(scope, external_key="mailchimp-us19-mc-campaign")
    other = campaign(scope, external_key="other")
    scope["mapping"].write_text(json.dumps({"mc-campaign": other.pk}))
    with pytest.raises(ClickImportError, match="conflicts"):
        run(scope, mapping_path=scope["mapping"])


def test_merged_alias_is_rejected_until_canonical_mapping_decided(scope):
    canonical = campaign(scope, external_key="canonical")
    campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=canonical)
    with pytest.raises(ClickImportError, match="merged alias"):
        run(scope, apply=True, allow_create=True)
    assert Campaign.objects.count() == 2


def test_explicit_merged_canonical_mapping_uses_chain_without_mutating_existing(scope):
    canonical = campaign(scope, external_key="canonical")
    intermediate = campaign(scope, external_key="intermediate", merged_into=canonical)
    campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=intermediate)
    before = list(Campaign.objects.order_by("pk").values())
    scope["mapping"].write_text(json.dumps({"mc-campaign": canonical.pk}))
    mapping, report = run(scope, mapping_path=scope["mapping"], apply=True, allow_create=True)
    assert mapping == {"mc-campaign": canonical.pk}
    assert report["counts"] == {"existing": 1}
    assert list(Campaign.objects.order_by("pk").values()) == before


def test_explicit_merged_alias_mapping_cannot_target_unrelated_campaign(scope):
    canonical = campaign(scope, external_key="canonical")
    unrelated = campaign(scope, external_key="unrelated")
    campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=canonical)
    scope["mapping"].write_text(json.dumps({"mc-campaign": unrelated.pk}))
    with pytest.raises(ClickImportError, match="verified merged canonical"):
        run(scope, mapping_path=scope["mapping"])


def test_explicit_merged_mapping_does_not_waive_alias_source_provenance(scope):
    canonical = campaign(scope, external_key="canonical")
    campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=canonical,
             metadata={"mailchimp": {"list_id": "wrong"}})
    scope["mapping"].write_text(json.dumps({"mc-campaign": canonical.pk}))
    with pytest.raises(ClickImportError, match="source provenance conflicts"):
        run(scope, mapping_path=scope["mapping"])


def test_explicit_merged_mapping_rejects_cycles(scope):
    first = campaign(scope, external_key="first")
    alias = campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=first)
    Campaign.objects.filter(pk=first.pk).update(merged_into=alias)
    scope["mapping"].write_text(json.dumps({"mc-campaign": first.pk}))
    with pytest.raises(ClickImportError, match="cycle"):
        run(scope, mapping_path=scope["mapping"])


def test_explicit_merged_chain_must_stay_in_scope(scope):
    other = Audience.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    canonical = Campaign.objects.create(client=scope["client"], audience=other, status=CampaignStatus.SENT,
                                         subject="Other")
    campaign(scope, external_key="mailchimp-us19-mc-campaign", merged_into=canonical)
    scope["mapping"].write_text(json.dumps({"mc-campaign": canonical.pk}))
    with pytest.raises(ClickImportError, match="outside"):
        run(scope, mapping_path=scope["mapping"])


@pytest.mark.parametrize("field,value", [("click_count", 7), ("unique_click_count", 2)])
def test_owned_history_rejects_incompatible_source_counts(scope, field, value):
    mapping, _ = run(scope, apply=True, allow_create=True)
    Campaign.objects.filter(pk=mapping["mc-campaign"]).update(**{field: value})
    with pytest.raises(ClickImportError, match="incompatible archive window or click counts"):
        run(scope, apply=True, allow_create=True)


def test_owned_history_rejects_different_archive_window(scope):
    run(scope, apply=True, allow_create=True)
    path = scope["root"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["window"]["end_inclusive"] = "2026-11-11T18:00:01Z"
    path.write_bytes(encoded(manifest))
    with pytest.raises(ClickImportError, match="incompatible archive window or click counts"):
        run(scope, apply=True, allow_create=True)


def test_owned_history_allows_same_window_counts_after_manifest_reverification(scope):
    mapping, _ = run(scope, apply=True, allow_create=True)
    path = scope["root"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["reverified_at"] = "2026-11-11T19:00:00Z"
    path.write_bytes(encoded(manifest))
    assert run(scope, apply=True, allow_create=True)[0] == mapping


def test_selected_list_without_clicks_creates_nothing(scope):
    mapping, report = prepare_mailchimp_click_campaigns(
        archive_path=scope["root"], organization="test", client_slug="app", audience_slug="news",
        list_id="other-list", apply=True, allow_create=True,
    )
    assert mapping == {}
    assert not report["campaigns"]
    assert not Campaign.objects.exists()


def test_explicit_mapping_cannot_cross_scope(scope):
    other_client = Client.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    existing = Campaign.objects.create(client=other_client, audience=scope["audience"], status=CampaignStatus.SENT,
                                       subject="Existing")
    scope["mapping"].write_text(json.dumps({"mc-campaign": existing.pk}))
    with pytest.raises(ClickImportError, match="outside"):
        run(scope, mapping_path=scope["mapping"])


def test_source_integrity_required_even_for_dry_run(scope):
    (scope["root"] / "raw/page.json").write_text("{}")
    with pytest.raises(ClickImportError, match="checksum"):
        run(scope)


def test_exact_key_in_another_audience_is_rejected_before_create(scope):
    other = Audience.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    Campaign.objects.create(client=scope["client"], audience=other, subject="Other", status=CampaignStatus.SENT,
                            external_key="mailchimp-us19-mc-campaign")
    with pytest.raises(ClickImportError, match="outside"):
        run(scope, apply=True, allow_create=True)
    assert Campaign.objects.count() == 1


def test_existing_unfinished_campaign_is_not_used_for_history(scope):
    existing = campaign(scope, external_key="mailchimp-us19-mc-campaign")
    Campaign.objects.filter(pk=existing.pk).update(status=CampaignStatus.QUEUED)
    with pytest.raises(ClickImportError, match="already-sent"):
        run(scope, apply=True, allow_create=True)


def test_command_outputs_importer_mapping_and_private_plan_with_counts_only_console(scope, tmp_path):
    mapping_path, report_path = tmp_path / "out.json", tmp_path / "plan.json"
    output = StringIO()
    call_command("prepare_mailchimp_click_campaigns", archive=str(scope["root"]), organization="test",
                 client="app", audience="news", list_id="list-one", mapping_out=str(mapping_path),
                 report=str(report_path), allow_create=True, apply=True, stdout=output)
    assert json.loads(mapping_path.read_text()) == {"mc-campaign": Campaign.objects.get().pk}
    assert json.loads(report_path.read_text())["campaigns"][0]["subject"] == "History subject"
    assert "History subject" not in output.getvalue()
    assert json.loads(output.getvalue())["counts"] == {"created": 1}
