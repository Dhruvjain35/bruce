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
            # THE LIST THE PROMPT ALREADY PROMISED. `conversation_model` instructs the model that "the
            # context contains the operations Bruce can run right now, as exact ids" and that
            # `required_capabilities` "MUST contain only exact operation ids copied from that list".
            # Until this line there was no such list anywhere in the context — the families above are
            # words like "email", not ids — so the model was ordered to copy from something it could not
            # see and wrote prose instead. `goal_runtime` then rejected the prose as NOT_AN_OPERATION_ID,
            # `GoalHandler` declined, and the student got a sentence where an email was meant.
            ops = advertised_operations(self)
            if ops:
                parts.append("Operations you may call, as exact ids — copy one of these verbatim into "
                             "required_capabilities, and treat anything not listed as nonexistent: "
                             + ", ".join(ops) + ".")
        else:
            parts.append("Right now you have NO connected tools.")
        cannot = self.unusable()
        if cannot:
            parts.append("You cannot use: " + ", ".join(f"{f.family} ({f.reason})" for f in cannot) + ".")
        parts.append("Never claim a tool you cannot use, and never deny one you can.")
        return " ".join(parts)


def advertised_operations(snap: CapabilitySnapshot) -> tuple[str, ...]:
    """The exact operation ids the model may name — TWO truths, both required.

      * BROKER truth: the capability is usable for THIS student right now. Already carried on
        `FamilyState.capabilities`, which `snapshot()` fills from `tool_broker.availability`.
      * EXECUTOR truth: the capability can be carried all the way to a verified provider call.

    The second filter is the one that is easy to skip and expensive to skip. `FAMILIES` declares five
    ids; only two of them have both a goal kind and an executor. `turn_context`'s `available_operations`
    is wider still — every registry spec that is `live` and broker-ok, which includes `gmail.get_message`,
    `gmail.get_thread` and `gmail.verify_sent`, none of which have an executor at all.

    Advertising any of those would not fix the defect, it would rename it: the model would copy a real id,
    `GoalHandler` would find nothing to run, and the turn would decline with `capability_has_no_goal_kind`
    instead of `NOT_AN_OPERATION_ID`. Identical outcome for the student, new label in the logs.

    DERIVED, never restated. `goal_handler.executable` is the single authority on carryability, so a
    capability that gains an executor becomes visible to the model the same day and one that loses its
    executor disappears the same day. A second hard-coded list here is a list that would drift.
    """
    # Local import: `goal_handler` pulls in the executor and adapter layers, and this module is imported
    # by the context path on every turn. Keeping the edge lazy keeps that import graph one-directional.
    from . import goal_handler

    out: list[str] = []
    for family in snap.families:
        if not family.usable:
            continue
        out.extend(cap for cap in family.capabilities if goal_handler.executable(cap))
    return tuple(dict.fromkeys(out))          # stable order, de-duplicated


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
