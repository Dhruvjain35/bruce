"""Relay outbound kill switch (operator tool — NOT a public endpoint).

The AUTHORITATIVE outbound kill for the self-hosted iMessage relay. Run against the DB (via the Cloud
SQL Auth Proxy) with the restricted ``bruce_app`` role, exactly like ``register_relay_device.py`` /
``capability_admin.py``; every mutation runs in a ``worker_session`` (relay_control + relay_devices are
worker-only infrastructure). This is deliberately an operator / DB-direct action — there is NO public
token-gated HTTP kill endpoint (a long-lived static bearer in a URL is disallowed; the emergency admin
surface is deferred to a properly step-up-authenticated console). The kill is ALSO enforced server-side
at ``/v1/relay/outbound/claim`` regardless of this CLI, so a paused device is never handed a message.

    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch pause-all --reason "abuse triage"
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch resume-all
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch pause-device <device-uuid> --reason "..."
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch resume-device <device-uuid>
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch status [--stale-seconds 180]

The environment is inherited from BRUCE_ENV (default 'local'), the SAME single source the claim path and
the migration seed resolve, so `pause-all` acts on the row the claim gate reads. Redacted: this prints
device ids / names / directives / timestamps only — never a device secret or any message content.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from bruce_engine import relay_control
from bruce_engine.access_control import current_environment

DEFAULT_STALE_SECONDS = 180


def _fmt(ts) -> str:
    return ts.isoformat() if ts is not None else "never"


async def _status(stale_seconds: int) -> None:
    env = current_environment()
    paused, reason = await relay_control.global_state()
    devices = await relay_control.list_devices()
    stale_ids = {d.id for d in await relay_control.stale_devices(stale_seconds)}
    print(f"env={env}")
    print(f"  global outbound: {'PAUSED' if paused else 'running'}"
          + (f" (reason: {reason})" if reason else ""))
    print(f"  devices ({len(devices)}); stale threshold = {stale_seconds}s:")
    for d in devices:
        flags = []
        if d.revoked_at is not None:
            flags.append("REVOKED")
        if d.id in stale_ids:
            flags.append("STALE")
        if d.outbound_paused:
            flags.append("paused")
        flag_s = (" [" + ",".join(flags) + "]") if flags else ""
        print(f"    {d.id} name={d.name} directive={d.directive}{flag_s}")
        print(f"        last_seen={_fmt(d.last_seen_at)} supervisor_seen={_fmt(d.supervisor_seen_at)} "
              f"agent_commit={d.agent_commit or '-'}"
              + (f" paused_reason={d.paused_reason}" if d.paused_reason else ""))


async def _run(args: argparse.Namespace) -> None:
    env = current_environment()
    if args.command == "pause-all":
        await relay_control.pause_all(reason=args.reason)
        print(f"GLOBAL outbound PAUSED for env={env}"
              + (f" (reason: {args.reason})" if args.reason else ""))
    elif args.command == "resume-all":
        await relay_control.resume_all()
        print(f"GLOBAL outbound RESUMED for env={env}")
    elif args.command == "pause-device":
        ok = await relay_control.pause_device(uuid.UUID(args.device), reason=args.reason)
        print(f"device {args.device} -> pause_outbound" if ok else f"device {args.device} not found")
    elif args.command == "resume-device":
        ok = await relay_control.resume_device(uuid.UUID(args.device))
        print(f"device {args.device} -> run" if ok else f"device {args.device} not found")
    elif args.command == "status":
        await _status(args.stale_seconds)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce relay outbound kill switch (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("pause-all", help="trip the GLOBAL outbound kill for the running env")
    pa.add_argument("--reason", default=None, help="reason (recorded on the control row)")

    sub.add_parser("resume-all", help="clear the GLOBAL outbound kill for the running env")

    pd = sub.add_parser("pause-device", help="pause a single device's outbound (directive=pause_outbound)")
    pd.add_argument("device", help="relay device_id (UUID)")
    pd.add_argument("--reason", default=None, help="reason (recorded on the device)")

    rd = sub.add_parser("resume-device", help="return a single device to normal (directive=run)")
    rd.add_argument("device", help="relay device_id (UUID)")

    st = sub.add_parser("status", help="list devices with last_seen/supervisor_seen/directive + stale flag")
    st.add_argument("--stale-seconds", dest="stale_seconds", type=int, default=DEFAULT_STALE_SECONDS,
                    help=f"supervisor-heartbeat staleness threshold (default {DEFAULT_STALE_SECONDS}s)")
    return p


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
