#!/usr/bin/python3
"""Root-owned forced SSH command; never accepts a shell command or DB secret.

Install as /usr/local/sbin/relay-db-preflight. See docs/database-ownership.md.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TARGETS = {
    "development": ("t9uudl5ik40webxrzamrm8qy", "relay_dev", "relay_dev"),
    "production": ("fjnzwomg8n42lztloo34just", "relay", "relay_app"),
}


def run_preflight(selection, credentials, run=subprocess.run):
    match = re.fullmatch(r"(development|production) (\d{8}-\d{6}-[0-9a-f]{7})", selection)
    if not match:
        raise ValueError("Expected an allowed environment and immutable Relay release tag")
    environment, tag = match.groups()
    uuid, database, role = TARGETS[environment]
    image = f"ghcr.io/datatalksclub/relay:{tag}"
    print(f"Preparing DB preflight: environment={environment} tag={tag}", flush=True)
    inspected = run(
        ["docker", "inspect", f"web-{uuid}"], check=True, capture_output=True, text=True,
    )
    web = json.loads(inspected.stdout)[0]
    networks = web["NetworkSettings"]["Networks"]
    if "coolify" not in networks:
        raise ValueError("Serving web container is missing the expected database network")
    # Only the database and Django bootstrap configuration are needed; no mail,
    # client API, AWS, or webhook credentials are passed to the migration runner.
    env = [v for v in web["Config"]["Env"] if v.split("=", 1)[0] in {"DATABASE_URL", "SECRET_KEY"}]
    if not any(v.startswith("DATABASE_URL=") for v in env):
        raise ValueError("Serving web container has no migration database configuration")
    with tempfile.TemporaryDirectory(prefix="relay-preflight-") as directory:
        env_file = Path(directory) / "migration.env"
        env_file.write_text("\n".join(env) + "\n")
        env_file.chmod(0o600)
        docker = ["docker", "--config", directory]
        print("Authenticating candidate image pull", flush=True)
        run(
            [*docker, "login", "ghcr.io", "--username", credentials["username"], "--password-stdin"],
            input=credentials["token"], check=True, capture_output=True, text=True,
        )
        print("Pulling candidate image", flush=True)
        run([*docker, "pull", "--quiet", image], check=True, capture_output=True, text=True)
        print(f"Candidate-image DB preflight: environment={environment} tag={tag}", flush=True)
        # Override the normal startup gate so ownership is checked BEFORE any DDL.
        run(
            [*docker, "run", "--rm", "--network", "coolify", "--env-file", str(env_file),
             "--entrypoint", "python", image, "/app/manage.py", "db_preflight",
             "--expected-role", role, "--expected-database", database],
            check=True,
        )


if __name__ == "__main__":
    try:
        # SSH_ORIGINAL_COMMAND is passed as one quoted argument by authorized_keys.
        allowed_environment, selection = sys.argv[1:]
        if allowed_environment not in TARGETS or not selection.startswith(allowed_environment + " "):
            raise ValueError("SSH key is restricted to one environment")
        run_preflight(selection, json.loads(sys.stdin.read(16384)))
    except (ValueError, KeyError, IndexError, subprocess.SubprocessError):
        print("Candidate DB preflight FAILED; serving containers were not changed.", file=sys.stderr)
        sys.exit(1)
