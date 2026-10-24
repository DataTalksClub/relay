import gzip
import io
import json
import zipfile
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.test import Client as HttpClient
from django.utils import timezone

from mailing.models import (
    BlockedSender,
    DmarcArchive,
    DmarcAttachment,
    DmarcRecord,
    DmarcReport,
    InboundDelivery,
    InboundDomain,
    TransactionalMessage,
)
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.dmarc import MAX_XML_BYTES, is_dmarc_report, mime_message
from mailing.services.inbound_routing import process_due_deliveries
from mailing.tests.test_inbound_routing import ingest, mailbox

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain():
    from mailing.models import Client, Organization  # noqa: PLC0415
    org = Organization.objects.create(name='DMARC', slug='dmarc')
    client = Client.objects.create(organization=org, name='DMARC', slug='dmarc')
    return InboundDomain.objects.create(name='example.com', client=client, from_email='relay@example.com')
XML = b'''<?xml version="1.0"?><feedback><report_metadata><org_name>Reporter</org_name><report_id>ac16-test</report_id><date_range><begin>1792713600</begin><end>1792799999</end></date_range></report_metadata><policy_published><domain>example.com</domain><adkim>r</adkim><aspf>r</aspf><p>none</p><sp>none</sp><pct>100</pct></policy_published><record><row><source_ip>192.0.2.16</source_ip><count>3</count><policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>pass</spf></policy_evaluated></row><identifiers><header_from>example.com</header_from></identifiers><auth_results><dkim><domain>example.com</domain><result>pass</result><selector>test</selector></dkim><spf><domain>example.com</domain><result>pass</result></spf></auth_results></record></feedback>'''


def report_mail(kind='zip', sender='dmarc-noreply@google.com', multipart=True, payload=None):
    if payload is None:
        if kind == 'zip':
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('report.xml', XML)
            payload = stream.getvalue()
        elif kind == 'xml.gz':
            payload = gzip.compress(XML)
        else:
            payload = XML
    message = EmailMessage()
    message['From'] = sender
    message['To'] = 'team@example.com'
    message['Subject'] = 'Prüfung 日本語 — Report Domain: example.com'
    message['Message-ID'] = '<dmarc-proof@example.net>'
    if multipart:
        message.set_content('Aggregate report')
        message.add_alternative('<p>Aggregate report</p>', subtype='html')
        message.add_attachment(payload, maintype='application', subtype='zip' if kind == 'zip' else 'gzip' if kind.endswith('gz') else 'xml', filename='dmarc-example.' + kind)
    else:
        message.set_content(payload, maintype='application', subtype='zip' if kind == 'zip' else 'gzip')
        message.add_header('Content-Disposition', 'attachment', filename='example.' + kind)
    return message.as_bytes()


@pytest.mark.parametrize('sender', ['dmarc-noreply@google.com', 'noreply-dmarc-support@microsoft.com', 'dmarc-noreply@yahoo.com', 'noreply@datatalks.club'])
@pytest.mark.parametrize('kind,multipart', [('zip', True), ('zip', False), ('xml.gz', True), ('xml.gz', False), ('xml', True)])
def test_durable_archive_and_records_never_forward_or_wake(domain, sender, kind, multipart):
    mailbox(domain, kind='forward', local_part='team', destinations=['own@example.net'])
    bot = mailbox(domain, local_part='bot', sender_allowlist=[sender])
    raw = report_mail(kind, sender, multipart)
    with patch('mailing.services.inbound_routing.post_webhook') as post:
        ingest(raw, ['team@example.com', 'bot@example.com'])
        process_due_deliveries()
        post.assert_not_called()
    archive = DmarcArchive.objects.get()
    assert bytes(archive.raw_eml) == raw
    assert archive.retain_until >= timezone.now() + timedelta(days=364)
    assert archive.raw_key.startswith('dmarc/' + sender.split('@')[1] + '/')
    attachment = DmarcAttachment.objects.get()
    assert bytes(attachment.payload)
    assert attachment.storage_key.startswith('dmarc-zips/' + sender.split('@')[1] + '/')
    report = DmarcReport.objects.get()
    assert report.reporter_org == 'Reporter' and report.report_id == 'ac16-test'
    assert report.policy['domain'] == 'example.com'
    record = DmarcRecord.objects.get()
    assert (record.source_ip, record.count, record.dkim, record.spf, record.disposition) == ('192.0.2.16', 3, 'pass', 'pass', 'none')
    assert record.auth_results['dkim'][0]['selector'] == 'test'
    assert InboundDelivery.objects.filter(reason='dmarc_archived', status='suppressed').count() == 2
    assert not TransactionalMessage.objects.exists()
    assert not InboundDelivery.objects.filter(mailbox=bot, status='pending').exists()
    assert ingest(raw, ['team@example.com', 'bot@example.com'])[0][0]['replay']
    assert DmarcArchive.objects.count() == DmarcRecord.objects.count() == 1


def test_shared_store_domains_discard_and_blocked_sender(domain):
    other = InboundDomain.objects.create(name='other.com', client=domain.client, from_email='relay@other.com')
    BlockedSender.objects.create(scope='domain', value='google.com')
    ingest(report_mail(), ['team@example.com', 'dmarc-reports@other.com'])
    assert set(DmarcArchive.objects.values_list('domain_id', flat=True)) == {domain.pk, other.pk}
    assert set(InboundDelivery.objects.values_list('reason', flat=True)) == {'dmarc_archived'}


@pytest.mark.parametrize('payload', [b'<broken', b'<!DOCTYPE feedback [<!ENTITY x "bad">]><feedback>&x;</feedback>', gzip.compress(b'x' * (MAX_XML_BYTES + 1)), b'not gzip'])
def test_malformed_evidence_retained_without_dispatch(domain, payload):
    mailbox(domain, kind='forward', local_part='team', destinations=['own@example.net'])
    raw = report_mail('xml.gz', payload=payload)
    ingest(raw)
    assert bytes(DmarcArchive.objects.get().raw_eml) == raw
    assert bytes(DmarcAttachment.objects.get().payload) == payload
    assert DmarcAttachment.objects.get().parse_error
    assert not DmarcReport.objects.exists()
    assert InboundDelivery.objects.get().status == 'suppressed'


def test_xml_entities_plain_rejected_and_atomic(domain):
    raw = report_mail('xml', payload=b'<!DOCTYPE feedback [<!ENTITY x "bad">]><feedback>&x;</feedback>')
    ingest(raw)
    assert DmarcAttachment.objects.get().parse_error == 'ValueError'
    assert not DmarcReport.objects.exists()


def test_lambda_sender_substring_subject_and_attachment_heuristics():
    for sender, subject, filename, expected in [
        ('prefix-dmarc-noreply@google.com', 'ordinary', 'report.zip', True),
        ('someone@example.net', 'DMARC Aggregation for example.com', 'report.zip', True),
        ('someone@example.net', 'ordinary', 'dmarc.zip', True),
        ('someone@example.net', 'ordinary', 'invoice.zip', False),
    ]:
        mime = mime_message(report_mail(sender=sender))
        mime.replace_header('Subject', subject)
        attachment = list(mime.iter_attachments())[0]
        attachment.set_param('filename', filename, header='Content-Disposition')
        assert is_dmarc_report(mime) is expected


def test_admin_api_cli_authorization_download_and_readonly(domain, django_user_model):
    ingest(report_mail())
    archive = DmarcArchive.objects.get()
    client = HttpClient()
    path = '/api/admin/inbound-dmarc'
    assert client.get(path).status_code in (401, 403)
    user = django_user_model.objects.create_user(username='dmarc-admin', is_staff=True, is_superuser=True)
    _, raw_key = create_admin_api_key(user=user, name='dmarc-test')
    headers = {'HTTP_AUTHORIZATION': 'Bearer ' + raw_key}
    response = client.get(path + f'/{archive.pk}', **headers)
    assert response.status_code == 200
    assert response.json()['attachments'][0]['reports'][0]['records'][0]['count'] == 3
    assert 'raw_eml' not in response.json()
    assert client.get(path + '?domain=absent.com', **headers).json()['results'] == []
    assert client.get(path + f'/{archive.pk}/raw', **headers).content == bytes(archive.raw_eml)
    attachment = DmarcAttachment.objects.get()
    assert client.get(path + f'/{archive.pk}/attachments/{attachment.pk}', **headers).content == bytes(attachment.payload)
    assert client.post(path, data={}, content_type='application/json', **headers).status_code == 405
    output = io.StringIO()
    call_command('dmarc_reports', id=archive.pk, stdout=output)
    assert json.loads(output.getvalue())['attachments'][0]['reports'][0]['report_id'] == 'ac16-test'
    request = type('Request', (), {'user': user})()
    for model in (DmarcArchive, DmarcAttachment, DmarcReport, DmarcRecord):
        assert not admin.site._registry[model].has_delete_permission(request)


def test_archive_storage_failure_retries_instead_of_dispatching(domain):
    mailbox(domain, kind='forward', local_part='team', destinations=['own@example.net'])
    with patch('mailing.services.dmarc.DmarcArchive.objects.get_or_create', side_effect=RuntimeError('storage unavailable')):
        with pytest.raises(RuntimeError):
            ingest(report_mail())
    assert not InboundDelivery.objects.exists()
    assert not DmarcArchive.objects.exists()
    assert not TransactionalMessage.objects.exists()
    ingest(report_mail())
    assert DmarcRecord.objects.count() == 1
    assert InboundDelivery.objects.get().reason == 'dmarc_archived'


def test_zip_multiple_reports_is_atomic_on_invalid_member(domain):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('../../report.xml', XML)
        archive.writestr('bad.xml', b'not xml')
    ingest(report_mail(payload=stream.getvalue()))
    assert bytes(DmarcAttachment.objects.get().payload) == stream.getvalue()
    assert DmarcArchive.objects.get().parse_error == 'attachment_parse_error'
    assert not DmarcReport.objects.exists()
    assert not DmarcRecord.objects.exists()
    assert InboundDelivery.objects.get().reason == 'dmarc_archived'
