from datetime import timedelta

import pytest
from botocore.credentials import RefreshableCredentials
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

    def fake_sts(service_name, **kwargs):
        calls.append((service_name, kwargs))
        return Sts()

    monkeypatch.setattr(aws.boto3, "client", fake_sts)

    client = aws.ses_client()
    assert calls[0][0] == "sts"
    assert calls[1][1]["RoleArn"] == "arn:aws:iam::123456789012:role/relay-email"
    assert client.meta.region_name == "us-east-1"
    creds = client._request_signer._credentials
    assert isinstance(creds, RefreshableCredentials)
    frozen = creds.get_frozen_credentials()
    assert (frozen.access_key, frozen.secret_key, frozen.token) == ("scoped-access", "scoped-secret", "scoped-token")


@override_settings(
    RELAY_TASK_ROLE_ARNS={},
    AWS_REGION="eu-west-1",
    AWS_SES_REGION="us-east-1",
    AWS_ENDPOINT_URL="",
)
def test_default_chain_clients_are_cached_per_process(monkeypatch):
    constructions = []

    class Ses:
        pass

    def recording_client(service_name, **kwargs):
        constructions.append((service_name, kwargs))
        return Ses()

    monkeypatch.setattr(aws.boto3, "client", recording_client)

    first = aws.ses_client()
    second = aws.ses_client()
    third = aws.sqs_client()

    assert first is second
    assert isinstance(third, Ses)
    assert [name for name, _kwargs in constructions] == ["ses", "sqs"]


@override_settings(
    RELAY_REQUIRE_TASK_ROLES=True,
    RELAY_TASK_ROLE_ARNS={"email.send": "arn:aws:iam::123456789012:role/relay-email"},
    AWS_REGION="eu-west-1",
    AWS_SES_REGION="us-east-1",
    AWS_ENDPOINT_URL="",
)
def test_role_client_is_cached_and_refreshes_credentials(monkeypatch):
    sts_calls = []
    # First credentials are already inside botocore's refresh window.
    lifetimes = iter([timedelta(minutes=1), timedelta(hours=1)])

    class Sts:
        def assume_role(self, **kwargs):
            sts_calls.append(kwargs)
            n = len(sts_calls)
            return {
                "Credentials": {
                    "AccessKeyId": f"access-{n}",
                    "SecretAccessKey": "secret",
                    "SessionToken": "token",
                    "Expiration": timezone.now() + next(lifetimes),
                }
            }

    monkeypatch.setattr(aws.boto3, "client", lambda service_name, **kwargs: Sts())

    first = aws.ses_client()
    assert len(sts_calls) == 1
    second = aws.ses_client()
    assert first is second  # created once per process, not per batch
    creds = first._request_signer._credentials
    assert isinstance(creds, RefreshableCredentials)
    # Signing near expiry refreshes in place on the same client.
    assert creds.get_frozen_credentials().access_key == "access-2"
    assert len(sts_calls) == 2
    assert aws.ses_client() is first


@override_settings(
    RELAY_TASK_ROLE_ARNS={},
    AWS_REGION="eu-west-1",
    AWS_SES_REGION="us-east-1",
    AWS_ENDPOINT_URL="",
    CAMPAIGN_SEND_CONCURRENCY=16,
    AWS_CONNECT_TIMEOUT=4,
    AWS_READ_TIMEOUT=17,
    AWS_MAX_ATTEMPTS=3,
    AWS_SES_SEND_MAX_ATTEMPTS=1,
)
def test_ses_client_config_pool_retries_timeouts_keepalive(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    config = aws.ses_client().meta.config
    assert config.max_pool_connections >= 16 + 4
    assert config.retries["mode"] == "standard"
    # SES sends are not idempotent: botocore must not retry a lost response (a duplicate
    # email the sender's UNKNOWN handling never sees). Other clients keep AWS_MAX_ATTEMPTS.
    assert config.retries["total_max_attempts"] == 1
    assert aws._boto_config().retries["total_max_attempts"] == 3
    assert (config.connect_timeout, config.read_timeout) == (4, 17)
    assert config.tcp_keepalive is True


@override_settings(CAMPAIGN_SEND_CONCURRENCY=40)
def test_pool_size_follows_send_concurrency():
    assert aws._ses_config().max_pool_connections >= 44
