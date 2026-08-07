import os

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client, ClientApiKey
from mailing.services.auth import hash_api_key, public_id_from_raw_key


class Command(BaseCommand):
    help = "Provision a client API key from an environment variable without printing it."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True)
        parser.add_argument("--client", required=True)
        parser.add_argument("--name", default="deployment")
        parser.add_argument("--raw-key-env", default="RELAY_BOOTSTRAP_API_KEY")

    def handle(self, *args, **options):
        raw_key = os.environ.get(options["raw_key_env"], "").strip()
        public_id = public_id_from_raw_key(raw_key)
        if not public_id:
            raise CommandError(
                f"{options['raw_key_env']} must contain a valid Relay API key."
            )
        try:
            client = Client.objects.get(
                organization__slug=options["organization"],
                slug=options["client"],
            )
        except Client.DoesNotExist as exc:
            raise CommandError("Client scope does not exist; provision it first.") from exc

        api_key = ClientApiKey.objects.filter(
            client=client,
            name=options["name"],
            revoked_at__isnull=True,
        ).first()
        if api_key is None:
            api_key = ClientApiKey(client=client, name=options["name"])
        api_key.public_id = public_id
        api_key.key_hash = hash_api_key(raw_key)
        api_key.notes = "Managed by the Relay sandbox deployment."
        api_key.save()
        self.stdout.write(
            self.style.SUCCESS(
                f"Provisioned API key name={api_key.name} client={client.slug} prefix={api_key.display_prefix}"
            )
        )
