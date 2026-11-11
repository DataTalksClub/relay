"""Import verified Mailchimp click history without invoking sending or consent services."""

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    Contact,
    ContactSourceMetadata,
    EmailEvent,
    EmailEventType,
)
from mailing.services.mailchimp_source_identity import (
    erased_source_keys,
    lock_source_history_scope,
    source_identity_key,
)


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
        for page in manifest.get("superseded_inventory_pages", {}).values():
            self.verify(page["file"], page["sha256"])
        for page in manifest["pages"].values():
            self.verify(page["file"], page["sha256"])
            self.pages[page["file"]] = page
        self.manifest_sha256 = digest_file(self.root / "manifest.json")
        self.expected = json.loads((self.root / "reconciliation.json").read_text(encoding="utf-8"))
        reports = json.loads((self.root / "campaigns.json").read_text(encoding="utf-8"))
        self.campaign_ids = {report["id"] for report in reports}
        self.subreport_parents = {report["id"] for report in reports if report.get("type") in {"variate", "absplit"}}
        self.related_campaigns = self.subreport_relations()

    def subreport_relations(self):
        """Read parent/child evidence only from verified, fully paginated raw pages."""
        groups = {}
        for page in self.pages.values():
            match = re.fullmatch(r"/reports/([A-Za-z0-9_-]+)/sub-reports", page["path"])
            if match:
                groups.setdefault(match[1], []).append(page)
            elif page["path"].endswith("/sub-reports"):
                raise ClickImportError("Sub-report relationship path is malformed")
        if self.subreport_parents - groups.keys():
            raise ClickImportError("Sub-report pagination is missing for an inventoried parent")
        related, parents = {}, {}
        for parent, pages in groups.items():
            if parent not in self.campaign_ids:
                raise ClickImportError("Sub-report parent is absent from the campaign inventory")
            offset, expected, children = 0, None, set()
            if any(not isinstance(page.get("params"), dict)
                   or type(page["params"].get("offset")) is not int for page in pages):
                raise ClickImportError("Sub-report pagination is malformed or incomplete")
            for page in sorted(pages, key=lambda item: item["params"]["offset"]):
                params = page["params"]
                if (type(params.get("offset")) is not int or params["offset"] != offset
                        or type(params.get("count")) is not int or not 1 <= params["count"] <= 1000):
                    raise ClickImportError("Sub-report pagination is malformed or incomplete")
                payload = json.loads(archive_path(self.root, page["file"]).read_text(encoding="utf-8"))
                reports, total = payload.get("reports"), payload.get("total_items")
                if (not isinstance(reports, list) or type(total) is not int or total < 0
                        or len(reports) > params["count"] or offset + len(reports) > total
                        or (expected is not None and expected != total)):
                    raise ClickImportError("Sub-report pagination is malformed or incomplete")
                expected = total
                for report in reports:
                    child = report.get("id") if isinstance(report, dict) else None
                    if (not isinstance(child, str) or child not in self.campaign_ids
                            or child == parent or child in children or child in parents):
                        raise ClickImportError("Sub-report relationship is malformed or missing from the campaign inventory")
                    children.add(child)
                    parents[child] = parent
                    related.setdefault(parent, set()).add(child)
                    related.setdefault(child, set()).add(parent)
                offset += len(reports)
            if offset != expected:
                raise ClickImportError("Sub-report pagination is malformed or incomplete")
        for child in parents:
            seen, current = set(), child
            while current in parents:
                if current in seen:
                    raise ClickImportError("Sub-report relationship contains a cycle")
                seen.add(current)
                current = parents[current]
        return related

    def verify(self, relative, digest):
        if digest_file(archive_path(self.root, relative)) != digest:
            raise ClickImportError("Archive checksum mismatch")

    def rows(self):
        current_file, payload = None, None
        seen = set()
        related_signatures = {campaign_id: set() for campaign_id in self.related_campaigns}
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
                self.check_parent_child_overlap(row, related_signatures)
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

    def check_parent_child_overlap(self, row, signatures):
        """Refuse ambiguous duplicate evidence; never guess which campaign owns it."""
        campaign_id = row["campaign_id"]
        if campaign_id not in self.related_campaigns:
            return
        canonical = timestamp(row["timestamp"]).isoformat().replace("+00:00", "Z")
        signature = hashlib.sha256(encoded([
            row["list_id"], row["email_id"], canonical, row["url"], row["occurrence"],
        ])).digest()
        if any(signature in signatures[other] for other in self.related_campaigns[campaign_id]):
            raise ClickImportError("Mailchimp parent/child click histories overlap; source ownership must be resolved")
        signatures[campaign_id].add(signature)

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
        if type(row["occurrence"]) is not int or row["occurrence"] != occurrence or row["source_event_id"] != expected_id:
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


def validate_mapping_scope(campaign, client, audience):
    if campaign.client_id != client.pk or campaign.audience_id != audience.pk:
        raise ClickImportError("Mapped campaign is outside the requested client/audience scope")
    if campaign.status != CampaignStatus.SENT:
        raise ClickImportError("Historical clicks require an already-sent mapped campaign")


def mapping_provenance(campaign):
    if not isinstance(campaign.metadata, dict) or not isinstance(campaign.metadata.get("mailchimp", {}), dict):
        raise ClickImportError("Mapped campaign has malformed Mailchimp source provenance")
    return campaign.metadata.get("mailchimp", {})


def conflicting_mapping_source(campaign, source_id, datacenter, list_id):
    provenance = mapping_provenance(campaign)
    expected_key = f"mailchimp-{datacenter}-{source_id}"
    if campaign.external_key.startswith("mailchimp-") and campaign.external_key != expected_key:
        return True
    return any(provenance.get(field) and provenance[field] != expected for field, expected in (
        ("campaign_id", source_id), ("datacenter", datacenter), ("list_id", list_id)
    ))


def verify_mapping_alias(alias, target, source_id, datacenter, list_id, client, audience):
    validate_mapping_scope(alias, client, audience)
    if conflicting_mapping_source(alias, source_id, datacenter, list_id):
        raise ClickImportError("Source alias provenance conflicts with the Mailchimp campaign mapping")
    current, visited = alias, set()
    while current.merged_into_id is not None:
        if current.pk in visited:
            raise ClickImportError("Mapped source alias merge chain contains a cycle")
        visited.add(current.pk)
        current = Campaign.objects.get(pk=current.merged_into_id)
        validate_mapping_scope(current, client, audience)
    if current.pk != target.pk:
        raise ClickImportError("Mapped target is unrelated to the source alias canonical campaign")


def validate_campaign_mapping(target, source_id, datacenter, list_id, client, audience, source_campaigns):
    validate_mapping_scope(target, client, audience)
    if target.merged_into_id is not None:
        raise ClickImportError("Historical clicks cannot map to a merged campaign alias")
    expected_key = f"mailchimp-{datacenter}-{source_id}"
    verified_alias = False
    for candidate in source_campaigns:
        raw_metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        provenance = raw_metadata.get("mailchimp", {})
        exact_metadata = isinstance(provenance, dict) and all(provenance.get(field) == value for field, value in (
            ("campaign_id", source_id), ("datacenter", datacenter), ("list_id", list_id)
        ))
        if candidate.pk == target.pk or not (candidate.external_key == expected_key or exact_metadata):
            continue
        if candidate.merged_into_id is None:
            raise ClickImportError("Another unmerged campaign claims the mapped Mailchimp source ID")
        verify_mapping_alias(candidate, target, source_id, datacenter, list_id, client, audience)
        verified_alias = True
    if conflicting_mapping_source(target, source_id, datacenter, list_id) and not verified_alias:
        raise ClickImportError("Mapped campaign source provenance conflicts with the Mailchimp source ID")


def resolve_campaigns(mapping_path, client, audience, *, datacenter, list_id):
    if not isinstance(datacenter, str) or not re.fullmatch(r"[a-z]{2}\d+", datacenter):
        raise ClickImportError("Archive Mailchimp datacenter is invalid")
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or type(value) is not int
                                            for key, value in mapping.items()):
        raise ClickImportError("Campaign map must map Mailchimp IDs to integer Relay campaign IDs")
    campaigns = {campaign.pk: campaign for campaign in Campaign.objects.filter(pk__in=mapping.values())}
    if set(mapping.values()) != set(campaigns):
        raise ClickImportError("Campaign map references missing Relay campaigns")
    source_campaigns = list(Campaign.objects.filter(client=client))
    for source_id, relay_id in mapping.items():
        validate_campaign_mapping(campaigns[relay_id], source_id, datacenter, list_id, client, audience, source_campaigns)
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
        "campaign_id": campaign.pk, "contact_id": contact.pk if contact else None, "client_id": client.pk,
        "audience_id": audience.pk, "event_type": EmailEventType.CLICK, "url": row["url"],
        "metadata": {"provider": "mailchimp", "historical_import": True, "mailchimp": {
            "campaign_id": row["campaign_id"], "list_id": row["list_id"], "email_id": row["email_id"],
            "datacenter": archive.manifest["datacenter"], "identity_status": "matched" if contact else "unlinked",
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
    for key in ("datacenter", "identity_status"):
        # Read valid legacy events without restoring or rewriting their metadata.
        if key in source and source[key] != values["metadata"]["mailchimp"][key]:
            raise ClickImportError("Existing source event identity decision conflicts with the archive")


def import_mailchimp_clicks(*, archive_path, campaign_map, organization, client_slug, audience_slug,
                            list_id, apply=False, unmatched_policy="unlinked"):
    """Return a private detailed report; dry-run is the default. All writes are atomic."""
    try:
        if unmatched_policy not in {"unlinked", "archive"}:
            raise ClickImportError("Unmatched policy must be unlinked or archive")
        archive = VerifiedArchive(archive_path)
        client, audience = resolve_scope(organization, client_slug, audience_slug)
        campaigns = resolve_campaigns(campaign_map, client, audience, datacenter=archive.manifest["datacenter"],
                                     list_id=list_id)
        return run_import(archive, campaigns, client, audience, list_id, apply, unmatched_policy)
    except ClickImportError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, Client.DoesNotExist, Audience.DoesNotExist):
        raise ClickImportError("Invalid or unreadable archive, campaign mapping, or target scope") from None


@transaction.atomic
def run_import(archive, campaigns, client, audience, list_id, apply, unmatched_policy):
    if apply:
        # Serialize history import/erasure without locking live sender FK rows.
        lock_source_history_scope(client, audience)
    erased = erased_source_keys(client, audience)
    decisions = existing_identity_decisions(archive, campaigns, client, audience, list_id, erased)
    saved_contacts = Contact.objects.in_bulk({value for value in decisions.values() if value is not None})
    report = {"dry_run": not apply, "manifest_sha256": archive.manifest_sha256,
              "window": archive.manifest["window"], "counts": Counter(), "missing_contacts": [],
              "missing_campaigns": [], "reconciliation": {}, "unmatched_policy": unmatched_policy}
    report["counts"].update({"matched_events": 0, "unlinked_events": 0, "erased_source_events": 0})
    groups = {key: {} for key in ("by_campaign", "by_contact", "by_link")}
    missing_contacts, missing_campaigns = set(), set()
    contacts = {}
    sources = {}
    verified_identities = set()
    pending = []
    identity_counts = {"matched": set(), "unlinked": set(), "erased_source": set()}
    for row in prefetch_contacts(archive.rows(), contacts, sources, client, audience):
        report["counts"]["source_clicks"] += 1
        if row["list_id"] != list_id:
            outcome = "outside_list"
        else:
            key = source_identity_key(archive.manifest["datacenter"], row["list_id"], row["email_id"])
            if key in erased:
                identity_counts["erased_source"].add(key)
                report["counts"]["erased_source_events"] += 1
                record_outcome(row, "erased_source", report, groups)
                continue
            email = row["email_address"].strip().casefold()
            contact, campaign = contacts[email], campaigns.get(row["campaign_id"])
            if key in decisions:
                contact_id = decisions[key]
                contact = saved_contacts.get(contact_id) if contact_id is not None else None
                if contact_id is not None and contact is None:
                    raise ClickImportError("Saved source identity references an unavailable Relay contact")
            else:
                decisions[key] = contact.pk if contact else None
                if contact is not None:
                    saved_contacts[contact.pk] = contact
            if contact is not None and contact.normalized_email.endswith("@erased.invalid"):
                raise ClickImportError("An anonymized Relay contact cannot define a source identity linkage")
            if contact is None and contacts[email] is None:
                missing_contacts.add(email)
            if campaign is None:
                missing_campaigns.add(row["campaign_id"])
            if campaign is None:
                outcome = "unmatched"
            else:
                identity = (contact.pk if contact else None, row["list_id"], row["email_id"])
                if contact is not None and identity not in verified_identities:
                    verify_contact_identity(row, sources.get(contact.pk, []))
                    verified_identities.add(identity)
                values = event_values(row, contact, campaign, client, audience, archive)
                pending.append((row, values))
                if len(pending) >= 500:
                    persist_batch(pending, apply, report, groups, unmatched_policy, identity_counts)
                    pending.clear()
                continue
        record_outcome(row, outcome, report, groups)
    persist_batch(pending, apply, report, groups, unmatched_policy, identity_counts)
    report["missing_contacts"] = sorted(missing_contacts)
    report["missing_campaigns"] = sorted(missing_campaigns)
    report["counts"]["missing_contacts"] = len(missing_contacts)
    report["counts"]["missing_campaigns"] = len(missing_campaigns)
    for status, identities in identity_counts.items():
        name = "erased_source_identities" if status == "erased_source" else status + "_source_identities"
        report["counts"][name] = len(identities)
    report["reconciliation"] = groups
    return report


def existing_identity_decisions(archive, campaigns, client, audience, list_id, erased):
    """Saved source IDs outrank email lookup, including intentionally unlinked IDs."""
    decisions = {}
    datacenter = archive.manifest["datacenter"]
    events = EmailEvent.objects.filter(client=client, audience=audience,
                                       provider_event_id__startswith="mailchimp:click:")
    for event in events.only("contact_id", "campaign_id", "metadata").iterator(chunk_size=2000):
        metadata = event.metadata
        if not isinstance(metadata, dict):
            raise ClickImportError("Historical source event metadata is malformed")
        if metadata.get("erased") or metadata.get("source_history_erased"):
            if metadata.get("mailchimp_source_key_hash") not in erased:
                raise ClickImportError("Erased historical source event lacks a verifiable source tombstone")
            continue
        if metadata.get("provider") != "mailchimp" or metadata.get("historical_import") is not True:
            continue
        source = metadata.get("mailchimp", {})
        if not isinstance(source, dict):
            raise ClickImportError("Historical source event provenance is malformed")
        if source.get("list_id") != list_id:
            continue
        source_dc = source.get("datacenter")
        if source_dc is None:
            # Legacy imports omitted dc. Infer only from an exact campaign map
            # independently validated by this invocation; never rewrite metadata.
            campaign = campaigns.get(source.get("campaign_id"))
            if campaign is None or campaign.pk != event.campaign_id:
                raise ClickImportError("Legacy source identity lacks a verifiable account/campaign mapping")
            source_dc = datacenter
        if source_dc != datacenter:
            continue
        key = source_identity_key(source_dc, source["list_id"], source["email_id"])
        if key in erased:
            continue
        expected = "matched" if event.contact_id else "unlinked"
        if source.get("identity_status", expected) != expected:
            raise ClickImportError("Existing source identity has a conflicting linked/unlinked decision")
        if key in decisions and decisions[key] != event.contact_id:
            raise ClickImportError("Existing source identity has conflicting Relay contact linkage decisions")
        decisions[key] = event.contact_id
    return decisions


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


def persist_batch(pending, apply, report, groups, unmatched_policy, identity_counts):
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
        elif values["contact_id"] is None and unmatched_policy == "archive":
            outcome = "unmatched"
        else:
            outcome = "created" if apply else "would_create"
            new.append((EmailEvent(provider_event_id=row["source_event_id"], **values), timestamp(row["timestamp"])))
        if outcome != "unmatched":
            source = values["metadata"]["mailchimp"]
            status = source["identity_status"]
            identity_counts[status].add(source_identity_key(source["datacenter"], source["list_id"], source["email_id"]))
            report["counts"][status + "_events"] += 1
        record_outcome(row, outcome, report, groups)
    if apply and new:
        # The unique provider ID constraint rejects a concurrent competing import,
        # rolling back this transaction rather than hiding a conflicting event.
        events = [event for event, _ in new]
        EmailEvent.objects.bulk_create(events, batch_size=500)
        for event, occurred_at in new:
            event.created_at = occurred_at
        EmailEvent.objects.bulk_update(events, ["created_at"], batch_size=500)
