"""Enqueue helpers for work that originates inside this application.

These replace the equivalent functions in ``mailing/sqs.py`` for the queues
whose only producer is Django. ``mailing/sqs.py`` stays as the transport for
the two queues that AWS services write into directly — SES notifications
arriving via SNS, and inbound mail arriving via object-storage events — which
cannot be replaced by an in-application queue.

Callers keep the same function names, so nothing downstream changes shape.
"""

import taskdeck


def enqueue_transactional_email(payload):
    from mailing import tasks  # noqa: PLC0415 - avoids service/enqueue import cycles

    result = tasks.send_transactional_email.enqueue(payload)
    # Stamped at enqueue rather than only inside the task body, so a queued
    # backlog shows what it is waiting on instead of a list of anonymous rows.
    taskdeck.stamp(
        result,
        entity=("transactional_message", payload["transactional_message_id"])
        if payload.get("transactional_message_id")
        else None,
        owner_id=payload.get("client_id"),
    )
    return result


def enqueue_campaign_email(payload):
    from mailing import tasks  # noqa: PLC0415 - avoids service/enqueue import cycles

    result = tasks.send_campaign_email_batch.enqueue(payload)
    recipient_ids = payload.get("recipient_ids") or []
    taskdeck.stamp(
        result,
        entity=("campaign", payload["campaign_id"]) if payload.get("campaign_id") else None,
        message=f"{len(recipient_ids)} recipients" if recipient_ids else None,
    )
    return result


def enqueue_transactional_email_batch(message_ids, *, list_key="", template_key="", client_id=None):
    """Queue one parent for a bulk send instead of one task per recipient.

    The parent fans out to the individual sends. Callers get a single run to
    watch, with progress, rather than a page of unrelated rows.
    """
    from mailing import tasks  # noqa: PLC0415 - avoids service/enqueue import cycles

    result = tasks.send_transactional_email_batch.enqueue(
        list(message_ids),
        list_key=list_key,
        template_key=template_key,
        client_id=client_id,
    )
    taskdeck.stamp(
        result,
        entity=("recipient_list", list_key) if list_key else None,
        owner_id=client_id,
        message=tasks._batch_message(template_key, len(message_ids)),
    )
    return result


def enqueue_ses_webhook(payload):
    """Queue an SES notification received over HTTP.

    Notifications delivered by SNS straight to the queue are drained by
    ``mailing/ingress.py`` instead; both paths converge on the same task.
    """
    from mailing import tasks  # noqa: PLC0415 - avoids service/enqueue import cycles

    return tasks.process_ses_webhook_event.enqueue(payload)
