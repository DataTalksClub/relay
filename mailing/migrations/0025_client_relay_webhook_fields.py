from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mailing", "0024_remove_capturedemail_capt_email_client_created_idx_and_more")]

    operations = [
        migrations.AddField(
            model_name="client",
            name="relay_webhook_allowed_origins",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="client",
            name="relay_webhook_signing_secret",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
