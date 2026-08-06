"""Shared background-task layer for Django 6 projects.

Application code depends on ``django.tasks`` for defining and enqueueing work.
taskdeck adds what the standard interface does not cover: a status projection
that survives crossing a service boundary, progress, tenant scoping, and
transactional enqueue.

Re-exports are resolved lazily. Importing this package must not pull in models,
or any project listing ``taskdeck`` in ``INSTALLED_APPS`` would touch the model
layer before the app registry is ready.
"""

_LAZY = {
    "bind_correlation": "taskdeck.context",
    "current_correlation_id": "taskdeck.context",
    "current_owner_id": "taskdeck.context",
    "current_run_id": "taskdeck.context",
    "enqueue_on_commit": "taskdeck.signals",
    "heartbeat": "taskdeck.progress",
    "report": "taskdeck.progress",
    "set_entity": "taskdeck.progress",
    "set_owner": "taskdeck.progress",
    "set_total": "taskdeck.progress",
}

__all__ = [
    "bind_correlation",
    "current_correlation_id",
    "current_owner_id",
    "current_run_id",
    "enqueue_on_commit",
    "heartbeat",
    "report",
    "set_entity",
    "set_owner",
    "set_total",
]


def __getattr__(name):
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module 'taskdeck' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
