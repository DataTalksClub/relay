"""Published template versions: publish, resolve, list, and API preview.

Each client template keeps one editable draft (:class:`EmailTemplate`).
Publishing snapshots the draft into an immutable :class:`TemplateVersion`
row (1-based per template). Sends render only from published versions; the
draft is never rendered by a send.
"""

from django.db import transaction

from mailing.models import EmailTemplate, TemplateVersion
from mailing.services.api_errors import ApiValidationError, isoformat
from mailing.services.transactional_content import TemplateContent, validate_required_context_names


def get_template_draft_for_client(template_key, authenticated_client):
    template = EmailTemplate.objects.filter(
        client=authenticated_client,
        key=template_key,
        is_transactional=True,
    ).first()
    if template is None:
        raise ApiValidationError({"template_key": "not_found"}, status_code=404)
    return template


def latest_template_version(template):
    return template.versions.order_by("-version").first()


def latest_published_version_number(template):
    version = latest_template_version(template)
    return version.version if version is not None else None


def resolve_template_version(template, template_version=None):
    """Resolve the version a send or preview renders from.

    ``None`` means the latest published version. A template with no published
    version is a 409 (the draft exists but nothing was published); an explicit
    unknown version number is a 404. Neither creates any side effect.
    """
    if template_version is None:
        latest = latest_template_version(template)
        if latest is None:
            raise ApiValidationError({"template_key": "no_published_version"}, status_code=409)
        return latest
    version = template.versions.filter(version=template_version).first()
    if version is None:
        raise ApiValidationError({"template_version": "not_found"}, status_code=404)
    return version


@transaction.atomic
def publish_template_draft(template):
    """Snapshot the draft as a new immutable version.

    Republishing content identical to the latest published version returns
    that version without creating a new row.
    """
    content = TemplateContent.from_draft(template)
    latest = latest_template_version(template)
    if latest is not None and TemplateContent.from_version(latest).as_dict() == content.as_dict():
        return latest, False

    version = TemplateVersion.objects.create(
        template=template,
        version=latest.version + 1 if latest is not None else 1,
        **content.as_dict(),
    )
    return version, True


def version_payload(version, *, latest_version=None):
    if latest_version is None:
        latest_version = latest_published_version_number(version.template)
    return {
        "version": version.version,
        "subject": version.subject,
        "content_format": version.content_format,
        "category": version.category,
        "required_context": version.required_context,
        "published_at": isoformat(version.published_at),
        "is_latest": version.version == latest_version,
    }


def list_template_versions_for_client(template_key, authenticated_client):
    template = get_template_draft_for_client(template_key, authenticated_client)
    latest_version = latest_published_version_number(template)
    versions = [version_payload(version, latest_version=latest_version) for version in template.versions.all()]
    return {
        "template_key": template.key,
        "latest_published_version": latest_version,
        "versions": versions,
    }


def publish_transactional_template_for_client(template_key, authenticated_client):
    template = get_template_draft_for_client(template_key, authenticated_client)
    version, created = publish_template_draft(template)
    return {
        "template_key": template.key,
        "created": created,
        "latest_published_version": version.version,
        "version": version_payload(version, latest_version=version.version),
    }


def validate_template_preview_payload(data):
    context = data.get("context", {})
    if context in (None, ""):
        context = {}
    elif not isinstance(context, dict):
        raise ApiValidationError({"context": "must_be_object"})

    template_version = data.get("template_version")
    if template_version in (None, ""):
        template_version = None
    elif isinstance(template_version, bool) or not isinstance(template_version, int):
        raise ApiValidationError({"template_version": "must_be_integer"})

    return {"context": context, "template_version": template_version}


def preview_transactional_template_for_client(template_key, data, authenticated_client):
    """Render the draft (no version given) or one published version.

    Shares :meth:`TemplateContent.render` with the send pipeline, so the same
    version plus the same context render identically through preview and
    ``dry_run`` send.
    """
    payload = validate_template_preview_payload(data)
    template = get_template_draft_for_client(template_key, authenticated_client)
    if payload["template_version"] is None:
        content = TemplateContent.from_draft(template)
        version_number = None
    else:
        version = resolve_template_version(template, payload["template_version"])
        content = TemplateContent.from_version(version)
        version_number = version.version

    validate_required_context_names(
        (requirement.name for requirement in content.required_context),
        payload["context"],
    )
    rendered = content.render(payload["context"], client_name=template.client.name)
    return {
        "template_key": template.key,
        "template_version": version_number,
        "preview": rendered,
    }
