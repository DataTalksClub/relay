"""Reconcile old record URLs with the selected operator workspace."""
from django.contrib import messages
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from mailing.models import Audience, Campaign, EmailTemplate, Tag, TransactionalMessage
from mailing.views import active_operator_client


class OperatorRecordScopeMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if request.method not in {"GET", "HEAD"} or not match or match.namespace != "mailing":
            return None
        if not request.user.is_authenticated or not request.user.is_staff:
            return None
        records = {
            "campaign_detail": (Campaign, "campaign_id", "client_id", "campaign_list"),
            "campaign_edit": (Campaign, "campaign_id", "client_id", "campaign_list"),
            "template_detail": (EmailTemplate, "template_id", "client_id", "template_catalog"),
            "transactional_message_detail": (TransactionalMessage, "message_id", "client_id", "email_activity"),
            "audience_detail": (Audience, "audience_id", "organization_id", "audience_list"),
            "audience_edit": (Audience, "audience_id", "organization_id", "audience_list"),
            "tag_detail": (Tag, "tag_id", "audience__organization_id", "audience_list"),
            "tag_edit": (Tag, "tag_id", "audience__organization_id", "audience_list"),
        }
        record = records.get(match.url_name)
        if record is None:
            return None

        client = active_operator_client(request)
        if client is None:
            return None
        model, key, scope_field, destination = record
        owner = model.objects.filter(pk=view_kwargs[key]).values_list(scope_field, flat=True).first()
        expected = client.organization_id if "organization_id" in scope_field else client.id
        if owner is not None and owner != expected:
            messages.info(request, f"That record belongs to another workspace. Showing {client.name} instead.")
            return redirect(f"mailing:{destination}")
        return None
