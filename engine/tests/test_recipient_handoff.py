"""THE RECIPIENT, from the student's sentence to the pending Decision — the handoff that dropped it.

MEASURED LIVE, 2026-07-30, on the founder's own account. One message:

    email dhruvhydrox@gmail.com with subject bruce staging test b515b40 and body this is a
    controlled test send from bruce staging. commit b515b40. nothing else was sent.

The model read it perfectly. It labelled the address `type='email'`. The slot is called `recipient`.
`goal_handler.entity_slots` matched an entity type to a slot by SHARED WORD-TOKEN, "email" and
"recipient" share none, and the address was dropped between the model's output and the goal. Bruce
created a `send_email` goal with `slots=['subject','body']`, found `recipient` missing, and asked "i still
need who this should go to." — for the address in the sentence it was answering.

That is the transcript's signature failure reached by a THIRD route. The first was a free-text capability
("sending messages"); the second was a capability in the wrong namespace (`email.send_message`); this one
is an ENTITY in the wrong namespace. Every one of them is the model's vocabulary meeting the system's.

TRACED, NOT GUESSED. The stages below are asserted individually — trusted text, model entities, the goal
patch, the merge, the persisted blob, the Decision's own arguments — so the first point of loss is a test
result rather than an opinion. The pre-fix behaviour is pinned too: `entity_slots` on a token match alone
still drops it, which is what makes the alias the thing doing the work.
"""

from __future__ import annotations

import pytest

from bruce_engine import goal_handler, goal_slots
from bruce_engine.goal_slots import GoalKind, Source

# The EXACT live message, byte for byte.
LIVE_TEXT = ("email dhruvhydrox@gmail.com with subject bruce staging test b515b40 and body "
             "this is a controlled test send from bruce staging. commit b515b40. nothing else was sent.")
ADDRESS = "dhruvhydrox@gmail.com"
SUBJECT = "bruce staging test b515b40"
BODY = "this is a controlled test send from bruce staging. commit b515b40. nothing else was sent."


class _E:
    """An extracted entity, shaped like `conversation_contract.ExtractedEntity`."""

    def __init__(self, ty, val, normalized=None):
        self.type, self.value, self.normalized = ty, val, normalized


# The EXACT entities the live model emitted, read back out of `conversation_turns.decision`.
LIVE_ENTITIES = [_E("email", ADDRESS), _E("subject", SUBJECT), _E("body", BODY)]


class _Decision:
    extracted_entities = LIVE_ENTITIES
    required_capabilities = ["email.send_message"]
    intent = "actionable"


# --- the trace, stage by stage ----------------------------------------------------------------------------

def test_stage_1_the_address_is_in_the_trusted_text():
    from bruce_engine import decision_resolver
    assert ADDRESS in decision_resolver.trusted_reply_text(LIVE_TEXT)


def test_stage_2_the_model_extracted_the_address():
    """The model was never the problem. It found the address and labelled it `email`."""
    assert any(e.value == ADDRESS for e in LIVE_ENTITIES)
    assert [e.type for e in LIVE_ENTITIES] == ["email", "subject", "body"]


def test_stage_3_the_goal_patch_is_where_it_used_to_disappear():
    """THE FIRST POINT OF LOSS, and now the fix. `entity_slots` is the handoff from the model's vocabulary
    into the goal's."""
    patch = goal_handler.entity_slots(GoalKind.send_email, LIVE_ENTITIES)
    assert patch.get("recipient") == ADDRESS, "the address is still lost at the model->goal handoff"
    assert patch.get("subject") == SUBJECT
    assert patch.get("body") == BODY


def test_stage_3_the_token_rule_alone_still_cannot_do_it():
    """The anti-vacuous partner: the OLD rule is reproduced here and still drops the address, so the test
    above is passing because of the declared alias and not because "email" and "recipient" somehow match."""
    import re
    names = [s.name for s in goal_slots.slot_specs(GoalKind.send_email)]
    raw = "email"
    parts = {t for t in raw.split("_") if t}
    token_hit = [n for n in names
                 if raw == n or {t for t in re.split(r"[^a-z0-9]+", n) if t} & parts]
    assert token_hit == [], f"token matching would have found {token_hit} — the alias is not load-bearing"


def test_stage_4_the_merge_keeps_all_three():
    slots = goal_handler.turn_slots(GoalKind.send_email, continuation=None, decision=_Decision(),
                                    turn_id="m1", turn_index=1, timezone_name="America/Chicago",
                                    trusted_text=LIVE_TEXT)
    merged = goal_slots.merge_slots(None, slots)
    assert merged["recipient"].value == ADDRESS
    assert merged["subject"].value == SUBJECT
    assert merged["body"].value == BODY


def test_stage_5_the_persisted_goal_round_trips_all_three():
    slots = goal_handler.turn_slots(GoalKind.send_email, continuation=None, decision=_Decision(),
                                    turn_id="m1", turn_index=1, timezone_name="America/Chicago",
                                    trusted_text=LIVE_TEXT)
    blob = goal_slots.to_goal_jsonb({"capability": "gmail.send_message"}, GoalKind.send_email,
                                    goal_slots.merge_slots(None, slots))
    kind, back = goal_slots.from_goal_jsonb(blob)
    assert kind is GoalKind.send_email
    assert back["recipient"].value == ADDRESS


def test_stage_6_nothing_is_missing_so_a_decision_can_be_proposed():
    """`next_step` only reaches PROPOSE_CONFIRMATION when `missing_required` is empty. With the recipient
    lost it was `('recipient',)` and the turn could only ever ASK."""
    slots = goal_handler.turn_slots(GoalKind.send_email, continuation=None, decision=_Decision(),
                                    turn_id="m1", turn_index=1, timezone_name="America/Chicago",
                                    trusted_text=LIVE_TEXT)
    merged = goal_slots.merge_slots(None, slots)
    assert goal_slots.missing_required(GoalKind.send_email, dict(merged)) == ()


# --- the generic mechanism ---------------------------------------------------------------------------------

@pytest.mark.parametrize("entity_type", [
    "email", "email_address", "recipient_email", "to", "to_address", "address",
    "recipient_address", "send_to", "recipient", "EMAIL", "Email Address",
])
def test_every_name_a_model_gives_an_address_reaches_the_recipient_slot(entity_type):
    got = goal_handler.entity_slots(GoalKind.send_email, [_E(entity_type, ADDRESS)])
    assert got.get("recipient") == ADDRESS, f"{entity_type!r} did not reach the recipient slot"


def test_the_aliases_live_on_the_slot_they_fill():
    """Declared beside the slot, so renaming or removing the slot takes its vocabulary with it. A second
    list somewhere else is how the capability namespace rotted in the first place."""
    spec = goal_slots.spec_for(GoalKind.send_email, "recipient")
    assert "email" in spec.entity_aliases
    for s in goal_slots.slot_specs(GoalKind.send_email):
        for alias in s.entity_aliases:
            assert alias == alias.lower().strip(), f"{alias!r} is not normalized"


def test_an_alias_never_steals_another_slots_entity():
    """`subject` and `body` must still land where they belong — an alias table that over-matched would
    put the body in the subject line and nobody would notice until it was sent."""
    got = goal_handler.entity_slots(GoalKind.send_email, LIVE_ENTITIES)
    assert got["subject"] == SUBJECT and got["body"] == BODY and got["recipient"] == ADDRESS


def test_an_unknown_entity_type_is_still_dropped():
    """Dropping is safe — it costs one question. Forcing a stranger into a slot costs a letter to the
    wrong person, which is why this stays narrow."""
    assert goal_handler.entity_slots(GoalKind.send_email, [_E("venue", "the gym")]) == {}


# --- the deterministic backstop ----------------------------------------------------------------------------

def test_an_address_in_the_students_own_words_fills_the_slot_without_the_model():
    """THE GUARANTEE: never ask again for something in the sentence being answered. Asserted with the
    model contributing NOTHING, so it holds however the reading goes wrong next time."""
    class _Blank:
        extracted_entities = []
        required_capabilities = ["gmail.send_message"]
        intent = "actionable"

    slots = goal_handler.turn_slots(GoalKind.send_email, continuation=None, decision=_Blank(),
                                    turn_id="m1", turn_index=1, timezone_name="America/Chicago",
                                    trusted_text=LIVE_TEXT)
    assert slots["recipient"].value == ADDRESS
    assert slots["recipient"].source is Source.user_stated, \
        "an address the student typed is stated, not guessed — it must be able to correct a model value"


def test_a_forwarded_or_quoted_address_never_becomes_the_recipient():
    """The safety direction, and the reason this reads TRUSTED text rather than the message. An address in
    somebody else's correspondence is their correspondent, not the student's."""
    quoted = 'send it to whoever. coach wrote "reply to coach@school.edu" earlier'
    forwarded = ("email them please\n\nFrom: coach@school.edu\nSent: today\n"
                 "reply to principal@school.edu")
    for text in (quoted, forwarded):
        got = goal_handler.address_from_trusted_text(GoalKind.send_email, text)
        assert got == {} or got.get("recipient") not in ("coach@school.edu", "principal@school.edu"), \
            f"an address from someone else's text became the recipient: {got}"


def test_two_addresses_in_one_sentence_resolve_to_a_question_not_a_guess():
    """Picking the first would be a guess about where mail goes."""
    both = "email dhruvhydrox@gmail.com or maybe coach@school.edu about saturday"
    assert goal_handler.address_from_trusted_text(GoalKind.send_email, both) == {}


def test_the_backstop_never_overwrites_what_the_model_or_the_student_already_gave():
    """It fills a hole; it does not correct anyone. A correction travels through `merge_slots`, where
    provenance can adjudicate it."""
    stated = [_E("recipient", "someone.else@school.edu")]

    class _D:
        extracted_entities = stated
        required_capabilities = ["gmail.send_message"]
        intent = "actionable"

    slots = goal_handler.turn_slots(GoalKind.send_email, continuation=None, decision=_D(),
                                    turn_id="m1", turn_index=1, timezone_name="America/Chicago",
                                    trusted_text=LIVE_TEXT)
    assert slots["recipient"].value == "someone.else@school.edu"


def test_a_kind_with_no_recipient_slot_is_untouched():
    """The backstop is found off the SCHEMA, so a calendar goal gains nothing and needs no branch."""
    assert goal_handler.address_from_trusted_text(GoalKind.schedule_event, LIVE_TEXT) == {}
