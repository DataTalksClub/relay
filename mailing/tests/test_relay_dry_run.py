"""RELAY_DRY_RUN enforcement unit tests: flag parsing, send gates, client factories.

All tests are offline — no real AWS, no network, no LocalStack. The dry-run
suppression path must never construct a boto3 client or touch STS, and the
tests enforce that with fail-the-test spies.
"""

from unittest.mock import Mock

import boto3
import pytest

from mailing.aws import ses_client, ses_client_for_source, sqs_client
from mailing.dry_run import (
    DryRunConfigurationError,
    DryRunGuardClient,
    DryRunSuppressed,
    dry_run_enabled,
    dry_run_enforced,
    ensure_transport_allowed,
)
from mailing.ses import send_email, send_raw_email


@pytest.mark.parametrize(("value", "enabled"), [(None, False), ("", False), ("0", False), ("1", True)])
def test_flag_accepts_only_exact_binary_values(value, enabled, monkeypatch):
    if value is None:
        monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("RELAY_DRY_RUN", value)

    assert dry_run_enabled() is enabled


@pytest.mark.parametrize("value", ["false", "true", "yes", "on", " 1", "1 ", "01", "enabled"])
def test_unrecognized_flag_values_fail_closed(value, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", value)

    with pytest.raises(DryRunConfigurationError):
        dry_run_enabled()


def test_gate_raises_naming_the_suppressed_action(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")

    with pytest.raises(DryRunSuppressed) as suppressed:
        ensure_transport_allowed("SES send_email")

    assert "SES send_email" in str(suppressed.value)
    assert "RELAY_DRY_RUN" in str(suppressed.value)


def test_gate_passes_when_unset_empty_or_zero(monkeypatch):
    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    assert ensure_transport_allowed("SES send_email") is None

    monkeypatch.setenv("RELAY_DRY_RUN", "")
    assert ensure_transport_allowed("SES send_email") is None

    monkeypatch.setenv("RELAY_DRY_RUN", "0")
    assert ensure_transport_allowed("SES send_email") is None


def test_factory_enforcement_is_true_for_enabled_and_unrecognized_values(monkeypatch):
    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    assert dry_run_enforced() is False

    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    assert dry_run_enforced() is True

    monkeypatch.setenv("RELAY_DRY_RUN", "garbage")
    assert dry_run_enforced() is True


def test_guard_client_methods_raise_and_private_attributes_stay_hidden():
    guard = DryRunGuardClient()

    with pytest.raises(DryRunSuppressed):
        guard.send_email(Source="s@example.com")

    with pytest.raises(DryRunSuppressed):
        guard.send_raw_email()

    with pytest.raises(AttributeError):
        _ = guard._private


class _UnreachableSesClient:
    def __getattr__(self, name):
        raise AssertionError(f"SES client method {name} must not be reached in dry run")


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("must not be called in dry run")


def _forbid_boto3_and_sts(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("dry run must not construct boto3 clients or call STS")

    monkeypatch.setattr("boto3.client", fail)
    monkeypatch.setattr("mailing.aws._assume_role", fail)


def test_send_email_suppressed_before_throttle_or_client_use(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    monkeypatch.setattr("mailing.ses.throttle_ses_send", _fail_if_called)

    with pytest.raises(DryRunSuppressed):
        send_email(
            ses_client=_UnreachableSesClient(),
            source="sender@example.com",
            to_email="to@example.com",
            subject="Subject",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )


def test_send_raw_email_suppressed_before_client_use(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")

    with pytest.raises(DryRunSuppressed):
        send_raw_email(
            ses_client=_UnreachableSesClient(),
            source="sender@example.com",
            to_email="to@example.com",
            subject="Subject",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )


def test_payload_validation_still_runs_before_suppression(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")

    with pytest.raises(ValueError):
        send_email(
            ses_client=_UnreachableSesClient(),
            source="sender@example.com",
            to_email="to@example.com",
            subject="Subject",
            html_body="<p>Hello</p>",
            text_body="Hello",
            calendar_alternative={"method": "REQUEST"},
        )


class _RecordingSesClient:
    def __init__(self):
        self.simple_params = None

    def send_email(self, **params):
        self.simple_params = params
        return {"MessageId": "direct-message-123"}


def test_disabled_flag_keeps_direct_send_path(monkeypatch):
    ses = _RecordingSesClient()

    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    assert send_email(
        ses_client=ses,
        source="sender@example.com",
        to_email="to@example.com",
        subject="Subject",
        html_body="<p>Hello</p>",
        text_body="Hello",
    ) == "direct-message-123"
    assert ses.simple_params["Source"] == "sender@example.com"

    monkeypatch.setenv("RELAY_DRY_RUN", "0")
    assert send_email(
        ses_client=ses,
        source="sender@example.com",
        to_email="to@example.com",
        subject="Subject",
        html_body="<p>Hello</p>",
        text_body="Hello",
    ) == "direct-message-123"


def test_ses_factories_return_inert_guard_without_boto3_or_sts(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    _forbid_boto3_and_sts(monkeypatch)

    assert isinstance(ses_client(), DryRunGuardClient)
    assert isinstance(ses_client_for_source("person@example.com"), DryRunGuardClient)


def test_unrecognized_flag_factory_does_not_raise_and_gate_fails_closed(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "true")
    _forbid_boto3_and_sts(monkeypatch)

    guard = ses_client()
    assert isinstance(guard, DryRunGuardClient)

    with pytest.raises(DryRunConfigurationError):
        send_email(
            ses_client=guard,
            source="sender@example.com",
            to_email="to@example.com",
            subject="Subject",
            html_body="<p>Hello</p>",
        )


def test_non_mail_aws_clients_are_not_gated(monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    constructed = []
    real_client = boto3.client

    def recording_client(*args, **kwargs):
        constructed.append(args[0] if args else kwargs.get("service_name"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr("boto3.client", recording_client)

    sqs = sqs_client()

    assert not isinstance(sqs, DryRunGuardClient)
    assert constructed == ["sqs"]


@pytest.mark.parametrize("sender", [send_email, send_raw_email])
@pytest.mark.parametrize("value", ["0", "1", "true"])
def test_raw_mime_validation_precedes_transport_guard(sender, value, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", value)
    _forbid_boto3_and_sts(monkeypatch)
    client = Mock()
    throttle = Mock(side_effect=_fail_if_called)
    monkeypatch.setattr("mailing.ses.throttle_ses_send", throttle)

    with pytest.raises(ValueError, match="linefeed or carriage return"):
        sender(
            ses_client=client,
            source="synthetic-sender",
            to_email="synthetic-recipient",
            subject="invalid\nheader",
            html_body="synthetic",
            headers={"X-Synthetic": "value"},
        )

    assert client.mock_calls == []
    assert throttle.call_count == 0


@pytest.mark.parametrize("sender", [send_email, send_raw_email])
@pytest.mark.parametrize("value", ["1", "true"])
def test_valid_raw_mime_is_guarded_before_throttle_or_dispatch(sender, value, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", value)
    _forbid_boto3_and_sts(monkeypatch)
    client = Mock()
    throttle = Mock(side_effect=_fail_if_called)
    monkeypatch.setattr("mailing.ses.throttle_ses_send", throttle)
    expected = DryRunSuppressed if value == "1" else DryRunConfigurationError

    with pytest.raises(expected, match="RELAY_DRY_RUN"):
        sender(
            ses_client=client,
            source="synthetic-sender",
            to_email="synthetic-recipient",
            subject="Valid subject",
            html_body="synthetic",
            headers={"X-Synthetic": "value"},
        )

    assert client.mock_calls == []
    assert throttle.call_count == 0


@pytest.mark.parametrize("factory", [ses_client, ses_client_for_source])
@pytest.mark.parametrize("method", ["send_email", "send_raw_email"])
@pytest.mark.parametrize("value", ["1", "true", " 1"])
def test_inert_factory_guard_checks_configuration_at_first_use(factory, method, value, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", value)
    _forbid_boto3_and_sts(monkeypatch)
    guard = factory() if factory is ses_client else factory("synthetic-source")
    assert isinstance(guard, DryRunGuardClient)
    expected = DryRunSuppressed if value == "1" else DryRunConfigurationError

    with pytest.raises(expected, match="RELAY_DRY_RUN"):
        getattr(guard, method)()
