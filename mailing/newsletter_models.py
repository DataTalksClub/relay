"""Newsletter issues assembled from typed content blocks.

An issue is an ordered list of blocks; each block has a type from
``mailing.services.newsletters.BLOCK_TYPES`` and its field values in ``data``.
Rendering, validation and test delivery live in that service module so the
operator UI and the client API share one implementation.
"""

from django.conf import settings
from django.db import models

from mailing.models import Campaign, Client, TimeStampedModel


class NewsletterIssue(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="newsletter_issues")
    key = models.SlugField(max_length=120)
    subject = models.CharField(max_length=255)
    preview_text = models.CharField(max_length=255, blank=True)
    issue_number = models.PositiveIntegerField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    updated_by = models.CharField(max_length=320, blank=True)
    # Which starter structure the issue was created from ("dtc-weekly", "blank"
    # or "" for issues created before templates existed / via plain create).
    template_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    # The draft campaign this issue was assembled into (see assemble_campaign).
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="newsletter_issues")

    class Meta:
        db_table = "newsletter_issues"
        ordering = ["-publication_date", "-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["client", "key"], name="unique_newsletter_issue_client_key"),
        ]

    def __str__(self):
        return f"{self.key} ({self.client.slug})"


class NewsletterBlock(TimeStampedModel):
    issue = models.ForeignKey(NewsletterIssue, on_delete=models.CASCADE, related_name="blocks")
    position = models.PositiveIntegerField(default=0)
    block_type = models.CharField(max_length=40)
    data = models.JSONField(default=dict, blank=True)
    updated_by = models.CharField(max_length=320, blank=True)
    # True for template placeholders and auto-filled blocks the editor still has
    # to look at; cleared by a successful (strict) update_block.
    needs_review = models.BooleanField(default=False, db_default=False)
    # Machine key of where the content came from ("website:events",
    # "substack:alexey", "computed", "placeholder", "" for manual).
    source = models.CharField(max_length=60, blank=True, default="", db_default="")
    source_note = models.CharField(max_length=255, blank=True, default="", db_default="")

    class Meta:
        db_table = "newsletter_blocks"
        ordering = ["issue_id", "position", "id"]
        indexes = [models.Index(fields=["issue", "position"], name="newsletter_block_issue_pos_idx")]

    def __str__(self):
        return f"{self.issue.key}#{self.position} {self.block_type}"


class NewsletterTestSend(models.Model):
    """Durable log of every test delivery attempt (one row per address)."""

    issue = models.ForeignKey(NewsletterIssue, on_delete=models.CASCADE, related_name="test_sends")
    block = models.ForeignKey(NewsletterBlock, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    via = models.CharField(max_length=10)
    email = models.EmailField(max_length=320)
    status = models.CharField(max_length=20)
    message_id = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "newsletter_test_sends"
        ordering = ["-created_at", "-id"]
