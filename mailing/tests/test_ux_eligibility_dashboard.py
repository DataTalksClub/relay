import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaigns import _skip_reason
from mailing.services.operator_ui import (
    ContactExplorerFilters,
    contact_detail_context,
    contact_explorer_queryset,
    contact_result_rows,
    dashboard_attention_items,
    dashboard_context,
    metadata_summary,
    worker_health,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    organization = Organization.objects.create(name="School", slug="school")
    audience = Audience.objects.create(organization=organization, name="Learners", slug="learners")
    client = Client.objects.create(organization=organization, name="Courses", slug="courses")
    return audience, client


def make_row(contact, audience, client):
    contacts = contact_explorer_queryset(ContactExplorerFilters(audience_id=audience.id, client_id=client.id))
    return contact_result_rows(contacts.filter(pk=contact.pk), audience=audience, client=client)[0]


@pytest.mark.parametrize("verification", ["none", "contact", "client", "audience"])
@pytest.mark.parametrize("blocker", ["none", "audience_unsubscribe", "client_unsubscribe", "invalid", "complaint", "global", "bounce"])
def test_list_and_detail_eligibility_match_campaign_policy(scope, verification, blocker):
    audience, client = scope
    contact = Contact.objects.create(
        email="learner@example.com", normalized_email="learner@example.com",
        verified_at=timezone.now() if verification == "contact" else None,
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID if blocker == "invalid" else "unknown",
        complained_at=timezone.now() if blocker == "complaint" else None,
        global_unsubscribed_at=timezone.now() if blocker == "global" else None,
        hard_bounced_at=timezone.now() if blocker == "bounce" else None,
    )
    Subscription.objects.create(
        contact=contact, audience=audience, client=client,
        status=SubscriptionStatus.UNSUBSCRIBED if blocker == "client_unsubscribe" else SubscriptionStatus.SUBSCRIBED,
        verified_at=timezone.now() if verification == "client" else None,
    )
    Subscription.objects.create(
        contact=contact, audience=audience,
        status=SubscriptionStatus.UNSUBSCRIBED if blocker == "audience_unsubscribe" else SubscriptionStatus.SUBSCRIBED,
        verified_at=timezone.now() if verification == "audience" else None,
    )
    campaign = Campaign.objects.create(client=client, audience=audience, subject="Test")
    can_send = not _skip_reason(contact, campaign)
    row = make_row(contact, audience, client)
    detail = contact_detail_context(contact, client=client)
    assert (row.primary_status_badge.tone == "success") == can_send
    assert (detail.sendability.marketing_badge.tone == "success") == can_send
    if not can_send:
        assert row.eligibility_reasons
        assert row.primary_status_badge.label.startswith("Cannot send")


def test_subscription_from_another_client_never_makes_selected_client_eligible(scope):
    audience, client = scope
    other_client = Client.objects.create(organization=client.organization, name="Other", slug="other")
    contact = Contact.objects.create(email="person@example.com", normalized_email="person@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=SubscriptionStatus.PENDING)
    Subscription.objects.create(
        contact=contact, audience=audience, client=other_client,
        status=SubscriptionStatus.SUBSCRIBED, verified_at=timezone.now(),
    )
    row = make_row(contact, audience, client)
    assert row.subscription_badge.label == "Pending"
    assert row.primary_status_badge.tone != "success"
    assert "unverified" in row.eligibility_reasons


def test_dashboard_distinguishes_incidents_from_expected_exclusions(scope):
    audience, client = scope
    contact = Contact.objects.create(email="person@example.com", normalized_email="person@example.com")
    for event_type in [EmailEventType.SKIPPED, EmailEventType.UNSUBSCRIBE, EmailEventType.COMPLAINT]:
        EmailEvent.objects.create(
            audience=audience, client=client, contact=contact,
            event_type=event_type, metadata={"feedback_type": "abuse"} if event_type == "complaint" else {},
        )
    items = dashboard_attention_items(client)
    assert len(items) == 1
    assert items[0].badge.label == "Complaint"
    assert "Marked as spam" in items[0].detail
    assert "Keep this contact suppressed" in items[0].detail
    assert metadata_summary({"feedback_type": "abuse"}) == "Marked as spam"


def test_dashboard_places_tasks_before_diagnostics_and_prioritizes_open_work(scope, monkeypatch):
    audience, client = scope
    monkeypatch.setattr("mailing.services.operator_ui.sandbox_worker_statuses", lambda: [])
    working = Campaign.objects.create(client=client, audience=audience, subject="Sending", status=CampaignStatus.SENDING)
    draft = Campaign.objects.create(client=client, audience=audience, subject="Draft", status=CampaignStatus.DRAFT)
    for index in range(6):
        Campaign.objects.create(client=client, audience=audience, subject=f"Done {index}", status=CampaignStatus.SENT)
    context = dashboard_context(client)
    assert [item.campaign for item in context.recent_campaigns[:2]] == [working, draft]
    html = render_to_string("mailing/dashboard.html", {"active_client": client, "dashboard": context})
    assert html.index('id="deliverability-attention"') < html.index('id="recent-campaigns"')
    assert html.index('id="recent-campaigns"') < html.index("Processing diagnostics")
    assert "Resume draft" in html
    assert "No recent delivery incidents found" in html
    assert worker_health([]) == ("Processing health unavailable", "neutral")
