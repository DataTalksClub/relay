"""Create the org/audience/client/sender/template that lead capture pages need.

Idempotent. The confirmation template is created from
``templates/talk-leads/lead-confirm.md`` only when it does not exist yet, so
edits made in Relay survive; pass ``--update-template`` to overwrite them.
"""

from pathlib import Path

import frontmatter
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mailing.models import Audience, Client, EmailTemplate, Organization
from mailing.services.senders import normalize_sender_email
from mailing.services.transactional_catalog import normalize_required_context

SENDER_ID = "alexey"
SENDER = "Alexey Grigorev <alexey@datatalks.club>"
TEMPLATE_DIR = "talk-leads"


class Command(BaseCommand):
    help = "Provision the leads client, audience, sender and confirmation template for lead capture pages."

    def add_arguments(self, parser):
        parser.add_argument("--sender", default=SENDER, help=f"From address (default: {SENDER}).")
        parser.add_argument("--update-template", action="store_true", help="Overwrite the template from the repo file.")

    def handle(self, *args, **options):
        sender_email = normalize_sender_email(options["sender"])
        try:
            organization = Organization.objects.get(slug=settings.RELAY_LEADS_ORG)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization {settings.RELAY_LEADS_ORG!r} does not exist.") from exc
        audience, _ = Audience.objects.get_or_create(
            organization=organization, slug=settings.RELAY_LEADS_AUDIENCE, defaults={"name": "Talk and workshop leads"}
        )
        client, created = Client.objects.get_or_create(
            organization=organization,
            slug=settings.RELAY_LEADS_CLIENT,
            defaults={
                "name": "Talk and workshop leads",
                "is_active": True,
                "default_sender_id": SENDER_ID,
                "sender_emails": [{"id": SENDER_ID, "email": sender_email}],
            },
        )
        if not created and not client.sender_emails:
            client.default_sender_id = SENDER_ID
            client.sender_emails = [{"id": SENDER_ID, "email": sender_email}]
            client.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])

        key = settings.RELAY_LEADS_CONFIRM_TEMPLATE
        path = Path(settings.BASE_DIR) / "templates" / TEMPLATE_DIR / f"{key}.md"
        if not path.is_file():
            raise CommandError(f"Missing template file {path}.")
        post = frontmatter.load(str(path))
        fields = {
            "name": str(post.metadata.get("name") or key),
            "subject": str(post.metadata["subject"]).strip(),
            "markdown_body": post.content,
            "category": str(post.metadata.get("category") or ""),
            "required_context": [
                {"name": item.name, "description": item.description}
                for item in normalize_required_context(post.metadata.get("required_context") or [])
            ],
            "example_context": post.metadata.get("example_context") or {},
            "default_sender_id": client.default_sender_id or SENDER_ID,
            "is_transactional": True,
            "is_active": True,
        }
        template = EmailTemplate.objects.filter(client=client, key=key).first()
        if template is None:
            EmailTemplate.objects.create(client=client, key=key, **fields)
            template_state = "created"
        elif options["update_template"]:
            for name, value in fields.items():
                setattr(template, name, value)
            template.save()
            template_state = "updated"
        else:
            template_state = "kept"
        self.stdout.write(
            self.style.SUCCESS(
                f"Lead capture ready: organization={organization.slug} audience={audience.slug} "
                f"client={client.slug} sender={client.sender_emails} template={key} ({template_state})"
            )
        )
