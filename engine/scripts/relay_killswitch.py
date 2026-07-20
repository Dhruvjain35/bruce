"""Relay outbound emergency-stop / claim-gate control (operator tool — NOT a public endpoint).

The operator control surface for the self-hosted iMessage relay's outbound CLAIM gate. Run against the
DB (via the Cloud SQL Auth Proxy) with the restricted ``bruce_app`` role, exactly like
``register_relay_device.py`` / ``capability_admin.py``; every mutation runs in a ``worker_session``
(relay_control + relay_devices are worker-only infrastructure) and is recorded append-only in
relay_control_audit with a SERVER-DERIVED actor (shell user@host) — there is deliberately no --actor
flag; a client-supplied actor is never trusted. This is an operator / DB-direct action — there is NO
public token-gated HTTP kill endpoint (a long-lived static bearer in a URL is disallowed; the emergency
admin surface is deferred to a properly step-up-authenticated console).

The pause is enforced AUTHORITATIVELY server-side at ``/v1/relay/outbound/claim`` (no new/reclaimed
message is leased while paused), independent of this CLI. It gates the CLAIM: it does not retract a
message a device already claimed — that in-flight non-send is A2's pre-send re-check
(docs/relay-emergency-stop.md).

    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch pause-all --reason "abuse triage"
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch resume-all
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch pause-device <device-uuid> --reason "..."
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch stop-device <device-uuid> --reason "..."
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch resume-device <device-uuid>
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch status [--stale-seconds 180]
    BRUCE_APP_DATABASE_URL=... python -m scripts.relay_killswitch audit [--limit 50]

The environment is inherited from BRUCE_ENV (validated against the strict enum; default 'local'), the
SAME single source the claim path and the migration seed resolve, so `pause-all` acts on the row the
claim gate reads. Redacted: this prints device ids / names / directives / timestamps / audit states only
— never a device secret, credential, or any message content.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import socket
import uuid

from bruce_engine import relay_control
from bruce_engine.access_control import current_environment

DEFAULT_STALE_SECONDS = 180


def _actor() -> str:
    """Server-derived operator identity (shell user@host). Never a client-supplied string."""
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


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


async def _audit(limit: int) -> None:
    rows = await relay_control.list_audit(limit)
    print(f"relay control-plane audit (most recent {len(rows)}):")
    for r in rows:
        dev = f" device={r.device_id}" if r.device_id else ""
        rsn = f" reason={r.reason}" if r.reason else ""
        print(f"  {r.created_at.isoformat()} {r.action} actor={r.actor} env={r.environment}{dev}{rsn} "
              f"prev={r.previous_state} new={r.new_state}")


async def _run(args: argparse.Namespace) -> None:
    env = current_environment()
    actor = _actor()
    if args.command == "pause-all":
        await relay_control.pause_all(reason=args.reason, actor=actor)
        print(f"GLOBAL outbound PAUSED for env={env}"
              + (f" (reason: {args.reason})" if args.reason else ""))
    elif args.command == "resume-all":
        await relay_control.resume_all(actor=actor)
        print(f"GLOBAL outbound RESUMED for env={env}")
    elif args.command == "pause-device":
        ok = await relay_control.pause_device(uuid.UUID(args.device), reason=args.reason, actor=actor)
        print(f"device {args.device} -> pause_outbound" if ok else f"device {args.device} not found")
    elif args.command == "stop-device":
        ok = await relay_control.set_directive(uuid.UUID(args.device), relay_control.STOP,
                                               reason=args.reason, actor=actor)
        print(f"device {args.device} -> stop" if ok else f"device {args.device} not found")
    elif args.command == "resume-device":
        ok = await relay_control.resume_device(uuid.UUID(args.device), actor=actor)
        print(f"device {args.device} -> run" if ok else f"device {args.device} not found")
    elif args.command == "status":
        await _status(args.stale_seconds)
    elif args.command == "audit":
        await _audit(args.limit)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce relay outbound emergency-stop control (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("pause-all", help="pause the GLOBAL outbound claim gate for the running env")
    pa.add_argument("--reason", default=None, help="reason (audited + recorded on the control row)")

    sub.add_parser("resume-all", help="clear the GLOBAL outbound claim pause for the running env")

    pd = sub.add_parser("pause-device", help="pause a single device's outbound (directive=pause_outbound)")
    pd.add_argument("device", help="relay device_id (UUID)")
    pd.add_argument("--reason", default=None, help="reason (audited + recorded on the device)")

    sd = sub.add_parser("stop-device", help="stop a single device entirely (directive=stop)")
    sd.add_argument("device", help="relay device_id (UUID)")
    sd.add_argument("--reason", default=None, help="reason (audited + recorded on the device)")

    rd = sub.add_parser("resume-device", help="return a single device to normal (directive=run)")
    rd.add_argument("device", help="relay device_id (UUID)")

    st = sub.add_parser("status", help="list devices with last_seen/supervisor_seen/directive + stale flag")
    st.add_argument("--stale-seconds", dest="stale_seconds", type=int, default=DEFAULT_STALE_SECONDS,
                    help=f"supervisor-heartbeat staleness threshold (default {DEFAULT_STALE_SECONDS}s)")

    au = sub.add_parser("audit", help="show recent control-plane audit rows (actor/action/env/states)")
    au.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    return p


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
