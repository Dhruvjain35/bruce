"""MissionStartPresentation (P0) — a context-aware mission acknowledgement + status, GENERATED from
verified mission state, never a hard-coded sentence.

The live failure ('Got it, I'm understanding this now. I'll message you when it needs review.') was a
canned line that referenced nothing. This module builds Bruce's response from the actual mission: the
extracted flyer facts (when confident), what Bruce is doing, and when it will interrupt — and NEVER
claims registration/booking/submission/completion unless externally verified (A1 does none of that).

Facts come ONLY from the model's grounded ExtractedEntity list; nothing is invented. All output is short,
casual, and flows through the universal outbound gate (no em dash, no corporate phrasing).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MissionStartPresentation:
    """The contract a mission-start acknowledgement is rendered from (item 3). A1 populates a subset;
    later phases fill next_actions / blocked_reason / external_action_attempted as they become real."""
    mission_id: str
    user_goal: str
    capability: str
    source_summary: str
    extracted_facts: dict = field(default_factory=dict)
    current_phase: str = "understanding"
    next_actions: list[str] = field(default_factory=list)
    next_interruption_condition: str = "i'll only ping u if i need ur call"
    blocked_reason: str | None = None
    evidence_count: int = 0
    external_action_attempted: bool = False
    style_profile: object | None = None
    bubble_plan: list[str] = field(default_factory=list)


# entity-type keyword -> canonical fact key. Grounded extraction only; a missing fact stays absent.
_FACT_MAP = (
    (("title", "event", "name"), "event"),
    (("deadline", "due"), "deadline"),          # before generic date so a deadline isn't mislabelled
    (("date", "day"), "date"),
    (("time",), "time"),
    (("location", "place", "venue", "address", "where"), "location"),
    (("price", "cost", "fee", "amount", "$"), "price"),
    (("url", "link", "registration", "register", "signup", "sign-up", "rsvp"), "url"),
    (("organizer", "host", "org"), "organizer"),
    (("eligibility", "grade", "who"), "eligibility"),
)


def extract_flyer_facts(decision) -> dict:
    """Pull grounded event facts from a decision's ExtractedEntity list into a canonical dict. First
    confident value per key wins; never invents a value the model didn't extract."""
    facts: dict[str, str] = {}
    for e in getattr(decision, "extracted_entities", None) or []:
        et = (getattr(e, "type", "") or "").lower()
        val = getattr(e, "value", None)
        if not val or getattr(e, "confidence", 1.0) < 0.4:
            continue
        for keys, canon in _FACT_MAP:
            if canon in facts:
                continue
            if any(k in et for k in keys):
                facts[canon] = (getattr(e, "normalized", None) or val) if canon in ("date", "deadline") else val
                break
    return facts


def _confident(facts: dict) -> bool:
    """We can be specific if we have an event name, OR both a date and a location."""
    return bool(facts.get("event") or (facts.get("date") and facts.get("location")))


def render_start(pres: MissionStartPresentation) -> str:
    """The mission-start acknowledgement: specific when facts are confident, honestly generic otherwise.
    Always states what Bruce is doing + when it interrupts; never claims an action it hasn't taken."""
    f = pres.extracted_facts
    if _confident(f):
        desc = f.get("event") or "this"
        tail = []
        if f.get("location"):
            tail.append(f"at {f['location']}")
        if f.get("date"):
            tail.append(f"on {f['date']}")
        where_when = (" " + " ".join(tail)) if tail else ""
        return (f"gotchu. this looks like {desc}{where_when}. i'm checking the details and what u need "
                f"to do, {pres.next_interruption_condition}.")
    return ("gotchu. i'm pulling the dates, cost, location, and anything u need to do. "
            "i'll only ping u if i need ur call or find something important.")


def render_status(state: dict) -> str:
    """Status from PERSISTED mission state (item 6). A1 truth: understood/captured, nothing external yet.
    Distinguishes understood/prepared/attempted/submitted/verified honestly (A1 is always 'understood')."""
    goal = state.get("goal") or {}
    facts = goal.get("extracted_facts") or {}
    have = [facts[k] for k in ("event", "date", "location") if facts.get(k)]
    got = (" (" + ", ".join(have) + ")") if have else ""
    what = goal.get("proposed_goal") or "it"
    lead = f"rn i've got {what} saved{got}" if not got else f"rn i've got the flyer saved{got}"
    return (f"{lead} and i'm checking the date, location, cost, and signup requirements. "
            f"i haven't submitted or registered anything yet.")
