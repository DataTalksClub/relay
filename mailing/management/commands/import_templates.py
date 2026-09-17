from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client, EmailTemplate, TemplateContentFormat
from mailing.services.transactional_markdown import TemplateMarkdownError, parse_frontmatter


class Command(BaseCommand):
    help = (
        "Import markdown templates (YAML frontmatter with subject/required_context/category plus a "
        "markdown body, the AISL email template format) as editable drafts, one per *.md file, "
        "keyed by file stem. Re-runs update the same drafts idempotently. Publishing still happens "
        "separately through the publish API."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dir", required=True, help="Directory containing *.md template files.")
        parser.add_argument("--client", required=True, help="Slug of the client that owns the drafts.")

    def handle(self, *args, **options):
        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"Not a directory: {directory}")

        clients = Client.objects.filter(slug=options["client"])
        if clients.count() == 0:
            raise CommandError(f"Client not found: {options['client']}")
        if clients.count() > 1:
            raise CommandError(
                f"Client slug '{options['client']}' matches {clients.count()} clients across organizations; "
                "rename one of them before importing."
            )
        client = clients.get()

        files = sorted(directory.glob("*.md"))
        if not files:
            raise CommandError(f"No *.md files found in {directory}")

        created = 0
        updated = 0
        errors = []
        for path in files:
            try:
                defaults = parse_template_file(path)
            except (OSError, TemplateMarkdownError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue

            _, was_created = EmailTemplate.objects.update_or_create(
                client=client,
                key=path.stem,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        for error in errors:
            self.stderr.write(f"error: {error}")

        summary = (
            f"Imported {len(files)} files from {directory} for client '{client.slug}': "
            f"created={created}, updated={updated}, errors={len(errors)}."
        )
        if errors:
            raise CommandError("\n".join([summary, *(f"error: {error}" for error in errors)]))
        self.stdout.write(self.style.SUCCESS(summary))


def parse_template_file(path):
    """Parse one markdown template file into upsert defaults for a draft."""
    source = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(source)

    subject = metadata.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise TemplateMarkdownError("frontmatter key 'subject' is required and must be a non-empty string")

    required_context = metadata.get("required_context")
    if required_context in (None, ""):
        required_context = []
    elif not isinstance(required_context, list):
        raise TemplateMarkdownError("frontmatter key 'required_context' must be a list")

    category = metadata.get("category")
    if category in (None, ""):
        category = ""
    elif not isinstance(category, str):
        raise TemplateMarkdownError("frontmatter key 'category' must be a string")

    return {
        "name": path.stem,
        "subject": subject.strip(),
        "content_format": TemplateContentFormat.MARKDOWN,
        "markdown_body": body,
        "category": category.strip(),
        "required_context": required_context,
        "is_transactional": True,
    }
