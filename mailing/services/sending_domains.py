from email.utils import parseaddr

"""SES identity lifecycle behind Relay's provider boundary, never caller credentials."""
import dns.exception
import dns.resolver
from botocore.exceptions import BotoCoreError, ClientError
from django.utils import timezone

from mailing.aws import aws_client
from mailing.models import SendingDomain
from mailing.services.api_errors import ApiValidationError
from mailing.ses_routes import domain_of


def provider(row):
    return aws_client("sesv2", role_arn=row.role_arn, region_name=row.region)


def dmarc_status(domain):
    try:
        records = [
            "".join(part.decode() for part in answer.strings)
            for answer in dns.resolver.resolve("_dmarc." + domain, "TXT", lifetime=3)
        ]
    except (dns.exception.DNSException, UnicodeError):
        return {"status": "unknown", "records": []}
    records = [record for record in records if record.lower().startswith("v=dmarc1;")]
    return {"status": "published" if len(records) == 1 else "missing_or_invalid", "records": records}


def refresh(row, *, register=False):
    result = {}
    try:
        ses = provider(row)
        try:
            identity = ses.get_email_identity(EmailIdentity=row.name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NotFoundException" or not register:
                raise
            ses.create_email_identity(EmailIdentity=row.name)
            ses.put_email_identity_mail_from_attributes(
                EmailIdentity=row.name, MailFromDomain="mail." + row.name, BehaviorOnMxFailure="REJECT_MESSAGE"
            )
            identity = ses.get_email_identity(EmailIdentity=row.name)
        if register and not identity.get("MailFromAttributes", {}).get("MailFromDomain"):
            ses.put_email_identity_mail_from_attributes(
                EmailIdentity=row.name, MailFromDomain="mail." + row.name, BehaviorOnMxFailure="REJECT_MESSAGE"
            )
            identity = ses.get_email_identity(EmailIdentity=row.name)
        ses.get_configuration_set(ConfigurationSetName=row.configuration_set)
        dkim = identity.get("DkimAttributes", {})
        mail_from = identity.get("MailFromAttributes", {})
        result = {
            "verified": identity.get("VerifiedForSendingStatus", False),
            "verification_status": identity.get("VerificationStatus", "UNKNOWN"),
            "dkim_status": dkim.get("Status", "NOT_STARTED"),
            "dkim_tokens": dkim.get("Tokens", []),
            "mail_from_domain": mail_from.get("MailFromDomain", ""),
            "mail_from_status": mail_from.get("MailFromDomainStatus", "NOT_STARTED"),
            "dmarc": dmarc_status(row.name),
            "route_status": "available",
            "dns_records": [
                {"type": "CNAME", "name": token + "._domainkey." + row.name, "value": token + ".dkim.amazonses.com"}
                for token in dkim.get("Tokens", [])
            ]
            + [
                {
                    "type": "MX",
                    "name": mail_from.get("MailFromDomain") or "mail." + row.name,
                    "value": "10 feedback-smtp." + row.region + ".amazonses.com",
                },
                {
                    "type": "TXT",
                    "name": mail_from.get("MailFromDomain") or "mail." + row.name,
                    "value": "v=spf1 include:amazonses.com ~all",
                },
                {"type": "TXT", "name": "_dmarc." + row.name, "value": "v=DMARC1; p=none"},
            ],
        }
    except (BotoCoreError, ClientError) as exc:
        # Provider messages may contain request details; only expose the stable code.
        code = (
            exc.response.get("Error", {}).get("Code", "provider_error")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        result = {"verified": False, "route_status": "unavailable", "error": code}
    row.verification = result
    row.checked_at = timezone.now()
    row.save(update_fields=["verification", "checked_at"])
    return result


def validate_sender_domains(client, senders, *, previous=()):
    # Existing addresses remain usable while operators onboard legacy domains.
    # New/changed addresses must pass the provider check; display-name changes
    # on an existing mailbox do not require re-onboarding it.
    existing = {parseaddr(item.get("email", ""))[1].lower() for item in previous if isinstance(item, dict)}
    checked = {}
    for sender in senders:
        domain = domain_of(sender["email"])
        if parseaddr(sender["email"])[1].lower() in existing and not SendingDomain.objects.filter(name=domain).exists():
            continue
        row = SendingDomain.objects.filter(name=domain, organization_id=client.organization_id, enabled=True).first()
        if row is None or (row.clients.exists() and not row.clients.filter(pk=client.pk).exists()):
            raise ApiValidationError({"senders": f"domain_not_registered: {domain}"})
        status = checked.setdefault(domain, None)
        if status is None:
            status = checked[domain] = refresh(row)
        if status.get("route_status") != "available":
            raise ApiValidationError({"senders": f"domain_unrouted: {domain} ({status.get('error', 'unknown')})"})
        if not status.get("verified") or status.get("dkim_status") != "SUCCESS":
            raise ApiValidationError({"senders": f"domain_unverified: {domain}"})
