"""Management API. Only dedicated admin bearer keys authenticate here."""

import json
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from mailing.models import AdminApiKey, Audience, Client, ClientApiKey, Organization
from mailing.services.admin_auth import authenticate_admin_request, create_admin_api_key
from mailing.services.auth import create_client_api_key
from mailing.services.operator_management import audit


class ApiError(Exception):
    def __init__(self, code, status=400, fields=None):
        self.code, self.status, self.fields = code, status, fields


def admin_endpoint(methods):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            key = authenticate_admin_request(request)
            if key is None:
                response = JsonResponse({"error": {"code": "invalid_admin_api_key"}}, status=401)
                response["WWW-Authenticate"] = 'Bearer realm="relay-admin"'
            elif request.method not in methods:
                response = JsonResponse({"error": {"code": "method_not_allowed"}}, status=405)
                response["Allow"] = ", ".join(methods)
            else:
                request.admin_key = key
                try:
                    with transaction.atomic():
                        response = view(request, *args, **kwargs)
                except ApiError as exc:
                    response = JsonResponse(
                        {"error": {"code": exc.code, "fields": exc.fields or {}}}, status=exc.status
                    )
                except ValidationError as exc:
                    fields = exc.message_dict if hasattr(exc, "message_dict") else {"body": exc.messages}
                    response = JsonResponse({"error": {"code": "validation_error", "fields": fields}}, status=400)
                except IntegrityError:
                    response = JsonResponse({"error": {"code": "conflict"}}, status=409)
                except Http404:
                    response = JsonResponse({"error": {"code": "not_found"}}, status=404)
            response["Cache-Control"] = "no-store"
            return response

        wrapped.allowed_methods = methods
        return wrapped

    return decorate


def body(request, allowed):
    if request.content_type != "application/json":
        raise ApiError("unsupported_media_type", 415)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ApiError("invalid_json") from None
    if not isinstance(data, dict):
        raise ApiError("body_must_be_object")
    unknown = set(data) - set(allowed)
    if unknown:
        raise ApiError("unknown_fields", fields={field: "unknown" for field in sorted(unknown)})
    return data


def get_record(model, **lookup):
    try:
        obj = model.objects.filter(**lookup).first()
    except (TypeError, ValueError, ValidationError):
        raise ApiError("invalid_identifier") from None
    if obj is None:
        raise ApiError("not_found", 404)
    return obj


def record_audit(request, action, target, metadata=None):
    audit(request.admin_key.user, action, target, {"admin_key_id": request.admin_key.id, **(metadata or {})})


def serialize(obj):
    data = {"id": obj.id, "name": obj.name, "slug": obj.slug}
    if isinstance(obj, (Audience, Client)):
        data["organization_id"] = obj.organization_id
    if isinstance(obj, Client):
        data["is_active"] = obj.is_active
    return data


def serialize_key(key):
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.display_prefix,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
    }


def listing(request, queryset, serializer):
    try:
        limit = int(request.GET.get("limit", 100))
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        raise ApiError("invalid_pagination") from None
    if not 1 <= limit <= 200 or offset < 0:
        raise ApiError("invalid_pagination")
    return JsonResponse(
        {
            "items": [serializer(obj) for obj in queryset.order_by("id")[offset : offset + limit]],
            "total": queryset.count(),
            "limit": limit,
            "offset": offset,
        }
    )


RESOURCE_MODELS = {"organizations": Organization, "audiences": Audience, "clients": Client}


@admin_endpoint(["GET", "POST", "PATCH"])
def resources(request, resource, record_id=None):
    model = RESOURCE_MODELS[resource]
    obj = get_record(model, pk=record_id) if record_id is not None else None
    if request.method == "GET":
        return JsonResponse(serialize(obj)) if obj else listing(request, model.objects.all(), serialize)
    if (request.method == "POST" and obj is not None) or (request.method == "PATCH" and obj is None):
        raise ApiError("method_not_allowed", 405)
    allowed = {"name", "slug"}
    if model in (Audience, Client):
        allowed.add("organization_id")
    if model is Client:
        allowed.add("is_active")
    data = body(request, allowed)
    obj = obj or model()
    changes = {}
    for field, value in data.items():
        if field == "is_active":
            valid_type = type(value) is bool
        elif field == "organization_id":
            valid_type = type(value) is int and value > 0
        else:
            valid_type = isinstance(value, str)
        if not valid_type:
            raise ApiError("validation_error", fields={field: "invalid_type"})
        if field == "organization_id":
            get_record(Organization, pk=value)
            if obj.pk and value != obj.organization_id:
                raise ApiError("validation_error", fields={field: "organization_is_immutable"})
        if getattr(obj, field) != value:
            changes[field] = value
        setattr(obj, field, value)
    obj.full_clean()
    obj.save()
    record_audit(request, f"{resource}.{'create' if request.method == 'POST' else 'update'}", obj, changes)
    return JsonResponse(serialize(obj), status=201 if request.method == "POST" else 200)


@admin_endpoint(["GET", "POST"])
def client_keys(request, client_id):
    client = get_record(Client, pk=client_id)
    if request.method == "GET":
        return listing(request, client.api_keys.all(), serialize_key)
    data = body(request, {"name", "notes"})
    validate_key_name(data)
    if not isinstance(data.get("notes", ""), str):
        raise ApiError("validation_error", fields={"notes": "invalid_type"})
    key, raw_key = create_client_api_key(client=client, name=data["name"], notes=data.get("notes", ""))
    record_audit(request, "client.api_key.create", client, {"key_id": key.id, "key_prefix": key.display_prefix})
    return JsonResponse({**serialize_key(key), "api_key": raw_key}, status=201)


def validate_key_name(data):
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ApiError("validation_error", fields={"name": "required_string_max_120"})


@admin_endpoint(["GET", "POST"])
def admin_keys(request):
    if request.method == "GET":
        return listing(request, AdminApiKey.objects.all(), lambda key: {**serialize_key(key), "user_id": key.user_id})
    data = body(request, {"name"})
    validate_key_name(data)
    key, raw_key = create_admin_api_key(user=request.admin_key.user, name=data["name"])
    record_audit(request, "admin.api_key.create", key, {"key_prefix": key.display_prefix})
    return JsonResponse({**serialize_key(key), "user_id": key.user_id, "api_key": raw_key}, status=201)


@admin_endpoint(["POST"])
def revoke_key(request, key_id, client_id=None):
    model = ClientApiKey if client_id is not None else AdminApiKey
    lookup = {"pk": key_id}
    if client_id is not None:
        lookup["client_id"] = client_id
    key = get_record(model, **lookup)
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at", "updated_at"])
        record_audit(
            request,
            "client.api_key.revoke" if client_id is not None else "admin.api_key.revoke",
            key,
            {"key_prefix": key.display_prefix},
        )
    return JsonResponse(serialize_key(key))


def scoped_client_endpoint(view):
    """Reuse the client operation with a server-selected scope, after admin auth."""

    @admin_endpoint(["GET", "POST", "PUT", "PATCH", "DELETE"])
    @wraps(view)
    def wrapped(request, client_id, **kwargs):
        client = get_record(Client, pk=client_id)
        request._admin_api_client = client
        request.user = request.admin_key.user
        response = view(request, **kwargs)
        if request.method != "GET" and response.status_code < 400:
            record_audit(request, f"admin.{view.__name__}", client, {"path": request.path})
        return response

    return wrapped


@admin_endpoint(["GET"])
def endpoint_index(request):
    from mailing.admin_urls import urlpatterns  # noqa: PLC0415 - URL registry imports this module

    return JsonResponse(
        {
            "authentication": "Authorization: Bearer <relay_admin_...>",
            "endpoints": [
                {"path": "/api/admin/" + str(pattern.pattern), "name": pattern.name} for pattern in urlpatterns
            ],
        }
    )
