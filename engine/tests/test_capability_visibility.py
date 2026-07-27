"""M1 — the model must SEE what Bruce can do, and may never deny a live capability.

THE DEFECT THIS CLOSES. `conversation_runtime` computed a ToolBroker shortlist every tool-bearing turn
and discarded it as shadow telemetry, while the system prompt told the model to "only claim a capability
Bruce actually has". Asked to be truthful about something it could not see, the model guessed — and on a
fully-scoped live Google connection it guessed wrong:

    "i can't add it to your calendar from here."

That false denial then blocked a P0 verification, because the turn never produced the calendar Decision
it should have. The runtime knew. The model was never told.

Two independent guarantees are tested here:
  1. the snapshot reaches the model's context, and reflects broker truth rather than a registry constant
  2. a reply that contradicts live capability is caught STRUCTURALLY, not by matching denial phrases —
     the previous guard was a phrase regex and missed a curly apostrophe in production.
"""

from __future__ import annotations

import asyncio

import pytest

from bruce_engine import capability_snapshot as cs
from bruce_engine import tool_broker

CAL_LIVE = cs.CapabilitySnapshot(families=(
    cs.FamilyState("calendar", True, capabilities=("calendar.create_event",)),
    cs.FamilyState("email", False, reason="not connected yet")))
NOTHING_LIVE = cs.CapabilitySnapshot(families=(
    cs.FamilyState("calendar", False, reason="not connected yet"),
    cs.FamilyState("email", False, reason="not connected yet")))
BOTH_LIVE = cs.CapabilitySnapshot(families=(
    cs.FamilyState("calendar", True, capabilities=("calendar.create_event",)),
    cs.FamilyState("email", True, capabilities=("gmail.send_message",))))


# --- the snapshot states truth plainly ----------------------------------------------------------------

def test_render_states_what_bruce_can_do_first():
    r = CAL_LIVE.render()
    assert "CAN use: calendar" in r
    assert "cannot use: email (not connected yet)" in r
    assert "never deny one you can" in r.lower()


def test_render_is_empty_when_nothing_is_known():
    assert cs.CapabilitySnapshot().render() == ""


def test_no_connected_tools_is_stated_honestly():
    assert "NO connected tools" in NOTHING_LIVE.render()


def test_usable_reflects_broker_status_not_the_registry():
    assert CAL_LIVE.is_usable("calendar") and not CAL_LIVE.is_usable("email")
    assert BOTH_LIVE.usable() == ("calendar", "email")


# --- the contradiction validator ----------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "i can’t add it to your calendar from here",      # THE production failure: curly apostrophe
    "i can't add that to your calendar",
    "i cant update your calendar",
    "i am not able to touch your calendar",           # the old regex missed this one entirely
    "i'm unable to change your calendar",
    "i don't have access to your calendar",
    "there's no way for me to put that on your calendar",
])
def test_denying_a_live_calendar_is_caught(reply):
    assert cs.contradicts(reply, CAL_LIVE) == "calendar"


@pytest.mark.parametrize("reply", [
    "sure, adding it now",
    "ok, i'll leave it off ur calendar.",             # a REFUSAL is not a capability denial
    "want me to put it on ur calendar?",
    "got it, not adding anything.",
])
def test_normal_calendar_replies_are_not_flagged(reply):
    assert cs.contradicts(reply, CAL_LIVE) is None


def test_a_TRUE_denial_is_not_flagged():
    """Email is genuinely unusable here, so declining it is honest and must pass untouched."""
    assert cs.contradicts("i cant send email from here", CAL_LIVE) is None


def test_denying_a_live_email_capability_is_caught():
    assert cs.contradicts("i can’t send email from here", BOTH_LIVE) == "email"


def test_nothing_is_flagged_when_nothing_is_live():
    for r in ("i can't add it to your calendar", "i cant send email"):
        assert cs.contradicts(r, NOTHING_LIVE) is None


# --- snapshot construction goes through broker truth ---------------------------------------------------

def test_snapshot_uses_tool_broker_availability(monkeypatch):
    seen = []

    async def _fake(_uid, cap):
        seen.append(cap)
        ok = cap.startswith("calendar.")
        return tool_broker.Availability(cap, tool_broker.OK if ok else tool_broker.DISCONNECTED,
                                        live=True, connected=ok, scoped=ok)

    monkeypatch.setattr(cs.tool_broker, "availability", _fake)
    import uuid
    snap = asyncio.run(cs.snapshot(uuid.uuid4()))
    assert snap.is_usable("calendar") and not snap.is_usable("email")
    assert any(c.startswith("calendar.") for c in seen), "must consult the broker, not the registry"
    assert "not connected yet" in snap.render()


def test_a_broker_error_degrades_that_family_not_the_snapshot(monkeypatch):
    """A missing snapshot returns the model to guessing, which is the failure being fixed. One broken
    family must not blank the rest."""
    async def _boom(_uid, cap):
        if cap.startswith("gmail."):
            raise RuntimeError("provider lookup exploded")
        return tool_broker.Availability(cap, tool_broker.OK, live=True, connected=True, scoped=True)

    monkeypatch.setattr(cs.tool_broker, "availability", _boom)
    import uuid
    snap = asyncio.run(cs.snapshot(uuid.uuid4()))
    assert snap.is_usable("calendar"), "a healthy family must survive a broken one"
    assert not snap.is_usable("email")
    assert snap.render() != ""


# --- the compiled context actually carries it ----------------------------------------------------------

def test_capability_block_is_the_highest_priority_context():
    """It must survive truncation: a context that drops capability truth returns the model to guessing."""
    from bruce_engine import context_compiler
    assert context_compiler._P_CAPABILITY > context_compiler._P_WORLD


def test_compile_accepts_and_renders_the_snapshot(monkeypatch):
    from bruce_engine import context_compiler

    async def _empty(*_a, **_k):
        return ""

    for fn in ("_world_block", "_operational_block", "_entity_block"):
        monkeypatch.setattr(context_compiler, fn, _empty)
    monkeypatch.setattr(context_compiler, "_episodic_block", lambda *a, **k: "")

    import uuid
    compiled = asyncio.run(context_compiler.compile(uuid.uuid4(), [], capabilities=CAL_LIVE))
    assert "CAN use: calendar" in compiled.text
    assert any(b.layer == "capability" for b in compiled.blocks)


def test_compile_without_a_snapshot_still_works():
    """Back-compat: callers that pass nothing must not break, they simply get no capability block."""
    from bruce_engine import context_compiler
    import uuid
    compiled = asyncio.run(context_compiler.compile(uuid.uuid4(), []))
    assert all(b.layer != "capability" for b in compiled.blocks)
