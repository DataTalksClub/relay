import tomllib
from functools import cache
from importlib import metadata
from pathlib import Path

from django.conf import settings

from mailing.models import Client

ACTIVE_CLIENT_SESSION_KEY = "operator_active_client_id"

# Screens that belong to the shared service rather than the selected client.
# The mode is derived from the route, never stored, so every URL keeps working.
SERVICE_URL_NAMES = frozenset(
    {
        "service_home",
        "inbound_list", "inbound_message_detail", "inbound_message_raw", "inbound_block_link", "inbound_unblock_link", "blocked_sender_delete",
        "inbound_address_list", "inbound_address_create", "inbound_address_archive",
        "dmarc_report_list", "dmarc_report_detail", "dmarc_summary", "dmarc_report_xml", "dmarc_report_attachment",
        "ses_quota",
        "client_list", "client_create",
        "api_docs", "api_docs_json", "api_docs_workflow", "api_docs_reference", "api_docs_legacy",
        "admin_api_keys", "admin_api_key_revoke",
    }
)
SERVICE_NAMESPACES = frozenset({"jobs"})


def is_service_route(match):
    return bool(match) and (match.url_name in SERVICE_URL_NAMES or match.namespace in SERVICE_NAMESPACES)


def operator_client_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated or not request.user.is_staff:
        return {}

    clients = Client.objects.select_related("organization").order_by("organization__slug", "slug")
    active_client = None
    active_client_id = request.session.get(ACTIVE_CLIENT_SESSION_KEY)
    if active_client_id:
        active_client = clients.filter(pk=active_client_id).first()
        if active_client is None:
            request.session.pop(ACTIVE_CLIENT_SESSION_KEY, None)
    else:
        first_client = clients.first()
        if first_client is not None and not clients.exclude(pk=first_client.pk).exists():
            request.session[ACTIVE_CLIENT_SESSION_KEY] = first_client.id
            active_client = first_client

    return {
        "operator_identity": operator_identity(request.user),
        "operator_meta": {"version": relay_version(), "region": settings.AWS_REGION},
        "operator_clients": clients,
        "operator_active_client": active_client,
        "operator_active_client_session_key": ACTIVE_CLIENT_SESSION_KEY,
        "operator_service_mode": is_service_route(getattr(request, "resolver_match", None)),
    }


def operator_identity(user):
    """Name, email and avatar initials for the shell's account row."""
    name = user.get_full_name() or user.get_username()
    parts = [part for part in (user.first_name, user.last_name) if part]
    initials = "".join(part[0] for part in parts) if parts else name[:1]
    return {"name": name, "email": user.email, "initials": initials.upper()}


@cache
def relay_version():
    try:
        return metadata.version("relay")
    except metadata.PackageNotFoundError:
        pyproject = Path(settings.BASE_DIR) / "pyproject.toml"
        try:
            return tomllib.loads(pyproject.read_text())["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            return ""
