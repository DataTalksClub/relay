import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client
from mailing.services.api_errors import ApiValidationError
from mailing.services.history_import import (
    import_campaigns_for_client,
    import_events_for_client,
    import_messages_for_client,
)

SECTIONS = ("campaigns", "messages", "events")


class Command(BaseCommand):
    help = (
        "Import a migrating client's historical send history from a JSON file. The file is an object with "
        "optional 'client' and 'audience' keys and any of the 'campaigns', 'messages' and 'events' lists, "
        "using the same row shapes as POST /api/import/campaigns, /api/import/messages and /api/import/events. "
        "Sections are imported in that order so events can reference the campaigns and messages they belong to. "
        "Every section is idempotent on client_reference, so re-running the same file creates no new rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from-json", required=True, help="Path to the JSON history export to import.")
        parser.add_argument(
            "--client",
            default=None,
            help="Target client slug. Overrides a 'client' key in the file; required when the file has none.",
        )
        parser.add_argument(
            "--audience",
            default=None,
            help="Target audience slug for campaigns and events. Overrides an 'audience' key in the file.",
        )

    def handle(self, *args, **options):
        path = Path(options["from_json"])
        if not path.is_file():
            raise CommandError(f"JSON path is not a file: {path}")

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise CommandError(f"{path} must contain a JSON object")

        client_slug = options["client"] or document.get("client")
        if not client_slug:
            raise CommandError("No client slug: pass --client or put a 'client' key in the file")
        client = Client.objects.filter(slug=client_slug).first()
        if client is None:
            raise CommandError(f"Unknown client slug: {client_slug}")

        audience_slug = options["audience"] or document.get("audience")

        summary = {"client": client.slug}
        for section in SECTIONS:
            rows = document.get(section)
            if rows is None:
                continue
            payload = {section: rows}
            if audience_slug:
                payload["audience"] = audience_slug
            try:
                summary[section] = self.import_section(section, payload, client)
            except ApiValidationError as exc:
                raise CommandError(f"{section}: {json.dumps(exc.errors, sort_keys=True)}") from exc

        self.stdout.write(json.dumps(summary, indent=2, sort_keys=True))

    def import_section(self, section, payload, client):
        if section == "campaigns":
            return import_campaigns_for_client(payload, client)
        if section == "messages":
            return import_messages_for_client(payload, client)
        return import_events_for_client(payload, client)
