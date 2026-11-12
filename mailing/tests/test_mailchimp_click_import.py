import hashlib
import json
from datetime import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    Contact,
    ContactSourceMetadata,
    EmailEvent,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.mailchimp_click_import import ClickImportError, encoded, import_mailchimp_clicks
from mailing.services.operator_ui import contact_metrics

pytestmark = pytest.mark.django_db
START = "2025-11-10T23:00:00Z"
END = "2026-11-11T18:00:00Z"
URL = "https://example.com/lesson"


@pytest.fixture
def scope(tmp_path):
    organization = Organization.objects.create(name="Test", slug="test")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="App", slug="app")
    campaign = Campaign.objects.create(client=client, audience=audience, subject="History", status=CampaignStatus.SENT)
    contact = Contact.objects.create(email="One@example.com", hard_bounced_at=datetime.fromisoformat(START.replace("Z", "+00:00")))
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=SubscriptionStatus.UNSUBSCRIBED)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"mc-campaign": campaign.pk}), encoding="utf-8")
    return {"client": client, "audience": audience, "campaign": campaign, "contact": contact,
            "mapping": mapping, "root": tmp_path / "archive"}


def archive(scope, *, timestamps=(START, START, END), email="One@example.com", list_id="list-one"):
    root = scope["root"]
    (root / "raw").mkdir(parents=True, exist_ok=True)
    activity = [{"action": "click", "timestamp": at, "url": URL} for at in timestamps]
    member = {"email_address": email, "email_id": "member-one", "list_id": list_id, "activity": activity}
    raw = {"emails": [member], "total_items": 1}
    raw_file = "raw/page.json"
    (root / raw_file).write_bytes(encoded(raw))
    occurrences, rows = {}, []
    for index, at in enumerate(timestamps):
        occurrences[at] = occurrences.get(at, 0) + 1
        occurrence = occurrences[at]
        signature = ["mc-campaign", list_id, "member-one", at, URL, occurrence]
        rows.append({"source": "mailchimp", "source_event_id": "mailchimp:click:" + hashlib.sha256(encoded(signature)).hexdigest(),
                     "campaign_id": "mc-campaign", "list_id": list_id, "email_id": "member-one", "email_address": email,
                     "timestamp": at, "url": URL, "occurrence": occurrence, "raw_file": raw_file,
                     "raw_member_index": 0, "raw_activity_index": index})
    (root / "clicks.jsonl").write_bytes(b"".join(encoded(row) + b"\n" for row in rows))
    (root / "campaigns.json").write_bytes(encoded([{"id": "mc-campaign"}]))
    reconciliation = {"by_campaign": {"mc-campaign": len(rows)},
                      "by_contact": {list_id + ":member-one": len(rows)},
                      "by_link": {"mc-campaign:" + URL: len(rows)}}
    (root / "reconciliation.json").write_bytes(encoded(reconciliation))
    manifest = {"schema_version": 1, "source": "mailchimp", "datacenter": "us19", "status": "complete_available_api_data",
                "window": {"start_inclusive": START, "end_inclusive": END, "timezone": "Europe/Berlin"},
                "pages": {"page": {"path": "/reports/mc-campaign/email-activity", "file": raw_file,
                                   "sha256": hashlib.sha256(encoded(raw)).hexdigest()}},
                "artifacts": {}, "totals": {"in_window_clicks": len(rows)}}
    for name in ("clicks.jsonl", "campaigns.json", "reconciliation.json"):
        manifest["artifacts"][name] = {"sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
    (root / "manifest.json").write_bytes(encoded(manifest))
    return rows


def run(scope, **kwargs):
    return import_mailchimp_clicks(archive_path=scope["root"], campaign_map=scope["mapping"],
                                   organization="test", client_slug="app", audience_slug="news",
                                   list_id="list-one", **kwargs)


def rewrite_artifact(scope, name, data):
    root = scope["root"]
    (root / name).write_bytes(data)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["artifacts"][name]["sha256"] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_bytes(encoded(manifest))


def test_default_dry_run_preserves_state_and_reports_reconciliation(scope):
    archive(scope)
    before = list(Subscription.objects.values())
    report = run(scope)
    assert report["dry_run"] is True
    assert report["counts"]["would_create"] == 3
    assert report["reconciliation"]["by_contact"]["list-one:member-one"] == {"source": 3, "would_create": 3}
    assert not EmailEvent.objects.exists()
    assert list(Subscription.objects.values()) == before


def test_apply_idempotent_preserves_duplicates_original_bounds_and_consent(scope):
    rows = archive(scope)
    before_contact = Contact.objects.values().get(pk=scope["contact"].pk)
    before_subscriptions = list(Subscription.objects.values())
    before_campaign = Campaign.objects.values().get(pk=scope["campaign"].pk)
    assert run(scope, apply=True)["counts"]["created"] == 3
    events = {event.provider_event_id: event for event in EmailEvent.objects.all()}
    assert len(events) == 3
    for row in rows:
        event = events[row["source_event_id"]]
        assert event.created_at == datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        assert event.url == URL
        assert event.campaign_recipient_id is None
        assert event.metadata["historical_import"] is True
    assert run(scope, apply=True)["counts"]["existing"] == 3
    assert Contact.objects.values().get(pk=scope["contact"].pk) == before_contact
    assert list(Subscription.objects.values()) == before_subscriptions
    assert Campaign.objects.values().get(pk=scope["campaign"].pk) == before_campaign
    assert not scope["campaign"].recipients.exists()


def test_missing_contacts_and_mapping_reported_without_creation(scope):
    archive(scope, email="missing@example.com")
    scope["mapping"].write_text("{}")
    report = run(scope, apply=True)
    assert report["missing_contacts"] == ["missing@example.com"]
    assert report["missing_campaigns"] == ["mc-campaign"]
    assert report["counts"]["unmatched"] == 3
    assert Contact.objects.count() == 1
    assert not EmailEvent.objects.exists()


def test_unmatched_contacts_load_as_unlinked_events_by_default(scope):
    archive(scope, email="missing@example.com")
    before = list(Subscription.objects.values())
    report = run(scope, apply=True)
    assert report["counts"]["created"] == 3
    assert report["counts"]["unlinked_events"] == 3
    assert report["counts"]["unlinked_source_identities"] == 1
    assert EmailEvent.objects.filter(contact__isnull=True).count() == 3
    assert Contact.objects.count() == 1
    assert list(Subscription.objects.values()) == before


def test_unmatched_archive_policy_is_explicit(scope):
    archive(scope, email="missing@example.com")
    report = run(scope, apply=True, unmatched_policy="archive")
    assert report["counts"]["unmatched"] == 3
    assert not EmailEvent.objects.exists()


def test_mapping_cannot_cross_client_scope(scope):
    archive(scope)
    other = Client.objects.create(organization=scope["client"].organization, slug="other", name="Other")
    Campaign.objects.filter(pk=scope["campaign"].pk).update(client=other)
    with pytest.raises(ClickImportError, match="outside"):
        run(scope, apply=True)
    assert not EmailEvent.objects.exists()


def test_checksum_tampering_rejected(scope):
    archive(scope)
    (scope["root"] / "raw/page.json").write_text("{}")
    with pytest.raises(ClickImportError, match="checksum"):
        run(scope, apply=True)


def test_late_invalid_row_rolls_back_earlier_writes(scope):
    rows = archive(scope)
    rows[-1]["timestamp"] = "2026-11-11T18:00:01Z"
    rewrite_artifact(scope, "clicks.jsonl", b"".join(encoded(row) + b"\n" for row in rows))
    with pytest.raises(ClickImportError, match="bounds"):
        run(scope, apply=True)
    assert not EmailEvent.objects.exists()


def test_existing_event_conflict_rejected(scope):
    archive(scope)
    run(scope, apply=True)
    EmailEvent.objects.filter(pk=EmailEvent.objects.first().pk).update(url="https://wrong.example/")
    with pytest.raises(ClickImportError, match="conflicts"):
        run(scope, apply=True)


def test_contact_source_hash_conflict_rejected(scope):
    archive(scope)
    ContactSourceMetadata.objects.create(contact=scope["contact"], client=scope["client"], audience=scope["audience"],
                                         source="mailchimp", metadata={"mailchimp": {"email_id": "different"}})
    with pytest.raises(ClickImportError, match="identity conflicts"):
        run(scope, apply=True)


def test_command_console_has_only_counts_private_report_contains_details(scope, tmp_path):
    archive(scope, email="missing@example.com")
    output, report_path = StringIO(), tmp_path / "report.json"
    call_command("import_mailchimp_clicks", archive=str(scope["root"]), campaign_map=str(scope["mapping"]),
                 organization="test", client="app", audience="news", list_id="list-one",
                 report=str(report_path), stdout=output)
    assert "missing@example.com" not in output.getvalue()
    assert URL not in output.getvalue()
    assert json.loads(report_path.read_text())["missing_contacts"] == ["missing@example.com"]


def test_other_mailchimp_list_is_reconciled_but_not_imported(scope):
    archive(scope, list_id="list-other")
    report = run(scope, apply=True)
    assert report["counts"]["outside_list"] == 3
    assert not EmailEvent.objects.exists()


def test_reconciliation_failure_rolls_back_all_events(scope):
    archive(scope)
    reconciliation = json.loads((scope["root"] / "reconciliation.json").read_text())
    reconciliation["by_contact"]["list-one:member-one"] = 2
    rewrite_artifact(scope, "reconciliation.json", encoded(reconciliation))
    with pytest.raises(ClickImportError, match="reconcile"):
        run(scope, apply=True)
    assert not EmailEvent.objects.exists()


def test_occurrence_tampering_cannot_collapse_repeated_clicks(scope):
    rows = archive(scope)
    rows[1]["occurrence"] = 1
    rewrite_artifact(scope, "clicks.jsonl", b"".join(encoded(row) + b"\n" for row in rows))
    with pytest.raises(ClickImportError, match="occurrence"):
        run(scope, apply=True)
    assert not EmailEvent.objects.exists()


def test_csv_euid_is_not_compared_to_api_subscriber_hash(scope):
    archive(scope)
    ContactSourceMetadata.objects.create(contact=scope["contact"], client=scope["client"], audience=scope["audience"],
                                         source="mailchimp", external_id="csv-euid", metadata={"mailchimp": {"euid": "csv-euid"}})
    assert run(scope, apply=True)["counts"]["created"] == 3


def test_omitted_raw_click_rejected_even_with_recomputed_derived_checksums(scope):
    rows = archive(scope)
    rows.pop()
    rewrite_artifact(scope, "clicks.jsonl", b"".join(encoded(row) + b"\n" for row in rows))
    reconciliation = {"by_campaign": {"mc-campaign": 2}, "by_contact": {"list-one:member-one": 2},
                      "by_link": {"mc-campaign:" + URL: 2}}
    rewrite_artifact(scope, "reconciliation.json", encoded(reconciliation))
    path = scope["root"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["totals"]["in_window_clicks"] = 2
    path.write_bytes(encoded(manifest))
    with pytest.raises(ClickImportError, match="absent"):
        run(scope, apply=True)
    assert not EmailEvent.objects.exists()


def test_multiple_batches_keep_identical_click_occurrences(scope):
    archive(scope, timestamps=(START,) * 501)
    assert run(scope, apply=True)["counts"]["created"] == 501
    assert EmailEvent.objects.count() == 501
    assert run(scope, apply=True)["counts"]["existing"] == 501


def test_imported_clicks_render_in_scoped_contact_timeline(scope, client, django_user_model):
    archive(scope)
    run(scope, apply=True)
    operator = django_user_model.objects.create_user(username="history-reader", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = scope["client"].pk
    session.save()

    response = client.get(reverse("mailing:contact_detail", args=[scope["contact"].normalized_email]))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Campaign: History" in html
    assert URL in html
    source_time = timezone.localtime(datetime.fromisoformat(START.replace("Z", "+00:00")))
    assert source_time.strftime("%Y-%m-%d %H:%M:%S") in html
    assert len(response.context["events"].object_list) == 3
    metrics = {metric.label: metric.value for metric in contact_metrics(scope["contact"], scope["client"])}
    assert metrics["Last clicked"] == datetime.fromisoformat(END.replace("Z", "+00:00"))

    other = Client.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = other.pk
    session.save()
    response = client.get(reverse("mailing:contact_detail", args=[scope["contact"].normalized_email]))
    assert response.status_code == 200
    assert not response.context["events"].object_list
    assert URL not in response.content.decode()
    assert next(metric.value for metric in contact_metrics(scope["contact"], other)
                if metric.label == "Last clicked") is None
