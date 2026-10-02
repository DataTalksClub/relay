"""Load a transfer export into this host. Prints counts only.

The archive directory is the one written by the export: manifest.json plus one
JSON document per resource. Pages stay under the API's upload limit.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

PAGE = 200
HOST = "127.0.0.1"
PORT = 8000


def _token():
    for line in Path("/etc/relay/runtime.env").read_text().splitlines():
        if line.startswith("RELAY_TRANSFER_TOKEN="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise SystemExit("RELAY_TRANSFER_TOKEN is unset")


def _host_header():
    for path in ("/etc/relay/infrastructure.env", "/etc/relay/runtime.env"):
        file = Path(path)
        if not file.exists():
            continue
        for line in file.read_text().splitlines():
            if line.startswith("ALLOWED_HOSTS="):
                for name in line.split("=", 1)[1].split(","):
                    name = name.strip()
                    if name and not name[0].isdigit() and name not in {"localhost"}:
                        return name
    return "relay.datatalks.club"


def _post(token, host_header, payload):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://{HOST}:{PORT}/internal/transfer/load",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Host": host_header,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{payload['resource']} failed: {exc.code} {detail}") from exc


def main():
    root = Path("/var/lib/relay/export/transfer")
    manifest = json.loads((root / "manifest.json").read_text())
    token = _token()
    host_header = _host_header()
    for item in manifest["resources"]:
        name = item["name"]
        rows = json.loads((root / f"{name}.json").read_text())["rows"]
        loaded = 0
        for offset in range(0, len(rows), PAGE):
            result = _post(
                token,
                host_header,
                {"version": manifest["version"], "resource": name, "rows": rows[offset : offset + PAGE]},
            )
            loaded += result["upserted"]
        print(f"{name} upserted={loaded} expected={item['count']}", flush=True)
        if loaded != item["count"]:
            raise SystemExit(f"{name} upserted {loaded}, expected {item['count']}")
    print("load_done", flush=True)


if __name__ == "__main__":
    main()
