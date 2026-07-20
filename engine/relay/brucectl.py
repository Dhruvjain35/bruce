"""brucectl — operator health readout for the relay supervisor (A3).

Reads the supervisor's content-free health status (state / pinned commit / uptime / restart count /
relay pid) and prints it. No secrets, no message content. Run on the dedicated Mac:

    python -m relay.brucectl status [--json]

The status file lives under the relay state dir (BRUCE_RELAY_STATE_DIR, default ~/.bruce-relay).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _status_path() -> str:
    state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
    return os.path.join(state, "supervisor-status.json")


def read_status(path: str | None = None) -> dict | None:
    path = path or _status_path()
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# fields we surface — an explicit allowlist so a status file can never leak an unexpected field.
_FIELDS = ("state", "park_reason", "pinned_commit", "uptime_s", "restart_count", "relay_pid",
           "relay_pgid", "updated_at")

# a status older than this (server clock skew aside) is treated as STALE (supervisor not writing).
STALE_AFTER_S = 60.0


def format_status(status: dict | None, *, now: float | None = None) -> str:
    if status is None:
        return "supervisor: NO STATUS (not running or state dir unset)"
    now = time.time() if now is None else now
    age = now - float(status.get("updated_at", 0) or 0)
    stale = age > STALE_AFTER_S
    lines = [f"supervisor: {status.get('state', '?')}" + ("  [STALE]" if stale else "")]
    for k in _FIELDS:
        if k in status and k != "state":
            lines.append(f"  {k} = {status[k]}")
    lines.append(f"  status_age_s = {round(age, 1)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Relay supervisor health (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="print the supervisor health status")
    st.add_argument("--json", action="store_true", help="raw JSON (still content-free)")
    args = p.parse_args(argv)

    status = read_status()
    if args.command == "status":
        if args.json:
            print(json.dumps(status or {}, indent=2))
        else:
            print(format_status(status))
        return 0 if status else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
