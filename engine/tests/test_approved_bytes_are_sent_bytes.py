"""THE BOUNDARY: voice policy owns Bruce's speech; authorization owns user-approved payloads.

Two kinds of text leave this system and they obey different laws.

    Bruce conversational text      -> voice/style gate: PROHIBITED_PHRASES, persona, punctuation
    approved consequential payload -> exact-byte integrity, safety/policy, authorization, provider limits

CONFLATING THEM PRODUCED TWO DEFECTS THAT PULL IN OPPOSITE DIRECTIONS, and this suite pins the resolution
of both.

DEFECT-13, as originally found: `gate_outbound_text` strips corporate filler and rewrites em dashes on
every plain-text outbound. The proposal the student read went through it; the `body` slot handed to
`gmail_send_args` did not. So the student was shown " talk about the extension" and the professor
received "I'd be happy to talk about the extension — Tuesday or Wednesday works for me."

THE FIRST FIX WAS WRONG IN DIRECTION. It closed the divergence by running the payload THROUGH the voice
gate, which made both copies the mangled one — the professor now received a sentence with its opening
clause deleted. Making the shown and sent bytes agree by damaging both is not integrity.

The mistake both versions share is treating the payload as a kind of Bruce-speech needing special
handling. It is not Bruce speaking. It is the student's outgoing message, quoted for approval. So the
boundary is a TYPE, and `gate_outbound_text` REFUSES it — a protected span inside the gate would have
left the payload in the gate's domain, one refactor from being styled again.

What still applies to a payload: authorization, safety and policy, provider limits, verified read-back,
and byte-exact equality between what was approved and what is sent. What stops applying is only the rules
governing how Bruce sounds.
"""

from __future__ import annotations

import pytest

from bruce_engine import authorization_evidence as ae
from bruce_engine import consequential_payload as cp
from bruce_engine.consequential_payload import (ApprovedConsequentialPayload,
                                                PayloadEnteredVoicePipeline, freeze)
from bruce_engine.conversation_style import PROHIBITED_PHRASES
from bruce_engine.messaging import ChannelKind
from bruce_engine.messaging_outbound import gate_outbound_text

PLAIN = ChannelKind.self_hosted_imessage.value

# One phrase from the voice gate's own list, and an em dash. Both are things BRUCE must never say and a
# student's email may perfectly well contain.
PROHIBITED = "I'd be happy to"
assert any(p in PROHIBITED.lower() for p in PROHIBITED_PHRASES), "fixture drifted from the real list"

BODY = (f"Hi Professor Chen,\n\n{PROHIBITED} talk about the extension — Tuesday or Wednesday "
        f"works for me.\n\nThanks,\nSam")
SUBJECT = "Extension request — CS 121"


def _payload(body: str = BODY, subject: str = SUBJECT) -> ApprovedConsequentialPayload:
    return ApprovedConsequentialPayload(capability="gmail.send_message",
                                        fields={"subject": subject, "body": body})


# --- 1 & 2: an approved payload ships exactly as approved ---------------------------------------------

def test_an_approved_payload_containing_a_prohibited_style_phrase_sends_unchanged():
    """The phrase is banned for BRUCE. The student's email is not Bruce speaking."""
    p = _payload()
    assert PROHIBITED in p.for_execution()["body"]
    assert p.for_execution()["body"] == BODY
    assert p.render_for_display().count(PROHIBITED) == 1, (
        "the copy the student is shown must contain the same bytes that will be sent")


def test_an_approved_payload_containing_an_em_dash_sends_unchanged():
    p = _payload()
    assert "—" in p.for_execution()["body"]
    assert "—" in p.for_execution()["subject"]
    assert "—" in p.render_for_display()


def test_the_shown_bytes_and_the_sent_bytes_are_the_same_bytes():
    """DEFECT-13's actual property, now satisfied by NOT touching the payload rather than by styling it."""
    p = _payload()
    shown = p.render_for_display()
    for value in p.for_execution().values():
        assert value in shown


# --- 3: Bruce's own speech is still fully governed -----------------------------------------------------

def test_bruce_conversational_text_with_the_same_phrase_still_fails_the_voice_gate():
    """Nothing here is a loosening. The identical string, spoken by Bruce, is still stripped."""
    said = f"{PROHIBITED} help you with that — want me to send it?"
    gated = gate_outbound_text(said, PLAIN)
    assert PROHIBITED.lower() not in gated.lower(), "the voice gate stopped governing Bruce's own words"
    assert "—" not in gated


def test_the_voice_gate_is_not_weakened_for_anything_it_accepts():
    for phrase in ("as an ai", "great question", "feel free to", "i hope this helps"):
        assert phrase not in gate_outbound_text(f"{phrase} ok", PLAIN).lower()


# --- 4: a byte changed after approval blocks execution ------------------------------------------------

def test_changing_one_payload_byte_after_approval_blocks_execution():
    """Approval binds the exact representation; execution re-derives it from what is actually being sent."""
    approved = _payload()
    assert approved.digest() == _payload().digest(), "the digest must be stable for identical bytes"

    tampered = _payload(body=BODY.replace("Tuesday", "Thursday"))
    assert tampered.digest() != approved.digest()
    assert not approved.matches(tampered.for_execution())


def test_a_whitespace_only_change_is_still_a_different_payload():
    """THE HOLE THIS CLOSES. `authorization_evidence._normalize_value` flattened `\\s+` before hashing, so
    a body whose paragraph breaks became spaces after approval still matched its own fingerprint and
    passed `execution_gate.require`. "\\n\\n" and "\\n" are two different emails."""
    approved = _payload()
    flattened = _payload(body=BODY.replace("\n\n", " "))
    assert flattened.digest() != approved.digest()
    assert not approved.matches(flattened.for_execution())

    # ...and the same must now hold through the fingerprint the execution gate actually compares.
    fp_a = ae.fingerprint(ae.normalize_arguments("gmail", "send_message", approved.for_execution()))
    fp_b = ae.fingerprint(ae.normalize_arguments("gmail", "send_message", flattened.for_execution()))
    assert fp_a != fp_b, (
        "the execution fingerprint still flattens whitespace, so a body reflowed after approval would "
        "pass the gate that exists to catch exactly that substitution")


def test_a_recipient_is_still_normalized_as_an_address():
    """Not everything binds byte-exactly, and that is deliberate: an address is case-insensitive in
    practice. Only the prose a student read is exempt from normalization."""
    a = ae.fingerprint(ae.normalize_arguments("gmail", "send_message", {"to": "Prof@Stanford.edu"}))
    b = ae.fingerprint(ae.normalize_arguments("gmail", "send_message", {"to": "prof@stanford.edu"}))
    assert a == b


# --- 5: the boundary is structural ---------------------------------------------------------------------

def test_routing_an_approved_payload_through_the_voice_gate_fails_structurally():
    """Not sanitized, not silently stringified — refused, at the call site."""
    with pytest.raises(PayloadEnteredVoicePipeline):
        gate_outbound_text(_payload(), PLAIN)


def test_interpolating_a_payload_into_bruce_text_fails_loudly():
    """The other way the boundary could be crossed: `f"...{payload}"` would hand the gate a plain string
    and the type would be gone. `__str__` raises so that mistake cannot be made quietly."""
    p = _payload()
    with pytest.raises(PayloadEnteredVoicePipeline):
        _ = f"here's the draft: {p}"


def test_the_gate_refuses_any_non_text_it_is_handed():
    with pytest.raises(PayloadEnteredVoicePipeline):
        gate_outbound_text({"body": BODY}, PLAIN)


# --- the frozen type itself ----------------------------------------------------------------------------

def test_freeze_takes_only_the_consequential_text_fields():
    p = freeze("gmail.send_message",
               {"subject": SUBJECT, "body": BODY, "to": "prof@stanford.edu", "attachments": []})
    assert set(p.fields) == {"subject", "body"}, (
        "a recipient is bound by authorization_evidence as an ADDRESS; freezing it here would bind it "
        "twice under two different rules")


def test_the_payload_is_immutable():
    p = _payload()
    with pytest.raises(Exception):
        p.fields["body"] = "something else"
    with pytest.raises(Exception):
        p.capability = "calendar.create_event"


def test_a_non_string_field_is_rejected_at_construction():
    with pytest.raises(TypeError):
        ApprovedConsequentialPayload(capability="gmail.send_message", fields={"body": 42})


def test_the_exact_field_list_is_shared_with_the_fingerprint():
    """One list, so the type and the fingerprint cannot disagree about which bytes are consequential."""
    from bruce_engine.authorization_evidence import _normalize_value
    for field in cp.EXACT_TEXT_FIELDS:
        assert _normalize_value(field, "a  b\n\nc") == "a  b\n\nc", f"{field} was normalized"
    assert _normalize_value("note", "a  b\n\nc") == "a b c", "a non-payload field should still normalize"


def test_a_calendar_title_is_still_whitespace_insensitive():
    """THE LINE, and it was drawn by an existing test rather than by taste.

    `title` was in EXACT_TEXT_FIELDS at first and broke
    `test_the_fingerprint_ignores_what_does_not_change_the_world_and_nothing_else`, which pins
    " chess  club " and "chess club" as the SAME calendar event. That test is right: a doubled space in
    an event label does not change the world, and forcing a re-approval over one would be consent
    theatre. Exact binding is for prose the student read AS prose — an email subject and body are the
    message; a calendar title is a label on an act whose substance is its time and its attendees.
    """
    from bruce_engine.authorization_evidence import _normalize_value
    assert "title" not in cp.EXACT_TEXT_FIELDS
    assert _normalize_value("title", " chess  club ") == "chess club"


# --- the display path ----------------------------------------------------------------------------------

def test_the_composed_payload_is_stored_exactly_as_composed(monkeypatch):
    """NO STYLISTIC REWRITE BEFORE APPROVAL EITHER — this is the seam the wrong fix lived on.

    `GoalHandler._compose` is where the drafted subject and body become slots, and therefore where they
    become the payload the student will approve and the provider will receive. The first attempt at
    DEFECT-13 ran them through the outbound VOICE gate right here, which is how "I'd be happy to talk
    about the extension" reached a professor as "talk about the extension".

    Without this test the whole suite passes while `_compose` restyles everything, because every other
    test constructs its payload directly. The mutation `post_approval_styling_reintroduced` survived
    exactly that gap.
    """
    import asyncio

    from bruce_engine import goal_handler, goal_runtime

    captured = {}

    async def _fake_ensure_goal(user_id, *, capability, conversation_id, slots_in, turn_index, decision):
        captured.update(slots_in)
        return "sentinel"

    class _Composer:
        async def compose(self, **_kw):
            return {"subject": SUBJECT, "body": BODY}

    class _Msg:
        text = "email professor chen about an extension"

    class _Octx:
        user_id = None
        pmid = "pmid-1"
        decision = None
        conversation_id = "conv-1"
        msg = _Msg()

    class _Step:
        missing = ("subject", "body")

    class _View:
        run_id = "run-1"
        slots = {}

    monkeypatch.setattr(goal_runtime, "ensure_goal", _fake_ensure_goal)
    monkeypatch.setattr(goal_handler, "compose_objective", lambda *a, **k: "objective")
    monkeypatch.setattr(goal_handler, "_recent_context", lambda *a, **k: "")

    asyncio.run(goal_handler.GoalHandler(composer=_Composer())._compose(
        _Octx(), _View(), "gmail.send_message", _Step(), turn_index=1, tz="America/Chicago"))

    assert captured["body"].value == BODY, (
        "the composed body was rewritten before it ever reached the student — the voice gate has no "
        "authority over a message the student is sending")
    assert captured["subject"].value == SUBJECT
    assert PROHIBITED in captured["body"].value
    assert "—" in captured["body"].value and "—" in captured["subject"].value


def test_enqueue_appends_the_payload_after_gating_bruce_words(monkeypatch):
    """Bruce's prose is gated; the payload beneath it is verbatim. Both in one message."""
    import asyncio

    from bruce_engine import messaging_outbound as mo

    captured = {}

    async def _fake_write_all(**kw):
        captured.update(kw)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mo, "_persist", _fake_write_all, raising=False)

    text = gate_outbound_text(f"{PROHIBITED} send this for you", PLAIN)
    p = _payload()
    combined = f"{text}\n\n{p.render_for_display()}"

    assert PROHIBITED.lower() not in text.lower(), "Bruce's half must still be gated"
    assert PROHIBITED in combined, "the payload's half must be verbatim"
    assert "—" in combined, "the payload keeps its em dash even on a plain-text channel"
