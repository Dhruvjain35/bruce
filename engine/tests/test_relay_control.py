"""Bite 1.5 A1 — relay control plane (server-side outbound kill + directives), against REAL Postgres.

Each security acceptance criterion of A1 gets a named test, exercised THROUGH the restricted ``bruce_app``
role (via ``pg_test_db`` / ``clean_db``) so the real RLS policies + the real claim path are the surfaces
under test — no SQLite, no mocks:

  * global outbound_paused  -> /v1/relay/outbound/claim hands out NOTHING even with a sendable message
  * per-device pause / stop -> same, for that device only; other devices unaffected
  * resume-all              -> claims restored
  * record_heartbeat        -> returns the current directive, stamps supervisor_seen_at CONTENT-FREE
  * heartbeat endpoint      -> returns the directive contract (run|pause_outbound|stop)
  * stale_devices           -> flags a device whose supervisor_seen_at is older than the threshold
  * relay_control RLS       -> a user_session (bruce_app) gets zero rows / denied writes (default-deny)

Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
import datetime
import types
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import messaging_outbound, relay_auth, relay_control, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind
from scripts import relay_killswitch

client = TestClient(api.app)
PHONE = "+15550001111"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _device():
    return _run(relay_auth.register_device("mac-test"))


def _hdrs(secret):
    return {"Authorization": f"Bearer {secret}", "X-Bruce-Timestamp": _now().isoformat(),
            "X-Bruce-Nonce": uuid4().hex, "X-Bruce-Request-Id": uuid4().hex}


def _stub(device_id: UUID):
    """A minimal stand-in for the authenticated RelayDevice (relay_control only reads .id)."""
    return types.SimpleNamespace(id=device_id, revoked_at=None)


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


def _enqueue(uid, key="k1"):
    _run(messaging_outbound.enqueue(user_id=uid, to_handle=PHONE, channel=ChannelKind.self_hosted_imessage,
                                    kind="acknowledged", text="hi", idempotency_key=key))


async def _msg_status(outbound_id):
    async with worker_session() as s:
        return (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.id == outbound_id))).scalar_one().status


async def _only_msg_status(uid):
    async with worker_session() as s:
        row = (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.user_id == uid))).scalar_one()
        return row.status


# --------------------------------------------------------------------------- global kill at the claim path


def test_global_pause_blocks_claim_even_with_sendable_message(clean_db):
    """A globally outbound_paused control -> claim returns nothing even though a sendable message exists,
    and the message is NEVER handed out (stays pending)."""
    uid = uuid4(); _run(_ensure_user(uid)); _enqueue(uid)
    _, secret = _device()
    _run(relay_control.pause_all(reason="triage"))

    r = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret))
    assert r.status_code == 204
    # never leased/handed out — the message is still pending (not 'sending')
    assert _run(_only_msg_status(uid)) == "pending"


def test_per_device_pause_blocks_only_that_device(clean_db):
    """A per-device pause_outbound directive blocks THAT device's claim; another device is unaffected and
    still claims the sendable message."""
    uid = uuid4(); _run(_ensure_user(uid)); _enqueue(uid)
    paused_id, paused_secret = _device()
    _, other_secret = _device()
    _run(relay_control.pause_device(paused_id, reason="isolate"))

    # paused device: hands out nothing
    assert client.post("/v1/relay/outbound/claim", headers=_hdrs(paused_secret)).status_code == 204
    assert _run(_only_msg_status(uid)) == "pending"

    # other device: unaffected -> claims the message
    ok = client.post("/v1/relay/outbound/claim", headers=_hdrs(other_secret))
    assert ok.status_code == 200 and ok.json()["text"] == "hi"


def test_stop_directive_blocks_claim(clean_db):
    """A per-device `stop` directive also short-circuits the claim path (device never sends)."""
    uid = uuid4(); _run(_ensure_user(uid)); _enqueue(uid)
    dev_id, secret = _device()
    _run(relay_control.set_directive(dev_id, relay_control.STOP))

    assert client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).status_code == 204
    assert _run(_only_msg_status(uid)) == "pending"


def test_resume_all_restores_claims(clean_db):
    """resume-all clears the global kill; a previously-blocked claim now succeeds."""
    uid = uuid4(); _run(_ensure_user(uid)); _enqueue(uid)
    _, secret = _device()

    _run(relay_control.pause_all())
    assert client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).status_code == 204

    _run(relay_control.resume_all())
    ok = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret))
    assert ok.status_code == 200 and ok.json()["text"] == "hi"


# --------------------------------------------------------------------------- heartbeat / directive contract


def test_record_heartbeat_returns_directive_and_stamps_supervisor_seen(clean_db):
    """record_heartbeat returns the current directive and stamps last_seen_at + supervisor_seen_at +
    agent_commit CONTENT-FREE (only a commit hash is persisted; a whitespace-y value is dropped)."""
    dev_id, _ = _device()

    d = _run(relay_control.record_heartbeat(_stub(dev_id),
             status={"agent_commit": "abc123", "uptime_s": 42.0, "restart_count": 1}))
    assert d == relay_control.RUN

    async def _read():
        async with worker_session() as s:
            return (await s.execute(select(schema.RelayDevice).where(schema.RelayDevice.id == dev_id))).scalar_one()
    row = _run(_read())
    assert row.supervisor_seen_at is not None and row.last_seen_at is not None
    assert row.agent_commit == "abc123"

    # content-free guard: a status field carrying whitespace/free text is NOT persisted
    _run(relay_control.record_heartbeat(_stub(dev_id), status={"agent_commit": "leaked message text"}))
    assert _run(_read()).agent_commit == "abc123"

    # under a global pause the directive returned flips to pause_outbound
    _run(relay_control.pause_all())
    assert _run(relay_control.record_heartbeat(_stub(dev_id), status={})) == relay_control.PAUSE_OUTBOUND


def test_heartbeat_endpoint_returns_directive(clean_db):
    """POST /v1/relay/heartbeat returns {"directive": ...}; run by default, pause_outbound once paused."""
    dev_id, secret = _device()
    r = client.post("/v1/relay/heartbeat", headers=_hdrs(secret), json={"agent_commit": "deadbeef"})
    assert r.status_code == 200 and r.json()["directive"] == "run" and r.json()["device_id"] == str(dev_id)

    _run(relay_control.pause_all())
    r2 = client.post("/v1/relay/heartbeat", headers=_hdrs(secret), json={})
    assert r2.status_code == 200 and r2.json()["directive"] == "pause_outbound"


# --------------------------------------------------------------------------- staleness alerting


def test_stale_devices_flags_old_supervisor_seen(clean_db):
    """stale_devices flags a device whose supervisor_seen_at is older than the threshold, and NOT a
    device that just heartbeated."""
    old_id, _ = _device()
    fresh_id, _ = _device()

    async def _age_old():
        async with worker_session() as s:
            await s.execute(sa_text(
                "UPDATE relay_devices SET supervisor_seen_at = now() - interval '10 minutes' WHERE id=:i"),
                {"i": str(old_id)})
    _run(_age_old())
    _run(relay_control.record_heartbeat(_stub(fresh_id), status={}))  # fresh supervisor_seen_at = now

    stale_ids = {d.id for d in _run(relay_control.stale_devices(300))}  # threshold 5 minutes
    assert old_id in stale_ids
    assert fresh_id not in stale_ids


# --------------------------------------------------------------------------- RLS default-deny (create_all DB)


def test_relay_control_rls_default_deny_for_tenant(clean_db):
    """A tenant (user_session as bruce_app) gets ZERO rows and a DENIED insert on relay_control, even
    though a worker-written row exists — proven on the create_all-BUILT migrated DB (0001 runs create_all;
    0014 layers worker_only RLS + FORCE on top). Mirrors the keystone default-deny test."""

    async def run():
        a = uuid4()
        await _ensure_user(a)
        # worker seeds a control row (the global pause switch)
        await relay_control.pause_all(reason="seed")

        # positive control: a worker_session DOES see the row
        async with worker_session() as s:
            assert (await s.execute(select(func.count()).select_from(schema.RelayControl))).scalar_one() >= 1

        # tenant READ: zero rows (RLS worker_only -> no tenant policy)
        async with user_session(a) as s:
            assert (await s.execute(select(func.count()).select_from(schema.RelayControl))).scalar_one() == 0

        # tenant WRITE: denied. A distinct environment => no unique conflict, so RLS is the ONLY cause.
        with pytest.raises(Exception):
            async with user_session(a) as s:
                await s.execute(schema.RelayControl.__table__.insert().values(
                    environment="denytest", outbound_paused=True))

    _run(run())


# --------------------------------------------------------------------------- operator CLI (smoke)


def test_killswitch_cli_status_and_pause_resume(clean_db, capsys):
    """The operator CLI pauses/resumes globally, pauses a device, and prints a redacted status (no
    secret). Exercised via the CLI's async _run, like capability_admin's tests."""
    dev_id, _ = _device()

    _run(relay_killswitch._run(types.SimpleNamespace(command="pause-all", reason="cli triage")))
    assert (await_paused := _run(relay_control.global_state()))[0] is True and await_paused[1] == "cli triage"

    _run(relay_killswitch._run(types.SimpleNamespace(command="pause-device", device=str(dev_id), reason="cli")))
    assert _run(relay_control.get_directive(_stub(dev_id))) == relay_control.PAUSE_OUTBOUND

    _run(relay_killswitch._run(types.SimpleNamespace(command="resume-all")))
    assert _run(relay_control.global_state())[0] is False

    _run(relay_killswitch._run(types.SimpleNamespace(command="status", stale_seconds=180)))
    out = capsys.readouterr().out
    assert str(dev_id) in out and "global outbound" in out
