import threading
from datetime import timedelta

import boto3
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

import taskdeck

_credential_cache = {}
_credential_cache_lock = threading.Lock()


def _role_credentials(task_type):
    role_arn = settings.RELAY_TASK_ROLE_ARNS.get(task_type, "")
    if not role_arn:
        if settings.RELAY_REQUIRE_TASK_ROLES:
            raise ImproperlyConfigured(
                f"Relay task type {task_type!r} has no configured IAM role."
            )
        return None

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


def aws_client(service_name, *, endpoint_url=None, task_type=None, region_name=None):
    kwargs = {
        "region_name": region_name or settings.AWS_REGION,
        "endpoint_url": endpoint_url if endpoint_url is not None else settings.AWS_ENDPOINT_URL or None,
    }
    if task_type:
        credentials = _role_credentials(task_type)
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
    return aws_client(
        "ses",
        endpoint_url=endpoint_url,
        task_type="email.send",
        region_name=settings.AWS_SES_REGION,
    )


def s3_client(*, endpoint_url=None):
    return aws_client("s3", endpoint_url=endpoint_url)
