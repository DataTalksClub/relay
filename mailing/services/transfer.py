"""Move mailing data between Relay hosts.

Rows are addressed by natural keys, not database ids, so a load onto an empty
production database matches a second load of the same export. Load each
resource in manifest order, then the next. Task runs, sessions, and Django
admin tables are not part of the document.
"""

from datetime import datetime

from django.db import transaction
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    CategoryPreference,
    Client,
    ClientApiKey,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    EmailEvent,
    EmailTemplate,
    Organization,
    Subscription,
    Tag,
    TransactionalMessage,
)

VERSION = 1
MAX_PAGE = 500

RESOURCE_ORDER = (
    "organizations",
    "audiences",
    "clients",
    "client_api_keys",
    "tags",
    "contacts",
    "contact_tags",
    "subscriptions",
    "category_preferences",
    "contact_source_metadata",
    "email_templates",
    "transactional_messages",
    "email_events",
)


class TransferError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def manifest():
    return {
        "version": VERSION,
        "resources": [{"name": name, "count": _handlers[name].count()} for name in RESOURCE_ORDER],
    }


def export_page(resource, offset, limit):
    handler = _handler(resource)
    total = handler.count()
    rows = [handler.export(obj) for obj in handler.page(offset, limit)]
    return {
        "version": VERSION,
        "resource": resource,
        "offset": offset,
        "total": total,
        "rows": rows,
    }


def load_page(payload):
    if not isinstance(payload, dict):
        raise TransferError("body must be an object")
    if payload.get("version") != VERSION:
        raise TransferError("unsupported version")
    resource = payload.get("resource")
    handler = _handler(resource)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise TransferError("rows must be a list")
    if len(rows) > MAX_PAGE:
        raise TransferError(f"at most {MAX_PAGE} rows")
    with transaction.atomic():
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise TransferError(f"row {index} must be an object")
            try:
                handler.load(row)
            except TransferError:
                raise
            except Exception as exc:
                raise TransferError(f"row {index} could not be loaded") from exc
    return {"version": VERSION, "resource": resource, "upserted": len(rows)}


def _handler(resource):
    handler = _handlers.get(resource)
    if handler is None:
        raise TransferError("unknown resource")
    return handler


def _iso(value):
    if value is None:
        return None
    return value.isoformat()


def _dt(value):
    if not value:
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise TransferError("invalid timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _require(row, *keys):
    missing = [key for key in keys if not row.get(key)]
    if missing:
        raise TransferError("missing " + ", ".join(missing))


def _stamp(model, pk, created_at, updated_at=None):
    fields = {}
    if created_at is not None and hasattr(model, "created_at"):
        fields["created_at"] = created_at
    if updated_at is not None and hasattr(model, "updated_at"):
        fields["updated_at"] = updated_at
    if fields:
        model.objects.filter(pk=pk).update(**fields)


def _org(slug):
    org = Organization.objects.filter(slug=slug).first()
    if org is None:
        raise TransferError(f"unknown organization {slug}")
    return org


def _audience(org_slug, slug):
    audience = Audience.objects.filter(organization__slug=org_slug, slug=slug).first()
    if audience is None:
        raise TransferError(f"unknown audience {org_slug}/{slug}")
    return audience


def _client(org_slug, slug):
    client = Client.objects.filter(organization__slug=org_slug, slug=slug).first()
    if client is None:
        raise TransferError(f"unknown client {org_slug}/{slug}")
    return client


def _contact(email):
    contact = Contact.objects.filter(normalized_email=(email or "").casefold()).first()
    if contact is None:
        raise TransferError("unknown contact")
    return contact


class _Organizations:
    def count(self):
        return Organization.objects.count()

    def page(self, offset, limit):
        return Organization.objects.order_by("slug")[offset : offset + limit]

    def export(self, obj):
        return {"slug": obj.slug, "name": obj.name, "created_at": _iso(obj.created_at)}

    def load(self, row):
        _require(row, "slug", "name")
        obj, _created = Organization.objects.update_or_create(
            slug=row["slug"], defaults={"name": row["name"]}
        )
        _stamp(Organization, obj.pk, _dt(row.get("created_at")))


class _Audiences:
    def count(self):
        return Audience.objects.count()

    def page(self, offset, limit):
        return Audience.objects.select_related("organization").order_by("organization__slug", "slug")[
            offset : offset + limit
        ]

    def export(self, obj):
        return {
            "organization": obj.organization.slug,
            "slug": obj.slug,
            "name": obj.name,
            "created_at": _iso(obj.created_at),
        }

    def load(self, row):
        _require(row, "organization", "slug", "name")
        obj, _created = Audience.objects.update_or_create(
            organization=_org(row["organization"]),
            slug=row["slug"],
            defaults={"name": row["name"]},
        )
        _stamp(Audience, obj.pk, _dt(row.get("created_at")))


_CLIENT_FIELDS = (
    "name",
    "default_from_email",
    "allowed_from_emails",
    "default_sender_id",
    "sender_emails",
    "cmp_webhook_url",
    "cmp_webhook_token",
    "relay_webhook_signing_secret",
    "relay_webhook_allowed_origins",
    "mailchimp_api_key",
    "mailchimp_list_id",
    "mailchimp_enabled",
    "is_active",
)


class _Clients:
    def count(self):
        return Client.objects.count()

    def page(self, offset, limit):
        return Client.objects.select_related("organization").order_by("organization__slug", "slug")[
            offset : offset + limit
        ]

    def export(self, obj):
        row = {"organization": obj.organization.slug, "slug": obj.slug}
        for field in _CLIENT_FIELDS:
            row[field] = getattr(obj, field)
        row["created_at"] = _iso(obj.created_at)
        row["updated_at"] = _iso(obj.updated_at)
        return row

    def load(self, row):
        _require(row, "organization", "slug", "name")
        defaults = {field: row.get(field) for field in _CLIENT_FIELDS}
        defaults["name"] = row["name"]
        for field in ("allowed_from_emails", "sender_emails", "relay_webhook_allowed_origins"):
            if defaults[field] is None:
                defaults[field] = []
        for field in (
            "default_from_email",
            "default_sender_id",
            "cmp_webhook_url",
            "cmp_webhook_token",
            "relay_webhook_signing_secret",
            "mailchimp_api_key",
            "mailchimp_list_id",
        ):
            if defaults[field] is None:
                defaults[field] = ""
        if defaults["mailchimp_enabled"] is None:
            defaults["mailchimp_enabled"] = False
        if defaults["is_active"] is None:
            defaults["is_active"] = True
        obj, _created = Client.objects.update_or_create(
            organization=_org(row["organization"]),
            slug=row["slug"],
            defaults=defaults,
        )
        _stamp(Client, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _ApiKeys:
    def count(self):
        return ClientApiKey.objects.count()

    def page(self, offset, limit):
        return ClientApiKey.objects.select_related("client__organization").order_by("public_id")[
            offset : offset + limit
        ]

    def export(self, obj):
        return {
            "organization": obj.client.organization.slug,
            "client": obj.client.slug,
            "name": obj.name,
            "public_id": obj.public_id,
            "key_hash": obj.key_hash,
            "notes": obj.notes,
            "last_used_at": _iso(obj.last_used_at),
            "revoked_at": _iso(obj.revoked_at),
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "organization", "client", "name", "public_id", "key_hash")
        obj, _created = ClientApiKey.objects.update_or_create(
            public_id=row["public_id"],
            defaults={
                "client": _client(row["organization"], row["client"]),
                "name": row["name"],
                "key_hash": row["key_hash"],
                "notes": row.get("notes") or "",
                "last_used_at": _dt(row.get("last_used_at")),
                "revoked_at": _dt(row.get("revoked_at")),
            },
        )
        _stamp(ClientApiKey, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _Tags:
    def count(self):
        return Tag.objects.count()

    def page(self, offset, limit):
        return Tag.objects.select_related("audience__organization").order_by("audience__slug", "slug")[
            offset : offset + limit
        ]

    def export(self, obj):
        return {
            "organization": obj.audience.organization.slug,
            "audience": obj.audience.slug,
            "slug": obj.slug,
            "name": obj.name,
        }

    def load(self, row):
        _require(row, "organization", "audience", "slug", "name")
        Tag.objects.update_or_create(
            audience=_audience(row["organization"], row["audience"]),
            slug=row["slug"],
            defaults={"name": row["name"]},
        )


class _Contacts:
    def count(self):
        return Contact.objects.count()

    def page(self, offset, limit):
        return Contact.objects.order_by("normalized_email")[offset : offset + limit]

    def export(self, obj):
        return {
            "email": obj.email,
            "verified_at": _iso(obj.verified_at),
            "email_validation_status": obj.email_validation_status,
            "email_validation_reason": obj.email_validation_reason,
            "email_validated_at": _iso(obj.email_validated_at),
            "global_unsubscribed_at": _iso(obj.global_unsubscribed_at),
            "hard_bounced_at": _iso(obj.hard_bounced_at),
            "complained_at": _iso(obj.complained_at),
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "email")
        obj, _created = Contact.objects.update_or_create(
            normalized_email=row["email"].strip().casefold(),
            defaults={
                "email": row["email"].strip(),
                "verified_at": _dt(row.get("verified_at")),
                "email_validation_status": row.get("email_validation_status") or "unknown",
                "email_validation_reason": row.get("email_validation_reason") or "",
                "email_validated_at": _dt(row.get("email_validated_at")),
                "global_unsubscribed_at": _dt(row.get("global_unsubscribed_at")),
                "hard_bounced_at": _dt(row.get("hard_bounced_at")),
                "complained_at": _dt(row.get("complained_at")),
            },
        )
        _stamp(Contact, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _ContactTags:
    def count(self):
        return ContactTag.objects.count()

    def page(self, offset, limit):
        return ContactTag.objects.select_related("contact", "tag__audience__organization").order_by("id")[
            offset : offset + limit
        ]

    def export(self, obj):
        return {
            "email": obj.contact.normalized_email,
            "organization": obj.tag.audience.organization.slug,
            "audience": obj.tag.audience.slug,
            "tag": obj.tag.slug,
            "created_at": _iso(obj.created_at),
        }

    def load(self, row):
        _require(row, "email", "organization", "audience", "tag")
        tag = Tag.objects.filter(
            audience=_audience(row["organization"], row["audience"]),
            slug=row["tag"],
        ).first()
        if tag is None:
            raise TransferError("unknown tag")
        obj, _created = ContactTag.objects.get_or_create(contact=_contact(row["email"]), tag=tag)
        _stamp(ContactTag, obj.pk, _dt(row.get("created_at")))


class _Subscriptions:
    def count(self):
        return Subscription.objects.count()

    def page(self, offset, limit):
        return Subscription.objects.select_related(
            "contact", "audience__organization", "client__organization"
        ).order_by("id")[offset : offset + limit]

    def export(self, obj):
        return {
            "email": obj.contact.normalized_email,
            "organization": obj.audience.organization.slug,
            "audience": obj.audience.slug,
            "client_organization": obj.client.organization.slug if obj.client_id else None,
            "client": obj.client.slug if obj.client_id else None,
            "status": obj.status,
            "verified_at": _iso(obj.verified_at),
            "unsubscribed_at": _iso(obj.unsubscribed_at),
            "unsubscribe_reason": obj.unsubscribe_reason,
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "email", "organization", "audience", "status")
        client = None
        if row.get("client"):
            _require(row, "client_organization")
            client = _client(row["client_organization"], row["client"])
        obj, _created = Subscription.objects.update_or_create(
            contact=_contact(row["email"]),
            audience=_audience(row["organization"], row["audience"]),
            client=client,
            defaults={
                "status": row["status"],
                "verified_at": _dt(row.get("verified_at")),
                "unsubscribed_at": _dt(row.get("unsubscribed_at")),
                "unsubscribe_reason": row.get("unsubscribe_reason") or "",
            },
        )
        _stamp(Subscription, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _Preferences:
    def count(self):
        return CategoryPreference.objects.count()

    def page(self, offset, limit):
        return CategoryPreference.objects.select_related(
            "contact", "audience__organization", "client__organization"
        ).order_by("id")[offset : offset + limit]

    def export(self, obj):
        return {
            "email": obj.contact.normalized_email,
            "organization": obj.audience.organization.slug,
            "audience": obj.audience.slug,
            "client_organization": obj.client.organization.slug,
            "client": obj.client.slug,
            "tag": obj.tag,
            "label": obj.label,
            "enabled": obj.enabled,
            "updated_reason": obj.updated_reason,
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "email", "organization", "audience", "client_organization", "client", "tag")
        obj, _created = CategoryPreference.objects.update_or_create(
            contact=_contact(row["email"]),
            audience=_audience(row["organization"], row["audience"]),
            client=_client(row["client_organization"], row["client"]),
            tag=row["tag"],
            defaults={
                "label": row.get("label") or "",
                "enabled": bool(row.get("enabled")),
                "updated_reason": row.get("updated_reason") or "",
            },
        )
        _stamp(CategoryPreference, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _Sources:
    def count(self):
        return ContactSourceMetadata.objects.count()

    def page(self, offset, limit):
        return ContactSourceMetadata.objects.select_related(
            "contact", "audience__organization", "client__organization"
        ).order_by("id")[offset : offset + limit]

    def export(self, obj):
        return {
            "email": obj.contact.normalized_email,
            "organization": obj.audience.organization.slug,
            "audience": obj.audience.slug,
            "client_organization": obj.client.organization.slug,
            "client": obj.client.slug,
            "source": obj.source,
            "external_id": obj.external_id,
            "metadata": obj.metadata,
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "email", "organization", "audience", "client_organization", "client", "source")
        obj, _created = ContactSourceMetadata.objects.update_or_create(
            contact=_contact(row["email"]),
            audience=_audience(row["organization"], row["audience"]),
            client=_client(row["client_organization"], row["client"]),
            source=row["source"],
            defaults={
                "external_id": row.get("external_id") or "",
                "metadata": row.get("metadata") or {},
            },
        )
        _stamp(ContactSourceMetadata, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


_TEMPLATE_FIELDS = (
    "name",
    "description",
    "subject",
    "html_body",
    "text_body",
    "markdown_body",
    "category",
    "required_context",
    "example_context",
    "default_sender_id",
    "is_transactional",
    "is_active",
)


class _Templates:
    def count(self):
        return EmailTemplate.objects.count()

    def page(self, offset, limit):
        return EmailTemplate.objects.select_related("client__organization").order_by("client__slug", "key")[
            offset : offset + limit
        ]

    def export(self, obj):
        row = {
            "organization": obj.client.organization.slug,
            "client": obj.client.slug,
            "key": obj.key,
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }
        for field in _TEMPLATE_FIELDS:
            row[field] = getattr(obj, field)
        return row

    def load(self, row):
        _require(row, "organization", "client", "key", "name", "subject")
        defaults = {field: row.get(field) for field in _TEMPLATE_FIELDS}
        defaults["name"] = row["name"]
        defaults["subject"] = row["subject"]
        for field in ("description", "html_body", "text_body", "markdown_body", "category", "default_sender_id"):
            if defaults[field] is None:
                defaults[field] = ""
        if defaults["required_context"] is None:
            defaults["required_context"] = []
        if defaults["example_context"] is None:
            defaults["example_context"] = {}
        if defaults["is_transactional"] is None:
            defaults["is_transactional"] = True
        if defaults["is_active"] is None:
            defaults["is_active"] = True
        obj, _created = EmailTemplate.objects.update_or_create(
            client=_client(row["organization"], row["client"]),
            key=row["key"],
            defaults=defaults,
        )
        _stamp(EmailTemplate, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


def _transfer_key(source_id):
    return f"transfer:{source_id}"


def _message_load_key(event):
    """Key the destination message row will use after its own page is loaded."""
    if not event.transactional_message_id:
        return None
    stored = event.transactional_message.idempotency_key
    return stored or _transfer_key(event.transactional_message_id)


class _Messages:
    def count(self):
        return TransactionalMessage.objects.count()

    def page(self, offset, limit):
        return TransactionalMessage.objects.select_related(
            "client__organization", "contact", "template"
        ).order_by("id")[offset : offset + limit]

    def export(self, obj):
        return {
            "source_id": obj.id,
            "organization": obj.client.organization.slug,
            "client": obj.client.slug,
            "email": obj.contact.normalized_email,
            "template_key": obj.template.key,
            "from_email_id": obj.from_email_id,
            "from_email": obj.from_email,
            "template_version": obj.template_version,
            "status": obj.status,
            "idempotency_key": obj.idempotency_key,
            "subject": obj.subject,
            "html_body": obj.html_body,
            "text_body": obj.text_body,
            "context": obj.context,
            "ses_message_id": obj.ses_message_id,
            "sent_at": _iso(obj.sent_at),
            "delivered_at": _iso(obj.delivered_at),
            "first_opened_at": _iso(obj.first_opened_at),
            "first_clicked_at": _iso(obj.first_clicked_at),
            "open_count": obj.open_count,
            "click_count": obj.click_count,
            "metadata": obj.metadata,
            "last_error": obj.last_error,
            "created_at": _iso(obj.created_at),
            "updated_at": _iso(obj.updated_at),
        }

    def load(self, row):
        _require(row, "source_id", "organization", "client", "email", "template_key", "subject", "status")
        client = _client(row["organization"], row["client"])
        template = EmailTemplate.objects.filter(client=client, key=row["template_key"]).first()
        if template is None:
            raise TransferError("unknown template")
        idempotency_key = row.get("idempotency_key") or _transfer_key(row["source_id"])
        obj, _created = TransactionalMessage.objects.update_or_create(
            client=client,
            idempotency_key=idempotency_key,
            defaults={
                "contact": _contact(row["email"]),
                "email": row["email"],
                "from_email_id": row.get("from_email_id") or "",
                "from_email": row.get("from_email") or "",
                "template": template,
                "template_key": row["template_key"],
                "template_version": row.get("template_version"),
                "status": row["status"],
                "subject": row["subject"],
                "html_body": row.get("html_body") or "",
                "text_body": row.get("text_body") or "",
                "context": row.get("context") or {},
                "ses_message_id": row.get("ses_message_id") or "",
                "sent_at": _dt(row.get("sent_at")),
                "delivered_at": _dt(row.get("delivered_at")),
                "first_opened_at": _dt(row.get("first_opened_at")),
                "first_clicked_at": _dt(row.get("first_clicked_at")),
                "open_count": row.get("open_count") or 0,
                "click_count": row.get("click_count") or 0,
                "metadata": row.get("metadata") or {},
                "last_error": row.get("last_error") or "",
            },
        )
        _stamp(TransactionalMessage, obj.pk, _dt(row.get("created_at")), _dt(row.get("updated_at")))


class _Events:
    def count(self):
        return EmailEvent.objects.count()

    def page(self, offset, limit):
        return EmailEvent.objects.select_related(
            "client__organization",
            "audience__organization",
            "contact",
            "transactional_message",
        ).order_by("id")[offset : offset + limit]

    def export(self, obj):
        return {
            "source_id": obj.id,
            "provider_event_id": obj.provider_event_id or _transfer_key(f"event:{obj.id}"),
            "event_type": obj.event_type,
            "url": obj.url,
            "metadata": obj.metadata,
            "created_at": _iso(obj.created_at),
            "organization": obj.client.organization.slug if obj.client_id else None,
            "client": obj.client.slug if obj.client_id else None,
            "audience_organization": obj.audience.organization.slug if obj.audience_id else None,
            "audience": obj.audience.slug if obj.audience_id else None,
            "email": obj.contact.normalized_email if obj.contact_id else None,
            "transactional_message_source_id": obj.transactional_message_id,
            "transactional_idempotency_key": _message_load_key(obj),
        }

    def load(self, row):
        _require(row, "provider_event_id", "event_type")
        client = _client(row["organization"], row["client"]) if row.get("client") else None
        audience = (
            _audience(row["audience_organization"], row["audience"]) if row.get("audience") else None
        )
        contact = _contact(row["email"]) if row.get("email") else None
        message = None
        source_message = row.get("transactional_message_source_id")
        if source_message and client is not None:
            message = TransactionalMessage.objects.filter(
                client=client,
                idempotency_key=row.get("transactional_idempotency_key") or _transfer_key(source_message),
            ).first()
            if message is None and row.get("transactional_idempotency_key"):
                message = TransactionalMessage.objects.filter(
                    client=client, idempotency_key=_transfer_key(source_message)
                ).first()
        obj, _created = EmailEvent.objects.update_or_create(
            provider_event_id=row["provider_event_id"],
            defaults={
                "client": client,
                "audience": audience,
                "contact": contact,
                "transactional_message": message,
                "event_type": row["event_type"],
                "url": row.get("url") or "",
                "metadata": row.get("metadata") or {},
            },
        )
        _stamp(EmailEvent, obj.pk, _dt(row.get("created_at")))


_handlers = {
    "organizations": _Organizations(),
    "audiences": _Audiences(),
    "clients": _Clients(),
    "client_api_keys": _ApiKeys(),
    "tags": _Tags(),
    "contacts": _Contacts(),
    "contact_tags": _ContactTags(),
    "subscriptions": _Subscriptions(),
    "category_preferences": _Preferences(),
    "contact_source_metadata": _Sources(),
    "email_templates": _Templates(),
    "transactional_messages": _Messages(),
    "email_events": _Events(),
}
