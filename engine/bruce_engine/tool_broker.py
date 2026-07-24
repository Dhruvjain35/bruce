"""ToolBroker (G0.3) — shortlist the FEW tools a request could need, never the whole capability universe.

When Gmail/Drive/Canvas/… come online the planner must not be handed every tool that exists — that bloats
the prompt, slows planning, and tempts the model to propose a tool that isn't live or isn't connected. The
broker sits between "what the user wants" (the router's RouterDecision / a GoalSpec: domain + action +
candidate capabilities) and "what Bruce can actually call" (the ToolRegistry joined with THIS user's live
connections). It returns a small, ranked shortlist of relevant tools — each tagged live/available — plus an
honest record of what it excluded because it isn't live yet (so the planner can say "not live yet" instead
of guessing) or isn't connected (so it can say "connect your calendar").

Deterministic and capability-truthful: relevance is structural (the router's named capability, or the
action→operation match), liveness/availability come straight from the registry. No model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from . import tool_registry
from .runtime_contracts import GoalAction

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


@dataclass(frozen=True)
class ToolCandidate:
    capability: str
    provider: str
    operation: str
    write: bool
    live: bool                 # declared live in the registry (always True for a candidate)
    available: bool            # live AND the provider is connected + healthy for THIS user
    reversible: bool
    score: float               # relevance rank (router prior + action match)
    reason: str


@dataclass(frozen=True)
class ToolShortlist:
    """A bounded, ranked set of relevant live tools — NOT the registry. `has_actionable` is True iff at
    least one candidate is callable right now (live AND connected). `excluded_dead` names relevant
    capabilities left out because they aren't live yet (feeds an honest "not live yet"); `unavailable`
    names live candidates the user hasn't connected (feeds an honest "connect …")."""
    candidates: tuple[ToolCandidate, ...]
    domain: str | None
    action: GoalAction | None
    has_actionable: bool
    excluded_dead: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    def actionable(self) -> tuple[ToolCandidate, ...]:
        return tuple(c for c in self.candidates if c.available)


def _reason(*, in_cand: bool, op_match: bool, available: bool) -> str:
    base = ("router prior + action match" if in_cand and op_match
            else "router prior" if in_cand
            else "action match" if op_match
            else "domain candidate")
    return base if available else f"{base} (provider not connected)"


async def shortlist(user_id: UUID, *, domain: str | None = None, action: GoalAction | None = None,
                    candidate_capabilities: tuple[str, ...] = (),
                    limit: int = DEFAULT_LIMIT) -> ToolShortlist:
    """Shortlist the relevant, live tools for one request. Relevance is structural: a capability the router
    explicitly named, or a tool whose operation matches the action. With neither signal, the domain's tools
    are weakly relevant (bounded by `limit`). Non-tool actions (answer/remember/plan/…) shortlist nothing."""
    cand_set = set(candidate_capabilities or ())
    op_prefixes = _ACTION_OP_PREFIX.get(action, ()) if action is not None else ()

    # An action with no tool operation (answer, remember, plan, coordinate, monitor, follow_up, verify) and
    # no explicitly-named capability needs no provider tool at all.
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
    for t, score, in_cand, op_match in ranked[:limit]:
        try:
            available = await tool_registry.is_available(t.capability, user_id)
        except Exception:
            available = False
        if not available:
            unavailable.append(t.capability)
        candidates.append(ToolCandidate(
            capability=t.capability, provider=t.provider, operation=t.operation, write=t.write,
            live=True, available=available, reversible=t.reversible, score=score,
            reason=_reason(in_cand=in_cand, op_match=op_match, available=available)))

    return ToolShortlist(
        candidates=tuple(candidates), domain=domain, action=action,
        has_actionable=any(c.available for c in candidates),
        excluded_dead=tuple(excluded_dead), unavailable=tuple(unavailable))
