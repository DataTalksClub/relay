"""Short setup guide and searchable reference built from existing API sources."""

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.text import slugify

from mailing.services.api_docs import (
    DEMO_API_KEYS,
    build_openapi_spec,
    docs_base_url,
    endpoint_groups,
    workflow_examples,
)


def documentation_context():
    groups = workflow_examples(docs_base_url())
    links = []
    legacy_anchors = {
        "auth": reverse("mailing:api_docs") + "#quickstart",
        "common-errors": reverse("mailing:api_docs_legacy") + "#common-errors",
        "endpoint-reference": reverse("mailing:api_docs_reference"),
    }
    for index, _group in enumerate(endpoint_groups(), 1):
        legacy_anchors[f"endpoint-group-{index}"] = reverse("mailing:api_docs_legacy") + f"#endpoint-group-{index}"
    for index, group in enumerate(groups, 1):
        group["slug"] = slugify(group["section"])
        group["index"] = index
        url = reverse("mailing:api_docs_workflow", args=[group["slug"]])
        links.append({"title": group["section"], "url": url})
        legacy_anchors[f"workflow-{index}"] = url
        for example in group["items"]:
            legacy_anchors[example["id"]] = url + "#" + example["id"]
    return {
        "workflow_examples": groups,
        "workflow_links": links,
        "docs_base_url": docs_base_url(),
        "legacy_anchors": legacy_anchors,
        "openapi_json_url": "mailing:api_docs_json",
    }


@staff_member_required
def api_docs(request):
    context = documentation_context()
    return render(request, "mailing/operator/api_docs.html", context)


@staff_member_required
def api_docs_workflow(request, workflow):
    context = documentation_context()
    group = next((group for group in context["workflow_examples"] if group["slug"] == workflow), None)
    if group is None:
        raise Http404("Unknown documentation workflow")
    context["group"] = group
    return render(request, "mailing/operator/api_docs_workflow.html", context)


@staff_member_required
def api_docs_reference(request):
    context = documentation_context()
    query = request.GET.get("q", "").strip()
    method = request.GET.get("method", "").upper()
    tag = request.GET.get("tag", "")
    spec = build_openapi_spec()
    operations = []
    tags = set()
    for path, path_item in spec["paths"].items():
        for verb, operation in path_item.items():
            if verb not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            operation_tags = operation.get("tags", [])
            tags.update(operation_tags)
            haystack = " ".join(
                [path, verb, operation.get("summary", ""), operation.get("description", ""), *operation_tags]
            ).casefold()
            if (
                (query and query.casefold() not in haystack)
                or (method and method != verb.upper())
                or (tag and tag not in operation_tags)
            ):
                continue
            operations.append(
                {
                    "method": verb.upper(),
                    "path": path,
                    "summary": operation.get("summary", operation.get("operationId", path)),
                    "description": operation.get("description", ""),
                    "contract": json.dumps(operation, indent=2),
                    "id": operation.get("operationId", slugify(verb + path)),
                }
            )
    page = Paginator(operations, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context.update(
        {
            "page_obj": page,
            "query": query,
            "method": method,
            "tag": tag,
            "tags": sorted(tags),
            "filter_query": params.urlencode(),
            "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
        }
    )
    return render(request, "mailing/operator/api_docs_reference.html", context)


@staff_member_required
def api_docs_legacy(request):
    context = documentation_context()
    context.update({"endpoint_groups": endpoint_groups(), "demo_api_keys": DEMO_API_KEYS})
    return render(request, "mailing/operator/api_docs_legacy.html", context)
