"""Repo-wide guard: a joined ``select_for_update()`` must say which rows it locks.

On PostgreSQL ``select_for_update().select_related(...)`` without ``of=``
emits ``FOR UPDATE`` with no ``OF`` clause, which locks the row of *every*
joined table. On 2026-10-26 (sender path) and 2026-10-27 (SES webhook
``correlate_ses_message``) that put a ``FOR UPDATE`` on the shared
``contacts``/``clients`` rows, and concurrent sends -- whose inserts take a
``FOR KEY SHARE`` on those same rows -- deadlocked against it.

Two checks, because neither alone sees every call site:

* a static AST walk over the source tree that flags any call chain containing
  both ``select_for_update(...)`` without ``of=`` and ``select_related(...)``;
* a runtime check (``conftest.py``) that fails any test whose evaluated query
  combines ``select_for_update`` and ``select_related`` without ``of`` -- it
  catches querysets built across several statements.
"""

import ast
from pathlib import Path

import pytest
from django.db import transaction

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "node_modules", ".git", "migrations", "static", "__pycache__"}


def _source_files():
    for path in ROOT.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        yield path


def _chain_calls(node):
    """Yield every ``Call`` in a fluent chain ``a.b(...).c(...).d(...)``."""
    while True:
        if isinstance(node, ast.Call):
            yield node
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return


def _method_name(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def find_unscoped_joined_locks(source, filename="<string>"):
    """Return ``(lineno, text)`` for each joined lock without ``of=``."""
    tree = ast.parse(source, filename=filename)
    inner_calls = set()
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in inner_calls:
            continue
        calls = list(_chain_calls(node))
        for call in calls[1:]:
            inner_calls.add(id(call))
        names = [_method_name(call) for call in calls]
        if "select_related" not in names:
            continue
        for call in calls:
            if _method_name(call) != "select_for_update":
                continue
            if not any(keyword.arg == "of" for keyword in call.keywords):
                violations.append((call.lineno, ast.get_source_segment(source, node) or ""))
    return violations


def test_guard_flags_a_joined_lock_without_of():
    flagged = find_unscoped_joined_locks(
        'Message.objects.select_for_update().select_related("contact").get(pk=1)\n'
        'Message.objects.select_related("contact").select_for_update(skip_locked=True).first()\n'
    )
    assert [line for line, _ in flagged] == [1, 2]


def test_guard_accepts_scoped_and_unjoined_locks():
    assert not find_unscoped_joined_locks(
        'Message.objects.select_for_update(of=("self",)).select_related("contact").get(pk=1)\n'
        'Message.objects.select_for_update().get(pk=1)\n'
        'Message.objects.select_related("contact").get(pk=1)\n'
    )


def test_no_joined_select_for_update_without_of_in_the_repo():
    offenders = []
    for path in _source_files():
        source = path.read_text()
        if "select_for_update" not in source:
            continue
        for lineno, text in find_unscoped_joined_locks(source, filename=str(path)):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {' '.join(text.split())[:160]}")
    assert not offenders, (
        "select_for_update() combined with select_related() must pass of=('self',) "
        "(or an explicit list of the joined rows it really means to lock):\n" + "\n".join(offenders)
    )


@pytest.mark.django_db
def test_runtime_guard_rejects_a_joined_lock_without_of():
    from mailing.models import TransactionalMessage  # noqa: PLC0415 - Django must be configured first

    # Built over two statements on purpose: the AST walk cannot see this one.
    joined = TransactionalMessage.objects.select_related("contact")
    with transaction.atomic():
        with pytest.raises(AssertionError, match="without of="):
            list(joined.select_for_update())
        assert list(joined.select_for_update(of=("self",))) == []
