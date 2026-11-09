#!/usr/bin/env python3
"""Newsletter batch tooling for the Mailchimp -> Relay migration (docs/mailchimp-migration.md).

Every contact in the newsletter audience carries one batch tag derived from
batch_key = sha256(lowercased email)[:8] mod 100, the same rule as the first 5%
(`migration_bucket`). A campaign selects a batch with include_tags=[<batch tag>].

    batch-00-04  first 5% (the delivery-relay cohort)
    batch-05-09  5%       batch-10-14  5%      batch-15-24  10%
    batch-25-44  20%      batch-45-99  the rest (55%)

Commands (read-only unless noted; nothing here sends email):

    dry-run       print the recipient count for a batch range after
                  suppression and already-sent exclusion
    delta-import  WRITES: import new Mailchimp signups since a timestamp

Environment: RELAY_URL (default https://relay.datatalks.club),
RELAY_API_KEY (newsletter client key), MAILCHIMP_API_KEY (delta-import only).
Secrets are never printed.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AUDIENCE = "dtc"
CLIENT = "dtc-newsletter"
MAILCHIMP_LIST_ID = "97178021aa"
SOURCE_TAG = "src-mailchimp-import-{date}"
BATCH_RANGES = [(0, 4), (5, 9), (10, 14), (15, 24), (25, 44), (45, 99)]
IMPORT_BODY_LIMIT = 6800


def normalize(email):
    return (email or "").strip().casefold()


def batch_key(email):
    return int(hashlib.sha256(normalize(email).encode("utf-8")).hexdigest()[:8], 16) % 100


def batch_tag(lo, hi):
    return f"batch-{lo:02d}-{hi:02d}"


def tag_for_key(key):
    for lo, hi in BATCH_RANGES:
        if lo <= key <= hi:
            return batch_tag(lo, hi)
    raise ValueError(key)


def parse_range(text):
    lo, _, hi = text.partition("-")
    pair = (int(lo), int(hi))
    if pair not in BATCH_RANGES:
        valid = ", ".join(f"{a}-{b}" for a, b in BATCH_RANGES)
        raise SystemExit(f"unsupported batch range {text}; valid ranges: {valid}")
    return pair


def http_json(url, headers, body=None, tries=5):
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    request_headers = {"User-Agent": "curl/8.5.0", **headers}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data, request_headers)
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"HTTP {exc.code} from {url.split('?')[0]}")
        except (TimeoutError, urllib.error.URLError):
            if attempt < tries - 1:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"network error calling {url.split('?')[0]}")


class Relay:
    def __init__(self):
        self.base = os.environ.get("RELAY_URL", "https://relay.datatalks.club").rstrip("/")
        self.headers = {"Authorization": "Bearer " + os.environ["RELAY_API_KEY"]}

    def get(self, path, **params):
        return http_json(f"{self.base}{path}?{urllib.parse.urlencode(params)}", self.headers)

    def contacts(self, tag):
        cursor = None
        while True:
            params = {"audience": AUDIENCE, "client": CLIENT, "tags": tag, "limit": 500}
            if cursor:
                params["cursor"] = cursor
            page = self.get("/api/contacts", **params)
            yield from page["contacts"]
            cursor = page["next_cursor"]
            if not cursor:
                return

    def sent_emails(self, campaign_key):
        payload = self.get(f"/api/campaigns/{campaign_key}/recipients", audience=AUDIENCE, client=CLIENT)
        return {normalize(row["email"]) for row in payload["recipients"]}

    def import_contacts(self, items, dry_run):
        body = {"audience": AUDIENCE, "client": CLIENT, "dry_run": dry_run, "contacts": items}
        return http_json(f"{self.base}/api/contacts/imports", self.headers, body)


def dry_run(args):
    lo, hi = parse_range(args.range)
    tag = batch_tag(lo, hi)
    excluded_sent = set()
    for key in args.exclude_campaign:
        excluded_sent |= Relay().sent_emails(key)
    relay = Relay()
    seen = set()
    counts = {"tagged": 0, "duplicate_email": 0, "not_subscribed": 0, "suppressed": 0, "already_sent": 0}
    for contact in relay.contacts(tag):
        email = normalize(contact["email"])
        counts["tagged"] += 1
        if email in seen:
            counts["duplicate_email"] += 1
        elif contact["subscription_status"] != "subscribed":
            counts["not_subscribed"] += 1
        elif not contact["can_send_marketing"]:
            counts["suppressed"] += 1
        elif email in excluded_sent:
            counts["already_sent"] += 1
        else:
            counts.setdefault("recipients", 0)
            counts["recipients"] += 1
        seen.add(email)
    counts.setdefault("recipients", 0)
    print(json.dumps({"batch": f"{lo}-{hi}", "include_tag": tag, "excluded_campaigns": args.exclude_campaign, **counts}))


def mailchimp_members(since):
    key = os.environ["MAILCHIMP_API_KEY"]
    datacenter = key.rsplit("-", 1)[-1]
    headers = {"Authorization": "Basic " + base64.b64encode(f"x:{key}".encode()).decode()}
    base = f"https://{datacenter}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members"
    offset = 0
    while True:
        params = {
            "status": "subscribed",
            "since_last_changed": since,
            "count": 1000,
            "offset": offset,
            "fields": "members.email_address,total_items",
        }
        page = http_json(f"{base}?{urllib.parse.urlencode(params)}", headers)
        for member in page["members"]:
            yield normalize(member["email_address"])
        offset += 1000
        if offset >= page["total_items"]:
            return


def delta_import(args):
    relay = Relay()
    source_tag = SOURCE_TAG.format(date=args.source_date)
    items, skipped = [], {"already_in_relay": 0}
    for email in sorted(set(mailchimp_members(args.since))):
        status = relay.get("/api/contacts/status", email=email, audience=AUDIENCE, client=CLIENT)
        # Never resubscribe or re-tag anyone Relay already knows in this audience:
        # an existing subscription may be an unsubscribe made in Relay after import.
        if status.get("subscription_status"):
            skipped["already_in_relay"] += 1
            continue
        items.append({"email": email, "status": "subscribed", "verified": True,
                      "tags": [tag_for_key(batch_key(email)), source_tag]})
    print(json.dumps({"new_signups": len(items), **skipped, "dry_run": not args.apply}))
    batch, size = [], 0
    totals = {}
    for item in items + [None]:
        line = len(json.dumps(item, separators=(",", ":"))) + 1 if item else 0
        if batch and (item is None or size + line > IMPORT_BODY_LIMIT):
            result = relay.import_contacts(batch, dry_run=not args.apply)
            for name, value in result["counts"].items():
                totals[name] = totals.get(name, 0) + value
            batch, size = [], 0
        if item:
            batch.append(item)
            size += line
    print(json.dumps({"import_counts": totals}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run", help="recipient count for a batch range")
    dry.add_argument("--range", required=True, help="batch_key range, e.g. 5-9")
    dry.add_argument("--exclude-campaign", action="append", default=[], metavar="EXTERNAL_KEY",
                     help="exclude everyone with a recipient row (any status) in this campaign; repeatable")
    dry.set_defaults(func=dry_run)
    delta = sub.add_parser("delta-import", help="import new Mailchimp signups (dry run unless --apply)")
    delta.add_argument("--since", required=True, help="ISO timestamp, e.g. 2026-11-09T09:00:00+00:00")
    delta.add_argument("--source-date", default="2026-11-09", help="date in the source tag")
    delta.add_argument("--apply", action="store_true", help="write to Relay (default is a dry run)")
    delta.set_defaults(func=delta_import)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
