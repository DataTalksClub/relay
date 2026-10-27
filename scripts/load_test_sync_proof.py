"""Local synchronous snapshot/health smoke benchmark; this is not deployed dev proof.

Use a dedicated DATABASE_URL and PYTHONPATH=. uv run python
scripts/load_test_sync_proof.py --contacts 10000. Seed data is retained.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import django


def setup_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "relay.settings")
    django.setup()
    from django.core.management import call_command  # noqa: PLC0415

    call_command("migrate", run_syncdb=True, verbosity=0)


def seed(contacts_n):
    from django.utils import timezone  # noqa: PLC0415

    from mailing.models import (  # noqa: PLC0415
        Audience,
        Campaign,
        Client,
        Contact,
        Organization,
        Subscription,
        SubscriptionStatus,
    )

    org = Organization.objects.create(name="Load proof", slug="load-proof")
    client = Client.objects.create(organization=org, name="Load client", slug="load-client")
    audience = Audience.objects.create(organization=org, name="Load readers", slug="load-readers")
    now = timezone.now()
    batch = []
    for i in range(contacts_n):
        email = f"load-{i:05d}@example.com"
        batch.append(Contact(email=email, normalized_email=email, verified_at=now))
    Contact.objects.bulk_create(batch, batch_size=500)
    subs = [
        Subscription(contact=c, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED)
        for c in Contact.objects.filter(email__startswith="load-")
    ]
    Subscription.objects.bulk_create(subs, batch_size=500)
    campaign = Campaign.objects.create(
        client=client, audience=audience, subject="Load proof", html_body="<p>Hi</p>", text_body="Hi"
    )
    return campaign


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", type=int, default=10000)
    ap.add_argument("--parallel", type=int, default=20)
    ap.add_argument("--requests", type=int, default=100)
    args = ap.parse_args()

    setup_django()
    campaign = seed(args.contacts)

    from django.test import Client as TestClient  # noqa: PLC0415

    from mailing.services.campaigns import estimate_campaign_recipients, request_campaign_queue  # noqa: PLC0415

    t0 = time.perf_counter()
    estimate = estimate_campaign_recipients(campaign)
    estimate_ms = (time.perf_counter() - t0) * 1000

    latencies = []
    errors_5xx = 0
    lock = threading.Lock()

    def hit(_i):
        nonlocal errors_5xx
        client = TestClient()
        start = time.perf_counter()
        try:
            resp = client.get("/health/ready")
            elapsed = (time.perf_counter() - start) * 1000
            with lock:
                latencies.append(elapsed)
                if resp.status_code >= 500:
                    errors_5xx += 1
        except Exception:
            with lock:
                latencies.append((time.perf_counter() - start) * 1000)
                errors_5xx += 1

    request_timings = []

    def queue_request():
        from django.db import connection  # noqa: PLC0415

        connection.close()
        start = time.perf_counter()
        result = request_campaign_queue(campaign)
        request_timings.append((time.perf_counter() - start) * 1000)
        assert result.accepted and not result.snapshotting

    wt = threading.Thread(target=queue_request, name="sync-request")
    wt.start()
    time.sleep(0.05)  # overlap health requests with the synchronous action
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        list(pool.map(hit, range(args.requests)))
    wt.join()

    campaign.refresh_from_db()
    report = {
        "contacts": args.contacts,
        "parallel": args.parallel,
        "requests": args.requests,
        "estimate_ms": round(estimate_ms, 1),
        "estimate_recipients": estimate.recipient_count,
        "sync_request_ms": round(request_timings[0], 1),
        "campaign_status_after_request": campaign.status,
        "health_p50_ms": round(percentile(latencies, 50), 1),
        "health_p95_ms": round(percentile(latencies, 95), 1),
        "health_max_ms": round(max(latencies) if latencies else 0, 1),
        "health_5xx": errors_5xx,
    }
    print(json.dumps(report, indent=2))
    ok = report["health_p95_ms"] < 500 and report["health_5xx"] == 0
    print("PROOF:", "PASS" if ok else "FAIL", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
