from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    Subscription,
    TransactionalMessage,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(client):
    org = Organization.objects.create(name="Community", slug="complete")
    integration = Client.objects.create(organization=org, name="News", slug="news")
    other = Client.objects.create(organization=org, name="Courses", slug="courses")
    audience = Audience.objects.create(organization=org, name="Members", slug="members")
    user = get_user_model().objects.create_user("complete", is_staff=True)
    client.force_login(user)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.id
    session.save()
    return integration, other, audience


def test_back_to_old_campaign_after_scope_change_is_useful(client, workspace):
    integration, other, audience = workspace
    campaign = Campaign.objects.create(client=integration, audience=audience, subject="News")
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = other.id
    session.save()
    response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]), follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [(reverse("mailing:campaign_list"), 302)]
    assert "Showing Courses instead" in response.content.decode()
    assert client.get(reverse("mailing:campaign_detail", args=[999999])).status_code == 404


def make_campaign(integration, audience, contact, **kwargs):
    campaign = Campaign.objects.create(client=integration, audience=audience, subject="Course announcement", text_body="Hello", status="sent")
    return CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email, **kwargs)


def test_activity_search_combines_types_and_preserves_client_scope(client, workspace):
    integration, other, audience = workspace
    contact = Contact.objects.create(email="learner+course@example.com")
    template = EmailTemplate.objects.create(client=integration, key="welcome", name="Welcome")
    message = TransactionalMessage.objects.create(client=integration, contact=contact, template=template, email=contact.email, subject="Welcome", status="sent")
    recipient = make_campaign(integration, audience, contact, status="failed", last_error="hard_bounce")
    make_campaign(other, audience, contact, status="failed")
    response = client.get(reverse("mailing:email_activity"), {"q": "learner"})
    rows = response.context["message_rows"]
    assert {(row["kind"], row["message"].id) for row in rows} == {("transactional", message.id), ("campaign", recipient.id)}
    row = next(row for row in rows if row["kind"] == "campaign")
    assert parse_qs(urlsplit(row["url"]).query)["recipient_q"] == [contact.email]
    result = client.get(row["url"])
    assert [item.id for item in result.context["recipients"]] == [recipient.id]


def test_activity_delivery_and_campaign_filters_are_exact(client, workspace):
    integration, _, audience = workspace
    contact = Contact.objects.create(email="deliver@example.com")
    delivered = make_campaign(integration, audience, contact, status="sent", delivered_at=timezone.now())
    make_campaign(integration, audience, contact, status="failed")
    response = client.get(reverse("mailing:email_activity"), {"status": "delivered", "type": "campaign", "campaign": delivered.campaign_id})
    assert [row["message"].id for row in response.context["message_rows"]] == [delivered.id]
    assert response.context["message_rows"][0]["badge"].label == "Delivered"


def test_global_contact_changes_require_fresh_review(client, workspace):
    integration, _, audience = workspace
    contact = Contact.objects.create(email="review@example.com", hard_bounced_at=timezone.now())
    Subscription.objects.create(client=integration, audience=audience, contact=contact)
    url = reverse("mailing:contact_state_update", args=[contact.email])
    values = {"verified_state": "unchanged", "email_validation_status": "unknown", "email_validation_reason": "", "hard_bounced": "", "global_unsubscribed": "on"}
    response = client.post(url, values)
    assert response.status_code == 200
    assert "All clients and audiences" in response.content.decode()
    assert all(change["label"] for change in response.context["changes"])
    assert {change["label"] for change in response.context["changes"]} == {"Global unsubscribed", "Hard bounced"}
    contact.refresh_from_db()
    assert contact.hard_bounced_at and not contact.global_unsubscribed_at
    token = response.context["review_token"]
    assert token
    contact.email_validation_reason = "Changed by another operator"
    contact.save()
    client.post(url, {"review_token": token})
    contact.refresh_from_db()
    assert contact.hard_bounced_at and not contact.global_unsubscribed_at
    response = client.post(url, values)
    client.post(url, {"review_token": response.context["review_token"]})
    contact.refresh_from_db()
    assert not contact.hard_bounced_at and contact.global_unsubscribed_at


def test_invalid_global_review_and_bad_token_never_update(client, workspace):
    contact = Contact.objects.create(email="invalid-review@example.com")
    url = reverse("mailing:contact_state_update", args=[contact.email])
    response = client.post(url, {"verified_state": "bad", "email_validation_status": "bad"})
    assert response.context["form"].errors
    assert not response.context["review_token"]
    client.post(url, {"review_token": "forged"})
    contact.refresh_from_db()
    assert contact.verified_at is None


def test_removing_one_filter_keeps_other_filters(client, workspace):
    response = client.get(reverse("mailing:contact_search"), {"q": "alex", "verified": "unverified", "page": "2"})
    email_chip = next(chip for chip in response.context["active_filters"] if chip["label"] == "Email")
    params = parse_qs(urlsplit(email_chip["remove_url"]).query)
    assert params == {"verified": ["unverified"]}


def test_invalid_subscription_edit_preserves_values_and_consent(client, workspace):
    integration, _, audience = workspace
    contact = Contact.objects.create(email="edit@example.com")
    Subscription.objects.create(client=integration, audience=audience, contact=contact, status="subscribed")
    response = client.post(reverse("mailing:contact_subscription_update", args=[contact.email]), {"audience": audience.id, "client": integration.id, "status": "invalid", "unsubscribe_reason": "Keep this note"})
    assert response.status_code == 200
    assert response.context["subscription_form"].errors
    assert "Keep this note" in response.content.decode()
    assert contact.subscriptions.get().status == "subscribed"
