"""Isolated template approval with ephemeral, validated sample values."""
import json

from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.template import TemplateSyntaxError
from django.views.decorators.http import require_http_methods

from mailing.services.api_errors import ApiValidationError
from mailing.services.transactional_catalog import (
    catalog_context,
    recent_message_rows,
    render_preview,
    transactional_template_queryset,
    validate_template_context,
)


class TemplateSampleForm(forms.Form):
    sample_context = forms.CharField(
        label="Sample variables (JSON object)", max_length=20000,
        widget=forms.Textarea(attrs={"rows": 7, "spellcheck": "false"}),
        help_text="Temporary values for this preview. They do not change the saved template or send email.",
    )

    def clean_sample_context(self):
        try:
            value = json.loads(self.cleaned_data["sample_context"])
        except (ValueError, TypeError, RecursionError) as exc:
            raise forms.ValidationError("Enter valid JSON, for example {\"name\": \"Alex\"}.") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError("Use a JSON object containing variable names and values.")
        return value


@staff_member_required
@require_http_methods(["GET", "POST"])
def template_detail(request, template_id):
    from mailing.views import require_active_client  # noqa: PLC0415 - avoid routing import cycle

    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    template = get_object_or_404(transactional_template_queryset(), pk=template_id, client=active_client)
    initial = template.example_context if isinstance(template.example_context, dict) else {}
    form = TemplateSampleForm(request.POST if request.method == "POST" else None,
                              initial={"sample_context": json.dumps(initial, indent=2)})
    context = catalog_context(template)
    if request.method == "POST":
        context["full_preview"] = {}
        if form.is_valid():
            try:
                validate_template_context(template, form.cleaned_data["sample_context"])
                context["full_preview"] = render_preview(template, form.cleaned_data["sample_context"], max_chars=None)
                context["preview_error"] = ""
            except ApiValidationError as exc:
                names = ", ".join(key.removeprefix("context.") for key in exc.errors)
                form.add_error("sample_context", f"Add a non-empty value for each required variable: {names}.")
            except (TemplateSyntaxError, ValueError, TypeError):
                form.add_error(None, "The template could not be rendered. Check its source and sample values.")
    context.update({"sample_form": form, "active_client": active_client,
                    "recent_message_rows": recent_message_rows(
                        template.transactional_messages.select_related("contact").order_by("-created_at", "-id")[:10])})
    return render(request, "mailing/operator/template_detail.html", context)
