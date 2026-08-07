# Generated manually for Relay's first-class job and schedule resources.

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("mailing", "0025_client_relay_webhook_fields")]

    operations = [
        migrations.CreateModel(
            name="Schedule",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.SlugField(max_length=120)),
                ("cron", models.CharField(max_length=120)),
                ("task_type", models.CharField(max_length=64)),
                ("task", models.JSONField(default=dict)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3)),
                ("enabled", models.BooleanField(default=True)),
                ("next_run_at", models.DateTimeField(db_index=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("last_missed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="relay_schedules",
                        to="mailing.client",
                    ),
                ),
            ],
            options={"db_table": "relay_schedules", "ordering": ["client_id", "name"]},
        ),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("task_type", models.CharField(db_index=True, max_length=64)),
                ("idempotency_key", models.CharField(max_length=255)),
                ("request_hash", models.CharField(max_length=64)),
                ("correlation_id", models.UUIDField(db_index=True, default=uuid.uuid4)),
                ("task", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("retrying", "Retrying"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("attempt", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=3)),
                ("run_after", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("task_result_id", models.CharField(blank=True, max_length=255)),
                ("response_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("response_body", models.TextField(blank=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="relay_jobs",
                        to="mailing.client",
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="jobs",
                        to="jobs.schedule",
                    ),
                ),
            ],
            options={"db_table": "relay_jobs", "ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="schedule",
            name="last_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="last_for_schedules",
                to="jobs.job",
            ),
        ),
        migrations.AddConstraint(
            model_name="schedule",
            constraint=models.UniqueConstraint(
                fields=("client", "name"),
                name="relay_schedule_client_name_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="schedule",
            index=models.Index(fields=["enabled", "next_run_at"], name="relay_sched_due_idx"),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(
                fields=("client", "idempotency_key"),
                name="relay_job_client_idempotency_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(fields=["client", "status", "-created_at"], name="relay_job_cli_status_idx"),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(fields=["status", "run_after"], name="relay_job_due_idx"),
        ),
    ]
