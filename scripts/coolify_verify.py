"""Coolify deploy verification helper (Relay).

Root cause it encodes: a Coolify *service* made of N compose services
reports aggregate ``status == "running:healthy"`` only when every
healthchecked container is healthy. Relay's compose templates define a
``healthcheck`` on ``web`` only, so the aggregate stays
``running:unknown`` forever and the old workflow (poll aggregate ==
``running:healthy`` 96x5s) always burned ~8 minutes before falling
through to the /health/ready curl.

The fix (mirroring CMP deploy-coolify.yml t658u) polls the per-application
list ``GET /api/v1/services/<uuid> -> applications[]`` and gates on:
  * every application image == expected pinned image, and
  * every application status startswith "running" (accepts
    running / running:healthy / running:unknown), and
  * no application reports a fatal state (exited/dead/unhealthy).

/health/ready is still the user-facing gate after Coolify reports ready.
"""

from __future__ import annotations

FATAL_SUBSTRINGS = ("unhealthy", "exited", "dead", "failed")


def _status(app: dict) -> str:
    return str(app.get("status", "") or "")


def _name(app: dict) -> str:
    for key in ("name", "service", "service_name"):
        value = app.get(key)
        if value:
            return str(value)
    return ""


def describe_applications(applications: list[dict]) -> str:
    parts = []
    for app in applications or []:
        parts.append(
            f"name={_name(app) or '?'} image={app.get('image')} status={_status(app)}"
        )
    return "; ".join(parts) if parts else "<no applications>"


def coolify_ready(applications: list[dict] | None, want_image: str) -> tuple[bool, str, bool]:
    """Evaluate one Coolify poll.

    Returns (ready, reason, fatal). ``fatal`` means the deploy is
    definitively broken (e.g. exited/unhealthy container) and the caller
    should fail fast instead of polling to the deadline. Non-fatal
    not-ready (old image, starting, restarting, missing list) means keep
    polling; a deadline expiry still fails the job.
    """
    if not applications:
        return False, "no applications in Coolify response yet", False
    problems: list[str] = []
    fatal = False
    for app in applications:
        image = str(app.get("image", "") or "")
        status = _status(app)
        lowered = status.lower()
        label = _name(app) or "?"
        if image != want_image:
            problems.append(f"{label}: image {image!r} != {want_image!r}")
        if not status.startswith("running"):
            problems.append(f"{label}: status {status!r} not running")
        if any(token in lowered for token in FATAL_SUBSTRINGS):
            problems.append(f"{label}: fatal status {status!r}")
            fatal = True
    if problems:
        return False, "; ".join(problems), fatal
    return True, f"all {len(applications)} apps on {want_image} and running", False
