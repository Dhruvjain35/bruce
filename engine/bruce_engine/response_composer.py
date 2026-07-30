"""ResponseComposer (G0.6) — the response-experience authority: shape a runtime outcome into Bruce's
natural, friend-like reply, and enforce the ONE invariant Bruce may never break — never claim an action
happened that wasn't actually done.

The voice mechanics already live in ConversationStyleEngine (filler-stripping, register-by-risk, the hard
no-em-dash rule, fact preservation); the composer is the thin authority on TOP of it. Its centerpiece is the
dual of the capability-truth guard: that guard catches a false DENIAL ("i can't schedule" when calendar is
live); this catches a false AFFIRMATION — a reply that says "added it to ur calendar ✅" when NO verified
action handler ran. A verified calendar op legitimately claims success (its handler only says ✅ after a
read-back); a casual/default reply that fabricates a calendar completion is a lie, and gets downgraded to
the honest non-claim. Scoped tightly to Bruce's action-completion vocabulary so ordinary chat ("i'm done
with hw") is never touched.
"""

from __future__ import annotations

import re

from .conversation_style import ConversationStyleEngine, VoiceProfile, strip_redundant_offer
from .conversation_contract import RiskLevel

# Handlers that ACTUALLY execute + verify a provider action, so their success copy is trustworthy (they emit
# a completion only after a read-back). Any OTHER owner claiming a completion is fabricating.
#
# `semantic_rescue_execute` is the ONE rescue path whose words are driven by a verified ScheduleState, and
# it is deliberately a different name from the `semantic_rescue` that proposes, clarifies and blocks — so
# a proposal or a question that somehow claimed a completion is still downgraded. One name for both would
# have made every rescue reply trusted on the strength of the one path that earned it.
#
# `goal` earns its place the same way and by one line: `goal_handler._run` emits `_COPY.receipt` ONLY
# under `if result.verified`, and `verified` is copied from the adapter's own independent read-back and
# never re-judged. Every other branch of that method says plainly that nothing happened. It is here now
# because registering `calendar.create_event` made the goal lane emit CALENDAR completion copy for the
# first time — an email receipt ("sent it to X ✅") never tripped this gate, and a true calendar receipt
# was being rewritten into "i didn't actually put that on ur calendar yet" after a verified create.
ACTION_HANDLERS = frozenset({"calendar_schedule", "calendar_approval", "calendar_mutation",
                             "semantic_rescue_execute", "goal"})

# A sentence that FRAMES an offer / intent / question is never a "it landed" claim, even if it contains
# completion-shaped words ("i'll put it on ur calendar", "want me to move it"). Checked per-sentence so a
# real receipt followed by an offer ("added it ✅. want me to add another?") still fires on the receipt.
_OFFER_CUE = re.compile(
    r"\b(?:i'?ll|i will|i'?d|i can|i could|want me to|wanna|gonna|going to|ready to|"
    r"can (?:i|you|we|u)|could (?:i|you|we|u)|lemme|let me|once (?:you|u)|if (?:you|u) want|should i)\b",
    re.IGNORECASE)

# A claim that a calendar change LANDED — object-bearing past forms, an "is (now) on/in your calendar"
# statement, or a receipt anchored by ✅ / an explicit first-person past tense (an offer is "i can/i'll
# <bare verb>", never "i <past verb>"). Split from the offer/question gate so tense and object are judged
# separately — precision-first: better to miss an object-less "you're booked" than to downgrade a good reply.
_COMPLETION_CLAIM = re.compile(
    r"\b(?:added|put|saved|scheduled|booked|created|slotted|moved|pushed|bumped|rescheduled|changed)\b"
    r"[^.?!\n]*\b(?:to|on)\s+(?:ur|your)\s+(?:cal|calendar)\b"
    r"|\b(?:deleted|removed|canceled|cancelled|took)\b[^.?!\n]*\b(?:off|from)\s+(?:ur|your)?\s*(?:cal|calendar)\b"
    r"|\b(?:is|it'?s|that'?s|they'?re)\s+(?:now\s+)?(?:on|in)\s+(?:ur|your)?\s*(?:cal|calendar)\b"
    r"|\b(?:on|in)\s+(?:ur|your)\s+(?:cal|calendar)\b[^.?!\n]*✅"
    r"|\bit'?s on the books\b"
    r"|\bit'?s (?:scheduled|booked)\b[^.?!\n]*✅"
    r"|\b(?:added|scheduled|booked|created|slotted|moved|deleted|removed)\b[^.?!\n]*✅"
    r"|\bi (?:just |already |went ahead and )?(?:scheduled|booked|added|created|slotted|moved|deleted|removed)\b",
    re.IGNORECASE)

# The honest non-claim when a fabricated completion is caught: never assert, never deny a live capability —
# just refuse to lie about the outcome and offer to actually do it.
_HONEST_NONCLAIM = ("i didn't actually put that on ur calendar yet, so i'm not gonna say it's done. "
                    "want me to actually add it?")


def claims_action_completion(reply: str | None) -> bool:
    """Does this reply assert a calendar action LANDED? A sentence that frames an offer/intent ('i can add
    that') or is a question ('is that on your calendar?') is never a landed-action claim — checked per
    sentence so a real receipt survives a trailing follow-up offer."""
    if not reply:
        return False
    for sentence in re.findall(r"[^.?!\n]+[.?!\n]?", reply):
        s = sentence.strip()
        if not s or s.endswith("?"):                  # a question is never a completion claim
            continue
        if _OFFER_CUE.search(s):                       # an offer/intent sentence — not "it landed"
            continue
        if _COMPLETION_CLAIM.search(s):
            return True
    return False


def no_false_completion(reply: str, *, handler: str | None) -> str:
    """If a NON-action handler's reply claims a calendar completion, it's a fabrication -> honest non-claim.
    A real action handler's success copy passes through (it only claims after a verified read-back)."""
    if handler in ACTION_HANDLERS:
        return reply
    if claims_action_completion(reply):
        return _HONEST_NONCLAIM
    return reply


def compose(text: str, *, style: ConversationStyleEngine, profile: VoiceProfile | None = None,
            risk_level: RiskLevel = RiskLevel.none, protect_technical: bool = False) -> str:
    """The natural, friend-like voice for a FREEFORM reply: the style engine's fact-guarded render, then
    drop any trailing redundant offer ('want me to help?' tacked on a complete answer). Fact-locked copy
    (templates, verified action sentences) should NOT go through here — it's already final."""
    styled = style.render(text, risk_level=risk_level, profile=profile, protect_technical=protect_technical)
    return strip_redundant_offer(styled)
