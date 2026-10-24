"""Domain routing configuration and durable per-mailbox delivery outbox."""
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models, transaction
from django.utils import timezone


class InboundDomain(models.Model):
    name = models.CharField(max_length=255, unique=True)
    client = models.ForeignKey('Client', on_delete=models.PROTECT)
    from_email = models.EmailField(max_length=320, help_text='Verified sender on this domain.')
    enabled = models.BooleanField(default=True)

    def clean(self):
        self.name = self.name.strip().lower()
        validate_email('mailbox@' + self.name)
        if self.from_email.rpartition('@')[2].lower() != self.name:
            raise ValidationError({'from_email': 'Must be on this domain and verified with the outbound provider.'})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            InboundMailbox.objects.get_or_create(domain=self, local_part='*', defaults={'kind': 'discard'})

    def __str__(self):
        return self.name


class InboundMailbox(models.Model):
    class Kind(models.TextChoices):
        BOT = 'bot', 'Bot webhook'
        FORWARD = 'forward', 'Forward'
        DISCARD = 'discard', 'Discard (default only)'

    domain = models.ForeignKey(InboundDomain, on_delete=models.PROTECT, related_name='mailboxes')
    local_part = models.CharField(max_length=64, help_text='Use * for the single domain default.')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    enabled = models.BooleanField(default=True)
    destinations = models.JSONField(default=list, blank=True)
    sender_allowlist = models.JSONField(default=list, blank=True, help_text='Exact sender addresses; empty denies all bots.')
    webhook_secret_id = models.CharField(max_length=512, blank=True, help_text='Secrets Manager secret ID, or env:VARIABLE. JSON: url, header, value. Never enter secret values here.')
    rate_per_minute = models.PositiveIntegerField(default=30)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['domain', 'local_part'], name='unique_routing_mailbox')]

    def clean(self):
        self.local_part = self.local_part.strip().lower()
        if self.local_part != '*' and not re.fullmatch(r'[a-z0-9.!#$%&\x27+/=?^_`{|}~-]+', self.local_part):
            raise ValidationError({'local_part': 'Invalid mailbox local part.'})
        for field in ('destinations', 'sender_allowlist'):
            values = getattr(self, field)
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValidationError({field: 'Must be a list of email addresses.'})
            for value in values:
                validate_email(value)
            setattr(self, field, list(dict.fromkeys(v.strip().lower() for v in values)))
        if self.kind == self.Kind.BOT and not self.webhook_secret_id:
            raise ValidationError({'webhook_secret_id': 'A secret reference is required.'})
        if self.kind == self.Kind.FORWARD and not self.destinations:
            raise ValidationError({'destinations': 'At least one destination is required.'})
        if self.kind == self.Kind.DISCARD and self.local_part != '*':
            raise ValidationError({'kind': 'Discard is a domain default rule.'})
        if not 1 <= self.rate_per_minute <= 1000:
            raise ValidationError({'rate_per_minute': 'Use 1 to 1000.'})
        if '://' in self.webhook_secret_id:
            raise ValidationError({'webhook_secret_id': 'Enter a secret reference, never a URL.'})

    def __str__(self):
        return f'{self.local_part}@{self.domain.name}'


class InboundDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        SUPPRESSED = 'suppressed', 'Suppressed'
        FAILED = 'failed', 'Failed'

    message = models.ForeignKey('InboundMessage', on_delete=models.PROTECT, related_name='routing_deliveries')
    mailbox = models.ForeignKey(InboundMailbox, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=InboundMailbox.Kind.choices)
    recipient = models.EmailField(max_length=320)
    destination = models.CharField(max_length=320, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    reason = models.CharField(max_length=80, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    outbound_message = models.OneToOneField('TransactionalMessage', on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['message', 'mailbox', 'destination'], name='unique_inbound_delivery')]
