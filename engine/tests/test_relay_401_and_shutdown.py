"""The two defects that turned a 6.5-minute network blip into 11.9 hours of silent relay downtime.

THE INCIDENT, because these tests only make sense against it. On 2026-07-28 the relay claimed normally
until 12:22:55Z, lost connectivity for 6.5 minutes across an IPv6->IPv4 transition, and its first request
on the far side was rejected 401 "stale request (replay)" — the timestamp check, not the credential. The
device was never revoked and never rotated. Two bugs compounded:

  1. the client treated EVERY 401 as revocation and decided to park;
  2. deciding to stop did not actually stop it — `run_inbound` was parked inside `imsg.watch()`, which
     only re-reads the stop flag when an event arrives, so `run()`'s gather never returned and the
     supervisor had nothing to restart.

Together: a healthy relay, holding a valid credential, resident but deaf, for half a day. Each test
below fails on the pre-fix code for a DIFFERENT reason, so neither fix can silently regress.
"""

from __future__ import annotations

import asyncio

import pytest

from relay import relay as relay_mod
from relay.backend import AuthError, BackendError, _retryable_401


# --------------------------------------------------------------------------------------------------
# FIX 1 — a 401 about the REQUEST is retried; a 401 about the CREDENTIAL still parks.
# --------------------------------------------------------------------------------------------------

class _Resp:
    """Minimal stand-in for the httpx response `_post` inspects."""

    def __init__(self, payload, *, raises: bool = False):
        self._payload, self._raises = payload, raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


@pytest.mark.parametrize("detail, expected, why", [
    ({"error": "relay_auth_failed", "reason": "stale request (replay)", "retryable": True},
     True, "the exact shape the server now returns for a clock blip"),
    ({"error": "relay_auth_failed", "reason": "bad timestamp", "retryable": True},
     True, "malformed timestamp is also a property of the request"),
    ({"error": "relay_auth_failed", "reason": "credential revoked", "retryable": False},
     False, "a real revocation must still park"),
    ({"error": "relay_auth_failed", "reason": "invalid credential", "retryable": False},
     False, "a wrong secret must still park"),
    ({"error": "relay_auth_failed", "reason": "stale request (replay)"},
     True, "OLDER SERVER, no retryable field -> fall back to the reason string"),
    ({"error": "relay_auth_failed", "reason": "credential revoked"},
     False, "older server, revocation still parks"),
    ({"error": "relay_auth_failed", "reason": "something nobody has written yet"},
     False, "UNKNOWN reason defaults to parking — never to polling forever"),
    ("missing relay credential", False, "a plain-string detail is not retryable"),
    (None, False, "no detail at all is not retryable"),
])
def test_401_is_classified_by_cause_not_by_status(detail, expected, why):
    assert _retryable_401(_Resp({"detail": detail})) is expected, why


def test_unparseable_401_body_parks_rather_than_retrying():
    """Fail-safe direction: if we cannot read the body we assume the credential is dead."""
    assert _retryable_401(_Resp(None, raises=True)) is False


def test_retryable_flag_beats_the_reason_string():
    """The server is authoritative. A reason that LOOKS transient does not override an explicit False —
    otherwise a future rename of a permanent reason could silently become retryable."""
    assert _retryable_401(_Resp({"detail": {
        "reason": "stale request (replay)", "retryable": False}})) is False


@pytest.mark.asyncio
async def test_transient_401_does_not_park_the_relay_and_permanent_one_does():
    """End to end through the real outbound path: which exception a 401 becomes decides the outcome.

    A transient rejection must leave the relay RUNNING with no exit code, because the durable outbound row
    is still claimable and the credential still works. A revocation must set REVOKED_CREDENTIAL_EXIT and
    stop, so the supervisor parks instead of hot-looping against a dead credential.
    """
    for exc, should_stop in ((BackendError("retryable"), False), (AuthError("revoked"), True)):
        r = relay_mod.Relay.__new__(relay_mod.Relay)
        r._stop = asyncio.Event()
        r.exit_code = 0
        r._backoff_s = None
        r.poll_interval = 0.01

        class _B:
            async def claim(self_):
                raise exc

        r.backend = _B()
        # The REAL outbound step — not a reimplementation of it. Anything else would only be testing
        # the test.
        handled = await r.process_one_outbound()

        assert handled is False, "a rejected claim never counts as work done"
        assert r._stop.is_set() is should_stop, f"{exc!r} should_stop={should_stop}"
        assert (r.exit_code == relay_mod.REVOKED_CREDENTIAL_EXIT) is should_stop


@pytest.mark.asyncio
async def test_a_stale_timestamp_401_leaves_the_relay_claiming():
    """The incident, replayed against the real client stack: the server answers one claim with a
    stale-timestamp 401 and every later claim normally. The relay must ride through it.

    Pre-fix, the single 401 became AuthError and the relay parked for good. Post-fix it is a BackendError,
    the loop backs off, and the very next claim succeeds — which is exactly what should have happened at
    12:29Z on 2026-07-28.
    """
    calls = {"n": 0}

    class _FlakyBackend:
        async def claim(self_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BackendError("relay auth rejected this request (retryable)")
            return None                      # 204: nothing to send, credential clearly fine

    r = relay_mod.Relay.__new__(relay_mod.Relay)
    r._stop = asyncio.Event()
    r.exit_code = 0
    r._backoff_s = None
    r.poll_interval = 0.01
    r.backend = _FlakyBackend()

    assert await r.process_one_outbound() is False        # the 401
    assert not r._stop.is_set(), "a stale-timestamp 401 must NOT park the relay"
    assert r.exit_code == 0, "and must not set the revoked exit code"

    assert await r.process_one_outbound() is False        # recovered, 204
    assert calls["n"] == 2, "the relay must keep claiming after a transient rejection"
    assert not r._stop.is_set()


# --------------------------------------------------------------------------------------------------
# FIX 1, SERVER SIDE — the API must actually SAY which kind of 401 it is.
# The timestamp check runs before any credential lookup, so these need no database.
# --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_stale_timestamp_is_reported_as_transient_and_never_reaches_the_database():
    """It must raise the transient subclass — and do so WITHOUT a DB session, which is what makes this
    runnable here and also what makes the check cheap enough to sit in front of every request."""
    import datetime

    from bruce_engine import relay_auth

    now = datetime.datetime(2026, 7, 28, 12, 29, 31, tzinfo=datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=6, seconds=34)).isoformat()   # the real gap

    with pytest.raises(relay_auth.RelayAuthTransientError) as ei:
        await relay_auth.authenticate("any-secret", timestamp=stale, now=now)
    assert "stale" in str(ei.value)
    # Still a RelayAuthError, so every existing handler keeps refusing the request.
    assert isinstance(ei.value, relay_auth.RelayAuthError)


@pytest.mark.asyncio
async def test_a_malformed_timestamp_is_transient_too():
    from bruce_engine import relay_auth

    with pytest.raises(relay_auth.RelayAuthTransientError):
        await relay_auth.authenticate("any-secret", timestamp="not-a-timestamp")


@pytest.mark.asyncio
async def test_a_timestamp_inside_the_window_passes_the_replay_check():
    """Anti-vacuous: the two tests above must fail for STALENESS, not because every timestamp is
    rejected. A fresh one gets past the replay check and on to the credential lookup."""
    import datetime

    from bruce_engine import relay_auth

    now = datetime.datetime(2026, 7, 28, 12, 29, 31, tzinfo=datetime.timezone.utc)
    fresh = (now - datetime.timedelta(seconds=30)).isoformat()

    # A fresh timestamp must reach the credential lookup. Proven by REPLACING that lookup: if the replay
    # check still rejected the request, the sentinel below would never be raised. `pytest.raises(Exception)`
    # would have accepted a DB connection error as "proof" and silently degraded to a vacuous test.
    class _Reached(Exception):
        pass

    def _boom(*a, **k):          # sync: raises at CALL time, before `async with` needs __aenter__
        raise _Reached()

    real = relay_auth.worker_session
    relay_auth.worker_session = _boom
    try:
        with pytest.raises(_Reached):
            await relay_auth.authenticate("definitely-not-a-real-secret", timestamp=fresh, now=now)
    finally:
        relay_auth.worker_session = real


@pytest.mark.asyncio
async def test_the_api_marks_a_stale_timestamp_401_retryable_on_the_wire():
    """The actual wire contract, by CALLING the dependency — not by reading its source.

    An earlier version of this test asserted on `inspect.getsource`, which passes against a server whose
    behaviour is broken as long as the string is present. This drives `current_relay_device` and reads
    the HTTPException it raises, so the response body itself is what is under test.
    """
    import datetime

    from fastapi import HTTPException

    from bruce_engine import api

    stale = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=7)).isoformat()
    with pytest.raises(HTTPException) as ei:
        await api.current_relay_device(authorization="Bearer whatever", x_bruce_timestamp=stale)

    assert ei.value.status_code == 401
    assert ei.value.detail["retryable"] is True, "a clock blip must be advertised as retryable"
    assert ei.value.detail["reason"] == "stale request (replay)"
    # And the client must agree with the server about what that body means.
    assert _retryable_401(_Resp({"detail": ei.value.detail})) is True


@pytest.mark.asyncio
async def test_the_api_marks_a_missing_credential_401_not_retryable():
    """Anti-vacuous partner: not every 401 comes back retryable, so the flag carries information."""
    from fastapi import HTTPException

    from bruce_engine import api

    with pytest.raises(HTTPException) as ei:
        await api.current_relay_device(authorization=None, x_bruce_timestamp=None)
    assert ei.value.status_code == 401
    assert _retryable_401(_Resp({"detail": ei.value.detail})) is False


# --------------------------------------------------------------------------------------------------
# FIX 1, THE ACTUAL SEAM — HttpBackend._post turning a 401 RESPONSE into the right exception.
# Everything above tests the two halves; this tests the join, which is where the outage happened.
# --------------------------------------------------------------------------------------------------

class _FakeHttpx:
    """Stands in for the lazily-imported httpx module inside _post."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = 0

    def AsyncClient(self, **_kw):
        outer = self

        class _C:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def post(self_, url, headers=None, json=None):
                outer.sent += 1
                return outer._responses.pop(0)

        return _C()


class _HttpResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _backend_with(monkeypatch, responses):
    import sys
    import types

    from relay.backend import HttpBackend

    fake = _FakeHttpx(responses)
    mod = types.ModuleType("httpx")
    mod.AsyncClient = fake.AsyncClient
    monkeypatch.setitem(sys.modules, "httpx", mod)
    return HttpBackend("https://example.invalid", "secret"), fake


_TRANSIENT_BODY = {"detail": {"error": "relay_auth_failed",
                              "reason": "stale request (replay)", "retryable": True}}
_REVOKED_BODY = {"detail": {"error": "relay_auth_failed",
                            "reason": "credential revoked", "retryable": False}}


@pytest.mark.asyncio
async def test_post_turns_a_transient_401_into_a_retryable_backend_error(monkeypatch):
    """THE REGRESSION TEST FOR THE OUTAGE, at the seam that actually failed."""
    b, _ = _backend_with(monkeypatch, [_HttpResp(401, _TRANSIENT_BODY)])
    with pytest.raises(BackendError):
        await b._post("/v1/relay/outbound/claim", {})


@pytest.mark.asyncio
async def test_post_turns_a_revoked_401_into_an_auth_error(monkeypatch):
    b, _ = _backend_with(monkeypatch, [_HttpResp(401, _REVOKED_BODY)])
    with pytest.raises(AuthError):
        await b._post("/v1/relay/outbound/claim", {})


@pytest.mark.asyncio
async def test_a_401_with_no_readable_body_parks(monkeypatch):
    b, _ = _backend_with(monkeypatch, [_HttpResp(401, None)])
    with pytest.raises(AuthError):
        await b._post("/v1/relay/outbound/claim", {})


@pytest.mark.asyncio
async def test_endless_transient_401s_eventually_park_instead_of_retrying_forever(monkeypatch):
    """A permanently skewed clock — or a REVOKED device whose clock is skewed, which the server reports
    as 'stale' because it checks the timestamp first — must not poll silently forever."""
    from relay.backend import MAX_CONSECUTIVE_TRANSIENT_401

    n = MAX_CONSECUTIVE_TRANSIENT_401
    b, _ = _backend_with(monkeypatch, [_HttpResp(401, _TRANSIENT_BODY) for _ in range(n + 1)])

    for i in range(n):
        with pytest.raises(BackendError):
            await b._post("/v1/relay/outbound/claim", {})
    with pytest.raises(AuthError):      # the (n+1)th gives up and parks
        await b._post("/v1/relay/outbound/claim", {})


@pytest.mark.asyncio
async def test_one_good_response_resets_the_transient_budget(monkeypatch):
    """A blip every few minutes is normal operation, not an escalating fault — the counter must be
    CONSECUTIVE, or a long-lived relay would eventually park for no reason."""
    from relay.backend import MAX_CONSECUTIVE_TRANSIENT_401

    n = MAX_CONSECUTIVE_TRANSIENT_401
    responses = ([_HttpResp(401, _TRANSIENT_BODY) for _ in range(n - 1)]
                 + [_HttpResp(204)]
                 + [_HttpResp(401, _TRANSIENT_BODY) for _ in range(n)])
    b, _ = _backend_with(monkeypatch, responses)

    for _ in range(n - 1):
        with pytest.raises(BackendError):
            await b._post("/x", {})
    await b._post("/x", {})                      # clean answer -> budget resets
    for _ in range(n):                           # a full fresh budget, none of which may park
        with pytest.raises(BackendError):
            await b._post("/x", {})


# --------------------------------------------------------------------------------------------------
# FIX 2 — shutdown does not wait for the next iMessage.
# --------------------------------------------------------------------------------------------------

class _IdleWatch:
    """A watch stream that behaves exactly like a quiet inbox: it never yields and never ends.

    This is the condition the old code could not survive. `aclose()` records that the stream was closed
    so the reconnect path cannot leak subprocesses.
    """

    def __init__(self):
        self.closed = False
        self.started = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started = True
        await asyncio.Event().wait()          # never resolves — no message ever arrives
        raise AssertionError("unreachable")

    async def aclose(self):
        self.closed = True


class _Imsg:
    def __init__(self, stream):
        self._stream = stream

    def watch(self):
        return self._stream


def _relay_with(stream) -> relay_mod.Relay:
    r = relay_mod.Relay.__new__(relay_mod.Relay)
    r._stop = asyncio.Event()
    r.exit_code = 0
    r.imsg = _Imsg(stream)
    r.reconnect_delay = 0.01
    return r


@pytest.mark.asyncio
async def test_run_inbound_exits_promptly_when_stopped_with_an_idle_stream():
    """THE REGRESSION TEST FOR THE OUTAGE. Pre-fix this hangs until the timeout and fails."""
    stream = _IdleWatch()
    r = _relay_with(stream)
    task = asyncio.ensure_future(r.run_inbound())
    await asyncio.sleep(0.05)
    assert stream.started, "the stream must actually be awaited, or this proves nothing"
    assert not task.done(), "still running while no stop has been requested — correct"

    r.stop()
    await asyncio.wait_for(task, timeout=2.0)   # pre-fix: never returns -> TimeoutError
    assert stream.closed, "the abandoned watch stream must be closed, not leaked"


@pytest.mark.asyncio
async def test_a_quiet_inbox_alone_does_not_terminate_the_loop():
    """Anti-vacuous companion: the test above must not pass merely because run_inbound returns early.
    With no stop requested, an idle stream keeps the loop alive indefinitely."""
    r = _relay_with(_IdleWatch())
    task = asyncio.ensure_future(r.run_inbound())
    await asyncio.sleep(0.2)
    assert not task.done(), "run_inbound exited without being told to stop"
    r.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_events_still_flow_before_a_stop_is_requested():
    """The shutdown race must not cost us delivery: every event yielded before the stop is processed."""
    delivered: list[object] = []

    class _ThreeThenIdle(_IdleWatch):
        def __init__(self):
            super().__init__()
            self._left = ["a", "b", "c"]

        async def __anext__(self):
            self.started = True
            if self._left:
                return self._left.pop(0)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    stream = _ThreeThenIdle()
    r = _relay_with(stream)

    async def _process(ev):
        delivered.append(ev)

    r.process_inbound = _process
    task = asyncio.ensure_future(r.run_inbound())
    await asyncio.sleep(0.05)
    assert delivered == ["a", "b", "c"], f"events lost to the stop race: {delivered}"
    r.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_revoked_credential_on_the_inbound_path_parks_rather_than_restart_looping():
    """The park invariant, which the rewrite of run_inbound silently dropped once already.

    Exit code 78 is what tells the supervisor to PARK. Without it the process exits 0, which
    `_PARK_EXITS` does not match, so the supervisor RESTARTS — straight into another rejected credential,
    forever. A hot restart loop against a dead credential is worse than the hang it replaced.
    """
    class _AuthFailingWatch(_IdleWatch):
        async def __anext__(self):
            self.started = True
            raise AuthError("revoked")

    r = _relay_with(_AuthFailingWatch())
    await asyncio.wait_for(r.run_inbound(), timeout=2.0)

    assert r._stop.is_set(), "a revoked credential must stop the relay"
    assert r.exit_code == relay_mod.REVOKED_CREDENTIAL_EXIT, (
        "exit code must be 78 so the supervisor parks; 0 would make it restart-loop")


@pytest.mark.asyncio
async def test_a_transient_backend_error_on_inbound_does_not_park(monkeypatch):
    """The counterpart: a retryable failure must reconnect, never set the park exit code."""
    class _FlakyWatch(_IdleWatch):
        async def __anext__(self):
            self.started = True
            raise BackendError("transient")

    r = _relay_with(_FlakyWatch())
    task = asyncio.ensure_future(r.run_inbound())
    await asyncio.sleep(0.06)
    assert r.exit_code == 0, "a transient failure must never set the revoked park code"
    r.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_stream_that_ends_reconnects_rather_than_exiting():
    """`imsg` restarting ends the stream. That is a reconnect, not a shutdown — the old behaviour, kept."""
    opened = []

    class _EndsImmediately:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    class _ReopeningImsg:
        def watch(self):
            s = _EndsImmediately()
            opened.append(s)
            return s

    r = relay_mod.Relay.__new__(relay_mod.Relay)
    r._stop = asyncio.Event()
    r.exit_code = 0
    r.imsg = _ReopeningImsg()
    r.reconnect_delay = 0.01

    task = asyncio.ensure_future(r.run_inbound())
    await asyncio.sleep(0.08)
    r.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(opened) > 1, "a stream that ends should be reopened, not treated as a shutdown"
    assert all(s.closed for s in opened), "every reopened stream must be closed"
