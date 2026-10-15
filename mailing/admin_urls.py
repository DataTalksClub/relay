from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from mailing import admin_api
from mailing import admin_operations as operations
from mailing.urls import urlpatterns as client_patterns

app_name = "admin_api"


def resource_dispatch(resource, write, id_arg):
    @csrf_exempt
    def dispatch(request, record_id=None):
        if request.method == "GET":
            return operations.read_resource(request, resource=resource, record_id=record_id)
        return write(request, **({id_arg: record_id} if record_id is not None else {}))

    return dispatch


urlpatterns = [
    path("jobs/<uuid:task_id>/retry", operations.job_retry, name="job_retry"),
    path("schedules/<uuid:schedule_id>/action", operations.schedule_action, name="schedule_action"),
    path("", admin_api.endpoint_index, name="index"),
    path("api-keys", admin_api.admin_keys, name="keys"),
    path("api-keys/<int:key_id>/revoke", admin_api.revoke_key, name="key_revoke"),
    path("clients/<int:client_id>/api-keys", admin_api.client_keys, name="client_keys"),
    path("clients/<int:client_id>/api-keys/<int:key_id>/revoke", admin_api.revoke_key, name="client_key_revoke"),
    path("clients/<int:client_id>/settings", operations.client_settings, name="client_settings"),
    path("clients/<int:client_id>/campaign-drafts", operations.campaign_write, name="campaign_create"),
    path("clients/<int:client_id>/campaign-drafts/<int:campaign_id>", operations.campaign_write, name="campaign_edit"),
    path("campaigns/<int:campaign_id>/queue", operations.campaign_queue, name="campaign_queue"),
    path(
        "campaigns/<int:campaign_id>/recipients/<int:recipient_id>/assume-sent",
        operations.assume_sent,
        name="assume_sent",
    ),
    path("contacts/<int:contact_id>/state", operations.contact_action, {"action": "state"}, name="contact_state"),
    path("workers", operations.status, {"kind": "workers"}, name="workers"),
    path("dashboard", operations.status, {"kind": "dashboard"}, name="dashboard"),
    path("dead-letters/<uuid:task_id>/retry", operations.dead_letter_retry, name="dead_letter_retry"),
]
for resource in admin_api.RESOURCE_MODELS:
    urlpatterns += [
        path(resource, admin_api.resources, {"resource": resource}, name=resource),
        path(f"{resource}/<int:record_id>", admin_api.resources, {"resource": resource}, name=f"{resource}_detail"),
    ]

for resource in ("contacts", "audiences", "campaigns"):
    urlpatterns.append(
        path(
            f"{resource}/<int:record_id>/overview",
            operations.overview,
            {"resource": resource},
            name=resource + "_overview",
        )
    )

# Each client operation also has an admin counterpart with explicit scope and
# the same contract. This reuses services without accepting admin keys on /api/.
for pattern in client_patterns:
    route = str(pattern.pattern)
    if route.startswith("api/") and route != "api/workers/status":
        urlpatterns.append(
            path(
                "clients/<int:client_id>/" + route.removeprefix("api/"),
                admin_api.scoped_client_endpoint(pattern.callback),
                name="scoped_" + pattern.name,
            )
        )
for action in ("subscriptions", "tags/add", "tags/remove"):
    urlpatterns.append(
        path(
            "clients/<int:client_id>/contacts/<int:contact_id>/" + action,
            operations.contact_action,
            {"action": action},
            name="contact_" + action.replace("/", "_"),
        )
    )
for action in ("mark-read", "block", "unblock"):
    urlpatterns.append(
        path(
            "inbound-messages/<int:message_id>/" + action,
            operations.inbound_action,
            {"action": action},
            name="inbound_" + action,
        )
    )

WRITE_RESOURCES = {
    "tags": (operations.tag_write, "tag_id"),
    "inbound-addresses": (operations.inbound_address_write, "address_id"),
    "blocked-senders": (operations.blocked_delete, "rule_id"),
}
for resource in operations.READ_FIELDS:
    converter = "uuid" if resource in {"dead-letters", "jobs", "schedules"} else "int"
    if resource in WRITE_RESOURCES:
        write, id_arg = WRITE_RESOURCES[resource]
        # Blocking starts with an inbound message; only existing rules can be deleted.
        collection_view = (
            operations.read_resource if resource == "blocked-senders" else resource_dispatch(resource, write, id_arg)
        )
        defaults = {"resource": resource} if resource == "blocked-senders" else {}
        urlpatterns += [
            path(resource, collection_view, defaults, name="read_" + resource),
            path(
                f"{resource}/<{converter}:record_id>",
                resource_dispatch(resource, write, id_arg),
                name="read_" + resource + "_detail",
            ),
        ]
    else:
        urlpatterns += [
            path(resource, operations.read_resource, {"resource": resource}, name="read_" + resource),
            path(
                f"{resource}/<{converter}:record_id>",
                operations.read_resource,
                {"resource": resource},
                name="read_" + resource + "_detail",
            ),
        ]

# Capability mapping, enforced by tests when staff UI endpoints are added.
# Client selection is explicit in API paths instead of stored in a session.
OPERATOR_API_PARITY = {
    "api_docs_workflow": "index", "api_docs_reference": "index", "api_docs_legacy": "index",
    "campaign_test_send": "scoped_api_campaign_test_send", "campaign_cancel": "scoped_api_campaign_cancel",
    "job_list": "read_jobs", "job_detail": "read_jobs_detail", "job_retry": "job_retry",
    "schedule_list": "read_schedules", "schedule_detail_ui": "read_schedules_detail", "schedule_action": "schedule_action",
    "service_health": "workers",
    "dashboard": "dashboard",
    "api_docs": "index",
    "api_docs_json": "index",
    "api_worker_status": "workers",
    "template_catalog": "read_templates",
    "template_detail": "read_templates_detail",
    "transactional_queue": "read_transactional-messages",
    "email_activity": "read_transactional-messages",
    "transactional_message_detail": "read_transactional-messages_detail",
    "inbound_list": "read_inbound-messages",
    "inbound_message_detail": "read_inbound-messages_detail",
    "inbound_address_list": "read_inbound-addresses",
    "inbound_address_create": "read_inbound-addresses",
    "inbound_address_archive": "read_inbound-addresses_detail",
    "blocked_sender_delete": "read_blocked-senders_detail",
    "campaign_list": "read_campaigns",
    "campaign_create": "campaign_create",
    "campaign_edit": "campaign_edit",
    "campaign_detail": "read_campaigns_detail",
    "campaign_queue": "campaign_queue",
    "campaign_recipient_assume_sent": "assume_sent",
    "contact_search": "read_contacts",
    "contact_detail": "read_contacts_detail",
    "contact_state_update": "contact_state",
    "contact_subscription_update": "contact_subscriptions",
    "contact_tag_add": "contact_tags_add",
    "contact_tag_remove": "contact_tags_remove",
    "audience_list": "audiences",
    "audience_create": "audiences",
    "audience_edit": "audiences_detail",
    "audience_detail": "audiences_detail",
    "tag_create": "read_tags",
    "tag_detail": "read_tags_detail",
    "tag_edit": "read_tags_detail",
    "client_list": "clients",
    "client_select": "clients",
    "client_create": "clients",
    "client_detail": "clients_detail",
    "client_mailchimp_tag_mappings": "scoped_api_client_mailchimp_tag_mappings",
    "client_edit": "client_settings",
    "client_api_key_create": "client_keys",
    "client_api_key_revoke": "client_key_revoke",
    "admin_api_keys": "keys",
    "admin_api_key_revoke": "key_revoke",
    "dead_letter_list": "read_dead-letters",
    "dead_letter_retry": "dead_letter_retry",
}
