"""THE TYPED BOUNDARY between Bruce's speech and the bytes a student approved for delivery.

TWO KINDS OF TEXT LEAVE THIS SYSTEM AND THEY OBEY DIFFERENT LAWS.

    Bruce conversational text      -> voice/style gate: PROHIBITED_PHRASES, persona, punctuation, slang
    approved consequential payload -> exact-byte integrity, safety/policy, authorization, provider limits

Conflating them produced two defects that pull in opposite directions. `gate_outbound_text` strips
corporate filler and rewrites em dashes on every plain-text outbound — correct for Bruce's own voice, and
wrong for an email body a student read and approved. So the proposal on screen said one thing and the
message that reached the professor said another (DEFECT-13); and a first attempt to close that gap by
running the payload THROUGH the voice gate fixed the divergence by damaging the payload instead —
"I'd be happy to talk about the extension" shipped as "talk about the extension".

Both attempts share one mistake: treating the payload as a kind of Bruce-speech that needs different
handling. It is not Bruce speaking at all. It is the student's outgoing message, quoted for approval.

SO THE BOUNDARY IS A TYPE, NOT A FLAG OR A PROTECTED SPAN. A protected-span carve-out inside the voice
gate would leave the payload inside the gate's domain, one refactor away from being styled again, and the
gate would still be the thing deciding what happens to bytes it has no authority over. Here the payload is
a different type that the gate REFUSES, so routing it into the voice pipeline is a structural failure at
the call site rather than a silent rewrite discovered in someone's inbox.

WHAT STILL APPLIES to a payload, and this is not a loosening: authorization (a payload is only sent under
a grant that names it), safety and policy checks, provider limits, and verified read-back. What stops
applying is only the rules that exist to govern how BRUCE sounds. If the student approved an email that
says something Bruce would never say conversationally, that is the student's message and it ships.

FROZEN BEFORE THE DECISION. `freeze()` is called when the confirmation is proposed, and `digest()` is what
the approval binds. Execution re-derives the digest from what the adapter is actually about to send, so a
single byte changed anywhere between approval and the provider call stops the write. That is why the
fields are a frozen mapping of exact strings and why nothing here normalizes whitespace: `\\n\\n` and
`\\n` are different emails, and an approval that cannot tell them apart is not an approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# The fields whose bytes a student actually READS AND APPROVES, and which therefore bind exactly.
#
# DELIBERATELY NARROW, and it was narrowed by a test rather than by taste. `title` and `description` were
# here at first, and `title` broke
# `test_the_fingerprint_ignores_what_does_not_change_the_world_and_nothing_else`, which pins
# `" chess  club "` and `"chess club"` as the SAME calendar event. That test is right: a doubled space in
# an event title does not change the world, and re-approving because of one would be consent theatre.
#
# The distinction that survives is prose the student read as prose. An email subject and body are the
# message; a calendar title is a label on an act whose substance is its time and its attendees.
#
# Recipients are deliberately absent for the opposite reason: an address IS consequential, and
# `authorization_evidence` already binds it correctly as an ADDRESS (case-folded, order-insensitive for
# a cc list). Binding it twice under two different rules would make "Prof@x.com" and "prof@x.com" two
# different approvals.
#
# `description` is left out because it was not asked for and nothing yet reads it as approved prose. If a
# calendar description ever becomes something a student composes and confirms, it belongs here — and that
# is a decision to make deliberately, not by widening a set.
EXACT_TEXT_FIELDS: frozenset[str] = frozenset({"subject", "body"})


class PayloadEnteredVoicePipeline(TypeError):
    """Raised when an approved payload is routed into Bruce's voice/style gate.

    A TypeError rather than a sanitizing fallback on purpose. The failure has to be structural and loud at
    the call site: a payload that quietly passes through the voice gate is a message the student approved
    and did not send, discovered later by the recipient.
    """


@dataclass(frozen=True)
class ApprovedConsequentialPayload:
    """The exact bytes a student approved for delivery. NOT Bruce's speech, and never styled.

    `fields` is a read-only mapping of exact strings — no normalization, no stripping, no whitespace
    collapsing. Two bodies differing only in a newline are two different payloads with two different
    digests, because they are two different emails.
    """

    capability: str
    fields: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability:
            raise ValueError("a payload must name the capability it will be delivered through")
        for key, value in dict(self.fields).items():
            if not isinstance(value, str):
                raise TypeError(f"payload field {key!r} must be an exact string, got {type(value).__name__}")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def digest(self) -> str:
        """The identity of these exact bytes. What the approval binds and what execution re-checks."""
        blob = json.dumps({k: self.fields[k] for k in sorted(self.fields)},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def matches(self, candidate: Mapping[str, str]) -> bool:
        """Byte-exact comparison against what a provider is about to receive. No tolerance."""
        return all(str(candidate.get(k, "")) == v for k, v in self.fields.items())

    def render_for_display(self) -> str:
        """The payload as the student must see it — verbatim, because they are approving these bytes.

        Kept here rather than at the call site so the rendering cannot drift from what `digest()` binds.
        """
        order = [k for k in ("subject", "title", "body", "description") if k in self.fields]
        order += [k for k in sorted(self.fields) if k not in order]
        return "\n\n".join(self.fields[k] for k in order if self.fields[k])

    def __str__(self) -> str:      # pragma: no cover - the message is the point, not the branch
        raise PayloadEnteredVoicePipeline(
            "an ApprovedConsequentialPayload was interpolated into text. Its bytes are approved for "
            "delivery and must not become part of Bruce's conversational output — use "
            "render_for_display() for the student's copy, or for_execution() for the provider call.")

    def for_execution(self) -> dict[str, str]:
        """A plain dict for the adapter. Named for its one legitimate use so a reader can see, at the call
        site, that these bytes are on their way to a provider rather than into a reply."""
        return dict(self.fields)


def freeze(capability: str, arguments: Mapping[str, object]) -> ApprovedConsequentialPayload:
    """Freeze the consequential text fields of a pending operation, before the Decision is proposed.

    Only `EXACT_TEXT_FIELDS` are frozen. Everything else about the call — recipient, times, attendees —
    is still bound by `authorization_evidence.fingerprint`, which normalizes them the way each kind of
    value deserves. This type exists for the fields where normalization is itself the bug.
    """
    return ApprovedConsequentialPayload(
        capability=capability,
        fields={k: v for k, v in arguments.items() if k in EXACT_TEXT_FIELDS and isinstance(v, str)})
