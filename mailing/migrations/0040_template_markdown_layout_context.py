from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0039_transactional_monitor_server_errors"),
    ]

    operations = [
        migrations.AddField(
            model_name="emailtemplate",
            name="markdown_layout_context",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="emailtemplateversion",
            name="markdown_layout_context",
            field=models.BooleanField(default=False),
        ),
    ]
