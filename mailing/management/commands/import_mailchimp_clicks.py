"""A console-safe interface to the restricted historical-click importer."""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.services.mailchimp_click_import import ClickImportError, import_mailchimp_clicks


class Command(BaseCommand):
    help = "Import verified Mailchimp historical clicks. Dry-run by default; never sends or changes consent."

    def add_arguments(self, parser):
        parser.add_argument("--archive", required=True)
        parser.add_argument("--campaign-map", required=True, help="JSON map of Mailchimp IDs to existing Relay IDs")
        parser.add_argument("--organization", required=True)
        parser.add_argument("--client", required=True)
        parser.add_argument("--audience", required=True)
        parser.add_argument("--list-id", required=True, help="Exact Mailchimp audience ID to import")
        parser.add_argument("--report", required=True, help="Private JSON report path; contains contact/link details")
        parser.add_argument("--unmatched-policy", choices=("unlinked", "archive"), default="unlinked",
                            help="Store unmatched clicks without a Relay contact (default), or leave them archived")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        # Check report writability before touching database state.
        path = Path(options["report"])
        temporary = path.with_name(path.name + ".tmp")
        created_temporary = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created_temporary = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                report = import_mailchimp_clicks(
                    archive_path=options["archive"], campaign_map=options["campaign_map"],
                    organization=options["organization"], client_slug=options["client"],
                    audience_slug=options["audience"], list_id=options["list_id"], apply=options["apply"],
                    unmatched_policy=options["unmatched_policy"],
                )
                json.dump(report, stream, sort_keys=True)
                stream.write("\n")
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(path)
        except (ClickImportError, OSError) as error:
            if created_temporary and temporary.exists():
                temporary.unlink()
            raise CommandError(str(error) if isinstance(error, ClickImportError) else "Cannot write private report") from None
        self.stdout.write(json.dumps({"dry_run": report["dry_run"], "counts": report["counts"]}, sort_keys=True))
