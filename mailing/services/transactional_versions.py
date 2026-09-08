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
        required_context=template.required_context,
    )


def resolve_render_source(template, version_number=None):
    """Explicit version, else latest published version, else the draft."""
    if version_number is not None:
        version = template.versions.filter(version=version_number).first()
        if version is None:
            raise ApiValidationError({"template_version": "not_found"}, status_code=404)
        return version_render_source(template, version)
    version = latest_version(template)
    if version is not None:
        return version_render_source(template, version)
    return draft_render_source(template)


def render_source_message_fields(source, context):
    """Subject/html/text exactly as a send renders them (preview parity).

    Markdown templates follow the donor render order: Django-template context
    substitution first, then markdown to HTML, then the shared email shell.
    The text part carries the substituted markdown source.
    """
    subject = render_template_string(source.subject, context)
    if source.markdown_body:
        rendered_body = render_template_string(source.markdown_body, context)
        return {
            "subject": subject,
            "html_body": wrap_email_html(subject, render_email_markdown(rendered_body)),
            "text_body": rendered_body,
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
        required_context=template.required_context,
        category=template.category,
    )


def version_payload(version):
    return {
        "version": version.version,
        "subject": version.subject,
        "html_body": version.html_body,
        "text_body": version.text_body,
        "markdown_body": version.markdown_body,
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


def preview_transactional_template(template, context, version_number=None):
    """Render the draft or one version with a context; no rows are written."""
    source = resolve_render_source(template, version_number)
    fields = render_source_message_fields(source, context)
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
    return context, version_number


def validate_template_version_value(value):
    if value in (None, ""):
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiValidationError({"template_version": "must_be_positive_int"})
    return value
