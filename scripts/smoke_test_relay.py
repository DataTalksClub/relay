#!/usr/bin/env python3
import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid


def request_json(url, *, api_key, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, json.load(response)


def main():
    parser = argparse.ArgumentParser(
        description="Submit a real Relay task and wait for the deployed worker to finish it."
    )
    parser.add_argument("--base-url", default="https://relay.dtcdev.click")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    api_key = os.environ.get("RELAY_BOOTSTRAP_API_KEY", "")
    if not api_key:
        raise SystemExit("RELAY_BOOTSTRAP_API_KEY is required")

    marker = uuid.uuid4().hex
    status, submitted = request_json(
        f"{args.base_url.rstrip('/')}/api/tasks",
        api_key=api_key,
        method="POST",
        payload={
            "type": "system.echo",
            "idempotency_key": f"deployment-smoke:{marker}",
            "correlation_id": str(uuid.uuid4()),
            "params": {"marker": marker},
        },
    )
    if status != 202:
        raise SystemExit(f"unexpected submission status: {status}")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        _, job = request_json(
            f"{args.base_url.rstrip('/')}/api/tasks/{submitted['id']}",
            api_key=api_key,
        )
        if job["status"] == "succeeded":
            if job["result"] != {"marker": marker}:
                raise SystemExit("Relay worker returned the wrong echo payload")
            print(json.dumps({"status": "ok", "task_id": job["id"], "marker": marker}))
            return
        if job["status"] in {"failed", "cancelled"}:
            raise SystemExit(f"Relay task failed: {job}")
        time.sleep(1)
    raise SystemExit("Relay worker did not finish the smoke task before the timeout")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Relay HTTP error {exc.code}: {exc.read().decode()}") from exc
