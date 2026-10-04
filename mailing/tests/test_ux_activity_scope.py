from datetime import timedelta

import pytest
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    ContactTag,
    EmailTemplate,
    Organization,
    Subscription,
    Tag,
    TransactionalMessage,
)
from mailing.services.operator_ui import ContactExplorerFilters, contact_explorer_queryset, contact_result_rows

pytestmark = pytest.mark.django_db


@pytest.fixture
def history():
    org = Organization.objects.create(name="Scope", slug="scope")
    active_client = Client.objects.create(organization=org, name="Active", slug="active")
    other_client = Client.objects.create(organization=org, name="Other", slug="other")
    audience = Audience.objects.create(organization=org, name="Readers", slug="readers")
    other_audience = Audience.objects.create(organization=org, name="Courses", slug="courses")
    contact = Contact.objects.create(email="shared@example.com", verified_at=timezone.now())
    Subscription.objects.create(client=active_client, audience=audience, contact=contact)
    earlier = timezone.now() - timedelta(days=10)
    later = timezone.now()
    for send_client, send_audience, stamp in [(active_client, audience, earlier),
                                              (other_client, audience, later),
                                              (active_client, other_audience, later)]:
        campaign = Campaign.objects.create(client=send_client, audience=send_audience, subject="Message")
        CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email,
                                         status="sent", sent_at=stamp, first_opened_at=stamp,
                                         first_clicked_at=stamp)
    for send_client, stamp in [(active_client, earlier), (other_client, later)]:
        template = EmailTemplate.objects.create(client=send_client, key="welcome", name="Welcome", subject="Welcome")
        TransactionalMessage.objects.create(client=send_client, template=template, template_key="welcome",
                                            contact=contact, email=contact.email, subject="Welcome", status="sent",
                                            sent_at=stamp, first_opened_at=stamp, first_clicked_at=stamp)
    return active_client, audience, contact, earlier, later


def test_explorer_history_uses_selected_client_and_audience(history):
    active_client, audience, contact, earlier, _ = history
    row = contact_explorer_queryset(ContactExplorerFilters(client_id=active_client.id, audience_id=audience.id)).get(pk=contact.id)
    assert row.campaign_recipient_count == 1
    assert row.transactional_message_count == 1
    for source in ["campaign", "transactional"]:
        for event in ["sent", "opened", "clicked"]:
            assert getattr(row, f"last_{source}_{event}_at") == earlier


def test_client_contact_history_includes_that_clients_other_audiences(history):
    active_client, _, contact, earlier, later = history
    row = contact_explorer_queryset(ContactExplorerFilters(client_id=active_client.id)).get(pk=contact.id)
    assert row.campaign_recipient_count == 2
    assert row.transactional_message_count == 1
    assert row.last_campaign_sent_at == later
    assert row.last_transactional_sent_at == earlier


def test_other_client_history_does_not_create_recent_activity(history):
    active_client, audience, contact, _, _ = history
    contact.campaign_recipients.filter(campaign__client=active_client).delete()
    contact.transactional_messages.filter(client=active_client).delete()
    row = contact_explorer_queryset(ContactExplorerFilters(client_id=active_client.id, audience_id=audience.id)).get(pk=contact.id)
    assert row.campaign_recipient_count == row.transactional_message_count == 0
    assert row.last_campaign_sent_at is None
    assert row.last_campaign_opened_at is None
    assert row.last_campaign_clicked_at is None
    assert row.last_transactional_sent_at is None
    assert row.last_transactional_opened_at is None
    assert row.last_transactional_clicked_at is None


@pytest.mark.parametrize("scope_field", ["client", "audience"])
@pytest.mark.parametrize("filter_values,recipient_values", [
    ({"campaign_status": "failed"}, {"status": "failed"}),
    ({"skip_reason": "unverified"}, {"status": "skipped", "skip_reason": "unverified"}),
])
def test_recipient_filters_do_not_match_another_scope(history, scope_field, filter_values, recipient_values):
    active_client, audience, contact, _, _ = history
    recipients = contact.campaign_recipients
    if scope_field == "client":
        recipients = recipients.exclude(campaign__client=active_client)
    else:
        recipients = recipients.exclude(campaign__audience=audience)
    recipients.update(**recipient_values)
    filters = ContactExplorerFilters(client_id=active_client.id, audience_id=audience.id, **filter_values)
    assert not contact_explorer_queryset(filters).filter(pk=contact.id).exists()
    matching = contact.campaign_recipients.get(campaign__client=active_client, campaign__audience=audience)
    for field, value in recipient_values.items():
        setattr(matching, field, value)
    matching.save()
    assert contact_explorer_queryset(filters).filter(pk=contact.id).exists()


def test_status_and_skip_reason_must_match_the_same_recipient(history):
    active_client, audience, contact, _, _ = history
    contact.campaign_recipients.filter(campaign__client=active_client, campaign__audience=audience).update(status="failed")
    campaign = Campaign.objects.create(client=active_client, audience=audience, subject="Skipped")
    CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email,
                                     status="skipped", skip_reason="unverified")
    filters = ContactExplorerFilters(client_id=active_client.id, audience_id=audience.id,
                                     campaign_status="failed", skip_reason="unverified")
    assert not contact_explorer_queryset(filters).filter(pk=contact.id).exists()


def test_contact_row_tags_include_only_selected_organization(history):
    active_client, audience, contact, _, _ = history
    other_organization = Organization.objects.create(name="Private", slug="private")
    private_audience = Audience.objects.create(organization=other_organization, name="Private readers", slug="private")
    other_local_audience = Audience.objects.get(organization=active_client.organization, slug="courses")
    for tag_audience, name, slug in [(audience, "Reader tag", "reader-tag"),
                                     (other_local_audience, "Course tag", "course-tag"),
                                     (private_audience, "Private tag", "private-tag")]:
        tag = Tag.objects.create(audience=tag_audience, name=name, slug=slug)
        ContactTag.objects.create(contact=contact, tag=tag)
    contacts = list(contact_explorer_queryset(ContactExplorerFilters(client_id=active_client.id)))
    row = contact_result_rows(contacts, client=active_client)[0]
    assert "reader-tag" in row.tag_summary
    assert "course-tag" in row.tag_summary
    assert "private-tag" not in row.tag_summary
    audience_row = contact_result_rows(contacts, client=active_client, audience=audience)[0]
    assert "reader-tag" in audience_row.tag_summary
    assert "course-tag" not in audience_row.tag_summary
    assert "private-tag" not in audience_row.tag_summary
