"""Newsletter issues and blocks: operator UI (staff) and client API (Bearer key).

Both paths call the same functions in ``mailing.services.newsletters``; staff
operators and API keys of the owning client have identical abilities.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from mailing.models import Audience, CampaignStatus, NewsletterBlock, NewsletterIssue
from mailing.services import newsletters as svc
from mailing.services.api_errors import ApiValidationError
from mailing.services.newsletter_templates import REFILL_SOURCES


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

MARKDOWN_HINT = "Markdown: **bold**, [link text](https://…), start lines with - for a list."
FIELD_HINTS = {
    "issue_label": "Shown above the intro, e.g. #303 · November 16, 2026.",
    "events": ("One event per line: when | title | link | with Speaker. Example: "
               "Mon, November 16 · 5:00 PM CET · Webinar | Course launch | https://luma.com/abc | with Alexey Grigorev"),
    "links": ("One link per line: title | link | optional note. Example: "
              "How we test LLM apps | https://example.com/post | a practical guide"),
    "bullets": "One bullet point per line.",
    "image_url": "Paste the address of an image (https://…). It is shown full width.",
    "image_link": "Where a click on the image leads. Usually the same as the button link.",
    "cta_url": "Full link starting with https://",
    "html": "Raw HTML, inserted as is. Use only when no other block type fits.",
}
FIELD_ERROR_TEXT = {
    "required": "Fill this in.",
    "invalid_url": "Enter a full link starting with https://",
    "too_long": "This is too long.",
    "unknown_field": "This field does not belong to this block.",
    "must_be_string": "Enter text.",
}
AUTO_SOURCE_PREFIXES = ("website:", "substack:")
# Card fields most blocks leave empty: shown under "More fields" unless they hold a value.
EXTRA_CARD_FIELDS = {"kicker", "meta", "image_link", "bullets", "closing"}


def _short_label(name):
    return svc.FIELD_LABELS.get(name, name).split(" (")[0]


def _field_errors(exc):
    """Map ApiValidationError codes to one plain sentence per form field."""
    errors = {}
    for field, code in exc.errors.items():
        parts = field.removeprefix("data.").split(".")
        name = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            text = f"Line {int(parts[1]) + 1} needs a full link starting with https://"
        else:
            text = FIELD_ERROR_TEXT.get(code if isinstance(code, str) else "", "This is not valid.")
        errors[name] = f"{errors[name]} {text}" if name in errors else text
    return errors


def _field_rows(block_type, values, errors=None):
    spec = svc.BLOCK_TYPES[block_type]
    errors = errors or {}
    rows = []
    for name in spec["fields"]:
        kind = svc.FIELD_KINDS[name]
        default = spec["defaults"].get(name, "")
        value = values.get(name, "")
        hint = FIELD_HINTS.get(name) or (MARKDOWN_HINT if kind == "markdown" else "")
        if default and name not in FIELD_HINTS:
            hint = f"Leave empty to use “{default}”." + (f" {hint}" if hint else "")
        rows.append({
            "name": name, "label": _short_label(name), "kind": kind, "hint": hint, "default": default,
            "required": name in spec["required"] and not default, "value": value, "error": errors.get(name, ""),
            "multiline": kind in {"markdown", "lines", "html"},
            "rows": {"html": 12, "lines": 6, "markdown": 4}.get(kind, 1),
            "thumbnail": value if name == "image_url" and value and svc._valid_url(value) else "",
            "extra": spec["render"] == "card" and name in EXTRA_CARD_FIELDS and not value and name not in errors,
        })
    return rows


def _field_groups(rows):
    return {"fields": [row for row in rows if not row["extra"]], "extra_fields": [row for row in rows if row["extra"]]}


def _block_status(block, problems):
    if problems:
        return {"label": "Needs content", "tone": "warning"}
    if (block.source or "").startswith(AUTO_SOURCE_PREFIXES):
        return {"label": "Auto-filled", "tone": "info"}
    return {"label": "Ready", "tone": "success"}


def _block_preview_doc(block):
    return svc.render_block_preview(block.block_type, block.data or {})


def _block_rows(issue, blocks, *, open_block_id=None, form_state=None):
    rows = []
    for index, block in enumerate(blocks):
        problems = svc.block_problems(block)
        known = block.block_type in svc.BLOCK_TYPES
        state = form_state if form_state and form_state.get("block_id") == block.pk else None
        values = state["values"] if state else dict(block.data or {})
        rows.append({
            "block": block, "number": index + 1, "label": svc.block_label(block.block_type), "known": known,
            "problems": _display_problems(problems), "status": _block_status(block, problems),
            "open": bool(state) or block.pk == open_block_id or (bool(problems) and known),
            **_field_groups(_field_rows(block.block_type, values, state["errors"] if state else None) if known else []),
            "preview_doc": _block_preview_doc(block) if known else "",
            "refillable": (block.source or "") in REFILL_SOURCES,
            "auto": (block.source or "").startswith(AUTO_SOURCE_PREFIXES),
            "sponsor_switch": block.block_type in {"sponsor", "sponsor_secondary"} and block.needs_review,
            "first": index == 0, "last": index == len(blocks) - 1,
        })
    return rows


def _audience_choice(client, issue):
    audiences = list(Audience.objects.filter(organization_id=client.organization_id).order_by("name"))
    selected = None
    if issue.campaign_id and issue.campaign is not None:
        selected = issue.campaign.audience_id
    elif len(audiences) == 1:
        selected = audiences[0].pk
    else:
        selected = next((item.pk for item in audiences if item.slug == "dtc"), None)
    return audiences, selected


def _editor_context(request, client, issue, *, open_block_id=None, form_state=None, new_block=None,
                    details_state=None):
    blocks = list(issue.blocks.order_by("position", "id"))
    rows = _block_rows(issue, blocks, open_block_id=open_block_id, form_state=form_state)
    problems = svc.issue_problems(issue)
    numbers = {row["block"].pk: row["number"] for row in rows}
    checklist = [{**item, "number": numbers.get(item["block_id"]), "problems": _display_problems(item["problems"])}
                 if item["block_id"] else item for item in problems]
    audiences, selected_audience = _audience_choice(client, issue)
    campaign = issue.campaign
    campaign_badge = None
    if campaign is not None:
        from mailing.services.operator_ui import campaign_status_badge  # noqa: PLC0415

        campaign_badge = campaign_status_badge(campaign.status)
    if new_block is not None:
        position = new_block.get("position")
        new_block["index"] = position if position is not None and position < len(blocks) else None
        new_block.update(_field_groups(_field_rows(new_block["type"], new_block.get("values", {}),
                                                   new_block.get("errors"))))
        new_block["label"] = svc.block_label(new_block["type"])
    test_block = request.GET.get("test_block", "")
    return {
        "issue": issue, "rows": rows, "active_client": client, "checklist": checklist,
        "inbox_preview": svc.effective_preview_text(issue),
        "problem_blocks": sum(1 for item in checklist if item["block_id"]),
        "preview_html": svc.render_issue(issue, mark_unfinished=True)["html_body"] if blocks else "",
        "block_types": [(key, spec["label"]) for key, spec in svc.BLOCK_TYPES.items()],
        "new_block": new_block, "details": details_state or {
            "subject": issue.subject, "preview_text": issue.preview_text,
            "issue_number": "" if issue.issue_number is None else issue.issue_number,
            "publication_date": issue.publication_date.isoformat() if issue.publication_date else "",
            "errors": {}},
        "audiences": audiences, "selected_audience": selected_audience, "campaign": campaign,
        "campaign_badge": campaign_badge,
        "campaign_editable": campaign is None or campaign.status == CampaignStatus.DRAFT,
        "test_presets": _test_presets(request), "max_test": svc.MAX_TEST_RECIPIENTS,
        "test_block": test_block,
        "recent_tests": issue.test_sends.select_related("actor")[:10],
        "last_test": request.session.pop(f"newsletter_test_result_{issue.pk}", None),
    }


def _render_editor(request, client, issue, *, status=200, **kwargs):
    return render(request, "mailing/operator/newsletter_detail.html",
                  _editor_context(request, client, issue, **kwargs), status=status)


def _issue_for_request(request, issue_id):
    client = _client(request)
    if client is None:
        return None, None
    return client, get_object_or_404(NewsletterIssue.objects.select_related("campaign"), pk=issue_id, client=client)


def _report_summary(issue, report):
    created = [entry for entry in report if entry["block_id"]]
    if not created:
        return "Newsletter created. Add the first block below."
    auto = sum(1 for entry in created if entry["status"] == "auto")
    attention = sum(1 for item in svc.issue_problems(issue) if item["block_id"])
    text = f"Newsletter created with {len(created)} block{pluralize(len(created))}"
    text += f" ({auto} filled automatically)." if auto else "."
    if attention:
        text += (f" {attention} block{pluralize(attention)} need{'s' if attention == 1 else ''} your content: "
                 "see the list below.")
    return text


REVIEW_PROBLEM = "Needs your review"


def _display_problems(problems):
    """The service flags every unreviewed block; say what to do instead of repeating the flag."""
    specific = [problem for problem in problems if problem != REVIEW_PROBLEM]
    if specific:
        return specific
    return ["Check the content and press Save block"] if problems else []


@staff_member_required
def newsletter_list(request):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    if request.method == "POST":
        # Kept for old bookmarks and scripts; the UI creates issues on the "New newsletter" page.
        try:
            issue = svc.create_issue(client, request.POST.dict(), actor=svc.actor_label(request.user))
        except ApiValidationError as exc:
            messages.error(request, f"The issue was not created. {_error_text(exc)}")
        else:
            messages.success(request, "Issue created. Add blocks below.")
            return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    issues = list(NewsletterIssue.objects.filter(client=client).select_related("campaign")
                  .annotate(block_count=Count("blocks")))
    from mailing.services.operator_ui import campaign_status_badge  # noqa: PLC0415

    rows = [{"issue": issue, "attention": sum(1 for item in svc.issue_problems(issue) if item["block_id"]),
             "campaign_badge": campaign_status_badge(issue.campaign.status) if issue.campaign else None}
            for issue in issues]
    return render(request, "mailing/operator/newsletter_list.html",
                  {"rows": rows, "issues": issues, "active_client": client})


ISSUE_FIELDS = ("key", "subject", "preview_text", "issue_number", "publication_date")
ISSUE_ERROR_TEXT = {
    "subject": {"required": "Enter a subject.", "too_long": "Keep the subject under 255 characters."},
    "preview_text": {"too_long": "Keep the preview text under 255 characters."},
    "issue_number": {"invalid": "Enter a whole number, e.g. 303."},
    "publication_date": {"invalid": "Enter a date like 2026-11-16."},
    "key": {"required": "Enter a key or a subject.", "invalid": "Use letters, numbers and dashes, e.g. weekly-303.",
            "already_exists": "Another newsletter already uses this key."},
}


def _issue_errors(exc):
    return {field: ISSUE_ERROR_TEXT.get(field, {}).get(code if isinstance(code, str) else "", "This is not valid.")
            for field, code in exc.errors.items()}


@staff_member_required
@require_http_methods(["GET", "POST"])
def newsletter_new(request):
    client = _client(request)
    if client is None:
        return redirect("mailing:dashboard")
    templates = svc.newsletter_templates()
    template_key = request.POST.get("template") or request.GET.get("template") or "dtc-weekly"
    if template_key not in svc.NEWSLETTER_TEMPLATES:
        template_key = "dtc-weekly"
    defaults = svc.issue_defaults(client, template_key)
    values = {name: "" if defaults.get(name) is None else str(defaults.get(name)) for name in ISSUE_FIELDS}
    errors, existing = {}, None
    if request.method == "POST":
        values = {name: request.POST.get(name, "").strip() for name in ISSUE_FIELDS}
        try:
            issue, report = svc.create_issue_from_template(client, template_key, values,
                                                           actor=svc.actor_label(request.user))
        except ApiValidationError as exc:
            errors = _issue_errors(exc)
            if exc.errors.get("key") == "already_exists":
                key = svc.normalize_issue_key(values["key"] or values["subject"])
                existing = NewsletterIssue.objects.filter(client=client, key=key).first()
        else:
            messages.success(request, _report_summary(issue, report))
            skipped = [entry["note"] for entry in report if entry["status"] == "skipped" and entry["note"]]
            if skipped:
                messages.info(request, "Not added: " + " ".join(dict.fromkeys(skipped)))
            return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    return render(request, "mailing/operator/newsletter_new.html", {
        "active_client": client, "templates": templates, "template_key": template_key, "values": values,
        "errors": errors, "existing": existing, "advanced_open": bool(errors.get("key")),
    })


@staff_member_required
@require_http_methods(["GET", "POST"])
def newsletter_detail(request, issue_id):
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
    if request.method == "POST":
        data = {key: request.POST.get(key, "") for key in ("subject", "preview_text", "issue_number",
                                                           "publication_date")}
        try:
            svc.update_issue(issue, data, actor=svc.actor_label(request.user))
        except ApiValidationError as exc:
            messages.error(request, "Issue details not saved. Check the highlighted fields.")
            return _render_editor(request, client, issue, details_state={**data, "errors": _issue_errors(exc)})
        messages.success(request, "Issue details saved.")
        return redirect(f"{_detail_url(issue)}#newsletter-details")
    add_type = request.GET.get("add", "")
    new_block = None
    if add_type in svc.BLOCK_TYPES:
        new_block = {"type": add_type, "position": _position(request.GET.get("at")),
                     "values": dict(svc.BLOCK_TYPES[add_type]["defaults"])}
    open_block = request.GET.get("edit", "")
    return _render_editor(request, client, issue, new_block=new_block,
                          open_block_id=int(open_block) if open_block.isdigit() else None)


def _position(value):
    try:
        position = int(value)
    except (TypeError, ValueError):
        return None
    return position if position >= 0 else None


@staff_member_required
@require_http_methods(["GET", "POST"])
def newsletter_block_edit(request, issue_id, block_id=None):
    """Save one block (existing or new). GET renders the editor with that block's form open."""
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
    block = get_object_or_404(NewsletterBlock, pk=block_id, issue=issue) if block_id else None
    block_type = block.block_type if block else request.POST.get("block_type") or request.GET.get("type")
    if block_type not in svc.BLOCK_TYPES:
        messages.error(request, "Choose a block type.")
        return redirect("mailing:newsletter_detail", issue_id=issue.pk)
    position = _position(request.POST.get("position") or request.GET.get("at"))
    spec = svc.BLOCK_TYPES[block_type]
    if request.method != "POST":
        if block:
            return _render_editor(request, client, issue, open_block_id=block.pk)
        return _render_editor(request, client, issue, new_block={"type": block_type, "position": position,
                                                                  "values": dict(spec["defaults"])})
    values = {name: request.POST.get(name, "") for name in spec["fields"]}
    actor = svc.actor_label(request.user)
    try:
        if block:
            svc.update_block(block, values, actor=actor)
        else:
            block = svc.add_block(issue, block_type, values, position=position, actor=actor)
    except ApiValidationError as exc:
        errors = _field_errors(exc)
        messages.error(request, "Block not saved. Fix the highlighted fields and save again.")
        if block:
            return _render_editor(request, client, issue,
                                  form_state={"block_id": block.pk, "values": values, "errors": errors})
        return _render_editor(request, client, issue, new_block={"type": block_type, "position": position,
                                                                  "values": values, "errors": errors})
    messages.success(request, f"Saved block {block.position + 1}: {svc.block_label(block.block_type)}.")
    if request.POST.get("then") == "test":
        return redirect(f"{_detail_url(issue)}?test_block={block.pk}#newsletter-test-send")
    return redirect(f"{_detail_url(issue)}#block-{block.pk}")


def _detail_url(issue):
    return reverse("mailing:newsletter_detail", args=[issue.pk])


REFILL_ERRORS = {
    "no_source": "This block was not filled automatically, so there is nothing to refresh it from.",
    "source_unavailable": ("Could not load fresh content right now (the website may be unreachable or has "
                           "nothing for this date). The block was not changed."),
}


@staff_member_required
@require_POST
def newsletter_block_action(request, issue_id, block_id):
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
    block = get_object_or_404(NewsletterBlock, pk=block_id, issue=issue)
    action = request.POST.get("action")
    actor = svc.actor_label(request.user)
    anchor = f"#block-{block.pk}"
    if action in {"up", "down"}:
        svc.move_block(block, action)
    elif action == "duplicate":
        copy = svc.duplicate_block(block, actor=actor)
        messages.success(request, f"Block duplicated. The copy is block {copy.position + 1}.")
        anchor = f"#block-{copy.pk}"
    elif action == "refill":
        try:
            svc.refill_block(block, actor=actor)
        except ApiValidationError as exc:
            if "publication_date" in exc.errors:
                messages.error(request, "Set the send date in the issue details first: content is looked up by date.")
            else:
                messages.error(request, REFILL_ERRORS.get(exc.errors.get("block"), "The block was not refreshed."))
        else:
            messages.success(request, f"Block {block.position + 1} refreshed from its source. Check it before sending.")
    elif action == "change_type":
        new_type = request.POST.get("new_type", "")
        if new_type == block.block_type:
            messages.info(request, "The block already has this type.")
        else:
            try:
                svc.change_block_type(block, new_type, actor=actor)
            except ApiValidationError:
                messages.error(request, "Choose a block type from the list.")
            else:
                messages.success(request, f"Block {block.position + 1} is now “{svc.block_label(new_type)}”. "
                                          "Fill in its fields and save.")
    elif action == "delete" and request.POST.get("confirm_delete") == "1":
        svc.delete_block(block, actor=actor)
        messages.success(request, "Block deleted.")
        anchor = "#newsletter-blocks"
    else:
        messages.error(request, "Tick the confirmation box before deleting a block.")
    return redirect(f"{_detail_url(issue)}{anchor}")


ASSEMBLE_ERRORS = {
    ("audience", "invalid"): "Choose who receives the newsletter (the audience).",
    ("issue", "has_problems"): "Some blocks still need your attention. Fix the items in the checklist first.",
    ("campaign", "not_draft"): ("The linked campaign was already queued, scheduled or sent, so the blocks can no "
                                "longer change it. Nothing was changed."),
    ("campaign", "key_in_use"): ("Another campaign of this client already uses the key “{key}”. Create the "
                                 "newsletter again with a different key (under Advanced), or rename that campaign."),
}


@staff_member_required
@require_POST
def newsletter_assemble(request, issue_id):
    """Create or update the issue's *draft* campaign. Never queues or sends."""
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
    audience = request.POST.get("audience", "")
    try:
        campaign, created = svc.assemble_campaign(issue, audience=int(audience) if audience.isdigit() else None,
                                                  actor=svc.actor_label(request.user))
    except ApiValidationError as exc:
        field, code = next(iter(exc.errors.items()), ("", ""))
        text = ASSEMBLE_ERRORS.get((field, code), "The campaign draft could not be created: the content is "
                                                  "not valid. Check the blocks and try again.")
        messages.error(request, text.format(key=issue.key))
        return redirect(f"{_detail_url(issue)}#newsletter-campaign")
    if created:
        messages.success(request, "Campaign draft created from the newsletter. Nothing has been sent. "
                                  "Review it, send a test and schedule it here.")
    else:
        messages.success(request, "Campaign draft updated with the latest blocks. Nothing has been sent.")
    return redirect("mailing:campaign_detail", campaign_id=campaign.pk)


@staff_member_required
@require_POST
def newsletter_test_send(request, issue_id):
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
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
    client, issue = _issue_for_request(request, issue_id)
    if client is None:
        return redirect("mailing:dashboard")
    response = HttpResponse(svc.render_issue(issue)["html_body"], content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src https: data:; style-src 'unsafe-inline'"
    return response


_WEB_VIEW_CSP = "sandbox allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation; " \
    "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'"


@require_http_methods(["GET", "HEAD"])
def newsletter_web_view(request, token):
    """Public "View this email in your browser" page (the link at the top of every issue).

    The token is signed, so issues cannot be enumerated. Once the issue's campaign
    has left draft this shows the HTML that went out; before that, the issue as
    it would be sent. Per-recipient preferences/unsubscribe links are disabled:
    the page has no recipient. Sandboxed like the operator preview.
    """
    issue = svc.issue_for_web_view_token(token)
    if issue is None:
        return HttpResponse("Not found", status=404, content_type="text/plain; charset=utf-8")
    campaign = issue.campaign
    if campaign is not None and campaign.status != CampaignStatus.DRAFT and campaign.html_body:
        html = campaign.html_body
    else:
        html = svc.render_issue(issue)["html_body"]
    response = HttpResponse(svc.disable_account_links(html), content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = _WEB_VIEW_CSP
    response["X-Robots-Tag"] = "noindex"
    response["Referrer-Policy"] = "no-referrer"
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
def api_newsletter_templates(request):
    return _api_call(request, {"GET"}, lambda client, data: ({"templates": svc.newsletter_templates()}, 200))


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
        template = data.get("template")
        if template not in (None, ""):
            if not isinstance(template, str) or template not in svc.NEWSLETTER_TEMPLATES:
                raise ApiValidationError({"template": "unknown"})
            normalized = svc.normalize_issue_key(key)
            if not NewsletterIssue.objects.filter(client=client, key=normalized).exists():
                fields = {name: value for name, value in data.items() if name != "template"}
                issue, report = svc.create_issue_from_template(client, template, {**fields, "key": normalized},
                                                               actor=_api_actor(client))
                return {"issue": svc.issue_payload(issue), "created": True, "autofill": report}, 201
        fields = {name: value for name, value in data.items() if name != "template"}
        issue, created = svc.upsert_issue(client, key, fields, actor=_api_actor(client))
        return {"issue": svc.issue_payload(issue), "created": created}, 201 if created else 200

    return _api_call(request, {"GET", "PUT"}, handler)


@csrf_exempt
def api_newsletter_blocks(request, key):
    def handler(client, data):
        issue = _issue_for(client, key)
        position = data.get("position")
        needs_review = data.get("needs_review", False)
        if not isinstance(needs_review, bool):
            raise ApiValidationError({"needs_review": "must_be_boolean"})
        block = svc.add_block(issue, data.get("type"), data.get("data", {}),
                              position=position if isinstance(position, int) else None, actor=_api_actor(client),
                              needs_review=needs_review)
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


@csrf_exempt
def api_newsletter_block_refill(request, key, block_id):
    def handler(client, data):
        issue = _issue_for(client, key)
        block = svc.refill_block(_block_for(issue, block_id), actor=_api_actor(client))
        return {"block": svc.block_payload(block), "issue": svc.issue_payload(issue)}, 200

    return _api_call(request, {"POST"}, handler)


@csrf_exempt
def api_newsletter_assemble(request, key):
    """Create or update the issue's *draft* campaign. Never queues or sends."""
    from mailing.services.api import campaign_payload  # noqa: PLC0415

    def handler(client, data):
        issue = _issue_for(client, key)
        campaign, created = svc.assemble_campaign(issue, audience=data.get("audience"), actor=_api_actor(client))
        return {"campaign": campaign_payload(campaign), "created": created}, 201 if created else 200

    return _api_call(request, {"POST"}, handler)
