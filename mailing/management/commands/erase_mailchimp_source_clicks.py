import json

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Audience, Client
from mailing.services.mailchimp_click_import import resolve_scope
from mailing.services.mailchimp_source_erasure import MailchimpSourceErasureError, erase_mailchimp_source_clicks


class Command(BaseCommand):
    help = (
        "Erase only the Relay copy of exact-source Mailchimp historical clicks; dry-run by default. "
        "Restricted archive retention is separate. Never changes contacts, consent or delivery state."
    )

    def add_arguments(self, parser):
        for name in ("organization", "client", "audience", "datacenter", "list-id", "email-id"):
            parser.add_argument("--" + name, required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            client, audience = resolve_scope(options["organization"], options["client"], options["audience"])
            report = erase_mailchimp_source_clicks(
                client=client, audience=audience, datacenter=options["datacenter"],
                list_id=options["list_id"], email_id=options["email_id"], apply=options["apply"],
            )
        except MailchimpSourceErasureError as error:
            raise CommandError(str(error)) from None
        except (Client.DoesNotExist, Audience.DoesNotExist, ValueError):
            raise CommandError("Invalid Mailchimp source identity or Relay scope") from None
        self.stdout.write(json.dumps(report, sort_keys=True))
