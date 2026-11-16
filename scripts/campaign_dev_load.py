"""Dev-address-only campaign load proof, run via manage.py shell on Relay.

RELAY_LOAD_RUN must be a fresh alphanumeric/hyphen label. RELAY_LOAD_COUNT
(default 6000, maximum 20000) controls the number of real SES sends. No existing
campaign, audience or list is accepted as input. The only destinations this
script creates/queues match dev+relay131k-...@dtcdev.click.

    RELAY_LOAD_RUN=20261116-a uv run python manage.py shell < scripts/campaign_dev_load.py

Output contains only aggregate measurements and the new campaign ID, no secrets.
"""

import json
import os
import re
import threading
import time
from collections import Counter

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.enqueue import batch_campaign_enqueues, enqueue_campaign_email
from mailing.models import Audience, Campaign, CampaignRecipient, Client, Contact, Organization, Subscription

run = os.environ.get("RELAY_LOAD_RUN", "")
count = int(os.environ.get("RELAY_LOAD_COUNT", "6000"))
if not re.fullmatch(r"[a-z0-9-]{1,40}", run) or not 100 <= count <= 20000:
    raise ValueError("Set a fresh RELAY_LOAD_RUN and count between 100 and 20000")
key = f"dev-relay131k-{run}"
# Refuse to mix this proof with runnable or running real campaigns. Stalled
# campaign records (including AISHL 253/254) are neither read nor changed.
if DBTaskResult.objects.filter(queue_name="campaign", status__in=["READY", "RUNNING"]).exists():
    raise RuntimeError("Campaign queue is busy; do not overlap another send")
if Organization.objects.filter(slug=key).exists():
    raise ValueError("Run label already exists; never resume/reuse a load campaign")
emails = [f"dev+relay131k-{run}-{i}@dtcdev.click" for i in range(count)]
if not all(re.fullmatch(r"dev\+relay131k-[a-z0-9-]+@dtcdev\.click", email) for email in emails):
    raise ValueError("Destination allowlist rejected")
now = timezone.now()
with transaction.atomic():
    org = Organization.objects.create(name=key, slug=key)
    audience = Audience.objects.create(organization=org, name=key, slug=key)
    client = Client.objects.create(organization=org, name=key, slug=key)
    contacts = Contact.objects.bulk_create([
        Contact(email=email, normalized_email=email, verified_at=now) for email in emails
    ])
    Subscription.objects.bulk_create([
        Subscription(contact=c, audience=audience, client=client, status="subscribed") for c in contacts
    ])
    # A realistic newsletter body: hundreds of tracked links, ~80 KB of HTML.
    html = "<html><body>" + "".join(
        f'<p>Dev load item {i}: <a href="https://datatalks.club/?load={i}">Read item</a></p>'
        + '<span style="font-family:Arial;color:#333">Dev capacity measurement. </span>' * 2
        for i in range(350)
    ) + "</body></html>"
    campaign = Campaign.objects.create(
        audience=audience, client=client, external_key=key, subject=f"Relay dev capacity proof {run}",
        html_body=html, text_body="Dev capacity measurement only.", status="queued", recipient_count=count,
    )
    recipients = CampaignRecipient.objects.bulk_create([
        CampaignRecipient(campaign=campaign, contact=c, email=c.email) for c in contacts
    ])
    # Recheck persisted destinations before creating any send tasks.
    persisted = list(campaign.recipients.values_list("email", flat=True))
    if len(persisted) != count or set(persisted) != set(emails):
        raise RuntimeError("Persisted destination mismatch; nothing queued")

samples = []
stop = threading.Event()


def observe():
    try:
        while not stop.is_set():
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity")
                total = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
                active = cursor.fetchone()[0]
            samples.append((time.monotonic(), total, active))
            stop.wait(0.1)
    finally:
        connection.close()


observer = threading.Thread(target=observe, daemon=True)
observer.start()
time.sleep(0.3)
started = time.monotonic()
print(json.dumps({"event": "load_start", "campaign_id": campaign.pk, "count": count,
                  "destination_pattern": "dev+relay131k-...@dtcdev.click", "html_bytes": len(html),
                  "pg_baseline": samples[-1][1] if samples else None}), flush=True)
try:
    with batch_campaign_enqueues(None):
        for offset in range(0, count, 500):
            enqueue_campaign_email({
                "contract": "campaign-email", "version": 1, "campaign_id": campaign.pk,
                "batch_id": f"{key}-{offset // 500}",
                "campaign_recipient_ids": [r.pk for r in recipients[offset:offset + 500]],
            })
    deadline = started + 900
    last_report = started
    while time.monotonic() < deadline:
        states = Counter(dict(campaign.recipients.values_list("status").order_by("status").annotate(
            n=Count('id')
        )))
        if time.monotonic() - last_report > 10:
            print(json.dumps({"event": "load_progress", "elapsed_s": round(time.monotonic() - started, 1),
                              "states": states}), flush=True)
            last_report = time.monotonic()
        if sum(states.get(s, 0) for s in ["pending", "sending"]) == 0:
            break
        time.sleep(0.5)
    elapsed = time.monotonic() - started
finally:
    stop.set()
    observer.join(timeout=5)

sent_times = sorted(campaign.recipients.exclude(sent_at=None).values_list("sent_at", flat=True))
send_span = (sent_times[-1] - sent_times[0]).total_seconds() if len(sent_times) > 1 else 0
lo, hi = int(len(sent_times) * 0.1), int(len(sent_times) * 0.9)
central_span = (sent_times[hi] - sent_times[lo]).total_seconds() if hi > lo else 0
print(json.dumps({"event": "load_result", "campaign_id": campaign.pk, "count": count,
                  "states": states, "elapsed_s": round(elapsed, 3),
                  "wall_rate_per_s": round(len(sent_times) / elapsed, 2),
                  "send_span_rate_per_s": round(len(sent_times) / send_span, 2) if send_span else None,
                  "central_80pct_rate_per_s": round((hi - lo) / central_span, 2) if central_span else None,
                  "pg_peak_total": max(s[1] for s in samples),
                  "pg_peak_active": max(s[2] for s in samples), "pg_samples": len(samples)}), flush=True)
if len(sent_times) != count or states.get("failed", 0) or states.get("unknown", 0):
    raise RuntimeError("Load proof incomplete; inspect only this new dev campaign")
