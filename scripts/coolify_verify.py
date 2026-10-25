"""Coolify deploy verification helper (Relay).

Root cause it encodes: a Coolify *service* made of N compose services
reports aggregate ``status == "running:healthy"`` only when every
healthchecked container is healthy. Relay's compose templates define a
``healthcheck`` on ``web`` only, so the aggregate stays
``running:unknown`` forever and the old workflow (poll aggregate ==
``running:healthy`` 96x5s) always burned ~8 minutes before falling
through to the /health/ready curl.

Follow-up false positive (intake 2f3aed4b, prod run 40152657824): the
per-application fix (301be94) gates on
``GET /api/v1/services/<uuid> -> applications[]`` image + running state,
but ``applications[].image`` is Coolify's *desired* state from the PATCH,
not the running container. It flips to the new tag instantly while the
old containers still serve traffic, so the gate passes with
``elapsed_s=0`` before the new containers log. The authoritative proof
that the NEW release is running is the public app serving the new
release identity from ``/health/ready`` (``image_tag``/``source_sha``
baked into the image at build time).

The gate is therefore two halves, polled together until BOTH are true:
  * Coolify half: every application image == expected pinned image, every
    status startswith "running", no fatal state (exited/dead/unhealthy).
    Fatal fails fast; anything else keeps polling.
  * Release half: ``GET <public>/health/ready`` returns
    ``{"status": "ready", "image_tag": <want>, ...}``. An old tag, a
    missing tag (pre-fix container), or a non-ready status keeps polling;
    only a matching ready payload proves the rolling follower promoted
    the new containers. Failures to fetch are transient and keep polling
    until the deadline.

/health/ready is the user-facing gate after Coolify reports ready.
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

    NOTE: ``ready=True`` here alone does NOT prove the new containers
    run -- ``applications[].image`` is Coolify's desired state and flips
    the instant the compose PATCH lands. Callers must ALSO require
    :func:`release_matches` on the public ``/health/ready`` payload.
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


def release_matches(payload: dict | None, want_tag: str, want_sha: str | None = None) -> tuple[bool, str]:
    """Evaluate one public ``/health/ready`` payload against the release.

    Returns (matched, reason). Never fatal: an old tag, a missing tag
    (pre-fix container serving only ``status``), a non-ready status, or
    a malformed payload all mean "the new release is not serving yet --
    keep polling". A deadline expiry still fails the job, and a Coolify
    fatal alongside it fails fast via :func:`coolify_ready`.

    ``want_sha`` is best-effort: when the payload carries a concrete
    ``source_sha`` (not ``"unknown"``) and a concrete ``want_sha`` is
    given, the SHAs must match too. Tag is always required -- the tag
    embeds the short SHA (``YYYYMMDD-HHMMSS-<7>``), so a tag match alone
    already binds the release.
    """
    if not isinstance(payload, dict):
        return False, "health payload is not a JSON object yet"
    status = str(payload.get("status", "") or "")
    if status != "ready":
        return False, f"health status {status!r} != 'ready'"
    got_tag = payload.get("image_tag", "")
    if not got_tag:
        return False, "health payload has no image_tag (old container still serving)"
    if str(got_tag) != str(want_tag):
        return False, f"health image_tag {got_tag!r} != {want_tag!r} (old release still serving)"
    if want_sha:
        got_sha = str(payload.get("source_sha", "") or "")
        if got_sha and got_sha != "unknown" and str(want_sha) != "unknown" and got_sha != str(want_sha):
            return False, f"health source_sha {got_sha!r} != {want_sha!r}"
    return True, f"public app serves {want_tag}"


def deploy_ready(
    applications: list[dict] | None,
    want_image: str,
    health_payload: dict | None,
    want_tag: str,
    want_sha: str | None = None,
) -> tuple[bool, str, bool]:
    """Combined gate: Coolify desired-state AND public release must agree.

    Returns (ready, reason, fatal). ``fatal`` comes only from the Coolify
    half (exited/unhealthy/dead/failed). A Coolify-ready + old-release
    combination -- the 40152657824 false positive -- is explicitly
    NOT ready and NOT fatal: keep polling until the new containers
    serve, or the deadline expires.
    """
    cool_ok, cool_reason, fatal = coolify_ready(applications, want_image)
    if fatal:
        return False, f"coolify fatal: {cool_reason}", True
    rel_ok, rel_reason = release_matches(health_payload, want_tag, want_sha)
    if not cool_ok:
        return False, f"coolify not ready: {cool_reason}; release: {rel_reason}", False
    if not rel_ok:
        return False, f"coolify reports new image but public app not on release yet: {rel_reason}", False
    return True, f"coolify ready and {rel_reason}", False
