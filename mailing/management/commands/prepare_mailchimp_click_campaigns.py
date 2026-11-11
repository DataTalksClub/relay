import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mailing.services.mailchimp_click_campaigns import prepare_mailchimp_click_campaigns
from mailing.services.mailchimp_click_import import ClickImportError


class Command(BaseCommand):
    help = "Plan source-ID campaign mappings; --apply --allow-create creates sent historical campaigns only."

    def add_arguments(self, parser):
        for name in ("archive", "organization", "client", "audience", "list-id", "mapping-out", "report"):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--mapping", help="Optional explicit map from Mailchimp IDs to existing Relay IDs")
        parser.add_argument("--allow-create", action="store_true")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        destinations = [Path(options["mapping_out"]), Path(options["report"])]
        if destinations[0].resolve() == destinations[1].resolve():
            raise CommandError("Mapping and plan report require distinct destinations")
        temporaries = []
        try:
            # Reserve both files before any database writes. Details stay off stdout.
            for path in destinations:
                temporary = path.with_name(path.name + ".tmp")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                temporaries.append(temporary)
            with transaction.atomic():
                mapping, report = prepare_mailchimp_click_campaigns(
                    archive_path=options["archive"], organization=options["organization"],
                    client_slug=options["client"], audience_slug=options["audience"], list_id=options["list_id"],
                    mapping_path=options["mapping"], apply=options["apply"], allow_create=options["allow_create"],
                )
                for temporary, value, destination in zip(temporaries, (mapping, report), destinations):
                    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                    if os.name != "nt":
                        temporary.chmod(0o600)
                    temporary.replace(destination)
        except (ClickImportError, OSError) as error:
            for temporary in temporaries:
                temporary.unlink(missing_ok=True)
            raise CommandError(str(error) if isinstance(error, ClickImportError) else "Cannot write private campaign plan") from None
        self.stdout.write(json.dumps({"dry_run": report["dry_run"], "counts": report["counts"]}, sort_keys=True))
