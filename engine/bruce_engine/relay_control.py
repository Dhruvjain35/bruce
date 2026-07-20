"""Bite 1.5 A1 — relay control plane (server-side outbound CLAIM gate + per-device directives).

This is the AUTHORITATIVE, server-side gate on NEW outbound CLAIMS for the self-hosted iMessage relay.
It is enforced in the claim path — never in the relay client — so no client can bypass it. A directive
is one of:

  * run            — normal operation; the relay may claim + send.
  * pause_outbound — the relay must NOT send (inbound still flows); the claim path hands out nothing.
  * stop           — the relay should stop entirely; the claim path also hands out nothing.

The effective directive is the STRONGER of the per-device directive/pause columns and the global
``relay_control.outbound_paused`` switch for the running environment (``BRUCE_ENV``). Everything runs in
``worker_session()`` — relay_control and relay_devices are worker-only infrastructure, never user-owned.

SCOPE / EMERGENCY-STOP SEMANTICS (full detail in docs/relay-emergency-stop.md). This gates the CLAIM,
authoritatively: while paused/stopped, NO new or reclaimed message is ever leased to a device. It does
NOT retract a message a device already claimed before the pause — a distributed system cannot recall
bytes already handed to iMessage. Preventing that in-flight send is A2's job (a pre-send directive
re-check that fails closed). What A1 DOES guarantee for an already-claimed-but-unsent message: while
paused it is never re-handed out (no duplicate delivery), and on resume it is reclaimable exactly once.

Every control-plane CHANGE (pause/resume/stop, global or per-device) is recorded append-only in
relay_control_audit with actor / action / environment / device / reason / previous+new state / timestamp.

This module is CONTENT-FREE: it records device telemetry (a pinned relay commit, liveness timestamps),
pause/kill state, and operator-supplied reasons. It NEVER stores or logs message content, handles, chat
ids, file paths, or credentials.
"""

from __future__ import annotations

import dataclasses
import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema
from .access_control import current_environment
from .db import worker_session

# Directive vocabulary (also the heartbeat / claim contract the relay learns).
RUN = "run"
PAUSE_OUTBOUND = "pause_outbound"
STOP = "stop"
DIRECTIVES = (RUN, PAUSE_OUTBOUND, STOP)
# Directives under which the claim path MUST hand out nothing (a paused/stopped device never sends).
BLOCKED = (PAUSE_OUTBOUND, STOP)


@dataclasses.dataclass
class DeviceStatus:
    """Content-free per-device control/telemetry snapshot for the operator CLI."""

    id: UUID
    name: str
    directive: str
    outbound_paused: bool
    paused_reason: str | None
    last_seen_at: datetime.datetime | None
    supervisor_seen_at: datetime.datetime | None
    agent_commit: str | None
    revoked_at: datetime.datetime | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _resolve(directive: str, device_paused: bool, global_paused: bool) -> str:
    """Pure precedence: ``stop`` wins; then any pause (per-device directive OR flag OR the global
    switch) -> ``pause_outbound``; otherwise ``run``."""
    if directive == STOP:
        return STOP
    if directive == PAUSE_OUTBOUND or device_paused or global_paused:
        return PAUSE_OUTBOUND
    return RUN


def _clean_commit(value) -> str | None:
    """Content-free guard on the reported commit: a git hash / short ref only, capped — never free text
    (drop anything with whitespace so a device can't smuggle message content through this field)."""
    if not value:
        return None
    s = str(value).strip()
    if not s or any(c.isspace() for c in s):
        return None
    return s[:64]


async def _global_paused(s) -> bool:
    """True iff the singleton relay_control row for the running env has outbound_paused set."""
    row = (await s.execute(select(schema.RelayControl).where(
        schema.RelayControl.environment == current_environment()))).scalar_one_or_none()
    return bool(row and row.outbound_paused)


async def get_directive(device: schema.RelayDevice) -> str:
    """Resolve the effective directive for ``device`` from the AUTHORITATIVE DB state — a FRESH read of
    the per-device columns AND the global relay_control switch (not the possibly-stale row handed in).
    Returns run|pause_outbound|stop; the claim path short-circuits on pause_outbound/stop."""
    async with worker_session() as s:
        dev = (await s.execute(select(schema.RelayDevice).where(
            schema.RelayDevice.id == device.id))).scalar_one_or_none()
        directive = (getattr(dev, "directive", None) or RUN)
        device_paused = bool(getattr(dev, "outbound_paused", False))
        global_paused = await _global_paused(s)
    return _resolve(directive, device_paused, global_paused)


async def record_heartbeat(device: schema.RelayDevice, *, status: dict) -> str:
    """Stamp liveness (last_seen_at + supervisor_seen_at) and the CONTENT-FREE supervisor telemetry
    (agent_commit), then return the current effective directive so the relay/supervisor learns whether
    to keep sending, pause outbound, or stop. ``status`` may carry agent_commit / uptime / restart_count
    — only agent_commit is persisted (sanitized to a commit hash); the rest is liveness noise and is
    NEVER stored. Message content / handles / paths must never appear in ``status``."""
    now = _now()
    commit = _clean_commit((status or {}).get("agent_commit"))
    async with worker_session() as s:
        dev = (await s.execute(select(schema.RelayDevice).where(
            schema.RelayDevice.id == device.id))).scalar_one_or_none()
        if dev is not None:
            dev.last_seen_at = now
            dev.supervisor_seen_at = now
            if commit is not None:
                dev.agent_commit = commit
            return _resolve(dev.directive or RUN, bool(dev.outbound_paused), await _global_paused(s))
        # Device vanished between auth and here (unexpected): still honor the global kill.
        return PAUSE_OUTBOUND if await _global_paused(s) else RUN


# action label per resulting per-device directive (audited on every change).
_DEVICE_ACTION = {RUN: "resume_device", PAUSE_OUTBOUND: "pause_device", STOP: "stop_device"}


def _audit(s, *, actor: str | None, action: str, environment: str, device_id: UUID | None,
           reason: str | None, previous: dict, new: dict) -> None:
    """Append one CONTENT-FREE control-plane audit row within the caller's transaction (atomic with the
    mutation). previous/new carry only directive/pause booleans — never payload content."""
    s.add(schema.RelayControlAudit(
        actor=actor, action=action, environment=environment, device_id=device_id,
        reason=(reason or None), previous_state=previous, new_state=new))


async def pause_all(reason: str | None = None, *, actor: str | None = None) -> None:
    """Trip the GLOBAL outbound-claim pause for the running env (UPSERT; works with no pre-existing row).
    Audited: actor / reason / environment / previous+new state / timestamp."""
    env = current_environment()
    now = _now()
    async with worker_session() as s:
        prev = await _global_paused(s)
        stmt = pg_insert(schema.RelayControl).values(
            environment=env, outbound_paused=True, reason=(reason or None), updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["environment"],
            set_={"outbound_paused": True, "reason": (reason or None), "updated_at": now})
        await s.execute(stmt)
        _audit(s, actor=actor, action="pause_all", environment=env, device_id=None, reason=reason,
               previous={"outbound_paused": prev}, new={"outbound_paused": True})


async def resume_all(*, actor: str | None = None) -> None:
    """Clear the GLOBAL outbound-claim pause for the running env (UPSERT; clears reason). Audited."""
    env = current_environment()
    now = _now()
    async with worker_session() as s:
        prev = await _global_paused(s)
        stmt = pg_insert(schema.RelayControl).values(
            environment=env, outbound_paused=False, reason=None, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["environment"],
            set_={"outbound_paused": False, "reason": None, "updated_at": now})
        await s.execute(stmt)
        _audit(s, actor=actor, action="resume_all", environment=env, device_id=None, reason=None,
               previous={"outbound_paused": prev}, new={"outbound_paused": False})


async def pause_device(device_id: UUID, reason: str | None = None, *, actor: str | None = None) -> bool:
    """Pause a single device's outbound (directive=pause_outbound). Returns False if unknown."""
    return await set_directive(device_id, PAUSE_OUTBOUND, reason=reason, actor=actor)


async def resume_device(device_id: UUID, *, actor: str | None = None) -> bool:
    """Return a single device to normal operation (directive=run). Returns False if unknown."""
    return await set_directive(device_id, RUN, actor=actor)


async def set_directive(device_id: UUID, directive: str, *, reason: str | None = None,
                        actor: str | None = None) -> bool:
    """Set a device's directive (run|pause_outbound|stop) and its resolved pause state. Returns False if
    the device is unknown; raises ValueError on an invalid directive. Audited: actor / action / env /
    device / reason / previous+new directive / timestamp."""
    if directive not in DIRECTIVES:
        raise ValueError(f"invalid directive: {directive!r}")
    env = current_environment()
    async with worker_session() as s:
        dev = (await s.execute(select(schema.RelayDevice).where(
            schema.RelayDevice.id == device_id))).scalar_one_or_none()
        if dev is None:
            return False
        previous = {"directive": dev.directive, "outbound_paused": dev.outbound_paused}
        dev.directive = directive
        if directive == RUN:
            dev.outbound_paused = False
            dev.paused_reason = None
            dev.paused_at = None
        else:  # pause_outbound OR stop -> not sending
            dev.outbound_paused = True
            dev.paused_reason = (reason or None)
            dev.paused_at = _now()
        _audit(s, actor=actor, action=_DEVICE_ACTION[directive], environment=env, device_id=device_id,
               reason=reason, previous=previous,
               new={"directive": directive, "outbound_paused": dev.outbound_paused})
        return True


async def stale_devices(threshold_s: int) -> list[DeviceStatus]:
    """Non-revoked devices whose supervisor_seen_at is older than ``threshold_s`` (or never reported),
    for alerting. Content-free records, not message data."""
    cutoff = _now() - datetime.timedelta(seconds=threshold_s)
    async with worker_session() as s:
        rows = (await s.execute(select(schema.RelayDevice).where(
            schema.RelayDevice.revoked_at.is_(None),
            or_(schema.RelayDevice.supervisor_seen_at.is_(None),
                schema.RelayDevice.supervisor_seen_at < cutoff),
        ).order_by(schema.RelayDevice.created_at))).scalars().all()
        return [_to_status(d) for d in rows]


async def list_devices() -> list[DeviceStatus]:
    """All relay devices as content-free status records (operator CLI)."""
    async with worker_session() as s:
        rows = (await s.execute(select(schema.RelayDevice).order_by(
            schema.RelayDevice.created_at))).scalars().all()
        return [_to_status(d) for d in rows]


async def global_state() -> tuple[bool, str | None]:
    """(outbound_paused, reason) for the running env's global switch (False/None when unset)."""
    async with worker_session() as s:
        row = (await s.execute(select(schema.RelayControl).where(
            schema.RelayControl.environment == current_environment()))).scalar_one_or_none()
        return (bool(row and row.outbound_paused), row.reason if row else None)


async def list_audit(limit: int = 50) -> list[schema.RelayControlAudit]:
    """Most-recent control-plane audit rows (operator CLI). Content-free (states are booleans/directives)."""
    async with worker_session() as s:
        return list((await s.execute(select(schema.RelayControlAudit).order_by(
            schema.RelayControlAudit.created_at.desc()).limit(limit))).scalars().all())


def _to_status(d: schema.RelayDevice) -> DeviceStatus:
    return DeviceStatus(
        id=d.id, name=d.name, directive=d.directive, outbound_paused=d.outbound_paused,
        paused_reason=d.paused_reason, last_seen_at=d.last_seen_at,
        supervisor_seen_at=d.supervisor_seen_at, agent_commit=d.agent_commit, revoked_at=d.revoked_at)
