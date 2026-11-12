"""Plan source-ID mappings and optionally create inert historical Mailchimp campaigns."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from django.db import transaction

from mailing.models import Audience, Campaign, CampaignStatus, Client
from mailing.services.mailchimp_click_import import ClickImportError, VerifiedArchive, resolve_scope, timestamp


def source_campaigns(archive, list_id):
    counts, members = Counter(), defaultdict(set)
    # Exhausting this iterator independently verifies every raw/normalized click.
    for row in archive.rows():
        if row["list_id"] == list_id:
            counts[row["campaign_id"]] += 1
            members[row["campaign_id"]].add(row["email_id"])
    reports = json.loads((archive.root / "campaigns.json").read_text(encoding="utf-8"))
    indexed = {}
    for report in reports:
        if report["id"] in indexed:
            raise ClickImportError("Archive campaign inventory contains duplicate IDs")
        indexed[report["id"]] = report
    selected = {}
    for campaign_id, count in counts.items():
        if not isinstance(campaign_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", campaign_id):
            raise ClickImportError("Archive campaign ID cannot form a supported source key")
        report = indexed[campaign_id]
        if report.get("list_id") and report["list_id"] != list_id:
            raise ClickImportError("Archived campaign audience conflicts with its click identities")
        sent_at = timestamp(report["send_time"])
        if sent_at > archive.end:
            raise ClickImportError("Archived campaign send time exceeds the extraction bound")
        selected[campaign_id] = {
            "campaign_id": campaign_id, "source_click_count": count,
            "source_unique_click_count": len(members[campaign_id]), "sent_at": sent_at,
            "subject": report.get("subject_line") or report.get("campaign_title") or campaign_id,
            "campaign_title": report.get("campaign_title", ""),
        }
    return selected


def explicit_mapping(path, selected):
    if path is None:
        return {}
    mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or type(value) is not int
                                            for key, value in mapping.items()):
        raise ClickImportError("Explicit mapping must map Mailchimp IDs to integer Relay campaign IDs")
    if set(mapping) - selected.keys():
        raise ClickImportError("Explicit mapping contains campaigns without clicks in the selected list/window")
    return mapping


def validate_scope_and_status(campaign, client, audience):
    if campaign.client_id != client.pk or campaign.audience_id != audience.pk:
        raise ClickImportError("Existing campaign is outside the requested client/audience scope")
    if campaign.status != CampaignStatus.SENT:
        raise ClickImportError("Historical clicks can only map to an already-sent campaign")


def validate_existing(campaign, campaign_id, datacenter, list_id, client, audience, *, allow_alias=False):
    validate_scope_and_status(campaign, client, audience)
    if campaign.merged_into_id is not None and not allow_alias:
        raise ClickImportError("Mailchimp mapping references a merged alias; resolve its canonical campaign first")
    expected_key = f"mailchimp-{datacenter}-{campaign_id}"
    if campaign.external_key.startswith("mailchimp-") and campaign.external_key != expected_key:
        raise ClickImportError("Existing campaign external key conflicts with the requested Mailchimp ID")
    metadata = campaign.metadata.get("mailchimp", {})
    if not isinstance(metadata, dict):
        raise ClickImportError("Existing campaign Mailchimp provenance is malformed")
    for key, expected in (("campaign_id", campaign_id), ("datacenter", datacenter), ("list_id", list_id)):
        if metadata.get(key) and metadata[key] != expected:
            raise ClickImportError("Existing campaign source provenance conflicts with the requested Mailchimp ID")


def validate_explicit_canonical(alias, target, campaign_id, datacenter, list_id, client, audience):
    # The alias itself must prove the source identity; an explicit target does
    # not waive conflicting original provenance or authorize rekeying anything.
    validate_existing(alias, campaign_id, datacenter, list_id, client, audience, allow_alias=True)
    current, visited = alias, set()
    while current.merged_into_id is not None:
        if current.pk in visited:
            raise ClickImportError("Merged campaign mapping contains a cycle")
        visited.add(current.pk)
        current = Campaign.objects.get(pk=current.merged_into_id)
        validate_scope_and_status(current, client, audience)
    if current.pk in visited:
        raise ClickImportError("Merged campaign mapping contains a cycle")
    if current.pk != target.pk:
        raise ClickImportError("Explicit mapping conflicts with the verified merged canonical campaign")
    return current


def validate_owned_history(campaign, source, archive):
    if campaign.metadata.get("historical_import") is not True or campaign.metadata.get("provider") != "mailchimp":
        return
    provenance = campaign.metadata.get("mailchimp", {})
    if (provenance.get("window") != archive.manifest["window"]
            or campaign.click_count != source["source_click_count"]
            or campaign.unique_click_count != source["source_unique_click_count"]):
        raise ClickImportError("Owned historical campaign has an incompatible archive window or click counts")


def find_existing(campaign_id, datacenter, list_id, campaigns, explicit, client, audience):
    key = f"mailchimp-{datacenter}-{campaign_id}"
    candidates = []
    for campaign in campaigns:
        provenance = campaign.metadata.get("mailchimp", {})
        if not isinstance(provenance, dict):
            if campaign.external_key == key:
                raise ClickImportError("Existing campaign Mailchimp provenance is malformed")
            continue
        exact_metadata = all(provenance.get(field) == value for field, value in (
            ("campaign_id", campaign_id), ("datacenter", datacenter), ("list_id", list_id)
        ))
        if campaign.external_key == key or exact_metadata:
            candidates.append(campaign)
        elif provenance.get("campaign_id") == campaign_id:
            raise ClickImportError("Existing campaign has conflicting or incomplete Mailchimp source provenance")
    if len(candidates) > 1:
        raise ClickImportError("More than one existing campaign claims the Mailchimp source ID")
    if campaign_id in explicit:
        campaign = Campaign.objects.filter(pk=explicit[campaign_id]).first()
        if campaign is None:
            raise ClickImportError("Explicit mapping references a missing Relay campaign")
        if candidates and candidates[0].pk != campaign.pk:
            if candidates[0].merged_into_id is None:
                raise ClickImportError("Explicit mapping conflicts with an existing Mailchimp source mapping")
            return validate_explicit_canonical(candidates[0], campaign, campaign_id, datacenter,
                                               list_id, client, audience)
    else:
        campaign = candidates[0] if candidates else None
    if campaign is not None:
        validate_existing(campaign, campaign_id, datacenter, list_id, client, audience)
    return campaign


def prepare_mailchimp_click_campaigns(*, archive_path, organization, client_slug, audience_slug,
                                     list_id, mapping_path=None, apply=False, allow_create=False):
    """Return (mapping, private plan). Existing campaign fields are never changed."""
    try:
        archive = VerifiedArchive(archive_path)
        datacenter = archive.manifest["datacenter"]
        if not re.fullmatch(r"[a-z]{2}\d+", datacenter):
            raise ClickImportError("Archive has an invalid Mailchimp datacenter")
        selected = source_campaigns(archive, list_id)
        explicit = explicit_mapping(mapping_path, selected)
        client, audience = resolve_scope(organization, client_slug, audience_slug)
        return apply_plan(archive, selected, explicit, client, audience, datacenter, list_id, apply, allow_create)
    except ClickImportError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError,
            Client.DoesNotExist, Audience.DoesNotExist):
        raise ClickImportError("Invalid archive, explicit mapping, or campaign preparation scope") from None


@transaction.atomic
def apply_plan(archive, selected, explicit, client, audience, datacenter, list_id, apply, allow_create):
    # external_key is unique at client scope, including other audiences.
    campaigns = list(Campaign.objects.filter(client=client))
    mapping, rows, counts = {}, [], Counter()
    # Resolve every conflict before making any changes.
    planned = {campaign_id: find_existing(campaign_id, datacenter, list_id, campaigns, explicit, client, audience)
               for campaign_id in selected}
    for campaign_id, campaign in planned.items():
        if campaign is not None:
            validate_owned_history(campaign, selected[campaign_id], archive)
    for campaign_id, source in selected.items():
        campaign = planned[campaign_id]
        action = "existing" if campaign else "missing"
        if campaign is None and allow_create:
            action = "would_create"
            if apply:
                campaign = Campaign.objects.create(
                    client=client, audience=audience, external_key=f"mailchimp-{datacenter}-{campaign_id}",
                    status=CampaignStatus.SENT, subject=source["subject"][:255], sent_at=source["sent_at"],
                    click_count=source["source_click_count"], unique_click_count=source["source_unique_click_count"],
                    metadata={"historical_import": True, "provider": "mailchimp", "mailchimp": {
                        "campaign_id": campaign_id, "datacenter": datacenter, "list_id": list_id,
                        "campaign_title": source["campaign_title"], "subject": source["subject"],
                        "archive_manifest_sha256": archive.manifest_sha256,
                        "window": archive.manifest["window"],
                        "count_basis": "Available in-window click events; lifetime send/delivery totals not imported",
                    }},
                )
                action = "created"
        if campaign is not None:
            mapping[campaign_id] = campaign.pk
        counts[action] += 1
        rows.append({**source, "sent_at": source["sent_at"].isoformat(), "action": action,
                     "relay_campaign_id": campaign.pk if campaign else None,
                     "external_key": campaign.external_key if campaign else f"mailchimp-{datacenter}-{campaign_id}"})
    report = {"dry_run": not apply, "allow_create": allow_create, "list_id": list_id,
              "window": archive.manifest["window"], "manifest_sha256": archive.manifest_sha256,
              "client_id": client.pk, "audience_id": audience.pk, "counts": counts, "campaigns": rows}
    return mapping, report
