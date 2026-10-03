"""Ensure each RELAY_PUBLIC_LISTS entry has an org, audience, client, sender, and template."""

from pathlib import Path

import frontmatter
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mailing.models import Audience, Client, EmailTemplate, Organization
from mailing.services.public_lists import parse_public_lists
from mailing.services.senders import normalize_sender_email
from mailing.services.transactional_catalog import normalize_required_context

SENDERS = {
    "pocketshell": ("hello", "PocketShell <hello@pocketshell.io>"),
    "agent-git-lab": ("hello", "Agent Git Lab <hello@datatalks.club>"),
}

NAMES = {
    "pocketshell": "PocketShell",
    "agent-git-lab": "Agent Git Lab",
}


class Command(BaseCommand):
    help = "Provision the public double opt-in lists named in RELAY_PUBLIC_LISTS."

    def handle(self, *args, **options):
        try:
            lists = parse_public_lists(settings.RELAY_PUBLIC_LISTS)
        except Exception as exc:
            raise CommandError(f"RELAY_PUBLIC_LISTS is invalid: {exc}") from exc
        if not lists:
            raise CommandError("RELAY_PUBLIC_LISTS is empty.")

        for spec in lists.values():
            self._provision(spec)

    def _provision(self, spec):
        if spec.key not in SENDERS:
            raise CommandError(f"No sender is defined for public list {spec.key}.")
        sender_id, sender_value = SENDERS[spec.key]
        sender_email = normalize_sender_email(sender_value)

        label = NAMES.get(spec.key, spec.organization_slug)
        organization, _ = Organization.objects.get_or_create(
            slug=spec.organization_slug,
            defaults={"name": label},
        )
        audience, _ = Audience.objects.get_or_create(
            organization=organization,
            slug=spec.audience_slug,
            defaults={"name": label},
        )
        client, created = Client.objects.get_or_create(
            organization=organization,
            slug=spec.client_slug,
            defaults={
                "name": label,
                "is_active": True,
                "default_sender_id": sender_id,
                "sender_emails": [{"id": sender_id, "email": sender_email}],
            },
        )
        if not created:
            client.is_active = True
            client.default_sender_id = sender_id
            client.sender_emails = [{"id": sender_id, "email": sender_email}]
            client.save()
        template_path = Path(settings.BASE_DIR) / "templates" / spec.key / f"{spec.template_key}.md"
        if not template_path.is_file():
            raise CommandError(f"Missing template file {template_path}.")
        post = frontmatter.load(str(template_path))
        required_context = [
            {"name": requirement.name, "description": requirement.description}
            for requirement in normalize_required_context(post.metadata.get("required_context") or [])
        ]
        EmailTemplate.objects.update_or_create(
            client=client,
            key=spec.template_key,
            defaults={
                "name": str(post.metadata.get("name") or spec.template_key),
                "subject": str(post.metadata["subject"]).strip(),
                "markdown_body": post.content,
                "category": str(post.metadata.get("category") or ""),
                "required_context": required_context,
                "example_context": post.metadata.get("example_context") or {},
                "default_sender_id": sender_id,
                "is_transactional": True,
                "is_active": True,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Provisioned public list {spec.key}: "
                f"organization={organization.slug} audience={audience.slug} "
                f"client={client.slug} sender={sender_email}"
            )
        )
