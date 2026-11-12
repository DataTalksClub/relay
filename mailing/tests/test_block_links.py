import io
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client as HttpClient
from django.utils import timezone

from mailing.inbound_mime import parse_mime
from mailing.models import BlockedSender, InboundDelivery, InboundMessage
from mailing.services.block_links import (
    LINK_MAX_AGE_SECONDS,
    UNBLOCK_SALT,
    block_url,
    forward_footer,
    issue_token,
    read_token,
    refusal,
)
from mailing.services.inbound_routing import process_due_deliveries
from mailing.services.transactional import build_transactional_queue_payload
from mailing.services.transactional_sender import send_transactional_email_from_queue
from mailing.tests.test_inbound_routing import domain, ingest, mailbox, mime  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db

PITCH = {'Reply-To': 'Stefan <stefan.s@leads.example>'}


@pytest.fixture(autouse=True)
def link_base(settings):
    settings.INBOUND_BLOCK_LINK_BASE_URL = 'https://relay.test'


def forward(domain, raw):  # noqa: F811
    box = mailbox(domain, kind='forward', destinations=['destination@example.org'])
    ingest(raw)
    with patch('mailing.services.inbound_routing.enqueue_transactional_email'):
        process_due_deliveries()
    outgoing = InboundDelivery.objects.get(mailbox=box).outbound_message
    provider = MagicMock()
    provider.send_raw_email.return_value = {'MessageId': 'provider-id'}
    with patch('mailing.services.inbound_routing.s3_client') as s3:
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(raw)}
        send_transactional_email_from_queue(build_transactional_queue_payload(outgoing), client=provider)
    return BytesParser(policy=policy.default).parsebytes(provider.send_raw_email.call_args.kwargs['RawMessage']['Data'])


def path_of(url):
    return url.removeprefix('https://relay.test')


# AC1: the forward path honours the blocklist on From and Reply-To.

@pytest.mark.parametrize('scope,value', [
    ('address', 'stefan.s@leads.example'),
    ('domain', 'leads.example'),
    ('address', 'alexey@example.net'),
])
def test_blocked_from_or_reply_to_is_filed_without_body_and_not_forwarded(domain, scope, value):  # noqa: F811
    box = mailbox(domain, kind='forward', destinations=['destination@example.org'])
    BlockedSender.objects.create(scope=scope, value=value)
    result, s3 = ingest(mime(to='bot@example.com', headers=PITCH))
    row = InboundMessage.objects.get()
    assert result['state'] == row.state == 'blocked'
    assert row.blocked_rule.value == value
    assert not row.body_text_key and not row.body_html_key and s3.puts == []
    assert not InboundDelivery.objects.filter(mailbox=box).exists()


def test_block_added_after_ingest_suppresses_pending_forward(domain):  # noqa: F811
    box = mailbox(domain, kind='forward', destinations=['destination@example.org'])
    ingest(mime(to='bot@example.com'))
    BlockedSender.objects.create(scope='address', value='alexey@example.net')
    with patch('mailing.services.inbound_routing.enqueue_transactional_email') as enqueue:
        process_due_deliveries()
    row = InboundDelivery.objects.get(mailbox=box)
    assert (row.status, row.reason, row.outbound_message) == ('suppressed', 'blocked_sender', None)
    enqueue.assert_not_called()


@pytest.mark.parametrize('scope,value', [
    ('address', 'stefan.s@leads.example'),
    ('domain', 'leads.example'),
])
def test_reply_to_block_added_after_ingest_suppresses_pending_forward(domain, scope, value):  # noqa: F811
    box = mailbox(domain, kind='forward', destinations=['destination@example.org'])
    raw = mime(to='bot@example.com', headers=PITCH)
    ingest(raw)
    BlockedSender.objects.create(scope=scope, value=value)
    with patch('mailing.services.inbound_routing.s3_client') as s3, patch(
        'mailing.services.inbound_routing.enqueue_transactional_email'
    ) as enqueue:
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(raw)}
        process_due_deliveries()
    row = InboundDelivery.objects.get(mailbox=box)
    assert (row.status, row.reason, row.outbound_message) == ('suppressed', 'blocked_sender', None)
    enqueue.assert_not_called()


def test_pending_block_recheck_storage_failure_does_not_fail_dispatch(domain, caplog):  # noqa: F811
    box = mailbox(domain, kind='forward', destinations=['destination@example.org'])
    ingest(mime(to='bot@example.com', headers=PITCH))
    BlockedSender.objects.create(scope='address', value='unrelated@leads.example')
    with patch('mailing.services.inbound_routing.s3_client') as s3, patch(
        'mailing.services.inbound_routing.enqueue_transactional_email'
    ):
        s3.return_value.get_object.side_effect = RuntimeError('secret-storage-detail')
        process_due_deliveries()
    row = InboundDelivery.objects.get(mailbox=box)
    assert row.outbound_message is not None and row.status == 'pending'
    assert row.attempt_count == 0
    assert 'inbound_block_recheck_unavailable' in caplog.text
    assert 'secret-storage-detail' not in caplog.text


# AC2/AC3: footer links, guards.

def test_forward_footer_links_reply_to_and_domain_and_keeps_attachments(domain):  # noqa: F811
    sent = forward(domain, mime(to='bot@example.com', headers=PITCH))
    text = sent.get_body(('plain',)).get_content()
    markup = sent.get_body(('html',)).get_content()
    assert text.startswith('Grüße! 日本語')
    assert 'Block this sender (stefan.s@leads.example): https://relay.test/inbound/block/' in text
    assert 'Block domain leads.example: https://relay.test/inbound/block/' in text
    assert markup.count('/inbound/block/') == 2 and 'Grüße! 日本語' in markup
    attachment = list(sent.iter_attachments())[0]
    assert attachment.get_filename() == 'Prüfung.pdf' and attachment.get_payload(decode=True) == b'\x00\xffbinary-pdf'
    assert parse_mime(sent.as_bytes()).body_text.startswith('Grüße! 日本語')


def test_freemail_sender_gets_address_link_only(domain):  # noqa: F811
    sent = forward(domain, mime(to='bot@example.com', sender='Someone <someone@gmail.com>'))
    text = sent.get_body(('plain',)).get_content()
    assert 'Block this sender (someone@gmail.com)' in text
    assert 'Block domain' not in text


def test_single_part_and_signed_bodies(domain):  # noqa: F811
    message = EmailMessage()
    message['From'], message['Message-ID'] = 'x@leads.example', '<a@leads.example>'
    message.set_content('only text')
    inbound = InboundMessage.objects.create(message_id='<a@leads.example>')
    footer = forward_footer(inbound, message.as_bytes())
    from mailing.ses import _Envelope, _forward_message  # noqa: PLC0415
    envelope = _Envelope('relay@example.com', 'to@example.org', '', None, None, None)
    sent = _forward_message(envelope, message.as_bytes(), footer=footer)
    assert sent.get_content().startswith('only text') and '/inbound/block/' in sent.get_content()

    signed = (
        b'From: x@leads.example\r\nMIME-Version: 1.0\r\n'
        b'Content-Type: multipart/signed; protocol="application/pgp-signature"; boundary="b"\r\n\r\n'
        b'--b\r\nContent-Type: text/plain\r\n\r\nsigned text\r\n'
        b'--b\r\nContent-Type: application/pgp-signature\r\n\r\nsig\r\n--b--\r\n'
    )
    sent = _forward_message(envelope, signed, footer=footer)
    assert sent.get_content_type() == 'multipart/mixed'
    inner = sent.get_payload()[0]
    assert inner.get_content_type() == 'multipart/signed' and '/inbound/block/' not in inner.as_string()
    assert '/inbound/block/' in sent.get_payload()[1].get_content()


def test_refusal_guards(domain):  # noqa: F811
    assert refusal('domain', 'gmail.com') == 'shared_domain'
    assert refusal('domain', 'proton.me') == 'shared_domain'
    assert refusal('address', 'someone@gmail.com') == ''
    assert refusal('domain', 'aishippinglabs.com') == 'own_domain'
    assert refusal('domain', 'mail.datatalks.club') == 'own_domain'
    assert refusal('address', 'alexey@datatalks.club') == 'own_domain'
    assert refusal('domain', 'example.com') == 'own_domain'  # an InboundDomain row
    assert refusal('address', 'no-at-sign') == 'invalid'
    assert refusal('domain', 'leads.example') == ''


def test_own_sender_gets_no_footer(domain):  # noqa: F811
    inbound = InboundMessage.objects.create(message_id='<own@example>')
    raw = b'From: noreply@aishippinglabs.com\r\nSubject: x\r\n\r\nbody\r\n'
    assert forward_footer(inbound, raw) is None


# Signing.

def test_token_roundtrip_tamper_salt_and_expiry():
    token = issue_token(message_id=7, scope='address', value='Stefan.S@Leads.Example')
    assert read_token(token) == {'m': 7, 's': 'address', 'v': 'stefan.s@leads.example'}
    assert read_token(token[:-2] + ('A' if token[-2] != 'A' else 'B') + token[-1]) is None
    assert read_token(token, salt=UNBLOCK_SALT) is None
    assert read_token('garbage') is None
    later = timezone.now().timestamp() + LINK_MAX_AGE_SECONDS + 60
    with patch('django.core.signing.time.time', return_value=later):
        assert read_token(token) is None


def test_token_with_wrong_shape_is_rejected():
    from django.core import signing  # noqa: PLC0415
    forged = signing.TimestampSigner(salt='mailing.inbound.block_link').sign_object({'m': 1, 's': 'everyone', 'v': 'x'})
    assert read_token(forged) is None


# Pages: GET confirms, POST blocks, idempotent, unblock.

def test_get_confirms_post_blocks_idempotently_and_unblock_deletes(domain):  # noqa: F811
    inbound = InboundMessage.objects.create(message_id='<pitch@leads.example>')
    web = HttpClient()
    url = path_of(block_url(message_id=inbound.pk, scope='address', value='stefan.s@leads.example'))

    page = web.get(url)
    assert page.status_code == 200 and b'Block stefan.s@leads.example' in page.content
    assert not BlockedSender.objects.exists()

    page = web.post(url)
    assert page.status_code == 200
    rule = BlockedSender.objects.get()
    assert (rule.scope, rule.value, rule.origin, rule.reason, rule.origin_message) == (
        'address', 'stefan.s@leads.example', 'email_link', '<pitch@leads.example>', inbound)
    assert b'is blocked' in page.content
    unblock = page.content.decode().split('href="')[-1].split('"')[0]
    assert '/inbound/unblock/' in unblock

    assert web.post(url).status_code == 200
    assert BlockedSender.objects.count() == 1
    assert b'already blocked' in web.post(url).content

    assert web.get(path_of(unblock)).status_code == 200
    assert BlockedSender.objects.count() == 1
    assert b'no longer blocked' in web.post(path_of(unblock)).content
    assert not BlockedSender.objects.exists()


def test_pages_refuse_tampered_freemail_and_own_domains(domain):  # noqa: F811
    web = HttpClient()
    good = path_of(block_url(message_id=1, scope='domain', value='leads.example'))
    assert web.post(good[:-3] + 'xx/').status_code == 404
    for value in ('gmail.com', 'aishippinglabs.com'):
        url = path_of(block_url(message_id=1, scope='domain', value=value))
        assert web.get(url).status_code == 400
        assert web.post(url).status_code == 400
    unblock_token = issue_token(message_id=1, scope='domain', value='leads.example', salt=UNBLOCK_SALT)
    assert web.post(f'/inbound/block/{unblock_token}/').status_code == 404
    assert not BlockedSender.objects.exists()
