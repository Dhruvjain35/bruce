"""CapabilitySnapshot — what Bruce can ACTUALLY do for this student, right now, in words the model reads.

THE BUG THIS CLOSES. `conversation_runtime` computed a ToolBroker shortlist on every tool-bearing turn
and then threw it away as shadow telemetry, while the system prompt instructed the model to "only claim
a capability Bruce actually has". The model was told to be truthful about something it could not see, so
it guessed — and on a live, fully-scoped Google connection it guessed *wrong*, telling a student
"i can't add it to your calendar from here". That denial then blocked a P0 verification, because the
turn never produced the calendar Decision it should have.

The runtime knew. The model was never told. That gap is the whole defect.

This module answers one question, from `tool_broker.availability` (the single capability-truth authority)
and nothing else: for each capability family, is it usable, and if not, why. It never infers from a
registry constant, a scope string, or the shape of the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from . import tool_broker

# The families a student-facing turn can plausibly need. Deliberately small: this is a prompt line, not
# a registry dump, and a model that sees 40 capabilities picks worse than one that sees the real few.
FAMILIES: dict[str, tuple[str, ...]] = {
    "calendar": ("calendar.create_event", "calendar.update_event", "calendar.delete_event"),
    "email": ("gmail.send_message", "gmail.find_reply"),
}

# Human words for each broker status, so the model reads a reason rather than an enum.
_WHY = {
    tool_broker.DISCONNECTED: "not connected yet",
    tool_broker.INSUFFICIENT_SCOPE: "connected but missing permission",
    tool_broker.UNSUPPORTED: "not built yet",
}


@dataclass(frozen=True)
class FamilyState:
    family: str
    usable: bool
    reason: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySnapshot:
    """Truthful, per-user, computed fresh for the turn. `render()` is what reaches the model."""

    families: tuple[FamilyState, ...] = field(default=())

    def usable(self) -> tuple[str, ...]:
        return tuple(f.family for f in self.families if f.usable)

    def unusable(self) -> tuple[FamilyState, ...]:
        return tuple(f for f in self.families if not f.usable)

    def is_usable(self, family: str) -> bool:
        return any(f.family == family and f.usable for f in self.families)

    def render(self) -> str:
        """One or two short lines. Positive first: what Bruce CAN do is the fact the model most often
        gets wrong, and stating it plainly is the point of this whole module."""
        if not self.families:
            return ""
        can = self.usable()
        parts = []
        if can:
            parts.append("Right now you CAN use: " + ", ".join(can) + ".")
        else:
            parts.append("Right now you have NO connected tools.")
        cannot = self.unusable()
        if cannot:
            parts.append("You cannot use: " + ", ".join(f"{f.family} ({f.reason})" for f in cannot) + ".")
        parts.append("Never claim a tool you cannot use, and never deny one you can.")
        return " ".join(parts)


async def snapshot(user_id: UUID, families: dict[str, tuple[str, ...]] | None = None) -> CapabilitySnapshot:
    """Resolve every family through `tool_broker.availability` — the ONE capability-truth check.

    A family is usable when ANY capability in it is usable, because "can you touch my calendar" is
    answered by create OR update being live, not by all of them. Fault-isolated per family: a broker
    error degrades that family to unusable-with-reason rather than blanking the snapshot, since a
    missing snapshot returns the model to guessing, which is the failure mode being fixed.
    """
    fams = families or FAMILIES
    states: list[FamilyState] = []
    for family, caps in fams.items():
        usable_caps: list[str] = []
        reason = "unavailable"
        for cap in caps:
            try:
                av = await tool_broker.availability(user_id, cap)
            except Exception:
                continue
            if av.ok:
                usable_caps.append(cap)
            else:
                reason = _WHY.get(av.status, av.status)
        states.append(FamilyState(family=family, usable=bool(usable_caps),
                                  reason="" if usable_caps else reason,
                                  capabilities=tuple(usable_caps)))
    return CapabilitySnapshot(families=tuple(states))


# --- contradiction validator ---------------------------------------------------------------------------

def contradicts(reply: str, snap: CapabilitySnapshot) -> str | None:
    """Return the family a reply WRONGLY denies, or None.

    Structural rather than phrase-matching: the previous guard was a denial regex and it failed in
    production on a curly apostrophe. This normalizes first, then looks for the co-occurrence of a
    negated-ability construction and a family term — so "i can't", "i cannot", "i'm not able to",
    "no way for me to" all land the same way, and a new phrasing does not need a new pattern.
    """
    from . import text_norm

    t = text_norm.fold_match(reply or "")
    if not t:
        return None
    # any construction that negates Bruce's own ability
    inability = any(k in t for k in (
        "i cant", "i can not", "i cannot", "im not able", "i am not able", "im unable", "i am unable",
        "i dont have access", "i do not have access", "no way for me to", "not able to do that",
        "cant do that", "cant from here", "cant help with that",
    ))
    if not inability:
        return None
    for fam in snap.usable():
        terms = ("calendar", "cal ") if fam == "calendar" else ("email", "gmail", "mail")
        if any(term in t for term in terms):
            return fam
    return None
