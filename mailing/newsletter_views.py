"""Newsletter issues and blocks: operator UI (staff) and client API (Bearer key).

Both paths call the same functions in ``mailing.services.newsletters``; staff
operators and API keys of the owning client have identical abilities.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from mailing.models import NewsletterBlock, NewsletterIssue
from mailing.services import newsletters as svc
from mailing.services.api_errors import ApiValidationError


def _error_text(exc):
    parts = []
    for field, code in exc.errors.items():
        name = field.removeprefix("data.")
        label = svc.FIELD_LABELS.get(name.split(".")[0], name)
        readable = {"required": "is required", "invalid_url": "needs a full http(s):// link",
                    "too_long": "is too long", "already_exists": "is already used by another issue",
                    "invalid": "is not valid", "too_many": f"has too many addresses (max {svc.MAX_TEST_RECIPIENTS})",
                    "no_blocks": "has no blocks yet"}.get(code if isinstance(code, str) else "", str(code))
        parts.append(f"{label}: {readable}")
    return "; ".join(parts)


def _client(request):
    from mailing.views import require_active_client  # noqa: PLC0415 - avoid routing import cycle

    return require_active_client(request)


def _test_presets(request):
    presets = svc.test_presets()
    own = (request.user.email or "").lower()
    if own and own not in presets:
        presets.insert(0, own)
    return presets


# --------------------------------------------------------------------------- operator UI

@staff_member_required
def newsletter_list(request):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    if request.method == "POST":
        try:
            issue = svc.create_issue(client, request.POST.dict(), actor=svc.actor_label(request.user))
        except ApiValidationError as exc:
            messages.error(request, f"The issue was not created. {_error_text(exc)}")
        else:
            messages.success(request, "Issue created. Add blocks below.")
            return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    issues = NewsletterIssue.objects.filter(client=client)
    return render(request, "mailing/operator/newsletter_list.html", {"issues": issues, "active_client": client})


@staff_member_required
def newsletter_detail(request, issue_id):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    issue = get_object_or_404(NewsletterIssue, pk=issue_id, client=client)
    if request.method == "POST":
        try:
            svc.update_issue(issue, {key: request.POST.get(key, "") for key in
                                     ("subject", "preview_text", "issue_number", "publication_date")},
                             actor=svc.actor_label(request.user))
        except ApiValidationError as exc:
            messages.error(request, f"Not saved. {_error_text(exc)}")
        else:
            messages.success(request, "Issue details saved.")
        return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    blocks = list(issue.blocks.order_by("position", "id"))
    rows = [{"block": block, "label": svc.BLOCK_TYPES.get(block.block_type, {}).get("label", block.block_type),
             "html": svc.render_block_html(block.block_type, block.data or {})
             if block.block_type in svc.BLOCK_TYPES else ""} for block in blocks]
    preview = svc.render_issue(issue) if blocks else None
    return render(request, "mailing/operator/newsletter_detail.html", {
        "issue": issue, "rows": rows, "preview": preview, "active_client": client,
        "block_types": [(key, spec["label"]) for key, spec in svc.BLOCK_TYPES.items()],
        "test_presets": _test_presets(request), "max_test": svc.MAX_TEST_RECIPIENTS,
        "recent_tests": issue.test_sends.select_related("actor")[:10],
        "last_test": request.session.pop(f"newsletter_test_result_{issue.pk}", None),
    })


@staff_member_required
def newsletter_block_edit(request, issue_id, block_id=None):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    issue = get_object_or_404(NewsletterIssue, pk=issue_id, client=client)
    block = get_object_or_404(NewsletterBlock, pk=block_id, issue=issue) if block_id else None
    block_type = block.block_type if block else request.GET.get("type") or request.POST.get("block_type")
    if block_type not in svc.BLOCK_TYPES:
        messages.error(request, "Choose a block type.")
        return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    spec = svc.BLOCK_TYPES[block_type]
    values = dict(block.data) if block else dict(spec["defaults"])
    errors = {}
    if request.method == "POST":
        values = {name: request.POST.get(name, "") for name in spec["fields"]}
        actor = svc.actor_label(request.user)
        try:
            if block:
                svc.update_block(block, values, actor=actor)
            else:
                block = svc.add_block(issue, block_type, values, actor=actor)
        except ApiValidationError as exc:
            errors = {field.removeprefix("data.").split(".")[0]: code for field, code in exc.errors.items()}
            messages.error(request, f"Block not saved. {_error_text(exc)}")
        else:
            messages.success(request, "Block saved.")
            if request.POST.get("then") == "test":
                return redirect(f"{_detail_url(issue)}?test_block={block.pk}#newsletter-test-send")
            return redirect(f"{_detail_url(issue)}#block-{block.pk}")
    fields = [{"name": name, "label": svc.FIELD_LABELS[name], "kind": svc.FIELD_KINDS[name],
               "required": name in spec["required"], "value": values.get(name, ""), "error": errors.get(name, "")}
              for name in spec["fields"]]
    preview_html = ""
    if block and not errors:
        preview_html = svc.render_issue(issue, block=block)["html_body"]
    return render(request, "mailing/operator/newsletter_block_form.html", {
        "issue": issue, "block": block, "block_type": block_type, "spec": spec, "fields": fields,
        "preview_html": preview_html, "active_client": client,
    })


def _detail_url(issue):
    from django.urls import reverse  # noqa: PLC0415

    return reverse("mailing:newsletter_detail", args=[issue.pk])


@staff_member_required
@require_POST
def newsletter_block_action(request, issue_id, block_id):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    issue = get_object_or_404(NewsletterIssue, pk=issue_id, client=client)
    block = get_object_or_404(NewsletterBlock, pk=block_id, issue=issue)
    action = request.POST.get("action")
    if action in {"up", "down"}:
        svc.move_block(block, action)
    elif action == "delete" and request.POST.get("confirm_delete") == "1":
        svc.delete_block(block, actor=svc.actor_label(request.user))
        messages.success(request, "Block deleted.")
    else:
        messages.error(request, "Confirm the deletion before removing a block.")
    return redirect(_detail_url(issue))


@staff_member_required
@require_POST
def newsletter_test_send(request, issue_id):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    issue = get_object_or_404(NewsletterIssue, pk=issue_id, client=client)
    block = None
    if request.POST.get("block_id"):
        block = get_object_or_404(NewsletterBlock, pk=request.POST["block_id"], issue=issue)
    raw = request.POST.getlist("preset") + [request.POST.get("emails", "")]
    try:
        emails = svc.validate_test_emails(" ".join(raw))
        result = svc.send_test(issue, emails, block=block, actor=request.user, via="ui")
    except ApiValidationError as exc:
        messages.error(request, f"Test not sent. {_error_text(exc) or 'Enter at least one email address.'}")
        return redirect(f"{_detail_url(issue)}#newsletter-test-send")
    lines = []
    for item in result["results"]:
        text = {"sent": "accepted by the email provider", "failed": "FAILED", "timeout": "NO ANSWER (timeout)",
                "dry_run": "NOT SENT (dry run)", "not_attempted": "not attempted"}[item["status"]]
        lines.append(f"{item['email']}: {text}{' — ' + item['error'] if item['error'] else ''}")
    summary = f"Test “{result['subject']}” from {result['from']}. " + " | ".join(lines)
    if result["status"] == "sent":
        messages.success(request, summary)
    else:
        messages.error(request, summary)
    request.session[f"newsletter_test_result_{issue.pk}"] = {"ok": result["status"] == "sent",
                                                            "subject": result["subject"], "lines": lines}
    return redirect(f"{_detail_url(issue)}#newsletter-test-send")


@staff_member_required
def newsletter_preview(request, issue_id):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    issue = get_object_or_404(NewsletterIssue, pk=issue_id, client=client)
    response = HttpResponse(svc.render_issue(issue)["html_body"], content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src https: data:; style-src 'unsafe-inline'"
    return response


# --------------------------------------------------------------------------- client API

def _api(request, allowed):
    from mailing.views import authenticate_api_request, method_not_allowed_response  # noqa: PLC0415

    if request.method not in allowed:
        return None, method_not_allowed_response(sorted(allowed))
    return authenticate_api_request(request)


def _api_call(request, allowed, handler):
    from mailing.views import json_request_body, validation_error_response  # noqa: PLC0415

    client, error = _api(request, allowed)
    if error:
        return error
    try:
        data = json_request_body(request) if request.method in {"POST", "PUT"} else {}
        payload, status = handler(client, data)
    except ApiValidationError as exc:
        return validation_error_response(exc)
    return JsonResponse(payload, status=status)


def _issue_for(client, key):
    issue = NewsletterIssue.objects.filter(client=client, key=svc.normalize_issue_key(key)).first()
    if issue is None:
        raise ApiValidationError({"key": "not_found"}, status_code=404)
    return issue


def _block_for(issue, block_id):
    block = NewsletterBlock.objects.filter(issue=issue, pk=block_id).first()
    if block is None:
        raise ApiValidationError({"block_id": "not_found"}, status_code=404)
    return block


def _api_actor(client):
    return svc.actor_label(api_client=client)


@csrf_exempt
def api_newsletter_block_types(request):
    return _api_call(request, {"GET"}, lambda client, data: (svc.block_types_payload(), 200))


@csrf_exempt
def api_newsletter_issues(request):
    def handler(client, data):
        issues = NewsletterIssue.objects.filter(client=client)[:100]
        return {"issues": [svc.issue_payload(issue, with_blocks=False) for issue in issues]}, 200

    return _api_call(request, {"GET"}, handler)


@csrf_exempt
def api_newsletter_issue(request, key):
    def handler(client, data):
        if request.method == "GET":
            return {"issue": svc.issue_payload(_issue_for(client, key))}, 200
        issue, created = svc.upsert_issue(client, key, data, actor=_api_actor(client))
        return {"issue": svc.issue_payload(issue), "created": created}, 201 if created else 200

    return _api_call(request, {"GET", "PUT"}, handler)


@csrf_exempt
def api_newsletter_blocks(request, key):
    def handler(client, data):
        issue = _issue_for(client, key)
        position = data.get("position")
        block = svc.add_block(issue, data.get("type"), data.get("data", {}),
                              position=position if isinstance(position, int) else None, actor=_api_actor(client))
        return {"block": svc.block_payload(block), "issue": svc.issue_payload(issue)}, 201

    return _api_call(request, {"POST"}, handler)


@csrf_exempt
def api_newsletter_block(request, key, block_id):
    def handler(client, data):
        issue = _issue_for(client, key)
        block = _block_for(issue, block_id)
        if request.method == "GET":
            return {"block": svc.block_payload(block)}, 200
        if request.method == "DELETE":
            svc.delete_block(block, actor=_api_actor(client))
            return {"deleted": True, "issue": svc.issue_payload(issue)}, 200
        position = data.get("position")
        svc.update_block(block, data.get("data", block.data), block_type=data.get("type") or None,
                         position=position if isinstance(position, int) else None, actor=_api_actor(client))
        block.refresh_from_db()
        return {"block": svc.block_payload(block), "issue": svc.issue_payload(issue)}, 200

    return _api_call(request, {"GET", "PUT", "DELETE"}, handler)


@csrf_exempt
def api_newsletter_preview(request, key):
    def handler(client, data):
        issue = _issue_for(client, key)
        block = _block_for(issue, data["block_id"]) if data.get("block_id") else None
        return {"preview": svc.render_issue(issue, block=block)}, 200

    return _api_call(request, {"POST"}, handler)


@csrf_exempt
def api_newsletter_test_send(request, key):
    def handler(client, data):
        issue = _issue_for(client, key)
        block = _block_for(issue, data["block_id"]) if data.get("block_id") else None
        emails = svc.validate_test_emails(data.get("emails"))
        result = svc.send_test(issue, emails, block=block, via="api")
        return result, 200 if result["status"] == "sent" else 502

    return _api_call(request, {"POST"}, handler)
