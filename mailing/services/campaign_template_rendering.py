"""Render Relay campaign templates with immutable per-recipient context."""

from functools import lru_cache

from mailing.models import EmailTemplate


@lru_cache(maxsize=64)
def _render_source(client_id, template_key, template_version):
    from mailing.services.transactional_versions import resolve_render_source  # noqa: PLC0415

    template = EmailTemplate.objects.filter(
        client_id=client_id,
        key=template_key,
        is_active=True,
    ).first()
    if template is None:
        raise ValueError("Campaign template is no longer active.")
    return resolve_render_source(template, template_version)


def render_campaign_fields(campaign, recipient=None, *, context_overrides=None):
    """Return subject and bodies for a static or template-backed campaign."""
    if not getattr(campaign, "template_key", None):
        return {
            "subject": campaign.subject,
            "html_body": campaign.html_body,
            "text_body": campaign.text_body,
        }

    source = _render_source(
        campaign.client_id,
        campaign.template_key,
        campaign.template_version,
    )
    from mailing.services.senders import resolve_sender_email  # noqa: PLC0415
    from mailing.services.transactional_catalog import validate_context_requirements  # noqa: PLC0415
    from mailing.services.transactional_versions import (  # noqa: PLC0415
        render_source_message_fields,
        validate_markdown_layout_context,
    )

    context = dict(campaign.context or {})
    if recipient is not None and isinstance(recipient.context, dict):
        context.update(recipient.context)
    if isinstance(context_overrides, dict):
        context.update(context_overrides)
    validate_context_requirements(source.required_context, context)
    validate_markdown_layout_context(source, context)
    sender = resolve_sender_email(campaign.client, campaign.sender_id)
    return render_source_message_fields(source, context, sender_id=sender.sender_id)
