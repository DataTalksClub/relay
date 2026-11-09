import json
import time

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.test import Client as HttpClient
from django.urls import reverse

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Client, NewsletterBlock, NewsletterIssue, NewsletterTestSend, Organization
from mailing.services import newsletters as svc
from mailing.services.api_errors import ApiValidationError
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_record():
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(
        organization=org, name="DTC newsletter", slug="dtc-newsletter", default_sender_id="alexey",
        sender_emails=[{"id": "alexey", "email": "Alexey Grigorev <alexey@datatalks.club>"}],
    )


@pytest.fixture
def other_client(client_record):
    return Client.objects.create(organization=client_record.organization, name="Other", slug="other")


@pytest.fixture
def operator(client_record):
    user = get_user_model().objects.create_user("valeriia@datatalks.club", "valeriia@datatalks.club", is_staff=True)
    http = HttpClient()
    http.force_login(user)
    session = http.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()
    return http


@pytest.fixture
def api(client_record):
    _, raw = create_client_api_key(client=client_record, name="dataops")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


@pytest.fixture
def issue(client_record):
    issue = svc.create_issue(client_record, {"key": "weekly-303", "subject": "DataTalks.Club Weekly #303",
                                             "preview_text": "Course launch", "issue_number": "303"})
    svc.add_block(issue, "intro", {"issue_label": "#303 · November 16, 2026", "summary": "This week: **news**."})
    svc.add_block(issue, "sponsor", {"title": "Try Acme", "body": "Acme <b>rocks</b> [docs](https://acme.dev/docs)",
                                     "cta_text": "Try it", "cta_url": "https://acme.dev/?utm_source=dtc"})
    return issue


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(source, email, rendered):
        calls.append({"source": source, "email": email, **rendered})
        return f"msg-{len(calls)}"

    monkeypatch.setattr(svc, "_deliver", fake)
    return calls


# --------------------------------------------------------------------------- validation and rendering

def test_validate_block_rejects_bad_urls_unknown_fields_and_missing_required():
    with pytest.raises(ApiValidationError) as exc:
        svc.validate_block("sponsor", {"title": "", "cta_url": "javascript:alert(1)", "nope": "x"})
    assert exc.value.errors == {"data.nope": "unknown_field", "data.title": "required",
                                "data.cta_text": "required", "data.cta_url": "invalid_url"}
    with pytest.raises(ApiValidationError):
        svc.validate_block("not-a-type", {})


def test_render_escapes_html_and_neutralises_script_links(issue):
    svc.add_block(issue, "feature", {"title": "<script>x</script>", "body": "[bad](javascript:alert(1))"})
    html = svc.render_issue(issue)["html_body"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "&lt;b&gt;rocks&lt;/b&gt;" in html
    assert 'href="https://acme.dev/docs"' in html
    assert "SPONSORED" in html and "DataTalks.Club is a global community" in html


def test_test_render_is_marked_and_untracked(issue):
    rendered = svc.render_issue(issue, test=True)
    assert rendered["subject"] == "[TEST] DataTalks.Club Weekly #303"
    assert "TEST EMAIL" in rendered["html_body"] and rendered["text_body"].startswith("[TEST EMAIL")
    assert "/t/c/" not in rendered["html_body"] and "/t/o/" not in rendered["html_body"]
    assert "/unsubscribe/" not in rendered["html_body"]


def test_block_order_moves_and_deletes(issue):
    first, second = issue.blocks.order_by("position")
    svc.move_block(second, "up")
    assert list(issue.blocks.order_by("position").values_list("block_type", flat=True)) == ["sponsor", "intro"]
    svc.add_block(issue, "html", {"html": "<p>x</p>"}, position=0)
    svc.delete_block(issue.blocks.get(block_type="sponsor"))
    assert list(issue.blocks.order_by("position").values_list("block_type", "position")) == [("html", 0), ("intro", 1)]


# --------------------------------------------------------------------------- test sends

def test_send_test_whole_issue_and_single_block(issue, sent):
    result = svc.send_test(issue, ["alexey@datatalks.club", "sponsor@acme.dev"], via="api")
    assert result["status"] == "sent"
    assert [call["email"] for call in sent] == ["alexey@datatalks.club", "sponsor@acme.dev"]
    assert sent[0]["source"] == "Alexey Grigorev <alexey@datatalks.club>"
    assert sent[0]["subject"].startswith("[TEST] ")
    block = issue.blocks.get(block_type="sponsor")
    svc.send_test(issue, ["sponsor@acme.dev"], block=block, via="ui")
    assert "Try Acme" in sent[-1]["html_body"] and "This week" not in sent[-1]["html_body"]
    assert NewsletterTestSend.objects.filter(issue=issue, status="sent").count() == 3


def test_send_test_reports_provider_errors_per_address(issue, monkeypatch):
    def failing(source, email, rendered):
        if email.startswith("bad"):
            raise ClientError({"Error": {"Code": "MessageRejected", "Message": "Email address is not verified."}},
                              "SendEmail")
        return "ok-id"

    monkeypatch.setattr(svc, "_deliver", failing)
    result = svc.send_test(issue, ["bad@acme.dev", "good@acme.dev"])
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "failed"
    assert "MessageRejected" in result["results"][0]["error"]
    assert result["results"][1] == {"email": "good@acme.dev", "status": "sent", "message_id": "ok-id", "error": ""}
    assert NewsletterTestSend.objects.get(email="bad@acme.dev").error.startswith("MessageRejected")


def test_send_test_times_out_and_stops(issue, monkeypatch):
    monkeypatch.setattr(svc, "TEST_SEND_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(svc, "_deliver", lambda source, email, rendered: time.sleep(1))
    result = svc.send_test(issue, ["a@acme.dev", "b@acme.dev"])
    assert [item["status"] for item in result["results"]] == ["timeout", "not_attempted"]
    assert result["status"] == "failed"


def test_test_email_validation():
    assert svc.validate_test_emails("A@x.com, b@y.com\nA@x.com") == ["a@x.com", "b@y.com"]
    with pytest.raises(ApiValidationError):
        svc.validate_test_emails("not-an-email")
    with pytest.raises(ApiValidationError):
        svc.validate_test_emails([f"u{i}@x.com" for i in range(11)])


# --------------------------------------------------------------------------- operator UI

def test_ui_create_issue_add_edit_block_and_test_send(operator, client_record, sent):
    response = operator.post(reverse("mailing:newsletter_list"),
                             {"key": "weekly-304", "subject": "Weekly #304", "operator_client_id": client_record.id})
    issue = NewsletterIssue.objects.get(key="weekly-304")
    assert response.status_code == 302
    url = reverse("mailing:newsletter_block_new", args=[issue.pk]) + "?type=course_promo"
    assert operator.get(url).status_code == 200
    operator.post(url, {"block_type": "course_promo", "title": "ML Zoomcamp", "cta_text": "Register",
                        "cta_url": "https://courses.datatalks.club/ml", "operator_client_id": client_record.id})
    block = issue.blocks.get()
    assert block.data["title"] == "ML Zoomcamp" and block.updated_by == "valeriia@datatalks.club"
    edit = reverse("mailing:newsletter_block_edit", args=[issue.pk, block.pk])
    operator.post(edit, {"title": "ML Zoomcamp 2027", "cta_text": "Register", "cta_url": "https://x.dev",
                         "operator_client_id": client_record.id})
    block.refresh_from_db()
    assert block.data["title"] == "ML Zoomcamp 2027"
    page = operator.get(reverse("mailing:newsletter_detail", args=[issue.pk]))
    assert page.status_code == 200 and b"ML Zoomcamp 2027" in page.content and b"alexey@datatalks.club" in page.content
    response = operator.post(reverse("mailing:newsletter_test_send", args=[issue.pk]),
                             {"preset": ["alexey@datatalks.club"], "emails": "sponsor@acme.dev",
                              "operator_client_id": client_record.id}, follow=True)
    assert [call["email"] for call in sent] == ["alexey@datatalks.club", "sponsor@acme.dev"]
    assert b"accepted by the email provider" in response.content
    assert NewsletterTestSend.objects.filter(actor__email="valeriia@datatalks.club").count() == 2


def test_ui_invalid_block_shows_errors_and_saves_nothing(operator, client_record, issue):
    url = reverse("mailing:newsletter_block_new", args=[issue.pk])
    response = operator.post(url, {"block_type": "sponsor", "title": "", "cta_url": "ftp://x",
                                   "operator_client_id": client_record.id})
    assert response.status_code == 200
    assert b"Block not saved" in response.content
    assert issue.blocks.count() == 2


def test_ui_test_send_failure_is_visible(operator, client_record, issue, monkeypatch):
    def failing(source, email, rendered):
        raise ClientError({"Error": {"Code": "Throttling", "Message": "Maximum sending rate exceeded."}}, "SendEmail")

    monkeypatch.setattr(svc, "_deliver", failing)
    response = operator.post(reverse("mailing:newsletter_test_send", args=[issue.pk]),
                             {"emails": "alexey@datatalks.club", "operator_client_id": client_record.id}, follow=True)
    assert b"FAILED" in response.content and b"Throttling" in response.content


def test_ui_requires_staff_and_scopes_to_active_client(client_record, other_client, issue):
    anonymous = HttpClient()
    assert anonymous.get(reverse("mailing:newsletter_list")).status_code == 302
    user = get_user_model().objects.create_user("x", "x@datatalks.club", is_staff=True)
    http = HttpClient()
    http.force_login(user)
    session = http.session
    session[ACTIVE_CLIENT_SESSION_KEY] = other_client.id
    session.save()
    assert http.get(reverse("mailing:newsletter_detail", args=[issue.pk])).status_code == 404


def test_ui_preview_page_is_sandboxed(operator, issue):
    response = operator.get(reverse("mailing:newsletter_preview", args=[issue.pk]))
    assert response.status_code == 200 and "sandbox" in response["Content-Security-Policy"]


# --------------------------------------------------------------------------- API (same abilities as UI)

def _json(response):
    return json.loads(response.content)


def test_api_full_flow(api, client_record, sent):
    http = HttpClient()
    types = _json(http.get("/api/newsletter-block-types", **api))
    assert {item["type"] for item in types["block_types"]} >= {"sponsor", "events_list", "intro", "html"}
    response = http.put("/api/newsletter-issues/weekly-305", json.dumps({"subject": "Weekly #305"}),
                        content_type="application/json", **api)
    assert response.status_code == 201
    response = http.post("/api/newsletter-issues/weekly-305/blocks",
                         json.dumps({"type": "events_list", "data": {"events": [
                             "Mon, Nov 16 · 17:00 CET · Webinar | LLM evals | https://lu.ma/x | with Jane"]}}),
                         content_type="application/json", **api)
    assert response.status_code == 201
    block_id = _json(response)["block"]["id"]
    response = http.put(f"/api/newsletter-issues/weekly-305/blocks/{block_id}",
                        json.dumps({"data": {"events": "Tue | Workshop | https://lu.ma/y"}}),
                        content_type="application/json", **api)
    assert _json(response)["block"]["data"]["events"] == "Tue | Workshop | https://lu.ma/y"
    assert _json(response)["block"]["updated_by"] == "api:dtc-newsletter"
    preview = _json(http.post("/api/newsletter-issues/weekly-305/preview", "{}",
                              content_type="application/json", **api))["preview"]
    assert "Upcoming Events" in preview["html_body"]
    response = http.post("/api/newsletter-issues/weekly-305/test-send",
                         json.dumps({"emails": ["alexey@datatalks.club"], "block_id": block_id}),
                         content_type="application/json", **api)
    assert response.status_code == 200 and _json(response)["status"] == "sent"
    assert sent[0]["subject"].startswith("[TEST] Weekly #305")
    assert _json(http.get("/api/newsletter-issues", **api))["issues"][0]["key"] == "weekly-305"
    response = http.delete(f"/api/newsletter-issues/weekly-305/blocks/{block_id}", **api)
    assert response.status_code == 200 and _json(response)["issue"]["blocks"] == []


def test_api_validation_and_failures_are_explicit(api, issue, monkeypatch):
    http = HttpClient()
    response = http.post("/api/newsletter-issues/weekly-303/blocks",
                         json.dumps({"type": "sponsor", "data": {"title": "x", "cta_url": "nope"}}),
                         content_type="application/json", **api)
    assert response.status_code == 400
    assert _json(response)["error"]["fields"]["data.cta_url"] == "invalid_url"
    monkeypatch.setattr(svc, "_deliver", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    response = http.post("/api/newsletter-issues/weekly-303/test-send", json.dumps({"emails": ["a@b.co"]}),
                         content_type="application/json", **api)
    assert response.status_code == 502 and _json(response)["results"][0]["error"] == "RuntimeError: boom"
    assert http.get("/api/newsletter-issues/missing", **api).status_code == 404


def test_api_is_scoped_to_key_client(other_client, issue):
    _, raw = create_client_api_key(client=other_client, name="other")
    response = HttpClient().get("/api/newsletter-issues/weekly-303", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 404
    assert HttpClient().get("/api/newsletter-issues/weekly-303").status_code == 401
    assert NewsletterBlock.objects.filter(issue=issue).count() == 2
