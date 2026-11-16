"""Tests for DB-backed newsletter templates and live block-rendering rules (no deploy)."""

import json

import pytest

from mailing.models import Client, NewsletterTemplate, Organization
from mailing.services import newsletter_layout as layout
from mailing.services import newsletters as svc
from mailing.services.auth import create_client_api_key
from mailing.tests import newsletter_302_blocks as fx


@pytest.fixture
def org_and_client(db):
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=org, slug="dtc-newsletter", name="DTC Newsletter")
    return org, client


def _build_302_issue(client, key="weekly-302"):
    issue = svc.create_issue(client, {
        "key": key,
        "subject": fx.SUBJECT,
        "preview_text": fx.PREHEADER,
        "issue_number": 302,
        "publication_date": "2026-11-09",
    })
    for block_type, data in fx.BLOCKS:
        svc.add_block(issue, block_type, data)
    return issue


def test_seeded_db_templates_exist_and_302_is_byte_equivalent(org_and_client):
    _, client = org_and_client
    tpl = NewsletterTemplate.objects.get(client__isnull=True, key="dtc-weekly")
    assert tpl.skeleton_html == layout.SKELETON_PATH.read_text(encoding="utf-8")
    assert tpl.footer_text == svc.FOOTER_TEXT
    assert tpl.layout_rules["close_adjacent_promos"] is True

    issue = _build_302_issue(client)
    db_rendered = svc.render_issue(issue)

    # Compare byte-for-byte against direct in-code baseline rendering.
    all_blocks = list(issue.blocks.order_by("position", "id"))
    intro_rows, items, texts = [], [], []
    for item in all_blocks:
        rows = layout.block_rows(
            item.block_type, svc.BLOCK_TYPES[item.block_type],
            lambda name, spec=svc.BLOCK_TYPES[item.block_type], data=item.data: svc._value(spec, data, name),
            svc._valid_url,
        )
        if item.block_type == "intro":
            intro_rows.append(rows)
        else:
            items.append((item.block_type, rows))
        text = svc.render_block_text(item.block_type, item.data)
        if text.strip():
            texts.append(text)
    view_url = svc.web_view_url(issue)
    fwd_url = svc.forward_url(issue, view_url)
    expected_html = layout.document(
        title=issue.subject,
        preheader=svc.effective_preview_text(issue),
        banner_html="",
        intro_html="".join(intro_rows),
        sections=layout.sections_html(items),
        web_view_url=view_url,
        forward_url=fwd_url,
    )
    expected_text = "\n\n".join([
        f"View this email in your browser ({view_url})",
        "https://datatalks.club/",
        *texts,
        svc.FOOTER_TEXT.replace("{forward_url}", fwd_url),
    ])
    assert db_rendered["html_body"] == expected_html
    assert db_rendered["text_body"] == expected_text


def test_303_adjacent_promo_divider_regression(org_and_client):
    _, client = org_and_client
    issue = svc.create_issue(client, {"key": "weekly-303", "subject": "DTC Weekly #303", "template_key": "dtc-weekly"})
    svc.add_block(issue, "intro", {"issue_label": "#303", "summary": "Hello"})
    svc.add_block(issue, "sponsor", {"title": "Sponsor", "cta_text": "Go", "cta_url": "https://example.com"})
    svc.add_block(issue, "course_promo", {"title": "Course", "cta_text": "Join", "cta_url": "https://example.com/c"})
    svc.add_block(issue, "course_update", {"title": "Week 1"})

    html = svc.render_issue(issue)["html_body"]
    assert html.count(f"background-color:{layout.PROMO_BACKGROUND}") == 1
    assert html.index("Sponsor") < html.index(layout.DIVIDER_ROW) < html.index("Course")


def test_editing_db_template_and_rules_applies_immediately_without_deploy(org_and_client):
    _, client = org_and_client
    issue = svc.create_issue(client, {"key": "live-edit", "subject": "Live Edit", "template_key": "dtc-weekly"})
    svc.add_block(issue, "intro", {"issue_label": "#304", "summary": "Intro"})
    svc.add_block(issue, "sponsor", {"title": "Sponsor", "cta_text": "Visit", "cta_url": "https://example.com"})
    svc.add_block(issue, "course_promo", {"title": "Promo", "cta_text": "Join", "cta_url": "https://example.com/p"})

    before = svc.render_issue(issue)
    assert "#ECECFB" in before["html_body"]
    assert "#2056A2" in before["html_body"]
    assert "CUSTOM-LIVE-MARKER" not in before["html_body"]

    # Edit client override in DB (no code change, no deploy, no restart).
    tpl, created = svc.upsert_newsletter_template(client, "dtc-weekly", {
        "skeleton_html": before["html_body"] if False else (
            NewsletterTemplate.objects.get(client__isnull=True, key="dtc-weekly")
            .skeleton_html.replace("<!--RELAY:SECTIONS-->", "<!--CUSTOM-LIVE-MARKER--><!--RELAY:SECTIONS-->")
        ),
        "footer_text": "Custom footer ({forward_url})",
        "layout_rules": {
            "promo_background": "#FFEEDD",
            "brand_blue": "#112233",
            "close_adjacent_promos": False,
        },
    }, actor="editor@datatalks.club")
    assert created is True
    assert tpl.version == 1

    after = svc.render_issue(issue)
    assert "<!--CUSTOM-LIVE-MARKER-->" in after["html_body"]
    assert "#FFEEDD" in after["html_body"]
    assert "#ECECFB" not in after["html_body"]
    assert "#112233" in after["html_body"]
    # With close_adjacent_promos=False, both adjacent promos get the promo background.
    assert after["html_body"].count("background-color:#FFEEDD") == 2
    assert after["text_body"].endswith("Custom footer (" + svc.forward_url(issue, svc.web_view_url(issue)) + ")")


def test_api_newsletter_template_get_and_put_updates_live_render(db, client):
    org = Organization.objects.create(name="API Org", slug="api-org")
    api_client = Client.objects.create(
        organization=org,
        slug="api-dtc",
        name="API DTC",
    )
    _, raw_key = create_client_api_key(client=api_client, name="test")
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

    resp = client.get("/api/newsletter-templates/dtc-weekly", **headers)
    assert resp.status_code == 200
    payload = resp.json()["template"]
    assert payload["key"] == "dtc-weekly"
    assert payload["is_custom"] is False

    # Update via API: change promo_background and add a custom footer line.
    put_resp = client.put(
        "/api/newsletter-templates/dtc-weekly",
        data=json.dumps({
            "layout_rules": {"promo_background": "#ABCDEF"},
            "footer_text": "API-UPDATED-FOOTER {forward_url}",
        }),
        content_type="application/json",
        **headers,
    )
    assert put_resp.status_code == 201
    assert put_resp.json()["template"]["is_custom"] is True

    # Create an issue and preview via API; verify the updated template took effect immediately.
    issue_resp = client.put(
        "/api/newsletter-issues/api-live-test",
        data=json.dumps({"subject": "API Live Test", "template": "dtc-weekly"}),
        content_type="application/json",
        **headers,
    )
    assert issue_resp.status_code == 201
    issue_obj = api_client.newsletter_issues.get(key="api-live-test")
    svc.add_block(issue_obj, "sponsor", {"title": "S", "cta_text": "Go", "cta_url": "https://example.com"})

    preview = client.post("/api/newsletter-issues/api-live-test/preview", data="{}", content_type="application/json",
                          **headers)
    assert preview.status_code == 200
    body = preview.json()["preview"]
    assert "#ABCDEF" in body["html_body"]
    assert "API-UPDATED-FOOTER" in body["text_body"]
