"""Local benchmark: how fast do N provider-events consumers drain SES events?

Needs a throwaway PostgreSQL (never prod): the database is migrated, seeded
with one campaign and ``--events`` SENT recipients, and one SNS-shaped SES event
per recipient is enqueued on the ``provider-events`` queue (delivery-heavy mix,
plus opens, clicks and bounces). Then ``--consumers`` real ``db_worker``
processes drain the queue and the script prints events/s, queries/event and
a double-apply check (``EmailEvent`` rows == events, no duplicate
``provider_event_id``).

    DATABASE_URL=postgres://relay:pw@127.0.0.1:55499/relay_bench \\
        uv run python scripts/bench_provider_events.py --events 6000 --consumers 3

Refuses to run unless the database host is local, so it cannot touch prod.
"""

# ruff: noqa: PLC0415 - Django must be configured before the model imports
import argparse
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "relay.settings")

MIX = (("Delivery", 0.70), ("Open", 0.22), ("Click", 0.06), ("Bounce", 0.02))
WAIT_TIMEOUT_SECONDS = 900


def _sns_event(message_id, event_name, n):
    detail_key = event_name.lower()
    ts = "2026-11-09T10:00:00.000Z"
    ses = {
        "eventType": event_name,
        "mail": {"messageId": message_id, "timestamp": ts, "source": "news@example.com"},
        detail_key: {"timestamp": ts},
    }
    if event_name == "Bounce":
        ses["bounce"] = {"bounceType": "Transient", "bounceSubType": "General", "timestamp": ts}
    if event_name == "Click":
        ses["click"] = {"link": "https://example.com/post", "timestamp": ts}
    return {
        "Type": "Notification",
        "MessageId": str(uuid.UUID(int=n + 1)),
        "TopicArn": "arn:aws:sns:eu-west-1:000000000000:bench",
        "Timestamp": ts,
        "Message": json.dumps(ses),
    }


def seed(events):
    from django.utils import timezone

    from mailing.models import (
        Audience,
        Campaign,
        CampaignRecipient,
        CampaignRecipientStatus,
        Client,
        Contact,
        Organization,
    )
    from mailing.tasks import process_ses_webhook_event

    org = Organization.objects.create(name="Bench", slug=f"bench-{uuid.uuid4().hex[:6]}")
    audience = Audience.objects.create(organization=org, name="Bench", slug=f"bench-{uuid.uuid4().hex[:6]}")
    client = Client.objects.create(organization=org, name="Bench", slug=f"bench-{uuid.uuid4().hex[:6]}")
    campaign = Campaign.objects.create(audience=audience, client=client, subject="Bench")
    tag = uuid.uuid4().hex[:8]
    contacts = Contact.objects.bulk_create(
        Contact(
            email=f"bench-{tag}-{i}@example.com",
            normalized_email=f"bench-{tag}-{i}@example.com",
            verified_at=timezone.now(),
        )
        for i in range(events)
    )
    CampaignRecipient.objects.bulk_create(
        CampaignRecipient(
            campaign=campaign,
            contact=c,
            email=c.email,
            status=CampaignRecipientStatus.SENT,
            ses_message_id=f"bench-{tag}-{i}",
            sent_at=timezone.now(),
        )
        for i, c in enumerate(contacts)
    )
    rng = random.Random(7)  # noqa: S311 - deterministic benchmark mix
    names = [n for n, _ in MIX]
    weights = [w for _, w in MIX]
    for i in range(events):
        name = rng.choices(names, weights)[0]
        process_ses_webhook_event.enqueue(_sns_event(f"bench-{tag}-{i}", name, i + int(tag, 16) * 10**7))
    return campaign.pk


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--events", type=int, default=6000)
    parser.add_argument("--consumers", type=int, default=1)
    parser.add_argument("--duplicates", type=float, default=0.0, help="fraction of events enqueued twice")
    args = parser.parse_args()

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection

    db = settings.DATABASES["default"]
    if db["ENGINE"].endswith("sqlite3") or db["HOST"] not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("refusing to run: DATABASE_URL must be a local PostgreSQL (benchmark mutates data)")

    call_command("migrate", verbosity=0)
    from django_tasks_db.models import DBTaskResult

    DBTaskResult.objects.filter(queue_name="provider-events").delete()  # leftovers of earlier runs
    campaign_id = seed(args.events)
    if args.duplicates:
        dup = list(DBTaskResult.objects.filter(status="READY").order_by("?")[: int(args.events * args.duplicates)])
        for task in dup:
            DBTaskResult.objects.create(
                task_path=task.task_path,
                args_kwargs=task.args_kwargs,
                queue_name=task.queue_name,
                backend_name=task.backend_name,
                priority=task.priority,
                run_after=task.run_after,
                status="READY",
            )
    connection.close()

    total = DBTaskResult.objects.filter(status="READY", queue_name="provider-events").count()
    cmd = [sys.executable, "manage.py", "db_worker", "--queue-name", "provider-events", "--interval", "0.2", "--batch"]
    started = time.monotonic()
    procs = [
        subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(args.consumers)
    ]  # noqa: S603
    deadline = started + WAIT_TIMEOUT_SECONDS
    for proc in procs:
        remaining = max(1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            for p in procs:
                p.kill()
            sys.exit(f"FAILED: consumers did not drain within {WAIT_TIMEOUT_SECONDS}s")
    wall = time.monotonic() - started

    from django.db.models import Count, Max, Min

    window = DBTaskResult.objects.filter(queue_name="provider-events", status="SUCCESSFUL").aggregate(
        first=Min("started_at"), last=Max("finished_at"), n=Count("id")
    )
    from mailing.models import EmailEvent

    events = EmailEvent.objects.filter(campaign_id=campaign_id)
    dupes = events.values("provider_event_id").annotate(c=Count("id")).filter(c__gt=1).count()
    failed = DBTaskResult.objects.filter(queue_name="provider-events").exclude(status="SUCCESSFUL").count()
    drain = (window["last"] - window["first"]).total_seconds()
    print(
        json.dumps(
            {
                "consumers": args.consumers,
                "tasks": total,
                "email_events": events.count(),
                "duplicate_provider_event_ids": dupes,
                "tasks_not_successful": failed,
                "wall_seconds_incl_startup": round(wall, 2),
                "drain_seconds": round(drain, 2),
                "events_per_second": round(window["n"] / drain, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
