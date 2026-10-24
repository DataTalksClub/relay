"""Operator-authorized sending domains; verification is always provider-derived."""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models


class SendingDomain(models.Model):
    name = models.CharField(max_length=253, unique=True)
    organization = models.ForeignKey("mailing.Organization", on_delete=models.PROTECT)
    clients = models.ManyToManyField(
        "mailing.Client", blank=True, help_text="Empty means all clients in this organization."
    )
    role_arn = models.CharField(max_length=2048)
    region = models.CharField(max_length=40)
    configuration_set = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    verification = models.JSONField(default=dict, blank=True, editable=False)
    checked_at = models.DateTimeField(null=True, editable=False)

    def clean(self):
        self.name = self.name.strip().lower()
        try:
            validate_email("relay@" + self.name)
        except ValidationError:
            raise ValidationError({"name": "Enter a DNS domain."}) from None
        if not self.role_arn.startswith("arn:aws:iam::") or ":role/" not in self.role_arn:
            raise ValidationError({"role_arn": "Enter a provider role ARN."})

    def __str__(self):
        return self.name
