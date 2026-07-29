"""What a negation is actually ABOUT — clause-level scope over the student's own words.

THE BUG THIS EXISTS FOR. Turn 2 of the founder transcript is:

    "make it a professional email and send it dont show me draft"

`decision_resolver` saw "dont", read the turn as a refusal, and the goal was CANCELLED. But the negation
governs SHOWING THE DRAFT. The student was asking for the opposite of a refusal — send it, and skip the
preview. Bruce cancelled the thing it was being told to hurry up and do.

WHY NOT JUST SOFTEN REFUSAL DETECTION. Because the same rule is what makes a bare "dont send it" stop a
send. Weakening it trades an annoying bug for an irreversible one: mail that goes out after someone said
don't. So the fix is not to care LESS about negation, it is to work out WHAT EACH NEGATION ATTACHES TO.

HOW. Split the trusted text into clauses, classify each independently, then resolve. A clause carries at
most one directive about the operation and at most one about presentation, so "send it" and "dont show me
draft" stop fighting over the same verdict — they are different clauses about different things.

Three readings are load-bearing and none of them is a phrase table:

  * a NEGATED operation verb rejects the operation            "dont send it"
  * a NEGATED presentation verb hides the draft               "dont show me the draft"
  * a negated operation plus "without <presentation>" is a DOUBLE NEGATIVE and asks for the preview —
    "dont send it without showing me first" means show me first, not hide it

WHAT THIS MODULE MAY NOT DECIDE. Nothing about authority. It reads text and returns structure. Whether an
approval may resolve a Decision, whether a rejection may close one, and whether anything executes are the
caller's calls — and the caller must pass TRUSTED text only. `authorizing_text()` exists precisely because
"yes send it" inside a forwarded email is not consent, and this module has no way to tell the difference:
it is a parser, so it must never be handed the join.

AMBIGUITY IS A RESULT, NOT A FAILURE. Two conflicting operation directives with nothing marking which is
the correction resolve to `unclear`, which the caller turns into exactly one question and zero provider
calls. Guessing here would mean guessing about a send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# --- vocabulary -------------------------------------------------------------------------------------
# These are ROLE words, not product phrases: an operation verb is anything that means "do the thing",
# whichever tool is behind it. Adding a tool adds a verb here, never a branch anywhere else.

_NEG = r"(?:dont|don't|do\s?not|doesnt|doesn't|never|no|nope|nah|stop|cancel|dont't)"
_OP_VERB = (r"(?:send|sent|sending|email|e-mail|mail|fire\s+off|shoot|create|add|schedule|book|move|"
            r"reschedul\w*|update|change|put|post|submit|do\s+it|go\s+ahead)")
_PRES_VERB = r"(?:show|showing|see|seeing|preview|previewing|display|read|check|look\s+at|proofread)"

# A marker that the clause after it SUPERSEDES what came before. Without one, two opposite operation
# directives are a contradiction rather than a correction — and a contradiction about sending is a
# question, not a coin flip.
_CORRECTION = r"(?:actually|wait|no\s+wait|scratch\s+that|nvm|nevermind|never\s+mind|instead|on\s+second)"

# Tone/content edits. Deliberately small and generic; the slot they land in is the caller's business.
_TONE = {
    "professional": ("professional", "formal", "business", "polite", "proper"),
    "casual": ("casual", "chill", "relaxed", "informal", "friendly"),
    "shorter": ("shorter", "short", "brief", "concise", "tighter", "trim"),
    "longer": ("longer", "detailed", "more detail", "expand", "fuller"),
    "warmer": ("warmer", "heartfelt", "sincere", "warm", "personal", "sweet"),
}

_CLAUSE_SPLIT = re.compile(
    r"(?:[,;.!?]+|\b(?:and|but|then|just|also|plus|however|though)\b|\b" + _CORRECTION + r"\b)",
    re.IGNORECASE)
# A negation that FOLLOWS other words starts a new clause: "send it dont show me draft" is two
# instructions, and treating it as one is exactly how the transcript's turn 2 became a cancellation.
_TRAILING_NEG = re.compile(r"(?<=\w)\s+(?=" + _NEG + r"\b)", re.IGNORECASE)


class Polarity(str, Enum):
    approve = "approve"
    reject = "reject"
    unclear = "unclear"     # conflicting directives with no correction marker -> ask, execute nothing
    none = "none"           # the turn said nothing about the operation at all


class ShowDraft(str, Enum):
    true = "true"
    false = "false"
    unchanged = "unchanged"


CONFLICTING_OPERATION = "conflicting_operation_directives_without_a_correction_marker"


@dataclass(frozen=True)
class DirectiveScope:
    """What the student's own words asked for, separated by what each part was about."""

    operation_polarity: Polarity = Polarity.none
    presentation_show_draft: ShowDraft = ShowDraft.unchanged
    tone_update: str | None = None
    content_update: str | None = None
    spans: tuple[str, ...] = ()            # the trusted substrings each reading rests on
    ambiguity_reason: str | None = None

    @property
    def rejects_operation(self) -> bool:
        return self.operation_polarity is Polarity.reject

    @property
    def approves_operation(self) -> bool:
        return self.operation_polarity is Polarity.approve

    @property
    def is_ambiguous(self) -> bool:
        return self.operation_polarity is Polarity.unclear


@dataclass
class _Clause:
    text: str
    corrected: bool = False                # preceded by a correction marker
    op: Polarity = Polarity.none
    show: ShowDraft = ShowDraft.unchanged
    tone: str | None = None
    bare_negation: bool = False            # "dont" with no verb — it refers back
    spans: list[str] = field(default_factory=list)


def _split(text: str) -> list[_Clause]:
    """Clauses, in order, each flagged if a correction marker introduced it."""
    marked: list[_Clause] = []
    cursor = 0
    for m in _CLAUSE_SPLIT.finditer(text):
        piece = text[cursor:m.start()].strip()
        if piece:
            marked.append(_Clause(piece))
        # the correction flag belongs to the clause that FOLLOWS the marker
        if re.fullmatch(_CORRECTION, m.group(0).strip(), re.IGNORECASE):
            marked.append(_Clause("", corrected=True))
        cursor = m.end()
    tail = text[cursor:].strip()
    if tail:
        marked.append(_Clause(tail))

    # carry a pending correction flag onto the next real clause, then split trailing negations
    out: list[_Clause] = []
    pending = False
    for c in marked:
        if not c.text:
            pending = pending or c.corrected
            continue
        for i, part in enumerate(p.strip() for p in _TRAILING_NEG.split(c.text) if p.strip()):
            out.append(_Clause(part, corrected=(pending and i == 0) or c.corrected))
        pending = False
    return out


def _classify(c: _Clause) -> None:
    t = c.text.lower()
    negated = bool(re.search(r"^\s*" + _NEG + r"\b", t) or re.search(r"\b" + _NEG + r"\b", t))
    has_op = bool(re.search(r"\b" + _OP_VERB + r"\b", t))
    has_pres = bool(re.search(r"\b" + _PRES_VERB + r"\b", t))
    without = bool(re.search(r"\bwithout\b", t))

    if negated and not has_op and not has_pres:
        # A BARE negation is the whole clause — "actually dont", "no", "nah". It refers backward and flips
        # the previous operation directive. "NO MORE QUESTIONS" is NOT one: the negation governs the noun
        # after it, and reading it as a refusal turns an impatient approval into a cancellation, which is
        # this module's own bug pointing the other way. So the clause must be the negation plus nothing
        # that carries meaning of its own.
        rest = re.sub(r"^\s*" + _NEG + r"\b", "", t, count=1, flags=re.IGNORECASE)
        rest = re.sub(r"\b(?:it|that|this|them|please|pls|thanks|thx|rn|now|tho|though)\b", "", rest,
                      flags=re.IGNORECASE)
        if re.fullmatch(r"[\W_]*", rest or ""):
            c.bare_negation = True
            c.spans.append(c.text)
        return

    if has_pres:
        # DOUBLE NEGATIVE: "dont send it without showing me first" asks FOR the preview. Reading the
        # negation as hiding it would do the precise opposite of what was said, before an irreversible act.
        c.show = ShowDraft.true if (negated and without) else (
            ShowDraft.false if negated else ShowDraft.true)
        c.spans.append(c.text)

    if has_op:
        # "without showing" is a condition ON the send, so the negation still rejects the send itself.
        c.op = Polarity.reject if negated else Polarity.approve
        c.spans.append(c.text)

    for tone, words in _TONE.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in words):
            c.tone = tone
            c.spans.append(c.text)
            break


def interpret(trusted_text: str | None) -> DirectiveScope:
    """Read the student's OWN words into separated directives.

    `trusted_text` must already be authored text — `input_envelope.authorizing_text()` or
    `decision_resolver.trusted_reply_text()`. Quoted, forwarded, OCR'd, attachment and provider text must
    never reach here, because this function cannot tell whose sentence it is and would happily read a
    stranger's "yes send it" as approval.
    """
    text = (trusted_text or "").strip()
    if not text:
        return DirectiveScope()

    clauses = _split(text)
    for c in clauses:
        _classify(c)

    # A bare negation refers BACKWARD to the nearest operation directive and flips it: "send it actually
    # dont" is a refusal, and it has to be, because the last thing said about sending was "don't".
    op_seq: list[tuple[Polarity, bool]] = []
    show, tone, spans = ShowDraft.unchanged, None, []
    for c in clauses:
        spans.extend(c.spans)
        if c.show is not ShowDraft.unchanged:
            show = c.show
        if c.tone:
            tone = c.tone
        if c.bare_negation:
            if op_seq:
                op_seq.append((Polarity.reject, True))
        elif c.op is not Polarity.none:
            op_seq.append((c.op, c.corrected))

    polarity, reason = _resolve(op_seq)
    return DirectiveScope(operation_polarity=polarity, presentation_show_draft=show, tone_update=tone,
                          spans=tuple(dict.fromkeys(spans)), ambiguity_reason=reason)


def _resolve(op_seq: list[tuple[Polarity, bool]]) -> tuple[Polarity, str | None]:
    """One verdict about the operation, or an honest `unclear`.

    A later directive wins ONLY when something marks it as a correction ("actually", "wait", a bare
    trailing "dont"). Two bare opposites in one breath are a contradiction, and the safe answer to a
    contradiction about sending is a question — never the more convenient half.
    """
    if not op_seq:
        return Polarity.none, None
    distinct = {p for p, _ in op_seq}
    if len(distinct) == 1:
        return op_seq[-1][0], None
    last, corrected = op_seq[-1]
    if corrected:
        return last, None
    return Polarity.unclear, CONFLICTING_OPERATION
