from django.urls import path

from jobs import schedule_views, views

app_name = "jobs"

urlpatterns = [
    path("api/tasks", views.tasks, name="tasks"),
    path("api/tasks/<uuid:task_id>", views.task_detail, name="task_detail"),
    path("api/tasks/<uuid:task_id>/complete", views.task_complete, name="task_complete"),
    path("api/tasks/<uuid:task_id>/fail", views.task_fail, name="task_fail"),
    path("jobs/dead-letters/", views.dead_letter_list, name="dead_letter_list"),
    path(
        "jobs/dead-letters/<uuid:task_id>/retry/",
        views.dead_letter_retry,
        name="dead_letter_retry",
    ),
    path("api/schedules", schedule_views.schedules, name="schedules"),
    path(
        "api/schedules/<uuid:schedule_id>",
        schedule_views.schedule_detail,
        name="schedule_detail",
    ),
]
