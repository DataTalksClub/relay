"""The generic task list endpoints: pagination, filters, and errors.

``GET /api/tasks`` used to be "the last hundred jobs, nothing else", which is
fine until a client actually needs to see the 101st. These cover the cursor
contract, the filters, and the error envelope, plus the shape a caller that
sends no query at all is entitled to rely on.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from jobs.models import Job, JobStatus
from jobs.services import submit_job
from mailing.models import Client, Organization
from mailing.services.api_docs import API_DOC_PATHS, build_openapi_spec
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def relay_client():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(organization=organization, name="Courses", slug="dtc-courses")


@pytest.fixture
def auth(relay_client):
    _, raw_key = create_client_api_key(client=relay_client, name="test")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def submit(client, key, *, status=None):
    job, _ = submit_job(
        {"type": "system.echo", "idempotency_key": key, "params": {}},
        client,
    )
    if status is not None:
        Job.objects.filter(pk=job.pk).update(status=status)
    return job


def make_job(relay_client, key, *, task_type="system.echo", status=None):
    """A job row written straight to the table.

    Submissions go through the queue and the per-client limits; a test about
    *reading* the list has no reason to involve either.
    """
    job = Job.objects.create(
        client=relay_client,
        task_type=task_type,
        idempotency_key=key,
        request_hash="hash",
    )
    if status is not None:
        Job.objects.filter(pk=job.pk).update(status=status)
    return job


def page(client, auth, **query):
    return client.get("/api/tasks", data=query, **auth).json()


def test_a_no_query_call_returns_the_same_tasks_it_always_did(client, auth, relay_client):
    """Backward compatibility: the existing caller must not have to change."""
    for index in range(3):
        submit(relay_client, f"existing-{index}")

    body = page(client, auth)

    assert [item["idempotency_key"] for item in body["tasks"]] == [
        "existing-2",
        "existing-1",
        "existing-0",
    ]
    assert body["next_cursor"] is None


def test_the_default_page_is_still_capped_at_a_hundred(client, auth, relay_client):
    """The cap is the point: it is what makes paging necessary, and the cursor
    is what stops the remainder from being silently invisible."""
    for index in range(105):
        make_job(relay_client, f"bulk-{index}")

    first = page(client, auth)
    assert len(first["tasks"]) == 100
    assert first["next_cursor"] is not None

    second = page(client, auth, cursor=first["next_cursor"])
    assert len(second["tasks"]) == 5
    assert second["next_cursor"] is None
    assert {item["id"] for item in first["tasks"]}.isdisjoint(
        {item["id"] for item in second["tasks"]}
    )


def test_page_size_can_be_lowered(client, auth, relay_client):
    for index in range(5):
        make_job(relay_client, f"sized-{index}")

    first = page(client, auth, limit=2)
    second = page(client, auth, limit=2, cursor=first["next_cursor"])
    third = page(client, auth, limit=2, cursor=second["next_cursor"])

    assert len(first["tasks"]) == 2
    assert len(second["tasks"]) == 2
    assert len(third["tasks"]) == 1
    assert third["next_cursor"] is None
    assert (
        {item["id"] for item in first["tasks"]}
        | {item["id"] for item in second["tasks"]}
        | {item["id"] for item in third["tasks"]}
        == {str(job.pk) for job in Job.objects.all()}
    )


def test_a_limit_outside_the_range_is_a_validation_error(client, auth):
    response = client.get("/api/tasks", data={"limit": 101}, **auth)

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "validation_error", "fields": {"limit": "must_be_between_1_and_100"}}
    }


def test_a_non_integer_limit_is_a_validation_error(client, auth):
    response = client.get("/api/tasks", data={"limit": "ten"}, **auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["limit"] == "must_be_integer"


def test_an_unparseable_cursor_is_a_validation_error(client, auth):
    response = client.get("/api/tasks", data={"cursor": "nonsense"}, **auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["cursor"] == "invalid"


def test_jobs_are_still_scoped_to_the_authenticating_client(client, auth, relay_client):
    """The filters narrow within a client; they never widen past it."""
    other_org = Organization.objects.create(name="Other", slug="other")
    other_client = Client.objects.create(organization=other_org, name="Other", slug="other")
    make_job(other_client, "not-yours")

    assert page(client, auth)["tasks"] == []


def test_status_filter_narrows_the_page(client, auth, relay_client):
    make_job(relay_client, "queued-one")
    make_job(relay_client, "failed-one", status=JobStatus.FAILED)

    body = page(client, auth, status=JobStatus.FAILED)

    assert [item["idempotency_key"] for item in body["tasks"]] == ["failed-one"]
    assert body["next_cursor"] is None


def test_task_type_filter_narrows_the_page(client, auth, relay_client):
    make_job(relay_client, "echo-one")
    make_job(relay_client, "webhook-one", task_type="webhook")

    body = page(client, auth, task_type="webhook")

    assert [item["idempotency_key"] for item in body["tasks"]] == ["webhook-one"]


def test_filters_combine(client, auth, relay_client):
    make_job(relay_client, "queued-echo", status=JobStatus.FAILED)
    make_job(relay_client, "queued-webhook", task_type="webhook")
    make_job(relay_client, "done-echo", status=JobStatus.SUCCEEDED)

    body = page(client, auth, status=JobStatus.SUCCEEDED, task_type="system.echo")

    assert [item["idempotency_key"] for item in body["tasks"]] == ["done-echo"]


def test_an_unknown_status_is_a_validation_error(client, auth):
    response = client.get("/api/tasks", data={"status": "half-done"}, **auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["status"] == "unknown"


def test_an_unknown_task_type_is_a_validation_error(client, auth):
    response = client.get("/api/tasks", data={"task_type": "teleport"}, **auth)

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["task_type"] == "unknown"


def test_paging_survives_a_submission_that_lands_on_the_page_boundary(client, auth, relay_client):
    """An offset would skip or repeat here; a cursor cannot.

    Jobs sharing the boundary row's timestamp are ordered after it by id, so a
    consumer walking the cursor never sees one twice and never misses one.
    """
    shared = timezone.now()
    for index in range(4):
        job = make_job(relay_client, f"tied-{index}")
        Job.objects.filter(pk=job.pk).update(created_at=shared)

    seen = []
    cursor = ""
    while True:
        query = {"limit": 1}
        if cursor:
            query["cursor"] = cursor
        body = page(client, auth, **query)
        seen.extend(item["idempotency_key"] for item in body["tasks"])
        cursor = body["next_cursor"] or ""
        if not cursor:
            break

    assert sorted(seen) == ["tied-0", "tied-1", "tied-2", "tied-3"]


TASK_PATHS = {
    "/api/tasks",
    "/api/tasks/{task_id}",
    "/api/tasks/{task_id}/complete",
    "/api/tasks/{task_id}/fail",
}


def test_the_generic_api_reaches_the_generated_specification():
    """Endpoints a client cannot discover are endpoints it cannot use.

    These live in the same OpenAPI document and the same in-app browser as the
    mail API, so discovering them costs a client nothing new.
    """
    spec = build_openapi_spec()

    assert TASK_PATHS <= set(spec["paths"])
    assert "/api/schedules" in spec["paths"]
    assert "/api/schedules/{schedule_id}" in spec["paths"]
    assert set(TASK_PATHS) <= set(API_DOC_PATHS.values())


def test_the_generic_api_is_listed_in_the_in_app_reference(client, auth):
    """Each path has to be findable in the searchable reference.

    Searched one at a time because the reference pages: a path that only shows
    up on page 4 is a path a client never sees.
    """
    client.force_login(get_user_model().objects.create_user(username="docs", is_staff=True))

    for path in sorted(TASK_PATHS | {"/api/schedules", "/api/schedules/{schedule_id}"}):
        response = client.get(reverse("mailing:api_docs_reference"), data={"q": path})

        assert response.status_code == 200
        assert path in response.content.decode()


def test_the_task_list_response_documents_its_page_cursor(client, auth):
    operation = build_openapi_spec()["paths"]["/api/tasks"]["get"]

    parameters = {parameter["name"] for parameter in operation["parameters"]}
    assert {"status", "task_type", "limit", "cursor"} == parameters

