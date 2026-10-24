"""Audit rendered client emails for stray Relay branding."""

import json

from django.core.management.base import BaseCommand

from mailing.models import Client, EmailTemplate
from mailing.services.email_layouts import get_layout_for_client
from mailing.services.transactional_versions import draft_render_source, render_source_message_fields


def _example_context(template):
    ctx = template.example_context if isinstance(template.example_context, dict) else {}
    for req in template.required_context or []:
        name = req.get("name") if isinstance(req, dict) else req
        if isinstance(name, str) and name and name not in ctx:
            ctx[name] = f"example-{name}"
    return ctx


class Command(BaseCommand):
    help = "Render each client template with example context and flag Relay text."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    def handle(self, *args, **options):
        rows = []
        for client in Client.objects.order_by("organization__slug", "slug"):
            layout, _ = get_layout_for_client(client)
            for template in EmailTemplate.objects.filter(client=client, is_active=True).order_by("key"):
                ctx = _example_context(template)
                try:
                    source = draft_render_source(template)
                    fields = render_source_message_fields(source, ctx, sender_id=template.default_sender_id or "")
                    html = fields["html_body"]
                except Exception as exc:  # noqa: BLE001 - audit must report, not crash
                    rows.append(
                        {
                            "client": client.slug,
                            "template": template.key,
                            "kind": "markdown" if template.markdown_body else "html",
                            "has_layout": layout is not None,
                            "layout_brand": layout.brand_name if layout else "",
                            "relay_found": True,
                            "error": str(exc)[:200],
                        }
                    )
                    continue
                relay_found = "Relay" in html
                rows.append(
                    {
                        "client": client.slug,
                        "template": template.key,
                        "kind": "markdown" if template.markdown_body else "html",
                        "has_layout": layout is not None,
                        "layout_brand": layout.brand_name if layout else "",
                        "relay_found": relay_found,
                        "error": "",
                    }
                )
        flagged = [row for row in rows if row["relay_found"]]
        if options["json"]:
            self.stdout.write(json.dumps({"rows": rows, "flagged": len(flagged), "total": len(rows)}, indent=2))
        else:
            for row in rows:
                mark = "RELAY!!" if row["relay_found"] else "clean"
                layout = row["layout_brand"] or ("none" if not row["has_layout"] else "?")
                self.stdout.write(f"{row['client']}/{row['template']} [{row['kind']}] layout={layout} -> {mark}")
            self.stdout.write(f"Checked {len(rows)} templates, {len(flagged)} flagged.")
