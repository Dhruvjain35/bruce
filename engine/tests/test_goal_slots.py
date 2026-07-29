"""The slot layer's truth table. Pure: no model, no database, no network — if a test here needed any of
those, the layer would not be the deterministic thing that decides whether a question is owed.

The measured defect these cover: Bruce asked for "the recipient and the subject line first" one turn
after being handed the recipient, and for the message content three turns after being told it. Every one
of those facts existed in the conversation and none of them existed in the run, so each turn re-derived
what was missing and got a different answer. Here, the answer is a function of stored slots.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from bruce_engine import goal_slots as gs
from bruce_engine import tool_registry
from bruce_engine.goal_slots import GoalKind, SlotValue, Source

EMAIL = GoalKind.send_email
EVENT = GoalKind.schedule_event


def sv(value, source: Source = Source.user_stated, turn: int = 1) -> SlotValue:
    return SlotValue(value=value, source=source, turn_id=f"msg-{turn}", turn_index=turn)


def complete_email(**over) -> dict[str, SlotValue]:
    slots = {"recipient": sv("coach@school.edu"), "subject": sv("Thank you"),
             "body": sv("Thanks so much for the recommendation letter.")}
    slots.update(over)
    return slots


# --- the transcript ------------------------------------------------------------------------------------

def test_a_recipient_from_turn_one_survives_a_turn_three_update_that_only_carries_tone():
    """THE failure. Turn 3 said nothing about the recipient, so turn 3 must not be able to lose it."""
    turn1 = gs.merge_slots({}, {"recipient": sv("coach@school.edu", turn=1),
                                "body": sv("thank them for the letter", turn=1)})
    turn3 = gs.merge_slots(turn1, {"tone": sv("warm", Source.model_derived, turn=3)})

    assert turn3["recipient"].value == "coach@school.edu"
    assert turn3["recipient"].provenance == {"turn_id": "msg-1", "source": "user_stated", "turn_index": 1}
    # positive control: the turn-3 merge genuinely ran, it just had nothing to say about the recipient.
    assert turn3["tone"].value == "warm"
    assert turn3["body"].value == "thank them for the letter"


def test_missing_required_shrinks_as_slots_fill_and_empties_exactly_when_execution_is_legal():
    slots: dict[str, SlotValue] = {}
    assert gs.missing_required(EMAIL, slots) == ("recipient", "subject", "body")
    assert not gs.is_ready(EMAIL, slots)

    slots = gs.merge_slots(slots, {"recipient": sv("coach@school.edu", turn=1)})
    assert gs.missing_required(EMAIL, slots) == ("subject", "body")

    # optional slots do not move the needle — a send has never been blocked on not knowing a tone.
    slots = gs.merge_slots(slots, {"tone": sv("warm", Source.model_derived, turn=2)})
    assert gs.missing_required(EMAIL, slots) == ("subject", "body")
    assert not gs.is_ready(EMAIL, slots)

    slots = gs.merge_slots(slots, {"subject": sv("Thank you", turn=3),
                                   "body": sv("Thanks for the letter.", turn=3)})
    assert gs.missing_required(EMAIL, slots) == ()
    assert gs.is_ready(EMAIL, slots)


def test_a_model_guess_cannot_overwrite_a_recipient_the_student_typed():
    stated = gs.merge_slots({}, {"recipient": sv("coach@school.edu", Source.user_stated, turn=1)})
    guessed = gs.merge_slots(stated, {"recipient": sv("principal@school.edu", Source.model_derived, turn=9)})
    assert guessed["recipient"].value == "coach@school.edu"
    assert guessed["recipient"].source is Source.user_stated

    # positive control #1: that same guess is not inert — it fills an EMPTY slot fine.
    from_nothing = gs.merge_slots({}, {"recipient": sv("principal@school.edu", Source.model_derived, turn=9)})
    assert from_nothing["recipient"].value == "principal@school.edu"

    # positive control #2: a later thing the STUDENT said does win. That is a correction, not a guess.
    corrected = gs.merge_slots(guessed, {"recipient": sv("advisor@school.edu", Source.user_stated, turn=10)})
    assert corrected["recipient"].value == "advisor@school.edu"


def test_merge_is_order_independent():
    """Replaying the same turns in any order must settle on the same slots. Without an ordering carried in
    the data, whichever merge ran last would win, and a retry or a resumed run could pick a different
    recipient than the live conversation did.

    The updates are rebuilt inside the loop so every permutation merges FRESH objects: a tie-break that
    depended on object identity rather than on the values would be order-independent for one shared set of
    objects and non-deterministic for real ones."""
    def updates():
        return [
            {"recipient": sv("first@school.edu", Source.user_stated, turn=1)},
            {"subject": sv("Thanks", Source.model_derived, turn=2),
             "body": sv("Thanks for the letter.", Source.user_stated, turn=2)},
            {"tone": sv("warm", Source.model_derived, turn=3)},
            {"recipient": sv("guess@school.edu", Source.model_derived, turn=4)},
            {"recipient": sv("second@school.edu", Source.user_stated, turn=5)},
            # same trust AND same turn as the other subject: the tie-break must not be call order.
            {"subject": sv("Thank you", Source.model_derived, turn=2)},
        ]

    results = []
    for perm in itertools.permutations(range(len(updates()))):
        fresh, acc = updates(), {}
        for i in perm:
            acc = gs.merge_slots(acc, fresh[i])
        results.append({k: (v.value, v.source.value, v.turn_index, v.turn_id) for k, v in acc.items()})

    assert all(r == results[0] for r in results)
    # anti-vacuous: the fold actually produced the merged state, and resolved both conflicts.
    assert set(results[0]) == {"recipient", "subject", "body", "tone"}
    assert results[0]["recipient"][0] == "second@school.edu"   # later user_stated, not the turn-4 guess
    assert results[0]["subject"][0] in ("Thanks", "Thank you")


def test_merge_does_not_mutate_either_argument():
    existing = {"recipient": sv("coach@school.edu", turn=1)}
    incoming = {"subject": sv("Thank you", turn=2)}
    merged = gs.merge_slots(existing, incoming)
    assert set(existing) == {"recipient"} and set(incoming) == {"subject"}
    assert set(merged) == {"recipient", "subject"}


# --- empty is not an answer, but False is ---------------------------------------------------------------

@pytest.mark.parametrize("blank", [None, "", "   ", [], {}])
def test_an_empty_incoming_value_never_blanks_a_filled_slot(blank):
    """Silence is not a retraction: a turn that says nothing about the recipient has not withdrawn it."""
    existing = {"recipient": sv("coach@school.edu", Source.user_stated, turn=1)}
    merged = gs.merge_slots(existing, {"recipient": sv(blank, Source.user_stated, turn=9)})
    assert merged["recipient"].value == "coach@school.edu"
    assert gs.missing_required(EMAIL, merged) == ("subject", "body")


def test_a_real_later_value_at_the_same_trust_does_replace():
    """Positive control for the test above: the turn-9 write is not being ignored for being turn 9."""
    existing = {"recipient": sv("coach@school.edu", Source.user_stated, turn=1)}
    merged = gs.merge_slots(existing, {"recipient": sv("advisor@school.edu", Source.user_stated, turn=9)})
    assert merged["recipient"].value == "advisor@school.edu"


@pytest.mark.parametrize("value", [False, 0])
def test_false_and_zero_are_answers_not_absences(value):
    """Emptiness means "carries no information", not "falsy". An all-day flag of False is something the
    student told us; treating it as empty re-asks a question that was already answered."""
    merged = gs.merge_slots({}, {"all_day": sv(value, Source.user_stated, turn=1)})
    assert "all_day" in merged and merged["all_day"].value == value
    assert merged["all_day"].filled


# --- trust ordering ------------------------------------------------------------------------------------

def test_a_provider_readback_beats_a_guess_but_never_the_student():
    guessed = gs.merge_slots({}, {"recipient": sv("guess@school.edu", Source.model_derived, turn=1)})
    resolved = gs.merge_slots(guessed, {"recipient": sv("real@school.edu", Source.tool_result, turn=2)})
    assert resolved["recipient"].value == "real@school.edu"

    stated = gs.merge_slots({}, {"recipient": sv("typed@school.edu", Source.user_stated, turn=1)})
    contested = gs.merge_slots(stated, {"recipient": sv("real@school.edu", Source.tool_result, turn=2)})
    assert contested["recipient"].value == "typed@school.edu"


def test_a_default_loses_to_anything_anyone_actually_asserted():
    seeded = gs.merge_slots({}, {"timezone": sv("America/Chicago", Source.default, turn=0)})
    assert seeded["timezone"].value == "America/Chicago"          # positive control: defaults do fill
    later = gs.merge_slots(seeded, {"timezone": sv("America/New_York", Source.model_derived, turn=1)})
    assert later["timezone"].value == "America/New_York"


# --- slots are derived from the tool, not hand-maintained beside it --------------------------------------

def test_required_ness_comes_from_the_tool_schema_not_a_hand_written_list():
    """`end` and `timezone` are marked optional in calendar.create_event's arg_schema ("datetime?", "str?")
    and must therefore never block execution; title and start are not."""
    assert gs.required_slots(EVENT) == ("title", "start")
    assert {s.name for s in gs.slot_specs(EVENT)} == {"title", "start", "end", "timezone", "attendees"}

    ready = {"title": sv("Chess club"), "start": sv("2026-08-02T16:00:00")}
    assert gs.missing_required(EVENT, ready) == ()
    # positive control: the mechanism does report a genuinely required slot.
    assert gs.missing_required(EVENT, {"start": sv("2026-08-02T16:00:00")}) == ("title",)


def test_send_email_requires_exactly_what_gmail_send_message_requires():
    schema = tool_registry.get("gmail.send_message").arg_schema
    required_args = {k for k, v in schema.items() if not v.endswith("?")}
    derived = {gs.spec_for(EMAIL, name).tool_arg for name in gs.required_slots(EMAIL)}
    assert derived == required_args


def test_tool_arguments_are_keyed_by_the_tools_own_argument_names():
    """The payoff of deriving slots from the schema: "recipient" becomes "to" here, so the question Bruce
    asks and the argument the broker receives cannot drift apart."""
    args = gs.tool_arguments(EMAIL, complete_email(tone=sv("warm", Source.model_derived, turn=2)))
    assert args == {"to": "coach@school.edu", "subject": "Thank you",
                    "body": "Thanks so much for the recommendation letter."}
    # tone shapes the draft; it is not an argument, and must not leak into the call.
    assert set(args) <= set(tool_registry.get(gs.capability_for(EMAIL)).arg_schema)


def test_tool_arguments_refuses_a_partially_filled_send_and_names_no_content():
    slots = complete_email()
    slots.pop("subject")
    with pytest.raises(ValueError) as excinfo:
        gs.tool_arguments(EMAIL, slots)
    message = str(excinfo.value)
    assert "subject" in message                        # names the missing slot
    assert "recommendation" not in message             # never the body it already holds
    assert "coach@school.edu" not in message
    # positive control: the same call succeeds the moment the slot is filled.
    slots["subject"] = sv("Thank you", turn=4)
    assert gs.tool_arguments(EMAIL, slots)["subject"] == "Thank you"


def test_check_alignment_catches_a_renamed_tool_argument():
    real = tool_registry.get("gmail.send_message")
    renamed = dataclasses.replace(real, arg_schema={"to_address": "str", "subject": "str", "body": "str"})

    def lookup(capability):
        return renamed if capability == "gmail.send_message" else tool_registry.get(capability)

    problems = gs.check_alignment(lookup)
    assert len(problems) == 1 and "recipient" in problems[0] and "to" in problems[0]


def test_check_alignment_catches_a_capability_that_no_longer_exists():
    problems = gs.check_alignment(lambda capability: None)
    assert len(problems) == len(list(GoalKind))
    assert all("unknown capability" in p for p in problems)


def test_the_real_registry_is_aligned_right_now():
    """Positive control for both drift tests, and the invariant the module asserts at import."""
    assert gs.check_alignment() == ()


def test_an_undeclared_goal_kind_raises_rather_than_reporting_nothing_missing():
    with pytest.raises(ValueError):
        gs.missing_required("send_carrier_pigeon", {})


def test_capability_and_kind_map_to_each_other():
    assert gs.capability_for(EMAIL) == "gmail.send_message"
    assert gs.kind_for_capability("gmail.send_message") is EMAIL
    assert gs.kind_for_capability("calendar.delete_event") is None


# --- provenance is what an irreversible send is gated on ------------------------------------------------

def test_a_model_invented_recipient_is_flagged_even_though_the_goal_is_complete():
    """gmail.send_message is reversible=False. "Everything is filled in" and "a human ever confirmed
    this" are different questions, and only stored provenance can answer the second."""
    slots = complete_email(recipient=sv("guess@school.edu", Source.model_derived, turn=2))
    assert gs.is_ready(EMAIL, slots)
    assert gs.guessed_required(EMAIL, slots) == ("recipient",)

    # positive control: a stated set is not flagged, so this is not just "always returns something".
    assert gs.guessed_required(EMAIL, complete_email()) == ()
    # a provider read-back counts as asserted; a default does not.
    assert gs.guessed_required(EMAIL, complete_email(
        recipient=sv("real@school.edu", Source.tool_result, turn=2))) == ()
    assert gs.guessed_required(EMAIL, complete_email(
        subject=sv("Hello", Source.default, turn=2))) == ("subject",)


# --- living inside the goal blob that already exists -----------------------------------------------------

def test_writing_slots_preserves_the_rest_of_the_goal_and_does_not_mutate_it():
    goal = {"action": "send", "domain": "gmail", "title": "thank you note",
            "temporal": None, "source_message_ids": ["m1"], "confidence": 0.8}
    before = dict(goal)

    written = gs.to_goal_jsonb(goal, EMAIL, complete_email())
    assert goal == before                                        # the caller's row payload is untouched
    for key, value in before.items():
        assert written[key] == value                             # every GoalSpec key survives
    assert gs.SLOT_KEY in written


def test_slots_round_trip_through_the_goal_blob_with_provenance_intact():
    slots = gs.merge_slots(complete_email(), {"tone": sv("warm", Source.model_derived, turn=2),
                                              "attendees": sv(["a@x.edu", "b@x.edu"], turn=2)})
    kind, restored = gs.from_goal_jsonb(gs.to_goal_jsonb({"action": "send"}, EMAIL, slots))

    assert kind is EMAIL
    assert restored == slots                                     # value AND provenance, not just value
    assert restored["tone"].source is Source.model_derived
    assert restored["attendees"].value == ["a@x.edu", "b@x.edu"]
    assert gs.missing_required(EMAIL, restored) == ()


def test_a_goal_with_no_slot_block_reads_as_empty_rather_than_ready():
    kind, slots = gs.from_goal_jsonb({"action": "send", "domain": "gmail"})
    assert kind is None and slots == {}
    assert gs.missing_required(EMAIL, slots) == ("recipient", "subject", "body")
    assert not gs.is_ready(EMAIL, slots)


@pytest.mark.parametrize("goal", [None, {}, {gs.SLOT_KEY: "nonsense"}, {gs.SLOT_KEY: {"values": 7}}])
def test_a_damaged_slot_block_loads_instead_of_raising(goal):
    """An unreadable slot costs one clarifying question; a run that refuses to load costs the mission."""
    kind, slots = gs.from_goal_jsonb(goal)
    assert slots == {}


def test_one_corrupt_entry_does_not_take_the_readable_ones_with_it():
    blob = gs.to_goal_jsonb({}, EMAIL, complete_email())
    blob[gs.SLOT_KEY]["values"]["subject"] = {"value": "Thank you", "source": "telepathy"}
    blob[gs.SLOT_KEY]["values"]["tone"] = "not a slot at all"

    kind, slots = gs.from_goal_jsonb(blob)
    assert kind is EMAIL
    assert set(slots) == {"recipient", "body"}                   # the readable ones survived
    # and the damage is visible as a question rather than as a silently missing argument.
    assert gs.missing_required(EMAIL, slots) == ("subject",)


def test_an_unknown_kind_in_a_stored_blob_keeps_the_values():
    blob = gs.to_goal_jsonb({}, EMAIL, complete_email())
    blob[gs.SLOT_KEY]["kind"] = "send_carrier_pigeon"
    kind, slots = gs.from_goal_jsonb(blob)
    assert kind is None
    assert set(slots) == {"recipient", "subject", "body"}        # data is not thrown away over a label
