from django.db import migrations
from django.utils import timezone


def recover_pending(apps, schema_editor):
    """Retain all rows; require explicit operator confirmation to resume sends."""
    alias = schema_editor.connection.alias
    Campaign = apps.get_model("mailing", "Campaign")
    Campaign.objects.using(alias).filter(status="snapshotting").update(status="draft", updated_at=timezone.now())
    Task = apps.get_model("django_tasks_database", "DBTaskResult")
    Run = apps.get_model("taskdeck", "TaskRun")
    paths = ["mailing.campaign_queue_tasks.run_campaign_snapshot",
             "mailing.transactional_retry_tasks.run_bulk_transactional_retry"]
    pending = Task.objects.using(alias).filter(task_path__in=paths, status="READY")
    ids = [str(value) for value in pending.values_list("pk", flat=True)]
    pending.update(status="FAILED", finished_at=timezone.now())
    Run.objects.using(alias).filter(result_id__in=ids).update(
        status="dead", finished_at=timezone.now(),
        message="Retired async request job. Review and confirm the synchronous action explicitly.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0041_fast_pages_indexes"),
        ("taskdeck", "0001_initial"),
        ("django_tasks_database", "0019_rename_django_task_new_ordering_idx_tasks_db_new_ordering_idx_and_more"),
    ]
    operations = [migrations.RunPython(recover_pending, migrations.RunPython.noop)]
