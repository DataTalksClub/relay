import tomllib
from functools import cache
from importlib import metadata
from pathlib import Path

from django.conf import settings

from mailing.models import Client

ACTIVE_CLIENT_SESSION_KEY = "operator_active_client_id"


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
