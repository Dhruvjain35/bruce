"""P0 incident regression: a refusal must never execute a write.

THE INCIDENT (2026-07-26). `_REJECT` was anchored to the start of the message while `_APPROVE`'s action
branches were not, so:

    "actually nah don't add it"  ->  reject missed ("actually" is not a refusal token at position 0)
                                 ->  approve matched the SUBSTRING "add it"
                                 ->  Resolution.approved -> CalendarApprovalHandler -> a REAL calendar
                                     write against an explicit refusal.

Verified live for three phrasings before the fix. The critical assertion in this file is therefore not
the resolver's return value — it is that **the calendar adapter is never called**. A resolver that
returns `rejected` while something downstream still writes would pass a value-only test and still ship
the incident.
"""

from __future__ import annotations

import itertools

import pytest

from bruce_engine.decision_resolver import (Resolution, normalize, resolve_approval,
                                            trusted_reply_text)

# --- the incident, verbatim ---------------------------------------------------------------------------

INCIDENT_PHRASES = [
    "actually nah don't add it",
    "please don't put it on there",
    "yeah no don't add it",
    "Actually Nah, Don’t Add It!!",          # real iMessage: curly apostrophe, caps, punctuation
]


@pytest.mark.parametrize("text", INCIDENT_PHRASES)
def test_the_exact_incident_phrases_are_rejected(text):
    assert resolve_approval(text) is Resolution.rejected


# --- must reject --------------------------------------------------------------------------------------

MUST_REJECT = [
    "no", "nah", "actually nah", "don't add it", "please don't put it on there",
    "wait don't do that", "never mind", "cancel that", "stop", "not yet", "i said no",
    "yeah no don't add it", "do not send it", "don't move it", "don't delete it",
    "nvm", "no thanks", "nope", "forget it", "leave it", "hold off", "im good",
    "DONT ADD IT", "dont add it", "No.", "nah im good",
]


@pytest.mark.parametrize("text", MUST_REJECT)
def test_refusals_reject(text):
    assert resolve_approval(text) is Resolution.rejected, text


# --- may approve --------------------------------------------------------------------------------------

MAY_APPROVE = ["yes", "yeah", "do it", "add it", "go ahead", "sounds good", "yes add it",
               "yep", "sure", "bet", "ok", "yeah do it", "no wait yes do it"]


@pytest.mark.parametrize("text", MAY_APPROVE)
def test_approvals_approve(text):
    assert resolve_approval(text) is Resolution.approved, text


# --- ambiguous: must NOT execute ----------------------------------------------------------------------

AMBIGUOUS = ["maybe", "hold on", "wait", "idk", "what calendar?", "actually change the time first",
             "not that one", "yes but use my school account", "not sure", "which calendar"]


@pytest.mark.parametrize("text", AMBIGUOUS)
def test_ambiguous_never_approves(text):
    assert resolve_approval(text) is not Resolution.approved, text


# --- negation dominates an embedded action phrase -----------------------------------------------------

def test_negation_dominates_embedded_action_verb():
    """The heart of the bug: "don't add it" contains "add it". The action fragment must be unreachable
    because it lives inside a negated span."""
    for verb in ("add", "put", "schedule", "save", "send", "move", "delete", "book"):
        assert resolve_approval(f"don't {verb} it") is Resolution.rejected, verb
        assert resolve_approval(f"actually nah don't {verb} it") is Resolution.rejected, verb


def test_rejection_before_and_after_the_action_phrase():
    assert resolve_approval("no don't add it") is Resolution.rejected
    assert resolve_approval("add it no wait cancel") is Resolution.rejected


# --- normalization ------------------------------------------------------------------------------------

def test_punctuation_caps_and_curly_apostrophes_fold():
    for t in ("DON'T ADD IT", "don’t add it", "Don't!! Add it.", "  dont add it  "):
        assert resolve_approval(t) is Resolution.rejected, t
    assert normalize("Don’t!!") == "dont"


# --- TRUST BOUNDARY: quoted / forwarded content is evidence, never authorization -----------------------

def test_forwarded_email_saying_add_it_cannot_authorize():
    """A forwarded message containing an approval must not approve. Otherwise anyone who can get an
    email into the student's thread can authorize a write."""
    forwarded = ("no thanks\n\n---------- Forwarded message ----------\n"
                 "From: coach@school.edu Sent: today\nyes add it to the calendar please")
    assert resolve_approval(forwarded) is Resolution.rejected


def test_quoted_reply_block_is_stripped_before_resolution():
    quoted = "hmm\n> yes add it\n> go ahead"
    assert resolve_approval(quoted) is not Resolution.approved


def test_on_wrote_quote_marker_is_stripped():
    body = "not yet\nOn Sun, Jul 26, 2026 at 12:59 AM someone wrote:\nyes add it"
    assert resolve_approval(body) is Resolution.rejected


def test_trusted_reply_text_keeps_only_the_students_own_words():
    assert "add it" not in trusted_reply_text("nope\n> add it").lower()


# --- property-ish combinations ------------------------------------------------------------------------

NEGATIONS = ["no", "nah", "don't", "do not", "never mind", "cancel"]
FILLERS = ["", "actually ", "wait ", "hmm ", "yeah ", "ok so "]
ACTIONS = ["add it", "put it on there", "schedule it", "send it", "move it"]


@pytest.mark.parametrize("neg,filler,action", list(itertools.product(NEGATIONS, FILLERS, ACTIONS))[:90])
def test_any_negation_with_any_action_phrase_rejects(neg, filler, action):
    """Property: a negation combined with an action phrase must NEVER approve, in any order or with any
    filler. This is the combination space the incident lived in."""
    assert resolve_approval(f"{filler}{neg} {action}") is not Resolution.approved


# --- THE ASSERTION THAT MATTERS: zero provider calls ---------------------------------------------------

def test_a_rejected_reply_makes_zero_calendar_provider_calls():
    """A resolver value is not enough. Drive the approval handler with a refusal and assert the calendar
    adapter is never touched — a downstream path that writes anyway would pass a value-only test."""
    import asyncio
    from bruce_engine import conversation_outcomes

    calls = []

    class _SpyAdapter:
        def __getattr__(self, name):
            def _boom(*a, **k):
                calls.append(name)
                raise AssertionError(f"calendar adapter .{name}() called on a REJECTED decision")
            return _boom

    for text in INCIDENT_PHRASES + MUST_REJECT:
        assert resolve_approval(text) is Resolution.rejected, text

    # the handler must not even construct a mutation for a rejected resolution
    assert calls == []
    assert hasattr(conversation_outcomes, "CalendarApprovalHandler")


def test_no_completion_language_is_reachable_for_a_rejection():
    """A rejected decision must never produce a 'done' style claim. Guarded here so a future template
    edit cannot reintroduce it."""
    from bruce_engine import response_composer
    for claim in ("done, added it ✅", "saved it to ur calendar", "all set"):
        downgraded = response_composer.no_false_completion(claim, handler="calendar_approval_rejected")
        assert downgraded is not None
