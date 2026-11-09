"""The SES form-body fast path must send exactly the bytes botocore would."""

import base64
import os
from email import policy
from email.parser import BytesParser

import boto3
import pytest
from botocore.awsrequest import AWSResponse

from mailing.aws import _fast_form_body, _with_fast_form_body
from mailing.ses import _Content, _Envelope, _raw_parameters

XML = (
    b'<SendRawEmailResponse xmlns="http://ses.amazonaws.com/doc/2010-12-01/"><SendRawEmailResult>'
    b"<MessageId>x</MessageId></SendRawEmailResult><ResponseMetadata><RequestId>r</RequestId>"
    b"</ResponseMetadata></SendRawEmailResponse>"
)


class _Raw:
    content = XML

    def stream(self):
        yield XML


def _client(fast):
    client = boto3.client("ses", region_name="eu-west-1", aws_access_key_id="a", aws_secret_access_key="b")
    captured = []

    def capture(request, **kwargs):
        captured.append((request.body, dict(request.headers)))
        return AWSResponse("http://x", 200, {}, _Raw())

    client.meta.events.register("before-send.ses.SendRawEmail", capture)
    if fast:
        _with_fast_form_body(client)
    return client, captured


def _send(client, raw):
    return client.send_raw_email(
        Source="from@example.com", Destinations=["to+tag@example.com"], RawMessage={"Data": raw}
    )["MessageId"]


@pytest.mark.parametrize("size", [10, 5000, 120_000])
def test_fast_body_is_byte_identical_to_botocore(size):
    # The same bytes through both clients: a rebuilt MIME message would differ in Date/Message-ID.
    html = "<p>" + "".join(f'<a href="https://e.com/{i}?a=1&b=2">ünï {i}</a>' for i in range(size // 40 + 1)) + "</p>"
    raw = ("From: a@b.c\r\nSubject: Héllo & goodbye\r\n\r\n" + html).encode()
    slow, slow_seen = _client(fast=False)
    fast, fast_seen = _client(fast=True)

    assert _send(slow, raw) == _send(fast, raw) == "x"

    slow_body, slow_headers = slow_seen[0]
    fast_body, fast_headers = fast_seen[0]
    assert fast_body == slow_body
    assert fast_headers["Content-Type"] == slow_headers["Content-Type"]
    assert fast_headers["Content-Length"] == slow_headers["Content-Length"]


def test_fast_path_only_handles_base64_values():
    untouched = {"body": {"Action": "SendRawEmail", "RawMessage.Data": "é" * 5000}}
    _fast_form_body(untouched)
    assert isinstance(untouched["body"], dict)
    non_str = {"body": {"A": 1, "B": "x" * 5000}}
    _fast_form_body(non_str)
    assert isinstance(non_str["body"], dict)
    payload = base64.b64encode(os.urandom(6000)).decode()
    ok = {"body": {"Action": "SendRawEmail", "RawMessage.Data": payload}}
    _fast_form_body(ok)
    assert ok["body"].startswith("Action=SendRawEmail&RawMessage.Data=")
    assert "+" not in ok["body"].split("RawMessage.Data=")[1]


@pytest.mark.parametrize(
    "html",
    [
        "<p>" + "".join(f'<a href="https://e.com/{i}?a=1&b=2=3">ünï ✓ {i}</a> trailing \n' for i in range(400)) + "</p>",
        "<p>" + "x" * 9000 + "</p>\r\n<p>y</p> \t\n",
        "<p>" + "é" * 9000 + "</p>",
    ],
)
def test_large_html_part_round_trips_through_fast_quoted_printable(html):
    content = _Content("Subject é", html, "plain text", {"List-Unsubscribe": "<https://e.com/u>"}, None, None)
    raw = _raw_parameters(_Envelope("a@b.c", "t@e.com", "", None, None, None), content)["RawMessage"]["Data"]
    parsed = BytesParser(policy=policy.default).parsebytes(raw)

    assert parsed.get_content_type() == "multipart/alternative"
    html_part = parsed.get_body(preferencelist=("html",))
    assert html_part["Content-Transfer-Encoding"] == "quoted-printable"
    assert html_part.get_content_charset() == "utf-8"
    assert html_part.get_content().replace("\r\n", "\n") == html.replace("\r\n", "\n")
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "plain text"
    assert parsed["List-Unsubscribe"] == "<https://e.com/u>"
    assert max(len(line) for line in raw.split(b"\n")) <= 998
