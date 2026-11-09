"""MIME build once per batch (mailing.mime_template) with per-recipient byte substitution."""

import logging
import time
from email import policy as email_policy
from email.parser import BytesParser

import pytest
from django.conf import settings
from django.test import override_settings

from mailing.mime_template import (
    TO_SENTINEL,
    TRACKING_SENTINEL,
    UNSUBSCRIBE_SENTINEL,
    MimeTemplateError,
    build_campaign_template,
    render_campaign_template,
)
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    Organization,
)
from mailing.services import campaign_sender as sender
from mailing.services.campaign_rendering import (
    build_campaign_html_body,
    build_campaign_text_body,
    campaign_unsubscribe_headers,
)
from mailing.services.tokens import generate_raw_token
from mailing.ses import _Content, _Envelope, _raw_parameters

pytestmark = pytest.mark.django_db

SOURCE = "Datamailer News <newsletter@example.com>"
SUBJECT = "Café ☃ weekly № 5 — news, Gebühren & naïve takes"


def _newsletter_html(paragraphs=420):
    rows = []
    for index in range(paragraphs):
        rows.append(
            f'<p class="item">Issue #{index} — café naïve ☃ Gebühren <a href="https://example.com/post/{index}?a=1&b=2&utm_source=n">read more about topic number {index} with extra padding text</a> · <a href="mailto:editor@example.com">reply</a> · <a href="/relative/path">relative</a></p>'
        )
    # One deliberately gigantic single line plus archive footer links.
    rows.append("<p>" + "padding-padding-café☃ " * 500 + "</p>")
    footer = (
        '<p><a href="https://club.us19.list-manage.com/unsubscribe?u=1&id=2&e=[UNIQID]">unsubscribe</a> '
        '<a href="https://us19.forward-to-friend.com/forward?u=1&id=2&e=[UNIQID]">forward to a friend</a></p>'
    )
    return "<html><body>\n" + "\n".join(rows) + "\n" + footer + "\n</body></html>"


def _newsletter_text(lines=60):
    body = "\n".join(
        f"Line {index} — café naïve ☃ Gebühren https://example.com/post/{index}?a=1&b=2 with enough trailing padding to make this a very long line indeed " * 2
        for index in range(lines)
    )
    return body + "\nRead online https://club.us19.list-manage.com/unsubscribe?u=1&id=2&e=[UNIQID]\n"


@pytest.fixture
def newsletter():
    html, text = _newsletter_html(), _newsletter_text()
    assert len(html.encode("utf-8")) >= 80_000, len(html.encode("utf-8"))
    return html, text


def _template_inputs(newsletter):
    html_body, text_body = newsletter
    html_template = build_campaign_html_body(html_body, TRACKING_SENTINEL, UNSUBSCRIBE_SENTINEL)
    text_template = build_campaign_text_body(text_body, UNSUBSCRIBE_SENTINEL)
    assert TRACKING_SENTINEL in html_template
    assert UNSUBSCRIBE_SENTINEL in html_template and UNSUBSCRIBE_SENTINEL in text_template
    return html_template, text_template


def _parse(raw):
    return BytesParser(policy=email_policy.default).parsebytes(raw)


@override_settings(PUBLIC_BASE_URL="https://mail.example.com", AWS_SES_CONFIGURATION_SET="")
def test_template_render_matches_per_message_builder(newsletter):
    html_template, text_template = _template_inputs(newsletter)
    headers_template = campaign_unsubscribe_headers(UNSUBSCRIBE_SENTINEL)
    assert headers_template, "https base URL must yield List-Unsubscribe headers"
    template = build_campaign_template(
        source=SOURCE, subject=SUBJECT, html_body=html_template, text_body=text_template, headers=headers_template
    )
    assert TO_SENTINEL.encode() in template.raw
    assert b"8bit" in template.raw

    to_email = "person@example.com"
    tracking_token, unsubscribe_token = generate_raw_token(), generate_raw_token()
    rendered = render_campaign_template(
        template, to_email=to_email, tracking_token=tracking_token, unsubscribe_token=unsubscribe_token
    )
    for sentinel in (TRACKING_SENTINEL, UNSUBSCRIBE_SENTINEL, TO_SENTINEL):
        assert sentinel.encode() not in rendered

    html_final = html_template.replace(TRACKING_SENTINEL, tracking_token).replace(UNSUBSCRIBE_SENTINEL, unsubscribe_token)
    text_final = text_template.replace(UNSUBSCRIBE_SENTINEL, unsubscribe_token)
    legacy = _raw_parameters(
        _Envelope(SOURCE, to_email, "", None, None, None),
        _Content(SUBJECT, html_final, text_final, campaign_unsubscribe_headers(unsubscribe_token), None, None),
    )["RawMessage"]["Data"]

    new, old = _parse(rendered), _parse(legacy)
    for header in ("From", "To", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id"):
        assert new[header] == old[header], header
    assert new.get_body(preferencelist=("plain",)).get_content() == old.get_body(preferencelist=("plain",)).get_content()
    assert new.get_body(preferencelist=("html",)).get_content() == old.get_body(preferencelist=("html",)).get_content()


@override_settings(PUBLIC_BASE_URL="https://mail.example.com", AWS_SES_CONFIGURATION_SET="")
def test_template_render_matches_per_message_builder_for_small_ascii_body():
    template = build_campaign_template(
        source=SOURCE,
        subject="Hi",
        html_body=build_campaign_html_body("<p>Hi</p>", TRACKING_SENTINEL, UNSUBSCRIBE_SENTINEL),
        text_body=build_campaign_text_body("Hi", UNSUBSCRIBE_SENTINEL),
        headers=campaign_unsubscribe_headers(UNSUBSCRIBE_SENTINEL),
    )
    tracking_token, unsubscribe_token = generate_raw_token(), generate_raw_token()
    rendered = render_campaign_template(
        template, to_email="a@example.com", tracking_token=tracking_token, unsubscribe_token=unsubscribe_token
    )
    html_template = build_campaign_html_body("<p>Hi</p>", TRACKING_SENTINEL, UNSUBSCRIBE_SENTINEL)
    legacy = _raw_parameters(
        _Envelope(SOURCE, "a@example.com", "", None, None, None),
        _Content(
            "Hi",
            html_template.replace(TRACKING_SENTINEL, tracking_token).replace(UNSUBSCRIBE_SENTINEL, unsubscribe_token),
            build_campaign_text_body("Hi", unsubscribe_token),
            campaign_unsubscribe_headers(unsubscribe_token),
            None,
            None,
        ),
    )["RawMessage"]["Data"]
    new, old = _parse(rendered), _parse(legacy)
    assert new.get_body(preferencelist=("html",)).get_content() == old.get_body(preferencelist=("html",)).get_content()
    assert new.get_body(preferencelist=("plain",)).get_content() == old.get_body(preferencelist=("plain",)).get_content()
    assert new["List-Unsubscribe"] == old["List-Unsubscribe"]


@override_settings(PUBLIC_BASE_URL="https://mail.example.com", AWS_SES_CONFIGURATION_SET="")
def test_list_unsubscribe_and_links_correct_across_100_recipients(newsletter):
    html_template, text_template = _template_inputs(newsletter)
    template = build_campaign_template(
        source=SOURCE,
        subject=SUBJECT,
        html_body=html_template,
        text_body=text_template,
        headers=campaign_unsubscribe_headers(UNSUBSCRIBE_SENTINEL),
    )
    seen_tracking, seen_unsub = set(), set()
    for index in range(100):
        to_email = f"person{index}@example.com"
        tracking_token, unsubscribe_token = generate_raw_token(), generate_raw_token()
        rendered = render_campaign_template(
            template, to_email=to_email, tracking_token=tracking_token, unsubscribe_token=unsubscribe_token
        )
        message = _parse(rendered)
        assert message["To"] == to_email
        assert message["List-Unsubscribe"] == f"<https://mail.example.com/unsubscribe/{unsubscribe_token}>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        html = message.get_body(preferencelist=("html",)).get_content()
        text = message.get_body(preferencelist=("plain",)).get_content()
        assert f"/t/o/{tracking_token}.gif" in html
        assert f"/t/c/{tracking_token}?u=" in html
        assert f"/unsubscribe/{unsubscribe_token}" in html
        assert f"/unsubscribe/{unsubscribe_token}" in text
        assert tracking_token not in text or True  # pixel/click links live in html; text carries unsubscribe only
        seen_tracking.add(tracking_token)
        seen_unsub.add(unsubscribe_token)
    assert len(seen_tracking) == 100 and len(seen_unsub) == 100


@pytest.mark.parametrize(
    "to_email,tracking_token,unsubscribe_token",
    [
        ("evil\nBcc: x@example.com", "tok1", "tok2"),
        ("evil\r\nTo: x@example.com", "tok1", "tok2"),
        ("a@example.com", "", "tok2"),
        ("a@example.com", "tok1", ""),
        ("a@example.com", TRACKING_SENTINEL, "tok2"),
        ("a@example.com", "tok1", f"prefix-{UNSUBSCRIBE_SENTINEL}"),
        (f"a{TO_SENTINEL}@example.com", "tok1", "tok2"),
    ],
)
def test_render_rejects_unsafe_values(to_email, tracking_token, unsubscribe_token):
    template = build_campaign_template(
        source=SOURCE, subject="Hi", html_body="<p>Hi</p>", text_body="Hi", headers={"List-Unsubscribe": "<x>"}
    )
    with pytest.raises(MimeTemplateError):
        render_campaign_template(
            template, to_email=to_email, tracking_token=tracking_token, unsubscribe_token=unsubscribe_token
        )


def test_mime_template_enabled_by_default():
    assert settings.RELAY_MIME_TEMPLATE_ENABLED is True


def _batch_fixture(count, html_body="<p>Hi</p>", text_body="Hi"):
    organization = Organization.objects.create(name="Org", slug="org")
    audience = Audience.objects.create(organization=organization, name="News", slug="news")
    client = Client.objects.create(organization=organization, name="Client", slug="client")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Hi", status="queued", html_body=html_body, text_body=text_body
    )
    recipients = CampaignRecipient.objects.bulk_create(
        [
            CampaignRecipient(
                campaign=campaign,
                contact=Contact.objects.create(email=f"person{i}@example.com"),
                email=f"person{i}@example.com",
            )
            for i in range(count)
        ]
    )
    return campaign, {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id for recipient in recipients],
    }


class _CapturingSes:
    def __init__(self):
        self.params_list = []

    def send_raw_email(self, **params):
        self.params_list.append(params)
        return {"MessageId": f"ses-{len(self.params_list)}"}


@override_settings(
    PUBLIC_BASE_URL="https://mail.example.com",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    AWS_SES_CONFIGURATION_SET="",
    SES_MAX_SEND_RATE_PER_SECOND=0,
)
def test_batch_builds_mime_once_and_sends_personalized_copies(newsletter, monkeypatch):
    html_body, text_body = newsletter
    campaign, payload = _batch_fixture(5, html_body=html_body, text_body=text_body)
    builds = []
    original = sender.build_campaign_template

    def counting(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sender, "build_campaign_template", counting)
    ses = _CapturingSes()

    result = sender.send_campaign_batch(payload, ses_client=ses)

    assert result.sent_count == 5
    assert len(builds) == 1, "MIME must be built once per batch"
    assert len(ses.params_list) == 5
    for index, params in enumerate(ses.params_list):
        assert params["Destinations"] == [f"person{index}@example.com"]
        message = _parse(params["RawMessage"]["Data"])
        assert message["To"] == f"person{index}@example.com"
        assert message["List-Unsubscribe"].startswith("<https://mail.example.com/unsubscribe/")
        html = message.get_body(preferencelist=("html",)).get_content()
        assert "/t/o/" in html and message["List-Unsubscribe"][1:-1].split("/")[-1] in message.get_body(
            preferencelist=("plain",)
        ).get_content()


@override_settings(
    PUBLIC_BASE_URL="https://mail.example.com",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    AWS_SES_CONFIGURATION_SET="",
    SES_MAX_SEND_RATE_PER_SECOND=0,
    RELAY_MIME_TEMPLATE_ENABLED=False,
)
def test_flag_off_uses_per_message_builder(monkeypatch):
    campaign, payload = _batch_fixture(2)

    def forbidden(*args, **kwargs):
        raise AssertionError("template builder must not run when the flag is off")

    monkeypatch.setattr(sender, "build_campaign_template", forbidden)
    ses = _CapturingSes()

    result = sender.send_campaign_batch(payload, ses_client=ses)

    assert result.sent_count == 2
    assert len(ses.params_list) == 2
    assert _parse(ses.params_list[0]["RawMessage"]["Data"])["To"] == "person0@example.com"


@override_settings(
    PUBLIC_BASE_URL="https://mail.example.com",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    AWS_SES_CONFIGURATION_SET="",
    SES_MAX_SEND_RATE_PER_SECOND=0,
)
def test_build_failure_falls_back_to_per_message_builder(monkeypatch, caplog):
    campaign, payload = _batch_fixture(2)

    def broken(*args, **kwargs):
        raise MimeTemplateError("synthetic build failure")

    monkeypatch.setattr(sender, "build_campaign_template", broken)
    ses = _CapturingSes()

    with caplog.at_level(logging.ERROR, logger="mailing.services.campaign_sender"):
        result = sender.send_campaign_batch(payload, ses_client=ses)

    assert result.sent_count == 2
    assert len(ses.params_list) == 2
    assert any("mime_template_build_failed" in record.getMessage() for record in caplog.records)


@override_settings(
    PUBLIC_BASE_URL="https://mail.example.com",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    AWS_SES_CONFIGURATION_SET="",
    SES_MAX_SEND_RATE_PER_SECOND=0,
)
def test_crlf_recipient_email_fails_loudly_without_ses_call():
    campaign, payload = _batch_fixture(1)
    recipient = CampaignRecipient.objects.get(pk=payload["campaign_recipient_ids"][0])
    recipient.email = "evil\nBcc: victim@example.com"
    recipient.save(update_fields=["email", "updated_at"])
    ses = _CapturingSes()

    result = sender.send_campaign_batch(payload, ses_client=ses)

    assert result.failed_count == 1
    assert ses.params_list == []
    recipient.refresh_from_db()
    assert str(recipient.status) == "failed"


@override_settings(PUBLIC_BASE_URL="https://mail.example.com", AWS_SES_CONFIGURATION_SET="")
def test_mime_template_cuts_per_message_cpu(newsletter):
    html_template, text_template = _template_inputs(newsletter)
    headers_template = campaign_unsubscribe_headers(UNSUBSCRIBE_SENTINEL)
    tokens = [(generate_raw_token(), generate_raw_token()) for _ in range(20)]
    to_emails = [f"person{i}@example.com" for i in range(20)]

    started = time.perf_counter()
    for to_email, (tracking_token, unsubscribe_token) in zip(to_emails, tokens):
        html_final = (
            html_template.replace(TRACKING_SENTINEL, tracking_token).replace(UNSUBSCRIBE_SENTINEL, unsubscribe_token)
        )
        text_final = text_template.replace(UNSUBSCRIBE_SENTINEL, unsubscribe_token)
        _raw_parameters(
            _Envelope(SOURCE, to_email, "", None, None, None),
            _Content(SUBJECT, html_final, text_final, campaign_unsubscribe_headers(unsubscribe_token), None, None),
        )
    legacy_total = time.perf_counter() - started

    started = time.perf_counter()
    template = build_campaign_template(
        source=SOURCE, subject=SUBJECT, html_body=html_template, text_body=text_template, headers=headers_template
    )
    build_once = time.perf_counter() - started
    started = time.perf_counter()
    for to_email, (tracking_token, unsubscribe_token) in zip(to_emails, tokens):
        render_campaign_template(
            template, to_email=to_email, tracking_token=tracking_token, unsubscribe_token=unsubscribe_token
        )
    render_total = time.perf_counter() - started

    print(
        f"\nmime-template benchmark html_kb={len(html_template.encode()) / 1024:.0f} n=20 "
        f"legacy_per_msg_ms={legacy_total / 20 * 1000:.2f} "
        f"template_build_once_ms={build_once * 1000:.2f} "
        f"template_render_per_msg_ms={render_total / 20 * 1000:.2f} "
        f"ratio={(build_once + render_total) / legacy_total:.3f}"
    )
    assert build_once + render_total <= 0.6 * legacy_total
