#!/usr/bin/python3
"""Root-owned forced SSH command; never accepts a shell command or DB secret.

Install as /usr/local/sbin/relay-db-preflight. See docs/database-ownership.md.
"""

import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

TARGETS = {
    "development": ("t9uudl5ik40webxrzamrm8qy", "relay_dev", "relay_dev"),
    "production": ("fjnzwomg8n42lztloo34just", "relay", "relay_app"),
}


def inspect_runtime(environment, run=subprocess.run):
    """Read-only, environment-scoped evidence; never return container env."""
    uuid = TARGETS[environment][0]
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                check=True, capture_output=True, text=True).stdout.splitlines()
    names = [name for name in names if name.endswith(f"-{uuid}")]
    containers = []
    if names:
        result = run(["docker", "inspect", *names], check=True, capture_output=True, text=True)
        for item in json.loads(result.stdout):
            state = item["State"]
            containers.append({
                "name": item["Name"].lstrip("/").removesuffix(f"-{uuid}"),
                "id": item["Id"], "image": item["Config"]["Image"],
                "image_id": item["Image"], "created": item["Created"],
                "status": state["Status"], "health": state.get("Health", {}).get("Status"),
            })
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "containers": containers}


MAX_DIAGNOSTIC_CHARS = 2000
PULL_RETRY_DELAYS = (5, 15, 30)

_NON_TRANSIENT_PULL_PATTERNS = (
    "manifest unknown",
    "unauthorized",
    "denied",
    "authentication required",
    "no basic auth credentials",
    "invalid reference format",
    "no space left on device",
)

_TRANSIENT_PULL_PATTERNS = (
    "connection reset",
    "i/o timeout",
    "tls handshake timeout",
    "context deadline exceeded",
    "unexpected eof",
    "connection refused",
    "broken pipe",
    "temporary failure in name resolution",
    "server misbehaving",
    "network is unreachable",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "too many requests",
)

_TRANSIENT_PULL_REGEXES = (
    re.compile(r"\beof\b"),
    re.compile(r"(?:http status|status(?:\s+code)?[:=]?|\bhttp)\s*5\d\d\b"),
    re.compile(r"\b50[0234]\b"),
)


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _format_step_output(exc, secrets):
    output = "\n".join(part for part in (exc.stderr, exc.stdout) if isinstance(part, str) and part.strip())
    return redact(output.strip(), secrets)[-MAX_DIAGNOSTIC_CHARS:] or "(no output)"


def is_transient_pull_error(output):
    lowered = output.lower()
    if any(pattern in lowered for pattern in _NON_TRANSIENT_PULL_PATTERNS):
        return False
    if any(pattern in lowered for pattern in _TRANSIENT_PULL_PATTERNS):
        return True
    return any(regex.search(lowered) for regex in _TRANSIENT_PULL_REGEXES)


def run_step(run, step, args, secrets, **kwargs):
    """Run a captured registry command; on failure report its exit code and
    output (credentials redacted) before failing closed."""
    try:
        return run(args, check=True, capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        output = _format_step_output(exc, secrets)
        print(f"{step} failed with exit code {exc.returncode}: {output}", file=sys.stderr, flush=True)
        raise


def pull_image_with_retry(run, args, secrets, retry_delays=PULL_RETRY_DELAYS, sleep=time.sleep):
    """Pull an image, retrying boundedly on transient registry/network errors."""
    total_attempts = len(retry_delays) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            output = _format_step_output(exc, secrets)
            if attempt <= len(retry_delays) and is_transient_pull_error(output):
                delay = retry_delays[attempt - 1]
                print(
                    f"docker pull attempt {attempt}/{total_attempts} failed with exit code "
                    f"{exc.returncode} (retrying in {delay}s): {output}",
                    file=sys.stderr,
                    flush=True,
                )
                sleep(delay)
                continue
            print(f"docker pull failed with exit code {exc.returncode}: {output}", file=sys.stderr, flush=True)
            raise


def run_preflight(selection, credentials, run=subprocess.run, sleep=time.sleep):
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
        secrets = [credentials["token"]]
        run_step(
            run, "docker login", [*docker, "login", "ghcr.io", "--username", credentials["username"], "--password-stdin"],
            secrets, input=credentials["token"],
        )
        print("Pulling candidate image", flush=True)
        pull_image_with_retry(run, [*docker, "pull", "--quiet", image], secrets, sleep=sleep)
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
        if selection == f"{allowed_environment} inspect":
            print(json.dumps(inspect_runtime(allowed_environment)))
        else:
            run_preflight(selection, json.loads(sys.stdin.read(16384)))
    except (ValueError, KeyError, IndexError, subprocess.SubprocessError):
        print("Candidate DB preflight FAILED; serving containers were not changed.", file=sys.stderr)
        sys.exit(1)
