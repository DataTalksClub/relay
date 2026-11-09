"""Relay operator login using DataTalksClub's shared device flow."""

from __future__ import annotations

import getpass
import importlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config


class LoginError(Exception):
    """A device login could not be completed."""


def _shared_device_login():
    """Load the shared polling implementation, as dapier does."""
    try:
        return importlib.import_module("dtc_device_login")
    except ImportError:
        pass
    roots = []
    if os.environ.get("DTC_AUTH_HOME"):
        roots.append(Path(os.environ["DTC_AUTH_HOME"]))
    here = Path(__file__).resolve()
    for parent in here.parents:
        roots.extend((parent / "auth", parent.parent / "auth"))
        if parent.name == "git" or parent == Path("/"):
            break
    for root in roots:
        if (root / "dtc_device_login" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            return importlib.import_module("dtc_device_login")
    raise LoginError(
        "Shared device login was not found. Install DataTalksClub/auth or set DTC_AUTH_HOME."
    )


def _post_json(url: str, body: dict, *, bearer: str = "", timeout: float = 20.0):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                body = json.loads(response.read() or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise LoginError("Relay returned an invalid JSON response during device login.") from None
            return response.status, body
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        return exc.code, data
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LoginError(f"Relay login request timed out after {timeout:g} seconds.") from exc
        raise LoginError(f"Could not reach Relay at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LoginError(f"Relay login request timed out after {timeout:g} seconds.") from exc


def login(base_url: str, *, timeout: int = 900, sleeper=time.sleep, poster=None, label=None) -> str:
    """Run the shared device flow and persist the issued Relay admin token."""
    flow = _shared_device_login()
    try:
        result = flow.device_login(
            base_url,
            poster or _post_json,
            start_body={"label": label or _machine_label()},
            start_path="/api/agent/device/start",
            token_path="/api/agent/device/token",
            sleeper=sleeper,
            clock=time.time,
            timeout=timeout,
        )
    except flow.DeviceLoginError as exc:
        raise LoginError(str(exc)) from None
    token = result.get("token") if isinstance(result, dict) else None
    if not token:
        raise LoginError("Relay returned an empty device session.")
    try:
        config.save_file({"url": base_url.rstrip("/"), "api_key": token})
    except OSError as exc:
        raise LoginError(f"Could not save the Relay credential to {config.config_path()}: {exc}") from exc
    return token


def logout():
    """Forget the locally stored CLI credential."""
    settings = config.resolve(None)
    revoked = True
    if settings.base_url and settings.api_key.startswith("relay_admin_"):
        try:
            status, result = _post_json(
                f"{settings.base_url}/api/agent/device/revoke", {}, bearer=settings.api_key, timeout=10
            )
            revoked = status == 200 and isinstance(result, dict) and result.get("revoked") is True
        except LoginError:
            # Logging out locally must still work when Relay cannot be reached.
            revoked = False
    try:
        config.save_file({"api_key": ""})
    except OSError as exc:
        raise LoginError(f"Could not update the Relay config file {config.config_path()}: {exc}") from exc
    return revoked


def _machine_label() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    return f"{user}@{host}".strip("@") or "relay CLI"
