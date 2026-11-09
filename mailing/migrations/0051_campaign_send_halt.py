# Generated for relay-fix-hard-stop: lock-free pause/resume/stop flag on Campaign.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailing', '0050_dispatch_outbox_heartbeat_alerts'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='send_halted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='campaign',
            name='send_halt_reason',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
    ]
