import threading
from datetime import timedelta

import boto3
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

import taskdeck
from mailing.dry_run import DryRunGuardClient, dry_run_enforced
from mailing.ses_routes import route_for_source

_credential_cache = {}
_credential_cache_lock = threading.Lock()


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


def aws_client(service_name, *, endpoint_url=None, task_type=None, role_arn=None, region_name=None):
    kwargs = {
        "region_name": region_name or settings.AWS_REGION,
        "endpoint_url": endpoint_url if endpoint_url is not None else settings.AWS_ENDPOINT_URL or None,
    }
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


def sqs_client(*, endpoint_url=None):
    return aws_client("sqs", endpoint_url=endpoint_url)


def ses_client(*, endpoint_url=None):
    if dry_run_enforced():
        # Inert transport guard, not a boto3 client: no STS, no network, and
        # no raise here — suppression surfaces at the send gate so pre-loop
        # factory resolution (e.g. campaign batches) stays non-raising.
        return DryRunGuardClient()
    return aws_client(
        "ses",
        endpoint_url=endpoint_url,
        task_type="email.send",
        region_name=settings.AWS_SES_REGION,
    )


def ses_client_for_source(source, *, endpoint_url=None):
    if dry_run_enforced():
        return DryRunGuardClient()
    route = route_for_source(source)
    if route is None:
        return ses_client(endpoint_url=endpoint_url)
    return aws_client(
        "ses",
        endpoint_url=endpoint_url,
        role_arn=route.role_arn,
        region_name=route.region,
    )


def s3_client(*, endpoint_url=None):
    return aws_client("s3", endpoint_url=endpoint_url)
