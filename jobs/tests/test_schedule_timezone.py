"""Per-schedule timezone.

A cron expression is wall-clock arithmetic in a place. Without a zone, "0 9 * * *"
is read as 09:00 UTC and a schedule for an audience in Berlin fires at 10:00 or
11:00 local, which is wrong for half the year and never looks broken.
"""

from datetime import datetime
from datetime import timezone as utc_timezone
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from jobs.models import Schedule
from jobs.operator_actions import change_schedule_state
from jobs.scheduling import (
    next_occurrence,
    run_due_schedules,
    upsert_schedule,
    validate_timezone,
)
from mailing.models import Client, Organization
from mailing.services.api_errors import ApiValidationError
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db(transaction=True)

BERLIN = "Europe/Berlin"


@pytest.fixture
def relay_client():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(
        organization=organization,
        name="Courses",
        slug="dtc-courses",
        relay_webhook_signing_secret="webhook-secret",
        relay_webhook_allowed_origins=["https://client.example.com"],
    )


@pytest.fixture
def auth(relay_client):
    _, raw_key = create_client_api_key(client=relay_client, name="test")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def test_only_real_iana_names_are_accepted():
    for name in ("UTC", "Europe/Berlin", "America/New_York", "Etc/GMT+2"):
        assert validate_timezone(name) == name

    for value in ("+02:00", "UTC+2", "utc", "Berlin", "not-a-zone", ""):
        with pytest.raises(ApiValidationError) as exc:
            validate_timezone(value)
        assert exc.value.errors == {"timezone": "unknown"} or exc.value.errors == {
            "timezone": "required"
        }


def test_nine_in_berlin_is_eight_utc_in_winter():
    base = datetime(2026, 1, 15, 12, 0, tzinfo=utc_timezone.utc)

    value = next_occurrence("0 9 * * *", base, BERLIN)

    assert value == datetime(2026, 1, 16, 8, 0, tzinfo=utc_timezone.utc)
    assert value.astimezone(ZoneInfo(BERLIN)).hour == 9


def test_nine_in_berlin_is_seven_utc_in_summer():
    """The whole reason the field exists: the same wall time, a different UTC one."""
    base = datetime(2026, 7, 15, 12, 0, tzinfo=utc_timezone.utc)

    value = next_occurrence("0 9 * * *", base, BERLIN)

    assert value == datetime(2026, 7, 16, 7, 0, tzinfo=utc_timezone.utc)


def test_the_spring_forward_boundary_does_not_skip_or_double_a_tick():
    """DST starts in Berlin at 02:00 local on 2026-03-29.

    A 09:00 cron is on the far side of the transition, so it must resolve once,
    to the summer offset. An implementation that adds a fixed UTC offset
    instead of naming the zone lands an hour out on this date and nowhere else.
    """
    base = datetime(2026, 3, 28, 12, 0, tzinfo=utc_timezone.utc)

    value = next_occurrence("0 9 * * *", base, BERLIN)

    assert value == datetime(2026, 3, 29, 7, 0, tzinfo=utc_timezone.utc)


def test_the_fall_back_boundary_resolves_to_the_winter_offset():
    """DST ends in Berlin at 03:00 local on 2026-10-25.

    The repeated hour is the other half of the boundary, and it is where an
    implementation that steps in local naive time can land on the second
    occurrence of 09:00 and be an hour out.
    """
    base = datetime(2026, 10, 24, 12, 0, tzinfo=utc_timezone.utc)

    value = next_occurrence("0 9 * * *", base, BERLIN)

    assert value == datetime(2026, 10, 25, 8, 0, tzinfo=utc_timezone.utc)


def test_a_hourly_schedule_runs_every_hour_across_a_transition():
    """A schedule denser than one firing per day must not collapse at the seam.

    Twenty-four hourly ticks starting the day before spring forward covers the
    skipped wall-clock hour. The UTC sequence has to stay contiguous hourly, and
    the local wall clock has to skip 02:00 rather than fire it twice.
    """
    base = datetime(2026, 3, 28, 10, 30, tzinfo=utc_timezone.utc)
    berlin = ZoneInfo(BERLIN)
    ticks = []
    value = base
    for _ in range(24):
        value = next_occurrence("0 * * * *", value, BERLIN)
        ticks.append(value)

    assert ticks == sorted(ticks)
    assert len(set(ticks)) == 24
    assert ticks[0] == datetime(2026, 3, 28, 11, 0, tzinfo=utc_timezone.utc)
    assert ticks[-1] == datetime(2026, 3, 29, 10, 0, tzinfo=utc_timezone.utc)

    local_hours = {(t.astimezone(berlin).day, t.astimezone(berlin).hour) for t in ticks}
    assert (29, 0) in local_hours
    assert (29, 1) in local_hours
    # The hour that does not exist that day.
    assert (29, 2) not in local_hours
    assert (29, 3) in local_hours


def test_a_caller_that_passes_no_zone_gets_the_project_zone(settings):
    """Existing callers must not change behaviour by gaining a parameter."""
    settings.TIME_ZONE = "UTC"
    base = datetime(2026, 5, 1, 12, 0, tzinfo=utc_timezone.utc)

    assert next_occurrence("0 9 * * *", base) == datetime(2026, 5, 2, 9, 0, tzinfo=utc_timezone.utc)
    assert next_occurrence("0 9 * * *") > timezone.now()


def test_an_unknown_zone_raises_value_error_rather_than_a_bare_keyerror():
    with pytest.raises(ValueError):
        next_occurrence("0 9 * * *", datetime(2026, 1, 1, tzinfo=utc_timezone.utc), "Mars/Olympus")


def test_the_schedule_api_round_trips_a_zone(client, auth):
    response = client.post(
        "/api/schedules",
        data={
            "name": "berlin-morning",
            "cron": "0 9 * * *",
            "timezone": BERLIN,
            "type": "system.echo",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )

    assert response.status_code == 201
    assert response.json()["timezone"] == BERLIN
    assert response.json()["next_run_at"].endswith("+00:00")

    listing = client.get("/api/schedules", **auth)
    assert listing.json()["schedules"][0]["timezone"] == BERLIN


def test_the_schedule_api_defaults_a_missing_zone_to_utc(client, auth):
    client.post(
        "/api/schedules",
        data={"name": "no-zone", "cron": "0 9 * * *", "type": "system.echo", "params": {}},
        content_type="application/json",
        **auth,
    )

    assert Schedule.objects.get(name="no-zone").timezone == "UTC"


def test_the_schedule_api_rejects_a_zone_that_is_not_a_real_name(client, auth):
    for value in ("+02:00", "UTC+2", "not-a-zone"):
        response = client.post(
            "/api/schedules",
            data={
                "name": "broken-zone",
                "cron": "0 9 * * *",
                "timezone": value,
                "type": "system.echo",
                "params": {},
            },
            content_type="application/json",
            **auth,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
        assert response.json()["error"]["fields"]["timezone"] == "unknown"


def test_due_schedules_fire_on_the_local_time_correct_occurrence(relay_client):
    """The firing semantics do not change; only the base they resolve against."""
    schedule, _ = upsert_schedule(
        {
            "name": "berlin-morning",
            "cron": "0 9 * * *",
            "timezone": BERLIN,
            "type": "system.echo",
            "params": {},
        },
        relay_client,
    )
    planned = datetime(2026, 1, 16, 8, 0, tzinfo=utc_timezone.utc)
    Schedule.objects.filter(pk=schedule.pk).update(next_run_at=planned)

    fired = run_due_schedules(now=datetime(2026, 1, 16, 8, 0, 30, tzinfo=utc_timezone.utc))

    schedule.refresh_from_db()
    assert len(fired) == 1
    assert schedule.next_run_at == datetime(2026, 1, 17, 8, 0, tzinfo=utc_timezone.utc)


def test_a_due_schedule_in_summer_lands_on_the_summer_utc_time(relay_client):
    schedule, _ = upsert_schedule(
        {
            "name": "berlin-morning",
            "cron": "0 9 * * *",
            "timezone": BERLIN,
            "type": "system.echo",
            "params": {},
        },
        relay_client,
    )
    Schedule.objects.filter(pk=schedule.pk).update(
        next_run_at=datetime(2026, 7, 15, 7, 0, tzinfo=utc_timezone.utc)
    )

    run_due_schedules(now=datetime(2026, 7, 15, 7, 0, 10, tzinfo=utc_timezone.utc))

    schedule.refresh_from_db()
    assert schedule.next_run_at == datetime(2026, 7, 16, 7, 0, tzinfo=utc_timezone.utc)


def test_resuming_a_paused_schedule_recomputes_in_its_own_zone(relay_client):
    schedule, _ = upsert_schedule(
        {
            "name": "berlin-morning",
            "cron": "0 9 * * *",
            "timezone": BERLIN,
            "type": "system.echo",
            "params": {},
            "enabled": False,
        },
        relay_client,
    )

    resumed = change_schedule_state(
        schedule.pk,
        "resume",
        client_id=schedule.client_id,
        revision=schedule.updated_at.isoformat(),
        confirmed=True,
    )

    assert resumed.enabled is True
    assert resumed.next_run_at == next_occurrence(schedule.cron, None, BERLIN)


def test_the_operator_schedule_page_names_the_schedules_own_zone(client, relay_client):
    """The page used to render the project's zone, which hid the field entirely.

    The whole value of a per-schedule zone is that a nine o'clock firing keeps
    meaning nine o'clock where the audience is, and that is only visible if the
    page prints the schedule's zone and not the server's.
    """
    client.force_login(get_user_model().objects.create_user(username="ops", is_staff=True))
    schedule, _ = upsert_schedule(
        {
            "name": "berlin-morning",
            "cron": "0 9 * * *",
            "timezone": BERLIN,
            "type": "system.echo",
            "params": {},
        },
        relay_client,
    )
    other, _ = upsert_schedule(
        {
            "name": "new-york-morning",
            "cron": "0 9 * * *",
            "timezone": "America/New_York",
            "type": "system.echo",
            "params": {},
        },
        relay_client,
    )

    berlin = client.get(f"/jobs/schedules/{schedule.pk}/").content.decode()
    new_york = client.get(f"/jobs/schedules/{other.pk}/").content.decode()

    assert "Europe/Berlin" in berlin
    assert "America/New_York" in new_york
    assert "America/New_York" not in berlin
