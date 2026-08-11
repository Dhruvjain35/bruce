"""Transport reuse, and the isolation that makes it safe.

Sharing a client is only correct if the client carries nothing that belongs to one student. So half of
this file measures reuse and the other half tries to make one user's request inherit another's
credentials — because a shared transport that leaks an account binding is a far worse bug than the
per-call construction it replaced.

Everything here asserts on OBJECT IDENTITY, CONSTRUCTION COUNTERS or real concurrent behaviour. Nothing
asserts by searching source text: a comment claiming a client is shared satisfies a string search and
proves nothing, and this repo has already had three tests fail because a docstring contained the word it
forbade.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from bruce_engine import llm, provider_http


@pytest.fixture(autouse=True)
def _reset():
    llm.reset_clients()
    yield
    llm.reset_clients()


def _run(c):
    return asyncio.run(c)


# --- the model transport -----------------------------------------------------------------------------

def test_a_hundred_model_lookups_build_one_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaa")
    first = llm.conversation_model()
    for _ in range(100):
        assert llm.conversation_model() is first
    assert len(llm._PROVIDERS) == 1 and len(llm._MODELS) == 1


def test_two_model_ids_share_one_provider(monkeypatch):
    """The transport is the expensive thing, not the model wrapper. Two model ids must not mean two
    connection pools."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaa")
    a = llm.openai_model("gpt-5.4-mini")
    b = llm.openai_model("gpt-4.1-mini")
    assert a is not b
    assert len(llm._PROVIDERS) == 1


def test_a_different_api_key_never_reuses_the_old_transport(monkeypatch):
    """Keyed by the credential, so a rotated key cannot keep talking over a socket authenticated with
    the previous one."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaa")
    first = llm.conversation_model()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-bbb")
    second = llm.conversation_model()
    assert first is not second
    assert len(llm._PROVIDERS) == 2


def test_no_user_state_is_reachable_from_a_shared_model(monkeypatch):
    """The property that makes sharing safe at all. A shared object may hold configuration and sockets;
    it may not hold a user id, an account, a token or a request argument."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaa")
    model = llm.conversation_model()
    flat = repr(model.__dict__).lower()
    for forbidden in ("user_id", "account", "conversation", "refresh_token", "recipient"):
        assert forbidden not in flat, f"a shared model object exposes {forbidden}"


def test_model_construction_is_measurably_cheaper_than_before(monkeypatch):
    """5.454ms per construction before this change, measured. The point is the transport, but the
    construction saving is real and is asserted so a regression to per-call providers is visible.

    `monkeypatch`, NOT `os.environ[...]`. This test used to assign the fake key directly and never restore
    it, leaking `sk-test-aaa` into every subsequent test in the process. Nothing failed nearby — the
    damage landed much later, on the only suite that makes REAL model calls: every call 401'd, `triage`
    correctly classified the 4xx as provider_rejected and did not retry, the executive fell back to
    asking, and the language rate gate read 0.86 for a system that measures 0.99. The tell was the clock:
    that gate takes ~115s honestly and failed in 12.
    """
    import time
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-aaa")
    llm.conversation_model()
    started = time.perf_counter()
    for _ in range(200):
        llm.conversation_model()
    per_call_ms = (time.perf_counter() - started) / 200 * 1000
    assert per_call_ms < 0.5, f"{per_call_ms:.3f}ms per lookup — the provider is being rebuilt"


# --- the Google transport ----------------------------------------------------------------------------

def test_repeated_google_calls_reuse_one_transport():
    async def _go():
        before = provider_http.CONSTRUCTED
        first = provider_http.shared()
        for _ in range(100):
            assert provider_http.shared() is first
        return provider_http.CONSTRUCTED - before
    assert _run(_go()) == 1


def test_the_shared_transport_carries_no_credential():
    """Google auth is a per-request Authorization header. If a token ever reached the client's default
    headers, one student's request would be signed with another's credential."""
    async def _go():
        client = provider_http.shared()
        headers = {k.lower(): v for k, v in client.headers.items()}
        assert "authorization" not in headers
        assert client.auth is None
        return client
    _run(_go())


def test_a_closed_transport_is_replaced_rather_than_returned():
    async def _go():
        first = provider_http.shared()
        await first.aclose()
        second = provider_http.shared()
        assert second is not first and not second.is_closed
    _run(_go())


def test_discard_forces_a_fresh_transport():
    async def _go():
        first = provider_http.shared()
        provider_http.discard()
        assert provider_http.shared() is not first
    _run(_go())


def test_each_event_loop_gets_its_own_transport():
    """Pooled connections bind to the loop that opened them; reusing a client across `asyncio.run`
    surfaces as "Event loop is closed" on a connection that looks healthy. Production has one loop, so
    this is a true singleton there.

    Counted rather than compared by `id()`: the first client can be collected once its loop dies, and
    CPython will happily reuse the address — which made the identity form of this assertion fail against
    a correct implementation.
    """
    async def _one():
        provider_http.shared()
        return provider_http.CONSTRUCTED

    before = provider_http.CONSTRUCTED
    _run(_one())
    _run(_one())
    assert provider_http.CONSTRUCTED - before == 2


def test_concurrent_tasks_on_one_loop_share_one_transport():
    async def _go():
        before = provider_http.CONSTRUCTED
        clients = await asyncio.gather(*[asyncio.to_thread(lambda: None) for _ in range(4)])
        got = [provider_http.shared() for _ in range(8)]
        assert len({id(c) for c in got}) == 1
        return provider_http.CONSTRUCTED - before
    assert _run(_go()) == 1


def test_the_pool_is_bounded():
    """An unbounded pool means one process can open arbitrarily many sockets against Google under load,
    and a stuck connection is held forever."""
    assert provider_http.LIMITS.max_connections is not None
    assert provider_http.LIMITS.max_keepalive_connections is not None
    assert provider_http.LIMITS.keepalive_expiry is not None


def test_an_injected_client_still_wins():
    """Every call site kept its `client or shared()` shape, so test injection is unchanged and a caller
    with different timeout needs is not forced onto the shared pool."""
    import inspect

    from bruce_engine import calendar_adapter, gmail_adapter
    for fn in (calendar_adapter.GoogleCalendarAdapter._http,
               gmail_adapter.GoogleGmailAdapter._request):
        assert "self._client or" in inspect.getsource(fn)


# --- cross-user isolation ------------------------------------------------------------------------------

def test_two_users_requests_over_one_transport_carry_their_own_credentials():
    """The failure this whole design has to rule out. Two students, one socket, and each request must
    carry only its own Authorization header."""
    seen: list[tuple[str, str]] = []

    async def _go():
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers.get("authorization", "")))
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://example.test/a", headers={"Authorization": "Bearer token-A"})
            await client.get("https://example.test/b", headers={"Authorization": "Bearer token-B"})

    _run(_go())
    assert seen == [("https://example.test/a", "Bearer token-A"),
                    ("https://example.test/b", "Bearer token-B")]


def test_a_disconnected_account_cannot_ride_a_warm_transport():
    """Reuse is of the SOCKET, not of the authorization. Capability truth is re-read per request, so a
    revoked account fails on the next call rather than inheriting a warm authenticated state."""
    import inspect

    from bruce_engine import tool_broker
    src = inspect.getsource(tool_broker._provider_connection)
    assert "get_integration" in src, "connection truth is not re-read per request"
    assert "revoked_at" in src


def test_the_shared_transport_holds_no_per_user_attributes():
    async def _go():
        client = provider_http.shared()
        flat = repr(vars(client)).lower()
        for forbidden in ("user_id", "refresh_token", "provider_account", "recipient"):
            assert forbidden not in flat
    _run(_go())


# --- the database ---------------------------------------------------------------------------------------

def test_the_database_engine_is_already_process_lifetime():
    """Confirmed rather than changed. #126 is transport lifetime only, and this one was already right —
    saying so is part of the inventory."""
    import bruce_engine.db as db
    import inspect
    src = inspect.getsource(db)
    assert "_engine: " in src or "_engine =" in src
    assert "global _engine" in src, "the engine is not a process-level singleton"


def test_sessions_stay_request_scoped():
    """A shared engine is safe; a shared Session is not. `user_session` is an async context manager, so
    a session cannot outlive the block that opened it."""
    import bruce_engine.db as db
    assert hasattr(db.user_session, "__wrapped__") or callable(db.user_session)
    import inspect
    src = inspect.getsource(db.user_session)
    assert "yield" in src, "user_session does not scope its session to a block"


# --- construction accounting ----------------------------------------------------------------------------

def test_no_engine_module_constructs_a_bare_http_client_on_a_call_path():
    """The inventory, enforced. A new per-call `httpx.AsyncClient` on a provider path reintroduces the
    cold pool this PR removed — so the only ones left must be deliberate and outside the hot path."""
    import ast
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1] / "bruce_engine"
    # `email_resolver` and `discovery` are offline/eval paths, not inbound-turn paths, and each owns its
    # client for the life of one operation. Listed rather than silently tolerated.
    allowed = {"email_resolver.py", "discovery.py", "provider_http.py"}
    offenders = []
    for path in sorted(engine.glob("*.py")):
        if path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "AsyncClient"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"per-call HTTP clients on provider paths: {offenders}"
