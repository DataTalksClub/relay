from django.db import migrations


def backfill(apps, schema_editor):
    Message = apps.get_model("mailing", "TransactionalMessage")
    Campaign = apps.get_model("mailing", "TransactionalCampaign")
    RecipientList = apps.get_model("mailing", "RecipientList")
    lists = {(row.client_id, row.key): row.id for row in RecipientList.objects.all().iterator()}
    campaigns = {}
    for message in Message.objects.filter(campaign__isnull=True).order_by("created_at", "id").iterator(chunk_size=1000):
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        list_id = lists.get((message.client_id, metadata.get("recipient_list_key")))
        source_key = metadata.get("source_object_key")
        if not list_id or not isinstance(source_key, str) or not source_key:
            continue
        suffix = ":" + source_key
        if not message.idempotency_key.endswith(suffix):
            continue
        key = message.idempotency_key[:-len(suffix)]
        if not key:
            continue
        identity = (message.client_id, key)
        if identity not in campaigns:
            campaign, created = Campaign.objects.get_or_create(
                client_id=message.client_id, idempotency_key=key,
                defaults={"recipient_list_id": list_id, "template_key": message.template_key},
            )
            if created:
                Campaign.objects.filter(pk=campaign.pk).update(created_at=message.created_at)
            campaigns[identity] = campaign.pk
        Message.objects.filter(pk=message.pk).update(campaign_id=campaigns[identity])


class Migration(migrations.Migration):
    dependencies = [("mailing", "0031_transactionalcampaign_transactionalmessage_campaign_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
