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
from .db import admin_session, worker_session

log = logging.getLogger("bruce.access")  # content-free: ids/sources/statuses only, never message text

DEFAULT_ENV = "local"
# The STRICT set of valid deployment environments. BRUCE_ENV must be one of these (or unset -> local).
# A value outside the enum is a fail-CLOSED error, never a silent fallback: an unrecognized env would
# otherwise resolve the WRONG (capability_global_state / relay_control) singleton than the one the
# migration seeded and the operator flipped — divergence that could silently defeat a kill switch.
ENVIRONMENTS = ("local", "staging", "production")
# BRUCE_CONVERSATION_RUNTIME is demoted to a global HARD-OFF only: an explicit off value forces DENY for
# everyone (an extra emergency switch alongside the DB kill). Unset or any other value defers entirely to
# the DB — per-user access comes from an entitlement/enrollment, NOT from this env.
_HARD_OFF = {"0", "false", "no", "off"}


class InvalidEnvironment(ValueError):
    """BRUCE_ENV is set to a value outside the strict ENVIRONMENTS enum (fail closed — no fallback)."""


def current_environment() -> str:
    """The SINGLE environment source, resolved identically here and by the kill CLI / migration seed.

    Unset (or empty) -> ``local`` (the documented default). Any other value is validated against the
    strict ``ENVIRONMENTS`` enum and, if unrecognized, raises ``InvalidEnvironment`` — it NEVER silently
    falls back to ``local`` (a typo'd env must not resolve a different kill-switch singleton than the one
    the operator flipped). Callers in fail-closed paths (``conversation_access``, the relay claim gate)
    turn the raise into a DENY / no-hand-out."""
    raw = os.environ.get("BRUCE_ENV")
    if raw is None or raw.strip() == "":
        return DEFAULT_ENV
    env = raw.strip()
    if env not in ENVIRONMENTS:
        raise InvalidEnvironment(f"BRUCE_ENV={env!r} is not one of {ENVIRONMENTS} (no silent fallback)")
    return env


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
    env = "?"
    try:
        env = current_environment()  # inside the try: an invalid BRUCE_ENV fails CLOSED (DENY), never a crash
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
                    and ent.verified_identity and _availability(ent, capability)):
                return Decision(True, "production", "active verified production entitlement")

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


async def activate_production_entitlement(
        user_id: UUID, *, reason: str | None = None, capability: str = "conversation",
        plan: str = "alpha", actor: str = "system") -> bool:
    """Create or (re)activate a user's PERSISTENT, VERIFIED production entitlement + append an audit row.
    Idempotent by user_id. This is the AUTOMATIC path D1 calls on verified signup — never an operator
    action ("every user needs a grant" must never mean "an operator grants every user"). The
    ``grant-production`` CLI is only a recovery/interim wrapper over this. Returns True if a new row was
    created. Runs in an ``admin_session`` (the entitlement tables are admin-write)."""
    env = current_environment()
    async with admin_session() as s:
        ent = (await s.execute(select(schema.ProductionAccountEntitlement).where(
            schema.ProductionAccountEntitlement.user_id == user_id))).scalar_one_or_none()
        if ent is None:
            s.add(schema.ProductionAccountEntitlement(
                user_id=user_id, account_status="active", plan=plan, messaging_enabled=True,
                verified_identity=True, entitlement_reason=reason, capability_availability=[capability]))
            created = True
        else:
            avail = list(ent.capability_availability or [])
            if capability not in avail:
                avail.append(capability)
            ent.capability_availability = avail
            ent.account_status = "active"
            ent.messaging_enabled = True
            ent.verified_identity = True
            ent.suspended_at = None
            if reason:
                ent.entitlement_reason = reason
            created = False
        await s.flush()   # surface a missing-user FK violation here, inside the audit'd transaction
        s.add(schema.CapabilityAudit(
            actor=actor, action="grant_production", capability=capability, environment=env,
            target_user_id=user_id,
            detail={k: v for k, v in {"reason": reason, "created": created}.items() if v is not None}))
    return created
