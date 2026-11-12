"""Durable DMARC evidence, independent of the expiring ingress S3 objects."""
from django.db import models


class DmarcArchive(models.Model):
    message = models.ForeignKey('mailing.InboundMessage', on_delete=models.PROTECT, related_name='dmarc_archives')
    domain = models.ForeignKey('mailing.InboundDomain', on_delete=models.PROTECT)
    reporter_domain = models.CharField(max_length=255, db_index=True)
    raw_eml = models.BinaryField()
    raw_key = models.CharField(max_length=1024)
    created_at = models.DateTimeField(auto_now_add=True)
    retain_until = models.DateTimeField()
    parse_error = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['message', 'domain'], name='unique_dmarc_message_domain')]


class DmarcAttachment(models.Model):
    archive = models.ForeignKey(DmarcArchive, on_delete=models.PROTECT, related_name='attachments')
    filename = models.CharField(max_length=1024)
    storage_key = models.CharField(max_length=2048)
    payload = models.BinaryField()
    parse_error = models.CharField(max_length=64, blank=True)


class DmarcReport(models.Model):
    attachment = models.ForeignKey(DmarcAttachment, on_delete=models.PROTECT, related_name='reports')
    member_name = models.CharField(max_length=1024, blank=True)
    reporter_org = models.CharField(max_length=255, blank=True)
    report_id = models.CharField(max_length=512, db_index=True)
    date_begin = models.DateTimeField()
    date_end = models.DateTimeField()
    policy = models.JSONField(default=dict)


class DmarcRecord(models.Model):
    report = models.ForeignKey(DmarcReport, on_delete=models.PROTECT, related_name='records')
    source_ip = models.GenericIPAddressField()
    count = models.PositiveBigIntegerField()
    disposition = models.CharField(max_length=64, blank=True)
    dkim = models.CharField(max_length=64, blank=True)
    spf = models.CharField(max_length=64, blank=True)
    identifiers = models.JSONField(default=dict)
    auth_results = models.JSONField(default=dict)
