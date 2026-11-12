"""Consolidate already-sent batch campaigns into one canonical campaign.

Dry run (default) prints the plan and totals; nothing is written:

    manage.py merge_campaigns --canonical 5 --sources 6,7,8

Apply (sources must be repeated in --confirm-sources exactly):

    manage.py merge_campaigns --canonical 5 --sources 6,7,8 --apply --confirm-sources 6,7,8

Afterwards sweep late writes, or undo the merge:

    manage.py merge_campaigns --canonical 5 --settle
    manage.py merge_campaigns --canonical 5 --rollback

Never sends, queues or re-queues anything; see mailing.services.campaign_consolidation.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from mailing.services.campaign_consolidation import (
    CampaignMergeError,
    merge_campaigns,
    rollback_merge,
    settle_merge,
)


def _ids(value):
    try:
        return [int(v) for v in value.split(",") if v.strip()]
    except ValueError as exc:
        raise CommandError(f"invalid id list: {value!r}") from exc


class Command(BaseCommand):
    help = "Merge sent batch campaigns into one canonical campaign (reversible, never sends)."

    def add_arguments(self, parser):
        parser.add_argument("--canonical", type=int, required=True)
        parser.add_argument("--sources", default="")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm-sources", default="")
        parser.add_argument("--settle", action="store_true")
        parser.add_argument("--rollback", action="store_true")

    def handle(self, *args, canonical, sources, apply, confirm_sources, settle, rollback, **options):
        try:
            if rollback:
                result = rollback_merge(canonical)
            elif settle:
                result = settle_merge(canonical)
            else:
                source_ids = _ids(sources)
                if not source_ids:
                    raise CommandError("--sources is required")
                if apply and _ids(confirm_sources) != source_ids:
                    raise CommandError("--apply needs --confirm-sources identical to --sources")
                result = merge_campaigns(canonical, source_ids, apply=apply)
                if apply:
                    result["settle"] = settle_merge(canonical)
        except CampaignMergeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
