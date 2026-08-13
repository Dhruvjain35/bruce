"""DEFECT-13 — what the student approves must be what actually ships.

THE DIVERGENCE, executed against the real code before this fix:

    raw composer body   "Hi Professor Chen,\\n\\nI'd be happy to talk about the extension — Tuesday or
                         Wednesday works for me.\\n\\nThanks,\\nSam"
    SHOWN to the student " talk about the extension, Tuesday or Wednesday works for me."
    SENT to the professor "I'd be happy to talk about the extension — Tuesday or Wednesday works for me."

The proposal reply is rendered through `enforce_no_dashes` and then
`messaging_outbound.gate_outbound_text`, which strips `PROHIBITED_PHRASES`, collapses `[ \\t]{2,}` and
rewrites em dashes. The `body` slot on the goal keeps the RAW composer output, and that raw string is
what `gmail_send_args` hands the adapter.

So the student reads one thing and the professor receives another. Note the direction: the gate DELETES
text from the copy the student sees while that text still ships. "I'd be happy to" is invisible to the
student and lands in the professor's inbox.

WHY THIS IS AN INTEGRITY DEFECT AND NOT A COSMETIC ONE. The most defensible property in this codebase is
that an approval means something — a refusal revokes consent everywhere, a receipt is verified by reading
the message back. An approval that does not bind the bytes it displayed weakens the thing the whole
authorization corpus exists to protect. The execution fingerprint compounds it: it hashes a
whitespace-FLATTENED form (`authorization_evidence._normalize_value`), so it pins neither the shown bytes
nor the sent bytes exactly.

IT IS NOT AN EM-DASH EDGE CASE. The composer prompt already asks the model to avoid em dashes, so that
trigger is model-dependent. `[ \\t]{2,}` is not: any ordinary double space after a sentence shows
single-spaced and ships double-spaced, on every proposal, deterministically. `self_hosted_imessage` is
always in `_PLAIN_TEXT_CHANNELS`, so the gate fires on 100% of proposals.

THE FIX IS AT COMPOSE TIME, NOT APPROVE TIME. Clean the drafted values once, before they become slots.
Then shown == stored == fingerprinted == sent, and the outbound gate becomes a no-op on them because it
is idempotent. Cleaning at approve time would leave the stored value dirty and the fingerprint would
still bind something the student never saw.
"""

from __future__ import annotations

import asyncio

import pytest

from bruce_engine import goal_handler
from bruce_engine.messaging import ChannelKind
from bruce_engine.messaging_outbound import gate_outbound_text

PLAIN = ChannelKind.self_hosted_imessage.value

# A body carrying BOTH triggers: a prohibited phrase (deleted from the shown copy, kept in the sent one)
# and an em dash (rewritten in the shown copy, kept in the sent one). Paragraph breaks are load-bearing:
# a fix that flattens them would wreck every real email.
DIRTY_BODY = ("Hi Professor Chen,\n\n"
              "I'd be happy to talk about the extension — Tuesday or Wednesday works for me.\n\n"
              "Thanks,\nSam")
DIRTY_SUBJECT = "Extension request — CS 121"


def _shown(text: str) -> str:
    """Exactly what the student reads: the transform the proposal reply passes through."""
    return gate_outbound_text(text, PLAIN)


def test_the_defect_is_real_at_the_gate_itself():
    """Anchor the premise. If these ever stop differing, the rest of this suite proves nothing."""
    assert _shown(DIRTY_BODY) != DIRTY_BODY
    assert "i'd be happy to" not in _shown(DIRTY_BODY).lower(), "the gate should delete the filler"
    assert "—" not in _shown(DIRTY_BODY)
    assert "i'd be happy to" in DIRTY_BODY.lower(), "...and the raw body should still contain it"


def test_the_hygiene_helper_makes_the_gate_a_no_op():
    """THE PROPERTY. After cleaning, showing the value changes nothing — so stored == shown == sent."""
    cleaned = goal_handler.hygienic_draft_value(DIRTY_BODY)
    assert _shown(cleaned) == cleaned, (
        f"the gate still alters the cleaned body, so the student would read something else again:\n"
        f"  cleaned={cleaned!r}\n  shown  ={_shown(cleaned)!r}")


def test_paragraph_breaks_survive_cleaning():
    """An email is not one line. `gate_outbound_text` collapses only [ \\t]{2,}, never newlines, and the
    fix must not be sloppier than the thing it is matching — `email_quality.scrub`'s \\s{2,} would
    destroy every paragraph break."""
    cleaned = goal_handler.hygienic_draft_value(DIRTY_BODY)
    assert cleaned.count("\n\n") == DIRTY_BODY.count("\n\n") == 2
    assert cleaned.startswith("Hi Professor Chen,")
    assert cleaned.rstrip().endswith("Sam")


def test_the_model_independent_trigger_is_closed():
    """The em dash is model-dependent; a double space is not. This is the case that fires every day."""
    body = "I missed lab today.  Could I come to office hours?"
    cleaned = goal_handler.hygienic_draft_value(body)
    assert "  " not in cleaned
    assert _shown(cleaned) == cleaned


def test_clean_text_is_left_alone():
    """Hygiene must be idempotent and must not rewrite a body that was already fine."""
    clean = "Hi Professor Chen,\n\nCould I come to office hours on Tuesday?\n\nThanks,\nSam"
    assert goal_handler.hygienic_draft_value(clean) == clean
    assert goal_handler.hygienic_draft_value(goal_handler.hygienic_draft_value(clean)) == clean


def test_no_line_is_left_starting_with_a_space():
    """Removing a phrase mid-line leaves the next line starting with a space ("\\n\\n talk about…"). The
    outbound gate cannot see it — it collapses only RUNS of two or more, and `.strip()` reaches the ends
    of the string, not the start of every line. A draft the student is asked to approve should not look
    damaged, so the helper tidies it. Asserted separately from gate-stability because a value can be
    perfectly gate-stable and still look broken."""
    cleaned = goal_handler.hygienic_draft_value(DIRTY_BODY)
    offenders = [ln for ln in cleaned.split("\n") if ln[:1] in (" ", "\t")]
    assert not offenders, f"lines left starting with whitespace: {offenders!r}"
    # ...and tidying must not have cost gate-stability.
    assert _shown(cleaned) == cleaned


def test_empty_and_none_are_safe():
    assert goal_handler.hygienic_draft_value("") == ""
    assert goal_handler.hygienic_draft_value(None) is None


# --- the seam that actually stores the slot ------------------------------------------------------------

class _Composer:
    def __init__(self, drafted):
        self._drafted = drafted
        self.calls = 0

    async def compose(self, **_kw):
        self.calls += 1
        return dict(self._drafted)


class _Msg:
    text = "email professor chen about an extension"


class _Octx:
    user_id = None
    pmid = "pmid-1"
    decision = None
    conversation_id = "conv-1"
    msg = _Msg()
    recent = ()


class _Step:
    missing = ("subject", "body")


def _compose_slots(monkeypatch, drafted):
    """Drive the REAL _compose and capture what it would persist, without a database."""
    captured = {}

    async def _fake_ensure_goal(user_id, *, capability, conversation_id, slots_in, turn_index, decision):
        captured.update(slots_in)
        return "sentinel-view"

    from bruce_engine import goal_runtime
    monkeypatch.setattr(goal_runtime, "ensure_goal", _fake_ensure_goal)
    monkeypatch.setattr(goal_handler, "compose_objective", lambda *a, **k: "objective")
    monkeypatch.setattr(goal_handler, "_recent_context", lambda *a, **k: "")

    handler = goal_handler.GoalHandler(composer=_Composer(drafted))

    class _View:
        run_id = "run-1"
        slots = {}

    asyncio.run(handler._compose(_Octx(), _View(), "gmail.send_message", _Step(),
                                 turn_index=1, tz="America/Chicago"))
    return captured


def test_the_stored_slot_is_already_what_the_student_will_read(monkeypatch):
    """THE DEFECT, at the seam that causes it. The value persisted onto the goal — the same value
    `gmail_send_args` later hands the adapter, and the same one the fingerprint binds — must already be
    the bytes the student is shown."""
    stored = _compose_slots(monkeypatch, {"subject": DIRTY_SUBJECT, "body": DIRTY_BODY})

    assert set(stored) == {"subject", "body"}
    for name in ("subject", "body"):
        value = stored[name].value
        assert _shown(value) == value, (
            f"the stored {name} is not what the student reads:\n"
            f"  stored={value!r}\n  shown ={_shown(value)!r}\n"
            f"That difference is what ships to the recipient.")


def test_the_prohibited_phrase_does_not_reach_the_provider(monkeypatch):
    stored = _compose_slots(monkeypatch, {"subject": DIRTY_SUBJECT, "body": DIRTY_BODY})
    assert "i'd be happy to" not in stored["body"].value.lower(), (
        "the student never saw this phrase and the professor would have received it")


def test_no_em_dash_survives_into_a_stored_slot(monkeypatch):
    stored = _compose_slots(monkeypatch, {"subject": DIRTY_SUBJECT, "body": DIRTY_BODY})
    assert "—" not in stored["body"].value
    assert "—" not in stored["subject"].value


def test_cleaning_does_not_change_which_slots_are_written(monkeypatch):
    """The `wanted` filter is a safety property of its own — a composer returning a recipient must not be
    able to invent an address. Hygiene must not disturb it."""

    class _OnlyBody:
        missing = ("body",)

    captured = {}

    async def _fake_ensure_goal(user_id, *, capability, conversation_id, slots_in, turn_index, decision):
        captured.update(slots_in)
        return "sentinel-view"

    from bruce_engine import goal_runtime
    monkeypatch.setattr(goal_runtime, "ensure_goal", _fake_ensure_goal)
    monkeypatch.setattr(goal_handler, "compose_objective", lambda *a, **k: "objective")
    monkeypatch.setattr(goal_handler, "_recent_context", lambda *a, **k: "")

    handler = goal_handler.GoalHandler(
        composer=_Composer({"body": DIRTY_BODY, "recipient": "attacker@example.com"}))

    class _View:
        run_id = "run-1"
        slots = {}

    asyncio.run(handler._compose(_Octx(), _View(), "gmail.send_message", _OnlyBody(),
                                 turn_index=1, tz="America/Chicago"))
    assert set(captured) == {"body"}, f"a slot outside `missing` was written: {set(captured)}"
