import json

import pytest

from mailing.models import Audience, Campaign, CampaignStatus, Client, Contact, EmailEvent, Organization
from mailing.services.mailchimp_click_import import ClickImportError, import_mailchimp_clicks
from mailing.tests.test_mailchimp_click_import import archive

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope(tmp_path):
    organization = Organization.objects.create(name="Test", slug="test")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="App", slug="app")
    target = Campaign.objects.create(client=client, audience=audience, subject="Existing", status=CampaignStatus.SENT)
    Contact.objects.create(email="One@example.com")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"mc-campaign": target.pk}))
    result = {"client": client, "audience": audience, "campaign": target, "mapping": mapping,
              "root": tmp_path / "archive"}
    archive(result)
    return result


def run(scope):
    return import_mailchimp_clicks(archive_path=scope["root"], campaign_map=scope["mapping"],
                                   organization="test", client_slug="app", audience_slug="news",
                                   list_id="list-one", apply=True)


def alias(scope, **kwargs):
    return Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                   status=CampaignStatus.SENT, subject="Source alias",
                                   external_key="mailchimp-us19-mc-campaign", **kwargs)


@pytest.mark.parametrize("status", [CampaignStatus.DRAFT, CampaignStatus.QUEUED, CampaignStatus.SENDING,
                                    CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED, CampaignStatus.FAILED])
def test_importer_rejects_non_sent_mapping_before_events(scope, status):
    Campaign.objects.filter(pk=scope["campaign"].pk).update(status=status)
    with pytest.raises(ClickImportError, match="already-sent"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_alias_target_without_following_it(scope):
    canonical = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                        status=CampaignStatus.SENT, subject="Canonical")
    Campaign.objects.filter(pk=scope["campaign"].pk).update(merged_into=canonical)
    with pytest.raises(ClickImportError, match="merged campaign alias"):
        run(scope)
    assert not EmailEvent.objects.exists()


@pytest.mark.parametrize("changes", [
    {"external_key": "mailchimp-us19-other"},
    {"external_key": "mailchimp-us20-mc-campaign"},
    {"metadata": {"mailchimp": {"campaign_id": "other"}}},
    {"metadata": {"mailchimp": {"datacenter": "us20"}}},
    {"metadata": {"mailchimp": {"list_id": "other-list"}}},
])
def test_importer_rejects_conflicting_target_source_without_alias_proof(scope, changes):
    Campaign.objects.filter(pk=scope["campaign"].pk).update(**changes)
    with pytest.raises(ClickImportError, match="source provenance conflicts"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_allows_explicit_ordinary_sent_target_without_claims(scope):
    assert run(scope)["counts"]["created"] == 3
    assert set(EmailEvent.objects.values_list("campaign_id", flat=True)) == {scope["campaign"].pk}


def test_importer_allows_direct_matching_source_provenance(scope):
    Campaign.objects.filter(pk=scope["campaign"].pk).update(
        external_key="mailchimp-us19-mc-campaign",
        metadata={"mailchimp": {"campaign_id": "mc-campaign", "datacenter": "us19", "list_id": "list-one"}},
    )
    assert run(scope)["counts"]["created"] == 3


def test_importer_accepts_different_source_canonical_only_through_verified_alias_chain(scope):
    canonical = scope["campaign"]
    Campaign.objects.filter(pk=canonical.pk).update(external_key="mailchimp-us19-other",
                                                  metadata={"mailchimp": {"campaign_id": "other"}})
    intermediate = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                            status=CampaignStatus.SENT, subject="Intermediate", merged_into=canonical)
    source_alias = alias(scope, merged_into=intermediate)
    before = list(Campaign.objects.order_by("pk").values())
    assert run(scope)["counts"]["created"] == 3
    assert set(EmailEvent.objects.values_list("campaign_id", flat=True)) == {canonical.pk}
    assert not EmailEvent.objects.filter(campaign=source_alias).exists()
    assert list(Campaign.objects.order_by("pk").values()) == before


def test_importer_alias_provenance_conflict_cannot_authorize_canonical(scope):
    alias(scope, merged_into=scope["campaign"], metadata={"mailchimp": {"list_id": "wrong"}})
    with pytest.raises(ClickImportError, match="Source alias provenance conflicts"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_unrelated_alias_canonical_even_with_ordinary_target(scope):
    other = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                     status=CampaignStatus.SENT, subject="Other canonical")
    alias(scope, merged_into=other)
    with pytest.raises(ClickImportError, match="unrelated"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_alias_chain_cycle(scope):
    intermediate = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                            status=CampaignStatus.SENT, subject="Intermediate")
    source = alias(scope, merged_into=intermediate)
    Campaign.objects.filter(pk=intermediate.pk).update(merged_into=source)
    with pytest.raises(ClickImportError, match="cycle"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_alias_chain_crossing_scope(scope):
    other = Audience.objects.create(organization=scope["client"].organization, slug="other", name="Other")
    intermediate = Campaign.objects.create(client=scope["client"], audience=other,
                                            status=CampaignStatus.SENT, subject="Intermediate", merged_into=scope["campaign"])
    alias(scope, merged_into=intermediate)
    with pytest.raises(ClickImportError, match="outside"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_non_sent_alias_chain_member(scope):
    alias(scope, merged_into=scope["campaign"], metadata={})
    Campaign.objects.filter(external_key="mailchimp-us19-mc-campaign").update(status=CampaignStatus.DRAFT)
    with pytest.raises(ClickImportError, match="already-sent"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_importer_rejects_unmerged_competing_exact_source_claim(scope):
    alias(scope)
    with pytest.raises(ClickImportError, match="Another unmerged"):
        run(scope)
    assert not EmailEvent.objects.exists()
