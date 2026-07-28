"""How many queries one turn actually issues, counted at the cursor.

#125 found that the router builds its own TurnContext and the ContextCompiler then reads overlapping
state again. That was inferred from stage timings, which is not evidence — a stage can be slow for
reasons that have nothing to do with duplication. This file counts statements at
`before_cursor_execute` and attributes each one to the `bruce_engine` frame that issued it, so the
duplication is a number rather than a suspicion.

It is also the regression guard: once #127A removes the duplicate reads, a repository call that comes
back has to fail here rather than quietly costing every turn a round trip.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import os
import re
import traceback
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import access_control, conversation_runtime, crypto, oauth_google, schema
from bruce_engine.conversation_contract import ConversationDecision, IntentKind, ResponseType, RiskLevel
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
PHONE = "+15550100"
COUNTS: collections.Counter = collections.Counter()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    def _counting_engine(url, **kw):
        kw.pop("poolclass", None)
        engine = _real_create_async_engine(url, poolclass=NullPool, **kw)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, params, context, executemany):
            flat = " ".join(statement.split())
            if not flat.lower().startswith(("select", "insert", "update", "delete")):
                return
            # CALLER ATTRIBUTION DOES NOT WORK HERE, and saying so is better than printing "unknown"
            # in a column headed "caller". SQLAlchemy's async layer runs the cursor event inside a
            # greenlet, so `extract_stack()` sees the plumbing and none of the `bruce_engine` frames
            # that decided to read. Attribution would need a contextvar set by each repository, which is
            # a change to production code that #127A's own refactor makes moot. The TABLE counts below
            # are the measurement; the readers are identified by grep in the PR description.
            frames = [f for f in traceback.extract_stack()
                      if f"{os.sep}bruce_engine{os.sep}" in f.filename
                      and os.path.basename(f.filename) != "db.py"]
            caller = (f"{os.path.basename(frames[-1].filename)}:{frames[-1].name}"
                      if frames else "-")
            m = re.search(r"\bfrom\s+([a-z_]+)", flat, re.I) or re.search(r"^(?:insert into|update)\s+([a-z_]+)", flat, re.I)
            COUNTS[(caller, m.group(1) if m else flat[:24])] += 1

        return engine

    monkeypatch.setattr(db, "create_async_engine", _counting_engine)
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None
    db._sessionmaker = None
    COUNTS.clear()
    yield
    COUNTS.clear()
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


class _FakeReasoner:
    provider = "fake"
    model = "fake"
    supports_vision = True

    async def decide(self, *, text, images, context):
        return ReasonResult(
            decision=ConversationDecision(
                intent=IntentKind.conversational, response_type=ResponseType.answer,
                user_visible_response="ok", extracted_entities=[], required_capabilities=[],
                needs_mission=False, risk_level=RiskLevel.none, confidence=0.8),
            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


def _message(text: str, pmid: str):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=[],
                          timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)


async def _seed(uid):
    await users.ensure(uid, auth_provider="test")
    await access_control.enroll_staging_test(uid, actor="test", reason="query counting")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id="me@example.com",
            scopes=["https://www.googleapis.com/auth/calendar.events"],
            refresh_token_encrypted=crypto.encrypt("rt"), selected_calendar_id="primary",
            status="connected"))


def _one_turn(text: str = "hey") -> collections.Counter:
    """Warm first, then measure. The first turn pays one-time work that is not per-turn cost, and
    counting it would report a duplication that does not exist."""
    uid = uuid4()
    _run(_seed(uid))
    runtime = conversation_runtime._Runtime(reasoner=_FakeReasoner())
    channel = FakeChannel()

    async def _go(pmid):
        await runtime.handle(channel, _message(text, pmid), user_id=uid, reply_target=PHONE)

    _run(_go(f"warm-{uuid4().hex[:6]}"))
    COUNTS.clear()
    _run(_go(f"measured-{uuid4().hex[:6]}"))
    return collections.Counter(COUNTS)


def _by_table(counts) -> collections.Counter:
    out: collections.Counter = collections.Counter()
    for (_caller, table), n in counts.items():
        out[table] += n
    return out


def test_report_the_query_profile_of_one_deterministic_turn(capsys):
    """The map, printed. Not an assertion about what is right — a measurement of what is."""
    counts = _one_turn()
    by_table = _by_table(counts)
    repeated = {t: n for t, n in by_table.items() if n > 1}

    with capsys.disabled():
        print(f"\nQUERY PROFILE — one deterministic conversational turn (warm)\n")
        print(f"  {'table':<30} n")
        for table, n in sorted(by_table.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {table:<30} {n}")
        print(f"\n  TOTAL: {sum(counts.values())} queries")
        print(f"\n  read more than once:")
        for t, n in sorted(repeated.items(), key=lambda kv: -kv[1]):
            print(f"    {t:<28} {n}")
        if not repeated:
            print("    (none)")

    assert sum(counts.values()) > 0, "the counter observed nothing — it is not wired to the engine"


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT, not a flaky test. One warm deterministic turn issues 35 queries and opens 16 "
    "database sessions. `integrations` is read 5 times, `agent_runs` twice and `user_world_state` "
    "twice, because fast_router builds its own TurnContext and ContextCompiler then reads overlapping "
    "state again. #127A's TurnStateSnapshot is what makes this pass; strict=True so it fails loudly "
    "the moment it does, rather than sitting green as a permanently-tolerated xfail."))
def test_a_deterministic_turn_reads_each_kind_of_state_at_most_once():
    """THE #127A CONTRACT, as a number.

    Each of these is state the turn needs exactly once. More than one read of the same table means two
    components each went and got it, which is the duplication #125 inferred from timing and this counts
    directly. Expressed as a per-table maximum rather than a total, so an unrelated new query elsewhere
    does not fail this and a genuine duplicate cannot pass it.
    """
    by_table = _by_table(_one_turn())
    limits = {
        "integrations": 1,          # capability truth / account binding
        "agent_runs": 1,            # active run
        "missions": 1,              # pending canonical Decision
        "user_world_state": 1,      # clock + timezone
        "calendar_event_entities": 1,
        "memory_records": 1,
    }
    over = {t: (n, limits[t]) for t, n in by_table.items() if t in limits and n > limits[t]}
    assert not over, f"state read more than once in one turn: {over}"


def test_two_turns_do_not_share_state_between_users():
    """A snapshot is per turn and per student. Counting is the cheap way to notice if one turn ever
    starts answering from another's reads."""
    a, b = uuid4(), uuid4()
    _run(_seed(a))
    _run(_seed(b))
    runtime = conversation_runtime._Runtime(reasoner=_FakeReasoner())
    channel = FakeChannel()

    async def _go(uid, pmid):
        await runtime.handle(channel, _message("hey", pmid), user_id=uid, reply_target=PHONE)

    _run(_go(a, "warm-a"))
    COUNTS.clear()
    _run(_go(b, "measured-b"))
    assert sum(COUNTS.values()) > 0, "the second user's turn issued no queries — state leaked"
