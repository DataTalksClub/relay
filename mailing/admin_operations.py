"""JSON counterparts for operator-only actions, sharing UI forms and services."""

from dataclasses import asdict, fields, is_dataclass

from django import forms
from django.db.models import Q, QuerySet
from django.forms.models import model_to_dict
from django.http import JsonResponse
from django.utils import timezone

from jobs.models import Job, JobStatus
from jobs.services import enqueue_job
from mailing import models
from mailing.admin_api import ApiError, admin_endpoint, body, get_record, listing, record_audit, serialize
from mailing.forms import CampaignForm, ClientForm, ContactStateForm, ContactSubscriptionForm, ContactTagAddForm
from mailing.services.campaigns import (
    CampaignRecipientConflict,
    estimate_campaign_recipients,
    queue_campaign,
)
from mailing.services.inbound_email import load_body_text
from mailing.services.inbound_views import BLOCK_ORIGIN_BUTTON, block_sender, mark_read, unblock_sender
from mailing.services.operator_management import (
    add_contact_tag,
    assume_recipient_sent,
    create_or_update_client,
    remove_contact_tag,
    update_contact_state,
    update_subscription,
)
from mailing.services.operator_ui import (
    audience_breakdowns,
    audience_summary,
    campaign_send_progress,
    campaign_stat_groups,
    contact_detail_context,
    contact_explorer_queryset,
    dashboard_context,
    parse_contact_explorer_filters,
)
from mailing.services.worker_status import worker_status_payload

# Explicit read projections keep credentials and signing/tracking tokens out of
# API responses. These are operator resources, not arbitrary database access.
READ_FIELDS = {
    "subscriptions": (
        models.Subscription,
        "id contact_id audience_id client_id status verified_at unsubscribed_at unsubscribe_reason",
    ),
    "contact-tags": (models.ContactTag, "id contact_id tag_id created_at"),
    "campaign-recipients": (
        models.CampaignRecipient,
        "id campaign_id contact_id email status skip_reason sent_at delivered_at first_opened_at first_clicked_at open_count click_count last_error",
    ),
    "client-callbacks": (
        models.ClientCallback,
        "id client_id transactional_message_id status sequence attempt_count last_error created_at",
    ),
    "cmp-callbacks": (
        models.CmpCallback,
        "id client_id status event_type event_id attempt_count last_error created_at",
    ),
    "contacts": (
        models.Contact,
        "id email normalized_email verified_at email_validation_status email_validation_reason global_unsubscribed_at hard_bounced_at complained_at",
    ),
    "tags": (models.Tag, "id audience_id name slug"),
    "campaigns": (
        models.Campaign,
        "id client_id audience_id subject preview_text html_body text_body status scheduled_at include_tags exclude_tags created_at updated_at",
    ),
    "templates": (
        models.EmailTemplate,
        "id client_id key name subject html_body text_body category markdown_body created_at updated_at",
    ),
    "transactional-messages": (
        models.TransactionalMessage,
        "id client_id template_id contact_id email subject status created_at updated_at last_error",
    ),
    "inbound-messages": (
        models.InboundMessage,
        "id sender_address sender_domain subject snippet recipient_address state has_body blocked_rule_id created_at",
    ),
    "inbound-addresses": (models.InboundAddress, "id local_part domain note is_active created_at updated_at"),
    "blocked-senders": (models.BlockedSender, "id value scope origin reason created_at"),
    "audits": (models.OperatorAudit, "id actor_id action target_type target_id metadata created_at"),
    "events": (models.EmailEvent, "id contact_id client_id audience_id campaign_id event_type created_at"),
    "dead-letters": (Job, "id client_id status attempt max_attempts error finished_at run_after created_at updated_at"),
}


def projection(resource, obj):
    return {field: getattr(obj, field) for field in READ_FIELDS[resource][1].split()}


@admin_endpoint(["GET"])
def read_resource(request, resource, record_id=None):
    model, _ = READ_FIELDS[resource]
    queryset = model.objects.all()
    if resource == "dead-letters":
        queryset = queryset.filter(status=JobStatus.FAILED)
    if record_id is not None:
        obj = get_record(model, pk=record_id)
        data = projection(resource, obj)
        if resource == "inbound-messages":
            data["body"] = load_body_text(obj) if obj.has_body else ""
        return JsonResponse(data)
    for field in ("client_id", "audience_id", "contact_id", "campaign_id", "tag_id", "transactional_message_id"):
        if field in READ_FIELDS[resource][1].split() and field in request.GET:
            try:
                value = int(request.GET[field])
            except ValueError:
                raise ApiError("invalid_filter") from None
            queryset = queryset.filter(**{field: value})
    if resource == "contacts":
        client_id = get_record(models.Client, pk=request.GET["client_id"]).pk if request.GET.get("client_id") else None
        audience_id = (
            get_record(models.Audience, pk=request.GET["audience_id"]).pk if request.GET.get("audience_id") else None
        )
        filters = parse_contact_explorer_filters(
            request.GET,
            forced_client_id=client_id,
            forced_audience_id=audience_id,
        )
        queryset = contact_explorer_queryset(filters)
    if resource == "inbound-messages":
        if request.GET.get("state") and request.GET["state"] != "all":
            queryset = queryset.filter(state=request.GET["state"])
        if request.GET.get("address"):
            queryset = queryset.filter(recipient_address__iexact=request.GET["address"].strip())
        if request.GET.get("q"):
            query = request.GET["q"].strip()
            queryset = queryset.filter(
                Q(subject__icontains=query)
                | Q(snippet__icontains=query)
                | Q(sender_address__icontains=query)
                | Q(from_header__icontains=query)
            )
    return listing(request, queryset, lambda obj: projection(resource, obj))


def validated_form(request, form_class, *, instance=None, **kwargs):
    blank = form_class(instance=instance, **kwargs) if issubclass(form_class, forms.ModelForm) else form_class(**kwargs)
    data = body(request, blank.fields)
    for field, value in data.items():
        form_field = blank.fields[field]
        if isinstance(form_field, forms.BooleanField) and type(value) is not bool:
            raise ApiError("validation_error", fields={field: "must_be_boolean"})
        if isinstance(value, (dict, list)) and not isinstance(form_field, forms.ModelMultipleChoiceField):
            raise ApiError("validation_error", fields={field: "invalid_type"})
    if request.method == "PATCH" and instance is not None:
        defaults = model_to_dict(instance)
        defaults.update({name: field.initial for name, field in blank.fields.items() if name not in defaults})
        if isinstance(instance, models.Client):
            defaults["sender_emails"] = "\n".join(f"{s['id']}={s['email']}" for s in instance.sender_emails)
        if isinstance(instance, models.Campaign):
            for field in ("include_tags", "exclude_tags"):
                defaults[field] = list(
                    models.Tag.objects.filter(
                        audience=instance.audience, slug__in=getattr(instance, field)
                    ).values_list("id", flat=True)
                )
        defaults.update(data)
        data = defaults
    form = (
        form_class(data, instance=instance, **kwargs)
        if issubclass(form_class, forms.ModelForm)
        else form_class(data, **kwargs)
    )
    if not form.is_valid():
        raise ApiError("validation_error", fields=form.errors.get_json_data())
    return form


@admin_endpoint(["GET", "PATCH"])
def client_settings(request, client_id):
    client = get_record(models.Client, pk=client_id)
    organization_id = client.organization_id
    if request.method == "PATCH":
        form = validated_form(request, ClientForm, instance=client)
        if form.cleaned_data["organization"].pk != organization_id:
            raise ApiError("organization_is_immutable")
        client = create_or_update_client(actor=request.admin_key.user, client=client, **form.cleaned_data)
        record_audit(request, "admin.client.settings.update", client)
    return JsonResponse(
        {
            "id": client.id,
            "name": client.name,
            "slug": client.slug,
            "organization": client.organization_id,
            "default_sender_id": client.default_sender_id,
            "sender_emails": client.sender_emails,
            "cmp_webhook_url": client.cmp_webhook_url,
            "cmp_webhook_token_configured": bool(client.cmp_webhook_token),
            "mailchimp_api_key_configured": bool(client.mailchimp_api_key),
            "mailchimp_list_id": client.mailchimp_list_id,
            "mailchimp_enabled": client.mailchimp_enabled,
            "is_active": client.is_active,
        }
    )


@admin_endpoint(["POST", "PATCH"])
def campaign_write(request, client_id, campaign_id=None):
    client = get_record(models.Client, pk=client_id)
    campaign = get_record(models.Campaign, pk=campaign_id, client=client) if campaign_id else None
    if (request.method == "POST") != (campaign is None):
        raise ApiError("method_not_allowed", 405)
    campaign = validated_form(request, CampaignForm, instance=campaign, active_client=client).save()
    record_audit(request, "admin.campaign.save", campaign)
    return JsonResponse(projection("campaigns", campaign), status=201 if request.method == "POST" else 200)


@admin_endpoint(["GET", "POST"])
def campaign_queue(request, campaign_id):
    campaign = get_record(models.Campaign, pk=campaign_id)
    if request.method == "GET":
        estimate = estimate_campaign_recipients(campaign)
        return JsonResponse({"estimate": asdict(estimate)})
    if body(request, {"confirm"}).get("confirm") is not True:
        raise ApiError("confirmation_required")
    result = queue_campaign(campaign)
    record_audit(request, "admin.campaign.queue", campaign)
    return JsonResponse(result.__dict__)


@admin_endpoint(["POST"])
def assume_sent(request, campaign_id, recipient_id):
    campaign = get_record(models.Campaign, pk=campaign_id)
    recipient = get_record(models.CampaignRecipient, pk=recipient_id, campaign=campaign)
    try:
        assume_recipient_sent(actor=request.admin_key.user, campaign=campaign, recipient=recipient)
    except CampaignRecipientConflict:
        raise ApiError("recipient_not_failed", 409) from None
    record_audit(request, "admin.campaign.recipient.assume_sent", recipient)
    return JsonResponse({"id": recipient.id, "status": recipient.status})


@admin_endpoint(["POST"])
def contact_action(request, contact_id, action, client_id=None):
    contact = get_record(models.Contact, pk=contact_id)
    actor = request.admin_key.user
    if action == "state":
        values = validated_form(request, ContactStateForm).cleaned_data
        update_contact_state(
            actor=actor,
            contact=contact,
            verified_state=values["verified_state"],
            validation_status=values["email_validation_status"],
            validation_reason=values["email_validation_reason"],
            suppression_flags={k: values[k] for k in ("global_unsubscribed", "hard_bounced", "complained")},
        )
    else:
        client = get_record(models.Client, pk=client_id)
        if action == "subscriptions":
            values = validated_form(request, ContactSubscriptionForm, active_client=client).cleaned_data
            update_subscription(
                actor=actor,
                contact=contact,
                audience=values["audience"],
                client=values["client"],
                status=values["status"],
                unsubscribe_reason=values["unsubscribe_reason"],
                verified=values["verified"],
            )
        elif action == "tags/add":
            values = validated_form(request, ContactTagAddForm, active_client=client).cleaned_data
            add_contact_tag(
                actor=actor,
                contact=contact,
                audience=values["audience"],
                tag=values["tag"],
                name=values["new_tag_name"],
                slug=values["new_tag_slug"],
            )
        else:
            data = body(request, {"tag_id"})
            tag = get_record(models.Tag, pk=data.get("tag_id"), audience__organization=client.organization)
            remove_contact_tag(actor=actor, contact=contact, tag=tag)
    record_audit(request, f"admin.contact.{action}", contact)
    return JsonResponse(projection("contacts", contact))


@admin_endpoint(["POST", "PATCH"])
def tag_write(request, tag_id=None):
    tag = get_record(models.Tag, pk=tag_id) if tag_id else models.Tag()
    data = body(request, {"name", "slug", "audience_id"})
    for field, value in data.items():
        if field == "audience_id":
            if type(value) is not int:
                raise ApiError("invalid_audience_id")
            get_record(models.Audience, pk=value)
            if tag.pk and tag.audience_id != value:
                raise ApiError("audience_is_immutable")
        elif not isinstance(value, str):
            raise ApiError("invalid_type")
        setattr(tag, field, value)
    tag.full_clean()
    tag.save()
    record_audit(request, "admin.tag.save", tag)
    return JsonResponse(projection("tags", tag), status=201 if request.method == "POST" else 200)


@admin_endpoint(["POST", "PATCH"])
def inbound_address_write(request, address_id=None):
    address = get_record(models.InboundAddress, pk=address_id) if address_id else models.InboundAddress()
    data = body(request, {"local_part", "domain", "note", "is_active"})
    for field, value in data.items():
        if field == "is_active" and type(value) is not bool:
            raise ApiError("invalid_type")
        if field != "is_active" and not isinstance(value, str):
            raise ApiError("invalid_type")
        setattr(address, field, value.strip().lower() if field in {"local_part", "domain"} else value)
    address.full_clean()
    address.save()
    record_audit(request, "admin.inbound_address.save", address)
    return JsonResponse(projection("inbound-addresses", address), status=201 if request.method == "POST" else 200)


@admin_endpoint(["POST"])
def inbound_action(request, message_id, action):
    message = get_record(models.InboundMessage, pk=message_id)
    if action == "mark-read":
        mark_read(message)
    elif action == "block":
        data = body(request, {"scope"})
        scope = data.get("scope", "address")
        if scope not in {"address", "domain"}:
            raise ApiError("invalid_scope")
        rule, _ = block_sender(
            message=message,
            scope=scope,
            value=message.sender_address if scope == "address" else message.sender_domain,
            origin=BLOCK_ORIGIN_BUTTON,
            actor=request.admin_key.user,
        )
        if rule is None:
            raise ApiError("missing_sender")
        if message.state == "received":
            message.state, message.blocked_rule = "read", rule
            message.save(update_fields=["state", "blocked_rule", "updated_at"])
    elif message.blocked_rule_id:
        unblock_sender(message.blocked_rule)
    record_audit(request, f"admin.inbound.{action}", message)
    message.refresh_from_db()
    return JsonResponse(projection("inbound-messages", message))


@admin_endpoint(["DELETE"])
def blocked_delete(request, rule_id):
    rule = get_record(models.BlockedSender, pk=rule_id)
    record_audit(request, "admin.blocked_sender.delete", rule)
    unblock_sender(rule)
    return JsonResponse({"deleted": True})


@admin_endpoint(["GET"])
def status(request, kind):
    if kind == "workers":
        return JsonResponse(worker_status_payload())
    client = get_record(models.Client, pk=request.GET["client_id"]) if request.GET.get("client_id") else None
    return JsonResponse(operator_payload(dashboard_context(client)))


def operator_payload(value):
    if isinstance(value, models.Client):
        return serialize(value)
    if isinstance(value, models.Campaign):
        return projection("campaigns", value)
    if isinstance(value, (models.Organization, models.Audience)):
        return serialize(value)
    for resource, (model, _) in READ_FIELDS.items():
        if isinstance(value, model):
            return projection(resource, value)
    if is_dataclass(value):
        return {field.name: operator_payload(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (list, tuple, QuerySet)):
        return [operator_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: operator_payload(item) for key, item in value.items()}
    return value


@admin_endpoint(["GET"])
def overview(request, resource, record_id):
    client = get_record(models.Client, pk=request.GET["client_id"]) if request.GET.get("client_id") else None
    if resource == "contacts":
        obj = get_record(models.Contact, pk=record_id)
        context = contact_detail_context(obj, client)
    elif resource == "audiences":
        obj = get_record(models.Audience, pk=record_id)
        if client and client.organization_id != obj.organization_id:
            raise ApiError("audience_scope_mismatch")
        context = {"summary": audience_summary(obj, client), "breakdowns": audience_breakdowns(obj, client)}
    else:
        obj = get_record(models.Campaign, pk=record_id)
        if client and obj.client_id != client.id:
            raise ApiError("campaign_scope_mismatch")
        context = {"stats": campaign_stat_groups(obj), "send_progress": campaign_send_progress(obj)}
    return JsonResponse({"overview": operator_payload(context)})


@admin_endpoint(["POST"])
def dead_letter_retry(request, task_id):
    job = get_record(Job, pk=task_id, status=JobStatus.FAILED)
    Job.objects.filter(pk=job.pk, status=JobStatus.FAILED).update(
        status=JobStatus.QUEUED,
        attempt=0,
        run_after=timezone.now(),
        task_result_id="",
        error="",
        response_status=None,
        response_body="",
        lease_expires_at=None,
    )
    from django.db import transaction  # noqa: PLC0415

    transaction.on_commit(lambda: enqueue_job(job.pk))
    record_audit(request, "admin.dead_letter.retry", job.client, {"task_id": str(job.pk)})
    return JsonResponse({"id": str(job.pk), "status": JobStatus.QUEUED})
