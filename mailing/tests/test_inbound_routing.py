import io
import json
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client as HttpClient
from django.utils import timezone

from mailing.inbound_mime import parse_mime
from mailing.models import InboundDelivery, InboundDomain, InboundMailbox, InboundMessage, OperatorAudit
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.inbound_email import process_inbound_object, process_inbound_s3_notification
from mailing.services.inbound_routing import match_mailboxes, process_due_deliveries, routing_health
from mailing.services.transactional import build_transactional_queue_payload
from mailing.services.transactional_sender import send_transactional_email_from_queue
from mailing.tests.test_inbound_email import FakeS3

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain():
    from mailing.models import Client, Organization  # noqa: PLC0415
    org = Organization.objects.create(name='Routing', slug='routing')
    client = Client.objects.create(organization=org, name='Routing', slug='routing')
    return InboundDomain.objects.create(name='example.com', client=client, from_email='relay@example.com')


def mailbox(domain, kind='bot', **kwargs):
    row = InboundMailbox(domain=domain, local_part=kwargs.pop('local_part', 'bot'), kind=kind,
                         sender_allowlist=kwargs.pop('sender_allowlist', ['alexey@example.net']), webhook_secret_id='env:ROUTING_TEST_SECRET', **kwargs)
    row.full_clean()
    row.save()
    return row


def mime(to='bot@example.com', sender='Alexey <alexey@example.net>', headers=None):
    message = EmailMessage()
    message['From'] = sender
    message['To'] = to
    message['Subject'] = 'Prüfung — 日本語'
    message['Message-ID'] = '<routing-test@example.net>'
    message['Date'] = 'Sat, 24 Oct 2026 12:00:00 +0200'
    message['References'] = '<original@example.net>'
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content('Grüße! 日本語')
    message.add_alternative('<p>Grüße! 日本語</p>', subtype='html')
    message.add_attachment(b'\x00\xffbinary-pdf', maintype='application', subtype='pdf', filename='Prüfung.pdf')
    return message.as_bytes()


def ingest(raw, recipients=None):
    s3 = FakeS3(raw)
    if recipients is not None:
        result = process_inbound_s3_notification({'notificationType': 'Received', 'receipt': {
            'recipients': recipients, 'action': {'type': 'S3', 'bucketName': 'bucket', 'objectKey': 'raw/test'},
        }}, s3=s3)
    else:
        result = process_inbound_object('bucket', 'raw/test', s3=s3)
    return result, s3


def test_domain_default_unique_and_multi_domain(domain):
    default = domain.mailboxes.get(local_part='*')
    assert default.kind == 'discard'
    assert match_mailboxes(['unknown@example.com']) == [(default, 'unknown@example.com')]
    assert match_mailboxes(['bot@unknown.net']) == []
    other = InboundDomain.objects.create(name='other.com', client=domain.client, from_email='relay@other.com')
    box = mailbox(other)
    assert match_mailboxes(['bot@other.com'])[0][0] == box
    with pytest.raises(IntegrityError), transaction.atomic():
        InboundMailbox.objects.create(domain=domain, local_part='*', kind='discard')


def test_bot_and_catchall_and_unknown(domain):
    box = mailbox(domain)
    result, _ = ingest(mime())
    delivery = InboundDelivery.objects.get(mailbox=box)
    assert delivery.status == 'pending'
    assert delivery.message.subject == 'Prüfung — 日本語'
    assert delivery.message.attachment_count == 1
    assert result['inbound_message_id'] == delivery.message_id
    replay, _ = ingest(mime())
    assert replay['replay']
    assert InboundDelivery.objects.count() == 1
    assert ingest(mime(to='other@unknown.net'))[0]['discarded'] == 'no_matching_address'


def test_envelope_recipient_and_multiple_routes(domain):
    bot = mailbox(domain)
    forward = mailbox(domain, kind='forward', local_part='forward', destinations=['destination@example.net'])
    ingest(mime(to='private@elsewhere.net'), ['bot@example.com', 'forward@example.com'])
    assert set(InboundDelivery.objects.values_list('mailbox_id', flat=True)) == {bot.pk, forward.pk}
    assert InboundDelivery.objects.get(mailbox=bot).recipient == 'bot@example.com'


def test_sender_allowlist_records_refusal(domain):
    mailbox(domain)
    ingest(mime(sender='Stranger <stranger@example.net>'))
    row = InboundDelivery.objects.get()
    assert (row.status, row.reason) == ('suppressed', 'sender_not_allowed')
    with patch('mailing.services.inbound_routing.post_webhook') as post:
        process_due_deliveries()
    post.assert_not_called()


@pytest.mark.parametrize('headers,sender,reason', [
    ({'X-Loop': 'relay-inbound'}, 'alexey@example.net', 'loop_header'),
    ({'X-Relay-Inbound': 'forward'}, 'alexey@example.net', 'loop_header'),
    ({'Auto-Submitted': 'auto-replied'}, 'alexey@example.net', 'auto_reply'),
    ({}, 'relay@example.com', 'own_sender'),
    ({}, 'mailer-daemon@example.net', 'bounce_sender'),
])
def test_loop_protection(domain, headers, sender, reason):
    mailbox(domain)
    ingest(mime(sender=sender, headers=headers))
    row = InboundDelivery.objects.get()
    assert (row.status, row.reason) == ('suppressed', reason)


@pytest.mark.parametrize('sender,subject', [
    ('DMARC <dmarc-noreply@google.com>', 'Prüfung — 日本語'),
    ('Alexey <alexey@example.net>', 'Report domain example.com submitted by Google'),
    ('Alexey <alexey@example.net>', 'DMARC aggregation for example.com'),
])
def test_dmarc_report_archived_not_forwarded(domain, sender, subject):
    # Migration difference vs legacy Lambda (see docs/inbound-routing.md):
    # legacy saved a second copy to the separate dmarc-reports bucket
    # (365-day retention, versioned, IA at 30 days); Relay keeps the single
    # raw MIME in the ingress bucket (7-day lifecycle) plus the durable
    # suppressed delivery row, and never forwards. Behaviour parity is
    # archive-only + no-forward; retention parity needs the proposed
    # dmarc-prefix lifecycle/copy follow-up, not claimed here.
    box = mailbox(domain, kind='forward', local_part='team',
                  destinations=['alexey@example.net'], sender_allowlist=[])
    raw_message = EmailMessage()
    raw_message['From'] = sender
    raw_message['To'] = 'team@example.com'
    raw_message['Subject'] = subject
    raw_message['Message-ID'] = '<dmarc-report@example.net>'
    raw_message['Date'] = 'Sat, 24 Oct 2026 12:00:00 +0200'
    raw_message.set_content('dmarc aggregate report')
    ingest(raw_message.as_bytes())
    row = InboundDelivery.objects.get(mailbox=box)
    assert (row.status, row.reason) == ('suppressed', 'dmarc_archived')
    assert InboundMessage.objects.filter(message_id='<dmarc-report@example.net>').exists()
    with patch('mailing.services.inbound_routing.post_webhook') as post:
        process_due_deliveries()
    post.assert_not_called()
    assert InboundDelivery.objects.get(pk=row.pk).status == 'suppressed'


def test_auto_generated_alert_allowed_and_rate_cap(domain):
    box = mailbox(domain, rate_per_minute=1)
    ingest(mime(headers={'Auto-Submitted': 'auto-generated'}))
    assert InboundDelivery.objects.get().status == 'pending'
    ingest(mime().replace(b'<routing-test@example.net>', b'<second@example.net>'))
    assert InboundDelivery.objects.filter(mailbox=box, reason='rate_cap', status='suppressed').count() == 1


def test_webhook_payload_auth_secret_redaction_and_2xx(domain, monkeypatch, settings):
    mailbox(domain)
    ingest(mime())
    monkeypatch.setenv('ROUTING_TEST_SECRET', json.dumps({'url': 'https://receiver.example.net/private-token', 'header': 'X-Auth', 'value': 'private-auth'}))
    settings.PUBLIC_BASE_URL = 'https://relay.example.net'
    response = MagicMock(status=204)
    response.__enter__.return_value = response
    with patch('mailing.services.inbound_routing.build_opener') as opener:
        opener.return_value.open.return_value = response
        process_due_deliveries()
    request = opener.return_value.open.call_args.args[0]
    assert request.get_header('X-auth') == 'private-auth'
    payload = json.loads(request.data)
    row = InboundDelivery.objects.get()
    assert payload == {'from': 'Alexey <alexey@example.net>', 'to': 'bot@example.com',
                       'subject': 'Prüfung — 日本語', 'snippet': 'Grüße! 日本語',
                       'message_id': '<routing-test@example.net>',
                       'message_url': f'https://relay.example.net/inbound/messages/{row.message_id}/raw/'}
    assert row.status == 'delivered' and row.response_status == 204
    assert routing_health()['status'] == 'OK'


def test_webhook_retries_backoff_and_failure_health_alert(domain, caplog):
    mailbox(domain)
    ingest(mime())
    with patch('mailing.services.inbound_routing.post_webhook', side_effect=RuntimeError('secret-url private-auth')):
        for attempt in range(1, 6):
            started = timezone.now()
            process_due_deliveries()
            row = InboundDelivery.objects.get()
            assert row.attempt_count == attempt
            if attempt < 5:
                assert row.status == 'pending'
                assert row.next_attempt_at >= started + timedelta(seconds=30 * 2 ** (attempt - 1))
                InboundDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    assert row.status == 'failed'
    assert routing_health()['status'] == 'FAIL'
    assert 'inbound_delivery_failed' in caplog.text
    assert 'secret-url' not in caplog.text and 'private-auth' not in caplog.text
    health = HttpClient().get('/health/')
    assert health.status_code == 503 and health.json()['inbound_routing']['failed'] == 1


def test_forward_uses_outbound_pipeline_with_original_mime(domain, django_capture_on_commit_callbacks):
    box = mailbox(domain, kind='forward', destinations=['destination@example.net'])
    raw = mime()
    ingest(raw)
    with patch('mailing.services.inbound_routing.enqueue_transactional_email') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            process_due_deliveries()
    row = InboundDelivery.objects.get(mailbox=box)
    outgoing = row.outbound_message
    assert outgoing is not None
    assert enqueue.call_args.args[0]['transactional_message_id'] == outgoing.pk
    assert outgoing.metadata['reply_to'] == 'Alexey <alexey@example.net>'
    provider = MagicMock()
    provider.send_raw_email.return_value = {'MessageId': 'provider-proof-id'}
    with patch('mailing.services.inbound_routing.s3_client') as s3:
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(raw)}
        send_transactional_email_from_queue(build_transactional_queue_payload(outgoing), client=provider)
    sent = BytesParser(policy=policy.default).parsebytes(provider.send_raw_email.call_args.kwargs['RawMessage']['Data'])
    assert str(sent['Reply-To']) == 'Alexey <alexey@example.net>'
    assert 'relay@example.com' in str(sent['From'])
    assert str(sent['Subject']) == 'Prüfung — 日本語'
    assert sent['X-Original-Message-ID'] == '<routing-test@example.net>'
    assert sent['X-Original-References'] == '<original@example.net>'
    assert sent['X-Original-Date']
    assert sent['X-Loop'] == 'relay-inbound'
    attachment = list(sent.iter_attachments())[0]
    assert attachment.get_filename() == 'Prüfung.pdf' and attachment.get_payload(decode=True) == b'\x00\xffbinary-pdf'
    assert parse_mime(sent.as_bytes()).body_text.startswith('Grüße! 日本語')
    process_due_deliveries() # reconcile provider result after the polling delay
    InboundDelivery.objects.update(next_attempt_at=timezone.now())
    process_due_deliveries()
    row.refresh_from_db()
    assert row.status == 'delivered'
    outgoing.refresh_from_db()
    assert outgoing.ses_message_id == 'provider-proof-id'


def test_forward_to_original_sender_suppressed(domain):
    mailbox(domain, kind='forward', destinations=['alexey@example.net'])
    ingest(mime())
    assert InboundDelivery.objects.get().reason == 'original_sender'


def test_admin_api_audit_and_secret_reference_only(domain):
    user = get_user_model().objects.create_user('routing-admin', is_staff=True)
    _, key = create_admin_api_key(user=user, name='routing')
    http = HttpClient(HTTP_AUTHORIZATION=f'Bearer {key}')
    data = {'domain_id': domain.pk, 'local_part': 'bot', 'kind': 'bot',
            'sender_allowlist': ['alexey@example.net'], 'webhook_secret_id': 'env:ROUTING_TEST_SECRET'}
    response = http.post('/api/admin/inbound-mailboxes', json.dumps(data), content_type='application/json')
    assert response.status_code == 201, response.content
    record = response.json()
    assert record['webhook_secret_id'] == 'env:ROUTING_TEST_SECRET'
    assert OperatorAudit.objects.filter(action='inbound_routing.create').exists()
    assert http.get('/api/admin/inbound-mailboxes').json()['results']
    assert HttpClient().get('/api/admin/inbound-mailboxes').status_code == 401
    data['local_part'] = 'bad'
    data['webhook_secret_id'] = 'https://secret.example.net/token'
    assert http.post('/api/admin/inbound-mailboxes', json.dumps(data), content_type='application/json').status_code == 400
    assert HttpClient().get('/inbound/messages/1/raw/').status_code == 302


def test_config_validation(domain):
    with pytest.raises(ValidationError):
        mailbox(domain, kind='forward')
    with pytest.raises(ValidationError):
        mailbox(domain, sender_allowlist='anyone')


def test_nested_message_and_inline_binary_parsing():
    parent = EmailMessage()
    parent['Subject'] = 'Parent'
    parent.set_content('Only parent body')
    child = EmailMessage()
    child['Subject'] = 'Attached message'
    child.set_content('Private child body')
    parent.add_attachment(child, filename='nested.eml')
    parent.add_attachment(b'\x89PNG', maintype='image', subtype='png', disposition='inline', cid='<image>')
    parsed = parse_mime(parent.as_bytes())
    assert parsed.body_text.strip() == 'Only parent body'
    assert len(parsed.attachments) == 2
    assert b'Private child body' in parsed.attachments[0].payload
    assert parsed.attachments[1].content_id == 'image'
    assert parsed.attachments[1].payload == b'\x89PNG'


def test_webhook_refuses_redirects():
    from mailing.services.inbound_routing import NoRedirect  # noqa: PLC0415
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.net') is None


def test_allowlist_revocation_before_dispatch(domain):
    box = mailbox(domain)
    ingest(mime())
    box.sender_allowlist = []
    box.save()
    with patch('mailing.services.inbound_routing.post_webhook') as post:
        process_due_deliveries()
    post.assert_not_called()
    assert InboundDelivery.objects.get().reason == 'sender_not_allowed'


def test_provision_manifest_is_validated_atomic_and_audited(domain, tmp_path):
    from django.core.management import call_command  # noqa: PLC0415
    manifest = tmp_path / 'routes.json'
    manifest.write_text(json.dumps({'domains': [{'name': 'new.com', 'client': domain.client.slug,
        'from_email': 'relay@new.com', 'mailboxes': [{'local_part': 'forward', 'kind': 'forward',
        'destinations': ['alexey@example.net']}]}]}))
    call_command('provision_domain_routes', str(manifest), stdout=io.StringIO())
    assert not InboundDomain.objects.filter(name='new.com').exists()
    call_command('provision_domain_routes', str(manifest), apply=True, stdout=io.StringIO())
    assert InboundDomain.objects.get(name='new.com').mailboxes.count() == 2
    assert OperatorAudit.objects.filter(action='inbound_routing.provision').count() == 2
