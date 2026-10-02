"""Public double opt-in lists for static sites.

A browser cannot hold a client API key. Each list in ``RELAY_PUBLIC_LISTS``
names a client, audience, category, and template that already exist. The
public routes only start or finish that one flow. They never accept a From
address, a template key, or a confirm URL from the request.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from mailing.models import Audience, Client, Contact, Subscription, SubscriptionStatus, TransactionalMessage
from mailing.services.api import validate_contact_scope
from mailing.services.api_errors import ApiValidationError
from mailing.services.categories import validate_canonical_category
from mailing.services.contacts import subscribe_contact, upsert_contact
from mailing.services.transactional import (
    confirm_category_verification_for_client,
    request_category_verification_for_client,
)

REQUIRED_FIELDS = ("org", "client", "audience", "category", "template", "confirm_base")


@dataclass(frozen=True)
class PublicList:
    key: str
    organization_slug: str
    client_slug: str
    audience_slug: str
    category: str
    template_key: str
    confirm_base_url: str


def parse_public_lists(raw):
    """Parse ``key org=… client=… audience=… category=… template=… confirm_base=…``.

    Several lists are separated by ``;`` or newlines. Field values are a single
    token, so a confirm URL must not contain spaces.
    """
    lists = {}
    text = (raw or "").replace("\n", ";")
    for chunk in text.split(";"):
        parts = chunk.split()
        if not parts:
            continue
        key = parts[0].strip()
        fields = {}
        for item in parts[1:]:
            name, separator, value = item.partition("=")
            if not separator or not name or not value:
                raise ApiValidationError({"RELAY_PUBLIC_LISTS": "invalid"})
            fields[name] = value
        missing = [name for name in REQUIRED_FIELDS if not fields.get(name)]
        if not key or missing:
            raise ApiValidationError({"RELAY_PUBLIC_LISTS": "invalid"})
        if key in lists:
            raise ApiValidationError({"RELAY_PUBLIC_LISTS": "duplicate"})
        lists[key] = PublicList(
            key=key,
            organization_slug=fields["org"],
            client_slug=fields["client"],
            audience_slug=fields["audience"],
            category=validate_canonical_category(fields["category"]),
            template_key=fields["template"],
            confirm_base_url=fields["confirm_base"].rstrip("/"),
        )
    return lists


def public_list(key):
    lists = parse_public_lists(settings.RELAY_PUBLIC_LISTS)
    found = lists.get(key)
    if found is None:
        raise ApiValidationError({"list": "not_found"}, status_code=404)
    return found


def load_public_client(spec):
    client = (
        Client.objects.select_related("organization")
        .filter(
            slug=spec.client_slug,
            organization__slug=spec.organization_slug,
            is_active=True,
        )
        .first()
    )
    if client is None:
        raise ApiValidationError({"list": "not_ready"}, status_code=503)
    return client


def request_public_verification(spec, email):
    """Record a pending subscription and send the confirmation from the list sender."""
    client = load_public_client(spec)
    payload = {
        "email": email,
        "audience": spec.audience_slug,
        "client": client.slug,
        "category": spec.category,
        "template_key": spec.template_key,
    }
    # Validate and upsert before the rate check so a bad address never counts,
    # and so the address is stored even when we decline to send again.
    scope = validate_contact_scope(payload, client)
    contact, _ = upsert_contact(scope.email)
    subscription = Subscription.objects.filter(
        contact=contact,
        audience=scope.audience,
        client=client,
    ).first()
    if subscription is not None and subscription.status == SubscriptionStatus.SUBSCRIBED:
        return {"status": "already_subscribed"}

    window_start = timezone.now() - timedelta(seconds=settings.RELAY_PUBLIC_SUBSCRIBE_EMAIL_WINDOW_SECONDS)
    recent = TransactionalMessage.objects.filter(
        client=client,
        email__iexact=scope.email,
        template_key=spec.template_key,
        created_at__gte=window_start,
    ).count()
    if recent >= settings.RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT:
        raise ApiValidationError({"email": "rate_limited"}, status_code=429)

    if subscription is None or subscription.status != SubscriptionStatus.PENDING:
        Subscription.objects.update_or_create(
            contact=contact,
            audience=scope.audience,
            client=client,
            defaults={
                "status": SubscriptionStatus.PENDING,
                "unsubscribed_at": None,
                "unsubscribe_reason": "",
            },
        )

    request_category_verification_for_client(
        payload,
        client,
        confirm_base_url=spec.confirm_base_url,
    )
    return {"status": "verification_requested"}


def confirm_public_verification(spec, token):
    """Enable the category and mark the client subscription subscribed."""
    client = load_public_client(spec)
    confirmed = confirm_category_verification_for_client({"token": token}, client)
    if confirmed["audience"] != spec.audience_slug or confirmed["category"]["tag"] != spec.category:
        raise ApiValidationError({"token": "invalid"})


    contact = Contact.objects.filter(normalized_email=confirmed["email"]).first()
    audience = Audience.objects.filter(organization=client.organization, slug=spec.audience_slug).first()
    if contact is None or audience is None:
        raise ApiValidationError({"token": "invalid"})

    now = timezone.now()
    subscribe_contact(contact, audience, client, verified_at=now)
    if contact.verified_at is None:
        contact.verified_at = now
        contact.save(update_fields=["verified_at", "updated_at"])
    return {"status": "subscribed"}
