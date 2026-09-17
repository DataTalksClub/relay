#!/usr/bin/env python3
"""Generate a synthetic history export for import_history and the import APIs.

The rows are made up; no real addresses, subjects or provider ids appear. Use it
to rehearse a history import, or to re-run the import verification:

    uv run python scripts/generate_history_fixture.py --out .tmp/history-10k.json
    uv run python manage.py import_history --from-json .tmp/history-10k.json
    uv run python manage.py import_history --from-json .tmp/history-10k.json

The second run must report every row as skipped and create nothing.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = datetime(2023, 1, 1, 8, 0, 0, tzinfo=UTC)
RECIPIENTS_PER_CAMPAIGN = 14
CONTACTS = 900

TEMPLATES = ["registration-welcome", "payment-failed", "event-reminder", "lead-magnet-delivery"]
EVENT_TYPES = ["bounce", "complaint", "open", "click"]
MESSAGE_STATUSES = ["sent", "bounced", "complained", "failed"]


def stamp(minutes):
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def address(index):
    return f"learner{index % CONTACTS}@example.test"


def build_campaigns(count):
    return [
        {
            "client_reference": f"history-campaign-{index}",
            "subject": f"Synthetic campaign {index}",
            "status": "sent",
            "created_at": stamp(index * 60),
            "sent_at": stamp(index * 60 + 1),
            "recipients": [
                {
                    "email": address(index * RECIPIENTS_PER_CAMPAIGN + offset),
                    "status": "sent" if offset % 7 else "bounced",
                    "created_at": stamp(index * 60 + 2),
                    "sent_at": stamp(index * 60 + 2),
                    "delivered_at": stamp(index * 60 + 3) if offset % 3 else None,
                    "open_count": offset % 4,
                    "ses_message_id": f"synthetic-campaign-{index}-{offset}",
                }
                for offset in range(RECIPIENTS_PER_CAMPAIGN)
            ],
        }
        for index in range(count)
    ]


def build_messages(count):
    return [
        {
            "client_reference": f"history-message-{index}",
            "email": address(index),
            "template_key": TEMPLATES[index % len(TEMPLATES)],
            "subject": f"Synthetic message {index}",
            "status": MESSAGE_STATUSES[index % len(MESSAGE_STATUSES)],
            "created_at": stamp(20000 + index),
            "sent_at": stamp(20000 + index),
            "delivered_at": stamp(20000 + index) if index % 2 else None,
            "open_count": index % 3,
            "click_count": index % 2,
            "ses_message_id": f"synthetic-message-{index}",
        }
        for index in range(count)
    ]


def build_events(count, message_count):
    return [
        {
            "client_reference": f"history-event-{index}",
            "email": address(index),
            "event_type": EVENT_TYPES[index % len(EVENT_TYPES)],
            "created_at": stamp(20000 + index + 1),
            "message_reference": f"history-message-{index}" if index < message_count else "",
            "url": "https://example.test/course" if index % 4 == 3 else "",
            "metadata": {"source": "ses"},
        }
        for index in range(count)
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Path to write the JSON export to.")
    parser.add_argument("--client", default="aisl-website", help="Client slug written into the file.")
    parser.add_argument("--audience", default="aisl", help="Audience slug written into the file.")
    parser.add_argument("--campaigns", type=int, default=200, help="Number of campaigns.")
    parser.add_argument("--messages", type=int, default=4000, help="Number of transactional messages.")
    parser.add_argument("--events", type=int, default=3000, help="Number of email events.")
    options = parser.parse_args()

    campaigns = build_campaigns(options.campaigns)
    messages = build_messages(options.messages)
    events = build_events(options.events, options.messages)
    recipients = sum(len(campaign["recipients"]) for campaign in campaigns)

    document = {
        "client": options.client,
        "audience": options.audience,
        "campaigns": campaigns,
        "messages": messages,
        "events": events,
    }
    path = Path(options.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    print(
        json.dumps(
            {
                "path": str(path),
                "campaigns": len(campaigns),
                "campaign_recipients": recipients,
                "messages": len(messages),
                "events": len(events),
                "total_rows": len(campaigns) + recipients + len(messages) + len(events),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
