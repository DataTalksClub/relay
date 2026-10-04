from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from mailing.models import AdminApiKey
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.operator_management import audit


class AdminKeyForm(forms.Form):
    name = forms.CharField(max_length=120)


@staff_member_required
@require_http_methods(["GET", "POST"])
def admin_keys(request):
    form = AdminKeyForm(request.POST if request.method == "POST" else None)
    raw_key = None
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                key, raw_key = create_admin_api_key(user=request.user, name=form.cleaned_data["name"])
                audit(
                    request.user,
                    "admin.api_key.create",
                    key,
                    {"key_prefix": key.display_prefix, "source": "operator_ui"},
                )
        except (ValidationError, IntegrityError):
            form.add_error("name", "You already have an active key with this name.")
    response = render(
        request,
        "mailing/operator/admin_api_keys.html",
        {
            "form": form,
            "raw_key": raw_key,
            "keys": AdminApiKey.objects.select_related("user").all(),
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@staff_member_required
@require_POST
def revoke_admin_key(request, key_id):
    with transaction.atomic():
        key = get_object_or_404(AdminApiKey, pk=key_id)
        if key.revoked_at is None:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at", "updated_at"])
            audit(
                request.user, "admin.api_key.revoke", key, {"key_prefix": key.display_prefix, "source": "operator_ui"}
            )
    return redirect("mailing:admin_api_keys")
