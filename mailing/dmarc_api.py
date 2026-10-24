"""Read-only operator access to durable DMARC evidence and parsed records."""

from django.http import HttpResponse, JsonResponse

from mailing.admin_api import ApiError, admin_endpoint, get_record
from mailing.models import DmarcArchive, DmarcAttachment


def serialize_archive(row, *, detail=False):
    data = {
        "id": row.pk,
        "message_id": row.message_id,
        "domain": row.domain.name,
        "reporter_domain": row.reporter_domain,
        "raw_key": row.raw_key,
        "created_at": row.created_at.isoformat(),
        "retain_until": row.retain_until.isoformat(),
        "parse_error": row.parse_error,
        "attachments": [],
    }
    for attachment in row.attachments.all():
        item = {
            "id": attachment.pk,
            "filename": attachment.filename,
            "storage_key": attachment.storage_key,
            "size_bytes": len(attachment.payload),
            "parse_error": attachment.parse_error,
            "reports": [],
        }
        for report in attachment.reports.all():
            parsed = {
                "id": report.pk,
                "reporter_org": report.reporter_org,
                "report_id": report.report_id,
                "date_begin": report.date_begin.isoformat(),
                "date_end": report.date_end.isoformat(),
                "policy": report.policy,
                "member_name": report.member_name,
            }
            if detail:
                parsed["records"] = list(
                    report.records.values(
                        "id", "source_ip", "count", "disposition", "dkim", "spf", "identifiers", "auth_results"
                    )
                )
            item["reports"].append(parsed)
        data["attachments"].append(item)
    return data


@admin_endpoint(["GET"])
def archives(request, pk=None):
    rows = (
        DmarcArchive.objects.select_related("domain").defer("raw_eml").prefetch_related("attachments__reports__records")
    )
    if pk is not None:
        return JsonResponse(serialize_archive(rows.get(pk=get_record(DmarcArchive, pk=pk).pk), detail=True))
    if request.GET.get("domain"):
        rows = rows.filter(domain__name=request.GET["domain"].lower())
    return JsonResponse({"results": [serialize_archive(row) for row in rows.order_by("-pk")[:100]]})


@admin_endpoint(["GET"])
def download(request, pk, attachment_id=None):
    archive = get_record(DmarcArchive, pk=pk)
    if attachment_id is None:
        payload, filename, content_type = archive.raw_eml, f"dmarc-{archive.pk}.eml", "message/rfc822"
    else:
        attachment = DmarcAttachment.objects.filter(pk=attachment_id, archive=archive).first()
        if attachment is None:
            raise ApiError("not_found", status=404)
        payload, filename, content_type = (
            attachment.payload,
            f"dmarc-attachment-{attachment.pk}.bin",
            "application/octet-stream",
        )
    response = HttpResponse(bytes(payload), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@admin_endpoint(["GET"])
def summary(request):
    """Per-domain aggregates: message volume, DMARC pass rate, SPF/DKIM fail rows."""
    from django.db.models import Count, Q, Sum  # noqa: PLC0415

    from mailing.models import DmarcRecord  # noqa: PLC0415

    rows = DmarcRecord.objects.select_related("report__attachment__archive__domain")
    if request.GET.get("domain"):
        rows = rows.filter(report__attachment__archive__domain__name=request.GET["domain"].lower())
    grouped = (
        rows.values("report__attachment__archive__domain__name")
        .annotate(
            records=Count("pk"),
            messages=Sum("count"),
            dmarc_pass=Sum("count", filter=Q(dkim="pass") | Q(spf="pass")),
            spf_fail=Count("pk", filter=~Q(spf="pass")),
            dkim_fail=Count("pk", filter=~Q(dkim="pass")),
        )
        .order_by("-messages")
    )
    results = []
    for row in grouped:
        messages = row["messages"] or 0
        results.append(
            {
                "domain": row["report__attachment__archive__domain__name"],
                "records": row["records"],
                "messages": messages,
                "dmarc_pass_messages": row["dmarc_pass"] or 0,
                "dmarc_pass_pct": round(100.0 * (row["dmarc_pass"] or 0) / messages, 1) if messages else 0.0,
                "spf_fail_rows": row["spf_fail"],
                "dkim_fail_rows": row["dkim_fail"],
            }
        )
    return JsonResponse({"results": results})
