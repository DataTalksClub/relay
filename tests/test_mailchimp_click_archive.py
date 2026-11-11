import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest

from scripts.mailchimp_click_archive import Archive, ArchiveError, MailchimpGET, checksum, encoded, timestamp

END = "2026-11-11T18:00:00Z"


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.reports = [{"id": "old", "send_time": "2024-01-01T00:00:00Z", "campaign_title": "Old"}]
        self.members = [{
            "email_id": "member1", "email_address": "one@example.org", "list_id": "audience",
            "activity": [
                {"action": "click", "timestamp": "2025-11-10T23:00:00Z", "url": "https://example.org/"},
                {"action": "click", "timestamp": "2025-11-10T23:00:00Z", "url": "https://example.org/"},
                {"action": "click", "timestamp": "2025-11-10T22:59:59Z", "url": "https://example.org/"},
                {"action": "click", "timestamp": END, "url": "https://example.org/"},
                {"action": "click", "timestamp": "2026-11-11T18:00:01Z", "url": "https://example.org/"},
                {"action": "open", "timestamp": END},
            ],
        }]

    def get(self, path, params):
        self.calls.append((path, params))
        if path == "/ping":
            return {"health_status": "Everything's Chimpy!"}
        collection, rows = ("reports", self.reports) if path == "/reports" else ("emails", self.members)
        offset, count = params["offset"], params["count"]
        return {collection: rows[offset:offset + count], "total_items": len(rows)}


def make_archive(tmp_path, api=None, **kwargs):
    return Archive(tmp_path, api or FakeAPI(), datacenter="us19", end=END, **kwargs)


def test_inventory_requests_no_report_sharing_credentials(tmp_path):
    api = FakeAPI()
    make_archive(tmp_path, api).run()
    assert next(params for path, params in api.calls if path == "/reports")["exclude_fields"] == "reports.share_report"


def test_unexpected_report_credentials_are_never_archived(tmp_path):
    api = FakeAPI()
    api.reports[0]["share_report"] = {"share_password": "unrelated-test-value"}
    with pytest.raises(ArchiveError, match="report-sharing credentials"):
        make_archive(tmp_path, api).run()
    assert not any(b"unrelated-test-value" in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_resume_preserves_superseded_inventory_audit_without_duplicate_pages(tmp_path):
    archive = make_archive(tmp_path)
    _, old_file = archive.page("/reports", {"count": 1000, "offset": 0})
    archive.run()
    assert (tmp_path / old_file).exists()
    assert next(iter(archive.manifest["superseded_inventory_pages"].values()))["file"] == old_file
    assert all(page["params"].get("exclude_fields") == "reports.share_report"
               for page in archive.manifest["pages"].values() if page["path"] == "/reports")


def test_old_campaign_and_inclusive_bounds_preserve_repeated_clicks(tmp_path):
    api = FakeAPI()
    result = make_archive(tmp_path, api).run()
    clicks = [json.loads(line) for line in (tmp_path / "clicks.jsonl").read_text().splitlines()]
    assert len(clicks) == 3
    assert len({row["source_event_id"] for row in clicks}) == 3
    assert [row["occurrence"] for row in clicks] == [1, 2, 1]
    assert all(row["campaign_id"] == "old" for row in clicks)
    assert result["window"]["start_inclusive"] == "2025-11-10T23:00:00Z"
    assert result["totals"]["raw_clicks"] == 5
    assert result["totals"]["clicks_before_window"] == 1
    assert result["totals"]["clicks_after_window"] == 1
    assert all("since_send_time" not in query for _, query in api.calls)
    activity_query = next(query for path, query in api.calls if path.endswith("email-activity"))
    assert activity_query["since"] == "2025-11-10T22:59:59Z"
    assert activity_query["filter_bots"] == "false"
    reconciliation = json.loads((tmp_path / "reconciliation.json").read_text())
    assert reconciliation["by_campaign"] == {"old": 3}
    assert reconciliation["by_contact"] == {"audience:member1": 3}
    assert reconciliation["by_link"] == {"old:https://example.org/": 3}
    assert result["artifacts"]["clicks.jsonl"]["sha256"] == checksum((tmp_path / "clicks.jsonl").read_bytes())


def test_full_pagination_uses_counts_and_offsets(tmp_path):
    api = FakeAPI()
    api.reports = [{"id": str(i)} for i in range(5)]
    archive = make_archive(tmp_path, api, page_size=2)
    assert len(archive.inventory()) == 5
    assert [query["offset"] for _, query in api.calls] == [0, 2, 4]


def test_resume_verifies_raw_checksums_and_reuses_frozen_end(tmp_path):
    archive = make_archive(tmp_path)
    archive.run()
    original = (tmp_path / "clicks.jsonl").read_bytes()
    api = FakeAPI()
    resumed = Archive(tmp_path, api, datacenter="us19")
    resumed.run()
    assert api.calls == [("/ping", {})]
    assert (tmp_path / "clicks.jsonl").read_bytes() == original
    page = next(iter(resumed.manifest["pages"].values()))
    (tmp_path / page["file"]).write_bytes(b"{}")
    with pytest.raises(ArchiveError, match="checksum"):
        make_archive(tmp_path).run()


def test_resume_rejects_account_window_or_page_size_change(tmp_path):
    make_archive(tmp_path)
    for changes in ({"datacenter": "us20"}, {"end": "2026-11-12T00:00:00Z"}, {"page_size": 20}):
        args = {"datacenter": "us19", "end": END, **changes}
        with pytest.raises(ArchiveError, match="Resume"):
            Archive(tmp_path, FakeAPI(), **args)


@pytest.mark.parametrize("rows,total", [([], 1), ([{"id": "x"}, {"id": "y"}], 1)])
def test_incomplete_pagination_fails_closed(tmp_path, rows, total):
    class BadAPI:
        def get(self, path, params):
            return {"reports": rows, "total_items": total}

    with pytest.raises(ArchiveError):
        list(make_archive(tmp_path, BadAPI()).paginate("/reports", "reports"))


def test_duplicate_members_across_pages_rejected(tmp_path):
    api = FakeAPI()
    api.reports = [{"id": "same"}, {"id": "same"}]
    with pytest.raises(ArchiveError, match="Duplicate"):
        make_archive(tmp_path, api, page_size=1).inventory()


def test_cap_is_reported_even_when_outside_window(tmp_path):
    archive = make_archive(tmp_path)
    member = FakeAPI().members[0]
    member["activity"] = [member["activity"][2]] * 1000
    counts = {key: Counter() for key in ("by_campaign", "by_contact", "by_link")}
    counters = Counter()
    archive.normalize_member(BytesIO(), "old", member, "raw/test.json", 0, counts, counters)
    assert counters["members_at_click_cap"] == 1
    assert counters["raw_clicks"] == 1000
    assert counters["in_window_clicks"] == 0


def test_variate_subreports_included(tmp_path):
    class ChildAPI(FakeAPI):
        def get(self, path, params):
            if path.endswith("sub-reports"):
                return {"reports": [{"id": "child", "type": "regular"}], "total_items": 1}
            return super().get(path, params)

    api = ChildAPI()
    api.reports[0]["type"] = "variate"
    assert set(make_archive(tmp_path, api).inventory()) == {"old", "child"}


def test_get_retries_rate_limit_and_redacts_failure():
    calls, sleeps = [], []

    def failing(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 429, "secret body", {"Retry-After": "2"}, None)

    api = MailchimpGET("TOP-SECRET", "us19", opener=failing, sleep=sleeps.append, attempts=3)
    with pytest.raises(ArchiveError, match=r"^Mailchimp GET failed \(HTTP 429\)$"):
        api.get("/reports", {"count": 1})
    assert sleeps == [2, 2]
    assert len(calls) == 3
    assert all(request.get_method() == "GET" for request in calls)


def test_invalid_timestamp_or_resource_is_rejected():
    with pytest.raises(ArchiveError, match="timezone"):
        timestamp("2025-11-11T00:00:00")
    api = MailchimpGET("secret", "us19")
    with pytest.raises(ArchiveError, match="Unsupported"):
        api.get("/lists/a/members", {})
    assert timestamp("2025-11-11T00:00:00+01:00") == datetime(2025, 11, 10, 23, tzinfo=UTC)
    assert checksum(encoded(["a", 1])) == checksum(encoded(["a", 1]))


def test_parallel_campaigns_bound_concurrency_and_keep_manifest_valid(tmp_path):
    class ParallelAPI(FakeAPI):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.reports = [{"id": str(i)} for i in range(8)]

        def get(self, path, params):
            if path.endswith("email-activity"):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.01)
                with self.lock:
                    self.active -= 1
            return super().get(path, params)

    api = ParallelAPI()
    manifest = make_archive(tmp_path, api, workers=3).run()
    assert 1 < api.maximum <= 3
    assert len(manifest["campaigns"]) == 8
    assert len(manifest["pages"]) == 9
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "complete_available_api_data"
    assert manifest["totals"]["in_window_clicks"] == 24
    with pytest.raises(ArchiveError, match="Workers"):
        make_archive(tmp_path / "bad", workers=9)
