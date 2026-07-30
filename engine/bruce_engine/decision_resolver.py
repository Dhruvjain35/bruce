"""DecisionResolver — turn a free-text reply into a resolution of a PENDING decision.

THE INCIDENT THIS EXISTS TO PREVENT (P0 execution-safety defect). The old rule anchored rejection to the START of
the message (`^\\s*(?:no|nah|don't|...)`) while leaving approval's action branches unanchored. So:

    "actually nah don't add it"   ->  reject misses (message starts with "actually")
                                 ->  approve matches the substring "add it"
                                 ->  Resolution.approved -> a REAL calendar write against a refusal.

Verified live for "actually nah don't add it", "please don't put it on there", and "yeah no don't add
it". A student saying no got a write. The defect was not the anchor; it was that approval could win by
matching a fragment of a sentence whose meaning was negative.

THE RULE NOW: strict precedence, evaluated over the whole message.

    1. rejection / cancellation   -> rejected   (dominates everything)
    2. ambiguity or a changed request -> ambiguous (never executes)
    3. approval                    -> approved
    4. otherwise                   -> unrelated (decision stays open)

A negation ANYWHERE in the student's own text prevents approval unless the language genuinely reverses
it ("no wait yes, do it"). "don't add it" can never approve merely because it contains "add it": the
approval scan runs only over text with negated spans removed.

TRUST BOUNDARY. This resolver may only ever be given text the STUDENT authored. Quoted flyer text,
forwarded email bodies, attachment OCR and model-written summaries are EVIDENCE, never authorization —
otherwise a forwarded email containing "yes add it" could authorize a write. `trusted_reply_text()`
enforces that by stripping quoted/forwarded regions before resolution.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import Enum


class Resolution(str, Enum):
    approved = "approved"
    rejected = "rejected"
    ambiguous = "ambiguous"      # a reply about the decision, but not a clear yes/no -> ask one question
    unrelated = "unrelated"      # not about the decision at all -> leave it open, don't approve


# --- normalization -----------------------------------------------------------------------------------

_CURLY = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}


def normalize(text: str | None) -> str:
    """Fold the ways a phone actually sends text: curly apostrophes, unicode punctuation, caps, repeated
    punctuation. Done BEFORE any matching so "Don’t!!" and "dont" resolve identically."""
    t = unicodedata.normalize("NFKC", text or "")
    for k, v in _CURLY.items():
        t = t.replace(k, v)
    t = t.lower()
    # Drop apostrophes entirely so every contraction folds to one form: "don't"/"don’t"/"dont" all
    # become "dont". Without this, \bdont\b silently fails on the most common way a refusal is typed,
    # which is exactly the class of miss that caused the incident.
    t = t.replace("'", "")
    t = re.sub(r"[!?.,;:]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Quoted / forwarded / attachment regions are EVIDENCE, never authorization. Everything from a quote
# marker onward is dropped before the resolver sees the text.
_QUOTE_CUT = re.compile(
    r"(^\s*>.*$)"                                     # classic quoted line
    r"|(-{2,}\s*forwarded message\s*-{2,})"
    r"|(^\s*on .{0,80}\bwrote:\s*$)"
    r"|(\bfrom:\s*.+\bsent:\s*)",
    re.IGNORECASE | re.MULTILINE)


def trusted_reply_text(text: str | None) -> str:
    """The student's OWN words only. A forwarded email that says "yes add it" must not authorize a
    write, so quoted and forwarded regions are removed before resolution."""
    raw = text or ""
    m = _QUOTE_CUT.search(raw)
    if m:
        raw = raw[:m.start()]
    return raw


# An inline quotation of someone ELSE's words. `trusted_reply_text` strips quoted REGIONS (reply blocks,
# forwarded headers); this catches the other shape — a student pasting a sentence mid-message. Measured:
# 'coach replied "yes add it" — what does he mean' authorized a write, because the quoted affirmative was
# read as the student's own.
_INLINE_QUOTE = re.compile(r"[\"“”][^\"“”]{1,200}[\"“”]")


def strip_inline_quotes(text: str | None) -> str:
    """Someone else's sentence, pasted mid-message, removed.

    Lives here rather than in each caller because the AFFIRMATIVE path and only the affirmative path may
    use it: removing a quote can turn a yes into a non-yes, never a no into a yes. Two copies of that
    asymmetry would eventually disagree, and the direction of the disagreement is the unsafe one.
    """
    return _INLINE_QUOTE.sub(" ", text or "")


def trusted_digest(text: str | None) -> str:
    """A stable identity for the student's own words in ONE turn.

    Exists so a reading DERIVED from a turn's trusted text cannot later be applied to different text. A
    caller computes a semantic proposal over the words it separated; the boundary recomputes this over the
    words IT was handed and refuses the proposal when they disagree. Without it, a proposal built from a
    correctly separated message could be spent against a message that still has an attacker's forwarded
    email joined onto it, and the separation would have bought nothing.
    """
    return hashlib.sha256(normalize(trusted_reply_text(text)).encode("utf-8")).hexdigest()


# --- lexicon -----------------------------------------------------------------------------------------

# Negation/cancellation tokens. Matched ANYWHERE in the student's text, not just at the start — that
# anchoring is precisely what let "actually nah don't add it" through.
_NEG = re.compile(
    r"\b(?:no|nope|nah+|naw|nvm|dont|do not|didnt|cancel|cancelled|stop|abort|undo|"
    r"never ?mind|forget it|leave it|skip|scratch that|not yet|not now|hold off|"
    r"no thanks?|nty|im good|i'?m good)\b")

# A genuine reversal must come AFTER the negation it undoes: "no wait yes do it". Position matters —
# an earlier "actually" must never reverse a later refusal, or "actually nah dont add it" reads as a
# yes. Applied ONLY to the text following the last negation token.
_REVERSAL_TAIL = re.compile(r"\b(?:wait|actually|jk|just kidding|scratch that|on second thought)\b"
                            r"[^.]{0,15}?\b(?:yes|yea|yeah|yep|do it|go ahead|add it)\b")

# Bare affirmatives and action directives.
_APPROVE = re.compile(
    r"\b(?:y(?:es|ea|eah|up|uh|e|a)+|yep|yup|ya|yah|sure|ok(?:ay)?|kk|word|bet|fs|fr|"
    r"please do|pls do|go for it|do it|send it|absolutely|definitely|for sure|"
    r"sounds? good|lets? (?:go|do it)|go ahead)\b"
    r"|\b(?:add|put|schedule|save|set|throw|pop|stick|slot|drop|book|send|move|delete)\s+"
    r"(?:it|this|that|ts|em|them)\b"
    r"|\bset (?:it|this|that|ts)? ?up\b|\bsetup\b")

# On-topic but unsettled -> ask ONE question, never execute.
_AMBIGUOUS = re.compile(
    r"\b(?:maybe|idk|i dont know|not sure|dunno|hold on|wait|hmm+|depends|lemme think|"
    r"thinking|which cal(?:endar)?|what cal(?:endar)?|what time|not that one)\b"
    r"|\bbut\b|\binstead\b|\bfirst\b|\bchange the\b")

# A negated span: the negation token plus what it governs, so approval never reads inside it.
_NEGATED_SPAN = re.compile(
    r"\b(?:no|nope|nah+|naw|dont|do not|never ?mind|cancel|stop|skip|leave it|forget it)\b"
    r"[^,;]{0,40}")


def _strip_negated(t: str) -> str:
    """Remove negated spans so an approval pattern cannot match a fragment INSIDE a refusal.
    "don't add it" -> "" (the "add it" lives inside the negated span and is unreachable)."""
    return _NEGATED_SPAN.sub(" ", t)


def resolve_approval(text: str | None) -> Resolution:
    """Resolve a reply against an OPEN yes/no decision, over TRUSTED text only.

    Precedence is absolute: rejection > ambiguity > approval > unrelated. Approval is evaluated only
    over text with negated spans removed, so an action verb inside a refusal can never authorize.
    """
    t = normalize(trusted_reply_text(text))
    if not t:
        return Resolution.unrelated

    negations = list(_NEG.finditer(t))
    if negations:
        after_last = t[negations[-1].end():]            # only a LATER affirmative can undo a refusal
        if not _REVERSAL_TAIL.search(after_last):
            return Resolution.rejected                  # 1. refusal dominates, wherever it appears
        # An explicit reversal ("no wait yes do it") already contains the affirmative, so it resolves
        # here. Falling through would hit the ambiguity check on the reversal's own cue word ("wait")
        # and stall a decision the student just plainly made.
        return Resolution.approved

    if _AMBIGUOUS.search(t):
        return Resolution.ambiguous                     # 2. never execute on a maybe or a change

    if _APPROVE.search(_strip_negated(t)):
        return Resolution.approved                      # 3. a clean, un-negated yes

    return Resolution.unrelated                         # 4. leave the decision open
