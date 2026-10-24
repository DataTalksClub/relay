"""AISHL Lambda detection port and durable, bounded aggregate XML ingestion.

Detection is ported from aws-infra main/aisl/lambdas/email-forwarder.py.
Database evidence has no expiry job; retain_until is a minimum, not a TTL.
"""
import gzip
import hashlib
import io
import logging
import re
import zipfile
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from xml.etree import ElementTree

from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.utils import timezone

from mailing.models import DmarcArchive, DmarcAttachment, DmarcRecord, DmarcReport

logger = logging.getLogger(__name__)
DMARC_SENDERS = {
    'dmarc-noreply@google.com', 'dmarc-noreply@yahoo.com',
    'noreply-dmarc-support@microsoft.com', 'dmarcreport@microsoft.com',
    'auto-dmarc@icloud.com', 'dmarc-reports@proofpoint.com', 'dmarc@returnpath.net',
}
DMARC_SUBJECT_PATTERNS = [r'report domain', r'dmarc report', r'dmarc aggregation']
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_ZIP_MEMBERS = 32
MAX_RECORDS = 25000


def is_dmarc_report(original):
    """Lambda heuristic, including its sender substring and suffix semantics."""
    sender = original.get('From', '').lower()
    sender_email = sender.split('<')[-1].rstrip('>').strip() if '<' in sender else sender
    if any(dmarc_sender in sender_email for dmarc_sender in DMARC_SENDERS):
        return True
    subject = original.get('Subject', '').lower()
    if any(re.search(pattern, subject) for pattern in DMARC_SUBJECT_PATTERNS):
        return True
    if original.is_multipart():
        for part in original.walk():
            filename = part.get_filename() or ''
            if filename.endswith(('.zip', '.xml.gz', '.xml')) and 'dmarc' in filename.lower():
                return True
    return False


def mime_message(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def sender_domain(original):
    _, addr = parseaddr(original.get('From', ''))
    if '@' not in addr:
        return 'unknown'
    return re.sub(r'[^\w\-\.]', '_', addr.split('@', 1)[1].lower()) or 'unknown'


def bounded_read(stream):
    data = stream.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError('xml_size_limit')
    return data


def xml_documents(payload, filename):
    if filename.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_XML_BYTES:
                raise ValueError('zip_size_limit')
            for member in members:
                if member.filename.lower().endswith('.xml') and not member.is_dir():
                    with archive.open(member) as stream:
                        yield member.filename, bounded_read(stream)
    elif filename.endswith('.gz'):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            yield filename, bounded_read(stream)
    else:
        yield filename, bounded_read(io.BytesIO(payload))


def child_values(element):
    return {child.tag: child.text or '' for child in element} if element is not None else {}


def parse_report(attachment, member_name, xml):
    # Reject declarations before ElementTree can expand entities, including UTF-16 XML.
    probe = xml.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
        raise ValueError('xml_declaration_forbidden')
    root = ElementTree.fromstring(xml)
    # Aggregate reports sometimes carry a default XML namespace.
    for element in root.iter():
        element.tag = element.tag.rsplit('}', 1)[-1]
    if root.tag != 'feedback':
        raise ValueError('not_aggregate_feedback')
    metadata = root.find('report_metadata')
    if metadata is None:
        raise ValueError('missing_metadata')
    report = DmarcReport.objects.create(
        attachment=attachment, member_name=member_name[:1024],
        reporter_org=metadata.findtext('org_name', '')[:255],
        report_id=metadata.findtext('report_id', '')[:512],
        date_begin=datetime.fromtimestamp(int(metadata.findtext('date_range/begin')), UTC),
        date_end=datetime.fromtimestamp(int(metadata.findtext('date_range/end')), UTC),
        policy=child_values(root.find('policy_published')),
    )
    elements = root.findall('record')
    if len(elements) > MAX_RECORDS:
        raise ValueError('record_limit')
    records = []
    for element in elements:
        values = child_values(element.find('row/policy_evaluated'))
        row = DmarcRecord(
            report=report, source_ip=element.findtext('row/source_ip'),
            count=int(element.findtext('row/count')), disposition=values.get('disposition', '')[:64],
            dkim=values.get('dkim', '')[:64], spf=values.get('spf', '')[:64],
            identifiers=child_values(element.find('identifiers')),
            auth_results={kind: [child_values(e) for e in element.findall('auth_results/' + kind)] for kind in ('dkim', 'spf')},
        )
        validate_ipv46_address(row.source_ip)
        if row.count < 0 or row.count > 9223372036854775807:
            raise ValueError('invalid_count')
        records.append(row)
    DmarcRecord.objects.bulk_create(records)


def archive_report(stored, raw, matches):
    original = mime_message(raw)
    if not is_dmarc_report(original):
        return False
    folder = sender_domain(original)
    # Stable identity prevents replays and timestamp collisions from overwriting evidence.
    stamp = stored.created_at.strftime('%Y%m%d-%H%M%S') + '-' + hashlib.sha256(raw).hexdigest()[:16]
    for domain_id in sorted({mailbox.domain_id for mailbox, _ in matches}):
        archive, created = DmarcArchive.objects.get_or_create(message=stored, domain_id=domain_id, defaults={
            'reporter_domain': folder, 'raw_eml': raw, 'raw_key': f'dmarc/{folder}/{stamp}.eml',
            'retain_until': timezone.now() + timedelta(days=365),
        })
        if not created:
            continue
        for part in original.walk():
            filename = part.get_filename() or ''
            if part.is_multipart() or part.get_content_maintype() != 'application':
                continue
            payload = part.get_payload(decode=True)
            if not payload or not filename.endswith(('.zip', '.xml.gz', '.gz', '.xml')):
                continue
            attachment = DmarcAttachment.objects.create(
                archive=archive, filename=filename[:1024], payload=payload,
                storage_key=f'dmarc-zips/{folder}/{stamp}-{filename}'[:2048],
            )
            try:
                with transaction.atomic():
                    found = False
                    for member, xml in xml_documents(payload, filename):
                        parse_report(attachment, member, xml)
                        found = True
                    if not found:
                        raise ValueError('no_xml_members')
            except Exception as exc:
                # Preserve malformed evidence and suppression; never log XML/exception text.
                attachment.parse_error = type(exc).__name__[:64]
                attachment.save(update_fields=['parse_error'])
                archive.parse_error = 'attachment_parse_error'
                archive.save(update_fields=['parse_error'])
                logger.error('dmarc_parse_failed archive_id=%s attachment_id=%s error_type=%s', archive.pk, attachment.pk, type(exc).__name__)
        if not archive.attachments.exists():
            archive.parse_error = 'no_report_attachment'
            archive.save(update_fields=['parse_error'])
        logger.info('dmarc_archived archive_id=%s message_id=%s domain_id=%s', archive.pk, stored.pk, domain_id)
    return True
