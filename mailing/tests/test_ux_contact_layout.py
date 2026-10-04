import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Client, Contact, Organization, Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def scoped_contact(client):
    operator = get_user_model().objects.create_user("ux-layout", is_staff=True)
    organization = Organization.objects.create(name="Community", slug="ux-community")
    integration = Client.objects.create(organization=organization, name="Announcements", slug="ux-announcements")
    audience = Audience.objects.create(organization=organization, name="Members", slug="ux-members")
    contact = Contact.objects.create(email="learner@example.com", normalized_email="learner@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=integration, status="subscribed")
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.pk
    session.save()
    return contact, audience


def test_audience_starts_with_members_and_discloses_reports(client, scoped_contact):
    _, audience = scoped_contact
    response = client.get(reverse("mailing:audience_detail", args=[audience.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="audience-members"' in html
    assert 'id="audience-health"' not in html
    assert "section=segments" in html
    assert 'id="audience-segments"' not in html
    assert 'aria-current="page"' in html
    assert 'name="client"' not in html
    assert "Campaign eligibility" in html
    assert 'data-label="Campaign eligibility"' in html
    assert "Announcements" in html


def test_contact_prioritizes_activity_and_isolates_global_changes(client, scoped_contact):
    contact, _ = scoped_contact
    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))
    assert response.status_code == 200
    html = response.content.decode()
    assert html.index('id="sendability"') < html.index('id="recent-activity"') < html.index('id="manage-contact"')
    assert '<details class="detail-section secondary-section" id="global-contact-state">' in html
    assert "across all clients and audiences" in html
    assert html.index("Update subscription") < html.index("Review global changes")
