"""Subprocess entry point for the claim/finalize fault tests (not collected by pytest).

Runs one campaign batch against ``FaultSes`` so the parent test can SIGKILL it
after SES accepts a message, or SIGTERM it mid-batch. Prints a JSON summary of
the fake SES state on a clean exit.
"""

import json
import os
import sys

import django

django.setup()

from django.conf import settings  # noqa: E402

from mailing.services import campaign_sender  # noqa: E402
from mailing.tests.claim_finalize_harness import FaultSes  # noqa: E402

settings.CAMPAIGN_SEND_CONCURRENCY = int(os.environ["CF_CONCURRENCY"])
settings.SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND = float(os.environ.get("CF_RATE", "0"))

ses = FaultSes(
    latency=float(os.environ["CF_LATENCY"]),
    faults={int(k): v for k, v in json.loads(os.environ["CF_FAULTS"]).items()},
)
# Phase B: the sender provides this; the worker entry point calls it at startup.
install = getattr(campaign_sender, "install_sigterm_drain", None)
if install is not None:
    install()
print("READY", flush=True)
exit_code = 0
try:
    campaign_sender.send_campaign_batch(json.loads(os.environ["CF_PAYLOAD"]), ses_client=ses)
except Exception as exc:  # noqa: BLE001 - reported to the parent test
    print(f"BATCH_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    exit_code = 3
print(json.dumps({"calls": ses.calls, "accepted": ses.accepted}), flush=True)
sys.exit(exit_code)
