"""One HTTP transport per event loop, shared by every Google call.

WHAT IS SHARED AND WHAT IS NOT. The client here holds a connection pool, TLS sessions and timeout
configuration. It holds no token, no account id, no user id and no request argument — Google auth is a
per-request `Authorization` header, set by the caller at the moment of the call, so two students'
requests can travel over the same socket without either one being able to reach the other's credential.
That is the whole reason this is safe to share, and it is the property the isolation tests assert rather
than assume.

MEASURED. Constructing `httpx.AsyncClient(timeout=30)` costs 5.71ms, which is the cheap half. The
expensive half is that a fresh client has an empty pool, so every first request pays DNS, TCP and a TLS
handshake — the same cost that made `semantic_triage`'s first model call take 3003ms in Cloud Run before
its provider was made process-lifetime.

KEYED BY EVENT LOOP, and this is not defensive decoration. An `httpx.AsyncClient` binds its pooled
connections to the loop that opened them; reusing one across `asyncio.run()` boundaries surfaces as
"Event loop is closed" on a connection that looks perfectly healthy. Production runs one loop for the
life of the process, so this is a true singleton there. A test suite that calls `asyncio.run` per
assertion gets one client per loop, which is correct rather than merely convenient.
"""

from __future__ import annotations

import asyncio
import logging
import weakref

import httpx

log = logging.getLogger("bruce.provider_http")   # CONTENT-FREE: counts and lifecycle only

DEFAULT_TIMEOUT = 30

# Bounded so one process cannot open an unbounded number of sockets against Google under load, and so a
# stuck connection is eventually recycled rather than held forever.
LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=90.0)

# How many clients this process has actually constructed. The reuse tests assert on this rather than on
# source text: a counter cannot be satisfied by a comment that says the client is shared.
CONSTRUCTED = 0

_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary())


def shared(timeout: int = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """The transport for this event loop, built once.

    A caller that needs different timeout semantics passes its own client, exactly as before — every
    call site kept its `client or shared()` shape, so injection for tests is unchanged.
    """
    global CONSTRUCTED
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop yet — the caller is constructing ahead of use. Hand back a private client rather than
        # caching one under a key that does not exist; caching it globally would be a client with no
        # owner and no way to know when its loop dies.
        CONSTRUCTED += 1
        return httpx.AsyncClient(timeout=timeout, limits=LIMITS)

    client = _CLIENTS.get(loop)
    if client is not None and not client.is_closed:
        return client
    client = httpx.AsyncClient(timeout=timeout, limits=LIMITS)
    _CLIENTS[loop] = client
    CONSTRUCTED += 1
    log.info("provider_http_client_constructed total=%d", CONSTRUCTED)
    return client


def discard() -> None:
    """Drop this loop's client so the next call builds a fresh one.

    For a transport that has genuinely broken — a socket the pool believes is fine but is not. It is not
    an error path anything takes automatically: httpx already retries a dead keep-alive connection
    internally, and discarding on every failure would turn one bad response into a permanent cold start.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _CLIENTS.pop(loop, None)


async def aclose() -> None:
    """Close this loop's client. Called at process shutdown; never on the hot path."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _CLIENTS.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()
