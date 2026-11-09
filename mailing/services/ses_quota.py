"""SES account quota/usage for every account+region pair Relay sends through."""

import re
import time
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from mailing.aws import aws_client
from mailing.models import SendingDomain
from mailing.ses_routes import parse_ses_domain_routes

CACHE_TTL_SECONDS = 60
PERMISSION_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException"}
REQUIRED_IAM_ACTIONS = ["ses:GetAccount", "ses:GetSendQuota", "ses:GetSendStatistics"]

_cache = {"expires": 0.0, "rows": None}


@dataclass(frozen=True)
class SesPair:
    role_arn: str
    region: str
    sources: tuple


def account_label(role_arn):
    match = re.match(r"arn:aws[a-z-]*:iam::(\d{12}):", role_arn or "")
    return match.group(1) if match else "runtime credentials"


def sending_pairs():
    """Every (role, region) pair Relay can send through, with what routes to it."""
    pairs = {}

    def add(role_arn, region, source):
        pairs.setdefault((role_arn, region), []).append(source)

    default_role = settings.RELAY_TASK_ROLE_ARNS.get("email.send", "")
    add(default_role, settings.AWS_SES_REGION, "default sender")
    for domain, route in parse_ses_domain_routes(settings.RELAY_SES_DOMAIN_ROUTES).items():
        add(route.role_arn, route.region, f"route:{domain}")
    for row in SendingDomain.objects.filter(enabled=True).order_by("name"):
        add(row.role_arn, row.region, f"domain:{row.name}")
    return [SesPair(role, region, tuple(sources)) for (role, region), sources in pairs.items()]


def _percent(used, limit):
    return round(100 * used / limit, 1) if limit else None


def fetch_row(pair):
    row = {
        "account": account_label(pair.role_arn),
        "region": pair.region,
        "sources": list(pair.sources),
        "configured_max_send_rate": settings.SES_MAX_SEND_RATE_PER_SECOND,
        "status": "ok",
        "error": "",
    }
    try:
        client = aws_client("sesv2", role_arn=pair.role_arn or None, region_name=pair.region)
        account = client.get_account()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        denied = code in PERMISSION_CODES
        row |= {"status": "no_permission" if denied else "error", "error": code or "ClientError"}
        if denied:
            row["required_iam_actions"] = REQUIRED_IAM_ACTIONS
        return row
    except BotoCoreError as exc:
        return row | {"status": "error", "error": type(exc).__name__}
    quota = account.get("SendQuota", {})
    sent = quota.get("SentLast24Hours")
    maximum = quota.get("Max24HourSend")
    return row | {
        "max_24h_send": maximum,
        "max_send_rate": quota.get("MaxSendRate"),
        "sent_last_24h": sent,
        "percent_used": _percent(sent or 0, maximum),
        "production_access": account.get("ProductionAccessEnabled"),
        "enforcement_status": account.get("EnforcementStatus"),
    }


def ses_quota_rows(*, refresh=False):
    now = time.monotonic()
    if not refresh and _cache["rows"] is not None and _cache["expires"] > now:
        return _cache["rows"]
    rows = [fetch_row(pair) for pair in sending_pairs()]
    _cache.update(rows=rows, expires=now + CACHE_TTL_SECONDS)
    return rows
