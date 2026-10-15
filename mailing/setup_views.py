"""Task-oriented client setup; configuration never implies delivery verification."""

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.forms import ClientForm
from mailing.models import Client, EmailTemplate, TransactionalMessage
from mailing.services.operator_management import create_or_update_client


class SenderRowsClientForm(ClientForm):
    """Keep the existing validated sender contract while accepting accessible rows."""

    def __init__(self, data=None, *args, **kwargs):
        self.sender_rows = []
        if data is not None and data.get("sender_editor") == "rows":
            data = data.copy()
            try:
                total = min(max(int(data.get("sender_count", 0)), 0), 100)
            except (TypeError, ValueError):
                total = 0
            self.sender_rows = [
                {"id": data.get(f"sender-{i}-id", "").strip(), "email": data.get(f"sender-{i}-email", "").strip()}
                for i in range(total)
            ]
            data["sender_emails"] = "\n".join(
                f"{row['id']}={row['email']}" for row in self.sender_rows if row["id"] or row["email"]
            )
        super().__init__(data, *args, **kwargs)
        if data is None:
            self.sender_rows = list(self.instance.sender_emails or [])
        self.sender_rows = self.sender_rows or [{"id": "", "email": ""}]
        choices = [("", "Choose a default sender")]
        choices += [(row["id"], row["id"]) for row in self.sender_rows if row["id"]]
        current = self["default_sender_id"].value()
        if current and current not in dict(choices):
            choices.append((current, current))
        # CharField retains legacy import validation; the widget presents a selector.
        self.fields["default_sender_id"].widget = forms.Select(choices=choices)

    def clean(self):
        cleaned = super().clean()
        if self.data.get("sender_editor") == "rows":
            try:
                count = int(self.data.get("sender_count", 0))
            except (TypeError, ValueError):
                count = -1
            if not 0 <= count <= 100:
                self.add_error("sender_emails", "Use between 0 and 100 sender rows.")
            for row in self.sender_rows:
                if "=" in row["id"] or "\n" in row["email"] or "\r" in row["email"]:
                    self.add_error("sender_emails", "Enter one sender ID and address per row.")
        return cleaned


def client_setup_context(client):
    configured = bool(
        client.sender_emails
        and client.default_sender_id
        in {sender.get("id") for sender in client.sender_emails if isinstance(sender, dict)}
    )
    template = (
        EmailTemplate.objects.filter(client=client, is_active=True, is_transactional=True)
        .exclude(subject="")
        .exclude(html_body="", text_body="", markdown_body="")
        .first()
    )
    delivered = (
        TransactionalMessage.objects.filter(client=client, delivered_at__isnull=False).order_by("-delivered_at").first()
    )
    detail = reverse("mailing:client_detail", args=[client.id])
    has_active_key = client.is_active and client.api_keys.filter(revoked_at__isnull=True).exists()
    return {
        "setup_checklist": [
            {
                "title": "Sender addresses",
                "done": configured,
                "status": "Configured" if configured else "Needs configuration",
                "url": reverse("mailing:client_edit", args=[client.id]) + "#client-sender-heading",
                "action": "Configure senders",
                "help": "Configured addresses still need provider identity verification; this screen cannot verify it.",
            },
            {
                "title": "API access",
                "done": has_active_key,
                "status": "Active key available" if has_active_key else "Needs an active client and key",
                "url": detail + "#api-keys"
                if client.is_active
                else reverse("mailing:client_edit", args=[client.id]) + "#client-access-heading",
                "action": "Manage API access",
                "help": "Keys authenticate requests; keep the raw secret outside Relay.",
            },
            {
                "title": "Transactional template",
                "done": bool(template),
                "status": "Content configured" if template else "Needs a template",
                "url": reverse("admin:mailing_emailtemplate_change", args=[template.id])
                if template
                else reverse("admin:mailing_emailtemplate_add") + f"?client={client.id}",
                "action": "Review template" if template else "Create template",
                "help": "Review required variables and preview before sending.",
            },
            {
                "title": "Delivery evidence",
                "done": bool(delivered),
                "status": "Delivery recorded" if delivered else "Not verified",
                "url": reverse("mailing:transactional_message_detail", args=[delivered.id])
                if delivered
                else reverse("mailing:api_docs") + "#quickstart",
                "action": "View delivered message" if delivered else "Run the quickstart",
                "help": "A provider delivery event is evidence of delivery, not proof of inbox placement. A dry run does not verify delivery.",
            },
        ]
    }


def _client_form(request, client=None):
    form = SenderRowsClientForm(request.POST if request.method == "POST" else None, instance=client)
    if request.method == "POST" and form.is_valid():
        saved = create_or_update_client(actor=request.user, client=client, **form.cleaned_data)
        if client is None:
            request.session[ACTIVE_CLIENT_SESSION_KEY] = saved.id
        messages.success(request, "Client updated." if client else "Client created.")
        return redirect("mailing:client_detail", client_id=saved.id)
    return render(
        request,
        "mailing/operator/client_form.html",
        {"form": form, "mode": "edit" if client else "create", "client": client},
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def client_create(request):
    return _client_form(request)


@staff_member_required
@require_http_methods(["GET", "POST"])
def client_edit(request, client_id):
    return _client_form(request, get_object_or_404(Client.objects.select_related("organization"), pk=client_id))
