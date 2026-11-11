"""Import verified Mailchimp click history without invoking sending or consent services."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from mailing.models import Audience, Campaign, Client, Contact, ContactSourceMetadata, EmailEvent, EmailEventType


class ClickImportError(Exception):
    """Public-safe archive/import validation failure."""


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ClickImportError("Archive timestamps must include a timezone")
    return result.astimezone(UTC)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ClickImportError("Archive file escapes its root")
    return path


class VerifiedArchive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest = self.manifest
        if (manifest.get("schema_version"), manifest.get("source"), manifest.get("status")) != (
            1, "mailchimp", "complete_available_api_data"
        ):
            raise ClickImportError("Archive is not a complete supported Mailchimp archive")
        self.start = timestamp(manifest["window"]["start_inclusive"])
        self.end = timestamp(manifest["window"]["end_inclusive"])
        if self.end < self.start:
            raise ClickImportError("Archive bounds are reversed")
        for name in ("clicks.jsonl", "campaigns.json", "reconciliation.json"):
            self.verify(name, manifest["artifacts"][name]["sha256"])
        self.pages = {}
        for page in manifest["pages"].values():
            self.verify(page["file"], page["sha256"])
            self.pages[page["file"]] = page
        self.manifest_sha256 = digest_file(self.root / "manifest.json")
        self.expected = json.loads((self.root / "reconciliation.json").read_text(encoding="utf-8"))
        self.campaign_ids = {
            report["id"] for report in json.loads((self.root / "campaigns.json").read_text(encoding="utf-8"))
        }

    def verify(self, relative, digest):
        if digest_file(archive_path(self.root, relative)) != digest:
            raise ClickImportError("Archive checksum mismatch")

    def rows(self):
        current_file, payload = None, None
        seen = set()
        counts = {key: Counter() for key in ("by_campaign", "by_contact", "by_link")}
        with (self.root / "clicks.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                raw_file = row["raw_file"]
                page = self.pages.get(raw_file)
                if page is None or page["path"] != f"/reports/{row['campaign_id']}/email-activity":
                    raise ClickImportError("Click provenance is not a verified campaign activity page")
                if raw_file != current_file:
                    payload = json.loads(archive_path(self.root, raw_file).read_text(encoding="utf-8"))
                    current_file = raw_file
                    self.member_occurrences = {}
                self.validate_row(row, payload)
                if row["source_event_id"] in seen:
                    raise ClickImportError("Duplicate normalized source event")
                seen.add(row["source_event_id"])
                counts["by_campaign"][row["campaign_id"]] += 1
                counts["by_contact"][row["list_id"] + ":" + row["email_id"]] += 1
                counts["by_link"][row["campaign_id"] + ":" + row["url"]] += 1
                yield row
        if any(dict(value) != self.expected[key] for key, value in counts.items()):
            raise ClickImportError("Normalized rows do not reconcile with the archive")
        if len(seen) != self.manifest["totals"].get("in_window_clicks", 0):
            raise ClickImportError("Normalized click total does not match the manifest")
        self.reconcile_raw_events(seen)

    def reconcile_raw_events(self, remaining):
        """Every available in-window raw click must have exactly one normalized row."""
        for page in self.pages.values():
            if not page["path"].endswith("/email-activity"):
                continue
            campaign_id = page["path"].split("/")[2]
            payload = json.loads(archive_path(self.root, page["file"]).read_text(encoding="utf-8"))
            for member in payload["emails"]:
                occurrences = Counter()
                for event in member["activity"]:
                    if event.get("action") != "click":
                        continue
                    at = timestamp(event["timestamp"])
                    canonical = at.isoformat().replace("+00:00", "Z")
                    signature = [campaign_id, member["list_id"], member["email_id"], canonical, event["url"]]
                    key = encoded(signature)
                    occurrences[key] += 1
                    if self.start <= at <= self.end:
                        identity = "mailchimp:click:" + hashlib.sha256(encoded(signature + [occurrences[key]] )).hexdigest()
                        if identity not in remaining:
                            raise ClickImportError("Raw in-window click is absent or duplicated in normalized history")
                        remaining.remove(identity)
        if remaining:
            raise ClickImportError("Normalized history contains clicks absent from the raw archive")

    def validate_row(self, row, payload):
        if any(type(row[field]) is not int or row[field] < 0
               for field in ("raw_member_index", "raw_activity_index")):
            raise ClickImportError("Click provenance indices must be nonnegative integers")
        member = payload["emails"][row["raw_member_index"]]
        event = member["activity"][row["raw_activity_index"]]
        at = timestamp(row["timestamp"])
        if (row["source"] != "mailchimp" or row["campaign_id"] not in self.campaign_ids
                or not self.start <= at <= self.end or event["action"] != "click"):
            raise ClickImportError("Click source, campaign or frozen bounds are invalid")
        if any(row[key] != member[key] for key in ("list_id", "email_id", "email_address")):
            raise ClickImportError("Click identity differs from its raw source")
        if row["url"] != event["url"] or at != timestamp(event["timestamp"]):
            raise ClickImportError("Click details differ from their raw source")
        if row["raw_member_index"] not in self.member_occurrences:
            seen, indices = Counter(), {}
            for index, activity in enumerate(member["activity"]):
                if activity.get("action") == "click":
                    key = (timestamp(activity["timestamp"]), activity["url"])
                    seen[key] += 1
                    indices[index] = seen[key]
            self.member_occurrences[row["raw_member_index"]] = indices
        occurrence = self.member_occurrences[row["raw_member_index"]][row["raw_activity_index"]]
        signature = [row["campaign_id"], row["list_id"], row["email_id"], row["timestamp"], row["url"], occurrence]
        expected_id = "mailchimp:click:" + hashlib.sha256(encoded(signature)).hexdigest()
        if row["occurrence"] != occurrence or row["source_event_id"] != expected_id:
            raise ClickImportError("Click occurrence/source event identity is invalid")
        if not isinstance(row["url"], str) or len(row["url"]) > 2048:
            raise ClickImportError("Click URL exceeds supported storage")
        try:
            validate_email(row["email_address"].strip().casefold())
        except ValidationError:
            raise ClickImportError("Click source has an invalid email address") from None


def resolve_scope(organization, client_slug, audience_slug):
    client = Client.objects.get(organization__slug=organization, slug=client_slug)
    audience = Audience.objects.get(organization=client.organization, slug=audience_slug)
    return client, audience


def resolve_campaigns(mapping_path, client, audience):
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or type(value) is not int
                                            for key, value in mapping.items()):
        raise ClickImportError("Campaign map must map Mailchimp IDs to integer Relay campaign IDs")
    campaigns = {campaign.pk: campaign for campaign in Campaign.objects.filter(pk__in=mapping.values())}
    for campaign in campaigns.values():
        if campaign.client_id != client.pk or campaign.audience_id != audience.pk:
            raise ClickImportError("Mapped campaign is outside the requested client/audience scope")
    if set(mapping.values()) != set(campaigns):
        raise ClickImportError("Campaign map references missing Relay campaigns")
    return {source_id: campaigns[relay_id] for source_id, relay_id in mapping.items()}


def verify_contact_identity(row, sources):
    # CSV imports use EUID/LEID as external_id, which is not the API subscriber hash.
    # Compare only source fields whose semantics explicitly match the API fields.
    for source in sources:
        metadata = source.metadata.get("mailchimp", {})
        for field in ("email_id", "list_id"):
            if metadata.get(field) and metadata[field] != row[field]:
                raise ClickImportError("Contact source identity conflicts with the archive")


def event_values(row, contact, campaign, client, audience, archive):
    return {
        "campaign_id": campaign.pk, "contact_id": contact.pk, "client_id": client.pk,
        "audience_id": audience.pk, "event_type": EmailEventType.CLICK, "url": row["url"],
        "metadata": {"provider": "mailchimp", "historical_import": True, "mailchimp": {
            "campaign_id": row["campaign_id"], "list_id": row["list_id"], "email_id": row["email_id"],
            "occurrence": row["occurrence"], "timestamp": row["timestamp"],
            "raw_file": row["raw_file"], "raw_member_index": row["raw_member_index"],
            "raw_activity_index": row["raw_activity_index"],
            "archive_manifest_sha256": archive.manifest_sha256,
        }},
    }


def verify_existing(event, values, row):
    for key in ("campaign_id", "contact_id", "client_id", "audience_id", "event_type", "url"):
        if getattr(event, key) != values[key]:
            raise ClickImportError("Existing source event conflicts with the archive")
    if event.created_at != timestamp(row["timestamp"]):
        raise ClickImportError("Existing source event timestamp conflicts with the archive")
    if event.metadata.get("provider") != "mailchimp" or not event.metadata.get("historical_import"):
        raise ClickImportError("Existing source event is not a Mailchimp historical import")
    source = event.metadata.get("mailchimp", {})
    for key in ("campaign_id", "list_id", "email_id", "occurrence", "timestamp"):
        if source.get(key) != values["metadata"]["mailchimp"][key]:
            raise ClickImportError("Existing source event provenance conflicts with the archive")


def import_mailchimp_clicks(*, archive_path, campaign_map, organization, client_slug, audience_slug,
                            list_id, apply=False):
    """Return a private detailed report; dry-run is the default. All writes are atomic."""
    try:
        archive = VerifiedArchive(archive_path)
        client, audience = resolve_scope(organization, client_slug, audience_slug)
        campaigns = resolve_campaigns(campaign_map, client, audience)
        return run_import(archive, campaigns, client, audience, list_id, apply)
    except ClickImportError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, Client.DoesNotExist, Audience.DoesNotExist):
        raise ClickImportError("Invalid or unreadable archive, campaign mapping, or target scope") from None


@transaction.atomic
def run_import(archive, campaigns, client, audience, list_id, apply):
    report = {"dry_run": not apply, "manifest_sha256": archive.manifest_sha256,
              "window": archive.manifest["window"], "counts": Counter(), "missing_contacts": [],
              "missing_campaigns": [], "reconciliation": {}}
    groups = {key: {} for key in ("by_campaign", "by_contact", "by_link")}
    missing_contacts, missing_campaigns = set(), set()
    contacts = {}
    sources = {}
    verified_identities = set()
    pending = []
    for row in prefetch_contacts(archive.rows(), contacts, sources, client, audience):
        report["counts"]["source_clicks"] += 1
        if row["list_id"] != list_id:
            outcome = "outside_list"
        else:
            email = row["email_address"].strip().casefold()
            contact, campaign = contacts[email], campaigns.get(row["campaign_id"])
            if contact is None:
                missing_contacts.add(email)
            if campaign is None:
                missing_campaigns.add(row["campaign_id"])
            if contact is None or campaign is None:
                outcome = "unmatched"
            else:
                identity = (contact.pk, row["list_id"], row["email_id"])
                if identity not in verified_identities:
                    verify_contact_identity(row, sources.get(contact.pk, []))
                    verified_identities.add(identity)
                values = event_values(row, contact, campaign, client, audience, archive)
                pending.append((row, values))
                if len(pending) >= 500:
                    persist_batch(pending, apply, report, groups)
                    pending.clear()
                continue
        record_outcome(row, outcome, report, groups)
    persist_batch(pending, apply, report, groups)
    report["missing_contacts"] = sorted(missing_contacts)
    report["missing_campaigns"] = sorted(missing_campaigns)
    report["counts"]["missing_contacts"] = len(missing_contacts)
    report["counts"]["missing_campaigns"] = len(missing_campaigns)
    report["reconciliation"] = groups
    return report


def prefetch_contacts(rows, contacts, sources, client, audience):
    for batch in batched(rows, 500):
        emails = {row["email_address"].strip().casefold() for row in batch} - contacts.keys()
        found = list(Contact.objects.filter(normalized_email__in=emails).only("pk", "normalized_email"))
        contacts.update({email: None for email in emails})
        contacts.update({contact.normalized_email: contact for contact in found})
        for source in ContactSourceMetadata.objects.filter(contact_id__in=[contact.pk for contact in found],
                                                           client=client, audience=audience, source="mailchimp"):
            sources.setdefault(source.contact_id, []).append(source)
        yield from batch


def record_outcome(row, outcome, report, groups):
    report["counts"][outcome] += 1
    dimensions = {"by_campaign": row["campaign_id"],
                  "by_contact": row["list_id"] + ":" + row["email_id"],
                  "by_link": row["campaign_id"] + ":" + row["url"]}
    for dimension, key in dimensions.items():
        counts = groups[dimension].setdefault(key, Counter())
        counts["source"] += 1
        counts[outcome] += 1


def persist_batch(pending, apply, report, groups):
    if not pending:
        return
    existing = {event.provider_event_id: event for event in EmailEvent.objects.filter(
        provider_event_id__in=[row["source_event_id"] for row, _ in pending]
    )}
    new = []
    for row, values in pending:
        event = existing.get(row["source_event_id"])
        if event is not None:
            verify_existing(event, values, row)
            outcome = "existing"
        else:
            outcome = "created" if apply else "would_create"
            new.append((EmailEvent(provider_event_id=row["source_event_id"], **values), timestamp(row["timestamp"])))
        record_outcome(row, outcome, report, groups)
    if apply and new:
        # The unique provider ID constraint rejects a concurrent competing import,
        # rolling back this transaction rather than hiding a conflicting event.
        events = [event for event, _ in new]
        EmailEvent.objects.bulk_create(events, batch_size=500)
        for event, occurred_at in new:
            event.created_at = occurred_at
        EmailEvent.objects.bulk_update(events, ["created_at"], batch_size=500)
