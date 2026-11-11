"""Erase the Relay copy of an exact Mailchimp source's historical clicks.

The restricted raw archive has separate retention; this service does not erase
that archive, contacts, consent, suppression, or delivery state.
"""

import re

from django.db import transaction

from mailing.models import EmailEvent, EmailEventType, OperatorAudit
from mailing.services.mailchimp_source_identity import (
    ERASURE_ACTION,
    ERASURE_TARGET,
    erased_source_keys,
    lock_source_history_scope,
    source_identity_key,
    validate_source_scope,
)


class MailchimpSourceErasureError(ValueError):
    """A bounded, console-safe refusal to claim an ambiguous erasure succeeded."""


@transaction.atomic
def erase_mailchimp_source_clicks(*, client, audience, datacenter, list_id, email_id, apply=False):
    """Default to a count-only dry-run; apply preserves event idempotency IDs."""
    source_key = source_identity_key(datacenter, list_id, email_id)
    validate_source_scope(client, audience)
    if apply:
        # Only history import/erasure operations share this advisory namespace.
        lock_source_history_scope(client, audience)
    source_events = EmailEvent.objects.filter(
        client=client, audience=audience, event_type=EmailEventType.CLICK,
        metadata__provider="mailchimp", metadata__historical_import=True,
        metadata__mailchimp__list_id=list_id, metadata__mailchimp__email_id=email_id,
    )
    # Intact legacy imports omitted the account datacenter. Never tombstone an
    # identity while leaving an ambiguous older copy visible in Relay history.
    for source_dc in source_events.values_list("metadata__mailchimp__datacenter", flat=True).iterator(chunk_size=2000):
        if (not isinstance(source_dc, str) or len(source_dc) > 32
                or not re.fullmatch(r"[a-z]{2}\d+", source_dc)):
            raise MailchimpSourceErasureError(
                "Legacy Mailchimp source datacenter is unverifiable; verified legacy source reconciliation is required before erasure"
            )
    tombstoned = source_key in erased_source_keys(client, audience)
    events = source_events.filter(metadata__mailchimp__datacenter=datacenter)
    matched = events.count()
    erased = 0
    if apply:
        erased = events.update(contact=None, url="", metadata={
            "provider": "mailchimp", "historical_import": True,
            "erased": True, "source_history_erased": True, "mailchimp_source_key_hash": source_key,
        })
        if not tombstoned:
            OperatorAudit.objects.create(
                action=ERASURE_ACTION, target_type=ERASURE_TARGET, target_id=0,
                metadata={"client_id": client.pk, "audience_id": audience.pk, "source_key_hash": source_key},
            )
    return {"dry_run": not apply, "matched": matched, "erased": erased,
            "tombstoned": tombstoned or apply}
