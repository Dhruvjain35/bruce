"""THE BASELINE. The production inbound handler, measured as it is, before anything is optimized.

This is deliberately not a microbenchmark. It drives `conversation_runtime._Runtime.handle` — the same
function a real iMessage takes — against real Postgres, with the real router, the real context compiler,
the real memory retriever and the real outbound enqueue. The only fakes are the two things that would
otherwise measure someone else's network: the reasoner and the provider adapters.

WHAT THAT MEANS FOR THE NUMBERS, STATED UP FRONT. With a fake reasoner, the model call costs ~0ms. So
these figures are NOT end-to-end response latency for a semantic turn; they are the latency of
everything Bruce does AROUND the model, which is precisely the part the next three PRs change. The model
call is measured separately and honestly in `#129`, against the real provider. Reporting a fake-model
number as a semantic p95 would be the "measuring a different path from production" failure the
acceleration program names.

COLD AND WARM ARE NEVER MIXED. The first traced turn in a process pays imports, pool construction and
first-query planning; every later turn does not. On this system that gap has been measured at 4.5s in
Cloud Run. They are reported as separate distributions.
"""

from __future__ import annotations

import asyncio
import statistics
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_runtime, crypto, oauth_google, schema, turn_trace as tt
from bruce_engine.conversation_contract import ConversationDecision, IntentKind, ResponseType, RiskLevel
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.db import user_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
ACCOUNT = "me@example.com"
CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"

# Big enough for a stable p95, small enough that the suite stays usable. p99 over 40 samples is one
# sample, and is reported as such rather than dressed up.
WARM_TURNS = 40


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None
    db._sessionmaker = None
    tt.clear()
    tt.reset_process_state()
    yield
    tt.clear()
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _seed(uid):
    from bruce_engine import access_control
    await users.ensure(uid, auth_provider="test")
    await access_control.enroll_staging_test(uid, actor="test", reason="latency baseline")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
            scopes=[CAL, GSEND], refresh_token_encrypted=crypto.encrypt("rt-secret"),
            selected_calendar_id="primary", status="connected"))


class _FakeReasoner:
    """A fixed decision with no network. Present so the baseline measures BRUCE, not OpenAI — and named
    so nobody reads the resulting p95 as a semantic response time."""

    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, raises: bool = False):
        self._raises = raises

    async def decide(self, *, text, images, context):
        if self._raises:
            raise RuntimeError("reasoner down")
        return ReasonResult(
            decision=ConversationDecision(
                intent=IntentKind.conversational, response_type=ResponseType.answer,
                user_visible_response="ok", extracted_entities=[], required_capabilities=[],
                needs_mission=False, risk_level=RiskLevel.none, confidence=0.8),
            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


PHONE = "+15550100"


def _message(text: str, pmid: str):
    import datetime
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=[],
                          timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)


def _drive(uid, texts: list[str]) -> list[dict]:
    """One turn per text through the production handler. Traces are read back from the ring buffer."""
    runtime = conversation_runtime._Runtime(reasoner=_FakeReasoner())
    channel = FakeChannel()

    async def _go():
        for i, text in enumerate(texts):
            # Deliberately NOT wrapped in try/except. A harness that swallows exceptions reports an
            # empty baseline as a fast one — which is exactly what the first run of this file did, and
            # the failure was an ImportError in the harness itself.
            await runtime.handle(channel, _message(text, f"pm-{uuid4().hex[:8]}-{i}"),
                                 user_id=uid, reply_target=PHONE)
    _run(_go())
    return tt.recent()


def _summarize(traces: list[dict], label: str) -> dict:
    totals = [t["total_ms"] for t in traces if t["total_ms"] is not None]
    stats = tt.percentiles(totals)
    stage_totals: dict[str, float] = {}
    for t in traces:
        marks = t["marks_ms"]
        seen = [(s, marks[s]) for s in tt.STAGES if s in marks]
        for a, b in zip(seen, seen[1:]):
            stage_totals[b[0]] = stage_totals.get(b[0], 0.0) + (b[1] - a[1])
    grand = sum(stage_totals.values()) or 1.0
    slowest = max(stage_totals.items(), key=lambda kv: kv[1]) if stage_totals else ("n/a", 0.0)
    errors = sum(1 for t in traces if t["error_class"])
    return {
        "label": label, **stats, "errors": errors,
        "error_rate": (errors / len(traces)) if traces else 0.0,
        "slowest_stage": slowest[0],
        "stage_pct": {k: 100.0 * v / grand for k, v in
                      sorted(stage_totals.items(), key=lambda kv: -kv[1])[:6]},
    }


def _report(rows: list[dict]) -> str:
    out = []
    for r in rows:
        if not r.get("n"):
            out.append(f"  {r['label']:<34} NO SAMPLES")
            continue
        pct = ", ".join(f"{k} {v:.0f}%" for k, v in r["stage_pct"].items())
        out.append(
            f"  {r['label']:<34} n={r['n']:<4} p50={r.get('p50', 0):7.1f}  p95={r.get('p95', 0):7.1f}  "
            f"p99={r.get('p99', 0):7.1f}  max={r.get('max', 0):7.1f}  err={r['error_rate']:.2f}\n"
            f"  {'':<34} slowest={r['slowest_stage']}  [{pct}]")
    return "\n".join(out)


# --- the baseline ----------------------------------------------------------------------------------------

def test_production_inbound_baseline(capsys):
    """Cold and warm, reported separately, through the real handler."""
    uid = uuid4()
    _run(_seed(uid))

    chat = ["hey", "yo whats up", "thanks", "lol ok", "how's it going"]
    traces = _drive(uid, [chat[i % len(chat)] for i in range(WARM_TURNS + 1)])

    cold = [t for t in traces if t["cold"]]
    warm = [t for t in traces if not t["cold"]]
    rows = [_summarize(cold, "deterministic conversation COLD"),
            _summarize(warm, "deterministic conversation WARM")]

    by_path: dict[str, list[dict]] = {}
    for t in warm:
        by_path.setdefault(t["execution_path"] or "unrouted", []).append(t)
    rows += [_summarize(v, f"  path: {k}") for k, v in sorted(by_path.items())]

    with capsys.disabled():
        print(f"""
PRODUCTION INBOUND LATENCY BASELINE  (real Postgres, real router/context/memory, FAKE reasoner)
  The model call is stubbed, so these are NOT semantic response times — they are everything Bruce does
  around the model, which is exactly what #126 and #127 change. Real-provider numbers land in #129.

{_report(rows)}

  tracing failures: {tt.TRACING_FAILURES}
""")

    assert warm, "no warm turns were traced — the handler did not run"
    assert all(t["total_ms"] is not None for t in traces), "a turn finished without a total"
    assert rows[1]["n"] >= WARM_TURNS - 2


def test_every_traced_turn_is_internally_consistent():
    """The baseline is only worth reading if no trace in it contradicts itself."""
    uid = uuid4()
    _run(_seed(uid))
    _drive(uid, ["hey", "thanks", "yo"])
    for payload in tt.recent():
        marks = payload["marks_ms"]
        observed = [marks[s] for s in tt.STAGES if s in marks]
        assert observed == sorted(observed), f"{payload['trace_id']} is out of order"
        assert payload["total_ms"] is not None
        tt.assert_content_free(payload)


def test_the_baseline_carries_no_message_content():
    uid = uuid4()
    _run(_seed(uid))
    _drive(uid, ["remind me about ms delgado's lab on friday"])
    flat = str(tt.recent())
    for leak in ("delgado", "friday", "remind"):
        assert leak.lower() not in flat.lower(), f"{leak!r} reached a latency record"


def test_a_turn_that_raises_still_produces_a_finished_trace():
    """The turn that fails is the turn whose timing matters most."""
    uid = uuid4()
    _run(_seed(uid))
    runtime = conversation_runtime._Runtime(reasoner=_FakeReasoner(raises=True))
    channel = FakeChannel()

    async def _go():
        await runtime.handle(channel, _message("hey", "pm-err"), user_id=uid, reply_target=PHONE)
    _run(_go())
    traces = tt.recent()
    assert traces, "a failing turn produced no trace at all"
    assert all(t["total_ms"] is not None for t in traces)


def test_tracing_does_not_change_what_the_student_receives():
    """#125 measures; it does not optimize, reroute or reword. The proof is that the outbound text is
    identical with tracing on and off."""
    uid_a, uid_b = uuid4(), uuid4()
    _run(_seed(uid_a))
    _run(_seed(uid_b))

    runtime = conversation_runtime._Runtime(reasoner=_FakeReasoner())
    on, off = FakeChannel(), FakeChannel()

    async def _one(channel, uid, pmid):
        await runtime.handle(channel, _message("hey", pmid), user_id=uid, reply_target=PHONE)

    _run(_one(on, uid_a, "pm-on"))
    import os
    os.environ["BRUCE_TURN_TRACE_OFF"] = "1"
    try:
        _run(_one(off, uid_b, "pm-off"))
    finally:
        os.environ.pop("BRUCE_TURN_TRACE_OFF", None)

    assert [m.text for m in on.sent] == [m.text for m in off.sent], "tracing changed the reply"
