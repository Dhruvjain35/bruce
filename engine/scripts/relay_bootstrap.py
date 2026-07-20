"""Mint a relay device BOOTSTRAP token (operator tool — NOT a public endpoint).

The one authorized, operator-authenticated step in the A4 install: mint a SHORT-LIVED, SINGLE-USE token
bound to (BRUCE_ENV, device name). The installer uses it once to register the device over TLS; the
PERMANENT credential the backend returns goes straight into the Mac Keychain and is never shown. This
token is the temporary bootstrap material — it expires quickly and is consumed on first use.

    BRUCE_ENV=staging BRUCE_APP_DATABASE_URL=... python -m scripts.relay_bootstrap mint --device mac-alpha --ttl 600

Prints the bootstrap token ONCE. Redacted otherwise; never prints a permanent device credential.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import socket

from bruce_engine import relay_auth
from bruce_engine.access_control import current_environment


def _actor() -> str:
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


async def _run(args: argparse.Namespace) -> None:
    env = current_environment()
    if args.command == "mint":
        token = await relay_auth.mint_bootstrap_token(
            args.device, environment=env, ttl_seconds=args.ttl, actor=_actor(), max_uses=1)
        print(f"bootstrap token for device={args.device} env={env} (single-use, expires in {args.ttl}s):")
        print(f"  {token}")
        print("Hand this to install_relay.sh (BRUCE_RELAY_BOOTSTRAP_TOKEN). It is consumed on first use.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce relay device bootstrap (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mint", help="mint a short-lived single-use device-registration token")
    m.add_argument("--device", required=True, help="intended relay device name (bound into the token)")
    m.add_argument("--ttl", type=int, default=600, help="token lifetime in seconds (default 600)")
    return p


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
