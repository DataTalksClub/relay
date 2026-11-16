from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, Client, Organization
from mailing.services.operator_ui import campaign_queryset

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    org = Organization.objects.create(name="List test", slug="list-test")
    client = Client.objects.create(organization=org, name="List client", slug="list-client")
    audience = Audience.objects.create(organization=org, name="List audience", slug="list-audience")
    return {"client": client, "audience": audience}


def test_send_date_sort_precedes_import_time_and_is_stable(scope):
    now = timezone.now()
    newest = Campaign.objects.create(**scope, subject="Recent send", status="sent", sent_at=now)
    old_import = Campaign.objects.create(**scope, subject="Old imported send", status="sent", sent_at=now-timedelta(days=365))
    scheduled = Campaign.objects.create(**scope, subject="Scheduled", status="scheduled", scheduled_at=now+timedelta(days=1))
    draft = Campaign.objects.create(**scope, subject="Draft")
    tie = Campaign.objects.create(**scope, subject="Same send date", status="sent", sent_at=now)
    Campaign.objects.filter(pk=draft.pk).update(created_at=now-timedelta(days=1))
    other = Client.objects.create(organization=scope["client"].organization, name="Other", slug="other")
    Campaign.objects.create(client=other, audience=scope["audience"], subject="Other tenant")
    Campaign.objects.create(**scope, subject="Merged source", merged_into=newest, sent_at=now+timedelta(days=2))
    assert list(campaign_queryset(scope["client"]).values_list("pk", flat=True)) == [
        scheduled.pk, tie.pk, newest.pk, draft.pk, old_import.pk,
    ]


def test_list_distinguishes_missing_import_totals_from_real_zero(client, scope):
    operator = get_user_model().objects.create_user("list-operator", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = scope["client"].pk
    session.save()
    now = timezone.now()
    Campaign.objects.create(**scope, subject="Historical campaign", status="sent", sent_at=now-timedelta(days=1),
                            metadata={"historical_import": True, "provider": "mailchimp"})
    Campaign.objects.create(**scope, subject="Native campaign", status="sent", sent_at=now)
    response = client.get(reverse("mailing:campaign_list"))
    assert response.status_code == 200
    html = response.content.decode()
    assert html.index("Native campaign") < html.index("Historical campaign")
    assert "Mailchimp history" in html
    assert "Send totals not imported" in html
    assert "Not imported" in html
    assert html.count("currently sent") == 1
    assert "0 bounces / 0 complaints" not in html
    assert "View results" not in html


def test_sort_happens_before_pagination(client, scope):
    operator = get_user_model().objects.create_user("page-operator", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = scope["client"].pk
    session.save()
    now = timezone.now()
    recent = Campaign.objects.create(**scope, subject="Newest actual send", sent_at=now, status="sent")
    for i in range(26):
        Campaign.objects.create(**scope, subject=f"Old import {i}", sent_at=now-timedelta(days=i+1), status="sent")
    response = client.get(reverse("mailing:campaign_list"))
    assert response.context["campaigns"].object_list[0].pk == recent.pk
    assert response.context["campaigns"].paginator.count == 27
