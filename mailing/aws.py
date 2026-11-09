import re
import threading
from datetime import timedelta
from urllib.parse import quote_plus

import boto3
from botocore.config import Config
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

import taskdeck
from mailing.dry_run import DryRunGuardClient, dry_run_enforced
from mailing.ses_routes import route_for_source

_credential_cache = {}
_credential_cache_lock = threading.Lock()
_client_cache = {}
_client_cache_lock = threading.Lock()


def _assume_role(role_arn):
    with _credential_cache_lock:
        cached = _credential_cache.get(role_arn)
        if cached and cached["Expiration"] > timezone.now() + timedelta(minutes=5):
            return cached

        run_id = taskdeck.current_run_id()
        session_name = f"relay-{str(run_id or 'worker')[:48]}"
        response = boto3.client(
            "sts",
            region_name=settings.AWS_REGION,
            endpoint_url=settings.AWS_ENDPOINT_URL or None,
        ).assume_role(RoleArn=role_arn, RoleSessionName=session_name)
        credentials = response["Credentials"]
        _credential_cache[role_arn] = credentials
        return credentials


def _role_credentials(task_type):
    role_arn = settings.RELAY_TASK_ROLE_ARNS.get(task_type, "")
    if not role_arn:
        if settings.RELAY_REQUIRE_TASK_ROLES:
            raise ImproperlyConfigured(
                f"Relay task type {task_type!r} has no configured IAM role."
            )
        return None
    return _assume_role(role_arn)


def aws_client(service_name, *, endpoint_url=None, task_type=None, role_arn=None, region_name=None, config=None):
    kwargs = {
        "region_name": region_name or settings.AWS_REGION,
        "endpoint_url": endpoint_url if endpoint_url is not None else settings.AWS_ENDPOINT_URL or None,
    }
    if config is not None:
        kwargs["config"] = config
    if role_arn:
        credentials = _assume_role(role_arn)
    elif task_type:
        credentials = _role_credentials(task_type)
    else:
        credentials = None
    if credentials:
        kwargs |= {
            "aws_access_key_id": credentials["AccessKeyId"],
            "aws_secret_access_key": credentials["SecretAccessKey"],
            "aws_session_token": credentials["SessionToken"],
        }
        return boto3.client(service_name, **kwargs)

    # Default-chain clients (no injected credentials) pay botocore's eager
    # instance-metadata credential probes at construction; on hosts without a
    # metadata service that is seconds of dead connect timeouts per build
    # before the eventual NoCredentialsError. Build one client per process
    # instead. Deployments that provide explicit credentials keep building
    # fresh, uncached clients, so no assumed-role credentials are ever frozen
    # into a cached object.
    cache_key = (service_name, kwargs["region_name"], kwargs["endpoint_url"])
    with _client_cache_lock:
        cached = _client_cache.get(cache_key)
    if cached is not None:
        return cached
    client = boto3.client(service_name, **kwargs)
    with _client_cache_lock:
        return _client_cache.setdefault(cache_key, client)


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
    return Config(max_pool_connections=max(10, 2 * int(getattr(settings, "CAMPAIGN_SEND_CONCURRENCY", 16) or 1)))


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
