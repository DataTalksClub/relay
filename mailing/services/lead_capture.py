"""Double opt-in lead capture for talk/workshop pages.

Flow: the page posts email (+ optional name) and an explicit consent checkbox.
Relay stores the contact as *pending* in the leads audience, records the
consent evidence under ``ContactSourceMetadata(source="lead:<slug>")`` and
sends one confirmation email from the ``lead-confirm`` template. The link in
that email confirms the address (a POST, so link scanners cannot confirm on
someone's behalf), subscribes the contact, tags it with the event slug and
unlocks the download. Slides go out later as an ordinary campaign to the
leads audience with ``include_tags=[<slug>]``.

Repeated submits are idempotent: inside ``RELAY_LEADS_RESEND_SECONDS`` no new
email is sent. Every confirmation carries a fresh signed token, so two
legitimate confirmations never share a rendered-content hash and the
cross-sender duplicate guard never has to block one.
"""

import io
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

import segno
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from mailing.lead_capture_models import LeadEvent
from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
)
from mailing.services.categories import TRANSACTIONAL_CATEGORY
from mailing.services.contacts import upsert_contact
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client

TOKEN_SALT = "relay.lead-capture.v1"
SOURCE_PREFIX = "lead:"
NAME_MAX_LENGTH = 120


class LeadError(Exception):
    """A visitor-facing failure. ``code`` picks the message on the page."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class LeadScope:
    client: Client
    audience: Audience


def leads_scope():
    client = (
        Client.objects.select_related("organization")
        .filter(slug=settings.RELAY_LEADS_CLIENT, organization__slug=settings.RELAY_LEADS_ORG, is_active=True)
        .first()
    )
    audience = (
        Audience.objects.filter(slug=settings.RELAY_LEADS_AUDIENCE, organization__slug=settings.RELAY_LEADS_ORG).first()
    )
    if client is None or audience is None:
        raise LeadError("not_ready", status=503)
    return LeadScope(client=client, audience=audience)


def event_url(event, suffix=""):
    return f"{settings.RELAY_LEADS_BASE_URL}/{event.slug}{suffix}"


def issue_token(contact_id, event_id):
    # The nonce makes every confirmation email unique, even within one second.
    return signing.dumps({"c": contact_id, "e": event_id, "n": secrets.token_hex(4)}, salt=TOKEN_SALT, compress=True)


def read_token(token, event):
    if not isinstance(token, str) or not token or len(token) > 500:
        raise LeadError("invalid_token")
    try:
        payload = signing.loads(token, salt=TOKEN_SALT, max_age=settings.RELAY_LEADS_TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise LeadError("expired_token") from exc
    except signing.BadSignature as exc:
        raise LeadError("invalid_token") from exc
    if not isinstance(payload, dict) or payload.get("e") != event.id or not isinstance(payload.get("c"), int):
        raise LeadError("invalid_token")
    return payload["c"]


def clean_name(value):
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    return " ".join(cleaned.split())[:NAME_MAX_LENGTH]


def clean_email(value):
    if not isinstance(value, str) or not value.strip():
        raise LeadError("email_required")
    email = value.strip()
    if len(email) > 254:
        raise LeadError("email_invalid")
    try:
        validate_email(email)
    except ValidationError as exc:
        raise LeadError("email_invalid") from exc
    return email


def _recent_confirmations(scope, email):
    since = timezone.now() - timedelta(seconds=settings.RELAY_LEADS_EMAIL_WINDOW_SECONDS)
    return TransactionalMessage.objects.filter(
        client=scope.client,
        email__iexact=email,
        template_key=settings.RELAY_LEADS_CONFIRM_TEMPLATE,
        created_at__gte=since,
    ).count()


def _recent_signups_from_ip(scope, ip):
    since = timezone.now() - timedelta(seconds=settings.RELAY_LEADS_IP_WINDOW_SECONDS)
    return ContactSourceMetadata.objects.filter(
        client=scope.client,
        source__startswith=SOURCE_PREFIX,
        metadata__last_submit_ip=ip,
        updated_at__gte=since,
    ).count()


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def request_lead(event, *, email, name="", consent=False, evidence=None):
    """Record a signup and send the confirmation email (at most once per window).

    Returns ``"sent"``, ``"already_sent"`` or ``"suppressed"``; the page shows
    the same "check your inbox" text for all three, so the form never reveals
    whether an address is known, bounced or unsubscribed.
    """
    if not event.is_active:
        raise LeadError("closed", status=410)
    if consent is not True:
        raise LeadError("consent_required")
    email = clean_email(email)
    name = clean_name(name)
    evidence = dict(evidence or {})
    scope = leads_scope()

    ip = evidence.get("ip", "")
    if ip and _recent_signups_from_ip(scope, ip) >= settings.RELAY_LEADS_IP_LIMIT:
        raise LeadError("rate_limited", status=429)

    now = timezone.now()
    with transaction.atomic():
        contact, _ = upsert_contact(email)
        record, _ = ContactSourceMetadata.objects.get_or_create(
            contact=contact,
            audience=scope.audience,
            client=scope.client,
            source=event.source,
            defaults={"metadata": {}},
        )
        record = ContactSourceMetadata.objects.select_for_update().get(pk=record.pk)
        metadata = dict(record.metadata or {})

        last_sent = _parse_time(metadata.get("confirmation_sent_at"))
        if last_sent and now - last_sent < timedelta(seconds=settings.RELAY_LEADS_RESEND_SECONDS):
            return "already_sent"
        if _recent_confirmations(scope, contact.normalized_email) >= settings.RELAY_LEADS_EMAIL_LIMIT:
            raise LeadError("rate_limited", status=429)

        Subscription.objects.get_or_create(
            contact=contact,
            audience=scope.audience,
            client=scope.client,
            defaults={"status": SubscriptionStatus.PENDING},
        )
        metadata.update(
            {
                "event": event.slug,
                "event_title": event.title,
                "consent_text": event.consent_text,
                "last_consent_at": now.isoformat(),
                "last_submit_ip": ip,
                "user_agent": str(evidence.get("user_agent", ""))[:300],
                "referer": str(evidence.get("referer", ""))[:300],
            }
        )
        metadata.setdefault("consent_at", now.isoformat())
        if name:
            metadata["name"] = name

        token = issue_token(contact.id, event.id)
        try:
            send_transactional_email_for_client(
                {
                    "email": contact.normalized_email,
                    "template_key": settings.RELAY_LEADS_CONFIRM_TEMPLATE,
                    "context": {
                        "confirm_url": f"{event_url(event, '/confirm')}?t={token}",
                        "event_title": event.title,
                        "offer": event.offer or (event.material.title if event.material_id else ""),
                        "name": metadata.get("name", ""),
                    },
                    "audience": scope.audience.slug,
                    "client": scope.client.slug,
                    "category_tag": TRANSACTIONAL_CATEGORY,
                    "metadata": {"lead_event": event.slug},
                },
                scope.client,
            )
            status = "sent"
            metadata["confirmation_sent_at"] = now.isoformat()
            metadata["confirmation_count"] = int(metadata.get("confirmation_count", 0)) + 1
        except TransactionalSendRejected:
            status = "suppressed"
            metadata["suppressed_at"] = now.isoformat()
        record.metadata = metadata
        record.save(update_fields=["metadata", "updated_at"])
    return status


def confirm_lead(event, token, *, ip=""):
    """Confirm the address behind ``token``: subscribe, tag with the event, unlock the download."""
    contact_id = read_token(token, event)
    scope = leads_scope()
    now = timezone.now()
    with transaction.atomic():
        contact = Contact.objects.filter(id=contact_id).first()
        record = (
            ContactSourceMetadata.objects.select_for_update()
            .filter(contact_id=contact_id, audience=scope.audience, client=scope.client, source=event.source)
            .first()
        )
        if contact is None or record is None:
            raise LeadError("invalid_token")
        subscription, _ = Subscription.objects.get_or_create(
            contact=contact, audience=scope.audience, client=scope.client
        )
        if subscription.status != SubscriptionStatus.SUBSCRIBED or subscription.verified_at is None:
            subscription.status = SubscriptionStatus.SUBSCRIBED
            subscription.verified_at = subscription.verified_at or now
            subscription.unsubscribed_at = None
            subscription.unsubscribe_reason = ""
            subscription.save()
        if contact.verified_at is None:
            contact.verified_at = now
            contact.save(update_fields=["verified_at", "updated_at"])
        tag, _ = Tag.objects.get_or_create(audience=scope.audience, slug=event.slug, defaults={"name": event.title})
        ContactTag.objects.get_or_create(contact=contact, tag=tag)
        metadata = dict(record.metadata or {})
        metadata.setdefault("confirmed_at", now.isoformat())
        metadata["last_confirm_ip"] = ip
        record.metadata = metadata
        record.save(update_fields=["metadata", "updated_at"])
    return contact, metadata


def material_for_download(event, token):
    """Return the event's material for a confirmed signup, counting the download."""
    contact_id = read_token(token, event)
    scope = leads_scope()
    with transaction.atomic():
        record = (
            ContactSourceMetadata.objects.select_for_update()
            .filter(contact_id=contact_id, audience=scope.audience, client=scope.client, source=event.source)
            .first()
        )
        if record is None or not (record.metadata or {}).get("confirmed_at"):
            raise LeadError("not_confirmed", status=403)
        metadata = dict(record.metadata)
        metadata["download_count"] = int(metadata.get("download_count", 0)) + 1
        metadata["last_download_at"] = timezone.now().isoformat()
        record.metadata = metadata
        record.save(update_fields=["metadata", "updated_at"])
    if not event.has_material:
        raise LeadError("no_material", status=404)
    return event.material


def event_stats(event):
    try:
        scope = leads_scope()
    except LeadError:
        return {"signups": 0, "confirmed": 0}
    records = ContactSourceMetadata.objects.filter(audience=scope.audience, client=scope.client, source=event.source)
    return {
        "signups": records.count(),
        "confirmed": records.filter(metadata__has_key="confirmed_at").count(),
    }


def active_event(slug):
    event = LeadEvent.objects.select_related("material").filter(slug=slug).first()
    if event is None:
        raise LeadError("not_found", status=404)
    return event


def qr_code(url, kind="svg", scale=12):
    """Render ``url`` as an SVG or PNG QR code (bytes). Error correction M, quiet zone 4."""
    qr = segno.make(url, error="m", micro=False)
    buffer = io.BytesIO()
    if kind == "png":
        qr.save(buffer, kind="png", scale=scale, border=4)
    else:
        qr.save(buffer, kind="svg", scale=scale, border=4, xmldecl=True, svgns=True)
    return buffer.getvalue()
