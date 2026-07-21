"""Single-use magic-link consumption (E1) — against REAL Postgres.

Proves the founder's internal-test magic link is genuinely SINGLE-USE, not merely short-TTL. Only the
sha256 hash of the token's random jti is stored (never the raw JWT / URL), bound to (user, environment);
``/internal/test/auth`` atomically consumes the matching unused row, so a replay — or a concurrent
double-open — yields exactly one session. Everything runs against the disposable ``bruce_test`` DB via
``pg_test_db`` / ``clean_db``, exercising the real admin-only RLS on ``magic_link_tokens`` — no mocks.

Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import internal_test, schema
from bruce_engine.db import admin_session


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    """Rebuild the app engine per test with NullPool so the ``asyncio.run`` mint/consume loops and the
    TestClient request loop never share a dead-loop connection. Skips when Postgres isn't configured."""

    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


def _client() -> TestClient:
    # https base URL so Secure cookies are stored + resent by the httpx cookie jar.
    return TestClient(api.app, base_url="https://testserver")


def _make_internal(monkeypatch, uid: UUID) -> None:
    monkeypatch.setenv("BRUCE_INTERNAL_USER_IDS", str(uid))


def _mint(uid: UUID, ttl: int = 600) -> str:
    return _run(internal_test.mint_magic_link_token(uid, ttl_seconds=ttl))


def _jti(token: str) -> str:
    verified = internal_test._verify_magic_token(token)
    assert verified is not None
    return verified[1]


def _url(token: str) -> str:
    return f"/internal/test/auth?t={token}"


async def _prod_entitlement_count() -> int:
    async with admin_session() as s:
        return (await s.execute(
            select(func.count()).select_from(schema.ProductionAccountEntitlement))).scalar_one()


# 1. first use succeeds -----------------------------------------------------------------------------
def test_first_use_succeeds(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    r = _client().get(_url(_mint(uid)), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == internal_test.COOKIE_PATH
    setc = r.headers.get("set-cookie", "").lower()
    assert "httponly" in setc and "secure" in setc and "samesite=strict" in setc


# 2. second use fails -------------------------------------------------------------------------------
def test_second_use_fails(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    tok = _mint(uid)
    assert _client().get(_url(tok), follow_redirects=False).status_code == 303
    r2 = _client().get(_url(tok), follow_redirects=False)      # fresh client, same link
    assert r2.status_code == 403
    assert internal_test.SESSION_COOKIE not in r2.headers.get("set-cookie", "")


# 3. reuse after logout fails -----------------------------------------------------------------------
def test_reuse_after_logout_fails(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    tok = _mint(uid)
    assert c.get(_url(tok), follow_redirects=False).status_code == 303
    assert c.post("/internal/test/logout").status_code == 200
    # logout invalidates the session but must NOT make the consumed link reusable.
    assert c.get(_url(tok), follow_redirects=False).status_code == 403


# 4. concurrent double-use -> exactly one success ---------------------------------------------------
def test_concurrent_double_use_exactly_one_success(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    env = internal_test._safe_env()
    jti = _jti(_mint(uid))

    async def _both():
        return await asyncio.gather(
            internal_test._consume_magic_link(jti, uid, env),
            internal_test._consume_magic_link(jti, uid, env))

    results = _run(_both())
    assert sum(1 for ok in results if ok) == 1   # exactly one winner under a row-locked conditional UPDATE


# 5. expired link fails -----------------------------------------------------------------------------
def test_expired_link_fails(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    assert _client().get(_url(_mint(uid, ttl=-5)), follow_redirects=False).status_code == 403


# 6. wrong environment fails ------------------------------------------------------------------------
def test_wrong_environment_fails(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    jti = _jti(_mint(uid))                                     # minted for the current env
    assert _run(internal_test._consume_magic_link(jti, uid, "some-other-env")) is False
    assert _run(internal_test._consume_magic_link(jti, uid, internal_test._safe_env())) is True  # control


# 7. wrong user fails -------------------------------------------------------------------------------
def test_wrong_user_fails(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    env = internal_test._safe_env()
    jti = _jti(_mint(uid))
    assert _run(internal_test._consume_magic_link(jti, uuid4(), env)) is False   # different user
    assert _run(internal_test._consume_magic_link(jti, uid, env)) is True        # control


# 8. malformed token fails generically --------------------------------------------------------------
def test_malformed_token_fails_generically(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    for bad in ("not-a-jwt", "a.b.c", "x"):
        r = c.get(_url(bad), follow_redirects=False)
        assert r.status_code == 403 and "not authorized" in r.text.lower()
    assert internal_test._verify_magic_token("garbage") is None


# 9. token hash / jti never exposed in responses or logs --------------------------------------------
def test_token_hash_and_jti_not_exposed(clean_db, monkeypatch, caplog):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    tok = _mint(uid)
    jti = _jti(tok)
    jti_hash = internal_test._sha256(jti)
    with caplog.at_level("DEBUG"):
        ok = _client().get(_url(tok), follow_redirects=False)          # success
        denied = _client().get(_url(tok), follow_redirects=False)      # reuse -> denied
    for resp in (ok, denied):
        assert jti not in resp.text and jti_hash not in resp.text
        assert jti not in str(resp.headers) and jti_hash not in str(resp.headers)
    logtext = "\n".join(rec.getMessage() for rec in caplog.records)
    assert jti not in logtext and jti_hash not in logtext


# 10. normal authenticated session still works after the link is consumed ---------------------------
def test_session_works_after_link_consumed(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    assert c.get(_url(_mint(uid)), follow_redirects=False).status_code == 303
    # the exchanged session cookie authenticates the JSON surface even though the link is now consumed.
    assert c.get("/internal/test/readiness").status_code == 200
    page = c.get("/internal/test")
    assert page.status_code == 200 and "sign-in link" not in page.text.lower()


# 11. ProductionAccountEntitlement remains untouched ------------------------------------------------
def test_production_entitlement_untouched(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    assert _client().get(_url(_mint(uid)), follow_redirects=False).status_code == 303
    assert _run(_prod_entitlement_count()) == 0
