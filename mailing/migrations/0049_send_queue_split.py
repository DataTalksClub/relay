"""Move pre-split READY send tasks off the `default` queue.

Before the transactional/campaign queue split, every send task was enqueued
on `default`. After this deploy the `campaign-worker` consumes only
`campaign` and the `worker` consumes `transactional` + `default`, so READY
campaign batches left on `default` would sit there with no consumer that ever
runs a campaign batch quickly (the transactional worker only drains `default`
for the small jobs/test/placeholder tasks that still enqueue there).

This migration re-points the stranded rows instead of leaving them:
  mailing.tasks.send_campaign_email_batch  default -> campaign
  mailing.tasks.send_transactional_email   default -> transactional
  mailing.tasks.send_transactional_email_batch  default -> transactional

Only READY rows move (including delayed retries with a future run_after:
they are READY, just not runnable yet). RUNNING rows are left alone -- they
are claimed by a live worker, and deploys must not happen mid-send anyway.
Every other task_path on `default` (jobs.execute_job, send_campaign_test,
process_email_event) stays: the transactional worker still drains `default`.

Reverse is a deliberate no-op: after the split, new tasks are enqueued
directly on `campaign`/`transactional`, so moving them back to `default`
would strand them in the other direction.
"""

from django.db import migrations

CAMPAIGN_TASK_PATHS = frozenset({
    "mailing.tasks.send_campaign_email_batch",
})

TRANSACTIONAL_TASK_PATHS = frozenset({
    "mailing.tasks.send_transactional_email",
    "mailing.tasks.send_transactional_email_batch",
})

SOURCE_QUEUE = "default"
CAMPAIGN_QUEUE = "campaign"
TRANSACTIONAL_QUEUE = "transactional"


def move_send_tasks_to_split_queues(apps, schema_editor):
    # App label is django_tasks_database (see django_tasks_db.apps).
    DBTaskResult = apps.get_model("django_tasks_database", "DBTaskResult")
    moved_campaign = (
        DBTaskResult.objects.filter(
            status="READY", queue_name=SOURCE_QUEUE, task_path__in=CAMPAIGN_TASK_PATHS,
        ).update(queue_name=CAMPAIGN_QUEUE)
    )
    moved_transactional = (
        DBTaskResult.objects.filter(
            status="READY", queue_name=SOURCE_QUEUE, task_path__in=TRANSACTIONAL_TASK_PATHS,
        ).update(queue_name=TRANSACTIONAL_QUEUE)
    )
    if moved_campaign or moved_transactional:
        print(
            f"send queue split: moved {moved_campaign} campaign + "
            f"{moved_transactional} transactional READY tasks off 'default'"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0048_newsletter_issues_blocks"),
    ]

    operations = [
        migrations.RunPython(move_send_tasks_to_split_queues, migrations.RunPython.noop),
    ]
