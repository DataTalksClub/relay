"""AISL body images fit the preview and outgoing template without sending mail."""

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from django.urls import reverse

from mailing.models import Client, EmailLayout, EmailTemplate, Organization, TransactionalMessage
from mailing.services.auth import create_client_api_key
from mailing.services.campaign_template_rendering import render_campaign_fields
from mailing.services.email_layouts import default_layouts
from mailing.services.transactional_markdown import render_email_markdown
from mailing.services.transactional_versions import draft_render_source, render_source_message_fields

HERO_URL = (
    "https://cdn.jsdelivr.net/gh/DataTalksClub/production-ai-agents-for-media-generation"
    "@a6cc756a9e4b4b75d02ebcbe0d754e5159eba646/images/social-preview.png"
)
HERO_MARKDOWN = f'![Production AI Agents for Generative Media]({HERO_URL} "Course hero")'


class Images(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.images = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def image_style(image):
    declarations = {}
    for declaration in image["style"].split(";"):
        name, _, value = declaration.partition(":")
        if name.strip():
            declarations[name.strip()] = value.strip()
    return declarations


@pytest.fixture
def aisl_template(db):
    organization = Organization.objects.create(name="AISL image test", slug="aisl-image-test")
    client = Client.objects.create(
        organization=organization, name="AISL", slug="aisl-website",
        default_sender_id="content",
        sender_emails=[{"id": "content", "email": "content@aishippinglabs.com"}],
    )
    create_client_api_key(client=client, name="Preview test", raw_api_key="image-preview-test-key")
    spec = default_layouts()["aisl-website"]
    EmailLayout.objects.create(client=client, sender_id="", **spec)
    return EmailTemplate.objects.create(
        client=client, key="campaign-broadcast", name="Campaign", subject="{{ subject }}",
        markdown_body="{{ body_markdown }}", default_sender_id="content",
    )


def test_aisl_preview_and_campaign_render_constrain_hero_without_sending(client, aisl_template):
    context = {"subject": "Generative media course", "body_markdown": HERO_MARKDOWN}
    response = client.post(
        reverse("mailing:api_transactional_template_preview", args=[aisl_template.key]),
        data={"context": context}, content_type="application/json",
        HTTP_AUTHORIZATION="Bearer image-preview-test-key",
    )
    assert response.status_code == 200
    preview = response.json()
    campaign = SimpleNamespace(
        template_key=aisl_template.key, template_version=None,
        client=aisl_template.client, client_id=aisl_template.client_id,
        context=context, sender_id="content",
    )
    outgoing = render_campaign_fields(campaign)
    assert outgoing["html_body"] == preview["html_body"]
    assert outgoing["text_body"] == HERO_MARKDOWN
    image, = Images(preview["html_body"]).images
    assert image["src"] == HERO_URL
    assert image["alt"] == "Production AI Agents for Generative Media"
    assert image["title"] == "Course hero"
    assert image["width"] == "536"
    assert image_style(image) == {"width": "auto", "max-width": "100%", "height": "auto"}
    assert not TransactionalMessage.objects.exists()


@pytest.mark.parametrize("width, expected", [("120", "120"), ("2560", "536"), ("0", "536")])
def test_author_dimensions_cannot_force_overflow_or_distort_aspect_ratio(width, expected):
    markdown = HERO_MARKDOWN + f'{{: width="{width}" height="1440" style="width:2560px!important;height:1440px;min-width:2560px;border:0"}}'
    image, = Images(render_email_markdown(markdown, image_max_width=536)).images
    styles = image_style(image)
    assert image["width"] == expected
    assert "height" not in image
    assert styles["max-width"] == "100%"
    assert styles["height"] == "auto"
    assert styles["border"] == "0"
    assert "min-width" not in styles
    if width == "0":
        assert styles["width"] == "auto"
    else:
        assert styles["width"] == f"{expected}px"


def test_markdown_images_for_other_clients_keep_existing_rendering(aisl_template):
    aisl_template.client.slug = "dtc-website"
    aisl_template.client.save(update_fields=["slug"])
    rendered = render_source_message_fields(
        draft_render_source(aisl_template),
        {"subject": "Course", "body_markdown": HERO_MARKDOWN},
    )
    image, = Images(rendered["html_body"]).images
    assert image == {"src": HERO_URL, "alt": "Production AI Agents for Generative Media", "title": "Course hero"}
