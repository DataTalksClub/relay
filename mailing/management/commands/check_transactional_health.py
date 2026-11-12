import json

from django.core.management.base import BaseCommand

from mailing.services.transactional_monitor import check_transactional_health, send_monitor_alert


class Command(BaseCommand):
    help = "Run transactional monitoring, or send an explicit test to dev@dtcdev.click only."

    def add_arguments(self, parser):
        parser.add_argument("--test-alert", action="store_true")

    def handle(self, *args, **options):
        if options["test_alert"]:
            send_monitor_alert({"kind": "forced_test", "client_id": None}, test=True)
            self.stdout.write("relay_transactional_monitor_test_alert_sent")
            return
        alerts = check_transactional_health(force=True)
        self.stdout.write(json.dumps({"event": "relay_transactional_monitor_heartbeat", "alerts": alerts}))
