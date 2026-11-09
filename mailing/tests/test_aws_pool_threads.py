"""AC4: CAMPAIGN_SEND_CONCURRENCY threads on one cached SES client never overflow the pool."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import override_settings

from mailing import aws

THREADS = 16


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = (
            b'<SendRawEmailResponse xmlns="http://ses.amazonaws.com/doc/2010-12-01/">'
            b"<SendRawEmailResult><MessageId>m-1</MessageId></SendRawEmailResult>"
            b"<ResponseMetadata><RequestId>r</RequestId></ResponseMetadata></SendRawEmailResponse>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    # The default listen backlog (5) resets some of 16 simultaneous connects; SES send
    # clients no longer retry in botocore (one attempt), so that would surface as an error.
    request_queue_size = 64


@override_settings(
    RELAY_TASK_ROLE_ARNS={}, RELAY_REQUIRE_TASK_ROLES=False,
    AWS_REGION="us-east-1", AWS_SES_REGION="us-east-1", CAMPAIGN_SEND_CONCURRENCY=THREADS,
)
def test_send_concurrency_threads_do_not_overflow_the_connection_pool(monkeypatch, caplog):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = aws.ses_client(endpoint_url=f"http://127.0.0.1:{server.server_port}")
        barrier = threading.Barrier(THREADS)
        errors = []

        def send():
            try:
                barrier.wait(timeout=10)
                for _ in range(5):
                    client.send_raw_email(Source="a@example.com", Destinations=["b@example.com"],
                                          RawMessage={"Data": b"Subject: x\r\n\r\nbody"})
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        with caplog.at_level(logging.WARNING, logger="urllib3.connectionpool"):
            threads = [threading.Thread(target=send) for _ in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
    finally:
        server.shutdown()
    assert errors == []
    assert [r.getMessage() for r in caplog.records if r.name == "urllib3.connectionpool"] == []
