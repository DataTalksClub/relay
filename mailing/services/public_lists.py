"""Public double opt-in lists for static sites.

A browser cannot hold a client API key. Each list in ``RELAY_PUBLIC_LISTS``
names a client, audience, category, and template that already exist. The
public routes only start or finish that one flow. They never accept a From
address, a template key, or a confirm URL from the request.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactSourceMetadata,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
)
from mailing.services.api import apply_canonical_category_preference, validate_contact_scope
from mailing.services.api_errors import ApiValidationError
from mailing.services.categories import TRANSACTIONAL_CATEGORY, validate_canonical_category
from mailing.services.contacts import subscribe_contact, upsert_contact
from mailing.services.transactional import (
    confirm_category_verification_for_client,
    request_category_verification_for_client,
    send_transactional_email_for_client,
)

REQUIRED_FIELDS = ("org", "client", "audience", "category", "template", "confirm_base")
OPTINS = ("double", "single")
SIGNUP_SOURCE = "website-signup"


@dataclass(frozen=True)
class PublicList:
    key: str
    organization_slug: str
    client_slug: str
    audience_slug: str
    category: str
    template_key: str
    confirm_base_url: str
    optin: str = "double"


def parse_public_lists(raw):
    """Parse ``key org=… client=… audience=… category=… template=… confirm_base=… [optin=single]``.

    Several lists are separated by ``;`` or newlines. Field values are a single
    token, so a confirm URL must not contain spaces. ``optin=single`` subscribes
    on submit and sends ``template`` as a welcome email; it needs no
    ``confirm_base``. The default is double opt-in.
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
        optin = fields.get("optin", "double")
        required = [name for name in REQUIRED_FIELDS if not (optin == "single" and name == "confirm_base")]
        missing = [name for name in required if not fields.get(name)]
        if not key or missing or optin not in OPTINS:
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
            confirm_base_url=fields.get("confirm_base", "").rstrip("/"),
            optin=optin,
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


def request_public_signup(spec, email, consent=None):
    """Entry point for the public subscribe route; picks the list's opt-in mode."""
    if spec.optin == "single":
        return subscribe_public_single_optin(spec, email, consent or {})
    return request_public_verification(spec, email)


def recent_signups_from_ip(client, ip):
    window_start = timezone.now() - timedelta(seconds=settings.RELAY_PUBLIC_SUBSCRIBE_IP_WINDOW_SECONDS)
    return ContactSourceMetadata.objects.filter(
        client=client,
        source=SIGNUP_SOURCE,
        metadata__consent_ip=ip,
        updated_at__gte=window_start,
    ).count()


def subscribe_public_single_optin(spec, email, consent):
    """Subscribe on submit, record consent evidence, and send the welcome email.

    Matches the Mailchimp form this replaces: no confirmation step. The
    subscription is not marked verified. Already-subscribed addresses get no
    email, and an address that unsubscribed, bounced or complained is never
    resubscribed by an anonymous form post.
    """
    client = load_public_client(spec)
    payload = {
        "email": email,
        "audience": spec.audience_slug,
        "client": client.slug,
        "category": spec.category,
    }
    scope = validate_contact_scope(payload, client)
    contact, _ = upsert_contact(scope.email)
    subscription = Subscription.objects.filter(contact=contact, audience=scope.audience, client=client).first()
    if subscription is not None and subscription.status == SubscriptionStatus.SUBSCRIBED:
        return {"status": "already_subscribed"}
    if (
        (subscription is not None and subscription.status == SubscriptionStatus.UNSUBSCRIBED)
        or contact.global_unsubscribed_at
        or contact.hard_bounced_at
        or contact.complained_at
    ):
        return {"status": "not_resubscribed"}

    window_start = timezone.now() - timedelta(seconds=settings.RELAY_PUBLIC_SUBSCRIBE_EMAIL_WINDOW_SECONDS)
    recent = TransactionalMessage.objects.filter(
        client=client,
        email__iexact=scope.email,
        template_key=spec.template_key,
        created_at__gte=window_start,
    ).count()
    if recent >= settings.RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT:
        raise ApiValidationError({"email": "rate_limited"}, status_code=429)
    ip = consent.get("consent_ip", "")
    if ip and recent_signups_from_ip(client, ip) >= settings.RELAY_PUBLIC_SUBSCRIBE_IP_LIMIT:
        raise ApiValidationError({"ip": "rate_limited"}, status_code=429)

    now = timezone.now()
    # One transaction: if the welcome email cannot be queued (for example the
    # template is missing) nobody is left subscribed without being told.
    with transaction.atomic():
        Subscription.objects.update_or_create(
            contact=contact,
            audience=scope.audience,
            client=client,
            defaults={
                "status": SubscriptionStatus.SUBSCRIBED,
                "unsubscribed_at": None,
                "unsubscribe_reason": "",
            },
        )
        apply_canonical_category_preference(
            contact, scope.audience, client, spec.category, enabled=True, reason="website_signup"
        )
        ContactSourceMetadata.objects.update_or_create(
            contact=contact,
            audience=scope.audience,
            client=client,
            source=SIGNUP_SOURCE,
            defaults={"metadata": {"consent_at": now.isoformat(), "list": spec.key, **consent}},
        )
        send_transactional_email_for_client(
            {
                "email": scope.email,
                "template_key": spec.template_key,
                "context": {},
                "audience": scope.audience.slug,
                "client": client.slug,
                "category_tag": TRANSACTIONAL_CATEGORY,
            },
            client,
        )
    return {"status": "subscribed"}
