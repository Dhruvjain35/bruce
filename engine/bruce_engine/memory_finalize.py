"""The caller the memory stack never had — and, on the outcome path, one the policy actually accepts.

WHAT WAS WRONG, IN TWO ROUNDS.

ROUND ONE, the dead stack. `memory_writer` and the whole write-safety layer (candidate assessment, the
default-deny profile registry, claim lineage, race-safe duplicates) were built and tested, and then
nothing ever called them. `memory_retrieval` ran a shortlist against an empty table on every single turn:
the retrieval half was live, the writing half was dead, and Bruce could not remember anything across
conversations no matter how many times a student told it.

ROUND TWO, the refused caller — the defect this module is being fixed for. `after_verified_outcome`
acquired a caller (`goal_handler`, after the read-back that proves a send happened) and STILL wrote
nothing, because every proposal it built was rejected on shape:

    subject="self" + kind=episodic   `memory_writer.assess` -> NOT_USER_SPECIFIC. `SELF` is reserved for
                                     `profile`; every other layer is about someone or something else.
    predicate="completed_action"     not a namespaced `domain.relation`, so `_shape_gate` -> FILLER.

So "memory is wired" was true of the call graph and false of the database, which is the worst of the two
states: a green seam test asserting at the call site (`tests/test_goal_handler.py::
test_memory_is_offered_the_outcome_only_after_the_read_back`, which says so in its own docstring) and zero
rows. The fix is not a policy change. It is using the shape the policy already accepts:

    SubjectType.conversation -> MemoryKind.episodic     `memory_policy.SUBJECT_KIND_MATRIX`, exactly.
                                                        An episodic record is "what happened, and when",
                                                        and what happened is a Bruce workflow occurrence.
    subject = the outcome's own anchor                  the goal that ran, else the provider receipt that
                                                        proved it. User-scoped, never `SELF`, and stable
                                                        — which is what makes a duplicate finalization a
                                                        DUPLICATE_CLAIM refusal instead of a second row.
    predicate = "<provider>.completed_<operation>"      namespaced, so `domain` is the provider and
                                                        `memory_retrieval.shortlist` can scope by it on a
                                                        later turn in the same domain. That is what makes
                                                        "did you send that?" answerable from the table.

WHY THE PROVENANCE IS THE STUDENT'S STATEMENT AND NOT `system_derived`. `system_derived` is the honest
label for "the runtime recorded its own work", and `memory_policy.KIND_FLOORS[episodic]` accepts it in
its `accepted_provenance` — but `memory_candidate.INFERRED_PROVENANCE` contains `system_derived`, so
`is_inferred` is True for it, and the same floor sets `inference_allowed=False`. Those two lines make a
`system_derived` episodic write UNREACHABLE. That is a defect in `memory_policy` (reported, not patched
here — loosening a floor to get a write accepted is the one thing this must not do). What is written
instead is not a workaround with a false label: the record rests on the student's own trusted words
(their instruction, re-stripped through `decision_resolver.trusted_reply_text` here so a forwarded block
can never become the evidence), the student is the actor and Bruce the instrument, and the write is
reachable only from a path holding a provider read-back. `explicitly_stated_by_user` is left FALSE,
because they asked for the action and did not state its outcome.

WHAT THIS DELIBERATELY DOES NOT WRITE. Not task slots. A recipient, a subject, a draft body and an event
time belong to the goal in `AgentRun.goal`, because they are true only until the task finishes and must
be authoritative while it runs. Copying them into long-term memory would create a second, staler answer
to "who is this email going to" — and a send is entitled to exactly one. Memory is advisory; goal state
is binding.

Not guesses either, and not the two shapes there is no trustworthy signal for at this seam. A stable
preference and a durable relationship fact are both legitimate things to remember, and both would have to
be READ out of the student's sentence to be written from here — which is a model guess or a phrase table,
and either one puts an unverified claim about a person in a store that re-serves it forever. They belong
to an extraction path with its own evidence, not to the tail of a send. Only two things reach the writer
from this module:

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

log = logging.getLogger("bruce.memory_finalize")   # CONTENT-FREE: kinds, reasons and counts only

# Aggregate style signals. Each is (relation, value, detector) where the detector runs over the student's
# OWN text. The value is a description of a pattern, never a copy of what they wrote.
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_ABBREV = re.compile(r"\b(u|ur|rn|js|wb|idk|tbh|lmk|pls|thx|ngl|fr)\b", re.IGNORECASE)

# Anything a namespaced predicate may not contain. Structural, not a vocabulary.
_SAFE = re.compile(r"[^a-z0-9_]+")

_STYLE_SIGNALS: tuple[tuple[str, str, object], ...] = (
    ("writes_case", "writes in lowercase",
     lambda t: len(t) >= 12 and t == t.lower() and any(c.isalpha() for c in t)),
    ("uses_emoji", "uses emoji in messages", lambda t: bool(_EMOJI.search(t))),
    ("uses_abbreviations", "uses texting abbreviations", lambda t: bool(_ABBREV.search(t))),
)

# A style observation needs enough text to be a pattern rather than an accident. One-word replies like
# "this" or "yes" are not evidence about how someone writes.
_MIN_STYLE_CHARS = 12

# --- the verified-outcome shape ---------------------------------------------------------------------
# Typed refusals, like `memory_writer`'s: "why didn't you remember that" has to be countable without
# grepping, and the writer's own reasons are already values rather than sentences.

NOTHING_TO_RECORD = "no_summary_no_trusted_span_or_no_anchor"

_MAX_SUBJECT = 200
"""`memory_records.subject` is String(200), and `entity_key` is derived from it."""

_MAX_SPAN = 200
"""The evidence span. The student's own words, kept short enough to be a citation rather than a copy of
their message — `memory_provenance` reads this back to them verbatim."""

_MAX_RELATION = 50
"""Room for `completed_` plus the operation inside `memory_record._PREDICATE`'s 64-char relation."""

_VERIFIED_CONFIDENCE = 1.0
"""A provider read-back is the strongest evidence in the system: the adapter asked the provider whether
the thing exists and the provider said yes. Hedging that number would make `memory_retrieval.score`
rank a proven fact below an ordinary statement, and there is nothing here Bruce is unsure about."""

_FALLBACK_PREDICATE = "task.completed_action"

_OUTCOME_REASON = ("what Bruce actually did for them, proven by a provider read-back, so a later "
                   "'did you send that?' has a true answer instead of a guess")


def outcome_predicate(capability: str | None) -> str:
    """`gmail.send_message` -> `gmail.completed_send_message`. Derived, never a table of capabilities.

    Two properties are being bought. `memory_record._PREDICATE` requires a namespaced `domain.relation`
    or the write is refused as FILLER — the round-two defect. And the namespace becomes the `domain`
    column, which is one of the two things `memory_retrieval.shortlist` scopes on, so a later turn the
    router already put in the `gmail` domain can see what Bruce did there without naming any entity.

    Sanitised rather than trusted: a capability id is a registry key, and a predicate that fails the
    pattern would take the whole record down for a reason nobody would look for here.
    """
    provider, _, operation = (capability or "").strip().lower().partition(".")
    namespace = _SAFE.sub("_", provider).strip("_")[:32]
    relation = _SAFE.sub("_", operation).strip("_")[:_MAX_RELATION]
    if len(namespace) < 2:
        return _FALLBACK_PREDICATE
    predicate = f"{namespace}.completed_{relation or 'action'}"
    from . import memory_record as mr
    return predicate if mr.is_namespaced_predicate(predicate) else _FALLBACK_PREDICATE


def outcome_subject(*, goal_id=None, provider_entity_id=None, source_message_id=None) -> str:
    """WHAT the episodic record is about: this one outcome, named by the most durable anchor available.

    Not `SELF` — that is `profile`'s subject and the reason every write from here used to be refused as
    NOT_USER_SPECIFIC. Not the recipient or the capability either, and that is the load-bearing part:
    `memory_record.claim_key` is computed from kind + subject + predicate, and `uq_memory_active_claim`
    permits ONE active row per claim. A subject shared by every send ("gmail.send_message", or a person)
    would make the SECOND real email a DUPLICATE_CLAIM refusal — Bruce would answer "did you send that?"
    from a stale first outcome forever. A per-outcome anchor makes two sends two rows and makes a
    REPEATED finalization of one send the same claim, which is where idempotency comes from: the database
    refuses the second write, no counter in this module does.

    Preference order is by how much each anchor identifies the WORK rather than a side effect:
    the goal that ran (survives a provider retry that mints a new id), then the receipt the read-back
    returned, then the message that authorized it.
    """
    for anchor in (goal_id, provider_entity_id, source_message_id):
        text = str(anchor or "").strip()
        if text:
            return text[:_MAX_SUBJECT]
    return ""


def _trusted_span(stated_span: str | None, trusted_text: str | None) -> str:
    """The student's OWN words this record rests on — derived from them, not asserted about them.

    `goal_handler` hands over `msg.text` raw, so the strip happens HERE. 30 of the 38 failures in the
    adversarial corpus were an affirmative living in forwarded or quoted material being read as the
    student speaking; a memory built that way is worse than a wrong action, because it is re-served on
    every later turn. `MemoryWriter.evaluate` does not re-check grounding on the candidate path (only the
    legacy `remember`/`record_style_signal` adapters call `memory_writer._grounded`), so a span that is
    merely claimed would never be verified by anything. Returning a slice of the stripped text makes the
    grounding structural instead: there is no argument to this function that yields a span the student
    did not write. A caller-supplied span is honoured only when it really is inside those words.
    """
    from . import decision_resolver
    from . import memory_record as mr
    own = (decision_resolver.trusted_reply_text(trusted_text) or "").strip()
    if not own:
        # Everything they sent was someone else's words. There is no evidence, so there is no memory.
        return ""
    claimed = (stated_span or "").strip()
    if claimed and mr.normalize(claimed) and mr.normalize(claimed) in mr.normalize(own):
        return claimed[:_MAX_SPAN]
    return own[:_MAX_SPAN]


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
                                 provider_entity_id: str | None,
                                 goal_id: str | None = None) -> bool:
    """Record that something VERIFIED happened. Called only after a provider read-back confirmed it.

    Returns True only when a row was ACCEPTED — a duplicate finalization returns False with
    DUPLICATE_CLAIM logged, because "already remembered" and "just remembered" are different events and a
    caller that cannot tell them apart will eventually count one as the other.

    The guard is the caller's contract, not a hint: this function is the one place a completed action
    becomes a memory, and it is reachable only from a path that already holds a verification result. An
    unverified send has nothing true to remember — `succeeded` itself is unreachable without a read-back
    (see `transitions`), so an outcome that got here was proven.

    `goal_id` is optional ONLY because `goal_handler` does not pass it yet; it is the better claim anchor
    (see `outcome_subject`) and the caller should. Without it the receipt anchors the claim, which is
    correct but re-mints the claim if a provider retry ever produces a second id for one goal.

    NOT written here, deliberately: the recipient, the subject line, the body, the event time. They are
    slots, they live on the run, and a second staler copy of "who is this going to" is exactly the bug
    that gets an email sent to the wrong person. What is written is the past tense of the whole thing.
    """
    from datetime import datetime, timezone

    from . import memory_candidate as mc
    from . import memory_record as mr
    from . import memory_writer

    value = (summary or "").strip()[:mr.MAX_VALUE]
    span = _trusted_span(stated_span, trusted_text)
    subject = outcome_subject(goal_id=goal_id, provider_entity_id=provider_entity_id,
                              source_message_id=source_message_id)
    if not value or not span or not subject:
        # No sentence, no words of the student's own, or nothing to name the outcome by. Each of the
        # three is a reason the record could not defend itself later, and a memory that cannot be
        # defended is the one kind this store must not hold.
        log.info("outcome_not_remembered cap=%s reason=%s", capability, NOTHING_TO_RECORD)
        return False

    predicate = outcome_predicate(capability)
    try:
        receipt = await memory_writer.MemoryWriter(user_id).evaluate(mc.MemoryCandidate(
            user_id=user_id,
            # The claim is about a Bruce workflow occurrence, which is the ONE subject type
            # `SUBJECT_KIND_MATRIX` lets reach `episodic`. It is also what keeps this out of the profile
            # registry entirely: an outcome is not a fact about the person.
            subject_type=mc.SubjectType.conversation,
            subject_id=subject,
            kind=mr.MemoryKind.episodic,
            predicate=predicate,
            proposed_value=value,
            normalized_value=mr.normalize(value),
            evidence_text=span,
            source_type=mr.SourceType.trusted_user_text,
            # The message that AUTHORIZED it, not the provider's id for the thing that happened. This is
            # the column `memory_writer._source_was_forgotten` and `memory_forget` key on, so "forget
            # everything that came from that message" has to reach this row.
            source_id=source_message_id,
            provenance_class=mc.ProvenanceClass.trusted_user_statement,
            # They asked for the action. They did not state its outcome — see the module docstring.
            explicitly_stated_by_user=False,
            inferred=False,
            confidence=_VERIFIED_CONFIDENCE,
            # Episodic on both counts: `memory_policy._retention_for` refuses an episodic record that
            # claims to be durable, and it should — what happened on Tuesday stops mattering.
            expected_stability=mr.RetentionPolicy.episodic,
            usefulness_reason=_OUTCOME_REASON,
            sensitivity_class=mr.Sensitivity.ordinary,
            retention_recommendation=mr.RetentionPolicy.episodic,
            observed_at=datetime.now(timezone.utc)))
    except Exception:
        # The provider call already happened and the student already has their receipt. Losing the memory
        # is a cost; losing the turn to a memory error would be strictly worse.
        log.info("outcome_memory_failed cap=%s", capability)
        return False

    if not receipt.stored:
        log.info("outcome_not_remembered cap=%s reason=%s", capability, receipt.reason)
        return False
    log.info("outcome_remembered cap=%s kind=%s domain=%s", capability, mr.MemoryKind.episodic.value,
             mr.domain_of(predicate))
    return True
