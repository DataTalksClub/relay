from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    Subscription,
    TransactionalMessage,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope(client):
    organization = Organization.objects.create(name="Example", slug="ux-example")
    integration = Client.objects.create(organization=organization, name="Newsletter", slug="ux-news")
    other = Client.objects.create(organization=organization, name="Courses", slug="ux-courses")
    audience = Audience.objects.create(organization=organization, name="Readers", slug="ux-readers")
    operator = get_user_model().objects.create_user("ux-operator", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.id
    session.save()
    return integration, other, audience


def message(integration, email, *, status="sent", delivered=False):
    contact = Contact.objects.create(email=email)
    template, _ = EmailTemplate.objects.get_or_create(client=integration, key="welcome", defaults={"name": "Welcome"})
    return TransactionalMessage.objects.create(
        client=integration, contact=contact, email=email, template=template,
        template_key="welcome", subject="Your course registration", status=status,
        delivered_at=timezone.now() if delivered else None,
    )


def test_activity_search_and_delivery_filter_keep_client_scope(client, scope):
    integration, other, _ = scope
    delivered = message(integration, "alex@example.com", delivered=True)
    message(integration, "bailey@example.com", status="failed")
    message(other, "alex@other.example", delivered=True)
    response = client.get(reverse("mailing:email_activity"), {"q": "alex", "status": "delivered"})
    assert [row["message"].id for row in response.context["message_rows"]] == [delivered.id]
    assert response.context["message_rows"][0]["badge"].label == "Delivered"


def test_activity_failed_filter_excludes_other_states_and_clients(client, scope):
    integration, other, _ = scope
    failed = message(integration, "failure@example.com", status="failed")
    message(integration, "sent@example.com")
    message(other, "otherfailure@example.com", status="failed")
    response = client.get(reverse("mailing:email_activity"), {"status": "failed"})
    assert [row["message"].id for row in response.context["message_rows"]] == [failed.id]


def test_switching_client_from_campaign_returns_valid_list(client, scope):
    integration, other, audience = scope
    campaign = Campaign.objects.create(client=integration, audience=audience, subject="Announcement")
    response = client.post(reverse("mailing:client_select"), {
        "client_id": other.id, "next": reverse("mailing:campaign_detail", args=[campaign.id]),
    }, follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [(reverse("mailing:campaign_list"), 302)]
    assert client.session[ACTIVE_CLIENT_SESSION_KEY] == other.id


def test_switching_client_rejects_external_return_url(client, scope):
    _, other, _ = scope
    response = client.post(reverse("mailing:client_select"), {"client_id": other.id, "next": "https://outside.example/"})
    assert response.url == reverse("mailing:dashboard")


def test_stale_tab_cannot_create_campaign_under_changed_client(client, scope):
    integration, other, audience = scope
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = other.id
    session.save()
    response = client.post(reverse("mailing:campaign_create"), {
        "operator_client_id": integration.id, "audience": audience.id,
        "subject": "Wrong scope", "html_body": "<p>Hello</p>", "text_body": "Hello",
    }, follow=True)
    assert not Campaign.objects.filter(subject="Wrong scope").exists()
    assert "changed in another tab" in response.content.decode()


def test_contacts_load_scoped_records_without_filters(client, scope):
    integration, other, audience = scope
    contact = Contact.objects.create(email="visible@example.com")
    other_contact = Contact.objects.create(email="hidden@example.com")
    Subscription.objects.create(contact=contact, client=integration, audience=audience, status="subscribed")
    Subscription.objects.create(contact=other_contact, client=other, audience=audience, status="subscribed")
    response = client.get(reverse("mailing:contact_search"))
    assert [row.contact.id for row in response.context["contact_rows"]] == [contact.id]


class FormIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.labels.append(attrs["for"])


def test_contact_forms_have_unique_labelled_audience_controls_and_preserve_return(client, scope):
    integration, _, audience = scope
    contact = Contact.objects.create(email="labels@example.com")
    Subscription.objects.create(contact=contact, client=integration, audience=audience, status="subscribed")
    return_url = reverse("mailing:contact_search") + "?q=labels&page=2"
    response = client.get(reverse("mailing:contact_detail", args=[contact.email]), {"return": return_url})
    parser = FormIds()
    parser.feed(response.content.decode())
    assert len(parser.ids) == len(set(parser.ids))
    assert "subscription_audience" in parser.labels
    assert "tag_add_audience" in parser.labels
    assert response.context["return_url"] == return_url


def test_contact_return_rejects_external_url(client, scope):
    contact = Contact.objects.create(email="back@example.com")
    response = client.get(reverse("mailing:contact_detail", args=[contact.email]), {"return": "https://outside.example/contacts/"})
    assert response.context["return_url"] == reverse("mailing:contact_search")


def test_changed_recipient_count_requires_new_confirmation(client, scope, monkeypatch):
    integration, _, audience = scope
    campaign = Campaign.objects.create(client=integration, audience=audience, subject="Announcement", text_body="Hello")
    called = []
    monkeypatch.setattr("mailing.views.queue_campaign", lambda campaign: called.append(campaign.id))
    response = client.post(reverse("mailing:campaign_queue", args=[campaign.id]), {
        "confirm": "1", "reviewed_recipient_count": "999",
    })
    assert not called
    assert response.url.endswith("?confirm_send=1")
    campaign.refresh_from_db()
    assert campaign.status == "draft"
