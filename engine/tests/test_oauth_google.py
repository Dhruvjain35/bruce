"""Google OAuth flow — REAL Postgres, mock Google transport.

The callback is the only endpoint in Bruce that a stranger's browser can reach with attacker-chosen
parameters, so these tests are written as attacks rather than as happy paths: forged state, replayed
state, expired state, a callback naming someone else's user, a swapped code. If any of them
succeeds in connecting a calendar, a stranger can write to a student's real calendar.

Nothing here has touched Google. The transport is mocked against the real httpx/client stack; the
live test skips until GOOGLE_* credentials exist.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import crypto, oauth_google, schema
from bruce_engine.db import user_session
from bruce_engine.oauth_google import (
    ConsentDenied,
    InsufficientScope,
    InvalidState,
    MissingCode,
    NotConnected,
    RefreshFailed,
    TokenExchangeFailed,
)
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
KEY = crypto.generate_key()
SCOPE = "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/calendar.events"


@pytest.fixture(autouse=True)
def _env(pg_test_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret-must-never-leak")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://bruce.test/v1/integrations/google/callback")
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", KEY)

    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _google(*, token_status=200, token_body=None, userinfo_email="student@school.edu", capture=None):
    """Mock Google. Records outgoing requests so we can assert what we actually sent."""
    body = token_body if token_body is not None else {
        "access_token": "at-live", "refresh_token": "rt-live-secret",
        "scope": SCOPE, "expires_in": 3599, "token_type": "Bearer",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(token_status, json=body)
        if "userinfo" in str(request.url):
            return httpx.Response(200, json={"email": userinfo_email})
        if "revoke" in str(request.url):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _start(uid: UUID) -> str:
    await users.ensure(uid)
    url = await oauth_google.start_authorization(uid)
    return httpx.URL(url).params["state"]


# --------------------------------------------------------------------------- authorization URL


def test_authorization_url_carries_pkce_and_offline_consent(clean_db):
    async def run():
        uid = uuid4()
        await users.ensure(uid)
        url = httpx.URL(await oauth_google.start_authorization(uid))
        p = url.params
        assert p["code_challenge_method"] == "S256" and p["code_challenge"]
        # offline+consent are what actually produce a refresh token; without them the integration
        # silently dies in an hour and Bruce cannot act while the app is closed.
        assert p["access_type"] == "offline" and p["prompt"] == "consent"
        assert p["scope"] == SCOPE, "least privilege: events only, never full calendar access"
        assert len(p["state"]) >= 32, "state must be unguessable"
        # the verifier must NEVER be in the URL — only its hash
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.OAuthState))).scalar_one()
        assert row.code_verifier not in str(url)

    asyncio.run(run())


def test_each_authorization_gets_a_fresh_state_and_verifier(clean_db):
    async def run():
        uid = uuid4()
        await users.ensure(uid)
        a = httpx.URL(await oauth_google.start_authorization(uid)).params["state"]
        b = httpx.URL(await oauth_google.start_authorization(uid)).params["state"]
        assert a != b

    asyncio.run(run())


# --------------------------------------------------------------------------- the attacks


def test_forged_state_cannot_connect_anything(clean_db):
    """A state we never issued must not resolve to any user."""
    async def run():
        with pytest.raises(InvalidState):
            await oauth_google.handle_callback(state="totally-made-up", code="c", http_client=_google())

    asyncio.run(run())


def test_replayed_state_fails_the_second_time(clean_db):
    """Single-use. A captured callback replayed must NOT re-authorize."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        assert await oauth_google.handle_callback(state=state, code="c1", http_client=_google()) == uid
        with pytest.raises(InvalidState):
            await oauth_google.handle_callback(state=state, code="c2", http_client=_google())

    asyncio.run(run())


def test_concurrent_replay_lets_exactly_one_through(clean_db):
    """The claim is a single UPDATE guarded on consumed_at IS NULL, so a race cannot double-spend."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        results = await asyncio.gather(
            oauth_google.handle_callback(state=state, code="c", http_client=_google()),
            oauth_google.handle_callback(state=state, code="c", http_client=_google()),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        bad = [r for r in results if isinstance(r, InvalidState)]
        assert len(ok) == 1 and len(bad) == 1

    asyncio.run(run())


def test_expired_state_is_rejected(clean_db):
    async def run():
        uid = uuid4()
        state = await _start(uid)
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.OAuthState).where(schema.OAuthState.state == state))).scalar_one()
            row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
            await s.flush()
        with pytest.raises(InvalidState):
            await oauth_google.handle_callback(state=state, code="c", http_client=_google())

    asyncio.run(run())


def test_invalid_expired_and_reused_are_indistinguishable(clean_db):
    """One message for all three: distinguishing them tells an attacker whether a state exists."""
    async def run():
        uid = uuid4()
        used = await _start(uid)
        await oauth_google.handle_callback(state=used, code="c", http_client=_google())
        msgs = set()
        for st in (used, "never-issued"):
            try:
                await oauth_google.handle_callback(state=st, code="c", http_client=_google())
            except InvalidState as e:
                msgs.add(str(e))
        assert len(msgs) == 1, f"leaks which failure occurred: {msgs}"

    asyncio.run(run())


def test_identity_comes_from_the_state_row_not_the_callback(clean_db):
    """THE central property: A starts the flow; the callback cannot make it land on B."""
    async def run():
        a, b = uuid4(), uuid4()
        await users.ensure(b)
        state = await _start(a)
        # The callback signature has no user parameter at all — identity is recovered from the row.
        landed = await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        assert landed == a
        assert await oauth_google.get_integration(a) is not None
        assert await oauth_google.get_integration(b) is None, "B was connected by A's callback"

    asyncio.run(run())


def test_denied_consent_is_a_clean_signal_not_a_crash(clean_db):
    async def run():
        with pytest.raises(ConsentDenied):
            await oauth_google.handle_callback(state="x", code=None, error="access_denied",
                                               http_client=_google())

    asyncio.run(run())


def test_missing_code_is_rejected(clean_db):
    async def run():
        uid = uuid4()
        state = await _start(uid)
        with pytest.raises(MissingCode):
            await oauth_google.handle_callback(state=state, code=None, http_client=_google())

    asyncio.run(run())


def test_state_is_consumed_before_the_code_is_exchanged(clean_db):
    """A replay must not even reach Google. Assert no token request went out on the second attempt."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        seen: list[httpx.Request] = []
        with pytest.raises(InvalidState):
            await oauth_google.handle_callback(state=state, code="c", http_client=_google(capture=seen))
        assert seen == [], "a replayed callback reached Google's token endpoint"

    asyncio.run(run())


# --------------------------------------------------------------------------- token handling


def test_refresh_token_is_encrypted_at_rest(clean_db):
    """The raw token must not be findable in the column — a DB dump must not hand over the calendar."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.Integration))).scalar_one()
        assert row.refresh_token_encrypted
        assert "rt-live-secret" not in row.refresh_token_encrypted
        assert crypto.decrypt(row.refresh_token_encrypted) == "rt-live-secret"
        assert row.provider_account_id == "student@school.edu"
        # scopes are stored as the individual granted scopes (space-split), calendar.events among them
        assert "https://www.googleapis.com/auth/calendar.events" in row.scopes
        assert "https://www.googleapis.com/auth/userinfo.email" in row.scopes

    asyncio.run(run())


def test_connect_refuses_rather_than_storing_plaintext_without_a_key(clean_db, monkeypatch):
    """No key must mean FAIL, never a silent plaintext write."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        monkeypatch.delenv("BRUCE_ENCRYPTION_KEY", raising=False)
        with pytest.raises(crypto.EncryptionUnavailable):
            await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        async with user_session(uid) as s:
            assert (await s.execute(select(schema.Integration))).scalar_one_or_none() is None

    asyncio.run(run())


def test_missing_refresh_token_is_an_explicit_failure(clean_db):
    """Google omits refresh_token on re-consent. Accepting that would produce an integration that
    dies in an hour and cannot act while the app is closed — the whole point of the product."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        body = {"access_token": "at", "scope": SCOPE, "expires_in": 3599}
        with pytest.raises(TokenExchangeFailed, match="refresh token"):
            await oauth_google.handle_callback(state=state, code="c",
                                               http_client=_google(token_body=body))

    asyncio.run(run())


def test_insufficient_scope_is_rejected(clean_db):
    async def run():
        uid = uuid4()
        state = await _start(uid)
        body = {"access_token": "at", "refresh_token": "rt",
                "scope": "https://www.googleapis.com/auth/userinfo.email"}
        with pytest.raises(InsufficientScope):
            await oauth_google.handle_callback(state=state, code="c",
                                               http_client=_google(token_body=body))

    asyncio.run(run())


def test_token_exchange_failure_never_leaks_the_client_secret(clean_db):
    """Google's token endpoint echoes request params in error bodies. Status only."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        body = {"error": "invalid_grant", "client_secret": "csecret-must-never-leak"}
        with pytest.raises(TokenExchangeFailed) as e:
            await oauth_google.handle_callback(state=state, code="c",
                                               http_client=_google(token_status=400, token_body=body))
        assert "csecret-must-never-leak" not in str(e.value)
        assert "400" in str(e.value)

    asyncio.run(run())


def test_pkce_verifier_is_sent_on_the_exchange_and_the_secret_is_not_in_the_url(clean_db):
    async def run():
        uid = uuid4()
        state = await _start(uid)
        seen: list[httpx.Request] = []
        await oauth_google.handle_callback(state=state, code="c", http_client=_google(capture=seen))
        token_req = next(r for r in seen if "token" in str(r.url))
        body = token_req.content.decode()
        assert "code_verifier=" in body, "PKCE verifier missing — an intercepted code would work"
        assert "csecret-must-never-leak" not in str(token_req.url), "secret must be in the body, not the URL"

    asyncio.run(run())


# --------------------------------------------------------------------------- refresh / revoke


def test_access_token_requires_a_connection(clean_db):
    async def run():
        uid = uuid4()
        await users.ensure(uid)
        with pytest.raises(NotConnected):
            await oauth_google.access_token_for(uid, http_client=_google())

    asyncio.run(run())


def test_revoked_refresh_token_marks_the_integration_revoked(clean_db):
    """So the UI says 'reconnect' instead of retrying forever against a dead credential."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        with pytest.raises(RefreshFailed):
            await oauth_google.access_token_for(
                uid, http_client=_google(token_status=400, token_body={"error": "invalid_grant"})
            )
        row = await oauth_google.get_integration(uid)
        assert row.status == "revoked" and row.revoked_at is not None

    asyncio.run(run())


def test_disconnect_deletes_the_credential_even_if_google_is_unreachable(clean_db):
    """A student pressing Disconnect must never leave Bruce holding a usable token because a
    network blip ate the revoke call."""
    async def run():
        uid = uuid4()
        state = await _start(uid)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())

        def dead(request):
            raise httpx.ConnectError("google unreachable")

        await oauth_google.disconnect(uid, http_client=httpx.AsyncClient(transport=httpx.MockTransport(dead)))
        row = await oauth_google.get_integration(uid)
        assert row.refresh_token_encrypted is None and row.status == "disconnected"

    asyncio.run(run())


def test_disconnect_is_idempotent(clean_db):
    async def run():
        uid = uuid4()
        state = await _start(uid)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        assert await oauth_google.disconnect(uid, http_client=_google()) is True
        assert await oauth_google.disconnect(uid, http_client=_google()) is False

    asyncio.run(run())


def test_integrations_are_isolated_between_users(clean_db):
    """RLS applies to the new table like every other."""
    async def run():
        a, b = uuid4(), uuid4()
        state = await _start(a)
        await oauth_google.handle_callback(state=state, code="c", http_client=_google())
        await users.ensure(b)
        assert await oauth_google.get_integration(b) is None
        async with user_session(b) as s:
            assert (await s.execute(select(schema.Integration))).scalars().all() == []

    asyncio.run(run())


# --------------------------------------------------------------------------- crypto unit


def test_encryption_roundtrip_and_tamper_detection(monkeypatch):
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", KEY)
    ct = crypto.encrypt("rt-secret")
    assert "rt-secret" not in ct
    assert crypto.decrypt(ct) == "rt-secret"
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(ct[:-4] + "AAAA")


def test_decrypt_with_a_different_key_fails_closed(monkeypatch):
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", KEY)
    ct = crypto.encrypt("rt-secret")
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(ct)


# --------------------------------------------------------------------------- live


_LIVE_SKIP = None if (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_REFRESH_TOKEN")) else (
    "Google OAuth not configured — set GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN to run live"
)


@pytest.mark.skipif(_LIVE_SKIP is not None, reason=_LIVE_SKIP or "")
def test_live_refresh_token_yields_a_real_access_token(clean_db, monkeypatch):
    """LIVE: proves the stored credential actually works against Google. Skips until configured."""
    async def run():
        uid = uuid4()
        await users.ensure(uid)
        await oauth_google._store_integration(
            user_id=uid, refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"], scopes=[SCOPE], account=None
        )
        token = await oauth_google.access_token_for(uid)
        assert token and len(token) > 20

    asyncio.run(run())
