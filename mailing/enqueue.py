"""Enqueue helpers for work that originates inside this application.

These replace the equivalent functions in ``mailing/sqs.py`` for the queues
whose only producer is Django. ``mailing/sqs.py`` stays as the transport for
the two queues that AWS services write into directly — SES notifications
arriving via SNS, and inbound mail arriving via object-storage events — which
cannot be replaced by an in-application queue.

Callers keep the same function names, so nothing downstream changes shape.
"""

import taskdeck

from mailing import tasks


def enqueue_transactional_email(payload):
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
    result = tasks.send_campaign_email_batch.enqueue(payload)
    recipient_ids = payload.get("recipient_ids") or []
    taskdeck.stamp(
        result,
        entity=("campaign", payload["campaign_id"]) if payload.get("campaign_id") else None,
        message=f"{len(recipient_ids)} recipients" if recipient_ids else None,
    )
    return result


def enqueue_ses_webhook(payload):
    """Queue an SES notification received over HTTP.

    Notifications delivered by SNS straight to the queue are drained by
    ``mailing/ingress.py`` instead; both paths converge on the same task.
    """
    return tasks.process_ses_webhook_event.enqueue(payload)
