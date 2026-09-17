from django.template import Context, Template
from django.template.loader import render_to_string

from mailing.models import TemplateContentFormat
from mailing.services.transactional_markdown import markdown_to_plain_text, render_markdown_html


def render_template_string(value, context):
    if not value:
        return ""
    return Template(value).render(Context(context, autoescape=False))


def render_template_content(
    *,
    subject,
    context,
    content_format=TemplateContentFormat.HTML,
    html_body="",
    text_body="",
    markdown_body="",
    client_name="",
):
    """Render one template draft or version into subject/html/text.

    This is the single render code path shared by the template preview API,
    the operator UI preview helpers, and every transactional send (real and
    ``dry_run``): the same content plus the same context produces identical
    output through all of them.

    Markdown content renders the context into the source FIRST (so Django
    template tags such as ``{% if %}`` blocks still apply), then converts
    markdown to sanitized HTML, then wraps it in the shared HTML email chrome
    with the client footer -- the port of the AISL
    ``_render_template_with_footer`` pipeline. Plain text derives from the
    same context-rendered markdown source. Direct-HTML content keeps the
    classic behavior: subject/html/text each render as Django template
    strings and no wrapper is applied, because templates provisioned through
    the API own their full HTML.
    """
    rendered_subject = render_template_string(subject, context)

    if content_format == TemplateContentFormat.MARKDOWN:
        rendered_markdown = render_template_string(markdown_body, context)
        body_html = render_to_string(
            "mailing/transactional_email_base.html",
            {
                "subject": rendered_subject,
                "header_title": client_name,
                "footer_title": client_name,
                "body_html": render_markdown_html(rendered_markdown),
            },
        )
        return {
            "subject": rendered_subject,
            "html": body_html,
            "text": markdown_to_plain_text(rendered_markdown),
        }

    return {
        "subject": rendered_subject,
        "html": render_template_string(html_body, context),
        "text": render_template_string(text_body, context),
    }
