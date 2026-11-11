"""Opaque, scoped Mailchimp source identities shared by import and erasure."""

import hashlib
import json
import re

from django.db import connection

from mailing.models import OperatorAudit

ERASURE_ACTION = "mailchimp.source_history.erased"
ERASURE_TARGET = "mailchimp_source_identity"


def validate_source_scope(client, audience):
    if not client.pk or not audience.pk or client.organization_id != audience.organization_id:
        raise ValueError("Mailchimp source erasure scope is invalid")


def lock_source_history_scope(client, audience):
    """Serialize history import/erasure without locking live sending's client row."""
    validate_source_scope(client, audience)
    if not connection.in_atomic_block:
        raise ValueError("Mailchimp source history lock requires an atomic transaction")
    if connection.vendor == "postgresql":
        namespace = json.dumps(["relay.mailchimp.history.scope", client.pk, audience.pk],
                               separators=(",", ":")).encode("utf-8")
        key = int.from_bytes(hashlib.sha256(namespace).digest()[:8], "big", signed=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def source_identity_key(datacenter, list_id, email_id):
    """Hash the exact source namespace, without retaining an address/member ID."""
    if (not isinstance(datacenter, str) or len(datacenter) > 32
            or not re.fullmatch(r"[a-z]{2}\d+", datacenter)):
        raise ValueError("Mailchimp source identity requires a valid account datacenter")
    for value, limit in ((datacenter, 32), (list_id, 255), (email_id, 255)):
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Mailchimp source identity requires bounded nonempty scalar IDs")
    value = json.dumps(["mailchimp_source_identity", datacenter, list_id, email_id],
                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def erased_source_keys(client, audience):
    """Return tombstones for one exact Relay client/audience scope."""
    validate_source_scope(client, audience)
    records = OperatorAudit.objects.filter(
        action=ERASURE_ACTION, target_type=ERASURE_TARGET, target_id=0,
        metadata__client_id=client.pk, metadata__audience_id=audience.pk,
    ).values_list("metadata", flat=True)
    return {record["source_key_hash"] for record in records
            if isinstance(record.get("source_key_hash"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["source_key_hash"])}
