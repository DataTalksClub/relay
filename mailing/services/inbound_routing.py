"""Durable inbound routing. Network errors are recorded by code, never by text."""
import json
import logging
import os
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import s3_client
from mailing.enqueue import enqueue_transactional_email
from mailing.models import (
    Contact,
    EmailEventType,
    EmailTemplate,
    InboundDelivery,
    InboundDomain,
    InboundMailbox,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.dmarc import is_dmarc_report
from mailing.services.transactional import append_transactional_event, build_transactional_queue_payload

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def match_mailboxes(addresses):
    matches = {}
    for address in dict.fromkeys(a.lower() for a in addresses):
        local, sep, domain = address.rpartition('@')
        if not sep:
            continue
        owner = InboundDomain.objects.filter(name=domain, enabled=True).first()
        if owner is None:
            continue
        # An explicitly disabled mailbox must not fall through to catch-all.
        mailbox = owner.mailboxes.filter(local_part=local).first()
        if mailbox is None:
            mailbox = owner.mailboxes.filter(local_part='*').first()
        if mailbox is not None and mailbox.enabled:
            matches.setdefault(mailbox.pk, (mailbox, address))
    return list(matches.values())


def loop_reason(raw, parsed):
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    if is_dmarc_report(mime):
        return 'dmarc_archived'
    if mime.get('X-Loop') or mime.get('X-Relay-Inbound'):
        return 'loop_header'
    # Alert-style Auto-Submitted mail is allowed once; Relay's own replies and
    # forwarded mail are blocked by the headers below and by sender identity.
    if str(mime.get('Auto-Submitted', '')).lower().startswith(('auto-replied', 'auto-forwarded')):
        return 'auto_reply'
    if mime.get_content_type() == 'multipart/report' or any(
        part.get_content_type() == 'text/calendar' and
        (str(part.get_param('method') or '').upper() == 'REPLY' or b'METHOD:REPLY' in (part.get_payload(decode=True) or b''))
        for part in mime.walk()
    ):
        return 'bounce_or_calendar_reply'
    if any(a.startswith(('mailer-daemon@', 'postmaster@')) for a in parsed.sender_addresses):
        return 'bounce_sender'
    if InboundDomain.objects.filter(from_email__in=parsed.sender_addresses).exists():
        return 'own_sender'
    if TransactionalMessage.objects.filter(ses_message_id__in=[parsed.message_id.strip('<>').split('@')[0], parsed.message_id.strip('<>'), parsed.message_id]).exclude(ses_message_id='').exists():
        return 'relay_message'
    return ''


def create_deliveries(stored, parsed, raw, matches):
    reason = loop_reason(raw, parsed)
    for mailbox, recipient in matches:
        # Serialize rate check and outbox insert for concurrent ingress workers.
        mailbox = InboundMailbox.objects.select_for_update().get(pk=mailbox.pk)
        why = reason
        if mailbox.kind == InboundMailbox.Kind.DISCARD:
            why = why or 'domain_default_discard'
        elif mailbox.kind == InboundMailbox.Kind.BOT and (
            len(parsed.sender_addresses) != 1 or parsed.sender_addresses[0] not in mailbox.sender_allowlist
        ):
            why = why or 'sender_not_allowed'
        count = InboundDelivery.objects.filter(
            mailbox=mailbox, created_at__gte=timezone.now() - timedelta(minutes=1),
        ).exclude(status=InboundDelivery.Status.SUPPRESSED).count()
        if count >= mailbox.rate_per_minute:
            why = why or 'rate_cap'
        destinations = mailbox.destinations if mailbox.kind == InboundMailbox.Kind.FORWARD else ['']
        for destination in destinations:
            suppressed = why or ('original_sender' if destination.lower() in parsed.sender_addresses else '')
            row, _ = InboundDelivery.objects.get_or_create(
                message=stored, mailbox=mailbox, destination=destination,
                defaults={'kind': mailbox.kind, 'recipient': recipient, 'reason': suppressed,
                          'status': InboundDelivery.Status.SUPPRESSED if suppressed else InboundDelivery.Status.PENDING},
            )
            logger.info('inbound_route delivery_id=%s message_id=%s status=%s reason=%s', row.pk, stored.pk, row.status, row.reason)


def webhook_config(mailbox):
    ref = mailbox.webhook_secret_id
    if ref.startswith('env:'):
        value = os.environ[ref[4:]]
    else:
        value = boto3.client('secretsmanager', region_name=settings.AWS_REGION).get_secret_value(SecretId=ref)['SecretString']
    config = json.loads(value)
    url = urlsplit(config['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError('invalid_webhook_configuration')
    header = config.get('header', 'Authorization')
    if not header or any(c in header for c in '\r\n:') or header.lower() in {'host', 'content-length', 'content-type'}:
        raise ValueError('invalid_webhook_configuration')
    if not config.get('value') or any(c in config['value'] for c in '\r\n'):
        raise ValueError('invalid_webhook_configuration')
    return config['url'], header, config['value']


def post_webhook(delivery):
    url, header, value = webhook_config(delivery.mailbox)
    message = delivery.message
    base = settings.PUBLIC_BASE_URL.rstrip('/')
    payload = {
        'from': message.from_header, 'to': delivery.recipient, 'subject': message.subject,
        'snippet': message.snippet, 'message_id': message.message_id,
        'message_url': f'{base}/inbound/messages/{message.pk}/raw/',
    }
    request = Request(url, data=json.dumps(payload).encode(), method='POST', headers={
        'Content-Type': 'application/json', header: value, 'Idempotency-Key': f'relay-inbound-{delivery.pk}',
        'X-Relay-Delivery-Id': str(delivery.pk),
    })
    with build_opener(NoRedirect).open(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise ValueError('webhook_non_2xx')
        response.read(1)
        return response.status


def queue_forward(delivery):
    mailbox, inbound = delivery.mailbox, delivery.message
    client = mailbox.domain.client
    contact, _ = Contact.objects.get_or_create(normalized_email=delivery.destination, defaults={'email': delivery.destination})
    if contact.hard_bounced_at or contact.complained_at:
        delivery.status, delivery.reason = InboundDelivery.Status.SUPPRESSED, 'outbound_suppression'
        return
    template, _ = EmailTemplate.objects.get_or_create(client=client, key='relay-inbound-forward', defaults={
        'name': 'Inbound forward', 'subject': '{{ subject }}',
    })
    outbound, created = TransactionalMessage.objects.get_or_create(
        client=client, idempotency_key=f'inbound-forward:{delivery.pk}', defaults={
            'contact': contact, 'email': delivery.destination, 'from_email': mailbox.domain.from_email,
            'template': template, 'template_key': template.key, 'subject': inbound.subject[:255],
            'inbound_source': inbound,
            'metadata': {'reply_to': inbound.from_header, 'inbound_delivery_id': delivery.pk},
        },
    )
    delivery.outbound_message = outbound
    if created:
        append_transactional_event(outbound, EmailEventType.QUEUED, {'inbound_delivery_id': delivery.pk})
        payload = build_transactional_queue_payload(outbound)
        transaction.on_commit(lambda: enqueue_transactional_email(payload))
    if outbound.status == TransactionalMessageStatus.SENT:
        delivery.status = InboundDelivery.Status.DELIVERED
        delivery.delivered_at = outbound.sent_at
    elif outbound.status in {TransactionalMessageStatus.FAILED, TransactionalMessageStatus.SKIPPED, TransactionalMessageStatus.BOUNCED, TransactionalMessageStatus.COMPLAINED}:
        delivery.status, delivery.reason = InboundDelivery.Status.FAILED, 'outbound_failed'
    else:
        delivery.next_attempt_at = timezone.now() + timedelta(seconds=30)


def sender_blocked(message):
    from mailing.services.inbound_email import find_blocked_sender  # noqa: PLC0415 (imports this module)
    return find_blocked_sender([message.sender_address]) is not None


def process_due_deliveries(*, limit=25):
    ids = list(InboundDelivery.objects.filter(status=InboundDelivery.Status.PENDING, next_attempt_at__lte=timezone.now()).order_by('pk').values_list('pk', flat=True)[:limit])
    for pk in ids:
        with transaction.atomic():
            row = InboundDelivery.objects.select_for_update(skip_locked=True, of=("self",)).select_related('mailbox__domain__client', 'message').filter(pk=pk, status=InboundDelivery.Status.PENDING, next_attempt_at__lte=timezone.now()).first()
            if row is None:
                continue
            if not row.mailbox.enabled or not row.mailbox.domain.enabled:
                row.status, row.reason = InboundDelivery.Status.SUPPRESSED, 'routing_disabled'
            elif row.kind != row.mailbox.kind:
                row.status, row.reason = InboundDelivery.Status.SUPPRESSED, 'routing_changed'
            elif row.kind == InboundMailbox.Kind.BOT and row.message.sender_address not in row.mailbox.sender_allowlist:
                row.status, row.reason = InboundDelivery.Status.SUPPRESSED, 'sender_not_allowed'
            elif row.kind == InboundMailbox.Kind.FORWARD and row.destination not in row.mailbox.destinations:
                row.status, row.reason = InboundDelivery.Status.SUPPRESSED, 'destination_removed'
            elif row.kind == InboundMailbox.Kind.FORWARD and row.outbound_message_id is None and sender_blocked(row.message):
                # Blocked between ingest and dispatch, e.g. from an earlier forward's link.
                row.status, row.reason = InboundDelivery.Status.SUPPRESSED, 'blocked_sender'
            else:
                try:
                    if row.mailbox.kind == InboundMailbox.Kind.BOT:
                        row.response_status = post_webhook(row)
                        row.attempt_count += 1
                        row.status, row.delivered_at = InboundDelivery.Status.DELIVERED, timezone.now()
                    else:
                        queue_forward(row)
                except Exception as exc:
                    row.attempt_count += 1
                    row.response_status = getattr(exc, 'code', None) if isinstance(getattr(exc, 'code', None), int) else None
                    row.reason = 'dispatch_error' # exception text can contain webhook credentials
                    if row.attempt_count >= MAX_ATTEMPTS:
                        row.status = InboundDelivery.Status.FAILED
                    else:
                        row.next_attempt_at = timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** (row.attempt_count - 1)))
                    logger.warning('inbound_delivery_retry delivery_id=%s attempt=%s error_type=%s', row.pk, row.attempt_count, type(exc).__name__)
            row.save()
            if row.status == InboundDelivery.Status.FAILED:
                logger.error('inbound_delivery_failed delivery_id=%s message_id=%s attempts=%s reason=%s', row.pk, row.message_id, row.attempt_count, row.reason)
            elif row.status == InboundDelivery.Status.DELIVERED:
                logger.info('inbound_delivery_delivered delivery_id=%s message_id=%s outbound_message_id=%s response_status=%s', row.pk, row.message_id, row.outbound_message_id, row.response_status)
    return len(ids)


def routing_health():
    failed = InboundDelivery.objects.filter(status=InboundDelivery.Status.FAILED).count()
    stale = InboundDelivery.objects.filter(status=InboundDelivery.Status.PENDING, created_at__lt=timezone.now() - timedelta(minutes=15)).count()
    return {'status': 'FAIL' if failed or stale else 'OK', 'failed': failed, 'overdue': stale}


def load_forward_mime(message):
    inbound = message.inbound_source
    return s3_client().get_object(Bucket=inbound.raw_bucket, Key=inbound.raw_key)['Body'].read()
