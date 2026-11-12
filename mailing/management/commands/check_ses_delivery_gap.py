import json

from django.core.management.base import BaseCommand

from mailing.services.ses_monitor import check_ses_delivery_gap
from mailing.services.transactional_monitor import send_monitor_alert


class Command(BaseCommand):
    help = "Run SES delivery gap monitoring, or send an explicit test alert to dev@dtcdev.click."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Bypass SNS alert cooldown; outcome gaps are reported once per SES send.")
        parser.add_argument("--test-alert", action="store_true", help="Send a test alert to dev@dtcdev.click.")

    def handle(self, *args, **options):
        if options["test_alert"]:
            send_monitor_alert({"kind": "ses_guard_forced_test", "client_id": None}, test=True)
            self.stdout.write("relay_ses_guard_test_alert_sent")
            return

        alerts = check_ses_delivery_gap(force=options["force"])
        self.stdout.write(json.dumps({"event": "relay_ses_delivery_gap_check", "alerts": alerts}))
