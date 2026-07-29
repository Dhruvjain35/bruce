"""The caller the memory stack never had.

WHAT WAS WRONG. `memory_writer` and the whole write-safety layer (candidate assessment, the default-deny
profile registry, claim lineage, race-safe duplicates) were built and tested, and then nothing ever
called them: `grep -rn "memory_writer\." bruce_engine | grep -v memory_` returns nothing. So
`memory_retrieval` ran a shortlist against an empty table on every single turn — the retrieval half was
live, the writing half was dead, and Bruce could not remember anything across conversations no matter how
many times a student told it.

WHAT THIS DELIBERATELY DOES NOT WRITE. Not task slots. A recipient, a subject, a draft body and an event
time belong to the goal in `AgentRun.goal`, because they are true only until the task finishes and must
be authoritative while it runs. Copying them into long-term memory would create a second, staler answer
to "who is this email going to" — and a send is entitled to exactly one. Memory is advisory; goal state
is binding.

Not guesses either. Only two things reach the writer from here:

  1. AGGREGATE style observations, through `record_style_signal`, which hard-codes its kind so an
     observation about how someone writes can never become a claim about who they are. The values are
     aggregates ("writes in lowercase") rather than copied phrases, because `memory_policy._style_gate`
     rejects a style value that is a verbatim span of the student's message — a phrase copy is evidence,
     not a pattern.
  2. VERIFIED outcomes, after a provider read-back proved the thing actually happened. "I emailed my
     coach on the 29th" is durable and true; "I am about to email my coach" is a slot.

Every call is fault-isolated. A memory failure must never cost the student their reply — the turn already
succeeded by the time this runs, and losing it to a write error would be a strictly worse bug than
forgetting.
"""

from __future__ import annotations

import logging
import re

from uuid import UUID

log = logging.getLogger("bruce.memory_finalize")   # CONTENT-FREE: kinds and counts only

# Aggregate style signals. Each is (relation, value, detector) where the detector runs over the student's
# OWN text. The value is a description of a pattern, never a copy of what they wrote.
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_ABBREV = re.compile(r"\b(u|ur|rn|js|wb|idk|tbh|lmk|pls|thx|ngl|fr)\b", re.IGNORECASE)

_STYLE_SIGNALS: tuple[tuple[str, str, object], ...] = (
    ("writes_case", "writes in lowercase",
     lambda t: len(t) >= 12 and t == t.lower() and any(c.isalpha() for c in t)),
    ("uses_emoji", "uses emoji in messages", lambda t: bool(_EMOJI.search(t))),
    ("uses_abbreviations", "uses texting abbreviations", lambda t: bool(_ABBREV.search(t))),
)

# A style observation needs enough text to be a pattern rather than an accident. One-word replies like
# "this" or "yes" are not evidence about how someone writes.
_MIN_STYLE_CHARS = 12


async def after_turn(user_id: UUID, *, trusted_text: str | None, source_message_id: str | None) -> int:
    """Record aggregate style observations from the student's OWN words. Returns how many were written.

    `trusted_text` must already be the authored text — the caller passes `InputEnvelope.authorizing_text()`
    or the equivalent, never a join with quoted or OCR'd material. A forwarded email's voice is not the
    student's voice, and learning to write like a stranger because they pasted one is a real failure mode.
    """
    text = (trusted_text or "").strip()
    if len(text) < _MIN_STYLE_CHARS:
        return 0
    written = 0
    for relation, value, detects in _STYLE_SIGNALS:
        try:
            if not detects(text):
                continue
            from . import memory_writer
            rec = await memory_writer.record_style_signal(
                user_id=user_id, relation=relation, value=value,
                trusted_text=text,
                # The span is the evidence the observation rests on; the VALUE above is the aggregate.
                # Keeping them different is what gets past the style gate, and is also just honest.
                stated_span=text[:120],
                source_message_id=source_message_id)
            written += 1 if rec is not None else 0
        except Exception:
            # One bad signal must not cost the others, and none of them may cost the turn.
            log.info("style_signal_failed relation=%s", relation)
    if written:
        log.info("style_signals_written n=%s", written)
    return written


async def after_verified_outcome(user_id: UUID, *, capability: str, summary: str,
                                 trusted_text: str | None, stated_span: str | None,
                                 source_message_id: str | None,
                                 provider_entity_id: str | None) -> bool:
    """Record that something VERIFIED happened. Called only after a provider read-back confirmed it.

    The guard is the caller's contract, not a hint: this function is the one place a completed action
    becomes a durable memory, and it is reachable only from a path that already holds a verification
    result. An unverified send has nothing true to remember — `succeeded` itself is unreachable without
    read-back (see transitions.py), so an outcome that got here was proven.
    """
    if not summary or not (trusted_text or "").strip():
        return False
    try:
        from . import memory_record, memory_writer

        p = memory_writer.MemoryProposal(
            user_id=user_id,
            kind=memory_record.MemoryKind.episodic,
            subject="self",
            predicate="completed_action",
            value=summary,
            reason_it_matters="what Bruce actually did for them, so a later 'did you send that?' has a "
                              "true answer instead of a guess",
            trusted_text=trusted_text or "",
            stated_span=(stated_span or (trusted_text or ""))[:200],
            source_message_id=source_message_id,
        )
        rec = await memory_writer.remember(p, reason_it_matters=None)
        if rec is not None:
            log.info("outcome_remembered capability=%s entity_present=%s", capability,
                     bool(provider_entity_id))
        return rec is not None
    except Exception:
        log.info("outcome_memory_failed capability=%s", capability)
        return False
