from django.urls import path

from jobs import schedule_views, views

app_name = "jobs"

urlpatterns = [
    path("api/tasks", views.tasks, name="tasks"),
    path("api/tasks/<uuid:task_id>", views.task_detail, name="task_detail"),
    path("api/schedules", schedule_views.schedules, name="schedules"),
    path(
        "api/schedules/<uuid:schedule_id>",
        schedule_views.schedule_detail,
        name="schedule_detail",
    ),
]
