"""Bulk-import historical DMARC reports from S3 or a local directory.

Reads .eml, .xml, .xml.gz, .gz and .zip objects (S3 bucket/prefix or local
dir), parses them with the AC16 parser (mailing.services.dmarc) and stores
them in the shared DMARC store. Idempotent: a report whose
(report_id, reporter_org) already exists is skipped; re-running imports 0 new
reports. Parse errors are recorded per key and never fatal.

Example:
    uv run python manage.py import_dmarc_reports --bucket my-bucket
    uv run python manage.py import_dmarc_reports --local-dir /tmp/dmarc --dry-run
"""

import hashlib
import json
import os
from datetime import timedelta
from xml.etree import ElementTree

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

ALLOWED_SUFFIXES = (".eml", ".xml", ".xml.gz", ".gz", ".zip")


def has_allowed_suffix(name):
    lowered = name.lower()
    return lowered.endswith(ALLOWED_SUFFIXES)


def peek_metadata(xml):
    """Report identity + policy domain without touching the database."""
    probe = xml.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise ValueError("xml_declaration_forbidden")
    root = ElementTree.fromstring(xml)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag != "feedback":
        raise ValueError("not_aggregate_feedback")
    metadata = root.find("report_metadata")
    if metadata is None:
        raise ValueError("missing_metadata")
    policy = root.find("policy_published")
    domain = (policy.findtext("domain", "") if policy is not None else "").strip()[:255]
    return {
        "report_id": (metadata.findtext("report_id", "") or "")[:512],
        "reporter_org": (metadata.findtext("org_name", "") or "")[:255],
        "policy_domain": domain,
    }


class Command(BaseCommand):
    help = "Import historical DMARC reports from S3 or a local directory (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--bucket", help="S3 bucket to scan.")
        parser.add_argument("--prefix", action="append", default=[], help="S3 prefix to scan (repeatable).")
        parser.add_argument("--local-dir", help="Local directory to scan recursively.")
        parser.add_argument("--domain", help="Fallback inbound domain when the report names none.")
        parser.add_argument("--dry-run", action="store_true", help="Scan and report without writing.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N objects (0 = no limit).")
        parser.add_argument("--s3-endpoint-url", default="", help="Override S3 endpoint (tests).")

    def handle(self, *args, **options):
        from mailing.models import DmarcReport, InboundDomain  # noqa: PLC0415
        from mailing.services.dmarc import is_dmarc_report, mime_message, sender_domain, xml_documents  # noqa: PLC0415

        bucket, local_dir = options["bucket"], options["local_dir"]
        if not bucket and not local_dir:
            raise CommandError("Pass --bucket and/or --local-dir.")
        if local_dir and not os.path.isdir(local_dir):
            raise CommandError(f"Local dir not found: {local_dir}")

        candidates = []
        if local_dir:
            for root, _, files in os.walk(local_dir):
                for name in sorted(files):
                    if not has_allowed_suffix(name):
                        continue
                    path = os.path.join(root, name)
                    with open(path, "rb") as handle:
                        candidates.append((os.path.relpath(path, local_dir), name, handle.read()))
        if bucket:
            candidates.extend(self._list_s3(bucket, options["prefix"], options["s3_endpoint_url"]))
        candidates.sort(key=lambda item: item[0])
        if options["limit"] and options["limit"] > 0:
            candidates = candidates[: options["limit"]]

        stats = {"scanned": 0, "imported": 0, "duplicates": 0, "skipped": 0, "errors": {}}
        for key, filename, payload in candidates:
            stats["scanned"] += 1
            try:
                if filename.lower().endswith(".eml"):
                    result = self._import_eml(
                        key,
                        payload,
                        options,
                        is_dmarc_report,
                        mime_message,
                        sender_domain,
                        xml_documents,
                        DmarcReport,
                        InboundDomain,
                    )
                else:
                    result = self._import_payload(
                        key,
                        filename,
                        payload,
                        folder=self._folder_from_key(key),
                        options=options,
                        xml_documents=xml_documents,
                        DmarcReport=DmarcReport,
                        InboundDomain=InboundDomain,
                    )
                stats[result] += 1
            except Exception as exc:  # noqa: BLE001 - recorded per key, never fatal
                stats["errors"][key] = type(exc).__name__
        self.stdout.write(json.dumps(stats))

    def _list_s3(self, bucket, prefixes, endpoint_url):
        import boto3  # noqa: PLC0415

        kwargs = {"endpoint_url": endpoint_url} if endpoint_url else {}
        client = boto3.client("s3", **kwargs)
        found = []
        work = list(prefixes) or [""]
        for prefix in work:
            token = None
            while True:
                call = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    call["ContinuationToken"] = token
                page = client.list_objects_v2(**call)
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/") or not has_allowed_suffix(key):
                        continue
                    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                    found.append((key, key.rsplit("/", 1)[-1], body))
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        return found

    @staticmethod
    def _folder_from_key(key):
        parts = key.split("/")
        if len(parts) >= 3 and parts[0] in ("dmarc", "dmarc-zips"):
            return parts[1][:255]
        return "import"

    def _import_eml(
        self,
        key,
        payload,
        options,
        is_dmarc_report,
        mime_message,
        sender_domain,
        xml_documents,
        DmarcReport,
        InboundDomain,
    ):
        original = mime_message(payload)
        if not is_dmarc_report(original):
            return "skipped"  # scanned but not a DMARC report: nothing to import
        members = []
        for part in original.walk():
            filename = part.get_filename() or ""
            if part.is_multipart() or part.get_content_maintype() != "application":
                continue
            body = part.get_payload(decode=True)
            if not body or not has_allowed_suffix(filename) or filename.lower().endswith(".eml"):
                continue
            members.append((filename, body))
        if not members:
            raise ValueError("no_report_attachment")
        folder = sender_domain(original)
        imported = duplicates = 0
        first_error = None
        for filename, body in members:
            try:
                outcome = self._import_payload(
                    f"{key}#{filename}",
                    filename,
                    body,
                    folder=folder,
                    raw_eml=payload,
                    options=options,
                    xml_documents=xml_documents,
                    DmarcReport=DmarcReport,
                    InboundDomain=InboundDomain,
                )
            except Exception as exc:  # noqa: BLE001 - one bad attachment must not drop the rest
                first_error = exc
                continue
            if outcome == "imported":
                imported += 1
            else:
                duplicates += 1
        if imported:
            return "imported"
        if duplicates and first_error is None:
            return "duplicates"
        if first_error is not None:
            raise first_error
        raise ValueError("no_report_attachment")

    def _import_payload(
        self, key, filename, payload, *, folder, options, xml_documents, DmarcReport, InboundDomain, raw_eml=None
    ):
        from mailing.models import DmarcArchive, DmarcAttachment, InboundMessage  # noqa: PLC0415
        from mailing.services.dmarc import parse_report  # noqa: PLC0415

        try:
            members = list(xml_documents(payload, filename))
        except Exception as exc:
            raise ValueError(type(exc).__name__) from exc
        if not members:
            raise ValueError("no_xml_members")
        metas = []
        for _member, xml in members:
            try:
                metas.append(peek_metadata(xml))
            except Exception as exc:
                raise ValueError(type(exc).__name__) from exc
        fresh = [
            (member, xml, meta)
            for (member, xml), meta in zip(members, metas)
            if not DmarcReport.objects.filter(report_id=meta["report_id"], reporter_org=meta["reporter_org"]).exists()
        ]
        if not fresh:
            return "duplicates"
        if options["dry_run"]:
            return "imported"
        policy_domains = {meta["policy_domain"] for _, _, meta in fresh if meta["policy_domain"]}
        if options.get("domain"):
            policy_domains.add(options["domain"].lower())
        domain = None
        for name in sorted(policy_domains):
            domain = InboundDomain.objects.filter(name=name).first()
            if domain is not None:
                break
        if domain is None:
            raise ValueError("unknown_domain")
        digest = hashlib.sha256(payload).hexdigest()[:16]
        message_id = f"dmarc-import-{digest}@relay.local"
        with transaction.atomic():
            message, _ = InboundMessage.objects.get_or_create(
                message_id=message_id,
                defaults={
                    "subject": f"DMARC import {filename}",
                    "sender_address": f"dmarc-import@{folder}",
                    "sender_domain": folder,
                    "recipient_address": f"dmarc-reports@{domain.name}",
                    "recipient_domain": domain.name,
                    "size_bytes": len(raw_eml or payload),
                    "raw_bucket": "dmarc-import",
                    "raw_key": key[:1024],
                },
            )
            archive, _ = DmarcArchive.objects.get_or_create(
                message=message,
                domain=domain,
                defaults={
                    "reporter_domain": folder,
                    "raw_eml": raw_eml or payload,
                    "raw_key": key[:1024],
                    "retain_until": timezone.now() + timedelta(days=365),
                },
            )
            attachment = DmarcAttachment.objects.create(
                archive=archive,
                filename=filename[:1024],
                storage_key=key[:2048],
                payload=payload,
            )
            try:
                for member, xml, _meta in fresh:
                    parse_report(attachment, member[:1024], xml)
            except Exception as exc:
                attachment.parse_error = type(exc).__name__[:64]
                attachment.save(update_fields=["parse_error"])
                archive.parse_error = "attachment_parse_error"
                archive.save(update_fields=["parse_error"])
                raise ValueError(type(exc).__name__) from exc
        return "imported"
