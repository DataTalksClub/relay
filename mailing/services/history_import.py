"""Import historical send history from a client that is migrating onto Relay.

AI Shipping Labs keeps its own ``EmailLog``, ``SesEvent``, ``EmailCampaign``
and ``CampaignDelivery`` tables. When it cuts over to Relay those rows have to
land in ``transactional_messages``, ``email_events``, ``campaigns`` and
``campaign_recipients`` with their original timestamps, so the contact history
an operator reads in Relay is continuous across the cutover.

Every import is idempotent on a caller-supplied ``client_reference``:

- messages reuse ``TransactionalMessage.idempotency_key``, which the API
  already exposes as ``client_reference``;
- events use ``EmailEvent.client_reference``;
- campaigns use ``Campaign.external_key``, and their recipients are keyed by
  ``(campaign, contact)``.

Re-running an import therefore creates zero rows. Nothing here sends email or
touches suppression state: it only records what already happened elsewhere.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    TransactionalMessage,
    TransactionalMessageStatus,
    normalize_category_tag,
)
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaigns import refresh_campaign_counts
from mailing.services.contacts import normalize_email

# One request may carry a whole AISL export slice. The cap keeps a single
# transaction bounded; operators with more rows send several batches.
IMPORT_BATCH_LIMIT = 10000

BULK_CHUNK_SIZE = 500

MESSAGE_STATUSES = {value for value, _label in TransactionalMessageStatus.choices}
EVENT_TYPES = {value for value, _label in EmailEventType.choices}
CAMPAIGN_STATUSES = {value for value, _label in CampaignStatus.choices}
RECIPIENT_STATUSES = {value for value, _label in CampaignRecipientStatus.choices}
RECIPIENT_SKIP_REASONS = {value for value, _label in CampaignRecipientSkipReason.choices}


def validate_batch(data, key):
    rows = data.get(key)
    if not isinstance(rows, list):
        raise ApiValidationError({key: "must_be_list"})
    if len(rows) > IMPORT_BATCH_LIMIT:
        raise ApiValidationError({key: "too_many_rows"})
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ApiValidationError({f"{key}.{index}": "must_be_object"})
    return rows


def validate_audience(data, authenticated_client, *, required):
    slug = data.get("audience")
    if slug in (None, ""):
        if required:
            raise ApiValidationError({"audience": "required"})
        return None
    if not isinstance(slug, str) or not slug.strip():
        raise ApiValidationError({"audience": "required"})
    audience = Audience.objects.filter(
        organization=authenticated_client.organization,
        slug=slug.strip(),
    ).first()
    if audience is None:
        raise ApiValidationError({"audience": "not_found"})
    return audience


def validate_client_slug(data, authenticated_client):
    slug = data.get("client")
    if slug in (None, ""):
        return
    if not isinstance(slug, str) or slug.strip() != authenticated_client.slug:
        raise ApiValidationError({"client": "forbidden"}, status_code=403)


def required_text(row, field, label, *, max_length=255):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({f"{label}.{field}": "required"})
    value = value.strip()
    if len(value) > max_length:
        raise ApiValidationError({f"{label}.{field}": "too_long"})
    return value


def optional_text(row, field, label, *, max_length=255, default=""):
    value = row.get(field)
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ApiValidationError({f"{label}.{field}": "must_be_string"})
    value = value.strip()
    if len(value) > max_length:
        raise ApiValidationError({f"{label}.{field}": "too_long"})
    return value


def required_email(row, field, label):
    value = required_text(row, field, label, max_length=320)
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ApiValidationError({f"{label}.{field}": "invalid"})
    return value


def required_datetime(row, field, label):
    value = parse_optional_datetime(row, field, label)
    if value is None:
        raise ApiValidationError({f"{label}.{field}": "required"})
    return value


def parse_optional_datetime(row, field, label):
    value = row.get(field)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ApiValidationError({f"{label}.{field}": "must_be_iso_datetime"})
    parsed = parse_datetime(value.strip())
    if parsed is None:
        raise ApiValidationError({f"{label}.{field}": "must_be_iso_datetime"})
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone=timezone.get_current_timezone())
    return parsed


def optional_count(row, field, label):
    value = row.get(field)
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiValidationError({f"{label}.{field}": "must_be_integer"})
    if value < 0:
        raise ApiValidationError({f"{label}.{field}": "must_not_be_negative"})
    return value


def optional_choice(row, field, label, allowed, *, default=""):
    value = row.get(field)
    if value in (None, ""):
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ApiValidationError({f"{label}.{field}": "unsupported"})
    return value


def optional_mapping(row, field, label):
    value = row.get(field)
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ApiValidationError({f"{label}.{field}": "must_be_object"})
    return value


def resolve_contacts(emails):
    """Return ``{normalized_email: Contact}``, creating the missing addresses.

    Bulk resolution keeps a ten-thousand-row import to a couple of queries
    instead of one ``get_or_create`` per row. ``Contact.save`` normalizes the
    address, and ``bulk_create`` skips ``save``, so both columns are set here.
    """
    wanted = {normalize_email(email): email.strip() for email in emails}
    if not wanted:
        return {}

    contacts = {
        contact.normalized_email: contact
        for contact in Contact.objects.filter(normalized_email__in=list(wanted))
    }
    missing = [
        Contact(email=original, normalized_email=normalized)
        for normalized, original in wanted.items()
        if normalized not in contacts
    ]
    if missing:
        Contact.objects.bulk_create(missing, batch_size=BULK_CHUNK_SIZE)
        for contact in Contact.objects.filter(normalized_email__in=[row.normalized_email for row in missing]):
            contacts[contact.normalized_email] = contact
    return contacts


def resolve_templates(client, template_keys):
    """Return ``{key: EmailTemplate}`` for the client, adding placeholders.

    A historical message names a template that may never have existed in
    Relay. The placeholder carries the key so the history reads correctly and
    is left inactive so it cannot be picked for a new send.
    """
    keys = {key for key in template_keys if key}
    if not keys:
        return {}

    templates = {
        template.key: template for template in EmailTemplate.objects.filter(client=client, key__in=list(keys))
    }
    missing = [
        EmailTemplate(
            client=client,
            key=key,
            name=key,
            description="Placeholder created by the history import.",
            subject="",
            is_transactional=True,
            is_active=False,
        )
        for key in sorted(keys - set(templates))
    ]
    if missing:
        EmailTemplate.objects.bulk_create(missing, batch_size=BULK_CHUNK_SIZE)
        for template in EmailTemplate.objects.filter(client=client, key__in=[row.key for row in missing]):
            templates[template.key] = template
    return templates


def apply_original_created_at(model, rows):
    """Restore the historical ``created_at`` that ``auto_now_add`` overwrote.

    ``created_at`` is not editable, so an insert always stamps "now". The
    import writes the row first and then replays the original timestamp, which
    is what the contact history orders on.
    """
    rows = [row for row in rows if row.pk is not None]
    if rows:
        model.objects.bulk_update(rows, ["created_at"], batch_size=BULK_CHUNK_SIZE)


def import_result(total, created, skipped):
    return {"total": total, "created": created, "skipped": skipped}


@transaction.atomic
def import_messages_for_client(data, authenticated_client):
    validate_client_slug(data, authenticated_client)
    rows = validate_batch(data, "messages")

    parsed = []
    seen_references = set()
    for index, row in enumerate(rows):
        label = f"messages.{index}"
        reference = required_text(row, "client_reference", label)
        if reference in seen_references:
            raise ApiValidationError({f"{label}.client_reference": "duplicate_in_batch"})
        seen_references.add(reference)
        parsed.append(
            {
                "client_reference": reference,
                "email": required_email(row, "email", label),
                "template_key": required_text(row, "template_key", label, max_length=120),
                "created_at": required_datetime(row, "created_at", label),
                "status": optional_choice(
                    row,
                    "status",
                    label,
                    MESSAGE_STATUSES,
                    default=TransactionalMessageStatus.SENT,
                ),
                "subject": optional_text(row, "subject", label),
                "from_email_id": optional_text(row, "from_email_id", label, max_length=80),
                "from_email": optional_text(row, "from_email", label, max_length=320),
                "ses_message_id": optional_text(row, "ses_message_id", label),
                "sent_at": parse_optional_datetime(row, "sent_at", label),
                "delivered_at": parse_optional_datetime(row, "delivered_at", label),
                "first_opened_at": parse_optional_datetime(row, "first_opened_at", label),
                "first_clicked_at": parse_optional_datetime(row, "first_clicked_at", label),
                "open_count": optional_count(row, "open_count", label),
                "click_count": optional_count(row, "click_count", label),
                "template_version": row.get("template_version"),
                "metadata": optional_mapping(row, "metadata", label),
                "last_error": optional_text(row, "last_error", label, max_length=2000),
            }
        )

    existing_references = set(
        TransactionalMessage.objects.filter(
            client=authenticated_client,
            idempotency_key__in=[row["client_reference"] for row in parsed],
        ).values_list("idempotency_key", flat=True)
    )
    new_rows = [row for row in parsed if row["client_reference"] not in existing_references]
    if not new_rows:
        return import_result(len(parsed), 0, len(parsed))

    contacts = resolve_contacts([row["email"] for row in new_rows])
    templates = resolve_templates(authenticated_client, [row["template_key"] for row in new_rows])

    messages = []
    for row in new_rows:
        template_version = row["template_version"]
        if isinstance(template_version, bool) or not isinstance(template_version, int):
            template_version = None
        messages.append(
            TransactionalMessage(
                client=authenticated_client,
                contact=contacts[normalize_email(row["email"])],
                email=row["email"],
                from_email_id=row["from_email_id"],
                from_email=row["from_email"],
                template=templates[row["template_key"]],
                template_key=row["template_key"],
                template_version=template_version,
                status=row["status"],
                idempotency_key=row["client_reference"],
                subject=row["subject"],
                ses_message_id=row["ses_message_id"],
                sent_at=row["sent_at"],
                delivered_at=row["delivered_at"],
                first_opened_at=row["first_opened_at"],
                first_clicked_at=row["first_clicked_at"],
                open_count=row["open_count"],
                click_count=row["click_count"],
                metadata=row["metadata"],
                last_error=row["last_error"],
            )
        )

    TransactionalMessage.objects.bulk_create(messages, batch_size=BULK_CHUNK_SIZE)
    for message, row in zip(messages, new_rows, strict=True):
        message.created_at = row["created_at"]
    apply_original_created_at(TransactionalMessage, messages)

    return import_result(len(parsed), len(messages), len(parsed) - len(messages))


@transaction.atomic
def import_events_for_client(data, authenticated_client):
    validate_client_slug(data, authenticated_client)
    audience = validate_audience(data, authenticated_client, required=False)
    rows = validate_batch(data, "events")

    parsed = []
    seen_references = set()
    for index, row in enumerate(rows):
        label = f"events.{index}"
        reference = required_text(row, "client_reference", label)
        if reference in seen_references:
            raise ApiValidationError({f"{label}.client_reference": "duplicate_in_batch"})
        seen_references.add(reference)
        event_type = required_text(row, "event_type", label, max_length=20)
        if event_type not in EVENT_TYPES:
            raise ApiValidationError({f"{label}.event_type": "unsupported"})
        parsed.append(
            {
                "index": index,
                "client_reference": reference,
                "email": required_email(row, "email", label),
                "event_type": event_type,
                "created_at": required_datetime(row, "created_at", label),
                "message_reference": optional_text(row, "message_reference", label),
                "campaign_reference": optional_text(row, "campaign_reference", label, max_length=180),
                "provider_event_id": optional_text(row, "provider_event_id", label),
                "url": optional_text(row, "url", label, max_length=2048),
                "metadata": optional_mapping(row, "metadata", label),
            }
        )

    existing_references = set(
        EmailEvent.objects.filter(
            client=authenticated_client,
            client_reference__in=[row["client_reference"] for row in parsed],
        ).values_list("client_reference", flat=True)
    )
    new_rows = [row for row in parsed if row["client_reference"] not in existing_references]
    if not new_rows:
        return import_result(len(parsed), 0, len(parsed))

    contacts = resolve_contacts([row["email"] for row in new_rows])
    messages = message_index(authenticated_client, new_rows)
    campaigns = campaign_index(authenticated_client, new_rows)
    recipients = recipient_index(campaigns.values())

    events = []
    for row in new_rows:
        label = f"events.{row['index']}"
        message = None
        if row["message_reference"]:
            message = messages.get(row["message_reference"])
            if message is None:
                raise ApiValidationError({f"{label}.message_reference": "not_found"}, status_code=404)
        campaign = None
        recipient = None
        contact = contacts[normalize_email(row["email"])]
        if row["campaign_reference"]:
            campaign = campaigns.get(row["campaign_reference"])
            if campaign is None:
                raise ApiValidationError({f"{label}.campaign_reference": "not_found"}, status_code=404)
            recipient = recipients.get((campaign.pk, contact.pk))
        events.append(
            EmailEvent(
                campaign=campaign,
                campaign_recipient=recipient,
                transactional_message=message,
                contact=contact,
                client=authenticated_client,
                audience=audience or (campaign.audience if campaign is not None else None),
                event_type=row["event_type"],
                provider_event_id=row["provider_event_id"],
                client_reference=row["client_reference"],
                url=row["url"],
                metadata=row["metadata"],
            )
        )

    EmailEvent.objects.bulk_create(events, batch_size=BULK_CHUNK_SIZE)
    for event, row in zip(events, new_rows, strict=True):
        event.created_at = row["created_at"]
    apply_original_created_at(EmailEvent, events)

    return import_result(len(parsed), len(events), len(parsed) - len(events))


def message_index(client, rows):
    references = {row["message_reference"] for row in rows if row["message_reference"]}
    if not references:
        return {}
    return {
        message.idempotency_key: message
        for message in TransactionalMessage.objects.filter(client=client, idempotency_key__in=list(references))
    }


def campaign_index(client, rows):
    references = {row["campaign_reference"] for row in rows if row["campaign_reference"]}
    if not references:
        return {}
    return {
        campaign.external_key: campaign
        for campaign in Campaign.objects.select_related("audience").filter(
            client=client,
            external_key__in=list(references),
        )
    }


def recipient_index(campaigns):
    campaign_ids = [campaign.pk for campaign in campaigns]
    if not campaign_ids:
        return {}
    return {
        (recipient.campaign_id, recipient.contact_id): recipient
        for recipient in CampaignRecipient.objects.filter(campaign_id__in=campaign_ids)
    }


def parse_campaign_recipient(row, label):
    return {
        "email": required_email(row, "email", label),
        "created_at": parse_optional_datetime(row, "created_at", label),
        "status": optional_choice(
            row,
            "status",
            label,
            RECIPIENT_STATUSES,
            default=CampaignRecipientStatus.SENT,
        ),
        "skip_reason": optional_choice(row, "skip_reason", label, RECIPIENT_SKIP_REASONS),
        "ses_message_id": optional_text(row, "ses_message_id", label),
        "sent_at": parse_optional_datetime(row, "sent_at", label),
        "delivered_at": parse_optional_datetime(row, "delivered_at", label),
        "first_opened_at": parse_optional_datetime(row, "first_opened_at", label),
        "first_clicked_at": parse_optional_datetime(row, "first_clicked_at", label),
        "open_count": optional_count(row, "open_count", label),
        "click_count": optional_count(row, "click_count", label),
        "last_error": optional_text(row, "last_error", label, max_length=2000),
    }


@transaction.atomic
def import_campaigns_for_client(data, authenticated_client):
    validate_client_slug(data, authenticated_client)
    audience = validate_audience(data, authenticated_client, required=True)
    rows = validate_batch(data, "campaigns")

    parsed = []
    seen_references = set()
    recipient_total = 0
    for index, row in enumerate(rows):
        label = f"campaigns.{index}"
        reference = required_text(row, "client_reference", label, max_length=180)
        if reference in seen_references:
            raise ApiValidationError({f"{label}.client_reference": "duplicate_in_batch"})
        seen_references.add(reference)

        raw_recipients = row.get("recipients") or []
        if not isinstance(raw_recipients, list):
            raise ApiValidationError({f"{label}.recipients": "must_be_list"})
        if len(raw_recipients) > IMPORT_BATCH_LIMIT:
            raise ApiValidationError({f"{label}.recipients": "too_many_rows"})
        recipients = []
        seen_emails = set()
        for recipient_index, raw_recipient in enumerate(raw_recipients):
            recipient_label = f"{label}.recipients.{recipient_index}"
            if not isinstance(raw_recipient, dict):
                raise ApiValidationError({recipient_label: "must_be_object"})
            recipient = parse_campaign_recipient(raw_recipient, recipient_label)
            normalized = normalize_email(recipient["email"])
            if normalized in seen_emails:
                raise ApiValidationError({f"{recipient_label}.email": "duplicate_in_batch"})
            seen_emails.add(normalized)
            recipients.append(recipient)
        recipient_total += len(recipients)

        parsed.append(
            {
                "client_reference": reference,
                "subject": required_text(row, "subject", label),
                "created_at": required_datetime(row, "created_at", label),
                "status": optional_choice(row, "status", label, CAMPAIGN_STATUSES, default=CampaignStatus.SENT),
                "preview_text": optional_text(row, "preview_text", label),
                "html_body": optional_text(row, "html_body", label, max_length=1_000_000),
                "text_body": optional_text(row, "text_body", label, max_length=1_000_000),
                "category_tag": normalize_category_tag(optional_text(row, "category_tag", label, max_length=80)),
                "scheduled_at": parse_optional_datetime(row, "scheduled_at", label),
                "sent_at": parse_optional_datetime(row, "sent_at", label),
                "metadata": optional_mapping(row, "metadata", label),
                "recipients": recipients,
            }
        )

    existing = {
        campaign.external_key: campaign
        for campaign in Campaign.objects.filter(
            client=authenticated_client,
            external_key__in=[row["client_reference"] for row in parsed],
        )
    }

    new_campaigns = []
    new_campaign_rows = []
    for row in parsed:
        if row["client_reference"] in existing:
            continue
        new_campaigns.append(
            Campaign(
                client=authenticated_client,
                audience=audience,
                external_key=row["client_reference"],
                subject=row["subject"],
                preview_text=row["preview_text"],
                html_body=row["html_body"],
                text_body=row["text_body"],
                status=row["status"],
                scheduled_at=row["scheduled_at"],
                sent_at=row["sent_at"],
                category_tag=row["category_tag"],
                metadata=row["metadata"],
            )
        )
        new_campaign_rows.append(row)

    if new_campaigns:
        Campaign.objects.bulk_create(new_campaigns, batch_size=BULK_CHUNK_SIZE)
        for campaign, row in zip(new_campaigns, new_campaign_rows, strict=True):
            campaign.created_at = row["created_at"]
            existing[campaign.external_key] = campaign
        apply_original_created_at(Campaign, new_campaigns)

    recipients_created = import_campaign_recipients(parsed, existing)

    for campaign in existing.values():
        refresh_campaign_counts(campaign)

    return import_result(len(parsed), len(new_campaigns), len(parsed) - len(new_campaigns)) | {
        "recipients_total": recipient_total,
        "recipients_created": recipients_created,
        "recipients_skipped": recipient_total - recipients_created,
    }


def import_campaign_recipients(parsed, campaigns_by_reference):
    all_emails = [recipient["email"] for row in parsed for recipient in row["recipients"]]
    if not all_emails:
        return 0
    contacts = resolve_contacts(all_emails)

    campaign_ids = [campaigns_by_reference[row["client_reference"]].pk for row in parsed]
    known_pairs = set(
        CampaignRecipient.objects.filter(campaign_id__in=campaign_ids).values_list("campaign_id", "contact_id")
    )

    new_recipients = []
    original_created_at = []
    for row in parsed:
        campaign = campaigns_by_reference[row["client_reference"]]
        for recipient in row["recipients"]:
            contact = contacts[normalize_email(recipient["email"])]
            if (campaign.pk, contact.pk) in known_pairs:
                continue
            known_pairs.add((campaign.pk, contact.pk))
            new_recipients.append(
                CampaignRecipient(
                    campaign=campaign,
                    contact=contact,
                    email=recipient["email"],
                    status=recipient["status"],
                    skip_reason=recipient["skip_reason"],
                    ses_message_id=recipient["ses_message_id"],
                    sent_at=recipient["sent_at"],
                    delivered_at=recipient["delivered_at"],
                    first_opened_at=recipient["first_opened_at"],
                    first_clicked_at=recipient["first_clicked_at"],
                    open_count=recipient["open_count"],
                    click_count=recipient["click_count"],
                    last_error=recipient["last_error"],
                )
            )
            original_created_at.append(recipient["created_at"] or row["created_at"])

    if not new_recipients:
        return 0

    CampaignRecipient.objects.bulk_create(new_recipients, batch_size=BULK_CHUNK_SIZE)
    for recipient, created_at in zip(new_recipients, original_created_at, strict=True):
        recipient.created_at = created_at
    apply_original_created_at(CampaignRecipient, new_recipients)
    return len(new_recipients)
