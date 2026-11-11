from unittest.mock import patch

import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    OperatorAudit,
    Organization,
)
from mailing.services.complaints import campaign_complaint_rates, check_complaint_rate, recent_complaints
from mailing.services.operator_ui import dashboard_context


@pytest.fixture
def campaign(db):
    org = Organization.objects.create(name="Org", slug="org")
    audience = Audience.objects.create(organization=org, name="News", slug="news")
    client = Client.objects.create(organization=org, name="DM", slug="dm")
    return Campaign.objects.create(
        audience=audience, client=client, subject="Weekly", status="sent", html_body="<p>x</p>", text_body="x"
    )


def add_sent(campaign, count):
    for i in range(count):
        contact = Contact.objects.create(email=f"s{i}@example.com")
        CampaignRecipient.objects.create(campaign=campaign, contact=contact, status="sent", sent_at=timezone.now())


def add_complaint(campaign, email, n=1):
    contact = Contact.objects.create(email=email)
    EmailEvent.objects.create(
        campaign=campaign, contact=contact, client=campaign.client, audience=campaign.audience,
        event_type=EmailEventType.COMPLAINT, provider_event_id=f"evt-{n}-{email}",
        metadata={"complaint_feedback_type": "abuse"},
    )


def test_recent_and_rates(campaign):
    add_sent(campaign, 1000)
    add_complaint(campaign, "a@example.com")
    rows = recent_complaints()
    assert [(r.recipient, r.campaign_id, r.complaint_type) for r in rows] == [("a@example.com", campaign.id, "abuse")]
    (rate,) = campaign_complaint_rates()
    assert (rate.complaints, rate.sent, rate.rate_display, rate.over_threshold) == (1, 1000, "0.100%", False)


def test_alert_only_above_threshold_and_once(campaign):
    add_sent(campaign, 1000)
    add_complaint(campaign, "a@example.com")
    with patch("mailing.services.complaints.send_monitor_alert") as send:
        assert check_complaint_rate() == []
        add_complaint(campaign, "b@example.com", 2)
        assert len(check_complaint_rate()) == 1
        assert check_complaint_rate() == []
        add_complaint(campaign, "c@example.com", 3)
        assert len(check_complaint_rate()) == 1
    assert send.call_count == 2
    assert OperatorAudit.objects.filter(action="monitor.complaint_rate.reported").count() == 2


def test_alert_transport_failure_is_loud(campaign):
    add_sent(campaign, 10)
    add_complaint(campaign, "a@example.com")
    with patch("mailing.services.complaints.send_monitor_alert", side_effect=RuntimeError("ops_alert_transport_failed")):
        with pytest.raises(RuntimeError):
            check_complaint_rate()
    assert not OperatorAudit.objects.filter(action="monitor.complaint_rate.reported").exists()


def test_dashboard_collapses_sub_threshold_complaints_and_recipients(campaign):
    add_sent(campaign, 1000)
    add_complaint(campaign, "hidden-recipient@example.com")
    html = render_to_string("mailing/dashboard.html", {"active_client": campaign.client, "dashboard": dashboard_context(campaign.client)})
    assert "No campaign above the 0.1% alert threshold" in html
    assert "<details" in html and "below the threshold" in html and "Recent complaints (1)" in html
    # Recipient and campaign rows exist only inside the collapsed <details>.
    panel = html[html.index('id="ses-complaints"') : html.index('id="background-processing"')]
    assert "hidden-recipient@example.com" not in panel.split("<details")[0]
    assert "None above 0.1%" in html
    assert "% complaint rate" in html
