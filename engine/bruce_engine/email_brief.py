"""Grounded email brief + relationship profiles (email quality layer).

The brief is the ONE structured input the shared email writer consumes — sender/recipient/relationship,
purpose, the exact requested outcome, the grounded facts, attachments, urgency, tone, reply-vs-new, the
required call to action, and privacy constraints. Nothing outside the brief may enter a draft: no invented
recipient details, no fabricated context, no facts the conversation or connected evidence didn't supply.

Relationship profiles carry the tone the situation calls for (teacher / coach / professional / counselor /
peer / support) as small, composable style directives — NOT a giant fixed prompt. The composer assembles a
prompt from {relationship profile + user writing profile + this brief}, so the same writer covers every
context without a per-relationship template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Relationship(str, Enum):
    teacher = "teacher"
    coach = "coach"                 # coach or club leader
    professional = "professional"   # internship / recruiter / professional contact
    counselor = "counselor"
    peer = "peer"                   # friend or peer
    support = "support"             # support / business / customer service
    other = "other"


class Tone(str, Enum):
    respectful = "respectful"
    direct = "direct"
    polished = "polished"
    casual = "casual"
    factual = "factual"


@dataclass(frozen=True)
class RelationshipStyle:
    """A small bundle of style directives for one relationship — fed to the composer, never hardcoded copy."""
    relationship: Relationship
    traits: tuple[str, ...]         # human-readable directives ("respectful", "concise", "student-like")
    default_tone: Tone
    formality: int                  # 1 (casual) .. 5 (polished)
    allow_slang: bool               # texting slang permitted (peer only, unless the user overrides)
    default_signoff: str            # a plain, human closing — never assistant-y


# The situation sets the register; the user's own writing profile refines it on top.
_STYLES: dict[Relationship, RelationshipStyle] = {
    Relationship.teacher: RelationshipStyle(
        Relationship.teacher, ("respectful", "clear", "concise", "student-like", "specific request",
                               "no fake formality"), Tone.respectful, formality=4, allow_slang=False,
        default_signoff="Thanks,"),
    Relationship.coach: RelationshipStyle(
        Relationship.coach, ("direct", "friendly", "organized"), Tone.direct, formality=3, allow_slang=False,
        default_signoff="Thanks,"),
    Relationship.professional: RelationshipStyle(
        Relationship.professional, ("polished", "concise", "confident", "not corporate"), Tone.polished,
        formality=4, allow_slang=False, default_signoff="Best,"),
    Relationship.counselor: RelationshipStyle(
        Relationship.counselor, ("respectful", "clear", "concise", "specific request"), Tone.respectful,
        formality=4, allow_slang=False, default_signoff="Thanks,"),
    Relationship.peer: RelationshipStyle(
        Relationship.peer, ("casual", "natural", "close to the user's texting voice"), Tone.casual,
        formality=1, allow_slang=True, default_signoff=""),
    Relationship.support: RelationshipStyle(
        Relationship.support, ("factual", "easy to scan", "clear requested resolution"), Tone.factual,
        formality=3, allow_slang=False, default_signoff="Thanks,"),
    Relationship.other: RelationshipStyle(
        Relationship.other, ("clear", "concise", "natural"), Tone.direct, formality=3, allow_slang=False,
        default_signoff="Thanks,"),
}


def relationship_style(rel: Relationship) -> RelationshipStyle:
    return _STYLES.get(rel, _STYLES[Relationship.other])


@dataclass(frozen=True)
class Attachment:
    name: str                       # human name shown in the body ("the updated resume")
    kind: str = "file"
    present: bool = True            # whether the file is ACTUALLY attached/available — a lie here is rejected


@dataclass(frozen=True)
class EmailBrief:
    """Everything the writer is allowed to know. Facts in the body must trace back to a field here."""
    sender_name: str
    recipient_relationship: Relationship
    purpose: str                    # one line: why we're emailing
    requested_outcome: str          # the exact ask / the call to action
    recipient_name: str | None = None
    recipient_email: str | None = None
    facts: tuple[str, ...] = ()     # the ONLY concrete facts allowed in the body
    source_context: str | None = None   # what prompted it (an assignment, a prior message) — grounded
    attachments: tuple[Attachment, ...] = ()
    urgency: str = "normal"         # low | normal | high
    desired_tone: Tone | None = None
    is_reply: bool = False
    thread_subject: str | None = None    # existing subject when is_reply
    privacy_constraints: tuple[str, ...] = ()   # e.g. "do not mention the medical reason"

    def grounding_text(self) -> str:
        """All text a draft is allowed to draw concrete facts from — the grounding corpus for the validator."""
        parts = [self.purpose, self.requested_outcome, self.source_context or "", self.thread_subject or "",
                 self.recipient_name or "", self.recipient_email or "", self.sender_name, *self.facts,
                 *(a.name for a in self.attachments)]
        return "\n".join(p for p in parts if p)
