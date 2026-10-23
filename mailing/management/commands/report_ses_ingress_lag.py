import json
import math
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import EmailEvent


def percentile(sorted_values, fraction):
    """Continuous percentile, with linear interpolation between observations."""
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


class Command(BaseCommand):
    help = "Report SES SNS-to-EmailEvent persistence lag over the last 24 hours (read only)."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=24)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        hours = options["hours"]
        if not math.isfinite(hours) or hours <= 0:
            raise CommandError("--hours must be a finite positive number")
        end = timezone.now()
        try:
            start = end - timedelta(hours=hours)
        except OverflowError as exc:
            raise CommandError("--hours is too large") from exc
        report = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "events": 0,
            "samples": 0,
            "missing_timestamp": 0,
            "invalid_timestamp": 0,
            "negative_lag": 0,
        }
        lags = []
        events = EmailEvent.objects.filter(
            created_at__gte=start, created_at__lte=end, metadata__provider="ses",
        ).order_by().values_list("created_at", "metadata").iterator(chunk_size=2000)
        for created_at, metadata in events:
            report["events"] += 1
            value = metadata.get("sns_timestamp")
            if value in (None, ""):
                report["missing_timestamp"] += 1
                continue
            try:
                timestamp = parse_datetime(value) if isinstance(value, str) else None
            except (ValueError, OverflowError):
                timestamp = None
            if timestamp is None or timezone.is_naive(timestamp):
                report["invalid_timestamp"] += 1
                continue
            lag = (created_at - timestamp).total_seconds()
            if lag < 0:
                report["negative_lag"] += 1
                continue
            lags.append(lag)
        lags.sort()
        report["samples"] = len(lags)
        report["p50_seconds"] = percentile(lags, 0.5)
        report["p95_seconds"] = percentile(lags, 0.95)
        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        self.stdout.write(f"SES ingress lag: {report['start']} to {report['end']}")
        self.stdout.write(f"events={report['events']} samples={report['samples']} "
                          f"missing_timestamp={report['missing_timestamp']} "
                          f"invalid_timestamp={report['invalid_timestamp']} negative_lag={report['negative_lag']}")
        for name in ("p50_seconds", "p95_seconds"):
            value = report[name]
            self.stdout.write(f"{name}={value:.3f}" if value is not None else f"{name}=unavailable")
