from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0040_template_markdown_layout_context"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(fields=["client", "status", "updated_at"], name="campaign_cli_status_upd_idx"),
        ),
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["campaign", "sent_at"], name="camp_recip_sent_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["audience", "client", "created_at"], name="email_evt_aud_cli_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailevent",
            index=models.Index(fields=["audience", "event_type", "created_at"], name="email_evt_aud_type_created_idx"),
        ),
    ]
