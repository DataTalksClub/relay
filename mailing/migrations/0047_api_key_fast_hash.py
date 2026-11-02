# Fast API-key verifier for intake e5cd1579: HMAC-SHA256 `key_fast_hash`
# replaces per-request PBKDF2 (~240 ms) on the authenticated API hot path.
# PBKDF2 `key_hash` stays as the legacy fallback; empty fast hash means
# "not upgraded yet" and is populated on the next successful check.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailing', '0046_transactionalrequestminute_missing_template'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientapikey',
            name='key_fast_hash',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='adminapikey',
            name='key_fast_hash',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
    ]
