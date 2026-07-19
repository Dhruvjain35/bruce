"""Capability access administration (operator tool — NOT a public endpoint).

The DB-direct replacement for per-user Cloud Run env editing. Run against the DB (via the Cloud SQL Auth
Proxy) with the restricted ``bruce_app`` role, exactly like ``register_relay_device.py``; every mutation
runs in an ``admin_session`` (transaction-local ``app.admin='on'``) so the capability-access RLS policies
admit the write. Every mutation appends a CapabilityAudit row whose actor is SERVER-DERIVED (the operating
shell user@host) — there is deliberately no --actor flag; a client-supplied actor is never trusted.

Two distinct concepts (do NOT conflate):
  * grant-production — PERSISTENT production access (no expiry). Access persists until an explicit
    unlink/delete/suspend/entitlement-end/abuse-block/global-kill.
  * enroll-staging   — TEMPORARY, internal-only enrollment (optional --hours TTL, immediate revoke).
    NEVER gates production.

    BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin grant-production --user <uuid> [--reason ...]
    BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin enroll-staging  --user <uuid> [--hours 24] [--reason ...]
    BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin revoke          --user <uuid>
    BRUCE_ENV=staging BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin kill --on   # or --off
    BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin list
    BRUCE_APP_DATABASE_URL=... python -m scripts.capability_admin audit [--limit 50]

The environment is inherited from BRUCE_ENV (default 'local'), never a hardcoded default — the same single
source the runtime gate resolves, so `kill` and `enroll-staging` act on the row the gate reads.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import getpass
import os
import socket
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bruce_engine import access_control, schema
from bruce_engine.db import admin_session

CAPABILITY = "conversation"


def _actor() -> str:
    """Server-derived operator identity (shell user@host). Never a client-supplied string."""
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


async def _write_audit(s, *, action: str, environment: str, target_user_id, detail: dict, actor: str,
                       capability: str = CAPABILITY) -> None:
    """Append one audit row (redacted detail — never a secret)."""
    s.add(schema.CapabilityAudit(
        actor=actor, action=action, capability=capability, environment=environment,
        target_user_id=target_user_id, detail={k: v for k, v in (detail or {}).items() if v is not None}))


async def grant_production(user_id: uuid.UUID, *, reason: str | None = None,
                           capability: str = CAPABILITY, actor: str | None = None) -> bool:
    """RECOVERY / INTERIM admin tool only. The NORMAL path is D1 auto-calling
    access_control.activate_production_entitlement() on verified signup (no operator). Thin wrapper over
    the canonical function so there is ONE create/activate path. Returns True if a new row was created."""
    return await access_control.activate_production_entitlement(
        user_id, reason=reason, capability=capability, actor=actor or _actor())


async def enroll_staging(user_id: uuid.UUID, *, hours: int | None = None, reason: str | None = None,
                         capability: str = CAPABILITY, actor: str | None = None) -> datetime.datetime | None:
    """Add a TEMPORARY staging enrollment (internal only). Returns the expiry (None => no expiry)."""
    actor = actor or _actor()
    env = access_control.current_environment()
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(hours=hours) if hours else None
    async with admin_session() as s:
        s.add(schema.StagingTestEnrollment(
            user_id=user_id, capability=capability, environment=env, enabled_at=now,
            expires_at=expires_at, enabled_by=actor, audit_reason=reason))
        await s.flush()
        await _write_audit(s, action="enroll_staging", environment=env, target_user_id=user_id,
                           detail={"reason": reason, "expires_at": expires_at.isoformat() if expires_at else None},
                           actor=actor, capability=capability)
    return expires_at


async def revoke(user_id: uuid.UUID, *, capability: str = CAPABILITY, actor: str | None = None) -> int:
    """Immediately revoke every live staging enrollment for the user (production is untouched — it is
    persistent). Returns the number of enrollments revoked."""
    actor = actor or _actor()
    env = access_control.current_environment()
    now = datetime.datetime.now(datetime.timezone.utc)
    async with admin_session() as s:
        res = await s.execute(update(schema.StagingTestEnrollment).where(
            schema.StagingTestEnrollment.user_id == user_id,
            schema.StagingTestEnrollment.capability == capability,
            schema.StagingTestEnrollment.environment == env,
            schema.StagingTestEnrollment.revoked_at.is_(None)).values(revoked_at=now))
        await _write_audit(s, action="revoke_staging", environment=env, target_user_id=user_id,
                           detail={"revoked_count": res.rowcount}, actor=actor, capability=capability)
    return res.rowcount


async def set_kill(on: bool, *, capability: str = CAPABILITY, actor: str | None = None) -> None:
    """UPSERT the global kill switch for (capability, BRUCE_ENV). Works with NO pre-existing state row."""
    actor = actor or _actor()
    env = access_control.current_environment()
    now = datetime.datetime.now(datetime.timezone.utc)
    async with admin_session() as s:
        stmt = pg_insert(schema.CapabilityGlobalState).values(
            capability=capability, environment=env, rollout_state="default_off", killed=on, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["capability", "environment"], set_={"killed": on, "updated_at": now})
        await s.execute(stmt)
        await _write_audit(s, action="kill_on" if on else "kill_off", environment=env,
                           target_user_id=None, detail={}, actor=actor, capability=capability)


async def list_state(*, capability: str = CAPABILITY):
    env = access_control.current_environment()
    async with admin_session() as s:
        gs = (await s.execute(select(schema.CapabilityGlobalState).where(
            schema.CapabilityGlobalState.capability == capability,
            schema.CapabilityGlobalState.environment == env))).scalar_one_or_none()
        ents = (await s.execute(select(schema.ProductionAccountEntitlement))).scalars().all()
        enrs = (await s.execute(select(schema.StagingTestEnrollment).where(
            schema.StagingTestEnrollment.capability == capability,
            schema.StagingTestEnrollment.environment == env))).scalars().all()
    return gs, ents, enrs


async def list_audit(*, limit: int = 50):
    async with admin_session() as s:
        rows = (await s.execute(select(schema.CapabilityAudit).order_by(
            schema.CapabilityAudit.created_at.desc()).limit(limit))).scalars().all()
    return rows


# --------------------------------------------------------------------------- CLI


async def _run(args: argparse.Namespace) -> None:
    env = access_control.current_environment()
    if args.command == "grant-production":
        created = await grant_production(uuid.UUID(args.user), reason=args.reason)
        print(f"production entitlement {'created' if created else 'updated'} (persistent) for {args.user} "
              f"[capability={CAPABILITY}]")
    elif args.command == "enroll-staging":
        expires = await enroll_staging(uuid.UUID(args.user), hours=args.hours, reason=args.reason)
        print(f"staging enrollment added for {args.user} in env={env} "
              f"[expires={expires.isoformat() if expires else 'never'}]")
    elif args.command == "revoke":
        n = await revoke(uuid.UUID(args.user))
        print(f"revoked {n} live staging enrollment(s) for {args.user} in env={env} "
              f"(production access, if any, is persistent and untouched)")
    elif args.command == "kill":
        await set_kill(args.on)
        print(f"global kill for capability={CAPABILITY} env={env} -> {'ON' if args.on else 'OFF'}")
    elif args.command == "list":
        gs, ents, enrs = await list_state()
        print(f"env={env} capability={CAPABILITY}")
        print(f"  global: rollout_state={getattr(gs, 'rollout_state', 'default_off')} "
              f"killed={getattr(gs, 'killed', False)}")
        print(f"  production entitlements ({len(ents)}):")
        for e in ents:
            print(f"    user={e.user_id} status={e.account_status} messaging={e.messaging_enabled} "
                  f"caps={e.capability_availability}")
        now = datetime.datetime.now(datetime.timezone.utc)
        print(f"  staging enrollments ({len(enrs)}):")
        for e in enrs:
            live = e.revoked_at is None and (e.expires_at is None or e.expires_at > now)
            print(f"    user={e.user_id} live={live} expires={e.expires_at.isoformat() if e.expires_at else 'never'} "
                  f"revoked={'yes' if e.revoked_at else 'no'}")
    elif args.command == "audit":
        rows = await list_audit(limit=args.limit)
        print(f"capability audit (most recent {len(rows)}):")
        for r in rows:
            print(f"  {r.created_at.isoformat()} {r.action} actor={r.actor} env={r.environment} "
                  f"target={r.target_user_id} detail={r.detail}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce capability access administration (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("grant-production", help="create/activate a PERSISTENT production entitlement")
    g.add_argument("--user", required=True, help="Bruce user_id (UUID)")
    g.add_argument("--reason", default=None, help="entitlement reason (audited)")

    e = sub.add_parser("enroll-staging", help="add a TEMPORARY internal staging enrollment")
    e.add_argument("--user", required=True, help="Bruce user_id (UUID)")
    e.add_argument("--hours", type=int, default=None, help="TTL in hours (omit => no expiry)")
    e.add_argument("--reason", default=None, help="enrollment reason (audited)")

    r = sub.add_parser("revoke", help="immediately revoke a user's live staging enrollments")
    r.add_argument("--user", required=True, help="Bruce user_id (UUID)")

    k = sub.add_parser("kill", help="UPSERT the global kill switch for (capability, BRUCE_ENV)")
    kg = k.add_mutually_exclusive_group(required=True)
    kg.add_argument("--on", dest="on", action="store_true", help="enable the global kill (DENY all)")
    kg.add_argument("--off", dest="on", action="store_false", help="disable the global kill")

    sub.add_parser("list", help="show global state, production entitlements, staging enrollments")

    a = sub.add_parser("audit", help="show recent capability audit rows")
    a.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    return p


if __name__ == "__main__":
    asyncio.run(_run(_build_parser().parse_args()))
