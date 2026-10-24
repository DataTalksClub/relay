"""Operator retry for failed transactional messages.

Single message:

    python manage.py retry_transactional 8017 --reason "route fixed, resend"

Bulk by filter (lists first with --dry-run, then confirm):

    python manage.py retry_transactional --client hetzner-maintenance \\
        --error-contains "MessageRejected" --dry-run
    python manage.py retry_transactional --client hetzner-maintenance \\
        --reason "route fixed" --yes

Only ``failed`` messages retry without ``--force``. ``--force`` needs
``--reason`` and still never retries suppressed/blocked recipients.
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.dry_run import dry_run_enforced
from mailing.models import TransactionalMessageStatus
from mailing.services.transactional_retry import (
    MAX_BULK_RETRY,
    BulkRetryResult,
    bulk_retry_transactional_messages,
    collect_retry_candidates,
    retry_transactional_message,
)
from mailing.services.transactional_retry import (
    TransactionalRetryError as RetryError,
)


def parse_dt(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CommandError(f"invalid datetime {value!r}: use ISO format") from exc
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class Command(BaseCommand):
    help = "Retry failed transactional messages (single id or bulk filter)."

    def add_arguments(self, parser):
        parser.add_argument("message_id", nargs="?", type=int, help="Retry one message by id.")
        parser.add_argument("--client", default="", help="Limit bulk retry to a client slug.")
        parser.add_argument(
            "--status",
            default=TransactionalMessageStatus.FAILED,
            help="Bulk filter status (default: failed). Non-failed needs --force.",
        )
        parser.add_argument("--template-key", default="", help="Limit bulk retry to a template key.")
        parser.add_argument("--error-contains", default="", help="Limit bulk retry by error text.")
        parser.add_argument("--created-after", default="", help="ISO datetime lower bound.")
        parser.add_argument("--created-before", default="", help="ISO datetime upper bound.")
        parser.add_argument(
            "--limit",
            type=int,
            default=MAX_BULK_RETRY,
            help=f"Cap bulk retries (default {MAX_BULK_RETRY}, max {MAX_BULK_RETRY}).",
        )
        parser.add_argument("--reason", default="", help="Operator reason recorded in audit/event.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow non-failed statuses (requires --reason; never overrides suppression).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be re-queued; enqueue nothing.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the bulk confirmation prompt.",
        )

    def handle(self, *args, **options):
        reason = (options["reason"] or "").strip()
        force = options["force"]
        dry_run = options["dry_run"]
        if force and not reason:
            raise CommandError("--force requires --reason")
        limit = options["limit"]
        if limit < 1 or limit > MAX_BULK_RETRY:
            raise CommandError(f"--limit must be 1..{MAX_BULK_RETRY}")
        if dry_run_enforced():
            self.stdout.write(self.style.WARNING("RELAY_DRY_RUN is enforced: sends stay suppressed downstream."))

        if options["message_id"] is not None:
            return self.handle_single(options["message_id"], reason=reason, force=force, dry_run=dry_run)

        return self.handle_bulk(options, reason=reason, force=force, dry_run=dry_run, limit=limit)

    def handle_single(self, message_id, *, reason, force, dry_run):
        from mailing.models import TransactionalMessage  # noqa: PLC0415

        try:
            message = TransactionalMessage.objects.select_related("client").get(pk=message_id)
        except TransactionalMessage.DoesNotExist as exc:
            raise CommandError(f"transactional message {message_id} not found") from exc
        if dry_run:
            self.stdout.write(
                f"would retry message {message.id} ({message.email}, "
                f"client={message.client.slug}, status={message.status}): {message.last_error[:200]}"
            )
            return
        try:
            result = retry_transactional_message(message.pk, actor=None, reason=reason, force=force)
        except RetryError as exc:
            raise CommandError(f"retry refused: {exc.code}") from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"retried message {result.message_id}: status -> {result.status} "
                f"(retry #{result.retry_count}, sender_changed={result.sender_changed})"
            )
        )

    def handle_bulk(self, options, *, reason, force, dry_run, limit):
        qs = collect_retry_candidates(
            client_slug=options["client"],
            status=options["status"],
            created_after=parse_dt(options["created_after"]),
            created_before=parse_dt(options["created_before"]),
            error_contains=options["error_contains"],
            template_key=options["template_key"],
        )
        total = qs.count()
        if total == 0:
            self.stdout.write("no messages match the filters.")
            return
        if total > MAX_BULK_RETRY and not dry_run:
            raise CommandError(
                f"{total} messages match; narrow the filters or use --limit 1..{MAX_BULK_RETRY} "
                f"(max batch is {MAX_BULK_RETRY})."
            )
        if dry_run:
            result: BulkRetryResult = bulk_retry_transactional_messages(
                qs, actor=None, reason=reason, force=force, limit=limit, dry_run=True
            )
            for row in result.retried:
                self.stdout.write(
                    f"would retry {row['id']} ({row['email']}, client={row['client']}, "
                    f"template={row['template_key']}, status={row['status']})"
                )
            self.stdout.write(self.style.WARNING(f"dry-run: {len(result.retried)} would be re-queued."))
            return
        if not options["yes"]:
            self.stdout.write(f"about to retry up to {min(total, limit)} message(s).")
            confirm = input("Type YES to continue: ").strip()
            if confirm != "YES":
                self.stdout.write("aborted.")
                return
        result = bulk_retry_transactional_messages(
            qs, actor=None, reason=reason, force=force, limit=limit, dry_run=False
        )
        for row in result.retried:
            self.stdout.write(f"retried {row['id']} ({row['email']})")
        for row in result.skipped:
            self.stderr.write(f"skipped {row['id']} ({row['email']}): {row['code']}")
        self.stdout.write(
            self.style.SUCCESS(f"retried {len(result.retried)} message(s); skipped {len(result.skipped)}.")
        )
