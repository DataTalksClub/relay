import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    BlockedSender,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    OperatorAudit,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.transactional_retry import (
    MAX_BULK_RETRY,
    MessageNotRetryable,
    RetrySuppressed,
    bulk_retry_transactional_messages,
    collect_retry_candidates,
    retry_transactional_message,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def client_record(org):
    return Client.objects.create(
        organization=org,
        name="Hetzner Maintenance",
        slug="hetzner-maintenance",
        default_sender_id="alerts",
        sender_emails=[{"id": "alerts", "email": "Alert <alerts@dtcdev.click>"}],
    )


@pytest.fixture
def contact():
    return Contact.objects.create(email="dev@dtcdev.click")


@pytest.fixture
def template(client_record):
    return EmailTemplate.objects.create(
        client=client_record,
        key="maintenance-notice",
        name="Maintenance notice",
        subject="Maintenance",
        html_body="<p>hi</p>",
        text_body="hi",
    )


@pytest.fixture
def failed_message(client_record, contact, template):
    return TransactionalMessage.objects.create(
        client=client_record,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="alerts",
        from_email="Alert <alerts@dtcdev.click>",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.FAILED,
        idempotency_key="msg-8017",
        subject="Maintenance",
        html_body="<p>hi</p>",
        text_body="hi",
        last_error="MessageRejected: bad route",
    )


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("op", "op@example.com", "pw", is_staff=True)


def test_retry_failed_message_requeues_same_row_and_audits(
    operator, failed_message, monkeypatch, django_capture_on_commit_callbacks
):
    enqueued = []
    monkeypatch.setattr(
        "mailing.enqueue.enqueue_transactional_email",
        enqueued.append,
    )
    before_key = failed_message.idempotency_key
    with django_capture_on_commit_callbacks(execute=True):
        result = retry_transactional_message(failed_message.pk, actor=operator, reason="route fixed")
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.QUEUED
    assert failed_message.idempotency_key == before_key
    assert failed_message.last_error == ""
    assert failed_message.metadata["retry_count"] == 1
    assert result.enqueued is True
    assert len(enqueued) == 1
    assert enqueued[0]["transactional_message_id"] == failed_message.id
    assert EmailEvent.objects.filter(
        transactional_message=failed_message, event_type=EmailEventType.QUEUED, metadata__retry=True
    ).exists()
    audit = OperatorAudit.objects.get(action="transactional_message.retry", target_id=failed_message.id)
    assert audit.actor_id == operator.id
    assert audit.metadata["reason"] == "route fixed"


def test_retry_reresolves_sender(operator, failed_message, client_record):
    client_record.sender_emails = [{"id": "alerts", "email": "Alert <fixed@example.com>"}]
    client_record.save(update_fields=["sender_emails"])
    retry_transactional_message(failed_message.pk, actor=operator, reason="route fixed")
    failed_message.refresh_from_db()
    assert failed_message.from_email == "Alert <fixed@example.com>"


def test_retry_refuses_non_failed_without_force(operator, failed_message):
    failed_message.status = TransactionalMessageStatus.SENT
    failed_message.save(update_fields=["status"])
    with pytest.raises(MessageNotRetryable):
        retry_transactional_message(failed_message.pk, actor=operator, reason="x")


def test_retry_force_requires_reason(operator, failed_message):
    failed_message.status = TransactionalMessageStatus.SENT
    failed_message.save(update_fields=["status"])
    with pytest.raises(Exception):
        retry_transactional_message(failed_message.pk, actor=operator, reason="", force=True)


def test_retry_force_with_reason_allows_sent(operator, failed_message, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    failed_message.status = TransactionalMessageStatus.SENT
    failed_message.save(update_fields=["status"])
    result = retry_transactional_message(
        failed_message.pk, actor=operator, reason="operator override", force=True
    )
    assert result.status == TransactionalMessageStatus.QUEUED


def test_retry_never_retries_suppressed_even_with_force(operator, failed_message, contact):
    contact.hard_bounced_at = timezone.now()
    contact.save(update_fields=["hard_bounced_at"])
    with pytest.raises(RetrySuppressed):
        retry_transactional_message(failed_message.pk, actor=operator, reason="x", force=True)


def test_retry_never_retries_blocked_recipient(operator, failed_message):
    BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="dev@dtcdev.click")
    with pytest.raises(RetrySuppressed):
        retry_transactional_message(failed_message.pk, actor=operator, reason="x", force=True)


def test_bulk_collect_filters_by_client_and_error(failed_message, client_record, org, contact, template):
    other_client = Client.objects.create(organization=org, name="Other", slug="other")
    collect_all = collect_retry_candidates()
    assert collect_all.count() >= 1
    by_client = collect_retry_candidates(client_slug="hetzner-maintenance")
    assert failed_message in list(by_client)
    by_error = collect_retry_candidates(error_contains="bad route")
    assert failed_message in list(by_error)
    by_other = collect_retry_candidates(client_slug="other")
    assert failed_message not in list(by_other)
    assert other_client.slug == "other"


def test_bulk_dry_run_lists_without_enqueue(failed_message, monkeypatch):
    calls = []
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", calls.append)
    qs = collect_retry_candidates()
    result = bulk_retry_transactional_messages(qs, reason="x", dry_run=True)
    assert result.dry_run is True
    assert any(row["id"] == failed_message.id for row in result.retried)
    assert calls == []


def test_bulk_skips_suppressed_rows(failed_message, contact, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    contact.complained_at = timezone.now()
    contact.save(update_fields=["complained_at"])
    qs = collect_retry_candidates()
    result = bulk_retry_transactional_messages(qs, reason="x")
    assert result.retried == []
    assert any(row["code"] == "recipient_suppressed" for row in result.skipped)


def test_cli_single_dry_run_lists(failed_message):
    out = io.StringIO()
    call_command("retry_transactional", str(failed_message.id), "--dry-run", stdout=out)
    assert f"would retry message {failed_message.id}" in out.getvalue()
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.FAILED


def test_cli_single_retries(failed_message, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    out = io.StringIO()
    call_command("retry_transactional", str(failed_message.id), "--reason", "fix", stdout=out)
    assert "retried message" in out.getvalue()
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.QUEUED


def test_cli_bulk_dry_run(failed_message):
    out = io.StringIO()
    call_command("retry_transactional", "--dry-run", stdout=out)
    assert "would be re-queued" in out.getvalue()


def test_cli_bulk_limit_guard(failed_message):
    with pytest.raises(CommandError):
        call_command("retry_transactional", "--limit", str(MAX_BULK_RETRY + 1), "--dry-run")


def test_cli_force_requires_reason(failed_message):
    with pytest.raises(CommandError):
        call_command("retry_transactional", str(failed_message.id), "--force")


def test_admin_api_single_retry(operator, failed_message, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    _, token = create_admin_api_key(user=operator, name="api")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = http.post(
        f"/api/admin/transactional-messages/{failed_message.id}/retry",
        data=json.dumps({"reason": "route fixed"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.QUEUED
    assert OperatorAudit.objects.filter(
        action="transactional_message.retry", target_id=failed_message.id
    ).exists()


def test_admin_api_single_refuses_sent(operator, failed_message, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    failed_message.status = TransactionalMessageStatus.SENT
    failed_message.save(update_fields=["status"])
    _, token = create_admin_api_key(user=operator, name="api2")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = http.post(
        f"/api/admin/transactional-messages/{failed_message.id}/retry",
        data=json.dumps({"reason": "x"}),
        content_type="application/json",
    )
    assert response.status_code == 409


def test_admin_api_bulk_requires_confirm(operator, failed_message):
    _, token = create_admin_api_key(user=operator, name="api3")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = http.post(
        "/api/admin/transactional-messages/retry",
        data=json.dumps({"reason": "x"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_required"


def test_admin_api_bulk_dry_run(operator, failed_message):
    _, token = create_admin_api_key(user=operator, name="api4")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = http.post(
        "/api/admin/transactional-messages/retry",
        data=json.dumps({"reason": "x", "dry_run": True}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["retried_count"] >= 1
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.FAILED


def test_ui_retry_action_only_failed(operator, client_record, failed_message, monkeypatch):
    monkeypatch.setattr("mailing.enqueue.enqueue_transactional_email", lambda payload: None)
    django_client = HttpClient()
    django_client.force_login(operator)
    session = django_client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()
    url = reverse("mailing:transactional_message_retry", args=[failed_message.id])
    response = django_client.post(url, {"reason": "ui retry"})
    assert response.status_code == 302
    failed_message.refresh_from_db()
    assert failed_message.status == TransactionalMessageStatus.QUEUED


def test_bulk_retry_1000_uses_one_message_update(client_record, contact, template, django_capture_on_commit_callbacks):
    from django.db import connection  # noqa: PLC0415
    from django.test.utils import CaptureQueriesContext  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    TransactionalMessage.objects.bulk_create([
        TransactionalMessage(client=client_record, contact=contact, template=template,
                             email=contact.email, from_email_id="alerts", template_key=template.key,
                             status=TransactionalMessageStatus.FAILED, idempotency_key=f"bulk-{i}")
        for i in range(1000)
    ])
    with CaptureQueriesContext(connection) as queries, django_capture_on_commit_callbacks(execute=True):
        result = bulk_retry_transactional_messages(collect_retry_candidates(client=client_record), reason="route repaired")
    assert len(result.retried) == 1000
    updates = [query for query in queries if query["sql"].startswith('UPDATE "transactional_messages"')]
    assert len(updates) == 1
    assert len(queries) < 80
    assert TransactionalMessage.objects.filter(status="queued").count() == 1000
    assert EmailEvent.objects.filter(event_type=EmailEventType.QUEUED).count() == 1000
    assert OperatorAudit.objects.filter(action="transactional_message.retry").count() == 1000
    assert DBTaskResult.objects.filter(task_path="mailing.tasks.send_transactional_email").count() == 1000


def test_bulk_retry_preserves_distinct_metadata_and_counts(failed_message, client_record, contact, template):
    other = TransactionalMessage.objects.create(
        client=client_record, contact=contact, template=template, template_key=template.key,
        email=contact.email, status="failed", idempotency_key="distinct-retry",
        metadata={"retry_count": "invalid", "keep": "second"},
    )
    failed_message.metadata = {"retry_count": 7, "keep": "first"}
    failed_message.save()
    result = bulk_retry_transactional_messages(collect_retry_candidates(client=client_record), reason="fixed")
    assert len(result.retried) == 2
    failed_message.refresh_from_db()
    other.refresh_from_db()
    assert failed_message.metadata == {"retry_count": 8, "keep": "first", "last_retry_reason": "fixed"}
    assert other.metadata == {"retry_count": 1, "keep": "second", "last_retry_reason": "fixed"}
