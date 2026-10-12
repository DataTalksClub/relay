import json
from unittest.mock import patch

from django.db import DatabaseError, OperationalError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from mailing.middleware import DatabaseOutageMiddleware


class DatabaseOutageMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_api_database_error_returns_json_503(self):
        def failing_view(request):
            raise OperationalError("could not connect to server: Connection refused")

        middleware = DatabaseOutageMiddleware(failing_view)
        request = self.factory.get("/api/tasks")
        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Retry-After"], "30")
        self.assertEqual(
            json.loads(response.content),
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Database is temporarily unavailable. Please retry later.",
                }
            },
        )

    def test_api_post_database_error_returns_json_503(self):
        def failing_view(request):
            raise DatabaseError("relation \"mailing_clientapikey\" does not exist")

        middleware = DatabaseOutageMiddleware(failing_view)
        request = self.factory.post("/api/transactional/send", data={"test": 1}, content_type="application/json")
        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Retry-After"], "30")
        self.assertEqual(
            json.loads(response.content),
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Database is temporarily unavailable. Please retry later.",
                }
            },
        )

    def test_json_accept_header_returns_json_503(self):
        def failing_view(request):
            raise OperationalError("FATAL: terminating connection due to administrator command")

        middleware = DatabaseOutageMiddleware(failing_view)
        request = self.factory.get("/health/ready", HTTP_ACCEPT="application/json")
        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        data = json.loads(response.content)
        self.assertEqual(data["error"]["code"], "service_unavailable")

    def test_html_request_database_error_returns_html_503(self):
        def failing_view(request):
            raise OperationalError("server closed the connection unexpectedly")

        middleware = DatabaseOutageMiddleware(failing_view)
        request = self.factory.get("/")
        response = middleware(request)

        self.assertEqual(response.status_code, 503)
        self.assertIn("text/html", response["Content-Type"])
        self.assertEqual(response["Retry-After"], "30")
        self.assertIn("Service Unavailable (503)", response.content.decode())

    def test_process_exception_handles_database_error(self):
        middleware = DatabaseOutageMiddleware(lambda req: HttpResponse("ok"))
        request = self.factory.get("/api/contacts")
        response = middleware.process_exception(request, OperationalError("db down"))

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 503)
        data = json.loads(response.content)
        self.assertEqual(data["error"]["code"], "service_unavailable")

    def test_non_database_error_is_not_handled(self):
        def crashing_view(request):
            raise ValueError("bad argument")

        middleware = DatabaseOutageMiddleware(crashing_view)
        request = self.factory.get("/api/tasks")
        with self.assertRaises(ValueError):
            middleware(request)

    def test_successful_request_passes_through(self):
        def ok_view(request):
            return HttpResponse("success", status=200)

        middleware = DatabaseOutageMiddleware(ok_view)
        request = self.factory.get("/api/tasks")
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"success")

    def test_client_integration_with_database_failure(self):
        """Full request pipeline test simulating DB failure during view execution."""
        with patch("jobs.views.authenticate_api_request") as mock_auth:
            mock_auth.side_effect = OperationalError("connection to server was lost")
            response = self.client.get("/api/tasks", HTTP_AUTHORIZATION="Bearer testkey")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response["Content-Type"], "application/json")
            self.assertEqual(
                response.json(),
                {
                    "error": {
                        "code": "service_unavailable",
                        "message": "Database is temporarily unavailable. Please retry later.",
                    }
                },
            )

    def test_session_save_database_error_returns_html_503(self):
        """Full request pipeline test simulating DB failure during session save (e.g. login)."""
        with self.settings(
            AUTH_BASE_URL="https://auth.example.com",
            AUTH_CLIENT_ID="client_id",
            AUTH_CALLBACK_URL="https://relay.example.com/auth/callback",
            AUTH_ISSUER="https://auth.example.com",
            AUTH_JWKS_URL="https://auth.example.com/.well-known/jwks.json",
        ):
            with patch("django.contrib.sessions.backends.db.SessionStore.save") as mock_save:
                mock_save.side_effect = OperationalError("server closed the connection unexpectedly")
                client = self.client_class(raise_request_exception=False)
                response = client.get("/auth/login?return_to=/admin/")
                self.assertEqual(response.status_code, 503)
                self.assertIn("text/html", response["Content-Type"])
                self.assertEqual(response["Retry-After"], "30")
                self.assertIn("Service Unavailable (503)", response.content.decode())

