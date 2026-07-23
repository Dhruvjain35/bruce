"""DecisionResolver — turn a free-text reply into a resolution of a PENDING decision.

The live bug this closes: Bruce asked "want me to add it to ur calendar?", the student said "ya", then
"add it to my google calendar", then "YES ADD IT" — and each was treated as a brand-new message with no
memory of the open question, so the offer just repeated. Authorization never carried forward.

This is the deterministic half of the loop: the model proposes what the user means, but a durable,
testable policy decides whether an OPEN decision is approved / rejected / still ambiguous. It is
intentionally NOT a fixed-string match on the test fixtures — it understands casual approval/refusal
(slang, caps, typos, calendar-action directives) and stays conservative: anything it can't read as a
clear yes/no is 'ambiguous' (ask one precise question), and anything off-topic is 'unrelated' (leave the
decision open, don't accidentally approve).
"""

from __future__ import annotations

import re
from enum import Enum


class Resolution(str, Enum):
    approved = "approved"
    rejected = "rejected"
    ambiguous = "ambiguous"      # a reply about the decision, but not a clear yes/no -> ask one question
    unrelated = "unrelated"      # not about the decision at all -> leave it open, handle normally


# Clear affirmatives: bare yes-words, "do it"/"add it"/"go ahead", and calendar-action directives
# ("put it on there", "add it to my calendar", "set it up", "schedule it"). Tolerant of slang + caps.
_APPROVE = re.compile(
    r"^\s*(?:y(?:es|ea|eah|up|uh|e|a)+|yep|yup|ya|yah|sure|ok(?:ay)?|k|kk|word|bet|"
    r"fs|fr|please\s+do|pls\s+do|yes\s+please|go(?:\s+for\s+it)?|do\s+it|send\s+it|"
    r"absolutely|definitely|for\s+sure|sounds?\s+good|lets?\s+(?:go|do\s+it))\b"
    r"|\b(?:add|put|schedule|save|set|throw|pop|stick|slot|drop|book)\s+(?:it|this|that|ts|em|them)\b"
    r"|\bset\s+(?:it|this|that|ts)?\s*up\b|\bsetup\b"
    r"|\b(?:put|add)\s+(?:it|this|that|ts)?\s*(?:on|in|to)\b"
    r"|\bgo\s+ahead\b|\bdo\s+it\b|\byes+\b",
    re.IGNORECASE)

# Clear negatives. Checked FIRST so "no don't" / "nah" / "not yet" never read as approval.
_REJECT = re.compile(
    r"^\s*(?:no+|nah+|nope|naw|don'?t|do\s+not|cancel|stop|wait|hold\s+on|not\s+yet|"
    r"never\s*mind|nvm|skip|leave\s+it|forget\s+it|nah\s+im\s+good|no\s+thanks?|nty)\b",
    re.IGNORECASE)

# On-topic-but-unresolved: the reply engages the decision but doesn't settle it -> ask ONE question.
_AMBIGUOUS = re.compile(
    r"\b(?:maybe|idk|i\s+dont\s+know|not\s+sure|dunno|which\s+cal(?:endar)?|what\s+time|"
    r"when\??$|hmm+|depends|lemme\s+think|thinking)\b",
    re.IGNORECASE)


def resolve_approval(text: str | None) -> Resolution:
    """Resolve a reply against an OPEN yes/no decision. Order matters: reject > ambiguous > approve, so
    a hedged or negated reply never executes an irreversible-ish action on a maybe."""
    t = (text or "").strip().lower()
    if not t:
        return Resolution.unrelated
    if _REJECT.search(t):
        return Resolution.rejected
    if _AMBIGUOUS.search(t):
        return Resolution.ambiguous
    if _APPROVE.search(t):
        return Resolution.approved
    return Resolution.unrelated
