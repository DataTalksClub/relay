"""Import AISL-format markdown email templates as drafts.

Reads a directory of markdown files with YAML frontmatter (``subject``,
optional ``category``, ``required_context``, ``example_context``) and
creates or updates one draft template per file, keyed by file stem. The
markdown body is stored as-is; HTML rendering happens at send/preview time
so the draft stays the single editable source.
"""

from pathlib import Path

import frontmatter
from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client, EmailTemplate
from mailing.services.transactional_catalog import normalize_required_context


class Command(BaseCommand):
    help = "Import a directory of AISL-format markdown email templates as drafts."

    def add_arguments(self, parser):
        parser.add_argument("--dir", required=True, help="Directory containing *.md template files.")
        parser.add_argument("--client", required=True, help="Slug of the client that owns the imported drafts.")

    def handle(self, *args, **options):
        template_dir = Path(options["dir"])
        if not template_dir.is_dir():
            raise CommandError(f"Not a directory: {template_dir}")
        client = Client.objects.filter(slug=options["client"]).first()
        if client is None:
            raise CommandError(f"Client not found: {options['client']}")

        files = sorted(template_dir.glob("*.md"))
        if not files:
            raise CommandError(f"No .md template files found in {template_dir}")

        created_count = 0
        updated_count = 0
        error_count = 0
        for path in files:
            try:
                created = self._import_file(client, path)
            except Exception as exc:
                error_count += 1
                self.stderr.write(f"error: {path.name}: {exc}")
                continue
            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            f"Imported {len(files)} templates from {template_dir}: "
            f"{created_count} created, {updated_count} updated, {error_count} errors."
        )
        if error_count:
            raise CommandError(f"{error_count} template file(s) failed to import")

    def _import_file(self, client, path):
        post = frontmatter.load(str(path))
        subject = str(post.metadata.get("subject", "")).strip()
        if not subject:
            raise ValueError("frontmatter subject is required")
        category = str(post.metadata.get("category", "")).strip()
        required_context = [
            {"name": requirement.name, "description": requirement.description}
            for requirement in normalize_required_context(post.metadata.get("required_context") or [])
        ]
        example_context = post.metadata.get("example_context") or {}
        if not isinstance(example_context, dict):
            raise ValueError("example_context must be a mapping")

        template, created = EmailTemplate.objects.update_or_create(
            client=client,
            key=path.stem,
            defaults={
                "name": str(post.metadata.get("name") or path.stem),
                "subject": subject,
                "markdown_body": post.content,
                "category": category,
                "required_context": required_context,
                "example_context": example_context,
                "is_transactional": True,
                "is_active": True,
            },
        )
        return created
