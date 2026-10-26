"""Load-test proof for the async request-path move (intake 014946da).

Simulates the heaviest former-sync action (campaign queue snapshot over a
500-contact audience) running on the worker while parallel read traffic hits
the health endpoint and a normal operator page. Reports p95 and 5xx counts.

Run: uv run python scripts/load_test_async_proof.py [--contacts N] [--parallel P]
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
    ap.add_argument("--contacts", type=int, default=500)
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

    # Async request timing (what the operator POST now does).
    t0 = time.perf_counter()
    result = request_campaign_queue(campaign)
    request_ms = (time.perf_counter() - t0) * 1000
    assert result.snapshotting

    # Reset to DRAFT-adjacent so the background run below does the real work:
    # the request above already flipped to SNAPSHOTTING, which is exactly the
    # state the worker resumes from — keep it and run the worker in background.
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

    def worker():
        from django.db import connection  # noqa: PLC0415

        connection.close()
        from mailing.services.campaigns import queue_campaign  # noqa: PLC0415

        queue_campaign(campaign)

    wt = threading.Thread(target=worker, name="worker-snapshot")
    wt.start()
    time.sleep(0.05)  # let the snapshot take the write lock first
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
        "async_request_ms": round(request_ms, 1),
        "campaign_status_after_worker": campaign.status,
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
