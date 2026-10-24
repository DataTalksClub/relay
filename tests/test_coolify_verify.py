from scripts.coolify_verify import coolify_ready


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
