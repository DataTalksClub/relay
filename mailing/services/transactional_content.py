"""The renderable content of a transactional template draft or version.

This module sits between the pure renderer (:mod:`mailing.services.transactional_rendering`)
and the catalog/version services: both drafts and published versions carry the
same content shape, so the shared render path and required-context validation
live here and every caller (sends, preview API, operator UI) goes through
:class:`TemplateContent`.
"""

from dataclasses import dataclass

from mailing.models import TemplateContentFormat
from mailing.services.api_errors import ApiValidationError
from mailing.services.transactional_rendering import render_template_content

CONTENT_FORMATS = (TemplateContentFormat.HTML, TemplateContentFormat.MARKDOWN)


@dataclass(frozen=True)
class ContextRequirement:
    name: str
    description: str = ""


def normalize_required_context(value):
    """Normalize a required-context payload into ContextRequirement items.

    Accepts plain strings and ``{name, description}`` items; drops blanks.
    """
    requirements = []
    for item in value or []:
        if isinstance(item, str):
            name = item.strip()
            description = ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
        else:
            continue
        if name:
            requirements.append(ContextRequirement(name=name, description=description))
    return tuple(requirements)


def required_context_names(value):
    return tuple(requirement.name for requirement in normalize_required_context(value))


def validate_required_context_names(names, context):
    """Fail with 400 ``context.<name>: required`` for each missing key.

    Shared by every send path and the preview API. Send paths pass the
    resolved published version's required context, so validation is always
    against the content that will actually render.
    """
    errors = {}
    for name in names:
        if name not in context or context[name] in (None, ""):
            errors[f"context.{name}"] = "required"
    if errors:
        raise ApiValidationError(errors)


class TemplateContent:
    """The renderable content of a draft or a published version."""

    def __init__(
        self,
        *,
        subject,
        content_format,
        html_body="",
        text_body="",
        markdown_body="",
        required_context=None,
        category="",
    ):
        self.subject = subject
        self.content_format = content_format or TemplateContentFormat.HTML
        self.html_body = html_body or ""
        self.text_body = text_body or ""
        self.markdown_body = markdown_body or ""
        self.required_context = normalize_required_context(required_context)
        self.category = category or ""

    @classmethod
    def from_draft(cls, template):
        return cls(
            subject=template.subject,
            content_format=template.content_format,
            html_body=template.html_body,
            text_body=template.text_body,
            markdown_body=template.markdown_body,
            required_context=template.required_context,
            category=template.category,
        )

    @classmethod
    def from_version(cls, version):
        return cls(
            subject=version.subject,
            content_format=version.content_format,
            html_body=version.html_body,
            text_body=version.text_body,
            markdown_body=version.markdown_body,
            required_context=version.required_context,
            category=version.category,
        )

    def as_dict(self):
        return {
            "subject": self.subject,
            "content_format": self.content_format,
            "html_body": self.html_body,
            "text_body": self.text_body,
            "markdown_body": self.markdown_body,
            "required_context": [
                {"name": requirement.name, "description": requirement.description}
                for requirement in self.required_context
            ],
            "category": self.category,
        }

    def render(self, context, *, client_name=""):
        return render_template_content(
            subject=self.subject,
            context=context,
            content_format=self.content_format,
            html_body=self.html_body,
            text_body=self.text_body,
            markdown_body=self.markdown_body,
            client_name=client_name,
        )
