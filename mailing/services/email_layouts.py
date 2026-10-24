"""Per-client email layouts: each site owns its own HTML shell.

A layout is a full Django template stored in the DB (EmailLayout row),
not a shared ``email_base.html`` with a substituted brand name. Markdown
transactional templates render inside their own client's layout; an
optional per-sender row (``sender_id``) overrides the client default.

Missing layout never falls back to a "Relay"-branded shell: rendering
uses a plain neutral shell with no brand text.
"""

from django.template import Context, Template

from mailing.models import EmailLayout


class MissingEmailLayout(Exception):
    pass


NEUTRAL_SHELL = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{{ subject }}</title></head>
<body style="font-family: sans-serif; line-height: 1.6; color: #222;">
<div style="max-width: 600px; margin: 0 auto; padding: 24px;">
{{ body_html|safe }}
{% if footer_note %}<p style="font-size: 12px; color: #888;">{{ footer_note }}</p>{% endif %}
</div>
</body>
</html>"""


# Ported pre-Relay layouts (t1385u/t1386u: "use the same template, no need to invent new ones").
#
# - AISHL: exact pre-Relay base from ai-shipping-labs git history, hardcoded brand.
#   Source: ~/git/ai-shipping-labs, templates/email_app/base_email.html @ 7c65ec0d2226ff304daedeb5a1e0132db89d86c5
#   (parent of first Relay commit 46e6a25c0; last touch 18779a72b 2026-05-19).
#   Ported AS-IS: black #0a0a0a header "AI Shipping Labs", link #6b8e23,
#   footer "© AI Shipping Labs" + footer_note/verify_email_url/unsubscribe_url blocks.
# - agent-git-lab / pocketshell: NO pre-Relay HTML shell ever existed (verified in git history).
#   agent-git-lab pre-Relay: cloudflare-agent-git website/relay/confirm-signup.md @ cfb7ca8fc8
#   (parent 29da523715ec; markdown body only, no shell).
#   pocketshell pre-Relay: pocketshell-site @ d445b13f3a (no email capture at all; capture arrived with Relay d0947eaa).
#   Relay canonical markdown bodies: relay templates/pocketshell/confirm-signup.md @ 18965e8 (parent e4dc005bf9),
#   relay templates/agent-git-lab/confirm-signup.md @ d77afa3 (parent 6d34abf5).
#   So these clients get a neutral plain shell carrying only their own brand (never "Relay").
# - DataTalks.Club: no shared branded layout ever existed (CMP html_body fragments are standalone,
#   courses.datatalks.club templates verified clean). Neutral plain shells with brand only.

AISL_LAYOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ subject }}</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      line-height: 1.6;
      color: #333333;
      background-color: #f5f5f5;
      margin: 0;
      padding: 0;
    }
    .email-wrapper {
      max-width: 600px;
      margin: 0 auto;
      background-color: #ffffff;
    }
    .email-header {
      background-color: #0a0a0a;
      color: #ffffff;
      padding: 24px 32px;
      text-align: center;
    }
    .email-header h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.025em;
    }
    .email-body {
      padding: 32px;
    }
    .email-body h1, .email-body h2, .email-body h3 {
      color: #0a0a0a;
    }
    .email-body a {
      color: #6b8e23;
      text-decoration: underline;
    }
    .email-body p {
      margin: 12px 0;
    }
    .email-body ul, .email-body ol {
      margin: 12px 0;
      padding-left: 24px;
    }
    .email-footer {
      background-color: #f9f9f9;
      border-top: 1px solid #e0e0e0;
      padding: 24px 32px;
      text-align: center;
      font-size: 12px;
      color: #999999;
    }
    .email-footer a {
      color: #999999;
      text-decoration: underline;
    }
    .email-footer .verify-email-cta {
      color: #92400e;
      font-size: 14px;
      margin: 0 0 12px 0;
    }
    .email-footer .verify-email-cta a {
      color: #92400e;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="email-wrapper">
    <div class="email-header">
      <h1>AI Shipping Labs</h1>
    </div>
    <div class="email-body">
      {{ body_html|safe }}
    </div>
    <div class="email-footer">
      <p>&copy; AI Shipping Labs</p>
      {% if footer_note %}
      <p>{{ footer_note }}</p>
      {% endif %}
      {% if verify_email_url %}
      <p class="verify-email-cta">
        Your email is not verified on our platform.
        To verify it, <a href="{{ verify_email_url }}">click here</a>.
      </p>
      {% endif %}
      {% if unsubscribe_url %}
      <p>
        <a href="{{ unsubscribe_url }}">Unsubscribe</a> from all emails
      </p>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""

def _neutral_brand_shell(brand_name, website_url=""):
    """Minimal neutral plain shell with hardcoded brand (no invented styling, never Relay)."""
    site_line = (
        f'      <p><a href="{website_url}" style="color:#999999;">{website_url}</a></p>\n'
        if website_url else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{{{ subject }}}}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      line-height: 1.6; color: #333333; background-color: #f5f5f5; margin: 0; padding: 0;
    }}
    .email-wrapper {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; }}
    .email-header {{ background-color: #ffffff; color: #111111; padding: 24px 32px; text-align: center; border-bottom: 1px solid #e0e0e0; }}
    .email-header h1 {{ margin: 0; font-size: 20px; font-weight: 600; }}
    .email-body {{ padding: 32px; }}
    .email-footer {{ background-color: #f9f9f9; border-top: 1px solid #e0e0e0; padding: 24px 32px; text-align: center; font-size: 12px; color: #999999; }}
    .email-footer a {{ color: #999999; text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="email-wrapper">
    <div class="email-header">
      <h1>{brand_name}</h1>
    </div>
    <div class="email-body">
      {{{{ body_html|safe }}}}
    </div>
    <div class="email-footer">
      <p>&copy; {brand_name}</p>
{site_line}      {{% if footer_note %}}<p>{{{{ footer_note }}}}</p>{{% endif %}}
    </div>
  </div>
</body>
</html>"""


def default_layouts():
    """Per-client shells: ported AISHL + neutral brand shells. No Relay branding anywhere."""
    return {
        "aisl": {
            "brand_name": "AI Shipping Labs",
            "website_url": "https://aishippinglabs.com",
            "logo_url": "",
            "layout_html": AISL_LAYOUT_HTML,
        },
        "aisl-website": {
            "brand_name": "AI Shipping Labs",
            "website_url": "https://aishippinglabs.com",
            "logo_url": "",
            "layout_html": AISL_LAYOUT_HTML,
        },
        "agent-git-lab": {
            "brand_name": "Agent Git Lab",
            "website_url": "https://alexeygrigorev.com/cloudflare-agent-git/",
            "logo_url": "",
            "layout_html": _neutral_brand_shell("Agent Git Lab", "https://alexeygrigorev.com/cloudflare-agent-git/"),
        },
        "pocketshell": {
            "brand_name": "PocketShell",
            "website_url": "https://pocketshell.io",
            "logo_url": "",
            "layout_html": _neutral_brand_shell("PocketShell", "https://pocketshell.io"),
        },
        "dtc-courses": {
            "brand_name": "DataTalks.Club",
            "website_url": "https://courses.datatalks.club",
            "logo_url": "",
            "layout_html": _neutral_brand_shell("DataTalks.Club", "https://courses.datatalks.club"),
        },
        "dtc-website": {
            "brand_name": "DataTalks.Club",
            "website_url": "https://datatalks.club",
            "logo_url": "",
            "layout_html": _neutral_brand_shell("DataTalks.Club", "https://datatalks.club"),
        },
        "hetzner-maintenance": {
            "brand_name": "DataTalks.Club Ops",
            "website_url": "https://datatalks.club",
            "logo_url": "",
            "layout_html": _neutral_brand_shell("DataTalks.Club Ops", "https://datatalks.club"),
        },
    }


def get_layout_for_client(client, sender_id=""):
    """Return (layout_or_None, is_sender_override)."""
    sender_id = (sender_id or "").strip()
    if sender_id:
        override = EmailLayout.objects.filter(client=client, sender_id=sender_id).first()
        if override is not None:
            return override, True
    default = EmailLayout.objects.filter(client=client, sender_id="").first()
    return default, False


def ensure_default_layouts(*, sender_id=""):
    """Create missing default layouts for known clients; update brand shell.

    Matches clients by slug (org-agnostic) so DEV seed slugs
    (``asl-platform``) and prod slugs (``aisl-website``) both resolve.
    Never deletes rows, never touches sender overrides unless asked.
    Returns (created, updated).
    """
    from mailing.models import Client  # noqa: PLC0415

    created = updated = 0
    defaults = default_layouts()
    for client in Client.objects.all():
        spec = defaults.get(client.slug)
        if spec is None:
            # Unknown slug: organisation-based fallback for the DTC family.
            org = (client.organization.slug if client.organization_id else "").lower()
            if org == "datatalksclub":
                spec = {
                    "brand_name": "DataTalks.Club",
                    "website_url": "https://datatalks.club",
                    "logo_url": "",
                    "layout_html": defaults["dtc-website"]["layout_html"],
                }
            else:
                continue
        layout, was_created = EmailLayout.objects.get_or_create(
            client=client,
            sender_id=sender_id,
            defaults={
                "brand_name": spec["brand_name"],
                "website_url": spec.get("website_url", ""),
                "logo_url": spec.get("logo_url", ""),
                "layout_html": spec["layout_html"],
            },
        )
        if was_created:
            created += 1
        else:
            changed = False
            for field in ("brand_name", "website_url", "logo_url", "layout_html"):
                want = spec.get(field, "") if field != "layout_html" else spec["layout_html"]
                if getattr(layout, field) != want:
                    setattr(layout, field, want)
                    changed = True
            if changed:
                # Brand shells evolve with the site look; keep stored rows in step.
                layout.save(update_fields=["brand_name", "website_url", "logo_url", "layout_html", "updated_at"])
                updated += 1
    return created, updated


def render_with_layout(*, client, subject, body_html, sender_id="", footer_note=""):
    """Render body HTML inside the client's own layout shell."""
    layout, _ = get_layout_for_client(client, sender_id=sender_id)
    if layout is None:
        template = Template(NEUTRAL_SHELL)
        context = {"subject": subject, "body_html": body_html, "footer_note": footer_note or ""}
        return template.render(Context(context, autoescape=False)), None
    template = Template(layout.layout_html)
    context = {
        "subject": subject,
        "body_html": body_html,
        "footer_note": footer_note or "",
        "brand_name": layout.brand_name,
        "brand_website_url": layout.website_url,
        "brand_logo_url": layout.logo_url,
        "layout": layout,
    }
    return template.render(Context(context, autoescape=False)), layout


def layout_payload(layout):
    return {
        "id": layout.id,
        "client": layout.client.slug if layout.client_id else "",
        "sender_id": layout.sender_id,
        "brand_name": layout.brand_name,
        "logo_url": layout.logo_url,
        "website_url": layout.website_url,
        "layout_html": layout.layout_html,
        "updated_at": layout.updated_at.isoformat() if layout.updated_at else None,
    }


def list_layouts_for_client(client):
    return {
        "client": client.slug,
        "layouts": [layout_payload(item) for item in EmailLayout.objects.filter(client=client).order_by("sender_id")],
    }


def validate_layout_payload(data):
    from mailing.services.api_errors import ApiValidationError  # noqa: PLC0415

    if not isinstance(data, dict):
        raise ApiValidationError({"body": "must_be_object"})
    errors = {}
    sender_id = data.get("sender_id", "")
    if sender_id in (None, ""):
        sender_id = ""
    elif not isinstance(sender_id, str):
        errors["sender_id"] = "must_be_string"
    else:
        sender_id = sender_id.strip()
        if sender_id:
            from django.core.exceptions import ValidationError as DjangoValidationError  # noqa: PLC0415
            from django.core.validators import validate_slug  # noqa: PLC0415

            try:
                validate_slug(sender_id)
            except DjangoValidationError:
                errors["sender_id"] = "invalid"
    brand_name = data.get("brand_name", "")
    if not isinstance(brand_name, str) or not brand_name.strip():
        errors["brand_name"] = "required"
    elif len(brand_name.strip()) > 120:
        errors["brand_name"] = "too_long"
    else:
        brand_name = brand_name.strip()
    layout_html = data.get("layout_html", "")
    if not isinstance(layout_html, str) or not layout_html.strip():
        errors["layout_html"] = "required"
    else:
        layout_html = layout_html.strip() if isinstance(layout_html, str) else layout_html
        for marker in ("{{ body_html", "{%", "{{ subject", "{{ brand_name"):
            pass
        if "body_html" not in layout_html:
            errors["layout_html"] = "must_include_body_html"
        if "Relay" in layout_html and "brand_name" not in layout_html:
            pass
    logo_url = data.get("logo_url", "")
    if logo_url in (None, ""):
        logo_url = ""
    elif not isinstance(logo_url, str):
        errors["logo_url"] = "must_be_string"
    else:
        logo_url = logo_url.strip()
    website_url = data.get("website_url", "")
    if website_url in (None, ""):
        website_url = ""
    elif not isinstance(website_url, str):
        errors["website_url"] = "must_be_string"
    else:
        website_url = website_url.strip()
    if errors:
        raise ApiValidationError(errors)
    if "Relay" in (brand_name or ""):
        raise ApiValidationError({"brand_name": "relay_reserved"})
    if "Relay" in layout_html:
        # Literal Relay text in a client layout is never allowed: layouts
        # must carry the client's own brand, not the Relay name.
        raise ApiValidationError({"layout_html": "must_not_contain_relay"})
    return {
        "sender_id": sender_id,
        "brand_name": brand_name,
        "logo_url": logo_url,
        "website_url": website_url,
        "layout_html": layout_html,
    }


def upsert_layout_for_client(client, data):
    cleaned = validate_layout_payload(data)
    layout, _ = EmailLayout.objects.update_or_create(
        client=client,
        sender_id=cleaned["sender_id"],
        defaults={
            "brand_name": cleaned["brand_name"],
            "logo_url": cleaned["logo_url"],
            "website_url": cleaned["website_url"],
            "layout_html": cleaned["layout_html"],
        },
    )
    return layout_payload(layout)


def get_layout_for_client_api(client, sender_id=""):
    sender_id = (sender_id or "").strip()
    layout = EmailLayout.objects.filter(client=client, sender_id=sender_id).first()
    if layout is None and sender_id:
        layout = EmailLayout.objects.filter(client=client, sender_id="").first()
    if layout is None:
        from mailing.services.api_errors import ApiValidationError  # noqa: PLC0415

        raise ApiValidationError({"layout": "not_found"}, status_code=404)
    return layout_payload(layout)
