"""Enqueue helpers for work that originates inside this application.

These replace the equivalent functions in ``mailing/sqs.py`` for the queues
whose only producer is Django. ``mailing/sqs.py`` stays as the transport for
the two queues that AWS services write into directly — SES notifications
arriving via SNS, and inbound mail arriving via object-storage events — which
cannot be replaced by an in-application queue.

Callers keep the same function names, so nothing downstream changes shape.
"""

from mailing import tasks


def enqueue_transactional_email(payload):
    return tasks.send_transactional_email.enqueue(payload)


def enqueue_campaign_email(payload):
    return tasks.send_campaign_email_batch.enqueue(payload)


def enqueue_ses_webhook(payload):
    """Queue an SES notification received over HTTP.

    Notifications delivered by SNS straight to the queue are drained by
    ``mailing/ingress.py`` instead; both paths converge on the same task.
    """
    return tasks.process_ses_webhook_event.enqueue(payload)
