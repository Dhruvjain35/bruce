"""DEFECT-4 — the model is ordered to copy exact operation ids from a list the context never contained.

`conversation_model.py:69-77` instructs, in the system prompt Bruce ships:

    "CAPABILITIES ARE GIVEN TO YOU, NEVER GUESSED. The context contains the operations Bruce can run
     right now, as exact ids..."
    "required_capabilities MUST contain only exact operation ids copied from that list. Never invent a
     capability id."

There was no such list. `capability_snapshot.render()` emitted FAMILY NAMES ONLY —

    "Right now you CAN use: calendar, email."

— and `context_compiler.py:255` inserted exactly that string and nothing else. Verified behaviourally:
a snapshot holding ('calendar.create_event',) and ('gmail.send_message',) rendered a string in which
`"gmail.send_message" in render()` was False.

So the model was told to copy ids from a list it could not see, and it did the only thing available —
it wrote prose. `goal_runtime.creation_verdict` then rejects the prose with `NOT_AN_OPERATION_ID`,
`GoalHandler` declines, and the student gets a sentence instead of an email. The ids were already
computed and thrown away: `FamilyState.capabilities` holds them, per user, from `tool_broker`.

WHY THE LIST MUST BE FILTERED, and not simply dumped. `capability_snapshot.FAMILIES` names five ids, and
`turn_context`'s `available_operations` is wider still — every registry spec that is `live` and broker-ok,
which includes `gmail.get_message`, `gmail.get_thread` and `gmail.verify_sent`, none of which have an
executor at all. Advertising any of those does not fix the defect, it renames it: the model would copy a
real id, `GoalHandler` would find no goal kind or no executor, and the turn would decline just as before
with `capability_has_no_goal_kind` instead of `NOT_AN_OPERATION_ID`. Same student outcome, new label.

The one authority on "can this be carried to a verified provider call" is `goal_handler.executable`, and
this suite requires the advertised list to be derived from it rather than restated next to it.
"""

from __future__ import annotations

import asyncio
import uuid

from bruce_engine import capability_snapshot as cs
from bruce_engine import goal_handler

# Every id the snapshot's own family map knows about, split by whether it can actually be carried out.
ALL_DECLARED = tuple(cap for caps in cs.FAMILIES.values() for cap in caps)
EXECUTABLE = tuple(c for c in ALL_DECLARED if goal_handler.executable(c))
UNEXECUTABLE = tuple(c for c in ALL_DECLARED if not goal_handler.executable(c))

# A student whose Google account is fully connected: the broker says every declared capability is usable.
# This is the state the defect is worst in — nothing is missing, and Bruce still cannot act.
FULLY_LIVE = cs.CapabilitySnapshot(families=tuple(
    cs.FamilyState(family, True, capabilities=caps) for family, caps in cs.FAMILIES.items()))


def test_the_corpus_of_this_suite_is_not_degenerate():
    """If the registry ever gains executors for everything, the negative assertions below stop meaning
    anything — and they must fail loudly rather than pass vacuously."""
    assert EXECUTABLE, "no capability is executable at all; the whole send path is dead"
    assert UNEXECUTABLE, (
        "every declared capability is now executable, so 'do not advertise the unexecutable ones' is no "
        "longer a testable property here — widen the corpus or delete these tests deliberately")


def test_the_exact_executable_operation_ids_reach_the_rendered_snapshot():
    """THE DEFECT, stated positively. The prompt says 'copied from that list'; there must BE a list."""
    r = FULLY_LIVE.render()
    for cap in EXECUTABLE:
        assert cap in r, (
            f"{cap!r} is executable and usable for this student, and the model is instructed to copy "
            f"exact ids from the context — but the context never names it. Rendered: {r!r}")


def test_registered_but_unexecutable_operations_are_never_advertised():
    """Naming an id Bruce cannot carry out trades NOT_AN_OPERATION_ID for capability_has_no_goal_kind.
    The student experience is identical: prose instead of an action."""
    r = FULLY_LIVE.render()
    for cap in UNEXECUTABLE:
        assert cap not in r, (
            f"{cap!r} was advertised to the model but has no executor "
            f"(goal_handler.executable({cap!r}) is False), so a goal opened for it stalls after the last "
            f"question. Rendered: {r!r}")


def test_the_advertised_list_is_derived_from_the_executor_registry():
    """Not a second hard-coded list. A capability that gains an executor must become visible to the model
    the same day, and one that loses its executor must disappear the same day — two lists that must be
    edited together are two lists that will disagree."""
    advertised = cs.advertised_operations(FULLY_LIVE)
    assert set(advertised) == set(EXECUTABLE)
    assert all(goal_handler.executable(c) for c in advertised)


def test_the_advertised_list_TRACKS_the_executor_registry(monkeypatch):
    """THE PROPERTY THE PREVIOUS TEST CANNOT SEE.

    Comparing the advertised list against a set computed the same way passes just as happily when the
    filter is a hard-coded tuple that happens to match today — a mutation replacing the derivation with
    `cap in ("gmail.send_message", "calendar.create_event")` survived that assertion. Derivation is only
    observable when the registry MOVES, so this test moves it.
    """
    victim = EXECUTABLE[0]

    # Losing an executor must remove the id from the model's context the same day.
    pruned = {k: v for k, v in goal_handler._EXECUTORS.items() if k != victim}
    monkeypatch.setattr(goal_handler, "_EXECUTORS", pruned)
    assert victim not in cs.advertised_operations(FULLY_LIVE), (
        f"{victim!r} lost its executor but is still advertised — the list is restated, not derived")

    # Gaining one must add it the same day.
    candidate = UNEXECUTABLE[0]
    grown = dict(goal_handler._EXECUTORS)
    grown[candidate] = object()
    monkeypatch.setattr(goal_handler, "_EXECUTORS", grown)
    assert candidate in cs.advertised_operations(FULLY_LIVE), (
        f"{candidate!r} gained an executor but is still hidden from the model")


def test_a_family_whose_only_usable_capabilities_are_unexecutable_advertises_no_ids():
    """`gmail.find_reply` being reachable does not mean Bruce can reply. The family may still be named —
    it IS connected — but no id may be offered for it."""
    only_unexecutable = cs.CapabilitySnapshot(families=(
        cs.FamilyState("email", True, capabilities=UNEXECUTABLE),))
    r = only_unexecutable.render()
    assert cs.advertised_operations(only_unexecutable) == ()
    for cap in UNEXECUTABLE:
        assert cap not in r


def test_an_unusable_family_contributes_no_ids_even_if_executable():
    """Broker truth still wins. A disconnected account must not be advertised because its capability
    happens to have an executor — that is the false-promise direction of the same defect.

    The fixture deliberately carries a NON-EMPTY `capabilities` tuple on an unusable family. `snapshot()`
    never builds that shape today, so an empty tuple would let the `usable` check be deleted without any
    test noticing — a mutation removing it survived exactly that way. The guard has to be load-bearing
    against a populated family or it is not being tested at all.
    """
    disconnected = cs.CapabilitySnapshot(families=(
        cs.FamilyState("email", False, reason="not connected yet", capabilities=EXECUTABLE),))
    assert cs.advertised_operations(disconnected) == ()
    for cap in EXECUTABLE:
        assert cap not in disconnected.render()


def test_the_ids_survive_into_the_compiled_model_context():
    """THE PRODUCTION SURFACE. `render()` is only useful if `context_compiler` carries it to the model —
    that is the seam the original defect actually lived on (context_compiler.py:255)."""
    from bruce_engine import context_compiler

    async def _empty(*_a, **_k):
        return ""

    import pytest
    mp = pytest.MonkeyPatch()
    try:
        for fn in ("_world_block", "_operational_block", "_entity_block"):
            mp.setattr(context_compiler, fn, _empty)
        mp.setattr(context_compiler, "_episodic_block", lambda *a, **k: "")
        compiled = asyncio.run(context_compiler.compile(uuid.uuid4(), [], capabilities=FULLY_LIVE))
    finally:
        mp.undo()

    for cap in EXECUTABLE:
        assert cap in compiled.text, (
            f"{cap!r} reached render() but not the compiled context the reasoner is handed")
    for cap in UNEXECUTABLE:
        assert cap not in compiled.text


def test_the_family_names_are_still_readable_alongside_the_ids():
    """The families were not a mistake — 'you CAN use: calendar, email' is the sentence that stops a false
    denial, and M1's guarantee must survive this change rather than be replaced by it."""
    r = FULLY_LIVE.render()
    assert "calendar" in r and "email" in r
    assert "never deny one you can" in r.lower()
