"""Short-lived state for the shared-auth CLI device flow."""

from django.conf import settings
from django.db import models


class DeviceLogin(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("denied", "Denied"),
        ("consumed", "Consumed"),
    ]

    device_code_hash = models.CharField(max_length=64, unique=True)
    user_code_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=160, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    admin_api_key = models.OneToOneField(
        "mailing.AdminApiKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="device_login"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "device_logins"
        indexes = [models.Index(fields=["status", "expires_at"], name="device_login_status_exp_idx")]
