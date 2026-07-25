"""C2 harness — a finished mission notifies the student EXACTLY ONCE, over the real relay path.

The exactly-once guarantee is split across two machines, so it is tested in two places:
  * ENGINE (real Postgres): the notifier hands off to the durable outbound queue, and the queue dedupes
    on `run_id:purpose`. A retry, a lease reclaim, or a worker restart cannot queue a second text,
    because the key is derived from the run rather than from wall-clock or an attempt count.
  * RELAY (in-process imsg fake): a returned guid is the ONLY thing that counts as an accepted handoff.
    An explicit decline is retryable; a crash or a missing guid is AMBIGUOUS, never reported as sent and
    never blindly resent.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, messaging_outbound, notifier as notifier_mod, schema
from bruce_engine.background_runner import PlanMissionAdvancer
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind
from bruce_engine.notifier import NotifierUnavailable, RelayNotifier
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

_handle_seq = iter(range(1000, 9999))


def _fresh_handle() -> str:
    """One handle per test: `uq_msg_identity` binds a handle to at most one user, so a shared constant
    would collide across tests in the same database rather than testing anything."""
    return f"+1555{next(_handle_seq)}"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _linked_user(handle: str | None = None):
    uid, handle = uuid4(), handle or _fresh_handle()
    await users.ensure(uid, auth_provider="test")
    async with user_session(uid) as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value,
                                       provider="self_hosted_imessage", channel_identity=handle))
    return uid, handle


async def _outbound(user_id) -> list:
    async with user_session(user_id) as s:
        return list((await s.execute(
            select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.user_id == user_id)
        )).scalars().all())


def _fake_run(user_id, run_id, to: str = "prof@nd.edu") -> dict:
    return {"id": str(run_id), "user_id": str(user_id), "goal": {"to": to, "notify": True},
            "recovery_state": {"results": {"1": {"reply": True}}}}


# --- 1. successful delivery ------------------------------------------------------------------------

def test_successful_delivery_queues_one_message_to_the_linked_handle():
    async def go():
        (uid, handle), rid = await _linked_user(), uuid4()
        await RelayNotifier()(uid, _fake_run(uid, rid), idempotency_key=f"{rid}:notify")
        rows = await _outbound(uid)
        assert len(rows) == 1
        row = rows[0]
        assert row.to_handle == handle
        assert row.channel == ChannelKind.self_hosted_imessage.value
        assert row.kind == notifier_mod.NOTIFY_KIND
        assert row.status == "pending"                 # queued; the RELAY is what actually delivers
        assert row.idempotency_key == f"{rid}:notify"  # run id + purpose
        assert "—" not in row.text                # outbound gate: never an em dash
    _run(go())


# --- 2. duplicate retry ----------------------------------------------------------------------------

def test_a_retried_notify_does_not_queue_a_second_imessage():
    """The advancer can be re-entered after a lease reclaim. The key is derived from the run, so the
    second and third hand-offs are no-ops rather than extra texts."""
    async def go():
        (uid, handle), rid = await _linked_user(), uuid4()
        key = f"{rid}:notify"
        for _ in range(3):
            await RelayNotifier()(uid, _fake_run(uid, rid), idempotency_key=key)
        assert len(await _outbound(uid)) == 1
    _run(go())


# --- 3. worker restart -----------------------------------------------------------------------------

def test_worker_restart_reuses_the_same_key_and_cannot_duplicate():
    """Nothing in the key is process-local, so a worker that restarts mid-notify converges on the same
    row instead of sending a second text."""
    async def go():
        (uid, handle), rid = await _linked_user(), uuid4()
        await RelayNotifier()(uid, _fake_run(uid, rid), idempotency_key=f"{rid}:notify")
        fresh = notifier_mod.build_notifier()        # as a restarted worker would construct it
        await fresh(uid, _fake_run(uid, rid), idempotency_key=f"{rid}:notify")
        assert len(await _outbound(uid)) == 1
    _run(go())


# --- 4. failures are loud and recoverable ----------------------------------------------------------

def test_no_linked_handle_raises_so_the_mission_is_not_marked_notified():
    """Nowhere to send is a REFUSAL, not a silent success: raising leaves the run un-notified so the
    runner retries, instead of completing on a delivery that never happened."""
    async def go():
        uid = uuid4()
        await users.ensure(uid, auth_provider="test")     # linked account, but NO messaging identity
        with pytest.raises(NotifierUnavailable):
            await RelayNotifier()(uid, _fake_run(uid, uuid4()), idempotency_key="k:notify")
        assert await _outbound(uid) == []
    _run(go())


def test_enqueue_failure_is_wrapped_and_raised(monkeypatch):
    async def go():
        (uid, handle), rid = await _linked_user(), uuid4()

        async def _boom(**_kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(messaging_outbound, "enqueue", _boom)
        with pytest.raises(NotifierUnavailable):
            await RelayNotifier()(uid, _fake_run(uid, rid), idempotency_key=f"{rid}:notify")
    _run(go())


def test_kill_switch_returns_none_so_no_run_claims_delivery(monkeypatch):
    monkeypatch.setenv("BRUCE_MISSION_NOTIFIER_OFF", "1")
    assert notifier_mod.build_notifier() is None, (
        "with the transport off a mission must complete WITHOUT stamping notified=true")


# --- 5. exactly one notification, driven through the REAL advancer ---------------------------------

def test_advancer_notifies_exactly_once_even_when_advanced_again():
    """The guarantee the student actually feels. Drive a finished mission through PlanMissionAdvancer
    three times: once normally, once with the checkpoint it produced, and once with the checkpoint
    LOST. Only one iMessage may be queued — the checkpoint stops the second, and the idempotency key
    stops the third even though the checkpoint is gone."""
    async def go():
        uid, handle = await _linked_user()
        run = await agent_run_store.enqueue_background(
            uid, domain="gmail", goal={"steps": [], "notify": True, "to": "prof@nd.edu"})

        adv = PlanMissionAdvancer(notifier=RelayNotifier())
        first = await adv.advance({**run, "recovery_state": {}})
        assert first.done is True and first.status == "completed"
        assert first.checkpoint.get("notified") is True

        await adv.advance({**run, "recovery_state": first.checkpoint})   # checkpoint says notified
        await adv.advance({**run, "recovery_state": {}})                 # checkpoint LOST

        assert len(await _outbound(uid)) == 1, "the student must receive exactly one iMessage"
    _run(go())


def test_a_failed_notify_leaves_the_mission_unnotified_for_retry():
    """If the hand-off raises, the advancer must not stamp notified — otherwise a retry would skip the
    notification entirely and the student would never hear anything."""
    async def go():
        uid = uuid4()
        await users.ensure(uid, auth_provider="test")     # no handle -> notifier raises
        run = await agent_run_store.enqueue_background(
            uid, domain="gmail", goal={"steps": [], "notify": True, "to": "prof@nd.edu"})
        adv = PlanMissionAdvancer(notifier=RelayNotifier())
        with pytest.raises(NotifierUnavailable):
            await adv.advance({**run, "recovery_state": {}})
        assert await _outbound(uid) == []
    _run(go())


# --- 6. relay side: only a guid counts as an accepted handoff --------------------------------------

def test_relay_missing_guid_is_ambiguous_never_success(tmp_path):
    """imsg answered but returned no confirmation guid. The bytes may already be gone, so this must be
    surfaced as ambiguous and must NOT be reported sent or blindly resent."""
    from relay.fake_imsg import InProcessImsg
    from tests.test_relay_component import FakeBackend, _relay
    from tests.test_relay_component import _run as _drive

    be = FakeBackend()
    be.claims = [{"id": "job-noguid", "to": "+1555", "text": "hi"}]
    imsg = InProcessImsg(no_guid=True)
    r = _relay(tmp_path, imsg, be)
    _drive(r.process_one_outbound())

    assert be.acks[0]["status"] == "terminal_failed"
    assert be.acks[0]["error"] == "handoff_unknown"
    assert be.acks[0]["guid"] is None
    assert imsg.calls == 1, "an ambiguous handoff must never trigger a blind resend"
