from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from mailing.services.admin_auth import create_admin_api_key
from mailing.services.operator_management import audit


class Command(BaseCommand):
    help = "Create an admin API key for an active staff user. Prints the secret once; store it securely."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Staff user's username (USERNAME_FIELD).")
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(**{user_model.USERNAME_FIELD: options["user"]})
        except user_model.DoesNotExist:
            raise CommandError("User does not exist.") from None
        try:
            with transaction.atomic():
                key, raw_key = create_admin_api_key(user=user, name=options["name"])
                audit(
                    user,
                    "admin.api_key.create",
                    key,
                    {"key_prefix": key.display_prefix, "source": "management_command"},
                )
        except (ValidationError, IntegrityError):
            raise CommandError(
                "Use an active staff user and a nonempty, unique key name (maximum 120 characters)."
            ) from None
        self.stdout.write(raw_key)
