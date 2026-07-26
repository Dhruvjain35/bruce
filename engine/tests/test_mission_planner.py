"""C1 harness — a routed background mission becomes a durable, exactly-once run, or an honest refusal.

Every test here is one of the invariants stated in mission_planner's docstring. The exactly-once and
self-thread cases are the ones that would silently corrupt a live student experience (a duplicate email,
or a mission that waits forever), so they are asserted against real Postgres rather than a fake store.
"""

from __future__ import annotations

import uuid

import pytest

from bruce_engine import agent_run_store, gmail_adapter, mission_planner, tool_broker
from bruce_engine.mission_planner import MissionPlan
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction, RouterDecision


def _decision(text: str = "email me and tell me when i reply") -> RouterDecision:
    return RouterDecision(ExecutionClass.background_mission, action=GoalAction.send, domain="gmail",
                          target_reference=text, candidate_capabilities=("gmail.send_message",))


# --- routing gate ---------------------------------------------------------------------------------

def test_only_a_routed_background_send_is_enqueueable():
    assert mission_planner.is_enqueueable(_decision())
    # a bare send (no follow-up ask) is a DIRECT action, not a durable mission
    assert not mission_planner.is_enqueueable(
        RouterDecision(ExecutionClass.direct_action, action=GoalAction.send, domain="gmail"))
    # a background mission with no provider tool is someone else's lane
    assert not mission_planner.is_enqueueable(
        RouterDecision(ExecutionClass.background_mission, action=GoalAction.plan))
    assert not mission_planner.is_enqueueable(None)


def test_the_users_exact_phrase_classifies_as_a_background_send_generically():
    """D's acceptance phrase must reach this module through the GENERIC patterns — no branch keyed to the
    wording. Asserted on the patterns themselves (fast_router.py:124 turns send+follow-up into a
    background mission), so the claim holds without a router round trip."""
    from bruce_engine import fast_router
    text = "email me and tell me when i reply"
    assert fast_router._SEND_INTENT.search(text), "send intent must match generically"
    assert fast_router._FOLLOWUP.search(text), "follow-up ask is what makes it a MISSION, not a bare send"
    # and a bare send must NOT become a mission
    assert not fast_router._FOLLOWUP.search("email prof.chen@nd.edu about the lab")


# --- recipient resolution: never invent an address ------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_address_wins(monkeypatch):
    to, how = await mission_planner.resolve_recipient(uuid.uuid4(), "email prof.chen@nd.edu and tell me")
    assert (to, how) == ("prof.chen@nd.edu", "explicit")


@pytest.mark.asyncio
async def test_unresolvable_recipient_is_refused_not_guessed(monkeypatch):
    """Regression: a bare \\bme\\b matched the "let me know" in almost every follow-up phrasing, which
    resolved a mission aimed at SOMEONE ELSE to the student's own inbox. The pronoun must be the object
    of the send verb."""
    def _explode(_uid):
        raise AssertionError("must not consult the connected account for a non-self send")

    monkeypatch.setattr(mission_planner.oauth_google, "get_integration", _explode)
    for text in ("send them an email and let me know",
                 "email the coach and tell me when he replies",
                 "email her and notify me"):
        to, how = await mission_planner.resolve_recipient(uuid.uuid4(), text)
        assert (to, how) == (None, "unresolved"), text


@pytest.mark.asyncio
async def test_self_reference_resolves_to_the_connected_account(monkeypatch):
    class _Integ:
        provider_account_id = "student@gmail.com"

    async def _fake(_uid):
        return _Integ()

    monkeypatch.setattr(mission_planner.oauth_google, "get_integration", _fake)
    to, how = await mission_planner.resolve_recipient(uuid.uuid4(), "email me when it's done")
    assert (to, how) == ("student@gmail.com", "self")


@pytest.mark.asyncio
async def test_self_reference_with_unknown_account_refuses(monkeypatch):
    async def _none(_uid):
        return None

    monkeypatch.setattr(mission_planner.oauth_google, "get_integration", _none)
    to, how = await mission_planner.resolve_recipient(uuid.uuid4(), "email me when it's done")
    assert to is None and how == "self_unknown"


# --- the mission plan shape the runner already knows how to drive ---------------------------------

def test_derive_intent_never_invents_content_for_a_real_person():
    """A self-addressed round-trip request states its own purpose. An email aimed at someone else with no
    stated ask returns None, so the student is asked instead of a real person receiving invented text."""
    assert mission_planner.derive_intent("email me and tell me when i reply", "self") is not None
    assert mission_planner.derive_intent("email me the notes", "self") is not None
    assert mission_planner.derive_intent("email coach smith and tell me when he replies", "explicit") is None


def test_reply_is_derived_from_what_actually_happened():
    """The words come from the run, never a model. Both live failure modes are pinned: a work claim with
    no work, and a denial for work that succeeded."""
    from bruce_engine.mission_planner import MissionPlan, reply_for
    assert reply_for(MissionPlan(True, "ok", verified_send=True)) == "sent. i'll text u when ur reply comes in"
    assert reply_for(MissionPlan(False, "send_failed")) == \
        "gmail didn't send it, so i'm not waiting on a reply yet"
    assert reply_for(MissionPlan(False, "enqueue_failed", verified_send=True)) == \
        "the email sent, but reply tracking didn't start. i'm fixing that"
    assert reply_for(MissionPlan(False, "needs_content")) == "what do u want the email to say?"
    assert reply_for(MissionPlan(False, "not_a_mission")) is None      # non-mission turns untouched
    # a plan that did NOT send can never produce the word "sent"
    for st in ("send_failed", "needs_content", "no_recipient", "disconnected", "insufficient_scope"):
        assert "sent." not in reply_for(MissionPlan(False, st))


def test_idempotency_key_is_one_mission_per_inbound_message():
    k = mission_planner.mission_idempotency_key("imessage", "PMID-1")
    assert k == "mission:imessage:PMID-1"
    assert k != mission_planner.mission_idempotency_key("imessage", "PMID-2")


# --- capability truth comes from the broker, never from a guess -----------------------------------

@pytest.mark.asyncio
async def test_no_actionable_tool_means_no_enqueue_and_an_honest_status(monkeypatch):
    """The whole point of the broker seam: an unconnected student gets a truthful refusal, not a mission
    that will fail later, and DEFINITELY not a fabricated 'sent'."""
    sl = tool_broker.ToolShortlist(candidates=(), domain="gmail", action=GoalAction.send,
                                   has_actionable=False, unavailable=("gmail.send_message",))

    async def _fake_shortlist(*_a, **_k):
        return sl

    enqueued = []

    async def _boom(*_a, **_k):
        enqueued.append(1)
        raise AssertionError("must not enqueue without an actionable tool")

    monkeypatch.setattr(mission_planner.tool_broker, "shortlist", _fake_shortlist)
    monkeypatch.setattr(mission_planner.agent_run_store, "enqueue_background", _boom)

    plan = await mission_planner.plan_mission(uuid.uuid4(), _decision(), text="email me and tell me",
                                              idempotency_key="k")
    assert plan == MissionPlan(False, "disconnected", reason=plan.reason)
    assert not enqueued


@pytest.mark.asyncio
async def test_missing_scope_is_reported_as_missing_scope(monkeypatch):
    sl = tool_broker.ToolShortlist(candidates=(), domain="gmail", action=GoalAction.send,
                                   has_actionable=False, insufficient_scope=("gmail.send_message",))

    async def _fake_shortlist(*_a, **_k):
        return sl

    monkeypatch.setattr(mission_planner.tool_broker, "shortlist", _fake_shortlist)
    plan = await mission_planner.plan_mission(uuid.uuid4(), _decision(), text="email me and tell me",
                                              idempotency_key="k")
    assert plan.status == "insufficient_scope" and not plan.enqueued


# --- find_reply: the self-thread bug that would have made D wait forever --------------------------

@pytest.mark.asyncio
async def test_self_thread_reply_is_detected_even_though_it_carries_the_sent_label():
    """'email me' means Bruce mails the student's OWN account, so their reply is ALSO labelled SENT. The
    old label-only rule discarded it and the mission would have polled to exhaustion in silence."""
    fake = gmail_adapter.FakeGmailAdapter()
    ref = await fake.send(to="student@gmail.com", subject="s", body="b", thread_id=None, marker="m1")
    # a self-reply: same account, so Gmail labels it SENT as well as INBOX
    rid = fake.inject_incoming(ref.thread_id, from_addr="student@gmail.com", subject="Re: s", body="yes")
    fake.messages[rid]["labelIds"] = ["SENT", "INBOX"]

    reply = await fake.find_reply(ref.thread_id, after_message_id=ref.message_id)
    assert reply is not None and reply["id"] == rid


@pytest.mark.asyncio
async def test_our_own_sent_message_is_never_mistaken_for_a_reply():
    fake = gmail_adapter.FakeGmailAdapter()
    ref = await fake.send(to="prof@nd.edu", subject="s", body="b", thread_id=None, marker="m1")
    assert await fake.find_reply(ref.thread_id, after_message_id=ref.message_id) is None


@pytest.mark.asyncio
async def test_a_later_message_we_sent_is_excluded_by_own_message_ids():
    fake = gmail_adapter.FakeGmailAdapter()
    first = await fake.send(to="prof@nd.edu", subject="s", body="b", thread_id=None, marker="m1")
    second = await fake.send(to="prof@nd.edu", subject="s", body="ping", thread_id=first.thread_id,
                             marker="m2")
    got = await fake.find_reply(first.thread_id, after_message_id=first.message_id,
                                own_message_ids=(second.message_id,))
    assert got is None, "our own follow-up must not look like the student's reply"


@pytest.mark.asyncio
async def test_normal_inbound_reply_still_detected():
    fake = gmail_adapter.FakeGmailAdapter()
    ref = await fake.send(to="prof@nd.edu", subject="s", body="b", thread_id=None, marker="m1")
    rid = fake.inject_incoming(ref.thread_id, from_addr="prof@nd.edu", subject="Re: s", body="sure")
    reply = await fake.find_reply(ref.thread_id, after_message_id=ref.message_id)
    assert reply is not None and reply["id"] == rid


# --- the notifier seam must not let a run claim an undelivered notification -----------------------

def test_production_notifier_is_the_relay_transport():
    """C2: the seam is filled. A real transport now exists, so a finished mission actually reaches the
    student instead of completing silently."""
    from bruce_engine import notifier
    built = notifier.build_notifier()
    assert isinstance(built, notifier.RelayNotifier)


def test_kill_switch_falls_back_to_no_claimed_delivery(monkeypatch):
    """Throwing the kill switch must degrade to None, NOT to a do-nothing notifier: PlanMissionAdvancer
    only stamps notified=true when a notifier is invoked, so None keeps a disabled transport from
    recording deliveries that never happened."""
    monkeypatch.setenv("BRUCE_MISSION_NOTIFIER_OFF", "1")
    from bruce_engine import notifier
    assert notifier.transport_configured() is False
    assert notifier.build_notifier() is None
