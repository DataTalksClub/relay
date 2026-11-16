"""Lead capture pages for talks and workshops (materials.dtcdev.click).

An event is a row, not code: slug, title, copy, consent text and the offered
material are edited in Django admin or with ``manage.py lead_event`` and take
effect without a deploy. Signups are ordinary Relay contacts in the leads
audience; per-event state lives in ``ContactSourceMetadata`` (source
``lead:<slug>``) and confirmed signups carry the event tag.
"""

import hashlib

from django.core.validators import RegexValidator
from django.db import models

EVENT_SLUG_VALIDATOR = RegexValidator(
    r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$",
    "Use 1-60 lowercase letters, digits and hyphens.",
)

DEFAULT_CONSENT_TEXT = "I agree that Alexey Grigorev may contact me and send me materials."


class LeadMaterial(models.Model):
    """A downloadable file stored in Relay's own database (never a GitHub link)."""

    slug = models.SlugField(max_length=80, unique=True)
    title = models.CharField(max_length=200)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, default="application/pdf")
    data = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lead_materials"
        ordering = ["slug"]

    def save(self, *args, **kwargs):
        raw = bytes(self.data or b"")
        self.size = len(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class LeadEvent(models.Model):
    slug = models.CharField(max_length=60, unique=True, validators=[EVENT_SLUG_VALIDATOR])
    title = models.CharField(max_length=200)
    intro = models.TextField(
        blank=True,
        help_text="Short text under the title, e.g. what people get. Plain text; blank lines start paragraphs.",
    )
    offer = models.CharField(
        max_length=200,
        blank=True,
        help_text="One line naming what they receive now, e.g. the slides and the 28-prompts kit.",
    )
    material = models.ForeignKey(
        LeadMaterial,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
        help_text="File delivered after the address is confirmed.",
    )
    material_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="Alternative to a stored file: link delivered after confirmation.",
    )
    consent_text = models.CharField(max_length=300, default=DEFAULT_CONSENT_TEXT)
    is_active = models.BooleanField(default=True, help_text="Inactive pages show 'closed' and accept no signups.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lead_events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.slug})"

    @property
    def source(self):
        return f"lead:{self.slug}"

    @property
    def has_material(self):
        return bool(self.material_id or self.material_url)
