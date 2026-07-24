"""ToolBroker (G0.3 + Phase B) — the SINGLE tool-selection + capability-truth authority.

Phase A made the router authoritative; Phase B makes the broker the ONE place that answers "which tool, and
can this user actually call it right now?" — so no production path re-derives capability truth on its own.
It sits between "what the user wants" (domain + action + the router's candidate capabilities) and "what Bruce
can actually call" (the ToolRegistry joined with THIS user's connection + granted scopes), and:

  * shortlists the FEW relevant live tools (never the whole registry), each carrying its compact arg schema
    + required scopes so a planner fills args from the schema, not ad-hoc handler knowledge;
  * distinguishes UNSUPPORTED (not live yet) from DISCONNECTED (live, not connected) from INSUFFICIENT_SCOPE
    (connected, but the grant is missing the scope) — three different honest replies;
  * exposes availability() as the single capability-truth check and select() as the single selection entry
    point, both deterministic and model-free.

A kill switch (BRUCE_BROKER_AUTHORITY_OFF) lets consumers fall back to their legacy inline checks during
rollout without touching the pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID

from . import tool_registry
from .runtime_contracts import GoalAction

_KILL_ON = {"1", "true", "yes", "on"}   # BRUCE_BROKER_AUTHORITY_OFF=<truthy> forces legacy fallback

# action → the operation-name prefix(es) that fulfil it. Provider-neutral: operations are named
# "create_event"/"update_event"/… so the action value is their prefix. A repair is a corrective update.
_ACTION_OP_PREFIX: dict[GoalAction, tuple[str, ...]] = {
    GoalAction.create: ("create",),
    GoalAction.update: ("update",),
    GoalAction.repair: ("update",),
    GoalAction.delete: ("delete",),
    GoalAction.search: ("search",),
    GoalAction.send: ("send",),
    GoalAction.submit: ("submit",),
}

DEFAULT_LIMIT = 4

# typed availability/selection statuses — the honest, distinct outcomes
OK, UNSUPPORTED, DISCONNECTED, INSUFFICIENT_SCOPE, NO_TOOL = (
    "ok", "unsupported", "disconnected", "insufficient_scope", "no_tool")


def authority_enabled() -> bool:
    """Whether consumers should route capability truth THROUGH the broker (vs their legacy inline check)."""
    return os.environ.get("BRUCE_BROKER_AUTHORITY_OFF", "").strip().lower() not in _KILL_ON


@dataclass(frozen=True)
class _Conn:
    connected: bool
    scopes: tuple[str, ...] = ()


async def _provider_connection(user_id: UUID, provider: str) -> _Conn:
    """THE connection probe (one integration fetch): connected + the scopes actually granted. Patchable seam
    so the whole broker's capability truth is tested without the DB/OAuth."""
    if provider == "google_calendar":
        from . import oauth_google
        try:
            integ = await oauth_google.get_integration(user_id)
        except Exception:
            return _Conn(False)
        if (integ is None or integ.status != "connected" or integ.revoked_at is not None
                or not integ.refresh_token_encrypted):
            return _Conn(False)
        return _Conn(True, tuple(integ.scopes or ()))
    return _Conn(False)


@dataclass(frozen=True)
class Availability:
    """The single capability-truth verdict for one capability + user. `status` is the honest distinction:
    ok / unsupported (not live) / disconnected / insufficient_scope."""
    capability: str
    status: str
    live: bool
    connected: bool
    scoped: bool
    missing_scopes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == OK


async def availability(user_id: UUID, capability: str) -> Availability:
    """The ONE capability-truth check. Collapses the previously-divergent liveness/connection/scope checks:
    live (registry) → connected (integration) → scoped (granted scopes)."""
    spec = tool_registry.get(capability)
    if spec is None or not spec.live:
        return Availability(capability, UNSUPPORTED, live=bool(spec and spec.live), connected=False, scoped=False)
    conn = await _provider_connection(user_id, spec.provider)
    if not conn.connected:
        return Availability(capability, DISCONNECTED, live=True, connected=False, scoped=False)
    if spec.requires_scope and spec.requires_scope not in conn.scopes:
        return Availability(capability, INSUFFICIENT_SCOPE, live=True, connected=True, scoped=False,
                            missing_scopes=(spec.requires_scope,))
    return Availability(capability, OK, live=True, connected=True, scoped=True)


@dataclass(frozen=True)
class ToolCandidate:
    capability: str
    provider: str
    operation: str
    write: bool
    live: bool                 # declared live in the registry (always True for a candidate)
    available: bool            # live AND connected AND scoped for THIS user (status == ok)
    status: str                # ok | disconnected | insufficient_scope
    reversible: bool
    score: float               # relevance rank (router prior + action match)
    required_scopes: tuple[str, ...]
    arg_schema: dict
    reason: str


@dataclass(frozen=True)
class ToolShortlist:
    """A bounded, ranked set of relevant live tools — NOT the registry. `has_actionable` iff a candidate is
    callable right now (ok). `excluded_dead`: relevant caps not live yet; `unavailable`: live but not
    connected; `insufficient_scope`: connected but the grant lacks the scope. Three honest buckets."""
    candidates: tuple[ToolCandidate, ...]
    domain: str | None
    action: GoalAction | None
    has_actionable: bool
    excluded_dead: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    insufficient_scope: tuple[str, ...] = ()

    def actionable(self) -> tuple[ToolCandidate, ...]:
        return tuple(c for c in self.candidates if c.available)


@dataclass(frozen=True)
class Selection:
    """The result of the SINGLE selection entry point. `chosen` is the one tool to call, or None with a
    typed status the caller turns into an honest reply (never a guess)."""
    status: str                       # ok | unsupported | disconnected | insufficient_scope | no_tool
    chosen: ToolCandidate | None
    shortlist: ToolShortlist
    reason: str = ""


def _reason(*, in_cand: bool, op_match: bool, status: str) -> str:
    base = ("router prior + action match" if in_cand and op_match
            else "router prior" if in_cand
            else "action match" if op_match
            else "domain candidate")
    return base if status == OK else f"{base} ({status})"


async def shortlist(user_id: UUID, *, domain: str | None = None, action: GoalAction | None = None,
                    candidate_capabilities: tuple[str, ...] = (),
                    limit: int = DEFAULT_LIMIT) -> ToolShortlist:
    """Shortlist the relevant, live tools for one request, each resolved to its full availability (connected
    + scoped). Relevance is structural: a capability the router named, or a tool whose operation matches the
    action. Non-tool actions (answer/remember/plan/…) shortlist nothing."""
    cand_set = set(candidate_capabilities or ())
    op_prefixes = _ACTION_OP_PREFIX.get(action, ()) if action is not None else ()

    if action is not None and not op_prefixes and not cand_set:
        return ToolShortlist(candidates=(), domain=domain, action=action, has_actionable=False)

    ranked: list[tuple[tool_registry.ToolSpec, float, bool, bool]] = []
    excluded_dead: list[str] = []
    for t in tool_registry.specs(domain):
        in_cand = t.capability in cand_set
        op_match = bool(op_prefixes) and any(t.operation.startswith(p) for p in op_prefixes)
        relevant = in_cand or op_match or (not op_prefixes and not cand_set)
        if not relevant:
            continue
        if not t.live:                                        # relevant but not live yet -> honest exclusion
            excluded_dead.append(t.capability)
            continue
        score = (1.0 if in_cand else 0.0) + (0.6 if op_match else 0.0) or 0.1
        ranked.append((t, score, in_cand, op_match))

    ranked.sort(key=lambda r: (-r[1], r[0].capability))       # score desc, stable by capability
    candidates: list[ToolCandidate] = []
    unavailable: list[str] = []
    insufficient: list[str] = []
    for t, score, in_cand, op_match in ranked[:limit]:
        try:
            av = await availability(user_id, t.capability)
        except Exception:
            av = Availability(t.capability, DISCONNECTED, live=True, connected=False, scoped=False)
        if av.status == INSUFFICIENT_SCOPE:
            insufficient.append(t.capability)
        elif not av.ok:
            unavailable.append(t.capability)
        candidates.append(ToolCandidate(
            capability=t.capability, provider=t.provider, operation=t.operation, write=t.write,
            live=True, available=av.ok, status=av.status, reversible=t.reversible, score=score,
            required_scopes=(t.requires_scope,) if t.requires_scope else (),
            arg_schema=dict(t.arg_schema),   # defensive copy — a planner must never mutate the registry's dict
            reason=_reason(in_cand=in_cand, op_match=op_match, status=av.status)))

    return ToolShortlist(
        candidates=tuple(candidates), domain=domain, action=action,
        has_actionable=any(c.available for c in candidates),
        excluded_dead=tuple(excluded_dead), unavailable=tuple(unavailable),
        insufficient_scope=tuple(insufficient))


async def select(user_id: UUID, *, domain: str | None = None, action: GoalAction | None = None,
                 candidate_capabilities: tuple[str, ...] = ()) -> Selection:
    """The SINGLE tool-selection entry point: shortlist, then pick the one actionable tool, or return a typed
    non-selection distinguishing no-tool / unsupported / disconnected / insufficient_scope for an honest
    reply. No production path should choose a provider tool except through here."""
    sl = await shortlist(user_id, domain=domain, action=action, candidate_capabilities=candidate_capabilities)
    act = sl.actionable()
    if act:
        return Selection(OK, act[0], sl, reason=act[0].reason)
    if not sl.candidates:
        status = UNSUPPORTED if sl.excluded_dead else NO_TOOL
        return Selection(status, None, sl, reason=status)
    # candidates exist but none actionable -> the top candidate's own status (disconnected vs scope)
    status = sl.candidates[0].status
    return Selection(status, None, sl, reason=status)
