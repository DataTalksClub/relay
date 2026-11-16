# ruff: noqa: F811 - pytest fixture imports are injected by name
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.campaign_detail_metrics import campaign_delivery_context
from mailing.models import CampaignRecipientStatus
from mailing.services.operator_ui import campaign_send_progress
from mailing.tests.test_operator_ui import (  # noqa: F401 - shared operator fixtures
    audience,
    campaign,
    client_record,
    create_recipient,
    operator,
    organization,
)

pytestmark = pytest.mark.django_db


def test_recent_rate_is_campaign_scoped_and_excludes_old_and_future_sends(campaign, django_assert_num_queries):
    now = timezone.now()
    campaign.status = 'sending'
    for index, seconds in enumerate([0, 30, 59, 61, -30]):
        create_recipient(campaign, f'person-{index}@example.test', sent_at=now - timedelta(seconds=seconds))
    other = type(campaign).objects.create(client=campaign.client, audience=campaign.audience, subject='Other')
    create_recipient(other, 'other@example.test', sent_at=now)
    progress = campaign_send_progress(campaign)
    with patch('mailing.campaign_detail_metrics.timezone.now', return_value=now), django_assert_num_queries(1):
        overview = campaign_delivery_context(campaign, progress)['delivery_overview']
    assert overview['recent_per_minute'] == 3
    assert overview['recent_per_second'] == '0.05'


@pytest.mark.parametrize('status,halted', [('sent', False), ('cancelled', False), ('sending', True)])
def test_inactive_campaign_does_not_report_stale_rate(campaign, status, halted, django_assert_num_queries):
    create_recipient(campaign, 'recent@example.test', sent_at=timezone.now())
    campaign.status = status
    campaign.send_halted_at = timezone.now() if halted else None
    progress = campaign_send_progress(campaign)
    with django_assert_num_queries(0):
        overview = campaign_delivery_context(campaign, progress)['delivery_overview']
    assert overview['recent_per_second'] == '0.00'
    assert not overview['active']


def test_snapshotting_and_empty_campaign_progress_are_not_complete(campaign):
    campaign.status = 'snapshotting'
    campaign.sent_count = campaign.skipped_count = 0
    overview = campaign_delivery_context(campaign, campaign_send_progress(campaign))['delivery_overview']
    assert overview['percent'] == 0
    campaign.recipient_count = 0
    assert campaign_delivery_context(campaign, campaign_send_progress(campaign))['delivery_overview']['percent'] == 0


def test_results_precede_secondary_sections_and_filters_reveal_recipients(client, operator, campaign):
    client.force_login(operator)
    create_recipient(campaign, 'pending@example.test', status=CampaignRecipientStatus.PENDING)
    url = reverse('mailing:campaign_detail', args=[campaign.pk])
    response = client.get(url)
    html = response.content.decode()
    assert response.status_code == 200
    assert html.index('id="stats-delivery"') < html.index('id="stats-engagement"') < html.index('id="campaign-message-preview"')
    assert html.count('<progress ') == 4
    assert 'Current send rate' in html
    assert 'last 60 seconds' in html
    assert 'max="4"' in html
    assert 'id="campaign-recipients"><summary>' in html
    filtered = client.get(url, {'filter': 'pending'}).content.decode()
    assert 'id="campaign-recipients" open><summary>' in filtered
    assert 'pending@example.test' in filtered
    campaign.refresh_from_db()
    assert campaign.status == 'sent'


def test_sent_bar_uses_send_evidence_instead_of_status_or_stale_counter(campaign):
    campaign.status = 'sending'
    campaign.sent_count = 999
    campaign.skipped_count = 500
    create_recipient(campaign, 'accepted@example.test', status=CampaignRecipientStatus.UNSUBSCRIBED, sent_at=timezone.now())
    create_recipient(campaign, 'waiting@example.test', status=CampaignRecipientStatus.PENDING)
    overview = campaign_delivery_context(campaign, campaign_send_progress(campaign))['delivery_overview']
    assert overview['sent'] == 1
    assert overview['pending'] == 1
    assert overview['percent'] == 25
