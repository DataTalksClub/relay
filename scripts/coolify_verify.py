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

The workflow requires all three forms of evidence:
  * Coolify half: every application image == expected pinned image, every
    status startswith "running", no fatal state (exited/dead/unhealthy).
    Fatal fails fast; anything else keeps polling.
  * Release half: ``GET <public>/health/ready`` returns
    ``{"status": "ready", "image_tag": <want>, ...}``. An old tag, a
    missing tag (pre-fix container), or a non-ready status keeps polling;
    only a matching ready payload proves the rolling follower promoted
    the new containers. Failures to fetch are transient and keep polling
    until the deadline.
  * Actual Docker containers: the exact compose service set has been
    replaced since a host-clock baseline taken before dispatch; every
    container has the pinned image and is running, with any configured
    healthcheck healthy. The environment-restricted SSH inspection returns
    only identity/state fields, never environment variables.

The service aggregate must have settled to running (running:unknown is
valid for workers without healthchecks). Public readiness bypasses caches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

    When the expected SHA is supplied, both baked release fields must
    match. Missing identity is never proof of the expected release.
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
        if got_sha != str(want_sha):
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


def service_ready(service: dict, want_image: str, payload: dict | None,
                  want_tag: str, want_sha: str) -> tuple[bool, str, bool]:
    """Wait for the service rollout as well as the public rolling follower.

    Service deploy responses on this Coolify installation contain only a
    resource UUID, not an application deployment UUID. Its starting aggregate
    can coexist with stale per-app running metadata. Require a settled running
    aggregate, while allowing running:unknown for workers without healthchecks.
    """
    ready, reason, fatal = deploy_ready(service.get("applications"), want_image, payload, want_tag, want_sha)
    status = str(service.get("status", "") or "").lower()
    # starting:unhealthy is a normal rollout state; per-app unhealthy still
    # fails fast through deploy_ready. A settled unhealthy service is fatal.
    service_fatal = any(s in status for s in ("failed", "exited", "dead")) or status == "running:unhealthy"
    if fatal or service_fatal:
        return False, f"aggregate={status}; {reason}", True
    if not status.startswith("running"):
        return False, f"aggregate={status} not running; {reason}", False
    return ready, f"aggregate={status}; {reason}", False


def runtime_ready(snapshot: dict, baseline: dict, names: set[str], want: str) -> tuple[bool, str, bool]:
    """Actual Docker evidence, including replacement on same-tag redeploys."""
    containers = snapshot.get("containers") or []
    by_name = {item["name"]: item for item in containers}
    started_after = datetime.fromisoformat(baseline["observed_at"])
    old_ids = {item["id"] for item in baseline.get("containers", [])}
    # Check all new containers before transient set/image checks, so a failed
    # new worker cannot be hidden by another worker still being replaced.
    for item in containers:
        created = datetime.fromisoformat(item["created"].replace("Z", "+00:00"))
        if item["id"] not in old_ids and created > started_after and (
            item["status"] in {"exited", "dead"} or item["health"] == "unhealthy"
        ):
            return False, f"runtime {item['name']}: failed {item['status']}/{item['health']}", True
    if not names or set(by_name) != names or len(containers) != len(names):
        return False, "runtime container set does not match expected services", False
    for name in sorted(names):
        item = by_name[name]
        created = datetime.fromisoformat(item["created"].replace("Z", "+00:00"))
        if item["id"] in old_ids or created <= started_after or item["image"] != want:
            return False, f"runtime {name}: old container/image still present", False
        if item["status"] != "running" or not item["image_id"] or item["health"] not in {None, "healthy"}:
            return False, f"runtime {name}: not running/healthy yet", False
    return True, f"all {len(names)} actual containers replaced and running on {want}", False


def inspect_runtime(timeout=30):
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
        "-o", f"UserKnownHostsFile={os.environ['RELAY_VERIFY_KNOWN_HOSTS']}",
        "-i", os.environ["RELAY_VERIFY_SSH_KEY"], os.environ["PREFLIGHT_TARGET"],
        f"{os.environ['PREFLIGHT_ENVIRONMENT']} inspect",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return json.loads(result.stdout)


def verify(*, timeout: float = 300, interval: float = 5) -> int:
    """Shared workflow entrypoint; monotonic deadline includes HTTP requests."""
    base = os.environ["COOLIFY_URL"].rstrip("/")
    token = os.environ["COOLIFY_TOKEN"]
    uuid = os.environ["COOLIFY_SERVICE_UUID"]
    want_tag = os.environ["IMAGE_TAG"]
    want_sha = os.environ["SOURCE_SHA"]
    want = f"{os.environ['IMAGE_REF']}:{want_tag}"
    baseline = json.loads(Path(".tmp/relay-container-baseline.json").read_text())
    names = set(json.loads(os.environ["EXPECTED_SERVICES"]))
    public_url = urlsplit(os.environ["PUBLIC_READY_URL"])
    start = time.monotonic()
    deadline = start + timeout
    last = "no successful poll"

    def fetch(request, request_timeout):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("verification deadline reached")
        with urllib.request.urlopen(request, timeout=min(request_timeout, remaining)) as response:
            return json.loads(response.read() or b"{}")

    while time.monotonic() < deadline:
        service = None
        payload = None
        try:
            service = fetch(urllib.request.Request(
                f"{base}/api/v1/services/{uuid}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ), 30)
            if not isinstance(service, dict):
                raise ValueError("service response is not an object")
        except Exception as exc:
            # Do not log exception bodies/URLs: these may contain credentials.
            last = f"transient Coolify fetch error: {type(exc).__name__}"
        else:
            # Public readiness must bypass intermediary caches on every poll.
            query = parse_qsl(public_url.query, keep_blank_values=True)
            query.append(("relay_verify", str(time.time_ns())))
            url = urlunsplit(public_url._replace(query=urlencode(query)))
            try:
                payload = fetch(urllib.request.Request(url, headers={
                    "Accept": "application/json", "Cache-Control": "no-cache, no-store",
                }), 15)
            except Exception as exc:
                last = f"transient public health fetch error: {type(exc).__name__}"
            ready, reason, fatal = service_ready(service, want, payload, want_tag, want_sha)
            last = reason
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("verification deadline reached")
                runtime = inspect_runtime(timeout=min(30, remaining))
                runtime_ok, runtime_reason, runtime_fatal = runtime_ready(runtime, baseline, names, want)
                ready = ready and runtime_ok
                fatal = fatal or runtime_fatal
                last = f"{reason}; {runtime_reason}"
            except Exception as exc:
                ready = False
                last = f"{reason}; transient runtime inspection: {type(exc).__name__}"
            if fatal:
                print(f"::error::Coolify deploy failed fast: {last}", flush=True)
                return 1
            if ready and time.monotonic() < deadline:
                print(f"coolify verify done ready=True elapsed_s={time.monotonic() - start:.1f} {last}", flush=True)
                return 0
        print(f"coolify verify: {last}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    print(f"::error::coolify verify done ready=False elapsed_s={time.monotonic() - start:.1f} {last}", flush=True)
    return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["snapshot"]:
        Path(".tmp").mkdir(exist_ok=True)
        Path(".tmp/relay-container-baseline.json").write_text(json.dumps(inspect_runtime()))
        print("Recorded pre-deploy runtime container baseline")
    elif sys.argv[1:]:
        raise SystemExit("Expected no arguments or 'snapshot'")
    else:
        raise SystemExit(verify())
