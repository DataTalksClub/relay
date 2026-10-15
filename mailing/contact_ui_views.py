"""Review global contact changes before applying them."""
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from mailing.forms import ContactStateForm
from mailing.models import Contact
from mailing.services.operator_management import update_contact_state
from mailing.views import contact_record_redirect, get_contact_by_email_or_404, require_active_client

SALT = "relay.contact-state-review"


def state_values(contact):
    return {
        "verified_state": "verified" if contact.verified_at else "unverified",
        "email_validation_status": contact.email_validation_status,
        "email_validation_reason": contact.email_validation_reason,
        "global_unsubscribed": bool(contact.global_unsubscribed_at),
        "hard_bounced": bool(contact.hard_bounced_at),
        "complained": bool(contact.complained_at),
    }


def state_changes(form, before):
    changes = []
    for name, field in form.fields.items():
        after = form.cleaned_data[name]
        if before[name] == after or (name == "verified_state" and after == "unchanged"):
            continue
        def label(value):
            if isinstance(value, bool):
                return "Yes" if value else "No"
            return dict(getattr(field, "choices", [])).get(value, value) or "None"
        changes.append({"label": form[name].label, "before": label(before[name]), "after": label(after)})
    return changes


@staff_member_required
@require_POST
def contact_state_update(request, contact_email):

    client = require_active_client(request)
    if client is None:
        return redirect("mailing:dashboard")
    contact = get_contact_by_email_or_404(contact_email)
    token = request.POST.get("review_token")
    if token:
        try:
            reviewed = signing.loads(token, salt=SALT, max_age=600)
        except signing.BadSignature:
            messages.error(request, "This review expired or changed. Review the global state again.")
            return contact_record_redirect(request, contact)
        if reviewed["contact"] != contact.id or reviewed["client"] != client.id or reviewed["actor"] != request.user.id:
            messages.error(request, "This review belongs to another contact or workspace. Review again.")
            return contact_record_redirect(request, contact)
        form = ContactStateForm(reviewed["values"])
        if form.is_valid():
            with transaction.atomic():
                contact = Contact.objects.select_for_update().get(pk=contact.pk)
                if state_values(contact) != reviewed["before"]:
                    messages.warning(request, "This contact changed since your review. Review the latest state again.")
                    return contact_record_redirect(request, contact)
                update_contact_state(
                    actor=request.user, contact=contact,
                    verified_state=form.cleaned_data["verified_state"],
                    validation_status=form.cleaned_data["email_validation_status"],
                    validation_reason=form.cleaned_data["email_validation_reason"],
                    suppression_flags={key: form.cleaned_data[key] for key in ("global_unsubscribed", "hard_bounced", "complained")},
                )
            messages.success(request, "Global contact state updated across all clients and audiences.")
        return contact_record_redirect(request, contact)
    form = ContactStateForm(request.POST, auto_id="review_%s")
    before = state_values(contact)
    changes = state_changes(form, before) if form.is_valid() else []
    review_token = signing.dumps({
        "contact": contact.id, "client": client.id, "actor": request.user.id,
        "before": before, "values": form.cleaned_data,
    }, salt=SALT) if form.is_valid() and changes else ""
    return render(request, "mailing/operator/contact_state_review.html", {
        "contact": contact, "form": form, "changes": changes,
        "review_token": review_token, "active_client": client,
        "contact_url": contact_record_redirect(request, contact).url,
    })
