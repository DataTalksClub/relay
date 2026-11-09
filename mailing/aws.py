import logging
import re
import threading
from urllib.parse import quote_plus

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

import taskdeck
from mailing.dry_run import DryRunGuardClient, dry_run_enforced
from mailing.ses_routes import route_for_source

logger = logging.getLogger(__name__)

_client_cache = {}
_client_cache_lock = threading.Lock()


def _boto_config(*, max_attempts=None, **overrides):
    """Botocore Config shared by every client: standard retries, bounded timeouts, keepalive."""
    return Config(
        retries={"mode": "standard", "total_max_attempts": max_attempts or settings.AWS_MAX_ATTEMPTS},
        connect_timeout=settings.AWS_CONNECT_TIMEOUT,
        read_timeout=settings.AWS_READ_TIMEOUT,
        tcp_keepalive=True,
        **overrides,
    )


def _assume_role(role_arn):
    """One uncached STS AssumeRole call; RefreshableCredentials owns reuse and expiry."""
    run_id = taskdeck.current_run_id()
    session_name = f"relay-{str(run_id or 'worker')[:48]}"
    response = boto3.client(
        "sts",
        region_name=settings.AWS_REGION,
        endpoint_url=settings.AWS_ENDPOINT_URL or None,
        config=_boto_config(),
    ).assume_role(RoleArn=role_arn, RoleSessionName=session_name)
    return response["Credentials"]


def _refreshable_credentials(role_arn):
    def fetch():
        try:
            credentials = _assume_role(role_arn)
        except Exception:
            logger.exception("STS AssumeRole failed for %s", role_arn)
            raise
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }

    return RefreshableCredentials.create_from_metadata(
        metadata=fetch(), refresh_using=fetch, method="sts-assume-role"
    )


def _role_client(service_name, role_arn, kwargs):
    # The botocore session's credentials object refreshes itself (thread-safe)
    # before expiry, so the client built here lives for the whole process.
    botocore_session = botocore.session.get_session()
    botocore_session._credentials = _refreshable_credentials(role_arn)
    return boto3.Session(botocore_session=botocore_session).client(service_name, **kwargs)


def _role_credentials_arn(task_type):
    role_arn = settings.RELAY_TASK_ROLE_ARNS.get(task_type, "")
    if not role_arn and settings.RELAY_REQUIRE_TASK_ROLES:
        raise ImproperlyConfigured(
            f"Relay task type {task_type!r} has no configured IAM role."
        )
    return role_arn or None


def aws_client(service_name, *, endpoint_url=None, task_type=None, role_arn=None, region_name=None, config=None):
    """Return the per-process client for (service, region, endpoint, role).

    Default-chain clients pay botocore's eager instance-metadata probes at
    construction (seconds of dead connect timeouts on hosts without a metadata
    service), and role clients pay an STS call, so both are built once.
    Role clients carry RefreshableCredentials instead of frozen keys.
    """
    kwargs = {
        "region_name": region_name or settings.AWS_REGION,
        "endpoint_url": endpoint_url if endpoint_url is not None else settings.AWS_ENDPOINT_URL or None,
        "config": config if config is not None else _boto_config(),
    }
    if not role_arn and task_type:
        role_arn = _role_credentials_arn(task_type)
    cache_key = (service_name, kwargs["region_name"], kwargs["endpoint_url"], role_arn)
    with _client_cache_lock:
        cached = _client_cache.get(cache_key)
        if cached is None:
            if role_arn:
                cached = _role_client(service_name, role_arn, kwargs)
            else:
                cached = boto3.client(service_name, **kwargs)
            _client_cache[cache_key] = cached
        return cached


def sqs_client(*, endpoint_url=None):
    return aws_client("sqs", endpoint_url=endpoint_url)


_BASE64_ONLY = re.compile(r"[A-Za-z0-9+/=]*")
_FAST_FORM_MIN_LENGTH = 4096


def _fast_form_body(params, **kwargs):
    """Form-encode a large base64 field without botocore's per-byte quote loop.

    SES v1 SendRawEmail carries the whole message base64-encoded in a
    form-urlencoded body. ``urllib.parse.quote`` takes a per-byte Python path
    for it because '+', '/' and '=' are not "always safe": ~25 ms of GIL-held CPU
    for an 80 KB newsletter, the largest single cost of a campaign send. For a
    value made only of base64 characters the encoding is exactly three
    replacements, so the bytes sent (and signed) are identical.
    """
    body = params.get("body")
    if not isinstance(body, dict):
        return
    pairs = []
    fast = False
    for key, value in body.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return
        if len(value) >= _FAST_FORM_MIN_LENGTH and _BASE64_ONLY.fullmatch(value):
            encoded = value.replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")
            fast = True
        else:
            encoded = quote_plus(value.encode("utf-8"))
        pairs.append(f"{quote_plus(key.encode('utf-8'))}={encoded}")
    if fast:
        params["body"] = "&".join(pairs)


def _ses_config():
    # botocore's default pool is 10 connections; campaign sends run
    # CAMPAIGN_SEND_CONCURRENCY threads. Over the pool size urllib3 opens a
    # connection, then discards it after the request: a fresh TLS handshake per
    # send, and its warning is not visible in the worker log.
    concurrency = int(settings.CAMPAIGN_SEND_CONCURRENCY or 1)
    # One attempt: botocore would retry a read timeout, and a send whose response
    # was lost has usually been accepted, so an in-client retry is a duplicate email
    # that the sender's UNKNOWN handling never sees. Throttling is requeued by the
    # campaign task instead.
    return _boto_config(
        max_attempts=settings.AWS_SES_SEND_MAX_ATTEMPTS,
        max_pool_connections=max(10, 2 * concurrency, concurrency + 4),
    )


def _with_fast_form_body(client):
    if not hasattr(client, "meta"):
        return client
    if not getattr(client, "_relay_fast_form_body", False):
        client.meta.events.register("before-call.ses.SendRawEmail", _fast_form_body)
        client._relay_fast_form_body = True
    return client


def ses_client(*, endpoint_url=None):
    if dry_run_enforced():
        # Inert transport guard, not a boto3 client: no STS, no network, and
        # no raise here — suppression surfaces at the send gate so pre-loop
        # factory resolution (e.g. campaign batches) stays non-raising.
        return DryRunGuardClient()
    return _with_fast_form_body(
        aws_client(
            "ses",
            endpoint_url=endpoint_url,
            task_type="email.send",
            region_name=settings.AWS_SES_REGION,
            config=_ses_config(),
        )
    )


def ses_client_for_source(source, *, endpoint_url=None):
    if dry_run_enforced():
        return DryRunGuardClient()
    route = route_for_source(source)
    if route is None:
        return ses_client(endpoint_url=endpoint_url)
    return _with_fast_form_body(
        aws_client(
            "ses",
            endpoint_url=endpoint_url,
            role_arn=route.role_arn,
            region_name=route.region,
            config=_ses_config(),
        )
    )


def s3_client(*, endpoint_url=None):
    return aws_client("s3", endpoint_url=endpoint_url)
