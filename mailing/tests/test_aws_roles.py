from datetime import timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from mailing import aws


@override_settings(RELAY_REQUIRE_TASK_ROLES=True, RELAY_TASK_ROLE_ARNS={})
def test_email_client_is_default_deny_without_task_role():
    with pytest.raises(ImproperlyConfigured, match="email.send"):
        aws.ses_client()


@override_settings(
    RELAY_REQUIRE_TASK_ROLES=True,
    RELAY_TASK_ROLE_ARNS={"email.send": "arn:aws:iam::123456789012:role/relay-email"},
    AWS_REGION="eu-west-1",
    AWS_SES_REGION="us-east-1",
    AWS_ENDPOINT_URL="",
)
def test_email_client_uses_assumed_role_credentials(monkeypatch):
    calls = []

    class Sts:
        def assume_role(self, **kwargs):
            calls.append(("assume_role", kwargs))
            return {
                "Credentials": {
                    "AccessKeyId": "scoped-access",
                    "SecretAccessKey": "scoped-secret",
                    "SessionToken": "scoped-token",
                    "Expiration": timezone.now() + timedelta(hours=1),
                }
            }

    class Ses:
        pass

    def fake_client(service_name, **kwargs):
        calls.append((service_name, kwargs))
        return Sts() if service_name == "sts" else Ses()

    aws._credential_cache.clear()
    monkeypatch.setattr(aws.boto3, "client", fake_client)

    assert isinstance(aws.ses_client(), Ses)
    assert calls[0][0] == "sts"
    assert calls[1][1]["RoleArn"] == "arn:aws:iam::123456789012:role/relay-email"
    assert calls[2] == (
        "ses",
        {
            "region_name": "us-east-1",
            "endpoint_url": None,
            "aws_access_key_id": "scoped-access",
            "aws_secret_access_key": "scoped-secret",
            "aws_session_token": "scoped-token",
        },
    )
