from dataclasses import dataclass

from django.db.models import Count, Q

from mailing.models import EmailTemplate, TransactionalMessage, TransactionalMessageStatus
from mailing.services.operator_ui import Badge, delivery_tone
from mailing.services.transactional_content import (
    ContextRequirement,
    TemplateContent,
    normalize_required_context,
    required_context_names,
    validate_required_context_names,
)


@dataclass(frozen=True)
class TemplateCatalogRow:
    template: EmailTemplate
    requirements: tuple[ContextRequirement, ...]
    hidden_requirement_count: int


@dataclass(frozen=True)
class RecentMessageRow:
    message: object
    badge: Badge


def validate_template_context(template, context):
    validate_required_context_names(required_context_names(template.required_context), context)


def transactional_template_queryset():
    return (
        EmailTemplate.objects.filter(is_transactional=True)
        .select_related("client", "client__organization")
        .annotate(
            queued_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.QUEUED),
                distinct=True,
            ),
            sent_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.SENT),
                distinct=True,
            ),
            skipped_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.SKIPPED),
                distinct=True,
            ),
            failed_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.FAILED),
                distinct=True,
            ),
            total_message_count=Count("transactional_messages", distinct=True),
        )
        .order_by("client__organization__slug", "client__slug", "key")
    )


def filter_transactional_templates(queryset, client_id):
    if str(client_id).isdigit():
        return queryset.filter(client_id=client_id)
    return queryset


def template_catalog_rows(templates, *, visible_requirements=3):
    rows = []
    for template in templates:
        requirements = normalize_required_context(template.required_context)
        rows.append(
            TemplateCatalogRow(
                template=template,
                requirements=requirements[:visible_requirements],
                hidden_requirement_count=max(len(requirements) - visible_requirements, 0),
            )
        )
    return rows


def transactional_queue_queryset(client):
    return (
        TransactionalMessage.objects.filter(client=client, status=TransactionalMessageStatus.QUEUED)
        .select_related("contact", "template")
        .order_by("created_at", "id")
    )


def recent_message_rows(messages):
    return tuple(
        RecentMessageRow(
            message=message,
            badge=Badge(message.get_status_display(), delivery_tone(message.status)),
        )
        for message in messages
    )


def catalog_context(template):
    requirements = normalize_required_context(template.required_context)
    example_context = template.example_context if isinstance(template.example_context, dict) else {}
    return {
        "template": template,
        "requirements": requirements,
        "example_context": example_context,
        "preview": render_preview(template, example_context),
    }


def render_preview(template, example_context, *, max_chars=1200):
    """Operator UI preview of the draft via the one shared render path."""
    rendered = TemplateContent.from_draft(template).render(example_context, client_name=template.client.name)
    preview = {
        "subject": rendered["subject"],
        "text_body": rendered["text"],
        "html_body": rendered["html"],
    }
    return {key: truncate(value, max_chars) for key, value in preview.items() if value}


def truncate(value, max_chars):
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}..."
