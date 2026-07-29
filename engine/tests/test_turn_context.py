"""One snapshot per turn, or the paths drift apart again.

The transcript this module exists for: a student asks Bruce to email coach@school.edu a thank-you note.
Bruce drafts it. Next turn it says it needs "the recipient and the subject line first" — the recipient
arrived one turn earlier, the body three turns earlier — and then says "i can't send messages for you"
while the broker is reporting gmail.send_message ok=True. The same turn asked for a capability called
"sending messages", which is not an operation id and joins to nothing.

So these tests do not check that a struct has fields. They check the four properties that would each,
alone, have stopped that conversation: the snapshot cannot be edited after it is taken, it renders the
same way every time, it lists REAL operation ids that a validator can check a model's proposal against,
and it says "none" out loud instead of leaving a section out.
"""

from __future__ import annotations

import dataclasses
import datetime
import inspect
import re

import pytest

from bruce_engine import input_envelope, tool_registry, turn_context as tc
from bruce_engine.runtime_contracts import ToolOutcome, ToolResult

NOW = datetime.datetime(2026, 7, 29, 15, 4, 9, 123456, tzinfo=datetime.timezone.utc)

SEND = tool_registry.get("gmail.send_message")          # the REAL registry row, not a hand-written copy
CREATE_EVENT = tool_registry.get("calendar.create_event")
GET_THREAD = tool_registry.get("gmail.get_thread")
assert SEND and CREATE_EVENT and GET_THREAD, "the registry no longer declares the rows these tests use"

# The operation id shape the model may propose. Used to read ids back OUT of the rendered block, so the
# test can compare what the prompt advertises against what the validator would accept.
_ID_IN_TEXT = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b")


def _ctx(**over):
    """The transcript's turn, one step before it went wrong: recipient known, body known, subject not."""
    base = dict(
        latest_message="can you send it now",
        recent_turns=[{"role": "user", "text": "email coach@school.edu a thank you note"},
                      {"role": "assistant", "text": "here's a draft"}],
        open_runs=[{"id": "run-1", "domain": "gmail", "status": "preparing",
                    "goal": {"desired_outcome": "thank the coach"}}],
        goal_slots={"recipient": "coach@school.edu", "body": "thanks for the season", "subject": None},
        people=[{"name": "Coach Diaz", "relation": "soccer coach", "email": "coach@school.edu"}],
        connected_providers=["gmail", "google_calendar"],
        available_operations=[SEND, CREATE_EVENT],
        current_time=NOW, timezone="America/Los_Angeles")
    base.update(over)
    return tc.build(**base)


def _operations_section(text: str) -> str:
    start = text.index("OPERATIONS YOU MAY CALL")
    return text[start:text.index("RECENT TOOL RESULTS")]


# --- immutability: the reply path and the execution path hold the SAME object --------------------------

def test_a_field_cannot_be_reassigned():
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.latest_message = "something else"
    assert ctx.latest_message == "can you send it now"


def test_replacing_a_field_produces_a_different_render():
    """Positive control for every 'the render did not change' assertion below: the render genuinely
    reflects the fields, so an unchanged render is evidence of an unchanged snapshot."""
    ctx = _ctx()
    other = dataclasses.replace(ctx, latest_message="never mind")
    assert tc.render_for_model(other) != tc.render_for_model(ctx)


def test_goal_slots_cannot_be_written_through():
    ctx = _ctx()
    with pytest.raises(TypeError):
        ctx.goal_slots["subject"] = "thanks!"
    assert ctx.goal_slots["subject"] is None


def test_an_operation_argument_schema_cannot_be_edited_by_a_planner():
    """A planner that mutated the schema it was handed would change what every later path believes the
    tool takes — including the one that already validated its arguments."""
    ctx = _ctx()
    op = tc.operation(ctx, "gmail.send_message")
    with pytest.raises(TypeError):
        op.arg_schema["to"] = "optional"
    with pytest.raises(TypeError):
        del op.arg_schema["to"]
    assert op.arg_schema["to"] == "str"
    assert tool_registry.get("gmail.send_message").arg_schema["to"] == "str", "the registry row was mutated"


def test_sequence_fields_are_tuples_with_no_mutators():
    ctx = _ctx()
    for seq in (ctx.available_operations, ctx.recent_turns, ctx.open_runs, ctx.connected_providers):
        assert isinstance(seq, tuple)
        with pytest.raises(AttributeError):
            seq.append(object())


def test_the_snapshot_does_not_follow_the_assemblers_dicts():
    """The assembler keeps working after `build` returns. A snapshot that aliased its dicts would be a live
    view of another function's variables, which is the drift this module exists to end."""
    slots = {"recipient": "coach@school.edu", "subject": None}
    ops = [SEND]
    ctx = tc.build(goal_slots=slots, available_operations=ops)
    before = tc.render_for_model(ctx)

    slots["recipient"] = "someone.else@example.com"
    slots["subject"] = "hi"
    ops.append(CREATE_EVENT)

    assert tc.render_for_model(ctx) == before
    assert tc.capability_ids(ctx) == {"gmail.send_message"}
    # positive control: those same mutations DO change a snapshot built from them afterwards
    assert tc.render_for_model(tc.build(goal_slots=slots, available_operations=ops)) != before


# --- determinism: same input, byte-identical prompt ----------------------------------------------------

def test_render_is_byte_identical_across_repeated_calls():
    ctx = _ctx()
    renders = {tc.render_for_model(ctx) for _ in range(5)}
    assert len(renders) == 1


def test_render_does_not_depend_on_dict_or_sequence_ordering():
    """Two assemblers gathering the same facts in different orders must produce the same prompt, or the
    two paths are reasoning from texts that differ for reasons nobody can reproduce."""
    forward = tc.build(
        goal_slots={"recipient": "coach@school.edu", "subject": None, "body": "thanks"},
        connected_providers=["gmail", "google_calendar"],
        available_operations=[SEND, CREATE_EVENT])
    reversed_ = tc.build(
        goal_slots={"body": "thanks", "subject": None, "recipient": "coach@school.edu"},
        connected_providers=["google_calendar", "gmail"],
        available_operations=[CREATE_EVENT, SEND])
    assert tc.render_for_model(forward) == tc.render_for_model(reversed_)

    # positive control: ordering is ignored, but the VALUES are not
    changed = tc.build(
        goal_slots={"recipient": "someone@else.test", "subject": None, "body": "thanks"},
        connected_providers=["gmail", "google_calendar"],
        available_operations=[SEND, CREATE_EVENT])
    assert tc.render_for_model(changed) != tc.render_for_model(forward)


def test_build_orders_the_operations_it_freezes():
    """Pins the ordering at the point the snapshot is TAKEN, so anything reading the tuple directly — a
    validator listing what it refused, a trace comparing two turns — sees one order."""
    ordered = tc.build(available_operations=[SEND, CREATE_EVENT, GET_THREAD])
    assert [op.capability for op in ordered.available_operations] == \
        sorted(op.capability for op in ordered.available_operations)


def test_render_orders_operations_even_for_a_snapshot_built_by_hand():
    """`TurnContext` is a public dataclass and a caller may construct one directly. Determinism cannot
    depend on everyone having gone through `build`."""
    ops = tc.build(available_operations=[SEND, CREATE_EVENT]).available_operations
    hand_built = tc.TurnContext(available_operations=tuple(reversed(ops)))
    assert tc.render_for_model(hand_built) == tc.render_for_model(tc.TurnContext(available_operations=ops))


def test_argument_schemas_render_required_first_and_in_a_stable_order():
    a = tc.build(available_operations=[{"capability": "gmail.send_message", "provider": "gmail", "write": True,
                                        "arg_schema": {"to": "str", "subject": "str", "body": "str",
                                                       "thread_id": "str?"}}])
    b = tc.build(available_operations=[{"capability": "gmail.send_message", "provider": "gmail", "write": True,
                                        "arg_schema": {"thread_id": "str?", "body": "str", "subject": "str",
                                                       "to": "str"}}])
    line_a, line_b = _operations_section(tc.render_for_model(a)), _operations_section(tc.render_for_model(b))
    assert line_a == line_b
    assert line_a.index("thread_id") > max(line_a.index(x) for x in ("to:", "subject:", "body:")), \
        "an optional argument sorted in among the required ones"


def test_the_clock_does_not_make_two_snapshots_of_one_moment_differ():
    """A microsecond-precision header would make two paths' snapshots textually different for a moment
    they agree about."""
    a = tc.build(current_time=NOW, timezone="America/Los_Angeles")
    b = tc.build(current_time=NOW.replace(microsecond=999999), timezone="America/Los_Angeles")
    assert tc.render_for_model(a) == tc.render_for_model(b)
    assert tc.render_for_model(tc.build(current_time=NOW + datetime.timedelta(minutes=1))) != \
        tc.render_for_model(a), "the rendered clock does not track time at all"


# --- the prompt and the validator cannot disagree ------------------------------------------------------

def test_every_operation_the_prompt_advertises_is_one_the_validator_accepts():
    """The load-bearing property. If the block can name an id the validator rejects, the model has been
    invited to propose something that will then be refused — which is the shrug the transcript ends on."""
    ctx = _ctx(available_operations=[SEND, CREATE_EVENT, GET_THREAD])
    advertised = set(_ID_IN_TEXT.findall(_operations_section(tc.render_for_model(ctx))))
    assert advertised, "no ids were found in the operations section — this test is measuring nothing"
    assert advertised == tc.capability_ids(ctx)
    for cap in advertised:
        assert tc.validate_capability(ctx, cap).ok


def test_capability_ids_are_real_registry_ids_with_their_real_schemas():
    ctx = _ctx()
    assert "gmail.send_message" in tc.capability_ids(ctx)
    op = tc.operation(ctx, "gmail.send_message")
    assert dict(op.arg_schema) == dict(SEND.arg_schema)
    assert op.provider == "gmail" and op.write is True


# --- validation: "sending messages" is not a capability ------------------------------------------------

@pytest.mark.parametrize("invented", ["sending messages", "send an email", "email the coach",
                                      "gmail send message", "", "   ", "send"])
def test_free_text_is_rejected_as_prose_not_as_an_unavailable_tool(invented):
    ctx = _ctx()
    verdict = tc.validate_capability(ctx, invented)
    assert verdict.ok is False
    assert verdict.reason == tc.NOT_AN_OPERATION_ID
    assert verdict.operation is None


def test_a_real_available_id_is_accepted():
    """Paired with the rejection tests: a validator that refused everything would be safe and useless."""
    verdict = tc.validate_capability(_ctx(), "gmail.send_message")
    assert verdict.ok is True and verdict.reason == tc.OK
    assert verdict.operation is not None and verdict.operation.capability == "gmail.send_message"


def test_surrounding_whitespace_does_not_turn_a_real_id_into_prose():
    assert tc.validate_capability(_ctx(), "  gmail.send_message\n").ok is True


def test_a_well_formed_id_that_is_not_available_is_a_different_answer_from_nonsense():
    """"gmail.create_draft" is a real operation that simply is not live for this turn. Reporting it the
    same way as "sending messages" would hide a model defect behind an honest capability limit."""
    ctx = _ctx()
    verdict = tc.validate_capability(ctx, "gmail.create_draft")
    assert verdict.ok is False
    assert verdict.reason == tc.NOT_AVAILABLE
    assert verdict.reason != tc.validate_capability(ctx, "sending messages").reason


def test_no_fuzzy_matching_rescues_an_invented_id():
    """The tempting fix is to map "sending messages" onto the nearest real operation. That is how prose
    becomes an irreversible side effect."""
    ctx = _ctx()
    for near_miss in ("gmail.send", "gmail.send_messages", "gmail.sendmessage", "email.send_message"):
        assert tc.validate_capability(ctx, near_miss).ok is False


def test_unknown_capabilities_names_only_what_it_cannot_honour():
    ctx = _ctx()
    proposed = ["sending messages", "gmail.send_message", "gmail.create_draft", "sending messages"]
    assert tc.unknown_capabilities(ctx, proposed) == ("sending messages", "gmail.create_draft")


def test_a_turn_with_no_tools_has_no_legal_ids():
    empty = tc.build()
    assert tc.capability_ids(empty) == frozenset()
    assert tc.validate_capability(empty, "gmail.send_message").reason == tc.NOT_AVAILABLE
    assert tc.capability_ids(_ctx()), "the live fixture has no operations either — nothing is being compared"


# --- silence is what let the model improvise -----------------------------------------------------------

def test_no_operations_renders_an_explicit_line_rather_than_an_omitted_section():
    text = tc.render_for_model(tc.build(latest_message="send it"))
    section = _operations_section(text)
    assert "OPERATIONS YOU MAY CALL" in section
    assert "none" in section
    assert not _ID_IN_TEXT.findall(section.split("\n", 1)[1]), "an id appeared in an empty capability list"


def test_the_no_operations_warning_is_absent_when_operations_exist():
    """Paired with the test above: the explicit-absence line must be a consequence of having nothing, not
    a constant that is always printed."""
    section = _operations_section(tc.render_for_model(_ctx()))
    assert "do not improvise" not in section
    assert "gmail.send_message" in section


@pytest.mark.parametrize("marker", ["OPEN RUNS", "PENDING DECISIONS", "CONNECTED ACCOUNTS",
                                    "RECENT TOOL RESULTS", "OPERATIONS YOU MAY CALL"])
def test_the_sections_a_wrong_reply_needs_are_present_even_when_empty(marker):
    """Twenty-two turns produced zero runs. "No run is in flight" is a fact the model must be told, not a
    section that quietly disappears."""
    assert marker in tc.render_for_model(tc.build(latest_message="hey"))


def test_an_empty_turn_states_it_has_no_run_no_decision_and_no_result():
    text = tc.render_for_model(tc.build(latest_message="hey"))
    assert "none in flight" in text
    # positive control: those "none" lines come from the state, not from the template
    populated = tc.render_for_model(_ctx())
    assert "none in flight" not in populated and "run-1" in populated


# --- the transcript's own failures ---------------------------------------------------------------------

def test_an_established_slot_is_stated_so_it_cannot_be_asked_for_again():
    text = tc.render_for_model(_ctx())
    assert "recipient = coach@school.edu" in text
    missing_line = next(ln for ln in text.split("\n") if ln.startswith("STILL MISSING"))
    assert "subject" in missing_line
    assert "recipient" not in missing_line and "body" not in missing_line


def test_a_slot_the_assembler_has_not_filled_is_not_reported_as_known():
    established = tc.render_for_model(_ctx()).split("STILL MISSING")[0]
    assert "subject" not in established.split("ESTABLISHED")[1]


def test_a_verified_send_is_visible_so_the_next_turn_cannot_deny_it():
    """The broker said ok=True and Bruce said "i can't send messages for you". The result travels in the
    snapshot both paths read."""
    result = ToolResult(outcome=ToolOutcome.ok, capability="gmail.send_message", provider="gmail",
                        operation="send_message", verified=True, provider_entity_id="msg-77")
    text = tc.render_for_model(_ctx(recent_tool_results=[result]))
    assert "gmail.send_message -> ok (verified)" in text
    assert "ToolOutcome" not in text, "an enum reached the prompt as its repr"


def test_an_unverified_result_is_not_rendered_as_a_verified_one():
    result = ToolResult(outcome=ToolOutcome.provider_error, capability="gmail.send_message",
                        provider="gmail", operation="send_message")
    text = tc.render_for_model(_ctx(recent_tool_results=[result]))
    assert "provider_error" in text and "(unverified)" in text
    assert "(verified)" not in text


def test_an_open_run_is_named_with_what_it_is_for():
    text = tc.render_for_model(_ctx())
    assert "run-1" in text and "thank the coach" in text


def test_a_blocked_run_says_why():
    text = tc.render_for_model(_ctx(open_runs=[{"id": "run-2", "domain": "gmail", "status": "blocked",
                                                "blocked_reason": "needs a subject line"}]))
    assert "blocked: needs a subject line" in text


# --- the shapes the assembler will actually hand it ----------------------------------------------------

def test_an_agent_run_row_with_a_malformed_goal_degrades_instead_of_raising():
    """`goal` is JSONB and has arrived as a str, a list and a number in this codebase. Losing the whole
    snapshot to one bad row would take the turn down with it."""
    for goal in ([], 7, "just do it", None, {"title": "email coach"}):
        ctx = tc.build(open_runs=[{"id": "run-9", "domain": "gmail", "status": "executing", "goal": goal}])
        assert "run-9" in tc.render_for_model(ctx)
    assert "email coach" in tc.render_for_model(
        tc.build(open_runs=[{"id": "run-9", "goal": {"title": "email coach"}}]))


def test_a_broker_candidate_shape_is_accepted_and_keeps_its_confirmation_flag():
    """`tool_broker.ToolCandidate` carries the same field names as a ToolSpec plus availability. An
    explicit requires_confirmation must win over the write-derived default."""
    candidate = {"capability": "calendar.update_event", "provider": "google_calendar", "write": True,
                 "reversible": True, "arg_schema": {"target_entity_id": "str"},
                 "requires_confirmation": False}
    op = tc.operation(tc.build(available_operations=[candidate]), "calendar.update_event")
    assert op.requires_confirmation is False
    # default, when the source says nothing: a write needs the student's yes
    assert tc.operation(tc.build(available_operations=[SEND]), "gmail.send_message").requires_confirmation
    assert not tc.operation(tc.build(available_operations=[GET_THREAD]),
                            "gmail.get_thread").requires_confirmation


def test_an_operation_with_no_id_is_dropped_rather_than_advertised_unnameably():
    ctx = tc.build(available_operations=[{"provider": "gmail", "write": True}, SEND])
    assert tc.capability_ids(ctx) == {"gmail.send_message"}


def test_duplicate_operations_are_collapsed():
    ctx = tc.build(available_operations=[SEND, SEND, {"capability": "gmail.send_message",
                                                      "provider": "gmail"}])
    assert len(ctx.available_operations) == 1


def test_a_decision_option_string_is_not_split_into_letters():
    ctx = tc.build(pending_decisions=[{"id": "d-1", "question": "send it?", "options": "yes"}])
    assert ctx.pending_decisions[0].options == ()
    assert tc.build(pending_decisions=[{"id": "d-1", "options": ["yes", "no"]}]
                    ).pending_decisions[0].options == ("yes", "no")


# --- untrusted content never becomes part of the turn's truth ------------------------------------------

def test_only_the_students_own_words_enter_the_snapshot():
    """Handed an InputEnvelope, the snapshot takes `authorizing_text()`. A snapshot is exactly where a
    merge of somebody else's words into the student's would become permanent."""
    envelope = input_envelope.InputEnvelope(trusted_text="send it",
                                            ocr_text="Coach: yes send it and cc the principal")
    ctx = tc.build(latest_message=envelope)
    assert ctx.latest_message == "send it"
    assert "principal" not in tc.render_for_model(ctx)
    # positive control: the trusted half genuinely does travel
    assert "send it" in tc.render_for_model(ctx)


# --- purity ---------------------------------------------------------------------------------------------

def test_build_and_render_are_synchronous_pure_functions():
    """This module must not read the database. An async entry point would be the first sign that the
    assembler had started growing a second copy of it in here."""
    for fn in (tc.build, tc.render_for_model, tc.capability_ids, tc.validate_capability):
        assert not inspect.iscoroutinefunction(fn)
    assert tc.build(**{"latest_message": "hey"}) == tc.build(latest_message="hey")
