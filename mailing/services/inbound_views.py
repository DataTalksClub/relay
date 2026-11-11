"""Query and command helpers for the inbound mailbox.

Kept out of views.py for the same reason every other view model is: the console
is a client-scoped operator surface, and inbound is not client-scoped, so these
have to be the part of the mailbox that does not care which tenant is selected.
"""

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from mailing.models import (
    BlockedSender,
    DmarcArchive,
    InboundAddress,
    InboundMessage,
    InboundMessageState,
)
from mailing.services.dmarc import DMARC_SENDERS

# The default view is the unread mail, because that is what an operator opening
# the mailbox is looking for. Read mail is still a click away and is not
# discarded by a filter anywhere.
DEFAULT_STATE = InboundMessageState.RECEIVED

BLOCK_ORIGIN_BUTTON = "mark_as_spam"
BLOCK_ORIGIN_MANUAL = "manual"


def normalize_address(value):
    return (value or "").strip().lower()


def address_domain(value):
    return normalize_address(value).partition("@")[2]


# Subjects the big mailbox providers use for aggregate reports. Anchored at the
# start so a person writing "question about the DMARC report" is not matched, and
# only trusted together with an attachment (the report itself).
DMARC_REPORT_SUBJECT_REGEX = r"^\s*(\[[^]]*\]\s*)?(report domain:|dmarc aggregate report|dmarc aggregation report)"


def automated_dmarc_q():
    """Messages that are automated DMARC aggregate reports (hidden from the default list, never deleted).

    Deliberately narrower than the ingest heuristic in `mailing.services.dmarc`:
    hiding a real message is worse than showing a report. A message qualifies if
    it was archived as a DMARC report, comes from an exact known report sender
    address, or has an anchored report subject plus at least one attachment.
    """
    archived = Exists(DmarcArchive.objects.filter(message=OuterRef("pk")))
    return (
        Q(sender_address__in=sorted(DMARC_SENDERS))
        | Q(archived_flag=True)
        | (Q(subject__iregex=DMARC_REPORT_SUBJECT_REGEX) & Q(attachment_count__gt=0))
    ), archived


def list_messages(*, state=DEFAULT_STATE, query="", address="", page=1, per_page=50, show_automated=False):
    """The mailbox list.

    `state=None` means every state, which is how the blocked and read mail stay
    reachable: a filter that cannot be turned off hides rows permanently.
    """
    queryset = InboundMessage.objects.select_related("inbound_address", "blocked_rule")

    automated_q, archived = automated_dmarc_q()
    queryset = queryset.annotate(archived_flag=archived)
    if state:
        queryset = queryset.filter(state=state)
    hidden_count = 0
    if not show_automated:
        hidden_count = queryset.filter(automated_q).count()
        queryset = queryset.exclude(automated_q)

    query = (query or "").strip()
    if query:
        queryset = queryset.filter(
            Q(subject__icontains=query)
            | Q(snippet__icontains=query)
            | Q(sender_address__icontains=query)
            | Q(from_header__icontains=query)
        )

    address = normalize_address(address)
    if address:
        queryset = queryset.filter(recipient_address__iexact=address)

    total = queryset.count()
    per_page = max(1, min(int(per_page or 50), 200))
    number = max(1, int(page or 1))
    start = (number - 1) * per_page
    rows = list(queryset[start : start + per_page])
    pages = max(1, -(-total // per_page))

    return {
        "rows": rows,
        "total": total,
        "page": number,
        "pages": pages,
        "per_page": per_page,
        "has_previous": number > 1,
        "has_next": number < pages,
        "previous_page": number - 1,
        "next_page": number + 1,
        "state": state or "",
        "query": query,
        "address": address,
        "show_automated": show_automated,
        "hidden_automated_count": hidden_count,
    }


def unread_sender_count():
    """Distinct senders still waiting to be read.

    This is the number that matters for the "how much junk is waiting" question,
    and it is deliberately not the message count: one sender pitching forty times
    is one sender.
    """
    return (
        InboundMessage.objects.annotate(archived_flag=automated_dmarc_q()[1])
        .filter(state=InboundMessageState.RECEIVED)
        .exclude(automated_dmarc_q()[0])
        .exclude(sender_address="")
        .values("sender_address")
        .distinct()
        .count()
    )


def get_message(message_id):
    return InboundMessage.objects.select_related("inbound_address", "blocked_rule").filter(pk=message_id).first()


def mark_read(message):
    if message.state == InboundMessageState.RECEIVED:
        message.state = InboundMessageState.READ
        message.read_at = timezone.now()
        message.save(update_fields=["state", "read_at", "updated_at"])
    return message


def list_addresses(*, include_inactive=True):
    queryset = InboundAddress.objects.annotate(message_count=Count("messages"))
    if not include_inactive:
        queryset = queryset.filter(is_active=True)
    return queryset.order_by("domain", "local_part")


def list_blocked_senders():
    return (
        BlockedSender.objects.select_related("origin_message")
        .annotate(blocked_message_count=Count("blocked_messages"))
        .order_by("-created_at", "-id")
    )


def block_sender(*, message, scope, value, reason="", origin=BLOCK_ORIGIN_BUTTON, actor=None):
    """Block a sender, or return the rule that already blocks them.

    Returns `(rule, created)`. The caller needs to know whether it created the
    rule so the UI can say "already blocked" instead of pretending it just
    stopped something, and because re-blocking the same sender from a second
    message is a no-op an operator should see rather than a silent duplicate.
    """
    scope = BlockedSender.Scope(scope)
    value = normalize_address(value)
    if not value:
        return None, False
    if scope == BlockedSender.Scope.DOMAIN and "@" in value:
        # Only an address is split. A value that is already a bare domain has no
        # "@", and splitting it anyway yields an empty string -- which would
        # silently block nothing.
        value = address_domain(value)
    if not value:
        return None, False

    rule, created = BlockedSender.objects.get_or_create(
        scope=scope,
        value=value,
        defaults={
            "reason": reason or (f"Marked as spam from {message.subject[:120]}" if message else ""),
            "origin": origin,
            "origin_message": message,
        },
    )
    return rule, created


def unblock_sender(rule):
    rule.delete()


def backfill_from_routes(routes):
    """Create addresses for every route in INBOUND_EMAIL_ROUTES.

    This is the migration path for a domain that was already receiving mail
    through the environment variable: without it, the first messages after a
    deploy would find no managed address and be discarded as unmatched. Routes
    that already exist are left alone, so retiring an address is not undone by
    running this again.
    """
    created = []
    for address in routes or {}:
        local_part, _, domain = normalize_address(address).partition("@")
        if not local_part or not domain:
            continue
        row, was_created = InboundAddress.objects.get_or_create(
            local_part=local_part,
            domain=domain,
            defaults={"note": "Backfilled from INBOUND_EMAIL_ROUTES."},
        )
        if was_created:
            created.append(row)
    return created
