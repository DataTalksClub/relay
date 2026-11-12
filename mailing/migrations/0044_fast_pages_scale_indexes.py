from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0043_retry_candidate_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["campaign", "status", "contact"], name="camp_recip_camp_st_cnt_idx"),
        ),
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["campaign", "skip_reason", "contact"], name="camp_recip_camp_skip_cnt_idx"),
        ),
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["contact", "created_at"], name="camp_recip_contact_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["contact", "client", "created_at"], name="email_evt_cnt_cli_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["campaign", "created_at"], name="email_evt_camp_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["client", "event_type", "created_at"], name="email_evt_cli_type_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["event_type", "created_at"], name="email_evt_type_created_idx"),
        ),
    ]
