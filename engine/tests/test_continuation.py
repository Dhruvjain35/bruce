"""The turn that lands on work already in flight — resolved from state, never from the student repeating
themselves.

THE TRANSCRIPT. A student asked Bruce to email one named address a thank-you note. Twenty-two turns
produced zero missions and zero agent_runs. Three of those turns were continuations and all three
vanished: "send it" (an approval), "make it professional" (an amendment), and an inline reply of "this"
(a pointer). None of them contains the word "email", and text-only routing is looking for the word
"email" — so a student who had already said what they wanted got treated as if they had said nothing.

These tests are about the properties that would each, alone, have changed that conversation:

  * a continuation resolves WITHOUT the original verb being repeated,
  * an affirmative with nothing pending never becomes a confirmation (`gmail.send_message` is
    reversible=False — a stale yes is unrecoverable),
  * a change is understood as a change even when it reads like a yes ("move it to friday"),
  * a pointer with no anchor says so instead of guessing,
  * and email and calendar go through the SAME code, differing only by a row in a table.

Pure: no database, no network, no clock, no model. A fixture here would mean the layer is not the
deterministic thing that decides whether Bruce is allowed to act.
"""

from __future__ import annotations

import dataclasses
import datetime
import inspect

import pytest

from bruce_engine import continuation as cont
from bruce_engine import decision_resolver, goal_slots, temporal
from bruce_engine.continuation import ContinuationKind as K
from bruce_engine.goal_slots import GoalKind

NOW = datetime.datetime(2026, 7, 29, 9, 0, tzinfo=datetime.timezone.utc)   # injected; never read from a clock


# --- the state a caller hands in -------------------------------------------------------------------------

def email_run(run_id: str = "run-email") -> dict:
    """An AgentRun row shaped the way `agent_run_store` returns it: a plain dict with a goal JSONB."""
    goal = goal_slots.to_goal_jsonb({"desired_outcome": "thank the coach"}, GoalKind.send_email, {})
    return {"id": run_id, "domain": "gmail", "status": "awaiting_approval", "goal": goal}


def event_run(run_id: str = "run-event") -> dict:
    goal = goal_slots.to_goal_jsonb({"desired_outcome": "put practice on the calendar"},
                                    GoalKind.schedule_event, {})
    return {"id": run_id, "domain": "calendar", "status": "awaiting_approval", "goal": goal}


def decision(decision_id: str = "dec-1", status: str = "pending") -> dict:
    return {"id": decision_id, "status": status, "question": "send this?"}


def resolve(text, *, reply=None, goal=None, pending=None, draft=None, tool_result=None):
    """Every state argument is required at the real call site; this fills the ones a case is not about."""
    return cont.resolve("student-1", text=text, reply_to_message_id=reply, open_goal=goal,
                        pending_decision=pending, recent_draft=draft, recent_tool_result=tool_result)


# --- the transcript --------------------------------------------------------------------------------------

# Every continuation turn from the failing conversation, plus the ones the same student would obviously
# type next. Not one of them names the thing being acted on.
TRANSCRIPT = ("send it", "yes", "do it", "go ahead", "make it professional", "make it shorter",
              "tell him thanks again", "no", "wait", "dont", "this")

# The vocabulary text-only routing keys on. A phrase containing any of it would be routable WITHOUT state,
# so it would prove nothing about this layer.
ROUTER_WORDS = ("email", "e-mail", "message", "msg", "note")


def test_every_transcript_phrase_resolves_and_none_of_them_names_the_thing():
    """THE failure. These turns carry no subject and no verb a router can key on; they resolve only
    because open state was consulted first."""
    for phrase in TRANSCRIPT:
        assert not any(word in phrase.lower() for word in ROUTER_WORDS), phrase

    for phrase in TRANSCRIPT:
        result = resolve(phrase, goal=email_run(), pending=decision(), reply="msg-77")
        assert result.resolved, f"{phrase!r} resolved to nothing"
        assert result.kind is not K.none
        # bound to state, not to the sentence: whatever it decided, it decided it ABOUT the open run
        assert result.target_run_id == "run-email"

    # positive control: the same machinery says "none" for a turn that genuinely is not a continuation, so
    # the assertions above are not a function that always returns something.
    unrelated = resolve("whats the homework for chem", goal=email_run(), pending=decision())
    assert unrelated.kind is K.none and unrelated.evidence == cont.NOT_A_CONTINUATION


def test_the_transcript_phrases_split_into_the_right_meanings():
    """A resolution that fired is not enough — "make it professional" resolving to CONFIRM would send the
    unedited draft."""
    kinds = {phrase: resolve(phrase, goal=email_run(), pending=decision(), reply="msg-77").kind
             for phrase in TRANSCRIPT}
    assert kinds["send it"] is kinds["yes"] is kinds["do it"] is kinds["go ahead"] is K.confirm
    assert kinds["make it professional"] is kinds["make it shorter"] is K.amend
    assert kinds["tell him thanks again"] is K.amend
    assert kinds["no"] is kinds["dont"] is kinds["wait"] is K.reject
    assert kinds["this"] is K.reference


# --- the stale-yes guard ----------------------------------------------------------------------------------

def test_send_it_with_no_pending_decision_does_not_confirm():
    """Nothing is pending, so there is nothing to confirm. gmail.send_message is reversible=False: a yes
    accepted against no decision cannot be taken back."""
    result = resolve("send it", goal=email_run(), pending=None)
    assert result.kind is K.none
    assert result.evidence == cont.AFFIRMATIVE_WITHOUT_DECISION
    assert result.target_decision_id is None
    assert result.confidence == 0.0                      # asserts no binding, so it claims no confidence

    # positive control: identical text, one pending decision -> a real confirmation bound to THAT id.
    confirmed = resolve("send it", goal=email_run(), pending=decision("dec-42"))
    assert confirmed.kind is K.confirm
    assert confirmed.target_decision_id == "dec-42"
    assert confirmed.evidence == cont.APPROVAL_OF_DECISION


def test_an_already_answered_decision_cannot_be_answered_again():
    stale = resolve("yes", goal=email_run(), pending=decision("dec-9", status="approved"))
    assert stale.kind is K.none and stale.evidence == cont.AFFIRMATIVE_WITHOUT_DECISION

    still_open = resolve("yes", goal=email_run(), pending=decision("dec-9", status="pending"))
    assert still_open.kind is K.confirm and still_open.target_decision_id == "dec-9"


def test_confirmation_binds_to_the_decision_it_was_given_not_to_the_run():
    """The id travels with the answer. A confirmation that named only the run would re-approve whatever
    decision happened to be open when it was applied."""
    result = resolve("go ahead", goal=email_run("run-a"), pending=decision("dec-b"))
    assert (result.target_run_id, result.target_decision_id) == ("run-a", "dec-b")


def test_a_confirmation_never_comes_from_quoted_or_forwarded_text():
    """`decision_resolver`'s trust boundary, kept: a forwarded email that says "yes send it" is evidence,
    never authorization."""
    forwarded = resolve("here's what he wrote\n> yes send it", goal=email_run(), pending=decision())
    assert forwarded.kind is not K.confirm

    typed = resolve("yes send it", goal=email_run(), pending=decision())
    assert typed.kind is K.confirm


# --- a change that reads like a yes -----------------------------------------------------------------------

def test_move_it_to_friday_is_a_change_even_though_a_yes_no_resolver_reads_it_as_approval():
    """The ordering property. `decision_resolver` approves this text — it contains "move it" — and it is
    not an approval of what is on screen."""
    assert decision_resolver.resolve_approval("move it to friday") is decision_resolver.Resolution.approved

    result = resolve("move it to friday", goal=event_run(), pending=decision())
    assert result.kind is K.amend
    assert dict(result.slot_patch) == {"start": "friday"}


def test_a_new_address_amends_the_recipient_instead_of_confirming_the_old_one():
    """"send it to <address>" also reads as an approval to a yes/no resolver. Confirming it would send the
    drafted note to the address the student just replaced."""
    result = resolve("actually send it to coach@school.edu", goal=email_run(), pending=decision())
    assert result.kind is K.amend
    assert dict(result.slot_patch) == {"recipient": "coach@school.edu"}


def test_an_unmappable_change_refuses_rather_than_falling_through_to_the_approval_branch():
    """A temporal change on an email goal maps to no slot. The turn must not become a send just because
    the change was not understood."""
    result = resolve("move it to friday", goal=email_run(), pending=decision())
    assert result.kind is K.none
    assert result.evidence == cont.AMEND_WITHOUT_SLOT

    # positive control: the identical phrase against a goal that HAS a temporal slot does amend.
    assert resolve("move it to friday", goal=event_run(), pending=decision()).kind is K.amend


# --- one code path, two products ---------------------------------------------------------------------------

def test_the_same_call_amends_an_email_tone_and_a_calendar_date():
    """The requirement that there is no per-product branch: one function, one rule set, one table row of
    difference."""
    tone = resolve("make it professional", goal=email_run(), pending=decision())
    date = resolve("move it to friday", goal=event_run(), pending=decision())

    assert tone.kind is date.kind is K.amend
    assert dict(tone.slot_patch) == {"tone": "professional"}
    assert dict(date.slot_patch) == {"start": "friday"}
    # the SAME role machinery produced both — the evidence names the slot each landed in, not a product
    assert tone.evidence == cont.AMEND_PREFIX + "tone"
    assert date.evidence == cont.AMEND_PREFIX + "start"


def test_one_role_lands_in_the_slot_each_product_declares_for_it():
    """"call it X" is the same fact in both worlds: an email's subject, an event's title."""
    email = resolve("call it Thank you", goal=email_run(), pending=decision())
    event = resolve("call it practice", goal=event_run(), pending=decision())
    assert dict(email.slot_patch) == {"subject": "Thank you"}
    assert dict(event.slot_patch) == {"title": "practice"}


def test_a_patched_slot_is_a_slot_the_goal_actually_declares():
    """A patch aimed at a slot nobody declares is free text wearing a slot's clothes — the same defect as
    `required_capabilities: "sending messages"`."""
    cases = ((email_run(), GoalKind.send_email, ("make it professional", "tell him thanks again",
                                                 "call it Thank you", "send it to coach@school.edu")),
             (event_run(), GoalKind.schedule_event, ("move it to friday", "call it practice")))
    seen = 0
    for run, kind, phrases in cases:
        for phrase in phrases:
            patch = resolve(phrase, goal=run, pending=decision()).slot_patch
            assert patch, phrase
            for slot in patch:
                assert goal_slots.spec_for(kind, slot) is not None, f"{phrase} -> {slot}"
                seen += 1
    assert seen >= 6                                   # the loop actually ran


def test_a_temporal_patch_is_a_phrase_the_temporal_resolver_can_read():
    """The handoff this module deliberately does not perform: it extracts the student's words, the caller
    turns them into a moment against the real timezone. A phrase `temporal` cannot parse would be a slot
    that never fills."""
    for phrase, goal in (("move it to friday", event_run()), ("push it to next tuesday at 3", event_run()),
                         ("make it aug 20 at 9am", event_run())):
        value = dict(resolve(phrase, goal=goal, pending=decision()).slot_patch)["start"]
        assert temporal.resolve(value, now=NOW) is not None, f"{phrase} -> {value!r}"

    # positive control: the resolver genuinely rejects things, so the assertion above has content.
    assert temporal.resolve("professional", now=NOW) is None


def test_a_day_and_a_time_stay_one_value():
    """"next tuesday at 3" is one moment. Carrying only "next tuesday" would silently drop the hour the
    student just gave — the same loss, one layer down."""
    patch = dict(resolve("push it to next tuesday at 3", goal=event_run(), pending=decision()).slot_patch)
    assert patch == {"start": "next tuesday at 3"}


def test_a_degree_word_travels_with_the_style_word():
    """"less formal" patched as "formal" would invert the instruction, and nothing downstream could tell."""
    assert dict(resolve("make it less formal", goal=email_run(), pending=decision()).slot_patch) \
        == {"tone": "less formal"}
    assert dict(resolve("make it formal", goal=email_run(), pending=decision()).slot_patch) \
        == {"tone": "formal"}


# --- pointing at something ---------------------------------------------------------------------------------

def test_this_with_an_inline_reply_resolves_to_the_replied_to_artifact():
    result = resolve("this", reply="msg-123", goal=email_run(), pending=decision())
    assert result.kind is K.reference
    assert result.referenced_artifact == "message:msg-123"
    assert cont.split_artifact(result.referenced_artifact) == (cont.ARTIFACT_MESSAGE, "msg-123")
    assert result.evidence == cont.REFERENCE_REPLY


def test_this_with_no_anchor_at_all_says_no_anchor_instead_of_guessing():
    result = resolve("this", goal=email_run(), pending=decision())
    assert result.kind is K.none
    assert result.evidence == cont.NO_ANCHOR
    assert result.referenced_artifact is None


def test_a_pointer_falls_back_to_a_draft_and_says_the_anchor_was_weaker():
    """A draft is a real anchor, and it is Bruce inferring rather than the student naming — the confidence
    has to record which one happened, because an irreversible send may hang off it."""
    replied = resolve("that one", reply="msg-5", goal=email_run(), draft={"id": "d-9"})
    inferred = resolve("that one", goal=email_run(), draft={"id": "d-9"})

    assert replied.referenced_artifact == "message:msg-5"
    assert inferred.referenced_artifact == "draft:d-9"
    assert inferred.evidence == cont.REFERENCE_DRAFT
    assert inferred.confidence < replied.confidence


def test_a_deictic_buried_in_a_sentence_is_not_a_pointer():
    """"i already sent this yesterday" is a statement. Treating every "this" as a pointer would attach
    unrelated turns to whatever run happened to be open."""
    assert resolve("i already sent this yesterday", reply="msg-5", goal=email_run()).kind is not K.reference
    assert resolve("this", reply="msg-5", goal=email_run()).kind is K.reference


# --- refusal -----------------------------------------------------------------------------------------------

def test_a_refusal_dominates_even_when_it_contains_an_action_phrase():
    """The P0 rule from `decision_resolver`, still holding one layer up: "actually nah dont send it" must
    never resolve to a confirmation."""
    result = resolve("actually nah dont send it", goal=email_run(), pending=decision("dec-3"))
    assert result.kind is K.reject
    assert result.target_decision_id == "dec-3"
    assert result.evidence == cont.REJECTION_OF_DECISION


def test_wait_stops_without_resolving_the_decision():
    """`decision_resolver` calls "wait" ambiguous — correctly, because it must not RESOLVE a yes/no. For
    continuation the question is "do I keep going", where a halt is unambiguous."""
    assert decision_resolver.resolve_approval("wait") is decision_resolver.Resolution.ambiguous

    result = resolve("wait", goal=email_run(), pending=decision())
    assert result.kind is K.reject and result.evidence == cont.HALT


def test_a_refusal_with_nothing_open_still_refuses_but_says_it_has_no_target():
    result = resolve("no dont", goal=None, pending=None)
    assert result.kind is K.reject
    assert result.evidence == cont.REJECTION_UNTARGETED
    assert result.target_run_id is None and result.target_decision_id is None
    assert result.confidence < resolve("no dont", goal=email_run(), pending=decision()).confidence


# --- the state the caller passes in --------------------------------------------------------------------------

def test_the_open_goal_may_be_a_run_row_or_the_goal_blob_itself():
    """Both shapes exist at real call sites. Requiring one would put a conversion step between the state
    and the resolver, which is where state gets lost."""
    row = email_run()
    from_row = resolve("make it professional", goal=row, pending=decision())
    from_blob = resolve("make it professional", goal=row["goal"], pending=decision())
    assert dict(from_row.slot_patch) == dict(from_blob.slot_patch) == {"tone": "professional"}
    assert from_row.target_run_id == "run-email" and from_blob.target_run_id is None


def test_a_decision_supplies_the_run_id_only_when_it_actually_names_one():
    """A decision's own id is not a run id. Returning it as one hands the caller a run that does not
    exist, and an update against a nonexistent run does nothing — silently."""
    named = resolve("send it", goal=None, pending={"id": "dec-1", "run_id": "run-7", "status": "pending"})
    assert (named.target_run_id, named.target_decision_id) == ("run-7", "dec-1")

    bare = resolve("send it", goal=None, pending={"id": "dec-1", "status": "pending"})
    assert bare.target_run_id is None and bare.target_decision_id == "dec-1"


def test_a_goal_that_names_its_capability_resolves_its_kind_through_the_registry():
    """A goal written before slots existed still amends, because the capability id joins to a kind. Free
    text does not — "sending messages" is not an operation and must not become one."""
    real = resolve("make it professional", goal={"capability": "gmail.send_message"}, pending=decision())
    assert dict(real.slot_patch) == {"tone": "professional"}

    prose = resolve("make it professional", goal={"capability": "sending messages"}, pending=decision())
    assert prose.kind is K.none and prose.evidence == cont.AMEND_WITHOUT_SLOT


def test_no_goal_at_all_means_an_amendment_has_nothing_to_amend():
    result = resolve("make it professional", goal=None, pending=decision())
    assert result.kind is K.none and result.evidence == cont.AMEND_WITHOUT_SLOT


# --- the layer's own invariants --------------------------------------------------------------------------------

def test_the_slot_table_matches_what_goal_slots_declares_and_the_check_can_fail():
    assert cont.check_slot_alignment() == ()

    drifted = cont.check_slot_alignment({cont.ROLE_STYLE: {GoalKind.send_email: "vibe"}})
    assert len(drifted) == 1 and "vibe" in drifted[0]


def test_every_evidence_string_is_from_the_declared_vocabulary():
    """Evidence is countable and switchable, not prose. The transcript's explanation was a sentence, which
    is why nothing downstream could act on it."""
    corpus = TRANSCRIPT + ("", "whats for lunch", "move it to friday", "call it practice",
                           "actually nah dont send it", "send it to coach@school.edu")
    for goal in (None, email_run(), event_run()):
        for pending in (None, decision()):
            for phrase in corpus:
                evidence = resolve(phrase, goal=goal, pending=pending, reply=None).evidence
                assert evidence in cont.EVIDENCE or evidence.startswith(cont.AMEND_PREFIX), \
                    f"{phrase!r} -> {evidence!r}"


def test_an_empty_turn_resolves_to_nothing_rather_than_to_the_last_thing_open():
    assert resolve("", goal=email_run(), pending=decision()).evidence == cont.NO_TEXT
    assert resolve(None, goal=email_run(), pending=decision()).evidence == cont.NO_TEXT


def test_the_result_cannot_be_edited_after_it_is_resolved():
    result = resolve("send it", goal=email_run(), pending=decision("dec-1"))
    assert result.target_decision_id == "dec-1"          # positive control: it is readable

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.kind = K.reject
    with pytest.raises(TypeError):
        result.slot_patch["tone"] = "professional"       # the patch is read-only too


def test_resolution_is_synchronous_and_repeatable():
    """A pure function is synchronous — it has no way to await IO — and gives the same answer twice for
    the same state. Both are what let this run before routing, on every turn, with no fixture."""
    assert not inspect.iscoroutinefunction(cont.resolve)

    state = dict(goal=event_run(), pending=decision(), reply="msg-1", draft={"id": "d-1"})
    first = resolve("move it to friday", **state)
    second = resolve("move it to friday", **state)
    assert first == second
    assert first.kind is K.amend                        # positive control: it resolved to something
