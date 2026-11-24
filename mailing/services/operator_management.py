from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    ClientApiKey,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailValidationStatus,
    OperatorAudit,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.auth import create_client_api_key
from mailing.services.campaign_sender import refresh_campaign_send_counts
from mailing.services.campaigns import CampaignRecipientConflict
from mailing.services.contacts import normalize_tag_slug

SECRET_METADATA_KEYS = {"api_key", "raw_api_key", "api_key_hash", "token", "secret", "password"}


def safe_metadata(metadata):
    cleaned = {}
    for key, value in (metadata or {}).items():
        if key in SECRET_METADATA_KEYS:
            continue
        if value in (None, "", [], {}):
            continue
        cleaned[key] = value
    return cleaned


def audit(actor, action, target, metadata=None):
    return OperatorAudit.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type=target.__class__.__name__.lower(),
        target_id=target.id,
        metadata=safe_metadata(metadata),
    )


def latest_audits_for(target, limit=25):
    return OperatorAudit.objects.filter(
        target_type=target.__class__.__name__.lower(),
        target_id=target.id,
    ).select_related("actor")[:limit]


@transaction.atomic
def create_or_update_audience(*, actor, audience=None, organization, name, slug):
    if audience is None:
        audience = Audience.objects.create(organization=organization, name=name, slug=slug)
        audit(actor, "audience.create", audience, {"organization": organization.slug, "slug": slug})
        return audience
    changed = {}
    if audience.name != name:
        changed["name"] = [audience.name, name]
        audience.name = name
    if audience.slug != slug:
        changed["slug"] = [audience.slug, slug]
        audience.slug = slug
    if changed:
        audience.save(update_fields=["name", "slug"])
        audit(actor, "audience.update", audience, changed)
    return audience


@transaction.atomic
def create_or_update_tag(*, actor, tag=None, audience, name, slug):
    if tag is None:
        tag = Tag.objects.create(audience=audience, name=name, slug=slug)
        audit(actor, "tag.create", tag, {"audience": audience.slug, "slug": slug})
        return tag
    changed = {}
    if tag.name != name:
        changed["name"] = [tag.name, name]
        tag.name = name
    if tag.slug != slug:
        changed["slug"] = [tag.slug, slug]
        tag.slug = slug
    if changed:
        tag.save(update_fields=["name", "slug"])
        audit(actor, "tag.update", tag, changed | {"audience": audience.slug})
    return tag


@transaction.atomic
def create_or_update_client(
    *,
    actor,
    client=None,
    organization,
    name,
    slug,
    default_sender_id,
    sender_emails,
    cmp_webhook_url,
    cmp_webhook_token,
    is_active,
    mailchimp_api_key="",
    mailchimp_list_id="",
    mailchimp_enabled=False,
):
    if client is None:
        client = Client.objects.create(
            organization=organization,
            name=name,
            slug=slug,
            default_sender_id=default_sender_id,
            sender_emails=sender_emails,
            cmp_webhook_url=cmp_webhook_url,
            cmp_webhook_token=cmp_webhook_token,
            mailchimp_api_key=mailchimp_api_key,
            mailchimp_list_id=mailchimp_list_id,
            mailchimp_enabled=mailchimp_enabled,
            is_active=is_active,
        )
        audit(
            actor,
            "client.create",
            client,
            {
                "organization": organization.slug,
                "slug": slug,
                "default_sender_id": default_sender_id,
                "sender_emails": sender_emails,
                "cmp_webhook_url": cmp_webhook_url,
                "cmp_webhook_token_configured": bool(cmp_webhook_token),
                "mailchimp_api_key_configured": bool(mailchimp_api_key),
                "mailchimp_list_id": mailchimp_list_id,
                "mailchimp_enabled": mailchimp_enabled,
                "is_active": is_active,
            },
        )
        return client
    # An empty API key on edit means "keep the current key" (the operator form
    # never renders the stored secret back).
    # Baseline against the stored row: when this is called from the operator
    # form, ``form.is_valid()`` has already mutated ``client`` in place, so
    # comparing against ``client`` would detect no changes.
    stored = Client.objects.get(pk=client.pk)
    if not mailchimp_api_key:
        mailchimp_api_key = stored.mailchimp_api_key
    changed = {}
    changed_fields = set()
    for field, value in {
        "name": name,
        "slug": slug,
        "default_sender_id": default_sender_id,
        "sender_emails": sender_emails,
        "cmp_webhook_url": cmp_webhook_url,
        "cmp_webhook_token": cmp_webhook_token,
        "mailchimp_api_key": mailchimp_api_key,
        "mailchimp_list_id": mailchimp_list_id,
        "mailchimp_enabled": mailchimp_enabled,
        "is_active": is_active,
    }.items():
        old = getattr(stored, field)
        if old != value:
            if field in {"cmp_webhook_token", "mailchimp_api_key"}:
                changed[f"{field}_configured"] = [
                    bool(old),
                    bool(value),
                ]
            else:
                changed[field] = [old, value]
            setattr(client, field, value)
            changed_fields.add(field)
    if changed:
        client.save(update_fields=sorted(changed_fields | {"updated_at"}))
        audit(actor, "client.update", client, changed)
    return client


@transaction.atomic
def create_api_key(*, actor, client, name, notes=""):
    api_key, raw_key = create_client_api_key(client=client, name=name, notes=notes)
    audit(
        actor,
        "client.api_key.create",
        client,
        {"client": client.slug, "key_id": api_key.id, "key_prefix": api_key.display_prefix, "key_name": api_key.name},
    )
    return api_key, raw_key


@transaction.atomic
def revoke_api_key(*, actor, api_key):
    if api_key.revoked_at:
        return False
    api_key.revoked_at = timezone.now()
    api_key.save(update_fields=["revoked_at", "updated_at"])
    audit(
        actor,
        "client.api_key.revoke",
        api_key.client,
        {
            "client": api_key.client.slug,
            "key_id": api_key.id,
            "key_prefix": api_key.display_prefix,
            "key_name": api_key.name,
        },
    )
    return True


def client_api_keys_for_detail(client):
    return ClientApiKey.objects.filter(client=client).order_by("revoked_at", "name", "created_at")


@transaction.atomic
def update_contact_state(*, actor, contact, verified_state, validation_status, validation_reason, suppression_flags):
    now = timezone.now()
    changed = {}
    verified_at = (
        contact.verified_at or now
        if verified_state == "verified"
        else None
        if verified_state == "unverified"
        else contact.verified_at
    )
    if contact.verified_at != verified_at:
        changed["verified_at"] = [contact.verified_at.isoformat() if contact.verified_at else None, bool(verified_at)]
        contact.verified_at = verified_at

    if validation_status != contact.email_validation_status or validation_reason != contact.email_validation_reason:
        changed["email_validation"] = [contact.email_validation_status, validation_status]
        contact.email_validation_status = validation_status
        contact.email_validation_reason = validation_reason
        contact.email_validated_at = None if validation_status == EmailValidationStatus.UNKNOWN else now

    for flag, field in {
        "global_unsubscribed": "global_unsubscribed_at",
        "hard_bounced": "hard_bounced_at",
        "complained": "complained_at",
    }.items():
        desired = now if suppression_flags.get(flag) else None
        current = getattr(contact, field)
        if bool(current) != bool(desired):
            changed[field] = [bool(current), bool(desired)]
            setattr(contact, field, desired)

    if changed:
        contact.save(
            update_fields=[
                "verified_at",
                "email_validation_status",
                "email_validation_reason",
                "email_validated_at",
                "global_unsubscribed_at",
                "hard_bounced_at",
                "complained_at",
                "updated_at",
            ]
        )
        audit(actor, "contact.state.update", contact, changed)
    return bool(changed)


@transaction.atomic
def update_subscription(*, actor, contact, audience, client, status, unsubscribe_reason, verified):
    if client and client.organization_id != audience.organization_id:
        raise ValueError("Client must belong to the selected audience organization.")
    now = timezone.now()
    subscription, created = Subscription.objects.get_or_create(contact=contact, audience=audience, client=client)
    old = {
        "status": subscription.status,
        "unsubscribe_reason": subscription.unsubscribe_reason,
        "verified": subscription.verified_at is not None,
    }
    subscription.status = status
    subscription.verified_at = now if verified else None
    subscription.unsubscribed_at = now if status == SubscriptionStatus.UNSUBSCRIBED else None
    subscription.unsubscribe_reason = unsubscribe_reason if status == SubscriptionStatus.UNSUBSCRIBED else ""
    changed = created or old != {
        "status": subscription.status,
        "unsubscribe_reason": subscription.unsubscribe_reason,
        "verified": subscription.verified_at is not None,
    }
    if changed:
        subscription.save()
        audit(
            actor,
            "contact.subscription.update",
            contact,
            {
                "audience": audience.slug,
                "client": client.slug if client else "audience",
                "status": status,
                "created": created,
            },
        )
    return subscription, changed


@transaction.atomic
def add_contact_tag(*, actor, contact, audience, tag=None, name="", slug=""):
    if tag is None:
        tag_slug = normalize_tag_slug(slug or name)
        tag, _ = Tag.objects.get_or_create(audience=audience, slug=tag_slug, defaults={"name": name or tag_slug})
    elif tag.audience_id != audience.id:
        raise ValueError("Tag must belong to the selected audience.")
    membership, created = ContactTag.objects.get_or_create(contact=contact, tag=tag)
    if created:
        audit(actor, "contact.tag.add", contact, {"audience": audience.slug, "tag": tag.slug})
    return membership, created


@transaction.atomic
def remove_contact_tag(*, actor, contact, tag):
    deleted, _ = ContactTag.objects.filter(contact=contact, tag=tag).delete()
    if deleted:
        audit(actor, "contact.tag.remove", contact, {"audience": tag.audience.slug, "tag": tag.slug})
    return bool(deleted)


@transaction.atomic
def assume_recipient_sent(*, actor, campaign, recipient):
    """Resolve one failed recipient as sent without another delivery attempt.

    Relay has no ambiguous delivery state; the failed row is where an operator
    records that the message actually reached the recipient despite the send
    error. The provider is never called: the recipient keeps its empty
    ``ses_message_id`` and its original error text as diagnostics, while the
    disposition moves to ``sent`` through an explicit email event and an
    operator audit entry.
    """
    # Every campaign writer locks the parent before its recipient.
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    recipient = (
        CampaignRecipient.objects.select_for_update(of=("self",))
        .select_related("campaign", "campaign__client", "campaign__audience", "contact")
        .get(pk=recipient.pk, campaign=campaign)
    )
    if recipient.status != CampaignRecipientStatus.FAILED:
        raise CampaignRecipientConflict("recipient_not_failed")

    prior_status = recipient.status
    now = timezone.now()
    recipient.status = CampaignRecipientStatus.SENT
    if recipient.sent_at is None:
        recipient.sent_at = now
    recipient.save(update_fields=["status", "sent_at", "updated_at"])

    EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=EmailEventType.SENT,
        metadata={"assumed_sent": True},
        created_at=now,
    )
    audit(
        actor,
        "campaign.recipient.assume_sent",
        recipient,
        {"campaign": campaign.id, "prior_status": prior_status},
    )
    refresh_campaign_send_counts(campaign.id)
    return recipient


MAX_SUBSCRIPTION_DELETE_IDS = 1000
SUBSCRIPTION_DELETE_FIELDS = ("id", "audience_id", "client_id", "status", "verified_at")


class SubscriptionDeleteRejected(ValueError):
    """The request was refused as a whole; nothing was deleted."""

    def __init__(self, code, fields=None):
        super().__init__(code)
        self.code, self.fields = code, fields or {}


def parse_subscription_ids(raw_ids):
    """Validate an explicit id list: positive integers, no duplicates, bounded size."""
    if not isinstance(raw_ids, list) or not raw_ids:
        raise SubscriptionDeleteRejected("ids_required", {"ids": "non_empty_list_required"})
    if len(raw_ids) > MAX_SUBSCRIPTION_DELETE_IDS:
        raise SubscriptionDeleteRejected("too_many_ids", {"ids": f"max_{MAX_SUBSCRIPTION_DELETE_IDS}"})
    malformed = [value for value in raw_ids if type(value) is not int or value <= 0]
    if malformed:
        raise SubscriptionDeleteRejected("malformed_ids", {"ids": [str(value) for value in malformed[:20]]})
    if len(set(raw_ids)) != len(raw_ids):
        duplicates = sorted({value for value in raw_ids if raw_ids.count(value) > 1})
        raise SubscriptionDeleteRejected("duplicate_ids", {"ids": duplicates[:20]})
    return sorted(raw_ids)


@transaction.atomic
def delete_subscriptions(*, actor, client, ids, confirm, audit_metadata=None):
    """Delete exactly the Subscription rows named by ``ids`` within ``client``.

    Fail-closed: every id must exist and belong to ``client``; if any id is
    missing or out of scope the whole request is refused and nothing is
    deleted. Without ``confirm`` this is a dry run that only reports the rows.
    The report never includes contact emails or names. Both outcomes write an
    operator audit entry on the client.
    """
    ids = parse_subscription_ids(ids)
    rows = list(
        Subscription.objects.select_for_update().filter(pk__in=ids).order_by("id").values(*SUBSCRIPTION_DELETE_FIELDS)
    )
    found = {row["id"] for row in rows}
    missing = [value for value in ids if value not in found]
    out_of_scope = [row["id"] for row in rows if row["client_id"] != client.id]
    if missing or out_of_scope:
        raise SubscriptionDeleteRejected(
            "ids_not_deletable",
            {key: value for key, value in {"missing": missing, "out_of_scope": out_of_scope}.items() if value},
        )

    deleted = 0
    if confirm:
        deleted, _ = Subscription.objects.filter(pk__in=ids, client=client).delete()
        if deleted != len(ids):  # pragma: no cover - guarded by the row lock above
            raise SubscriptionDeleteRejected("delete_count_mismatch")
    audit(
        actor,
        "subscription.delete" if confirm else "subscription.delete.dry_run",
        client,
        {
            **(audit_metadata or {}),
            "client": client.slug,
            "ids": ids,
            "dry_run": not confirm,
            "confirm": bool(confirm),
            "requested": len(ids),
            "matched": len(rows),
            "deleted": deleted,
        },
    )
    return {
        "dry_run": not confirm,
        "client_id": client.id,
        "requested": len(ids),
        "matched": len(rows),
        "deleted": deleted,
        "subscriptions": rows,
    }
