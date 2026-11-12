from django import forms
from django.contrib import admin
from django.db import models

from mailing.models import (
    Audience,
    BlockedSender,
    CallbackEndpoint,
    Campaign,
    CampaignRecipient,
    Client,
    ClientApiKey,
    ClientCallback,
    CmpCallback,
    Contact,
    ContactTag,
    EmailEvent,
    EmailLayout,
    EmailTemplate,
    InboundAddress,
    InboundMessage,
    MailchimpSync,
    MailchimpTagMapping,
    Organization,
    SendingDomain,
    Subscription,
    Tag,
    TransactionalMessage,
)
from mailing.services.api import validate_sender_policy_payload
from mailing.services.api_errors import ApiValidationError
from mailing.services.sending_domains import refresh, validate_sender_domains


class CreatedAtReadOnlyMixin:
    readonly_fields = ("created_at",)


@admin.register(Organization)
class OrganizationAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Audience)
class AudienceAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "organization", "created_at")
    list_filter = ("organization",)
    search_fields = ("name", "slug", "organization__name", "organization__slug")
    prepopulated_fields = {"slug": ("name",)}


class ClientSenderAdminForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        org = cleaned.get('organization')
        if org and cleaned.get('sender_emails'):
            try:
                policy = validate_sender_policy_payload({'senders': cleaned.get('sender_emails'), 'default_sender_id': cleaned.get('default_sender_id')})
                validate_sender_domains(Client(pk=self.instance.pk, organization=org),
                    policy['sender_emails'], previous=(self.instance.sender_emails or []) if org.pk == self.instance.organization_id else [])
            except ApiValidationError as exc:
                self.add_error('sender_emails', str(exc.errors))
        return cleaned


@admin.register(Client)
class ClientAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    form = ClientSenderAdminForm
    readonly_fields = ("created_at", "updated_at")
    list_display = ("name", "slug", "organization", "default_sender_id", "is_active", "created_at")
    list_filter = ("organization", "is_active")
    search_fields = ("name", "slug", "organization__name", "organization__slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ClientApiKey)
class ClientApiKeyAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at", "last_used_at", "revoked_at")
    list_display = ("name", "public_id", "client", "revoked_at", "last_used_at", "created_at")
    list_filter = ("revoked_at", "client__organization")
    search_fields = ("name", "public_id", "client__name", "client__slug")


class SubscriptionInline(admin.TabularInline):
    model = Subscription
    extra = 0
    fields = ("audience", "client", "status", "verified_at", "unsubscribed_at", "unsubscribe_reason")
    autocomplete_fields = ("audience", "client")


class ContactTagInline(admin.TabularInline):
    model = ContactTag
    extra = 0
    fields = ("tag", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("tag",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = (
        "email",
        "normalized_email",
        "verified_at",
        "email_validation_status",
        "email_validated_at",
        "global_unsubscribed_at",
        "hard_bounced_at",
        "complained_at",
        "updated_at",
    )
    list_filter = (
        "verified_at",
        "email_validation_status",
        "email_validated_at",
        "global_unsubscribed_at",
        "hard_bounced_at",
        "complained_at",
    )
    search_fields = ("email", "normalized_email", "subscriptions__audience__slug", "subscriptions__client__slug")
    inlines = (SubscriptionInline, ContactTagInline)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("contact", "audience", "client", "status", "verified_at", "unsubscribed_at", "updated_at")
    list_filter = ("status", "audience", "client")
    search_fields = (
        "contact__email",
        "contact__normalized_email",
        "audience__name",
        "audience__slug",
        "client__name",
        "client__slug",
    )
    autocomplete_fields = ("contact", "audience", "client")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "audience")
    list_filter = ("audience",)
    search_fields = ("name", "slug", "audience__name", "audience__slug")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("audience",)


@admin.register(ContactTag)
class ContactTagAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("contact", "tag", "created_at")
    list_filter = ("tag__audience", "tag")
    search_fields = ("contact__email", "contact__normalized_email", "tag__name", "tag__slug")
    autocomplete_fields = ("contact", "tag")


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    extra = 0
    fields = ("contact", "email", "status", "skip_reason", "sent_at", "last_error")
    readonly_fields = ("email", "status", "skip_reason", "sent_at", "last_error")
    autocomplete_fields = ("contact",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    readonly_fields = (
        "created_at",
        "updated_at",
        "recipient_count",
        "sent_count",
        "skipped_count",
        "delivered_count",
        "unique_open_count",
        "open_count",
        "unique_click_count",
        "click_count",
        "unsubscribe_count",
        "bounce_count",
        "complaint_count",
    )
    list_display = (
        "subject",
        "client",
        "audience",
        "category_tag",
        "status",
        "scheduled_at",
        "recipient_count",
        "skipped_count",
        "sent_count",
        "created_at",
    )
    list_filter = ("status", "client", "audience", "category_tag")
    search_fields = ("subject", "category_tag", "client__name", "client__slug", "audience__name", "audience__slug")
    autocomplete_fields = ("client", "audience")
    inlines = (CampaignRecipientInline,)


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("campaign", "email", "contact", "status", "skip_reason", "sent_at", "last_error")
    list_filter = ("status", "skip_reason", "campaign__client", "campaign__audience")
    search_fields = (
        "email",
        "contact__email",
        "contact__normalized_email",
        "campaign__subject",
        "campaign__client__name",
        "campaign__client__slug",
        "campaign__audience__name",
        "campaign__audience__slug",
        "ses_message_id",
    )
    autocomplete_fields = ("campaign", "contact")


@admin.register(EmailLayout)
class EmailLayoutAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("client", "sender_id", "brand_name", "website_url", "updated_at")
    list_filter = ("client",)
    search_fields = ("brand_name", "client__name", "client__slug", "sender_id")
    autocomplete_fields = ("client",)


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = (
        "key",
        "name",
        "client",
        "default_sender_id",
        "is_transactional",
        "is_active",
        "updated_at",
    )
    list_filter = ("client", "is_transactional", "is_active")
    search_fields = ("key", "name", "description", "subject", "client__name", "client__slug")
    autocomplete_fields = ("client",)


class EmailEventInline(admin.TabularInline):
    model = EmailEvent
    extra = 0
    fields = ("event_type", "created_at", "metadata")
    readonly_fields = ("event_type", "created_at", "metadata")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TransactionalMessage)
class TransactionalMessageAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = (
        "email",
        "from_email_id",
        "from_email",
        "template_key",
        "client",
        "status",
        "idempotency_key",
        "created_at",
    )
    list_filter = ("status", "client", "template")
    search_fields = (
        "email",
        "from_email_id",
        "from_email",
        "contact__email",
        "contact__normalized_email",
        "template_key",
        "idempotency_key",
        "ses_message_id",
    )
    autocomplete_fields = ("client", "contact", "template")
    inlines = (EmailEventInline,)


@admin.register(EmailEvent)
class EmailEventAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at",)
    list_display = ("event_type", "client", "contact", "transactional_message", "campaign", "created_at")
    list_filter = ("event_type", "client")
    search_fields = (
        "contact__email",
        "contact__normalized_email",
        "client__name",
        "client__slug",
        "transactional_message__email",
        "transactional_message__template_key",
        "campaign__subject",
    )
    autocomplete_fields = ("campaign", "campaign_recipient", "transactional_message", "contact", "client", "audience")


class CallbackEndpointForm(forms.ModelForm):
    """Keeps the signing secret write-only: blank keeps the current secret."""

    class Meta:
        model = CallbackEndpoint
        fields = "__all__"
        widgets = {"signing_secret": forms.PasswordInput(render_value=False)}

    def clean_signing_secret(self):
        new_secret = self.cleaned_data.get("signing_secret", "")
        if not new_secret and self.instance and self.instance.pk:
            return self.instance.signing_secret
        return new_secret


@admin.register(CallbackEndpoint)
class CallbackEndpointAdmin(admin.ModelAdmin):
    form = CallbackEndpointForm
    readonly_fields = ("created_at", "updated_at", "secret_rotated_at", "disabled_at")
    list_display = ("client", "enabled", "url", "contract_version", "secret_rotated_at", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("client__name", "client__slug", "url")
    autocomplete_fields = ("client",)


@admin.register(CmpCallback)
class CmpCallbackAdmin(admin.ModelAdmin):
    readonly_fields = (
        "created_at",
        "updated_at",
        "last_attempt_at",
        "delivered_at",
    )
    list_display = (
        "event_type",
        "status",
        "client",
        "contact",
        "attempt_count",
        "next_attempt_at",
        "delivered_at",
        "created_at",
    )
    list_filter = ("status", "event_type", "client")
    search_fields = (
        "event_id",
        "event_type",
        "contact__email",
        "contact__normalized_email",
        "client__name",
        "client__slug",
        "last_error",
    )
    autocomplete_fields = ("email_event", "contact", "client", "audience")


@admin.register(ClientCallback)
class ClientCallbackAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    readonly_fields = (
        "updated_at",
        "last_attempt_at",
        "delivered_at",
        "body_hash",
    )
    list_display = (
        "event_type",
        "status",
        "client",
        "attempt_count",
        "next_attempt_at",
        "delivered_at",
        "created_at",
    )
    list_filter = ("status", "event_type", "client")
    search_fields = (
        "event_id",
        "event_type",
        "client_reference",
        "client__name",
        "client__slug",
        "last_error",
    )
    autocomplete_fields = ("email_event", "transactional_message", "campaign_recipient", "client", "endpoint")


@admin.register(MailchimpTagMapping)
class MailchimpTagMappingAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("client", "audience", "list_key", "tag", "enabled", "updated_at")
    list_filter = ("enabled", "client", "audience")
    search_fields = ("list_key", "tag", "client__slug", "audience__slug")


@admin.register(MailchimpSync)
class MailchimpSyncAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at", "last_attempt_at", "delivered_at")
    list_display = (
        "email",
        "list_key",
        "tag",
        "status",
        "client",
        "attempt_count",
        "next_attempt_at",
        "delivered_at",
        "created_at",
    )
    list_filter = ("status", "client")
    search_fields = (
        "email",
        "list_key",
        "tag",
        "dedup_key",
        "client__name",
        "client__slug",
        "last_error",
    )
    autocomplete_fields = ("contact", "client", "audience")


@admin.register(InboundAddress)
class InboundAddressAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("local_part", "domain", "is_active", "note", "created_at")
    list_filter = ("is_active", "domain")
    search_fields = ("local_part", "domain", "note")


@admin.register(InboundMessage)
class InboundMessageAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("sender_address", "subject", "recipient_address", "state", "created_at")
    list_filter = ("state", "recipient_domain", "spam_verdict")
    search_fields = ("subject", "snippet", "sender_address", "from_header", "recipient_address", "message_id")
    readonly_fields = tuple(
        field.name for field in InboundMessage._meta.fields if field.name not in {"id"}
    )
    autocomplete_fields = ("inbound_address",)


@admin.register(BlockedSender)
class BlockedSenderAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("value", "scope", "origin", "created_at")
    list_filter = ("scope", "origin")
    search_fields = ("value", "reason")
    autocomplete_fields = ("origin_message",)


from mailing.models import InboundDelivery, InboundDomain, InboundMailbox  # noqa: E402
from mailing.services.operator_management import audit as routing_audit  # noqa: E402


class RoutingAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        routing_audit(request.user, "inbound_routing.update" if change else "inbound_routing.create", obj,
                      {"fields": sorted(form.changed_data)})

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InboundDomain)
class InboundDomainAdmin(RoutingAdmin):
    list_display = ("name", "client", "from_email", "enabled")


@admin.register(InboundMailbox)
class InboundMailboxAdmin(RoutingAdmin):
    list_display = ("local_part", "domain", "kind", "enabled")
    list_filter = ("domain", "kind", "enabled")


@admin.register(InboundDelivery)
class InboundDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "message", "mailbox", "status", "reason", "attempt_count", "response_status", "outbound_message")
    readonly_fields = tuple(field.name for field in InboundDelivery._meta.fields)
    list_filter = ("status", "mailbox")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SendingDomainAdminForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get('organization')
        clients = cleaned.get('clients')
        if organization and clients is not None and clients.exclude(organization=organization).exists():
            self.add_error('clients', 'All clients must belong to the selected organization.')
        return cleaned


@admin.register(SendingDomain)
class SendingDomainAdmin(admin.ModelAdmin):
    form = SendingDomainAdminForm
    list_display = ('name', 'organization', 'region', 'enabled', 'checked_at')
    readonly_fields = ('verification', 'checked_at')
    filter_horizontal = ('clients',)
    actions = ('verify_domains',)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        refresh(obj, register=True)

    @admin.action(description='Refresh provider verification')
    def verify_domains(self, request, queryset):
        for row in queryset:
            refresh(row)


from mailing.models import DmarcArchive, DmarcAttachment, DmarcRecord, DmarcReport  # noqa: E402


class DmarcReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if not isinstance(field, models.BinaryField))

    def get_exclude(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields if isinstance(field, models.BinaryField))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DmarcArchive)
class DmarcArchiveAdmin(DmarcReadOnlyAdmin):
    list_display = ('id', 'domain', 'reporter_domain', 'message_id', 'created_at', 'retain_until', 'parse_error')
    list_filter = ('domain', 'reporter_domain')
    search_fields = ('message__message_id', 'reporter_domain')


@admin.register(DmarcAttachment)
class DmarcAttachmentAdmin(DmarcReadOnlyAdmin):
    list_display = ('id', 'archive', 'filename', 'parse_error')
    list_select_related = ('archive',)


@admin.register(DmarcReport)
class DmarcReportAdmin(DmarcReadOnlyAdmin):
    list_display = ('id', 'reporter_org', 'report_id', 'date_begin', 'date_end', 'attachment')
    search_fields = ('reporter_org', 'report_id')


@admin.register(DmarcRecord)
class DmarcRecordAdmin(DmarcReadOnlyAdmin):
    list_display = ('id', 'report', 'source_ip', 'count', 'disposition', 'dkim', 'spf')
    list_filter = ('disposition', 'dkim', 'spf')
    search_fields = ('source_ip', 'report__report_id')
