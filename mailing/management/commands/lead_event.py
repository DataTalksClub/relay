"""Create and inspect lead capture pages from the command line.

    manage.py lead_event list
    manage.py lead_event material 28-prompts --title "From Idea to Production: 28 prompts" \
        --file kit.pdf --filename from-idea-to-production-28-prompts.pdf      # --file - reads stdin
    manage.py lead_event set my-talk --title "My talk" --offer "Slides + kit" --material 28-prompts
    manage.py lead_event set my-talk --inactive
    manage.py lead_event qr my-talk --out /tmp/qr                             # writes .svg and .png

The same fields are editable in Django admin (Lead events / Lead materials).
"""

import sys
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from mailing.lead_capture_models import LeadEvent, LeadMaterial
from mailing.services import lead_capture


class Command(BaseCommand):
    help = "Manage lead capture event pages (materials.dtcdev.click)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list")

        setp = sub.add_parser("set", help="Create or update an event page.")
        setp.add_argument("slug")
        setp.add_argument("--title")
        setp.add_argument("--intro")
        setp.add_argument("--offer")
        setp.add_argument("--consent-text")
        setp.add_argument("--material", help="Slug of a stored material ('' to clear).")
        setp.add_argument("--material-url")
        state = setp.add_mutually_exclusive_group()
        state.add_argument("--active", dest="active", action="store_true", default=None)
        state.add_argument("--inactive", dest="active", action="store_false")

        mat = sub.add_parser("material", help="Create or replace a stored downloadable.")
        mat.add_argument("slug")
        mat.add_argument("--file", required=True, help="Path, or - for stdin.")
        mat.add_argument("--title")
        mat.add_argument("--filename")
        mat.add_argument("--content-type", default="application/pdf")

        qrp = sub.add_parser("qr", help="Write the page QR code as SVG and PNG.")
        qrp.add_argument("slug")
        qrp.add_argument("--out", default=".")

    def handle(self, *args, **options):
        getattr(self, f"_{options['action']}")(options)

    def _list(self, options):
        for event in LeadEvent.objects.select_related("material").order_by("slug"):
            stats = lead_capture.event_stats(event)
            self.stdout.write(
                f"{event.slug}\t{'active' if event.is_active else 'inactive'}\t"
                f"signups={stats['signups']} confirmed={stats['confirmed']}\t"
                f"material={event.material.slug if event.material else event.material_url or '-'}\t"
                f"{lead_capture.event_url(event)}"
            )

    def _set(self, options):
        event = LeadEvent.objects.filter(slug=options["slug"]).first()
        if event is None:
            if not options["title"]:
                raise CommandError("--title is required for a new event.")
            event = LeadEvent(slug=options["slug"])
        for field, option in [
            ("title", "title"), ("intro", "intro"), ("offer", "offer"),
            ("consent_text", "consent_text"), ("material_url", "material_url"),
        ]:
            if options[option] is not None:
                setattr(event, field, options[option])
        if options["material"] is not None:
            if options["material"] == "":
                event.material = None
            else:
                material = LeadMaterial.objects.filter(slug=options["material"]).first()
                if material is None:
                    raise CommandError(f"No material {options['material']!r}.")
                event.material = material
        if options["active"] is not None:
            event.is_active = options["active"]
        try:
            event.full_clean()
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        event.save()
        self.stdout.write(self.style.SUCCESS(f"{event.slug}: {lead_capture.event_url(event)}"))

    def _material(self, options):
        data = sys.stdin.buffer.read() if options["file"] == "-" else Path(options["file"]).read_bytes()
        if not data:
            raise CommandError("The file is empty.")
        if len(data) > settings.RELAY_LEADS_MAX_MATERIAL_BYTES:
            raise CommandError("The file is larger than RELAY_LEADS_MAX_MATERIAL_BYTES.")
        material = LeadMaterial.objects.filter(slug=options["slug"]).first() or LeadMaterial(slug=options["slug"])
        if options["title"]:
            material.title = options["title"]
        if options["filename"]:
            material.filename = options["filename"]
        elif not material.filename and options["file"] != "-":
            material.filename = Path(options["file"]).name
        if not material.title or not material.filename:
            raise CommandError("--title and --filename are required for a new material.")
        material.content_type = options["content_type"]
        material.data = data
        material.save()
        self.stdout.write(self.style.SUCCESS(f"{material.slug}: {material.size} bytes sha256={material.sha256}"))

    def _qr(self, options):
        event = LeadEvent.objects.filter(slug=options["slug"]).first()
        if event is None:
            raise CommandError(f"No event {options['slug']!r}.")
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        url = lead_capture.event_url(event)
        for kind in ("svg", "png"):
            path = out / f"{event.slug}-qr.{kind}"
            path.write_bytes(lead_capture.qr_code(url, kind=kind))
            self.stdout.write(str(path))
        self.stdout.write(self.style.SUCCESS(url))
