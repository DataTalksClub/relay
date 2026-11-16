import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import ses_client_for_source
from mailing.db_retry import run_with_db_retry
from mailing.dry_run import DryRunSuppressed
from mailing.models import EmailEvent, EmailEventType, TransactionalMessage, TransactionalMessageStatus
from mailing.services import send_dedupe
from mailing.services.client_callbacks import emit_client_callback
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.ses import send_email
from mailing.ses_rate_limiter import TRANSACTIONAL, SesRateLimiterUnavailable
from mailing.ses_routes import configuration_set_for_source

logger = logging.getLogger(__name__)

TERMINAL_ACK_STATUSES = {
    TransactionalMessageStatus.SENT,
    TransactionalMessageStatus.SKIPPED,
    TransactionalMessageStatus.FAILED,
    TransactionalMessageStatus.BOUNCED,
    TransactionalMessageStatus.COMPLAINED,
    TransactionalMessageStatus.DUPLICATE_BLOCKED,
}

PERMANENT_SES_ERROR_CODES = {
    "ConfigurationSetDoesNotExistException",
    "InvalidParameterCombination",
    "InvalidParameterValue",
    "InvalidParameterValueException",
    "MailFromDomainNotVerifiedException",
    "MessageRejected",
    "ValidationError",
}


class TransactionalMessageNotFound(RuntimeError):
    """Raised for valid queue records whose source-of-truth row is unavailable."""


class TransientSendFailure(RuntimeError):
    """Raised when Lambda should return this SQS record for retry."""


def send_transactional_email_from_queue(payload, *, client=None, source=None):
    message_id = payload["transactional_message_id"]

    try:
        message = _claim_message_for_send(payload)

        if message.status in TERMINAL_ACK_STATUSES:
            return message

        if message.status == TransactionalMessageStatus.SENDING and not getattr(message, "_claimed_for_send", False):
            raise TransientSendFailure(f"transactional message {message.id} is already sending")

        source = source or message.from_email or message.client.default_from_email or settings.DEFAULT_FROM_EMAIL
        configuration_set = None
        if client is None:
            client = ses_client_for_source(source)
            configuration_set = configuration_set_for_source(source)
        blocked = _reserve_content(message)
        if blocked is not None:
            return _mark_duplicate_blocked(message.id, blocked)
        try:
            forward_options = {}
            if message.inbound_source_id:
                from mailing.services.block_links import forward_footer  # noqa: PLC0415
                from mailing.services.inbound_routing import load_forward_mime  # noqa: PLC0415
                forward_options["original_mime"] = load_forward_mime(message)
                try:
                    forward_options["forward_footer"] = forward_footer(message.inbound_source, forward_options["original_mime"])
                except Exception as exc:
                    # Forward without block links rather than fail the forward.
                    logger.warning("forward footer unavailable message_id=%s error_type=%s", message.id, type(exc).__name__)
            ses_message_id = send_email(
                ses_client=client,
                configuration_set=configuration_set,
                source=source,
                to_email=message.email,
                subject=message.subject,
                html_body=message.html_body,
                text_body=message.text_body,
                reply_to=message.metadata.get("reply_to", ""),
                cc=message.metadata.get("cc", []),
                bcc=message.metadata.get("bcc", []),
                headers=message.metadata.get("headers", {}),
                message_parts=message.metadata.get("message_parts", []),
                calendar_alternative=message.metadata.get("calendar_alternative"),
                kind=TRANSACTIONAL,
                **forward_options,
            )
        except SesRateLimiterUnavailable as exc:
            # Fail-closed limiter: SES never saw the message, so requeueing
            # for a later retry cannot duplicate it.
            _mark_retryable(message.id, str(exc))
            raise TransientSendFailure(str(exc)) from exc
        except ClientError as exc:
            if _is_permanent_client_error(exc):
                send_dedupe.release("transactional", [message.id])  # SES rejected it: a corrected resend is legitimate
                return _mark_failed(
                    message.id,
                    _error_message(exc),
                    metadata={"reason": "ses_permanent_failure"},
                )
            _mark_retryable(message.id, _error_message(exc))
            raise TransientSendFailure(_error_message(exc)) from exc
        except BotoCoreError as exc:
            _mark_retryable(message.id, _error_message(exc))
            raise TransientSendFailure(_error_message(exc)) from exc

        sent_at = timezone.now()
        send_dedupe.mark_sent("transactional", [message.id])
        return _mark_sent(
            message.id,
            ses_message_id,
            {"ses_message_id": ses_message_id, "sent_at": sent_at.isoformat()},
        )
    except TransactionalMessage.DoesNotExist as exc:
        raise TransactionalMessageNotFound(f"transactional message {message_id} was not found") from exc
    except TransientSendFailure as exc:
        _record_retryable_error(message_id, str(exc))
        raise
    except DryRunSuppressed as exc:
        # Covers both the client factory and the send call: dry run suppresses
        # the transport, the record is acknowledged durably, and nothing
        # redrives.
        send_dedupe.release("transactional", [message_id])
        return _mark_skipped_dry_run(message_id, str(exc))


def _message_content_hash(message):
    if message.inbound_source_id:
        # A forward's body is the original MIME, not subject/html/text: key on the inbound message itself.
        return send_dedupe.content_hash("forward", str(message.inbound_source_id), message.subject)
    return send_dedupe.content_hash(message.subject, message.html_body, message.text_body)


def _reserve_content(message):
    """Atomic duplicate guard before the SES call; returns a Blocked record or None to proceed."""
    if not send_dedupe.dedupe_enabled():
        return None
    window = send_dedupe.transactional_window_seconds(message.template)
    if window == 0:
        return None  # template opted out: identical repeats (OTP, password reset) are legitimate
    chash = _message_content_hash(message)
    reserved, blocked = send_dedupe.reserve(
        message.client_id,
        [send_dedupe.Candidate(key=message.id, email=message.email, content_hash=chash)],
        kind="transactional", window_seconds=window,
    )
    return blocked.get(message.id)


def _mark_duplicate_blocked(message_id, blocked):
    logger.error(
        "send_duplicate_blocked transactional_message_id=%s content_hash=%s first_state=%s",
        message_id, blocked.content_hash, blocked.first_state,
    )

    def _attempt():
        with transaction.atomic():
            message = _locked_messages().get(id=message_id)
            if message.status in TERMINAL_ACK_STATUSES:
                return message
            message.status = TransactionalMessageStatus.DUPLICATE_BLOCKED
            message.last_error = "duplicate_blocked"
            message.save(update_fields=["status", "last_error", "updated_at"])
            _append_event(message, EmailEventType.DUPLICATE_BLOCKED, {
                "reason": "duplicate_blocked", "content_hash": blocked.content_hash,
                "first_state": blocked.first_state,
                "first_at": blocked.first_at.isoformat() if blocked.first_at else None,
                "first_transactional_message_id": blocked.first_transactional_message_id,
            })
            return message

    return run_with_db_retry(
        _attempt, action=f"mark transactional message {message_id} duplicate_blocked",
        attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0,
    )


def _is_permanent_client_error(exc):
    return exc.response.get("Error", {}).get("Code", "") in PERMANENT_SES_ERROR_CODES


def _locked_messages():
    """TransactionalMessage rows locked without pinning related rows.

    ``select_related`` joins client/contact for use after the lock, but the
    ``FOR UPDATE`` clause must cover only the message table
    (``of=("self",)``). Locking the joined contact row as well deadlocks
    when two concurrent sends touch the same contact: each holds its own
    message lock and waits on the other's contact lock (2026-10-26 prod).
    """
    return TransactionalMessage.objects.select_for_update(of=("self",)).select_related("client", "contact")


def _claim_message_for_send(payload):
    def _attempt():
        with transaction.atomic():
            message = _locked_messages().get(id=payload["transactional_message_id"])

            if message.status in TERMINAL_ACK_STATUSES:
                return message

            if message.client_id != payload["client_id"] or message.idempotency_key != payload["idempotency_key"]:
                return _mark_failed(
                    message.id,
                    "queue payload does not match transactional message client_id/idempotency_key",
                    metadata={
                        "reason": "queue_payload_mismatch",
                        "queued_client_id": payload["client_id"],
                        "queued_idempotency_key": payload["idempotency_key"],
                    },
                )

            if message.status == TransactionalMessageStatus.SENDING:
                return message

            if message.status != TransactionalMessageStatus.QUEUED:
                _record_retryable_error(
                    message.id,
                    f"transactional message status is not sendable: {message.status}",
                )
                raise TransientSendFailure(f"transactional message status is not sendable: {message.status}")

            message.status = TransactionalMessageStatus.SENDING
            message.last_error = ""
            message.save(update_fields=["status", "last_error", "updated_at"])
            message._claimed_for_send = True
            return message

    return run_with_db_retry(
        _attempt,
        action=f"claim transactional message {payload['transactional_message_id']}",
        attempts=3,
        base_delay_seconds=0.2,
        max_delay_seconds=2.0,
    )


def _mark_sent(message_id, ses_message_id, metadata):
    def _attempt():
        with transaction.atomic():
            message = _locked_messages().get(id=message_id)
            if message.status in TERMINAL_ACK_STATUSES:
                return message

            sent_at = timezone.now()
            message.status = TransactionalMessageStatus.SENT
            message.ses_message_id = ses_message_id
            message.sent_at = sent_at
            message.last_error = ""
            message.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
            _append_event(message, EmailEventType.SENT, metadata)
            return message

    # The SES send already happened above; a deadlock or DNS blip here must
    # not fail the task (and trip the host watchdog) when a retry succeeds.
    return run_with_db_retry(
        _attempt,
        action=f"mark transactional message {message_id} sent",
        attempts=3,
        base_delay_seconds=0.2,
        max_delay_seconds=2.0,
    )


def _mark_skipped_dry_run(message_id, reason):
    def _attempt():
        with transaction.atomic():
            message = _locked_messages().get(id=message_id)
            if message.status in TERMINAL_ACK_STATUSES:
                return message

            metadata = dict(message.metadata or {})
            metadata["dry_run"] = True
            metadata["skip_reason"] = "dry_run"
            message.metadata = metadata
            message.status = TransactionalMessageStatus.SKIPPED
            # ses_message_id stays empty: dry run never contacts SES, so there is
            # no message id to record and the status must not claim a send.
            message.save(update_fields=["status", "metadata", "updated_at"])
            _append_event(message, EmailEventType.SKIPPED, {"reason": "dry_run", "error": reason})
            return message

    return run_with_db_retry(
        _attempt,
        action=f"mark transactional message {message_id} skipped (dry run)",
        attempts=3,
        base_delay_seconds=0.2,
        max_delay_seconds=2.0,
    )


def _mark_failed(message_id, error, *, metadata):
    with transaction.atomic():
        message = _locked_messages().get(id=message_id)
        if message.status in TERMINAL_ACK_STATUSES:
            return message

        message.status = TransactionalMessageStatus.FAILED
        message.last_error = error
        message.save(update_fields=["status", "last_error", "updated_at"])
        _append_event(message, EmailEventType.FAILED, metadata | {"error": error})
        return message


def _mark_retryable(message_id, error):
    TransactionalMessage.objects.filter(
        id=message_id,
        status=TransactionalMessageStatus.SENDING,
    ).update(
        status=TransactionalMessageStatus.QUEUED,
        last_error=error,
        updated_at=timezone.now(),
    )


def _record_retryable_error(message_id, error):
    TransactionalMessage.objects.filter(id=message_id, status=TransactionalMessageStatus.QUEUED).update(
        last_error=error,
        updated_at=timezone.now(),
    )


def _append_event(message, event_type, metadata):
    event = EmailEvent.objects.create(
        transactional_message=message,
        contact=message.contact,
        client=message.client,
        event_type=event_type,
        metadata=metadata,
    )
    emit_cmp_contact_event(event)
    emit_client_callback(event)
    return event


def _error_message(exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "ClientError")
        message = error.get("Message", str(exc))
        return f"{code}: {message}"
    return str(exc)
