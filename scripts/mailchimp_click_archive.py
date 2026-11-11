"""Read-only, resumable Mailchimp per-contact click archive (stdlib only).

Set MAILCHIMP_API_KEY in the process environment; credentials are never archived.
All sent reports are inventoried, including campaigns sent before the window.
The raw files contain personal data. Restrict the archive's parent directory and
its Windows ACL before execution; POSIX directories/files use 0700/0600.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_START = "2025-11-10T23:00:00Z"  # 2025-11-11 midnight Europe/Berlin (CET).
MAX_WORKERS = 8  # Mailchimp permits 10 connections per user; reserve two for other account work.
DOCS = "https://mailchimp.com/developer/marketing/api/reports/email-activity/list-email-activity"
CAVEATS = [
    "Mailchimp limits activity to 1000 clicks and 1000 opens per member per campaign; capped histories may be incomplete.",
    "Only data retained and made available by the account/API can be recovered; deleted campaigns/contacts may be absent.",
    "API pagination is not a transactional snapshot; the end timestamp is frozen but membership may change during extraction.",
    "All bot activity is requested; activity is not filtered to human clicks.",
    "Campaign lifetime aggregate counters are context, not evidence of timestamp-level completeness for this window.",
]


class ArchiveError(Exception):
    """A safe error message that never includes HTTP response bodies or secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ArchiveError("Unexpected API redirect")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ArchiveError("All timestamp bounds and activity must include a timezone")
    return result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def restricted_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def atomic_write(path, data):
    restricted_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


class MailchimpGET:
    """GET-only API transport. No redirects may forward the Authorization header."""

    def __init__(self, token, datacenter, *, opener=None, sleep=time.sleep, attempts=6):
        if not re.fullmatch(r"[a-z]{2}\d+", datacenter):
            raise ArchiveError("Invalid Mailchimp datacenter")
        self.token = token
        self.base_url = f"https://{datacenter}.api.mailchimp.com/3.0"
        self.opener = opener or build_opener(NoRedirect).open
        self.sleep = sleep
        self.attempts = attempts

    def get(self, path, params):
        if not re.fullmatch(r"/(?:reports(?:/[a-zA-Z0-9_-]+(?:/(?:email-activity|sub-reports))?)?|ping)", path):
            raise ArchiveError("Unsupported GET resource")
        request = Request(self.base_url + path + "?" + urlencode(params), method="GET")
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("Accept", "application/json")
        for attempt in range(self.attempts):
            try:
                with self.opener(request, timeout=120) as response:
                    # Mailchimp's canonical API endpoint must never redirect.
                    if response.geturl().split("?")[0] != self.base_url + path:
                        raise ArchiveError("Unexpected API redirect")
                    return json.loads(response.read())
            except HTTPError as error:
                retry = error.code == 429 or 500 <= error.code < 600
                if not retry or attempt + 1 == self.attempts:
                    raise ArchiveError(f"Mailchimp GET failed (HTTP {error.code})") from None
                delay = error.headers.get("Retry-After", "") if error.headers else ""
                self.sleep(min(float(delay) if delay.isdigit() else 2**attempt, 120))
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    raise ArchiveError("Mailchimp GET failed (network error)") from None
                self.sleep(min(2**attempt, 120))
        raise ArchiveError("Mailchimp GET exhausted retries")


class Archive:
    def __init__(self, root, api, *, datacenter, start=DEFAULT_START, end=None, page_size=1000, workers=1):
        if not 1 <= page_size <= 1000:
            raise ArchiveError("Page size must be between 1 and 1000")
        if not 1 <= workers <= MAX_WORKERS:
            raise ArchiveError(f"Workers must be between 1 and {MAX_WORKERS}")
        self.lock = RLock()
        self.workers = workers
        self.root = Path(root)
        self.api = api
        self.page_size = page_size
        restricted_directory(self.root)
        manifest_path = self.root / "manifest.json"
        if manifest_path.exists():
            self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if self.manifest["datacenter"] != datacenter or timestamp(start) != timestamp(self.manifest["window"]["start_inclusive"]):
                raise ArchiveError("Resume bounds/account do not match the archive")
            if end and timestamp(end) != timestamp(self.manifest["window"]["end_inclusive"]):
                raise ArchiveError("Resume end does not match the frozen archive end")
            if page_size != self.manifest["page_size"]:
                raise ArchiveError("Resume page size does not match the archive")
        else:
            self.manifest = {
                "schema_version": 1, "source": "mailchimp", "datacenter": datacenter,
                "window": {"start_inclusive": iso(timestamp(start)),
                           "end_inclusive": iso(timestamp(end) if end else datetime.now(UTC)),
                           "timezone": "Europe/Berlin", "start_local": "2025-11-11T00:00:00+01:00" if start == DEFAULT_START else None},
                "page_size": page_size, "status": "in_progress", "pages": {}, "campaigns": {},
                "caveats": CAVEATS, "documentation": DOCS,
                "inventory_policy": "All reports without a send-date lower bound; variate/absplit child reports included",
            }
        self.start = timestamp(self.manifest["window"]["start_inclusive"])
        self.end = timestamp(self.manifest["window"]["end_inclusive"])
        if self.end < self.start:
            raise ArchiveError("End precedes start")
        self.save()

    def save(self):
        with self.lock:
            atomic_write(self.root / "manifest.json", encoded(self.manifest))

    def page(self, path, params):
        key = checksum(encoded([path, params]))
        relative = "raw/" + key + ".json"
        destination = self.root / relative
        previous = self.manifest["pages"].get(key)
        if previous:
            if not destination.exists() or checksum(destination.read_bytes()) != previous["sha256"]:
                raise ArchiveError("Raw page missing or checksum mismatch; refusing unverified resume")
            return json.loads(destination.read_text(encoding="utf-8")), relative
        payload = self.api.get(path, params)
        if (path == "/reports" or path.endswith("/sub-reports")) and any(
            "share_report" in report for report in payload.get("reports", [])
        ):
            raise ArchiveError("API did not exclude unrelated report-sharing credentials")
        data = encoded(payload)
        atomic_write(destination, data)
        with self.lock:
            self.manifest["pages"][key] = {
                "path": path, "params": params, "file": relative,
                "sha256": checksum(data), "retrieved_at": iso(datetime.now(UTC)),
            }
            self.save()
        return payload, relative

    def paginate(self, path, collection, params=None):
        offset = 0
        expected = None
        seen = set()
        while True:
            query = {**(params or {}), "count": self.page_size, "offset": offset}
            payload, relative = self.page(path, query)
            items = payload.get(collection)
            total = payload.get("total_items")
            if not isinstance(items, list) or not isinstance(total, int) or total < 0:
                raise ArchiveError("Malformed paginated API response")
            if expected is not None and total != expected:
                raise ArchiveError("Collection total changed during pagination; completeness cannot be established")
            expected = total
            if len(items) > self.page_size or offset + len(items) > total:
                raise ArchiveError("API pagination count mismatch")
            for index, item in enumerate(items):
                identity = item.get("id") or item.get("email_id")
                if identity:
                    if identity in seen:
                        raise ArchiveError("Duplicate API item across pages; completeness cannot be established")
                    seen.add(identity)
                yield item, relative, index
            offset += len(items)
            if offset == total:
                return
            if not items:
                raise ArchiveError("API returned an empty page before total_items")

    def inventory(self):
        reports = {}
        pending = [item for item, _, _ in self.paginate(
            "/reports", "reports", {"exclude_fields": "reports.share_report"}
        )]
        while pending:
            report = pending.pop(0)
            campaign_id = report["id"]
            if campaign_id in reports:
                continue
            reports[campaign_id] = report
            if report.get("type") in {"variate", "absplit"}:
                pending.extend(item for item, _, _ in self.paginate(
                    f"/reports/{campaign_id}/sub-reports", "reports", {"exclude_fields": "reports.share_report"}
                ))
        atomic_write(self.root / "campaigns.json", encoded(list(reports.values())))
        # A resumed archive may contain an earlier inventory request shape.
        # Keep its audit/checksum record without treating two snapshots as two
        # pages of the same sub-report collection.
        for key, page in list(self.manifest["pages"].items()):
            if (page["path"] == "/reports" or page["path"].endswith("/sub-reports")) and (
                page["params"].get("exclude_fields") != "reports.share_report"
            ):
                self.manifest.setdefault("superseded_inventory_pages", {})[key] = page
                del self.manifest["pages"][key]
        self.save()
        return reports

    def run(self):
        # A successful GET verifies the grant before campaign extraction. A key may
        # have broader permissions, but this program never exercises a write grant.
        self.api.get("/ping", {})
        self.manifest["status"] = "in_progress"
        self.manifest["grant_verified_at"] = iso(datetime.now(UTC))
        reports = self.inventory()
        self.manifest["campaign_count"] = len(reports)
        self.save()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self.extract_campaign, campaign_id, report) for campaign_id, report in reports.items()]
            for future in futures:
                future.result()
        self.normalize(reports)
        self.manifest["status"] = "complete_available_api_data"
        self.manifest["completed_at"] = iso(datetime.now(UTC))
        self.save()
        return self.manifest

    def extract_campaign(self, campaign_id, report):
        # API's `since` is strictly after; request one second before the inclusive
        # lower bound, then apply exact bounds locally to the returned timestamps.
        params = {"since": iso(self.start - timedelta(seconds=1)), "filter_bots": "false"}
        count = sum(1 for _ in self.paginate(f"/reports/{campaign_id}/email-activity", "emails", params))
        with self.lock:
            self.manifest["campaigns"][campaign_id] = {
                "members_archived": count, "title": report.get("campaign_title", ""),
                "send_time": report.get("send_time"), "lifetime_clicks": report.get("clicks", {}).get("clicks_total"),
                "status": "pages_complete",
            }
            self.save()

    def normalize(self, reports):
        counts = {"by_campaign": Counter(), "by_contact": Counter(), "by_link": Counter()}
        counters = Counter({key: 0 for key in (
            "members", "raw_clicks", "in_window_clicks", "members_at_click_cap",
            "clicks_before_window", "clicks_after_window",
        )})
        output = self.root / "clicks.jsonl.tmp"
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for campaign_id in reports:
                params = {"since": iso(self.start - timedelta(seconds=1)), "filter_bots": "false"}
                rows = self.paginate(f"/reports/{campaign_id}/email-activity", "emails", params)
                for member, raw_file, member_index in rows:
                    self.normalize_member(stream, campaign_id, member, raw_file, member_index, counts, counters)
            stream.flush()
            os.fsync(stream.fileno())
        output.replace(self.root / "clicks.jsonl")
        reconciliation = {key: dict(sorted(value.items())) for key, value in counts.items()}
        reconciliation["totals"] = dict(counters)
        atomic_write(self.root / "reconciliation.json", encoded(reconciliation))
        self.manifest["totals"] = dict(counters)
        self.manifest["artifacts"] = {
            filename: {"sha256": checksum((self.root / filename).read_bytes())}
            for filename in ("campaigns.json", "clicks.jsonl", "reconciliation.json")
        }

    def normalize_member(self, stream, campaign_id, member, raw_file, member_index, counts, counters):
        clicks = [(i, event) for i, event in enumerate(member.get("activity", [])) if event.get("action") == "click"]
        counters["members"] += 1
        counters["raw_clicks"] += len(clicks)
        if len(clicks) >= 1000:
            counters["members_at_click_cap"] += 1
        occurrences = Counter()
        for activity_index, event in clicks:
            at = timestamp(event["timestamp"])
            if at < self.start:
                counters["clicks_before_window"] += 1
                continue
            if at > self.end:
                counters["clicks_after_window"] += 1
                continue
            value = self.click_value(campaign_id, member, event, at, occurrences)
            value.update(raw_file=raw_file, raw_member_index=member_index, raw_activity_index=activity_index)
            stream.write(encoded(value) + b"\n")
            counters["in_window_clicks"] += 1
            counts["by_campaign"][campaign_id] += 1
            counts["by_contact"][member["list_id"] + ":" + member["email_id"]] += 1
            counts["by_link"][campaign_id + ":" + event["url"]] += 1

    @staticmethod
    def click_value(campaign_id, member, event, at, occurrences):
        signature = [campaign_id, member["list_id"], member["email_id"], iso(at), event["url"]]
        key = encoded(signature)
        occurrences[key] += 1
        return {
            "source": "mailchimp", "source_event_id": "mailchimp:click:" + checksum(encoded(signature + [occurrences[key]])),
            "campaign_id": campaign_id, "list_id": member["list_id"], "email_id": member["email_id"],
            "email_address": member["email_address"], "timestamp": iso(at), "url": event["url"], "occurrence": occurrences[key],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--datacenter", required=True, help="Verified account datacenter, for example us19")
    parser.add_argument("--start", default=DEFAULT_START, help="Inclusive ISO timestamp (default: Berlin 2025-11-11 midnight)")
    parser.add_argument("--end", help="Inclusive frozen extraction timestamp; defaults to initial invocation UTC time")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=1, help=f"Parallel campaigns, 1 to {MAX_WORKERS} (default 1)")
    args = parser.parse_args(argv)
    token = os.environ.get("MAILCHIMP_API_KEY", "").strip()
    if not token:
        parser.error("MAILCHIMP_API_KEY is required in the process environment")
    try:
        api = MailchimpGET(token, args.datacenter)
        archive = Archive(
            args.archive, api, datacenter=args.datacenter, start=args.start, end=args.end,
            page_size=args.page_size, workers=args.workers,
        )
        manifest = archive.run()
        print(json.dumps({"status": manifest["status"], "campaign_count": manifest["campaign_count"], "totals": manifest["totals"]}))
    except (ArchiveError, ValueError, KeyError) as error:
        # Do not echo values from malformed API data, which can contain PII.
        print(str(error) if isinstance(error, ArchiveError) else "Invalid archive or API data", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
