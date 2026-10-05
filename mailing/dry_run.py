"""RELAY_DRY_RUN enforcement for the SES transport boundary.

The flag is read directly from the process environment (settings stay untouched):
dry run is enabled iff the value is exactly ``"1"``. Unset, empty, or ``"0"``
disables it. Any other non-empty value is a configuration error that fails
closed at the first transport use instead of falling through to a real send.
"""

import os

DRY_RUN_ENV_VAR = "RELAY_DRY_RUN"


class DryRunSuppressed(Exception):
    """A mail transport action was suppressed because dry run is enforced."""

    def __init__(self, action):
        self.action = action
        super().__init__(f"dry run enforced via {DRY_RUN_ENV_VAR}: {action} suppressed, no email sent")


class DryRunConfigurationError(RuntimeError):
    """RELAY_DRY_RUN holds an unrecognized value; the transport fails closed."""


def dry_run_enabled():
    """Return True iff RELAY_DRY_RUN is exactly "1"; False for unset/empty/"0".

    Any other non-empty value raises :class:`DryRunConfigurationError` — a
    misconfigured guard flag must fail loud, never fail open into real email.
    """
    value = os.environ.get(DRY_RUN_ENV_VAR)
    if value is None or value == "" or value == "0":
        return False
    if value == "1":
        return True
    raise DryRunConfigurationError(f'{DRY_RUN_ENV_VAR} must be unset, empty, "0", or "1"; got {value!r}')


def dry_run_enforced():
    """Return True when SES client factories must hand out the inert guard.

    Unlike :func:`dry_run_enabled`, an unrecognized flag value also enforces:
    the guard fails closed on first use instead of constructing a real client.
    """
    try:
        return dry_run_enabled()
    except DryRunConfigurationError:
        return True


def ensure_transport_allowed(action):
    """The single transport gate shared by every enforcement point.

    Raises :class:`DryRunSuppressed` when dry run is enabled. An unrecognized
    RELAY_DRY_RUN value raises :class:`DryRunConfigurationError` here, so no
    transport use can fall through to a real send.
    """
    if dry_run_enabled():
        raise DryRunSuppressed(action)


class DryRunGuardClient:
    """Inert stand-in for a boto3 SES client handed out under dry run.

    Constructing it builds no boto3 client, performs no STS ``assume_role``,
    and makes no network call. Any attribute access yields a callable that
    fails closed, so a caller bypassing :mod:`mailing.ses` still cannot reach
    the provider. It renders nothing and sends nothing; it is a transport
    guard, not a second sender implementation.
    """

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def suppressed(*args, **kwargs):
            action = f"SES {name} call"
            ensure_transport_allowed(action)
            # A retained guard stays inert even if the environment changes.
            raise DryRunSuppressed(action)

        return suppressed
