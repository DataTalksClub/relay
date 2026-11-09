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
REQUIRED_IAM_ACTIONS = ["ses:GetAccount", "ses:GetSendQuota", "ses:GetSendStatistics", "ses:ListEmailIdentities"]

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
    """Every (role, region) Relay can reach: each credential set it holds x each region of interest."""
    used = {}

    def add(role_arn, region, source):
        used.setdefault((role_arn, region), []).append(source)

    add(settings.RELAY_TASK_ROLE_ARNS.get("email.send", ""), settings.AWS_SES_REGION, "default sender")
    for domain, route in parse_ses_domain_routes(settings.RELAY_SES_DOMAIN_ROUTES).items():
        add(route.role_arn, route.region, f"route:{domain}")
    for row in SendingDomain.objects.filter(enabled=True).order_by("name"):
        add(row.role_arn, row.region, f"domain:{row.name}")
    roles = list(dict.fromkeys(role for role, _region in used))
    regions = list(dict.fromkeys([*(region for _role, region in used), *settings.RELAY_SES_QUOTA_REGIONS]))
    return [SesPair(role, region, tuple(used.get((role, region), ()))) for role in roles for region in regions]


def account_id(client, role_arn):
    match = re.match(r"arn:aws[a-z-]*:iam::(\d{12}):", role_arn or "")
    if match:
        return match.group(1)
    try:
        return client.get_caller_identity()["Account"]
    except (BotoCoreError, ClientError):
        return "runtime credentials"


def list_identities(client):
    identities, token = [], None
    while True:
        response = client.list_email_identities(PageSize=1000, **({"NextToken": token} if token else {}))
        identities += [
            {
                "name": item["IdentityName"],
                "type": "domain" if item.get("IdentityType") == "DOMAIN" else "email",
                "verification": item.get("VerificationStatus", "UNKNOWN"),
                "sending_enabled": item.get("SendingEnabled"),
            }
            for item in response.get("EmailIdentities", [])
        ]
        token = response.get("NextToken")
        if not token:
            return sorted(identities, key=lambda i: (i["type"], i["name"]))


def _percent(used, limit):
    return round(100 * used / limit, 1) if limit else None


def _denied(row, exc):
    code = exc.response.get("Error", {}).get("Code", "")
    denied = code in PERMISSION_CODES
    row |= {"status": "no_permission" if denied else "error", "error": code or "ClientError"}
    if denied:
        row["required_iam_actions"] = REQUIRED_IAM_ACTIONS
    return row


def fetch_row(pair):
    row = {
        "account": account_label(pair.role_arn),
        "region": pair.region,
        "sources": list(pair.sources),
        "configured_max_send_rate": settings.SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND,
        "status": "ok",
        "error": "",
        "identities": [],
        "identities_status": "ok",
    }
    try:
        client = aws_client("sesv2", role_arn=pair.role_arn or None, region_name=pair.region)
        account = client.get_account()
    except ClientError as exc:
        return _denied(row, exc)
    except BotoCoreError as exc:
        return row | {"status": "error", "error": type(exc).__name__}
    if not pair.role_arn:
        row["account"] = account_id(aws_client("sts", region_name=pair.region), "")
    try:
        row["identities"] = list_identities(client)
    except ClientError as exc:
        row["identities_status"] = _denied({}, exc)["status"]
    except BotoCoreError:
        row["identities_status"] = "error"
    quota = account.get("SendQuota", {})
    sent = quota.get("SentLast24Hours")
    maximum = quota.get("Max24HourSend")
    production = account.get("ProductionAccessEnabled")
    return row | {
        "max_24h_send": maximum,
        "max_send_rate": quota.get("MaxSendRate"),
        "sent_last_24h": sent,
        "percent_used": _percent(sent or 0, maximum),
        "production_access": production,
        "mode": "production" if production else "SES sandbox",
        "enforcement_status": account.get("EnforcementStatus"),
    }


def ses_quota_rows(*, refresh=False):
    now = time.monotonic()
    if not refresh and _cache["rows"] is not None and _cache["expires"] > now:
        return _cache["rows"]
    rows = [fetch_row(pair) for pair in sending_pairs()]
    _cache.update(rows=rows, expires=now + CACHE_TTL_SECONDS)
    return rows
