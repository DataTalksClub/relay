from datetime import date

import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.models import Audience, Campaign, CampaignStatus, Client, Organization
from mailing.services import newsletters as newsletter_svc
from mailing.services.operator_ui import campaign_metadata


@pytest.fixture
def scope(db):
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client_record = Client.objects.create(organization=org, name="DTC Newsletter", slug="dtc-newsletter")
    audience = Audience.objects.create(organization=org, name="DTC", slug="dtc")
    return org, client_record, audience


@pytest.fixture
def operator(django_user_model):
    return django_user_model.objects.create_user(
        username="operator", email="operator@example.com", password="pw", is_staff=True
    )


def _login(http_client, user, client_record):
    http_client.force_login(user)
    session = http_client.session
    session["operator_client_id"] = client_record.pk
    session.save()


def test_campaign_metadata_extracts_newsletter_and_fallbacks(scope):
    _, client_record, audience = scope
    issue = newsletter_svc.create_issue(
        client_record,
        {
            "key": "weekly-303",
            "subject": "DataTalks.Club Weekly #303",
            "issue_number": 303,
            "publication_date": "2026-11-16",
        },
        actor="api:dataops",
    )
    issue.template_key = "dtc-weekly"
    issue.save(update_fields=["template_key", "updated_at"])
    newsletter_svc.add_block(
        issue,
        "sponsor",
        {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev/"},
        actor="api:dataops",
    )
    campaign, _ = newsletter_svc.assemble_campaign(issue, audience=audience, actor="api:dataops")

    meta = campaign_metadata(campaign, newsletter_issue=issue)
    assert meta.issue_number == 303
    assert meta.external_key == "weekly-303"
    assert meta.campaign_type == "Weekly newsletter"
    assert meta.source == "newsletter-issues (api:dataops)"
    assert meta.publication_date == "2026-11-16"

    # Legacy newsletter campaign without issue_number in metadata still derives #303 from key.
    legacy = Campaign.objects.create(
        client=client_record,
        audience=audience,
        external_key="weekly-302",
        subject="DataTalks.Club Weekly #302",
        metadata={"newsletter": {"issue_id": 999, "issue_key": "weekly-302"}},
    )
    legacy_meta = campaign_metadata(legacy)
    assert legacy_meta.issue_number == 302
    assert legacy_meta.source == "newsletter-issues"

    # Non-issue external_key with a year suffix must not be mistaken for an issue number.
    course = Campaign.objects.create(
        client=client_record,
        audience=audience,
        external_key="ml-zoomcamp-2026",
        subject="ML Zoomcamp 2026",
        metadata={"source_system": "DataOps", "type": "course-launch", "course_slug": "ml-zoomcamp-2026"},
    )
    course_meta = campaign_metadata(course)
    assert course_meta.issue_number is None
    assert course_meta.campaign_type == "course-launch"
    assert course_meta.source == "DataOps"
    assert ("course_slug", "ml-zoomcamp-2026") in course_meta.extra_items


def test_campaign_metadata_extracts_mailchimp_history(scope):
    _, client_record, audience = scope
    campaign = Campaign.objects.create(
        client=client_record,
        audience=audience,
        external_key="mailchimp-us19-mc250",
        status=CampaignStatus.SENT,
        sent_at=timezone.now(),
        subject="DataTalks.Club Weekly: RAG",
        metadata={
            "historical_import": True,
            "provider": "mailchimp",
            "mailchimp": {
                "campaign_id": "mc250",
                "datacenter": "us19",
                "list_id": "list-1",
                "campaign_title": "Weekly #250",
            },
        },
    )
    meta = campaign_metadata(campaign)
    assert meta.source == "Mailchimp"
    assert meta.campaign_type == "Historical import"
    assert meta.mailchimp_campaign_id == "mc250"
    assert meta.mailchimp_datacenter == "us19"
    assert meta.mailchimp_list_id == "list-1"
    assert meta.mailchimp_campaign_title == "Weekly #250"


def test_campaign_list_and_detail_show_metadata_beside_subject(client, operator, scope):
    _, client_record, audience = scope
    _login(client, operator, client_record)

    newsletter_campaign = Campaign.objects.create(
        client=client_record,
        audience=audience,
        external_key="weekly-303",
        subject="DataTalks.Club Weekly #303",
        status=CampaignStatus.SENT,
        sent_at=timezone.now(),
        metadata={
            "newsletter": {
                "issue_id": 1,
                "issue_key": "weekly-303",
                "issue_number": 303,
                "publication_date": date(2026, 11, 16).isoformat(),
                "template_key": "dtc-weekly",
                "source_system": "newsletter-issues",
                "assembled_by": "api:dataops",
            }
        },
    )
    mc_campaign = Campaign.objects.create(
        client=client_record,
        audience=audience,
        external_key="mailchimp-us19-mc250",
        subject="Archive subject",
        status=CampaignStatus.SENT,
        sent_at=timezone.now(),
        metadata={
            "historical_import": True,
            "provider": "mailchimp",
            "mailchimp": {
                "campaign_id": "mc250",
                "datacenter": "us19",
                "list_id": "list-1",
                "campaign_title": "Weekly #250",
            },
        },
    )

    list_html = client.get(reverse("mailing:campaign_list")).content.decode()
    assert "#303" in list_html
    assert "Weekly newsletter" in list_html
    assert "newsletter-issues (api:dataops)" in list_html
    assert "weekly-303" in list_html
    assert "Historical import" in list_html
    assert "Mailchimp" in list_html
    assert "mailchimp-us19-mc250" in list_html

    detail_html = client.get(reverse("mailing:campaign_detail", args=[newsletter_campaign.pk])).content.decode()
    assert "Issue #303" in detail_html
    assert "Weekly newsletter" in detail_html
    assert "newsletter-issues (api:dataops)" in detail_html
    assert "weekly-303" in detail_html
    assert "2026-11-16" in detail_html

    mc_detail_html = client.get(reverse("mailing:campaign_detail", args=[mc_campaign.pk])).content.decode()
    assert "Historical import" in mc_detail_html
    assert "Mailchimp" in mc_detail_html
    assert "mc250" in mc_detail_html
    assert "Weekly #250" in mc_detail_html
