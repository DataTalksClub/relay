"""Count API status classes only: never persist paths, tokens, bodies or addresses."""
import logging

from django.db.models import F
from django.utils import timezone

from mailing.models import ClientApiKey, TransactionalRequestMinute
from mailing.services.auth import public_id_from_raw_key

logger = logging.getLogger(__name__)


class TransactionalMonitorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/api/"):
            return response
        try:
            # Public ID is an attribution hint, not authentication. Includes revoked keys
            # so auth failures remain visible; spoofing it grants no access.
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            public_id = public_id_from_raw_key(token.strip()) if scheme.lower() == "bearer" else ""
            client_id = getattr(request, "_transactional_monitor_client_id", None)
            if client_id is None and public_id:
                client_id = ClientApiKey.objects.filter(public_id=public_id).values_list("client_id", flat=True).first()
            if client_id is not None:
                row, _ = TransactionalRequestMinute.objects.get_or_create(
                    client_id=client_id, minute=timezone.now().replace(second=0, microsecond=0))
                TransactionalRequestMinute.objects.filter(pk=row.pk).update(
                    total=F("total") + 1, errors=F("errors") + int(response.status_code >= 400),
                    server_errors=F("server_errors") + int(response.status_code >= 500))
        except Exception as exc:
            logger.error("relay_transactional_api_metric_failed error_type=%s", type(exc).__name__)
        return response
