"""The operator side of the inbound mailbox: the list, the detail page, and the
button that blocks a sender.

Each test here drives a URL and asserts on what an operator would see or the
row that resulted. The storage behaviour is covered in test_inbound_email.py;
what matters here is that the console can reach it, that the button works, and
that a non-staff user cannot reach any of it.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from mailing.models import BlockedSender, InboundAddress, InboundMessage, InboundMessageState

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("operator", "operator@example.com", "password", is_staff=True)


@pytest.fixture
def outsider():
    return get_user_model().objects.create_user("outsider", "outsider@example.com", "password")


@pytest.fixture
def address():
    return InboundAddress.objects.create(local_part="support", domain="aishippinglabs.com", note="Support inbox")


def received(**kwargs):
    defaults = {
        "message_id": "<one@example.com>",
        "state": InboundMessageState.RECEIVED,
        "subject": "Question about your pricing",
        "snippet": "Hello, I saw your site and wanted to ask about pricing.",
        "from_header": "Jane <jane@prospect.example>",
        "sender_address": "jane@prospect.example",
        "sender_domain": "prospect.example",
        "recipient": "support",
        "recipient_domain": "aishippinglabs.com",
        "recipient_address": "support@aishippinglabs.com",
        "raw_bucket": "relay-production-inbound-mail",
        "raw_key": "raw/abc",
        "size_bytes": 4096,
    }
    defaults.update(kwargs)
    return InboundMessage.objects.create(**defaults)


def test_the_mailbox_requires_a_staff_login(client, outsider, address):
    received()
    response = client.get(reverse("mailing:inbound_list"))

    assert response.status_code == 302
    assert b"jane@prospect.example" not in response.content


def test_every_inbound_page_is_closed_to_non_staff(client, outsider, address):
    client.force_login(outsider)
    message = received()

    for name, args in (
        ("mailing:inbound_list", []),
        ("mailing:inbound_message_detail", [message.pk]),
        ("mailing:inbound_address_list", []),
        ("mailing:inbound_address_create", []),
    ):
        assert client.get(reverse(name, args=args)).status_code == 302, name


def test_the_list_shows_unread_mail_with_the_sender_and_subject(client, operator, address):
    received()
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_list"))

    assert response.status_code == 200
    assert b"jane@prospect.example" in response.content
    assert b"Question about your pricing" in response.content
    assert b"support@aishippinglabs.com" in response.content


def test_the_list_defaults_to_unread_and_read_mail_is_reachable(client, operator, address):
    read = received(message_id="<old@example.com>", state=InboundMessageState.READ)
    client.force_login(operator)

    unread = client.get(reverse("mailing:inbound_list"))
    read_page = client.get(reverse("mailing:inbound_list"), {"state": "read"})

    assert read.subject.encode() not in unread.content
    assert read.subject.encode() in read_page.content


def test_search_matches_the_sender_the_subject_and_the_body(client, operator, address):
    received()
    client.force_login(operator)

    by_sender = client.get(reverse("mailing:inbound_list"), {"q": "prospect.example"})
    by_body = client.get(reverse("mailing:inbound_list"), {"q": "wanted to ask"})
    by_nothing = client.get(reverse("mailing:inbound_list"), {"q": "cryptocurrency"})

    assert by_sender.status_code == 200
    assert b"Question about your pricing" in by_sender.content
    assert b"Question about your pricing" in by_body.content
    assert b"Question about your pricing" not in by_nothing.content


def test_an_empty_mailbox_says_so_without_claiming_nothing_arrived(client, operator):
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_list"))

    assert response.status_code == 200
    assert b"No unread mail" in response.content


def test_a_filtered_empty_result_does_not_claim_nothing_arrived(client, operator, address):
    received()
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_list"), {"q": "nothing matches this"})

    assert b"No messages match this filter" in response.content
    assert b"No unread mail" not in response.content


def test_opening_a_message_marks_it_read(client, operator, address):
    message = received()
    client.force_login(operator)

    client.get(reverse("mailing:inbound_message_detail", args=[message.pk]))
    message.refresh_from_db()

    assert message.state == InboundMessageState.READ
    assert message.read_at is not None


def test_mark_as_spam_blocks_the_sender_and_stops_future_mail(client, operator, address):
    """The button's whole contract: this sender is not filed again.

    The row the operator was looking at is kept and marked, so the message they
    were reading does not vanish from under them, and the block applies to the
    next message rather than to the one already read.
    """
    message = received()
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_message_detail", args=[message.pk]),
        {"action": "mark_as_spam", "scope": "address"},
        follow=True,
    )

    assert response.status_code == 200
    rule = BlockedSender.objects.get()
    assert rule.scope == BlockedSender.Scope.ADDRESS
    assert rule.value == "jane@prospect.example"
    assert rule.origin_message_id == message.pk
    assert rule.origin == "mark_as_spam"
    message.refresh_from_db()
    assert message.state == InboundMessageState.READ
    assert message.blocked_rule_id == rule.pk
    assert b"Blocked sender jane@prospect.example" in response.content


def test_mark_as_spam_can_block_the_whole_domain(client, operator, address):
    message = received()
    client.force_login(operator)

    client.post(
        reverse("mailing:inbound_message_detail", args=[message.pk]),
        {"action": "mark_as_spam", "scope": "domain"},
    )

    rule = BlockedSender.objects.get()
    assert rule.scope == BlockedSender.Scope.DOMAIN
    assert rule.value == "prospect.example"


def test_blocking_a_second_message_from_the_same_sender_does_not_duplicate(client, operator, address):
    first = received()
    second = received(message_id="<two@example.com>")
    client.force_login(operator)

    client.post(
        reverse("mailing:inbound_message_detail", args=[first.pk]),
        {"action": "mark_as_spam", "scope": "address"},
    )
    response = client.post(
        reverse("mailing:inbound_message_detail", args=[second.pk]),
        {"action": "mark_as_spam", "scope": "address"},
        follow=True,
    )

    assert BlockedSender.objects.count() == 1
    # The operator is told it was already blocked rather than shown a silent no-op.
    assert b"was already blocked" in response.content


def test_a_blocked_message_offers_unblocking(client, operator, address):
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="jane@prospect.example")
    message = received(state=InboundMessageState.BLOCKED, blocked_rule=rule)
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_message_detail", args=[message.pk]))

    assert b"Remove this block rule" in response.content
    assert b"will keep future mail" not in response.content


def test_a_blocked_message_explains_that_its_body_was_not_kept(client, operator, address):
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="jane@prospect.example")
    message = received(
        state=InboundMessageState.BLOCKED,
        blocked_rule=rule,
        body_text_key="processed/abc/body.txt",
    )
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_message_detail", args=[message.pk]))

    assert b"its body was not kept" in response.content
    # A blocked message must not render a body even if one is somehow present.
    assert b"wanted to ask about pricing" not in response.content


def test_unblocking_from_the_message_lets_the_sender_through(client, operator, address):
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="jane@prospect.example")
    message = received(state=InboundMessageState.BLOCKED, blocked_rule=rule)
    client.force_login(operator)

    client.post(reverse("mailing:inbound_message_detail", args=[message.pk]), {"action": "unblock"})

    assert not BlockedSender.objects.exists()


def test_a_message_with_no_sender_address_cannot_be_blocked(client, operator, address):
    message = received(sender_address="", sender_domain="", from_header="")
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_message_detail", args=[message.pk]),
        {"action": "mark_as_spam", "scope": "address"},
        follow=True,
    )

    assert not BlockedSender.objects.exists()
    assert b"no sender address to block" in response.content


def test_the_address_page_lists_addresses_and_their_message_counts(client, operator, address):
    received()
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_address_list"))

    assert response.status_code == 200
    assert b"support@aishippinglabs.com" in response.content
    assert b"Support inbox" in response.content


def test_creating_an_address_is_a_row_not_a_deploy(client, operator):
    """The whole point: a new address needs no Terraform and no redeploy."""
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_address_create"),
        {"local_part": "sales", "domain": "aishippinglabs.com", "note": "Inbound leads"},
        follow=True,
    )

    assert response.status_code == 200
    created = InboundAddress.objects.get()
    assert created.address == "sales@aishippinglabs.com"
    assert created.local_part == "sales"
    assert created.is_active is True
    assert b"sales@aishippinglabs.com" in response.content


def test_creating_an_address_normalises_case(client, operator):
    client.force_login(operator)

    client.post(
        reverse("mailing:inbound_address_create"),
        {"local_part": "Sales", "domain": "AIShippingLabs.COM"},
    )

    created = InboundAddress.objects.get()
    assert created.address == "sales@aishippinglabs.com"


def test_creating_a_duplicate_address_is_reported_not_silently_ignored(client, operator, address):
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_address_create"),
        {"local_part": "support", "domain": "aishippinglabs.com"},
        follow=True,
    )

    assert InboundAddress.objects.count() == 1
    assert b"already exists" in response.content


def test_an_address_with_an_at_sign_in_the_local_part_is_rejected(client, operator):
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_address_create"),
        {"local_part": "sales@team", "domain": "aishippinglabs.com"},
        follow=True,
    )

    assert not InboundAddress.objects.exists()
    assert b"no @" in response.content


def test_a_missing_domain_is_rejected(client, operator):
    client.force_login(operator)

    response = client.post(
        reverse("mailing:inbound_address_create"),
        {"local_part": "sales", "domain": ""},
        follow=True,
    )

    assert not InboundAddress.objects.exists()
    assert b"Enter a domain" in response.content


def test_retiring_an_address_stops_receiving_but_keeps_its_mail(client, operator, address):
    received()
    client.force_login(operator)

    client.post(reverse("mailing:inbound_address_archive", args=[address.pk]))
    address.refresh_from_db()

    assert address.is_active is False
    # The message that arrived before the retirement is not collateral damage.
    assert InboundMessage.objects.count() == 1


def test_a_retired_address_can_be_reactivated(client, operator, address):
    address.is_active = False
    address.save(update_fields=["is_active"])
    client.force_login(operator)

    client.post(reverse("mailing:inbound_address_archive", args=[address.pk]))
    address.refresh_from_db()

    assert address.is_active is True


def test_a_blocked_sender_can_be_unblocked_from_the_address_page(client, operator, address):
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="jane@prospect.example")
    client.force_login(operator)

    client.post(reverse("mailing:blocked_sender_delete", args=[rule.pk]))

    assert not BlockedSender.objects.exists()


def test_the_ses_spam_verdict_is_shown_as_a_signal_not_as_a_verdict(client, operator, address):
    """A service spam warning is a signal to investigate rather than certainty.

    Showing "spam" next to a message with no explanation invites an operator to
    act on SES's opinion as though it were a finding.
    """
    received(spam_verdict="Yes")
    client.force_login(operator)

    response = client.get(reverse("mailing:inbound_list"))

    assert b"Suspected spam" in response.content


def test_the_nav_reaches_the_mailbox_and_the_addresses(client, operator, address):
    client.force_login(operator)

    html = client.get(reverse("mailing:dashboard")).content.decode()

    assert reverse("mailing:inbound_list") in html
    assert reverse("mailing:inbound_address_list") in html
