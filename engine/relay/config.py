"""Relay configuration + rotating credential resolution. Runs on the dedicated Mac only.

The device secret lives in the macOS Keychain (rotating; server stores only its hash). We NEVER read
a cloud/DB/OpenAI key here — the relay is transport only. Everything else (API base URL, spool dir,
checkpoint path, poll cadence) comes from the environment so nothing sensitive is hard-coded.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

KEYCHAIN_SERVICE = "com.bruce.relay.device-secret"


class ConfigError(Exception):
    """The relay is misconfigured (missing base URL or credential). Refuse to start."""


def _keychain_secret(account: str) -> str | None:
    """Read the rotating device secret from the login Keychain via `security` (macOS only)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def resolve_secret(account: str = "default") -> str:
    """Keychain first (production Mac), then BRUCE_RELAY_SECRET (dev only). Never logged."""
    secret = _keychain_secret(account) or os.environ.get("BRUCE_RELAY_SECRET")
    if not secret:
        raise ConfigError("no relay device secret (Keychain or BRUCE_RELAY_SECRET)")
    return secret


@dataclass(frozen=True)
class RelayConfig:
    base_url: str
    secret: str
    spool_dir: str
    checkpoint_path: str
    imsg_bin: str
    poll_interval: float
    reconnect_delay: float

    @classmethod
    def from_env(cls) -> "RelayConfig":
        base_url = os.environ.get("BRUCE_API_BASE_URL")
        if not base_url:
            raise ConfigError("BRUCE_API_BASE_URL is required")
        if not base_url.startswith("https://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
            raise ConfigError("BRUCE_API_BASE_URL must be https (TLS is mandatory)")
        state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
        return cls(
            base_url=base_url,
            secret=resolve_secret(os.environ.get("BRUCE_RELAY_ACCOUNT", "default")),
            spool_dir=os.path.join(state, "spool"),
            checkpoint_path=os.path.join(state, "checkpoint.json"),
            imsg_bin=os.environ.get("BRUCE_IMSG_BIN", "imsg"),
            poll_interval=float(os.environ.get("BRUCE_RELAY_POLL_INTERVAL", "2.0")),
            reconnect_delay=float(os.environ.get("BRUCE_RELAY_RECONNECT_DELAY", "3.0")),
        )
