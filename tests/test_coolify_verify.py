from scripts.coolify_verify import coolify_ready, deploy_ready, release_matches


def test_ready_web_healthy_plus_workers_unknown():
    want = "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0"
    apps = [
        {"name": "web", "image": want, "status": "running:healthy"},
        {"name": "worker", "image": want, "status": "running:unknown"},
        {"name": "scheduler", "image": want, "status": "running"},
    ]
    ready, _, fatal = coolify_ready(apps, want)
    assert ready is True
    assert fatal is False


def test_not_ready_old_image_is_not_fatal():
    want = "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0"
    apps = [
        {"name": "web", "image": "ghcr.io/datatalksclub/relay:20261024-010000-0000000", "status": "running:healthy"},
        {"name": "worker", "image": "ghcr.io/datatalksclub/relay:20261024-010000-0000000", "status": "running"},
    ]
    ready, _, fatal = coolify_ready(apps, want)
    assert ready is False
    assert fatal is False


def test_unhealthy_web_is_fatal():
    want = "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0"
    apps = [
        {"name": "web", "image": want, "status": "running:unhealthy"},
        {"name": "worker", "image": want, "status": "running"},
    ]
    ready, _, fatal = coolify_ready(apps, want)
    assert ready is False
    assert fatal is True


def test_exited_worker_is_fatal():
    want = "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0"
    apps = [
        {"name": "web", "image": want, "status": "running:healthy"},
        {"name": "worker", "image": want, "status": "exited"},
    ]
    ready, _, fatal = coolify_ready(apps, want)
    assert ready is False
    assert fatal is True


def test_restarting_is_not_ready_but_not_fatal():
    want = "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0"
    apps = [
        {"name": "web", "image": want, "status": "restarting"},
    ]
    ready, _, fatal = coolify_ready(apps, want)
    assert ready is False
    assert fatal is False


def test_empty_applications_is_not_ready():
    ready, _, fatal = coolify_ready([], "ghcr.io/datatalksclub/relay:20261025-010000-abcdef0")
    assert ready is False
    assert fatal is False


def test_release_matches_new_release_serving():
    ok, _ = release_matches(
        {"status": "ready", "image_tag": "20261025-010000-abcdef0", "source_sha": "abcdef0"},
        "20261025-010000-abcdef0",
        "abcdef0",
    )
    assert ok is True


def test_release_old_version_served_is_not_ready_not_fatal():
    # The public follower still routes to the previous release: keep polling.
    ok, reason = release_matches(
        {"status": "ready", "image_tag": "20261024-010000-0000000", "source_sha": "0000000"},
        "20261025-010000-abcdef0",
        "abcdef0",
    )
    assert ok is False
    assert "old release" in reason or "!=" in reason


def test_release_missing_tag_is_old_container_not_ready():
    # Pre-fix containers serve only {"status": "ready"} with no release fields.
    ok, _ = release_matches({"status": "ready"}, "20261025-010000-abcdef0", "abcdef0")
    assert ok is False


def test_release_not_ready_status_is_not_matched():
    ok, _ = release_matches(
        {"status": "not_ready", "image_tag": "20261025-010000-abcdef0"},
        "20261025-010000-abcdef0",
    )
    assert ok is False


def test_deploy_false_positive_stale_configured_image_vs_running():
    # Regression for prod run 40152657824: Coolify already reports the new
    # configured image as running, but the public app still serves the old
    # release. The combined gate must NOT be ready (and must NOT fail fast).
    want_image = "ghcr.io/datatalksclub/relay:20261025-000721-301be94"
    apps = [
        {"name": "web", "image": want_image, "status": "running:healthy"},
        {"name": "worker", "image": want_image, "status": "running:unknown"},
    ]
    cool_ok, _, cool_fatal = coolify_ready(apps, want_image)
    assert cool_ok is True  # stale desired-state passes on its own
    assert cool_fatal is False
    ready, _, fatal = deploy_ready(
        apps,
        want_image,
        {"status": "ready", "image_tag": "20261025-000000-fb785a9", "source_sha": "fb785a9"},
        "20261025-000721-301be94",
        "301be94",
    )
    assert ready is False
    assert fatal is False


def test_deploy_ready_only_when_both_halves_agree():
    want_image = "ghcr.io/datatalksclub/relay:20261025-000721-301be94"
    apps = [
        {"name": "web", "image": want_image, "status": "running:healthy"},
        {"name": "worker", "image": want_image, "status": "running:unknown"},
    ]
    ready, _, fatal = deploy_ready(
        apps,
        want_image,
        {"status": "ready", "image_tag": "20261025-000721-301be94", "source_sha": "301be94"},
        "20261025-000721-301be94",
        "301be94",
    )
    assert ready is True
    assert fatal is False


def test_deploy_failed_container_is_fatal_even_with_old_release():
    want_image = "ghcr.io/datatalksclub/relay:20261025-000721-301be94"
    apps = [
        {"name": "web", "image": want_image, "status": "running:healthy"},
        {"name": "worker", "image": want_image, "status": "exited"},
    ]
    ready, _, fatal = deploy_ready(
        apps,
        want_image,
        {"status": "ready", "image_tag": "20261025-000000-fb785a9"},
        "20261025-000721-301be94",
    )
    assert ready is False
    assert fatal is True
