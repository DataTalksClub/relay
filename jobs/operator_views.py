"""Staff service operations. These screens never start a worker or scheduler."""

from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from jobs.models import Job, JobStatus, Schedule
from jobs.operator_actions import OperationConflict, change_schedule_state, retry_failed_job
from mailing.models import Client
from mailing.services.operator_ui import worker_health
from mailing.services.worker_status import sandbox_worker_statuses
from mailing.views import paginate, pagination_querystring


def _filtered(request, queryset):
    client_id = request.GET.get("client", "")
    if client_id:
        if client_id.isdecimal() and Client.objects.filter(pk=client_id).exists():
            queryset = queryset.filter(client_id=client_id)
        else:
            queryset = queryset.none()
    return queryset, client_id


def _return_url(request, record, *, schedule=False):
    path = reverse("jobs:schedule_list" if schedule else "jobs:job_list")
    candidate = request.POST.get("return", request.GET.get("return", ""))
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        parsed = None
    allowed = {path} if schedule else {path, reverse("jobs:dead_letter_list")}
    if parsed and not parsed.scheme and not parsed.netloc and parsed.path in allowed:
        return candidate
    return path + "?" + urlencode({"client": record.client_id})


def _detail_redirect(request, record, *, schedule=False):
    path = reverse("jobs:schedule_detail_ui" if schedule else "jobs:job_detail", args=[record.pk])
    return redirect(path + "?" + urlencode({"return": _return_url(request, record, schedule=schedule)}))


@staff_member_required
def job_list(request):
    jobs, client_id = _filtered(request, Job.objects.select_related("client", "schedule"))
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    counts = dict(jobs.values("status").annotate(total=Count("pk")).values_list("status", "total"))
    oldest = jobs.filter(status__in=[JobStatus.QUEUED, JobStatus.RETRYING]).order_by("run_after").first()
    if status in JobStatus.values:
        jobs = jobs.filter(status=status)
    if query:
        jobs = jobs.filter(Q(task_type__icontains=query) | Q(error__icontains=query) | Q(idempotency_key__icontains=query))
    return render(request, "jobs/job_list.html", {
        "page": paginate(request, jobs.order_by("-created_at"), per_page=25),
        "pagination_querystring": pagination_querystring(request),
        "clients": Client.objects.order_by("name"), "client_filter": client_id,
        "statuses": JobStatus.choices, "status_filter": status, "query": query,
        "counts": counts, "oldest": oldest, "observed_at": timezone.now(),
        "list_return_url": request.get_full_path(),
        "invalid_client_filter": bool(client_id and not Client.objects.filter(pk=client_id).exists()) if client_id.isdecimal() else bool(client_id),
    })


@staff_member_required
def job_detail(request, task_id):
    job = get_object_or_404(Job.objects.select_related("client", "schedule"), pk=task_id)
    return render(request, "jobs/job_detail.html", {"job": job, "return_url": _return_url(request, job)})


def _confirmation(request):
    return {
        "client_id": request.POST.get("record_client_id"),
        "revision": request.POST.get("revision"),
        "confirmed": request.POST.get("confirmed") == "yes",
    }


@staff_member_required
@require_POST
def job_retry(request, task_id):
    try:
        retry_failed_job(task_id, **_confirmation(request))
    except Job.DoesNotExist as exc:
        raise Http404 from exc
    except OperationConflict as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Job queued for a new attempt. The worker has not completed it yet.")
    return _detail_redirect(request, get_object_or_404(Job, pk=task_id))


@staff_member_required
def schedule_list(request):
    schedules, client_id = _filtered(request, Schedule.objects.select_related("client", "last_job"))
    state = request.GET.get("state", "")
    if state in {"enabled", "paused"}:
        schedules = schedules.filter(enabled=state == "enabled")
    return render(request, "jobs/schedule_list.html", {
        "page": paginate(request, schedules.order_by("client__name", "name"), per_page=25),
        "pagination_querystring": pagination_querystring(request),
        "clients": Client.objects.order_by("name"), "client_filter": client_id,
        "state_filter": state, "observed_at": timezone.now(),
        "list_return_url": request.get_full_path(),
        "invalid_client_filter": bool(client_id and not Client.objects.filter(pk=client_id).exists()) if client_id.isdecimal() else bool(client_id),
    })


@staff_member_required
def schedule_detail(request, schedule_id):
    schedule = get_object_or_404(Schedule.objects.select_related("client", "last_job"), pk=schedule_id)
    return render(request, "jobs/schedule_detail.html", {
        "schedule": schedule,
        "recent_jobs": schedule.jobs.order_by("-created_at")[:10],
        "schedule_timezone": str(timezone.get_current_timezone()),
        "return_url": _return_url(request, schedule, schedule=True),
    })


@staff_member_required
@require_POST
def schedule_action(request, schedule_id):
    try:
        schedule = change_schedule_state(schedule_id, request.POST.get("action"), **_confirmation(request))
    except Schedule.DoesNotExist as exc:
        raise Http404 from exc
    except OperationConflict as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Schedule resumed from its next future occurrence." if schedule.enabled else "Schedule paused. Already queued or running jobs continue.")
    return _detail_redirect(request, get_object_or_404(Schedule, pk=schedule_id), schedule=True)


@staff_member_required
def service_health(request):
    workers = sandbox_worker_statuses()
    label, tone = worker_health(workers)
    return render(request, "jobs/service_health.html", {
        "workers": workers, "health_label": label, "health_tone": tone,
        "observed_at": timezone.now(),
        "oldest_waiting": Job.objects.filter(status__in=[JobStatus.QUEUED, JobStatus.RETRYING]).order_by("run_after").first(),
    })
