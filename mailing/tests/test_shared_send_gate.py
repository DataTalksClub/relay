"""_SharedGate: one gate read in flight, but never a result older than the caller's question."""

import threading
import time

import pytest

from mailing.services.campaign_sender import _SharedGate


def test_caller_arriving_mid_read_waits_for_a_fresh_read():
    release_first = threading.Event()
    reads = []

    def read(campaign_id):
        number = len(reads) + 1
        reads.append(number)
        if number == 1:
            release_first.wait(5)
        return f"state-{number}"

    gate = _SharedGate(7, read=read)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", gate.read()))
    first.start()
    while not reads:
        time.sleep(0.001)
    # Asked while read 1 is running: read 1 may reflect state from before this question.
    late = [threading.Thread(target=lambda i=i: results.setdefault(f"late{i}", gate.read())) for i in range(5)]
    for t in late:
        t.start()
    time.sleep(0.05)
    release_first.set()
    first.join(5)
    for t in late:
        t.join(5)
    assert results["first"] == "state-1"
    assert {results[f"late{i}"] for i in range(5)} == {"state-2"}
    assert reads == [1, 2]  # five late callers shared one fresh read


def test_sequential_callers_each_get_a_new_read():
    counter = iter(range(1, 100))
    gate = _SharedGate(1, read=lambda _cid: next(counter))
    assert [gate.read() for _ in range(3)] == [1, 2, 3]


def test_read_error_reaches_every_waiter_and_next_read_retries():
    calls = []

    def read(_cid):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return "ok"

    gate = _SharedGate(1, read=read)
    with pytest.raises(RuntimeError):
        gate.read()
    assert gate.read() == "ok"
