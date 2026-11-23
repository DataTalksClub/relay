"""Tests for client IP extraction, offline GeoIP lookup, and open/click geo capture."""

from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import geo
from mailing.services.public_urls import click_redirect_url
from mailing.services.tokens import ensure_campaign_recipient_tokens
from mailing.tests.task_helpers import drain_queue

pytestmark = pytest.mark.django_db


def _signed_click_params(token, destination):
    return parse_qs(urlparse(click_redirect_url(token, destination)).query)


@pytest.fixture(autouse=True)
def _reset_geo_cache():
    geo.reset_geoip_reader_cache()
    yield
    geo.reset_geoip_reader_cache()


@pytest.fixture
def recipient():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")
    app_client = Client.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    contact = Contact.objects.create(email="reader@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=contact, audience=audience, status=SubscriptionStatus.SUBSCRIBED)
    campaign = Campaign.objects.create(audience=audience, client=app_client, subject="Weekly")
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
    )


class TestClientIp:
    def setup_method(self):
        self.rf = RequestFactory()

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_extracts_nth_from_right_behind_cloudfront_and_traefik(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 130.176.1.2",
            REMOTE_ADDR="172.18.0.2",
        )
        assert geo.client_ip(request) == "203.0.113.5"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_ignores_spoofed_leftmost_xff_entries(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2, 203.0.113.5, 130.176.1.2",
            REMOTE_ADDR="172.18.0.2",
        )
        assert geo.client_ip(request) == "203.0.113.5"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_fewer_hops_than_trusted_falls_back_to_remote_addr_never_leftmost(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
            REMOTE_ADDR="198.51.100.9",
        )
        assert geo.client_ip(request) == "198.51.100.9"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=1, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_single_trusted_hop_uses_rightmost_xff(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.42",
            REMOTE_ADDR="10.0.0.1",
        )
        assert geo.client_ip(request) == "203.0.113.42"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=0, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_zero_trusted_hops_ignores_xff_and_uses_remote_addr(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 130.176.1.2",
            REMOTE_ADDR="198.51.100.9",
        )
        assert geo.client_ip(request) == "198.51.100.9"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_ignores_spoofed_cf_headers_when_cloudflare_not_trusted(self):
        request = self.rf.get(
            "/t/o/abc.gif",
            HTTP_CF_CONNECTING_IP="8.8.8.8",
            HTTP_CF_IPCOUNTRY="US",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 130.176.1.2",
            REMOTE_ADDR="172.18.0.2",
        )
        assert geo.client_ip(request) == "203.0.113.5"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=True)
    def test_uses_cf_connecting_ip_only_when_enabled_and_via_trusted_proxy(self):
        trusted = self.rf.get(
            "/t/o/abc.gif",
            HTTP_CF_CONNECTING_IP="203.0.113.99",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 130.176.1.2",
            REMOTE_ADDR="172.18.0.2",
        )
        assert geo.client_ip(trusted) == "203.0.113.99"

        direct_untrusted = self.rf.get(
            "/t/o/abc.gif",
            HTTP_CF_CONNECTING_IP="85.214.132.117",
            REMOTE_ADDR="8.8.4.4",
        )
        assert geo.client_ip(direct_untrusted) == "8.8.4.4"

    @override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
    def test_invalid_xff_entry_falls_back_to_remote_addr_and_ipv6_works(self):
        bad = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="not-an-ip, 130.176.1.2",
            REMOTE_ADDR="198.51.100.9",
        )
        assert geo.client_ip(bad) == "198.51.100.9"

        ipv6 = self.rf.get(
            "/t/o/abc.gif",
            HTTP_X_FORWARDED_FOR="2001:db8::1, 130.176.1.2",
            REMOTE_ADDR="172.18.0.2",
        )
        assert geo.client_ip(ipv6) == "2001:db8::1"


class TestGeoLookup:
    @override_settings(RELAY_GEOIP_DB_PATH="/nonexistent/path/geoip.mmdb")
    def test_missing_mmdb_returns_none_without_raising(self):
        assert geo.lookup_geo("8.8.8.8") == (None, None)

    def test_private_and_invalid_ips_short_circuit(self):
        assert geo.lookup_geo("127.0.0.1") == (None, None)
        assert geo.lookup_geo("10.0.0.1") == (None, None)
        assert geo.lookup_geo("172.18.0.2") == (None, None)
        assert geo.lookup_geo("not-an-ip") == (None, None)
        assert geo.lookup_geo(None) == (None, None)

    def test_reader_lookup_extracts_country_and_city_and_survives_errors(self):
        fake_reader = mock.Mock()
        fake_reader.get.return_value = {
            "country": {"iso_code": "de"},
            "city": {"names": {"en": "Berlin"}},
        }
        with mock.patch("mailing.services.geo._get_geoip_reader", return_value=fake_reader):
            assert geo.lookup_geo("85.214.132.117") == ("DE", "Berlin")

        fake_reader.get.side_effect = RuntimeError("corrupt mmdb")
        with mock.patch("mailing.services.geo._get_geoip_reader", return_value=fake_reader):
            assert geo.lookup_geo("85.214.132.117") == (None, None)


@override_settings(RELAY_TRUSTED_PROXY_HOPS=2, RELAY_TRUST_CLOUDFLARE_HEADERS=False)
def test_open_and_click_persist_ip_country_city(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    fake_reader = mock.Mock()
    fake_reader.get.return_value = {
        "country": {"iso_code": "DE"},
        "city": {"names": {"en": "Frankfurt am Main"}},
    }
    headers = {
        "HTTP_X_FORWARDED_FOR": "1.2.3.4, 85.214.132.117, 130.176.1.2",
        "REMOTE_ADDR": "172.18.0.2",
    }

    with mock.patch("mailing.services.geo._get_geoip_reader", return_value=fake_reader):
        open_resp = client.get(reverse("mailing:tracking_open", args=[token]), **headers)
        click_resp = client.get(
            reverse("mailing:tracking_click", args=[token]),
            _signed_click_params(token, "https://example.com/geo"),
            **headers,
        )

    assert open_resp.status_code == 200
    assert click_resp.status_code == 302
    assert click_resp.headers["Location"] == "https://example.com/geo"
    assert drain_queue("tracking") == 2

    open_event = EmailEvent.objects.get(event_type=EmailEventType.OPEN)
    click_event = EmailEvent.objects.get(event_type=EmailEventType.CLICK)
    assert (open_event.ip, open_event.country, open_event.city) == ("85.214.132.117", "DE", "Frankfurt am Main")
    assert (click_event.ip, click_event.country, click_event.city) == ("85.214.132.117", "DE", "Frankfurt am Main")


@override_settings(
    RELAY_TRUSTED_PROXY_HOPS=2,
    RELAY_TRUST_CLOUDFLARE_HEADERS=False,
    RELAY_GEOIP_DB_PATH="/nonexistent/geoip.mmdb",
)
def test_missing_geo_db_still_records_ip_and_serves_pixel_and_redirect(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    headers = {
        "HTTP_X_FORWARDED_FOR": "85.214.132.117, 130.176.1.2",
        "REMOTE_ADDR": "172.18.0.2",
    }

    open_resp = client.get(reverse("mailing:tracking_open", args=[token]), **headers)
    click_resp = client.get(
        reverse("mailing:tracking_click", args=[token]),
        _signed_click_params(token, "https://example.com/fallback"),
        **headers,
    )

    assert open_resp.status_code == 200
    assert click_resp.status_code == 302
    assert drain_queue("tracking") == 2

    open_event = EmailEvent.objects.get(event_type=EmailEventType.OPEN)
    click_event = EmailEvent.objects.get(event_type=EmailEventType.CLICK)
    assert (open_event.ip, open_event.country, open_event.city) == ("85.214.132.117", None, None)
    assert (click_event.ip, click_event.country, click_event.city) == ("85.214.132.117", None, None)
