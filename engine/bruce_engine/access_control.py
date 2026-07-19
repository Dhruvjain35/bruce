"""Bite 1.5 keystone — the DB-backed capability access gate.

Replaces per-user Cloud Run env editing. Whether a linked user may use a capability (today only the
conversation runtime) is decided by rows in the DB, never by a string allow-list in an env var and never
by a process the model/relay can bypass. Read in a ``worker_session`` (the service reads across users,
filtered by user_id in the query) against the admin-write / worker-read state tables from migration 0013.

Two concepts, never conflated:
  * ProductionAccountEntitlement — PERSISTENT production access (no expiry).
  * StagingTestEnrollment        — TEMPORARY, internal-only (optional expiry, revoke); NEVER gates prod.

FAIL-CLOSED: any exception anywhere in the check -> DENY.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import worker_session

log = logging.getLogger("bruce.access")  # content-free: ids/sources/statuses only, never message text

DEFAULT_ENV = "local"
# BRUCE_CONVERSATION_RUNTIME is demoted to a global HARD-OFF only: an explicit off value forces DENY for
# everyone (an extra emergency switch alongside the DB kill). Unset or any other value defers entirely to
# the DB — per-user access comes from an entitlement/enrollment, NOT from this env.
_HARD_OFF = {"0", "false", "no", "off"}


def current_environment() -> str:
    """The SINGLE environment source, resolved identically here and by the kill CLI / migration seed."""
    return (os.environ.get("BRUCE_ENV", DEFAULT_ENV) or DEFAULT_ENV).strip() or DEFAULT_ENV


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    source: str   # production | staging | killed | env_off | no_grant | error
    reason: str


def _availability(ent: schema.ProductionAccountEntitlement, capability: str) -> bool:
    avail = ent.capability_availability
    try:
        return capability in (avail or [])   # list membership (also tolerates a dict of capability->cfg)
    except TypeError:
        return False


async def conversation_access(user_id: UUID, capability: str = "conversation") -> Decision:
    """Decide access for (user_id, capability), fail-closed. Order:

    a. resolve ``environment`` from BRUCE_ENV (the single source).
    b. optional global hard-off env -> DENY; then load the (capability, environment) global state and if
       ``killed`` -> DENY (emergency shutdown wins over everything).
    c. REQUIRE a per-user grant regardless of rollout_state (rolled_out must NEVER mass-enable here):
       an active ProductionAccountEntitlement -> ALLOW(production); else a live StagingTestEnrollment ->
       ALLOW(staging); else DENY.
    """
    env = current_environment()
    try:
        raw = os.environ.get("BRUCE_CONVERSATION_RUNTIME")
        if raw is not None and raw.strip().lower() in _HARD_OFF:
            return Decision(False, "env_off", "BRUCE_CONVERSATION_RUNTIME is explicitly off (global hard-off)")

        async with worker_session() as s:
            gs = (await s.execute(select(schema.CapabilityGlobalState).where(
                schema.CapabilityGlobalState.capability == capability,
                schema.CapabilityGlobalState.environment == env))).scalar_one_or_none()
            if gs is not None and gs.killed:
                return Decision(False, "killed", "global kill switch is on for this capability")

            # PRODUCTION (persistent): active + messaging_enabled + capability available.
            ent = (await s.execute(select(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == user_id))).scalar_one_or_none()
            if (ent is not None and ent.account_status == "active" and ent.messaging_enabled
                    and _availability(ent, capability)):
                return Decision(True, "production", "active production entitlement")

            # STAGING (temporary, internal): live means not revoked and not past expiry.
            now = datetime.datetime.now(datetime.timezone.utc)
            enrollments = (await s.execute(select(schema.StagingTestEnrollment).where(
                schema.StagingTestEnrollment.user_id == user_id,
                schema.StagingTestEnrollment.capability == capability,
                schema.StagingTestEnrollment.environment == env,
                schema.StagingTestEnrollment.revoked_at.is_(None)))).scalars().all()
            for e in enrollments:
                if e.expires_at is None or e.expires_at > now:
                    return Decision(True, "staging", "live staging enrollment")

            return Decision(False, "no_grant", "no active entitlement or live staging enrollment")
    except Exception:
        log.warning("conversation_access_error env=%s", env)  # content-free; no user text
        return Decision(False, "error", "access check failed (fail-closed)")
