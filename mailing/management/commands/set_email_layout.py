"""Ensure a per-client email layout exists (create or update)."""

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client, EmailLayout
from mailing.services.email_layouts import default_layouts


class Command(BaseCommand):
    help = "Create or update a client's email layout (own header/footer/HTML shell)."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Client slug.")
        parser.add_argument("--brand-name", default="", help="Brand display name.")
        parser.add_argument("--website-url", default="", help="Brand website URL.")
        parser.add_argument("--logo-url", default="", help="Brand logo URL.")
        parser.add_argument("--layout-file", default="", help="Path to a Django template file for layout_html.")
        parser.add_argument("--sender-id", default="", help="Optional per-sender override.")
        parser.add_argument("--use-default", action="store_true", help="Use the built-in default shell for this slug.")

    def handle(self, *args, **options):
        client = Client.objects.filter(slug=options["client"]).first()
        if client is None:
            raise CommandError(f"Client not found: {options['client']}")
        sender_id = (options["sender_id"] or "").strip()
        layout_html = ""
        if options["layout_file"]:
            from pathlib import Path  # noqa: PLC0415

            path = Path(options["layout_file"])
            if not path.is_file():
                raise CommandError(f"Not a file: {path}")
            layout_html = path.read_text(encoding="utf-8")
        spec = default_layouts().get(client.slug, {}) if options["use_default"] else {}
        brand_name = options["brand_name"].strip() or spec.get("brand_name", "")
        website_url = options["website_url"].strip() or spec.get("website_url", "")
        logo_url = options["logo_url"].strip() or spec.get("logo_url", "")
        layout_html = layout_html.strip() or spec.get("layout_html", "")
        if not brand_name:
            raise CommandError("--brand-name is required (or --use-default for a known slug).")
        if not layout_html or "body_html" not in layout_html:
            raise CommandError("layout_html is required and must include body_html (or --use-default).")
        if "Relay" in layout_html or "Relay" in brand_name:
            raise CommandError("Relay text is not allowed in a client layout.")
        layout, created = EmailLayout.objects.update_or_create(
            client=client,
            sender_id=sender_id,
            defaults={
                "brand_name": brand_name,
                "website_url": website_url,
                "logo_url": logo_url,
                "layout_html": layout_html,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Updated'} layout {layout} (id={layout.id})")
        )
