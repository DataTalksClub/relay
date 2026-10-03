import logging
import traceback
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import ses_client as default_ses_client
from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    EmailEvent,
    EmailEventType,
)
from mailing.services.campaign_rendering import (
    build_campaign_html_body as build_campaign_html_body,
)
from mailing.services.campaign_rendering import (
    build_campaign_text_body as build_campaign_text_body,
)
from mailing.services.campaign_rendering import (
    rewrite_html_links as rewrite_html_links,
)
from mailing.services.tokens import (
    CampaignRecipientTokens,
    ensure_campaign_recipient_tokens,
    generate_raw_token,
    token_hash,
)
from mailing.ses import send_email

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    CampaignRecipientStatus.SENT,
    CampaignRecipientStatus.SKIPPED,
    CampaignRecipientStatus.FAILED,
    CampaignRecipientStatus.BOUNCED,
    CampaignRecipientStatus.COMPLAINED,
    CampaignRecipientStatus.UNSUBSCRIBED,
}

TRANSIENT_SES_ERROR_CODES = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "InternalError",
    "InternalFailure",
}


class CampaignSenderError(Exception):
    pass


class RetryableCampaignSendError(CampaignSenderError):
    pass


@dataclass(frozen=True)
class CampaignSendResult:
    sent_count: int
    skipped_count: int
    failed_count: int


def render_campaign_message(
    campaign, *, tracking_token="preview-tracking-token", unsubscribe_token="preview-unsubscribe-token"
):
    return {
        "subject": campaign.subject,
        "preview_text": campaign.preview_text,
        "html_body": build_campaign_html_body(campaign.html_body, tracking_token, unsubscribe_token),
        "text_body": build_campaign_text_body(campaign.text_body, unsubscribe_token),
    }


def send_campaign_test_message(campaign, to_email, *, ses_client=None, source=None):
    rendered = render_campaign_message(
        campaign,
        tracking_token=generate_raw_token(),
        unsubscribe_token=generate_raw_token(),
    )
    return send_email(
        ses_client=ses_client or default_ses_client(),
        source=source or settings.DEFAULT_FROM_EMAIL,
        to_email=to_email,
        subject=rendered["subject"],
        html_body=rendered["html_body"],
        text_body=rendered["text_body"],
    )


def send_campaign_batch(payload, *, ses_client=None):
    campaign_id = payload["campaign_id"]
    recipient_ids = payload["campaign_recipient_ids"]
    ses = ses_client or default_ses_client()
    result = {"sent_count": 0, "skipped_count": 0, "failed_count": 0}

    if not Campaign.objects.filter(pk=campaign_id).exists():
        raise CampaignSenderError(f"campaign {campaign_id} does not exist")

    recipients = CampaignRecipient.objects.filter(pk__in=recipient_ids).only("id", "campaign_id")
    found_recipient_ids = {recipient.id for recipient in recipients}
    missing_recipient_ids = sorted(set(recipient_ids) - found_recipient_ids)
    wrong_campaign_ids = sorted(recipient.id for recipient in recipients if recipient.campaign_id != campaign_id)
    if missing_recipient_ids or wrong_campaign_ids:
        raise CampaignSenderError(
            f"invalid campaign recipient ids: missing={missing_recipient_ids}, wrong_campaign={wrong_campaign_ids}"
        )

    for recipient_id in recipient_ids:
        try:
            outcome = _send_campaign_recipient(campaign_id, recipient_id, ses)
        except RetryableCampaignSendError as exc:
            _record_retryable_error(campaign_id, recipient_id, str(exc))
            refresh_campaign_send_counts(campaign_id)
            raise
        result[f"{outcome}_count"] += 1

    refresh_campaign_send_counts(campaign_id)
    return CampaignSendResult(**result)


@transaction.atomic
def _send_campaign_recipient(campaign_id, recipient_id, ses):
    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related("campaign", "campaign__client", "campaign__audience", "contact")
        .get(pk=recipient_id, campaign_id=campaign_id)
    )

    if recipient.status in TERMINAL_STATUSES or recipient.ses_message_id:
        return "skipped"

    if recipient.status != CampaignRecipientStatus.PENDING:
        return "skipped"

    if recipient.campaign.status == CampaignStatus.CANCELLED:
        recipient.status = CampaignRecipientStatus.SKIPPED
        recipient.last_error = "campaign_cancelled"
        recipient.save(update_fields=["status", "last_error", "updated_at"])
        _create_campaign_event(recipient, EmailEventType.SKIPPED, metadata={"reason": "campaign_cancelled"})
        return "skipped"

    tokens = _send_tokens_for_pending_recipient(recipient)
    return _deliver_campaign_recipient(recipient, ses, tokens)


def _deliver_campaign_recipient(recipient, ses, tokens):
    html_body = build_campaign_html_body(recipient.campaign.html_body, tokens.tracking_token, tokens.unsubscribe_token)
    text_body = build_campaign_text_body(recipient.campaign.text_body, tokens.unsubscribe_token)

    # SES clients can raise transport, service, or unexpected adapter errors;
    # preserve the catch-all delivery contract at this external boundary.
    try:
        message_id = send_email(
            ses_client=ses,
            source=settings.DEFAULT_FROM_EMAIL,
            to_email=recipient.email,
            subject=recipient.campaign.subject,
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        retryable = is_retryable_send_error(exc)
        _log_campaign_delivery_error(recipient, exc, retryable)
        if retryable:
            raise RetryableCampaignSendError(str(exc)) from exc
        _mark_recipient_failed(recipient, str(exc))
        return "failed"

    _complete_campaign_recipient(recipient, message_id)
    return "sent"


def _log_campaign_delivery_error(recipient, exc, retryable):
    # Copy only stack locations: provider text, chains, source lines, and locals
    # may contain recipient content or credentials. Bound the emitted stack.
    diagnostic = traceback.TracebackException(RuntimeError, RuntimeError("SES delivery error details omitted"), None)
    frames = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        frames.append((frame.f_code.co_filename, lineno, frame.f_code.co_name, ""))
    diagnostic.stack = traceback.StackSummary.from_list(frames[-10:])
    classification = "permanent"
    if retryable:
        classification = "retryable"
    logger.error(
        "Campaign delivery failed campaign_id=%s recipient_id=%s classification=%s\n%s",
        recipient.campaign_id,
        recipient.pk,
        classification,
        "".join(diagnostic.format(chain=False)),
    )


def _complete_campaign_recipient(recipient, message_id):
    now = timezone.now()
    recipient.status = CampaignRecipientStatus.SENT
    recipient.ses_message_id = message_id
    recipient.sent_at = now
    recipient.last_error = ""
    recipient.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.SENT, metadata={"ses_message_id": message_id})


def is_retryable_send_error(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, BotoCoreError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in TRANSIENT_SES_ERROR_CODES or status_code >= 500
    return False


def refresh_campaign_send_counts(campaign_id):
    sent_count = CampaignRecipient.objects.filter(campaign_id=campaign_id, status=CampaignRecipientStatus.SENT).count()
    failed_count = CampaignRecipient.objects.filter(
        campaign_id=campaign_id,
        status=CampaignRecipientStatus.FAILED,
    ).count()
    updates = {"sent_count": sent_count, "updated_at": timezone.now()}
    if sent_count:
        pending_exists = CampaignRecipient.objects.filter(
            campaign_id=campaign_id,
            status=CampaignRecipientStatus.PENDING,
        ).exists()
        if not pending_exists:
            updates["sent_at"] = timezone.now()
    Campaign.objects.filter(pk=campaign_id).update(**updates)
    return {"sent_count": sent_count, "failed_count": failed_count}


def _mark_recipient_failed(recipient, error_message):
    recipient.status = CampaignRecipientStatus.FAILED
    recipient.last_error = error_message[:2000]
    recipient.save(update_fields=["status", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.FAILED, metadata={"error": recipient.last_error})


def _send_tokens_for_pending_recipient(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    if tokens.tracking_token and tokens.unsubscribe_token:
        return tokens

    # Raw tokens are intentionally not stored by #10. If a retry reaches a
    # pending, unsent recipient with only hashes, no delivered email can depend
    # on those URLs yet, so rotating the hashes preserves that security model
    # while making the transient SES failure retryable.
    tracking_token = generate_raw_token()
    unsubscribe_token = generate_raw_token()
    recipient.tracking_token_hash = token_hash(tracking_token)
    recipient.unsubscribe_token_hash = token_hash(unsubscribe_token)
    recipient.save(update_fields=["tracking_token_hash", "unsubscribe_token_hash", "updated_at"])
    return CampaignRecipientTokens(
        tracking_token=tracking_token,
        unsubscribe_token=unsubscribe_token,
    )


def _record_retryable_error(campaign_id, recipient_id, error_message):
    CampaignRecipient.objects.filter(
        pk=recipient_id,
        campaign_id=campaign_id,
        status=CampaignRecipientStatus.PENDING,
    ).update(last_error=error_message[:2000], updated_at=timezone.now())


def _create_campaign_event(recipient, event_type, *, metadata=None):
    return EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=event_type,
        metadata=metadata or {},
    )
