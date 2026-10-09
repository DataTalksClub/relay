from datetime import datetime, timedelta
from datetime import timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from jobs.models import DEFAULT_SCHEDULE_TIMEZONE, Schedule
from jobs.services import normalize_submission, submit_job
from mailing.services.api_errors import ApiValidationError

MISSED_AFTER_SECONDS = 60


def validate_cron(value):
    if not isinstance(value, str) or not value.strip() or not croniter.is_valid(value.strip()):
        raise ApiValidationError({"cron": "invalid"})
    return value.strip()


def validate_timezone(value):
    """Accept only a real IANA zone name.

    Offsets are the trap: ``"+02:00"`` parses, resolves to a fixed instant, and
    is then an hour wrong for half the year. A webhook that fired at nine in
    Berlin looks copied-but-not-quite for months before anyone checks, so the
    name has to be one a clock in that place would actually show. Aliases that
    only exist on some hosts are rejected for the same reason.
    """
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({"timezone": "required"})
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiValidationError({"timezone": "unknown"}) from exc
    return name


def _zone(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # A stored name the validator never admitted. Surfaces as a ValueError
        # so callers that already guard croniter against a bad expression do
        # not need a second except clause for it.
        raise ValueError(f"unknown schedule timezone: {timezone_name!r}") from exc


def next_occurrence(expression, base=None, timezone_name=None):
    """The next time ``expression`` fires, as an aware datetime in UTC.

    ``timezone_name`` is the zone the expression is written against: ``0 9 * * *``
    is nine in the morning where the audience is, not nine UTC, and evaluating
    it any other way drifts by an hour twice a year without anything failing.

    Callers that pass no zone get the project's current timezone, which is what
    every caller got before schedules could name one. The arithmetic is done on
    the naive local time and localised back afterwards, because croniter steps
    in wall-clock terms and letting it reason about UTC offsets itself gets
    both sides of a daylight-saving transition wrong.
    """
    zone = _zone(timezone_name) if timezone_name else timezone.get_current_timezone()
    base = base or timezone.now()
    if timezone.is_naive(base):
        base = timezone.make_aware(base, zone)
    local = base.astimezone(zone)
    value = croniter(expression, local.replace(tzinfo=None)).get_next(datetime)
    return value.replace(tzinfo=zone).astimezone(utc_timezone.utc)


def upsert_schedule(data, client):
    errors = {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = "required"
        name = ""
    elif len(name.strip()) > 120:
        errors["name"] = "too_long"
    else:
        name = name.strip()

    try:
        cron = validate_cron(data.get("cron"))
    except ApiValidationError as exc:
        errors.update(exc.errors)
        cron = "* * * * *"

    definition = {
        "type": data.get("type"),
        "params": data.get("params", {}),
        "url": data.get("url"),
        "timeout_seconds": data.get("timeout_seconds"),
        "max_attempts": data.get("max_attempts", settings.RELAY_JOB_MAX_ATTEMPTS),
        "idempotency_key": "schedule-validation",
    }
    if definition["timeout_seconds"] is None:
        definition.pop("timeout_seconds")
    try:
        normalized = normalize_submission(definition, client)
    except ApiValidationError as exc:
        errors.update(exc.errors)
        normalized = None

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        errors["enabled"] = "must_be_boolean"

    timezone_name = data.get("timezone")
    if timezone_name is None:
        timezone_name = DEFAULT_SCHEDULE_TIMEZONE
    try:
        timezone_name = validate_timezone(timezone_name)
    except ApiValidationError as exc:
        errors.update(exc.errors)

    if errors:
        raise ApiValidationError(errors)

    schedule, created = Schedule.objects.update_or_create(
        client=client,
        name=name,
        defaults={
            "cron": cron,
            "timezone": timezone_name,
            "task_type": normalized.task_type,
            "task": normalized.task,
            "max_attempts": normalized.max_attempts,
            "enabled": enabled,
            "next_run_at": next_occurrence(cron, timezone_name=timezone_name),
        },
    )
    return schedule, created


def serialize_schedule(schedule):
    return {
        "id": str(schedule.pk),
        "name": schedule.name,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "type": schedule.task_type,
        "task": schedule.task,
        "max_attempts": schedule.max_attempts,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat(),
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "last_success_at": (
            schedule.last_success_at.isoformat() if schedule.last_success_at else None
        ),
        "last_missed_at": (
            schedule.last_missed_at.isoformat() if schedule.last_missed_at else None
        ),
        "last_job_id": str(schedule.last_job_id) if schedule.last_job_id else None,
    }


def run_due_schedules(*, limit=100, now=None):
    now = now or timezone.now()
    fired = []
    for _ in range(limit):
        with transaction.atomic():
            schedule = (
                Schedule.objects.select_for_update(skip_locked=True)
                .select_related("client")
                .filter(enabled=True, next_run_at__lte=now)
                .order_by("next_run_at")
                .first()
            )
            if schedule is None:
                break
            planned_for = schedule.next_run_at
            data = _submission_for_schedule(schedule, planned_for)
            job, _ = submit_job(data, schedule.client, schedule=schedule)
            schedule.last_run_at = now
            schedule.last_job = job
            if now - planned_for > timedelta(seconds=MISSED_AFTER_SECONDS):
                schedule.last_missed_at = planned_for
            # Fire at most once when the scheduler comes back after downtime.
            # The missed timestamp preserves the operational signal without a
            # restart causing an unbounded catch-up burst.
            schedule.next_run_at = next_occurrence(schedule.cron, now, schedule.timezone)
            schedule.save(
                update_fields=[
                    "last_run_at",
                    "last_job",
                    "last_missed_at",
                    "next_run_at",
                    "updated_at",
                ]
            )
            fired.append(job)
    return fired


def _submission_for_schedule(schedule, planned_for):
    data = {
        "type": schedule.task_type,
        "idempotency_key": f"schedule:{schedule.pk}:{planned_for.isoformat()}",
        "correlation_id": schedule.pk,
        "max_attempts": schedule.max_attempts,
    }
    if schedule.task_type == "webhook":
        data |= {
            "url": schedule.task["url"],
            "params": schedule.task["payload"],
            "timeout_seconds": schedule.task["timeout_seconds"],
        }
    else:
        data["params"] = schedule.task["params"]
    return data
