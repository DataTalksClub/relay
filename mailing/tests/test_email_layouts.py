"""Per-client email layouts: each site owns its shell; no Relay fallback."""

import pytest
from django.urls import reverse

from mailing.models import Client, EmailLayout, EmailTemplate, Organization
from mailing.services.auth import create_client_api_key
from mailing.services.email_layouts import (
    default_layouts,
    ensure_default_layouts,
    get_layout_for_client,
    render_with_layout,
)
from mailing.services.transactional_markdown import wrap_email_html
from mailing.services.transactional_versions import draft_render_source, render_source_message_fields

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def org():
    return Organization.objects.create(name="Test Org", slug="test-org")


def make_client(org, slug, name, raw_key=None):
    import secrets  # noqa: PLC0415

    raw_key = raw_key or f"relay_{secrets.token_hex(6)}_{secrets.token_hex(8)}"
    client = Client.objects.create(
        organization=org,
        name=name,
        slug=slug,
        default_sender_id="hello",
        sender_emails=[{"id": "hello", "email": f"hello@{slug}.example.com"}],
    )
    create_client_api_key(client=client, name="t", raw_api_key=raw_key)
    return client, raw_key


def test_each_client_gets_own_layout_no_cross_leak(org):
    aisl, _ = make_client(org, "aisl-test-a", "AISL")
    agl, _ = make_client(org, "aisl-test-b", "AGL")
    specs = default_layouts()
    for client, spec_key in ((aisl, "aisl"), (agl, "agent-git-lab")):
        spec = specs[spec_key]
        EmailLayout.objects.create(
            client=client,
            sender_id="",
            brand_name=spec["brand_name"],
            website_url=spec["website_url"],
            layout_html=spec["layout_html"],
        )
    html_a, layout_a = render_with_layout(client=aisl, subject="Hi", body_html="<p>body</p>")
    html_b, layout_b = render_with_layout(client=agl, subject="Hi", body_html="<p>body</p>")
    assert "AI Shipping Labs" in html_a
    assert "Agent Git Lab" not in html_a
    assert "Agent Git Lab" in html_b
    assert "AI Shipping Labs" not in html_b
    assert "Relay" not in html_a and "Relay" not in html_b
    assert layout_a.brand_name != layout_b.brand_name


def test_missing_layout_renders_neutral_shell_never_relay(org):
    client, _ = make_client(org, "no-layout-client", "No Layout")
    html, layout = render_with_layout(client=client, subject="Hi", body_html="<p>body</p>")
    assert layout is None
    assert "Relay" not in html
    assert "<p>body</p>" in html


def test_wrap_email_html_requires_no_global_brand(org):
    client, _ = make_client(org, "wrap-client", "Wrap")
    spec = default_layouts()["pocketshell"]
    EmailLayout.objects.create(
        client=client,
        sender_id="",
        brand_name=spec["brand_name"],
        website_url=spec["website_url"],
        layout_html=spec["layout_html"],
    )
    html = wrap_email_html("Subj", "<p>x</p>", client=client)
    assert "PocketShell" in html
    assert "Relay" not in html
    # Legacy call without a client must not inject Relay either.
    neutral = wrap_email_html("Subj", "<p>x</p>")
    assert "Relay" not in neutral


def test_markdown_template_renders_inside_own_client_layout(org):
    aisl, _ = make_client(org, "md-aisl", "AISL")
    spec = default_layouts()["aisl"]
    EmailLayout.objects.create(
        client=aisl, sender_id="", brand_name=spec["brand_name"],
        website_url=spec["website_url"], layout_html=spec["layout_html"],
    )
    template = EmailTemplate.objects.create(
        client=aisl, key="password_reset", name="PR", subject="Reset {{ name }}",
        markdown_body="# Hi {{ name }}\n\nReset: {{ reset_url }}",
    )
    source = draft_render_source(template)
    fields = render_source_message_fields(source, {"name": "Ada", "reset_url": "https://x.example/r"})
    assert "AI Shipping Labs" in fields["html_body"]
    assert "Relay" not in fields["html_body"]
    assert "AI Shipping Labs" in fields["html_body"].replace("&copy;", "©")


def test_per_sender_override_beats_default(org):
    client, _ = make_client(org, "sender-ovr", "OVR")
    specs = default_layouts()
    EmailLayout.objects.create(
        client=client, sender_id="", brand_name=specs["dtc-courses"]["brand_name"],
        website_url=specs["dtc-courses"]["website_url"],
        layout_html=specs["dtc-courses"]["layout_html"],
    )
    EmailLayout.objects.create(
        client=client, sender_id="hello", brand_name="Sender Brand",
        website_url="https://example.com",
        layout_html=specs["dtc-courses"]["layout_html"].replace("DataTalks.Club", "Sender Brand"),
    )
    layout, is_override = get_layout_for_client(client, sender_id="hello")
    assert is_override and layout.brand_name == "Sender Brand"
    default, is_default_override = get_layout_for_client(client)
    assert not is_default_override and default.brand_name == "DataTalks.Club"


def test_layout_api_rejects_relay_text(org):
    from mailing.services.api_errors import ApiValidationError  # noqa: PLC0415
    from mailing.services.email_layouts import validate_layout_payload  # noqa: PLC0415

    spec = default_layouts()["aisl"]
    with pytest.raises(ApiValidationError):
        validate_layout_payload({"brand_name": "Relay", "layout_html": spec["layout_html"]})
    with pytest.raises(ApiValidationError):
        validate_layout_payload({"brand_name": "Good", "layout_html": "<p>Relay header {{ body_html }}</p>"})


def test_ensure_default_layouts_creates_distinct_shells(org):
    aisl, _ = make_client(org, "aisl", "AISL Prod")
    agl, _ = make_client(org, "agent-git-lab", "AGL")
    ps, _ = make_client(org, "pocketshell", "PS")
    created, _ = ensure_default_layouts()
    assert created >= 3
    brands = {}
    shells = {}
    for client in (aisl, agl, ps):
        layout, _ = get_layout_for_client(client)
        assert layout is not None
        assert "Relay" not in layout.layout_html
        assert "Relay" not in layout.brand_name
        brands[client.slug] = layout.brand_name
        shells[client.slug] = layout.layout_html
    assert shells["aisl"] != shells["agent-git-lab"] != shells["pocketshell"]
    assert brands["aisl"] == "AI Shipping Labs"
    assert brands["agent-git-lab"] == "Agent Git Lab"
    assert brands["pocketshell"] == "PocketShell"


def test_aisl_layout_is_ported_pre_relay_base_not_invented():
    """t1385u/t1386u: AISHL layout must be the OLD pre-Relay base, not a new design."""
    specs = default_layouts()
    aisl_html = specs["aisl"]["layout_html"]
    aisl_website_html = specs["aisl-website"]["layout_html"]
    assert aisl_html == aisl_website_html
    # Old base markers (ai-shipping-labs templates/email_app/base_email.html @ 7c65ec0d2).
    assert "<h1>AI Shipping Labs</h1>" in aisl_html
    assert "&copy; AI Shipping Labs" in aisl_html
    assert "#0a0a0a" in aisl_html  # black header from old base
    assert "#6b8e23" in aisl_html  # olive link color from old base
    assert "verify_email_url" in aisl_html
    assert "unsubscribe_url" in aisl_html
    assert "body_html" in aisl_html
    # Invented redesign markers from the discarded plumbing attempt must be gone.
    for invented in ("#4f46e5", "#38bdf8", "#0d9488", "#1e40af", "border-bottom: 3px solid"):
        assert invented not in aisl_html
    assert "Relay" not in aisl_html
    # Non-AISHL clients: no pre-Relay shell existed -> neutral plain shells, distinct, no Relay.
    for slug in ("agent-git-lab", "pocketshell", "dtc-courses", "dtc-website", "hetzner-maintenance"):
        html = specs[slug]["layout_html"]
        assert "Relay" not in html
        assert "body_html" in html
        assert specs[slug]["brand_name"] in html
    assert specs["agent-git-lab"]["layout_html"] != specs["pocketshell"]["layout_html"]


def test_layout_api_endpoints(org):
    from django.test import Client as DjangoClient  # noqa: PLC0415

    client, raw_key = make_client(org, "api-layout-client", "API")
    spec = default_layouts()["aisl"]
    http = DjangoClient()
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}
    put = http.put(
        reverse("mailing:api_email_layouts"),
        data={"brand_name": spec["brand_name"], "website_url": spec["website_url"], "layout_html": spec["layout_html"]},
        content_type="application/json",
        **auth,
    )
    assert put.status_code == 200, put.content[:300]
    get = http.get(reverse("mailing:api_email_layouts"), **auth)
    assert get.status_code == 200
    assert get.json()["layouts"][0]["brand_name"] == "AI Shipping Labs"
    bad = http.put(
        reverse("mailing:api_email_layouts"),
        data={"brand_name": "Relay", "layout_html": spec["layout_html"]},
        content_type="application/json",
        **auth,
    )
    assert bad.status_code == 400
