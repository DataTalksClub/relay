from datetime import timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from mailing import aws
from mailing.ses_routes import configuration_set_for_source, parse_ses_domain_routes

SANDBOX_ROLE = "arn:aws:iam::817685572750:role/relay-sandbox-email-send"
ROUTES = (
    "dtcdev.click role="
    + SANDBOX_ROLE
    + " region=us-east-1 configuration_set=datamailer-sandbox;"
    "pocketshell.io role="
    + SANDBOX_ROLE
    + " region=us-east-1 configuration_set=datamailer-sandbox"
)


def test_parse_rejects_a_route_without_a_role():
    with pytest.raises(ImproperlyConfigured):
        parse_ses_domain_routes("pocketshell.io region=us-east-1")


@override_settings(
    RELAY_SES_DOMAIN_ROUTES=ROUTES,
    AWS_SES_CONFIGURATION_SET="relay-production",
)
def test_sandbox_domain_uses_the_sandbox_configuration_set():
    assert configuration_set_for_source("PocketShell <hello@pocketshell.io>") == "datamailer-sandbox"
    assert configuration_set_for_source("relay@datatalks.club") == "relay-production"


@override_settings(
    RELAY_REQUIRE_TASK_ROLES=True,
    RELAY_TASK_ROLE_ARNS={"email.send": "arn:aws:iam::387546586013:role/relay-production-email-send"},
    RELAY_SES_DOMAIN_ROUTES=ROUTES,
    AWS_REGION="eu-west-1",
    AWS_SES_REGION="eu-west-1",
    AWS_ENDPOINT_URL="",
)
def test_sandbox_domain_assumes_the_sandbox_role(monkeypatch):
    calls = []

    class Sts:
        def assume_role(self, **kwargs):
            calls.append(kwargs["RoleArn"])
            return {
                "Credentials": {
                    "AccessKeyId": "scoped-access",
                    "SecretAccessKey": "scoped-secret",
                    "SessionToken": "scoped-token",
                    "Expiration": timezone.now() + timedelta(hours=1),
                }
            }

    def fake_client(service_name, **kwargs):
        calls.append((service_name, kwargs.get("region_name")))
        return Sts() if service_name == "sts" else object()

    aws._credential_cache.clear()
    monkeypatch.setattr(aws.boto3, "client", fake_client)

    aws.ses_client_for_source("hello@pocketshell.io")
    aws.ses_client_for_source("relay@datatalks.club")

    assert SANDBOX_ROLE in calls
    assert "arn:aws:iam::387546586013:role/relay-production-email-send" in calls
    assert ("ses", "us-east-1") in calls
    assert ("ses", "eu-west-1") in calls
