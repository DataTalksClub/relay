import re
from html.parser import HTMLParser
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.utils import timezone

from mailing.services.transactional_catalog import catalog_context, render_preview


class PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames = []
        self.host_scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.frames.append(dict(attrs))
        if tag == "script":
            self.host_scripts.append(dict(attrs))


def template_example(**overrides):
    values = {
        "id": 1, "name": "Welcome", "key": "welcome", "is_active": True,
        "is_transactional": True, "description": "", "updated_at": timezone.now(),
        "client": SimpleNamespace(id=1, name="Courses", slug="courses"),
        "subject": "Hello {{ name }}", "text_body": "Welcome {{ name }}",
        "html_body": "<p>Welcome {{ name }}</p>", "example_context": {"name": "Alex"},
        "required_context": [{"name": "name", "description": "Recipient's first name"}],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_catalog_preserves_full_preview_and_legacy_truncation():
    template = template_example(html_body="<p>" + "message " * 220 + "END_OF_EMAIL</p>")
    context = catalog_context(template)
    assert "END_OF_EMAIL" in context["full_preview"]["html_body"]
    assert context["preview"]["html_body"].endswith("...")
    assert "END_OF_EMAIL" not in render_preview(template, template.example_context)["html_body"]
    assert context["full_preview"]["subject"] == "Hello Alex"
    html = render_to_string("mailing/operator/template_detail.html", context)
    parser = PreviewParser()
    parser.feed(html)
    assert len(parser.frames) == 1
    assert "END_OF_EMAIL" in parser.frames[0]["srcdoc"]
    assert html.index('id="preview"') < html.index("Developer setup and template source")


def test_visual_preview_is_escaped_and_sandboxed():
    body = '<p title="sample">Hello</p><script src="https://evil.example/test.js"></script>'
    template = template_example(html_body=body, text_body=body, subject=body)
    html = render_to_string("mailing/operator/template_detail.html", catalog_context(template))
    parser = PreviewParser()
    parser.feed(html)
    assert parser.frames[0]["sandbox"] == ""
    assert parser.frames[0]["referrerpolicy"] == "no-referrer"
    assert parser.frames[0]["srcdoc"] == body
    assert parser.frames[0]["title"] == "Template email preview"
    assert 'srcdoc="&lt;p title=&quot;sample&quot;' in html
    assert not any(script.get("src") == "https://evil.example/test.js" for script in parser.host_scripts)


def test_plain_text_only_template_is_visible_without_html_frame():
    template = template_example(html_body="", text_body="Welcome {{ name }}")
    html = render_to_string("mailing/operator/template_detail.html", catalog_context(template))
    parser = PreviewParser()
    parser.feed(html)
    assert parser.frames == []
    assert re.search(r'<details class="[^"]*secondary-section[^"]*" open>', html)
    assert "Welcome Alex" in html


def test_catalog_empty_state_offers_real_creation_action():
    html = render_to_string("mailing/operator/template_catalog.html", {
        "active_client": SimpleNamespace(id=1, name="Courses"), "template_rows": [],
    })
    assert "No transactional templates are configured for Courses" in html
    assert 'href="/admin/mailing/emailtemplate/add/"' in html
    assert "Adjust the client filter" not in html
    assert "seed" not in html
