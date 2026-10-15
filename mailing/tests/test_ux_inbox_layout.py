from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from mailing.models import InboundMessage
from mailing.services.operator_ui import Badge


class FormControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.fields.append(dict(attrs))


def test_inbox_detail_prioritizes_body_and_requires_domain_confirmation():
    request = RequestFactory().get("/inbound/messages/1/")
    request.user = AnonymousUser()
    message = InboundMessage(
        pk=1, state="read", subject="Help needed", sender_address="learner@example.com", sender_domain="example.com"
    )
    html = render_to_string(
        "mailing/operator/inbound_message_detail.html",
        {
            "message": message,
            "body": "Please help with my account.",
            "badge": Badge("Read", "neutral"),
            "return_url": "/inbound/?state=all&q=learner",
        },
        request=request,
    )
    assert html.index("Please help with my account.") < html.index("Authentication and spam checks")
    assert 'href="/inbound/?state=all&amp;q=learner"' in html
    assert 'value="mark_unread"' in html
    assert "every receiving address" in html
    domain_form = html[html.index('<details class="sender-domain-action">') :]
    fields = FormControls()
    fields.feed(domain_form)
    confirmation = next(field for field in fields.fields if field.get("id") == "confirm-domain-block")
    assert confirmation["type"] == "checkbox"
    assert "required" in confirmation


def test_inbox_subject_links_preserve_search_context():
    request = RequestFactory().get("/inbound/?state=all&q=learner&page=2")
    request.user = AnonymousUser()
    message = InboundMessage(pk=1, state="read", subject="Help needed", sender_address="learner@example.com")
    html = render_to_string(
        "mailing/operator/inbound_list.html", {"rows": [message], "counts": {}, "total": 1}, request=request
    )
    target = reverse("mailing:inbound_message_detail", args=[1])
    assert f'href="{target}?return=/inbound/%3Fstate%3Dall%26q%3Dlearner%26page%3D2">Help needed</a>' in html
    assert 'class="table-wrap triage-table"' in html
    assert 'data-label="Message status"' in html


@pytest.mark.django_db
def test_mark_unread_returns_to_filtered_inbox_without_reopening_message(client):
    operator = get_user_model().objects.create_user("inbox-unread", is_staff=True)
    client.force_login(operator)
    message = InboundMessage.objects.create(
        state="read", subject="Follow up later", sender_address="learner@example.com"
    )
    target = reverse("mailing:inbound_message_detail", args=[message.pk])
    response = client.post(
        target + "?return=/inbound/%3Fstate%3Dread", {"action": "mark_unread"}, follow=True
    )
    assert response.status_code == 200
    assert response.redirect_chain == [("/inbound/?state=read", 302)]
    message.refresh_from_db()
    assert message.state == "received"
    assert all(row.pk != message.pk for row in response.context["rows"])
    # Default unread inbox now exposes the message without opening its detail.
    unread_response = client.get(reverse("mailing:inbound_list"))
    assert any(row.pk == message.pk for row in unread_response.context["rows"])
    message.refresh_from_db()
    assert message.state == "received"


@pytest.mark.django_db
def test_blocked_message_never_fetches_stored_body_even_with_pointer(client):
    operator = get_user_model().objects.create_user("inbox-blocked", is_staff=True)
    client.force_login(operator)
    message = InboundMessage.objects.create(
        state="blocked",
        subject="Blocked message",
        sender_address="blocked@example.com",
        raw_bucket="private-inbound",
        body_text_key="historical/body.txt",
    )
    assert message.has_body
    with patch("mailing.services.inbound_email.load_body_text") as load_body:
        response = client.get(reverse("mailing:inbound_message_detail", args=[message.pk]))
    assert response.status_code == 200
    load_body.assert_not_called()
    assert response.context["body"] == ""
    assert "This message was blocked and its body was not kept." in response.content.decode()
    message.refresh_from_db()
    assert message.state == "blocked"
