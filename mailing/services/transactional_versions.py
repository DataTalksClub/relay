"""Immutable published versions for email templates.

A template has one editable draft (the template row itself) and N immutable
published versions. Sends and previews render through a render source: an
explicit version when the request names one, else the latest published
version, else the draft for templates that have never been published (that
keeps the pre-versions behavior for templates provisioned without versions;
once a version exists, every send renders the published snapshot).

This module holds the version/render mechanics. The API orchestration that
resolves a template key and applies the test-send allowlist lives in
:mod:`mailing.services.transactional`.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from mailing.models import EmailTemplate, EmailTemplateVersion
from mailing.services.api import ApiValidationError, isoformat
from mailing.services.transactional_catalog import normalize_required_context
from mailing.services.transactional_markdown import render_email_markdown, wrap_email_html
from mailing.services.transactional_rendering import render_template_string


@dataclass(frozen=True)
class TemplateRenderSource:
    """Where a send or preview renders from: a published version or the draft."""

    template: EmailTemplate
    version: EmailTemplateVersion | None
    subject: str
    html_body: str
    text_body: str
    markdown_body: str
    markdown_layout_context: bool
    required_context: object

    @property
    def version_number(self):
        return self.version.version if self.version is not None else None


def latest_version(template):
    return template.versions.order_by("-version").first()


def version_render_source(template, version):
    return TemplateRenderSource(
        template=template,
        version=version,
        subject=version.subject,
        html_body=version.html_body,
        text_body=version.text_body,
        markdown_body=version.markdown_body,
        markdown_layout_context=version.markdown_layout_context,
        required_context=version.required_context,
    )


def draft_render_source(template):
    return TemplateRenderSource(
        template=template,
        version=None,
        subject=template.subject,
        html_body=template.html_body,
        text_body=template.text_body,
        markdown_body=template.markdown_body,
        markdown_layout_context=template.markdown_layout_context,
        required_context=template.required_context,
    )


def resolve_render_source(template, version_number=None, *, draft=False):
    """Explicit draft/version, else latest published version, else the draft."""
    if draft:
        return draft_render_source(template)
    if version_number is not None:
        version = template.versions.filter(version=version_number).first()
        if version is None:
            raise ApiValidationError({"template_version": "not_found"}, status_code=404)
        return version_render_source(template, version)
    version = latest_version(template)
    if version is not None:
        return version_render_source(template, version)
    return draft_render_source(template)


MARKDOWN_LAYOUT_CONTEXT_KEYS = (
    "footer_note",
    "verify_email_url",
    "unsubscribe_url",
)
URL_LAYOUT_CONTEXT_KEYS = frozenset({"verify_email_url", "unsubscribe_url"})
URL_VALIDATOR = URLValidator(schemes=("http", "https"))


def validate_markdown_layout_context(source, context):
    """Return the explicitly approved layout subset for an opted-in version."""
    if not source.markdown_body or not source.markdown_layout_context:
        return {}

    errors = {}
    cleaned = {}
    for name in MARKDOWN_LAYOUT_CONTEXT_KEYS:
        value = context.get(name, "")
        if value in (None, ""):
            cleaned[name] = ""
            continue
        if not isinstance(value, str):
            errors[f"context.{name}"] = "must_be_string"
            continue
        limit = 2048 if name in URL_LAYOUT_CONTEXT_KEYS else 500
        if len(value) > limit:
            errors[f"context.{name}"] = "too_long"
            continue
        if name in URL_LAYOUT_CONTEXT_KEYS:
            try:
                URL_VALIDATOR(value)
            except ValidationError:
                errors[f"context.{name}"] = "invalid_url"
                continue
        cleaned[name] = value
    if errors:
        raise ApiValidationError(errors)
    return cleaned


def markdown_plain_text(source, rendered_body, layout_context, *, sender_id=""):
    """Append the same approved actions that the HTML layout renders."""
    if not layout_context:
        return rendered_body

    from mailing.services.email_layouts import get_layout_for_client  # noqa: PLC0415

    layout, _ = get_layout_for_client(source.template.client, sender_id=sender_id)
    sections = [rendered_body]
    if layout is not None and layout.brand_name:
        sections.append(layout.brand_name)
    if layout_context["footer_note"]:
        sections.append(layout_context["footer_note"].strip())
    if layout_context["verify_email_url"]:
        sections.append(
            "Your email is not verified on our platform.\n"
            f"Verify your email: {layout_context['verify_email_url']}"
        )
    if layout_context["unsubscribe_url"]:
        sections.append(f"Unsubscribe from all emails: {layout_context['unsubscribe_url']}")
    return "\n\n".join(section for section in sections if section).strip()


def render_source_message_fields(source, context, *, sender_id=""):
    """Subject/html/text exactly as a send renders them (preview parity).

    Markdown templates follow the donor render order: Django-template context
    substitution first, then markdown to HTML, then the CLIENT'S OWN layout
    shell. The text part carries the substituted markdown source plus any
    version-approved layout actions.
    """
    subject = render_template_string(source.subject, context)
    if source.markdown_body:
        rendered_body = render_template_string(source.markdown_body, context)
        layout_context = validate_markdown_layout_context(source, context)
        return {
            "subject": subject,
            "html_body": wrap_email_html(
                subject,
                render_email_markdown(rendered_body),
                client=source.template.client,
                sender_id=sender_id,
                markdown_layout_context=layout_context,
            ),
            "text_body": markdown_plain_text(
                source, rendered_body, layout_context, sender_id=sender_id
            ),
        }
    return {
        "subject": subject,
        "html_body": render_template_string(source.html_body, context),
        "text_body": render_template_string(source.text_body, context),
    }


def publish_transactional_template(template):
    """Snapshot the draft into a new immutable version (max version + 1)."""
    current = latest_version(template)
    return EmailTemplateVersion.objects.create(
        template=template,
        version=(current.version + 1) if current is not None else 1,
        subject=template.subject,
        html_body=template.html_body,
        text_body=template.text_body,
        markdown_body=template.markdown_body,
        markdown_layout_context=template.markdown_layout_context,
        required_context=template.required_context,
        category=template.category,
    )


def version_payload(version):
    return {
        "template_key": version.template.key,
        "version": version.version,
        "subject": version.subject,
        "html_body": version.html_body,
        "text_body": version.text_body,
        "markdown_body": version.markdown_body,
        "markdown_layout_context": version.markdown_layout_context,
        "required_context": version.required_context,
        "category": version.category,
        "created_at": isoformat(version.created_at),
    }


def versions_payload(template):
    versions = list(template.versions.order_by("-version"))
    return {
        "template_key": template.key,
        "latest_version": versions[0].version if versions else None,
        "versions": [version_payload(version) for version in versions],
    }


def preview_transactional_template(
    template, context, version_number=None, *, draft=False
):
    """Render the explicit draft/version with a context; no rows are written."""
    source = resolve_render_source(template, version_number, draft=draft)
    fields = render_source_message_fields(source, context, sender_id=template.default_sender_id or "")
    return fields | {
        "template_key": template.key,
        "template_version": source.version_number,
        "published": source.version is not None,
        "missing_context": missing_context_names(source.required_context, context),
    }


def missing_context_names(required_context, context):
    return [
        requirement.name
        for requirement in normalize_required_context(required_context)
        if requirement.name not in context or context[requirement.name] in (None, "")
    ]


def validate_preview_payload(data):
    """Context and optional version for a preview request."""
    context = data.get("context", {})
    if context in (None, ""):
        context = {}
    if not isinstance(context, dict):
        raise ApiValidationError({"context": "must_be_object"})

    version_number = validate_template_version_value(data.get("template_version"))
    draft = data.get("draft", False)
    if not isinstance(draft, bool):
        raise ApiValidationError({"draft": "must_be_boolean"})
    if draft and version_number is not None:
        raise ApiValidationError({"draft": "conflicts_with_template_version"})
    return context, version_number, draft


def validate_template_version_value(value):
    if value in (None, ""):
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiValidationError({"template_version": "must_be_positive_int"})
    return value
