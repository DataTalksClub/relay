"""Prod compose must run enough transactional-lane consumers for bulk fan-out.

One db_worker sends one message at a time (~4 msg/s measured on prod), so a
transient-list fan-out to ~1000 recipients needs >= 10 consumers for 40 msg/s.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent.parent / "deploy" / "coolify" / "docker-compose.staging.yml"


def _lanes(command):
    if "db_worker" not in command or "--queue-name" not in command:
        return set()
    return set(command[command.index("--queue-name") + 1].split(","))


def test_prod_compose_runs_at_least_ten_transactional_consumers():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    lanes = {name: _lanes(svc.get("command", [])) for name, svc in services.items()}
    transactional = [name for name, queues in lanes.items() if "transactional" in queues]
    assert len(transactional) >= 10, transactional
    # The legacy "default" drain stays on exactly one consumer.
    assert [name for name, queues in lanes.items() if "default" in queues] == ["worker"]
    # Campaign lane is unchanged: exactly one consumer.
    assert [name for name, queues in lanes.items() if "campaign" in queues] == ["campaign-worker"]
