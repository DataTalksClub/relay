import hashlib
import json
from datetime import datetime

import pytest

from mailing.models import Audience, Campaign, CampaignStatus, Client, Contact, EmailEvent, Organization
from mailing.services.mailchimp_click_campaigns import prepare_mailchimp_click_campaigns
from mailing.services.mailchimp_click_import import ClickImportError, VerifiedArchive, encoded, import_mailchimp_clicks
from scripts.mailchimp_click_archive import Archive

pytestmark = pytest.mark.django_db
START = "2025-11-10T23:00:00Z"
END = "2026-11-11T18:00:00Z"


class SourceAPI:
    def __init__(self, activities, children=("child", "second-child")):
        self.activities = activities
        self.children = children
        self.reports = [{"id": value, "type": "variate" if value == "parent" else "regular",
                         "send_time": START, "list_id": "list-one", "subject_line": value}
                        for value in ("parent", "child", "second-child", "independent")]

    def get(self, path, params):
        if path == "/ping":
            return {"health_status": "ok"}
        if path == "/reports":
            key, rows = "reports", self.reports
        elif path.endswith("sub-reports"):
            key, rows = "reports", [{"id": child, "type": "regular"} for child in self.children]
        else:
            campaign_id = path.split("/")[2]
            key = "emails"
            rows = [{"email_address": "one@example.com", "email_id": "one", "list_id": "list-one",
                     "activity": self.activities.get(campaign_id, [])}]
        offset, count = params["offset"], params["count"]
        return {key: rows[offset:offset + count], "total_items": len(rows)}


def click(at=START, url="https://example.com/a"):
    return {"action": "click", "timestamp": at, "url": url}


@pytest.fixture
def scope(tmp_path):
    organization = Organization.objects.create(name="Overlap", slug="overlap")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="App", slug="app")
    Contact.objects.create(email="one@example.com")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="Existing",
                                       status=CampaignStatus.SENT,
                                       sent_at=datetime.fromisoformat(START.replace("Z", "+00:00")))
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({source: campaign.pk for source in (
        "parent", "child", "second-child", "independent",
    )}))
    return {"root": tmp_path / "archive", "mapping": mapping}


def build(scope, activities, **kwargs):
    return Archive(scope["root"], SourceAPI(activities, **kwargs), datacenter="us19", end=END, page_size=1).run()


def run(scope):
    return import_mailchimp_clicks(archive_path=scope["root"], campaign_map=scope["mapping"],
                                   organization="overlap", client_slug="app", audience_slug="news",
                                   list_id="list-one", apply=True)


def rewrite_page(scope, predicate, transform):
    manifest_path = scope["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for page in manifest["pages"].values():
        if predicate(page):
            path = scope["root"] / page["file"]
            payload = json.loads(path.read_text())
            transform(page, payload)
            path.write_bytes(encoded(payload))
            page["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_bytes(encoded(manifest))


def test_repeated_parent_child_overlap_rolls_back_already_flushed_import_batch(scope):
    build(scope, {"parent": [click()] * 501, "child": [click()] * 2})
    with pytest.raises(ClickImportError, match="parent/child.*overlap"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_parent_child_overlap_rejected_independent_of_normalized_order(scope):
    build(scope, {"parent": [click()], "second-child": [click()]})
    path = scope["root"] / "clicks.jsonl"
    path.write_bytes(b"\n".join(reversed(path.read_bytes().splitlines())) + b"\n")
    manifest_path = scope["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["clicks.jsonl"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_bytes(encoded(manifest))
    with pytest.raises(ClickImportError, match="parent/child.*overlap"):
        run(scope)
    assert not EmailEvent.objects.exists()


def test_disjoint_parent_child_histories_are_imported(scope):
    build(scope, {"parent": [click()], "child": [click(url="https://example.com/child")]})
    assert run(scope)["counts"]["created"] == 2


def test_zero_parent_activity_and_identical_independent_campaign_events_are_valid(scope):
    build(scope, {"child": [click(), click()], "independent": [click(), click()]})
    assert run(scope)["counts"]["created"] == 4
    assert run(scope)["counts"]["existing"] == 4


def test_identical_sibling_campaign_events_are_not_parent_child_overlap(scope):
    build(scope, {"child": [click()], "second-child": [click()]})
    assert run(scope)["counts"]["created"] == 2


def test_preparer_also_refuses_overlap_before_creating_campaigns(scope):
    build(scope, {"parent": [click()], "child": [click()]})
    before = Campaign.objects.count()
    with pytest.raises(ClickImportError, match="parent/child.*overlap"):
        prepare_mailchimp_click_campaigns(archive_path=scope["root"], organization="overlap",
                                         client_slug="app", audience_slug="news", list_id="list-one",
                                         allow_create=True, apply=True)
    assert Campaign.objects.count() == before


def test_missing_inventoried_child_relationship_refused(scope):
    build(scope, {"parent": [click()]})
    rewrite_page(scope, lambda page: page["path"].endswith("sub-reports") and page["params"]["offset"] == 0,
                 lambda page, payload: payload["reports"][0].update(id="not-in-inventory"))
    with pytest.raises(ClickImportError, match="relationship.*inventory"):
        VerifiedArchive(scope["root"])


def test_missing_subreport_page_is_not_accepted_as_complete_relationship(scope):
    build(scope, {"parent": [click()]})
    manifest_path = scope["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["pages"] = {key: page for key, page in manifest["pages"].items()
                         if not (page["path"].endswith("sub-reports") and page["params"]["offset"] == 1)}
    manifest_path.write_bytes(encoded(manifest))
    with pytest.raises(ClickImportError, match="pagination.*incomplete"):
        VerifiedArchive(scope["root"])


def test_self_referential_child_is_rejected(scope):
    build(scope, {"parent": [click()]})
    rewrite_page(scope, lambda page: page["path"].endswith("sub-reports") and page["params"]["offset"] == 0,
                 lambda page, payload: payload["reports"][0].update(id="parent"))
    with pytest.raises(ClickImportError, match="relationship.*malformed"):
        VerifiedArchive(scope["root"])


def test_omitting_all_subreport_pages_is_rejected(scope):
    build(scope, {"parent": [click()]})
    manifest_path = scope["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["pages"] = {key: page for key, page in manifest["pages"].items()
                         if not page["path"].endswith("sub-reports")}
    manifest_path.write_bytes(encoded(manifest))
    with pytest.raises(ClickImportError, match="pagination.*missing"):
        VerifiedArchive(scope["root"])
