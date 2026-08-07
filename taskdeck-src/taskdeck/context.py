"""Ambient task context.

When a task starts, its run id and correlation id are put here. Anything that
enqueues while that task is running becomes a child of it, carrying the same
correlation id, with no argument threading in the task body.

The correlation id can also be set explicitly at the edge of a request — see
``bind_correlation`` — so that work triggered by another service inherits that
service's identifier and the whole chain reads as one operation.
"""

import contextlib
import uuid
from contextvars import ContextVar

_current_run_id: ContextVar[uuid.UUID | None] = ContextVar("taskdeck_run_id", default=None)
_correlation_id: ContextVar[uuid.UUID | None] = ContextVar("taskdeck_correlation", default=None)
_owner_id: ContextVar[str] = ContextVar("taskdeck_owner", default="")


def current_run_id():
    return _current_run_id.get()


def current_correlation_id():
    return _correlation_id.get()


def current_owner_id():
    return _owner_id.get()


def _set_running(run_id, correlation_id, owner_id=""):
    """Install the executing task as the ambient parent. Returns reset tokens."""
    return (
        _current_run_id.set(run_id),
        _correlation_id.set(correlation_id),
        _owner_id.set(owner_id or ""),
    )


def _reset(tokens):
    run_token, corr_token, owner_token = tokens
    _current_run_id.reset(run_token)
    _correlation_id.reset(corr_token)
    _owner_id.reset(owner_token)


@contextlib.contextmanager
def bind_correlation(correlation_id=None, owner_id=None):
    """Bind a correlation id (and optionally an owner) for the enclosed block.

    Use at a service boundary: a request that arrives carrying another
    service's correlation identifier binds it here, and every task enqueued
    while handling that request joins the same chain.
    """
    if correlation_id is not None and not isinstance(correlation_id, uuid.UUID):
        correlation_id = uuid.UUID(str(correlation_id))

    tokens = []
    if correlation_id is not None:
        tokens.append((_correlation_id, _correlation_id.set(correlation_id)))
    if owner_id is not None:
        tokens.append((_owner_id, _owner_id.set(owner_id)))
    try:
        yield correlation_id
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
