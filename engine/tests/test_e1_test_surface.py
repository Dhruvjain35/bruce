"""E1 — the internal zero-terminal staging test surface, against REAL Postgres.

Adversarial coverage of the smallest authenticated internal page that starts a staging enrollment,
shows privacy-safe live results, and ends the enrollment. Everything runs against the disposable
``bruce_test`` DB through the restricted ``bruce_app`` role (via ``pg_test_db`` / ``clean_db``), so the
real RLS policies + the append-only capability audit are exercised — no SQLite, no mocks. The FastAPI
app (``TestClient(bruce_engine.api.app)``) with minted internal-user HS256 JWTs is the surface under
test. Covers, per the E1 acceptance criteria:

  * unauthenticated access denied (401) on the JSON surfaces
  * a non-internal user is denied with a GENERIC 403 (no account enumeration)
  * a state-changing POST without the CSRF token is rejected (403)
  * the session cookie is Secure + HttpOnly + SameSite=Strict
  * start-test creates a StagingTestEnrollment with the correct TTL for each duration option
  * end-test revokes the enrollment immediately
  * the readiness screen returns the expected fields
  * live results are PRIVACY-SAFE: seeded message content / full handle / attachment path / prompt /
    chain-of-thought never appear, while the derived privacy-safe fields DO
  * E1 never creates a ProductionAccountEntitlement (production separation)
  * an audit row is recorded for start AND end, with the server-derived internal actor

Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import access_control, internal_test, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository

users_repo = PostgresUserRepository()

# Sentinel content seeded into the conversation. NONE of these may ever surface in an API response.
FULL_HANDLE = "+15551234567"
SENTINELS = [
    "SECRET_MESSAGE_BODY_ZZZ",       # inbound user text
    "SECRET_ASSISTANT_REPLY_ZZZ",    # styled assistant reply text
    "SECRET_SYSTEM_PROMPT_ZZZ",      # decision JSONB (prompt-ish)
    "SECRET_COT_REASONING_ZZZ",      # decision JSONB (chain-of-thought)
    "SECRET_EVENT_TITLE_ZZZ",        # event candidate title
    "SECRET_OUTBOUND_REPLY_ZZZ",     # outbound message text
    "SECRET_ERROR_DETAIL_ZZZ",       # mission error detail (only the category may show)
    "SECRET_SPAN_TEXT_ZZZ",          # event-candidate provenance span
    FULL_HANDLE,                     # the full phone handle (only the masked tail may show)
    "/secret/path/attach.png",       # attachment url/path
    "secret_flyer.png",              # attachment filename
]


# --------------------------------------------------------------------------- fixtures / helpers


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    """Rebuild the app engine per test with NullPool (real asyncpg, real PG — no cross-loop pooling), so
    the ``asyncio.run`` seeding loops and the TestClient request loop never share a dead-loop connection.
    Depends on ``pg_test_db`` so the module skips cleanly when Postgres isn't configured."""

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


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _client() -> TestClient:
    # https base URL so Secure cookies are stored + resent by the httpx cookie jar over the test flow.
    return TestClient(api.app, base_url="https://testserver")


def _token(uid: UUID) -> str:
    payload = {"sub": str(uid), "exp": int(time.time()) + 3600}
    aud = os.environ.get("BRUCE_JWT_AUDIENCE")
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, os.environ["BRUCE_JWT_SECRET"], algorithm="HS256")


def _make_internal(monkeypatch, uid: UUID) -> None:
    monkeypatch.setenv("BRUCE_INTERNAL_USER_IDS", str(uid))


def _login(client: TestClient, uid: UUID):
    return client.post("/internal/test/login", json={"token": _token(uid)})


async def _seed_identity(uid: UUID) -> None:
    """One active linked messaging identity (required before a test may start)."""
    await users_repo.ensure(uid)
    async with user_session(uid) as s:
        s.add(schema.MessagingIdentity(
            user_id=uid, channel="imessage", provider="apple", channel_identity=FULL_HANDLE))


async def _seed_conversation(uid: UUID) -> None:
    """A full, realistic turn carrying sentinel content in every sensitive column, so the privacy test
    can prove the surface derives safe fields WITHOUT ever exposing the raw content."""
    await users_repo.ensure(uid)
    async with user_session(uid) as s:
        mission = schema.Mission(user_id=uid, kind="conversation", status="failed", phase="failed",
                                 error="ProviderUnavailable: SECRET_ERROR_DETAIL_ZZZ")
        s.add(mission)
        await s.flush()

        inbound = schema.InboundMessageRow(
            user_id=uid, channel="imessage", provider_message_id="pm-e1-1",
            channel_identity=FULL_HANDLE, text="SECRET_MESSAGE_BODY_ZZZ")
        s.add(inbound)
        await s.flush()

        s.add(schema.MessageAttachment(
            user_id=uid, inbound_message_id=inbound.id, kind="image", media_type="image/png",
            url="/secret/path/attach.png", filename="secret_flyer.png"))

        ec = schema.EventCandidate(
            user_id=uid, title="SECRET_EVENT_TITLE_ZZZ", status="proposed",
            idempotency_key="ec-e1-1", inbound_message_id=inbound.id,
            provenance={"span": "SECRET_SPAN_TEXT_ZZZ"})
        s.add(ec)
        await s.flush()

        s.add(schema.ConversationTurn(
            user_id=uid, channel="imessage", channel_identity=FULL_HANDLE,
            provider_message_id="pm-e1-1", role="user", text="SECRET_MESSAGE_BODY_ZZZ"))
        s.add(schema.ConversationTurn(
            user_id=uid, channel="imessage", channel_identity=FULL_HANDLE,
            provider_message_id="pm-e1-1", role="assistant", intent="event_capture",
            response_type="event_candidate", risk_level="low", text="SECRET_ASSISTANT_REPLY_ZZZ",
            decision={"prompt": "SECRET_SYSTEM_PROMPT_ZZZ", "cot": "SECRET_COT_REASONING_ZZZ"},
            mission_id=mission.id, event_candidate_id=ec.id))

        s.add(schema.OutboundMessageRow(
            user_id=uid, channel="imessage", kind="acknowledged", text="SECRET_OUTBOUND_REPLY_ZZZ",
            to_handle=FULL_HANDLE, status="sent", idempotency_key="ob-e1-1", mission_id=mission.id))

        s.add(schema.ModelCost(
            user_id=uid, mission_id=mission.id, provider="openai", model="gpt-x",
            input_tokens=100, output_tokens=50, cost_usd=0.0123))


async def _live_enrollment(uid: UUID):
    env = access_control.current_environment()
    now = _now()
    async with worker_session() as s:
        rows = (await s.execute(select(schema.StagingTestEnrollment).where(
            schema.StagingTestEnrollment.user_id == uid,
            schema.StagingTestEnrollment.capability == "conversation",
            schema.StagingTestEnrollment.environment == env,
            schema.StagingTestEnrollment.revoked_at.is_(None)))).scalars().all()
    return [r for r in rows if r.expires_at is None or r.expires_at > now]


# --------------------------------------------------------------------------- authn / authz


def test_unauthenticated_json_surfaces_denied(clean_db):
    """No session cookie -> 401 on every internal JSON surface (readiness, results, start, end)."""
    c = _client()
    assert c.get("/internal/test/readiness").status_code == 401
    assert c.get("/internal/test/results").status_code == 401
    assert c.post("/internal/test/start", json={"duration": "15m"}).status_code == 401
    assert c.post("/internal/test/end").status_code == 401


def test_non_internal_user_denied_generic_no_enumeration(clean_db, monkeypatch):
    """An authenticated but NON-internal user is denied. Login refuses (403). Even with a directly-set
    session cookie the JSON surface returns a GENERIC 403 that reveals nothing about the user."""
    internal = uuid4()
    outsider = uuid4()
    _make_internal(monkeypatch, internal)   # allowlist contains ONLY `internal`
    _run(users_repo.ensure(outsider))

    c = _client()
    r = _login(c, outsider)
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden"        # generic — no "unknown user" / "not enrolled" leak

    # Bypass login by planting the (valid, signed) cookie directly: the gate STILL denies (allowlist).
    c.cookies.set(internal_test.SESSION_COOKIE, _token(outsider))
    r2 = c.get("/internal/test/readiness")
    assert r2.status_code == 403 and r2.json()["detail"] == "forbidden"


def test_login_sets_secure_httponly_samesite_cookie(clean_db, monkeypatch):
    """The internal-test session cookie is Secure + HttpOnly + SameSite=Strict, and short-lived."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(users_repo.ensure(uid))

    c = _client()
    r = _login(c, uid)
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    low = sc.lower()
    assert internal_test.SESSION_COOKIE in sc
    assert "httponly" in low
    assert "secure" in low
    assert "samesite=strict" in low
    assert "max-age=" in low   # short-lived / bounded session


# --------------------------------------------------------------------------- CSRF


def test_start_without_csrf_is_rejected(clean_db, monkeypatch):
    """A state-changing POST (start) with NO X-CSRF-Token header is rejected (403), and creates no
    enrollment. The CSRF check runs before any DB mutation."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))

    c = _client()
    assert _login(c, uid).status_code == 200
    r = c.post("/internal/test/start", json={"duration": "15m"})   # deliberately no CSRF header
    assert r.status_code == 403
    assert _run(_live_enrollment(uid)) == []


def test_start_with_wrong_csrf_is_rejected(clean_db, monkeypatch):
    """A forged/mismatched CSRF token is rejected (constant-time compare)."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    r = c.post("/internal/test/start", json={"duration": "15m"},
               headers={"X-CSRF-Token": "not-the-real-token"})
    assert r.status_code == 403
    assert _run(_live_enrollment(uid)) == []


# --------------------------------------------------------------------------- start / end + TTL


@pytest.mark.parametrize("duration,ttl_s", [
    ("15m", 15 * 60), ("30m", 30 * 60), ("1h", 60 * 60), ("persistent", None),
])
def test_start_creates_enrollment_with_correct_ttl(clean_db, monkeypatch, duration, ttl_s):
    """Start with each duration option creates exactly one live StagingTestEnrollment with the right TTL
    (or no expiry for persistent), for capability=conversation, WITHOUT a redeploy/restart/DB edit."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))

    c = _client()
    _login(c, uid)
    csrf = internal_test._csrf_for_session(_token(uid))
    before = _now()
    r = c.post("/internal/test/start", json={"duration": duration}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["duration"] == duration

    live = _run(_live_enrollment(uid))
    assert len(live) == 1
    enr = live[0]
    assert enr.capability == "conversation"
    if ttl_s is None:
        assert enr.expires_at is None and body["persistent"] is True
    else:
        expected = before + datetime.timedelta(seconds=ttl_s)
        assert enr.expires_at is not None
        assert abs((enr.expires_at - expected).total_seconds()) < 120


def test_end_revokes_enrollment_immediately(clean_db, monkeypatch):
    """End immediately revokes every live enrollment for self (revoked_at set; access gate flips to DENY)."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    csrf = internal_test._csrf_for_session(_token(uid))

    assert c.post("/internal/test/start", json={"duration": "1h"},
                  headers={"X-CSRF-Token": csrf}).status_code == 200
    assert _run(access_control.conversation_access(uid)).allow is True   # enrolled -> ALLOW

    r = c.post("/internal/test/end", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    assert _run(_live_enrollment(uid)) == []
    assert _run(access_control.conversation_access(uid)).allow is False  # revoked -> DENY


def test_start_requires_a_linked_identity(clean_db, monkeypatch):
    """With no linked messaging identity, start is refused (409) — E1 enrolls an ALREADY-LINKED user and
    never creates/links an account here."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(users_repo.ensure(uid))   # user exists but has NO linked identity
    c = _client()
    _login(c, uid)
    csrf = internal_test._csrf_for_session(_token(uid))
    r = c.post("/internal/test/start", json={"duration": "15m"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert _run(_live_enrollment(uid)) == []


# --------------------------------------------------------------------------- readiness


def test_readiness_returns_expected_fields(clean_db, monkeypatch):
    """The readiness screen returns the documented content-free fields."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    rd = c.get("/internal/test/readiness")
    assert rd.status_code == 200
    j = rd.json()
    for key in ("environment", "deployment_version", "api", "database", "model", "relay",
                "relay_outbound_paused", "capability_killed", "outbound_queue",
                "linked_identities", "current_enrollment", "can_start"):
        assert key in j, key
    assert j["environment"] == access_control.current_environment()
    assert j["database"] == "ok"
    assert j["can_start"] is True                     # has a linked identity
    assert j["linked_identities"][0]["handle_masked"].endswith("4567")
    assert FULL_HANDLE not in rd.text                 # never the full handle


# --------------------------------------------------------------------------- privacy-safe live results


def test_live_results_are_privacy_safe(clean_db, monkeypatch):
    """The live-results view exposes DERIVED privacy-safe fields and NEVER the raw content: no message
    text, full handle, attachment path/filename, prompt, or chain-of-thought appears anywhere."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    _run(_seed_conversation(uid))

    c = _client()
    _login(c, uid)
    r = c.get("/internal/test/results")
    assert r.status_code == 200
    raw = r.text
    for secret in SENTINELS:
        assert secret not in raw, f"leaked sensitive content: {secret}"

    turns = r.json()["turns"]
    assert len(turns) == 1
    t = turns[0]
    # derived, privacy-safe fields ARE present
    assert t["handle_masked"] == "•••4567"
    assert t["attachment_detected"] is True
    assert t["attachment_format"] == "image/png"      # normalized format, not the path/filename
    assert t["intent"] == "event_capture"
    assert t["response_type"] == "event_candidate"
    assert t["risk_level"] == "low"
    assert t["mission_created"] is True
    assert t["event_candidate_created"] is True
    assert t["outbound_status"] == "sent"
    assert abs(t["estimated_cost_usd"] - 0.0123) < 1e-6
    assert t["error_category"] == "ProviderUnavailable"   # category only, not the detail
    assert t["model_latency_ms"] is not None and t["model_latency_ms"] >= 0


# --------------------------------------------------------------------------- production separation + audit


def test_start_never_creates_production_entitlement(clean_db, monkeypatch):
    """E1 start creates ONLY a StagingTestEnrollment — never a ProductionAccountEntitlement."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    csrf = internal_test._csrf_for_session(_token(uid))
    assert c.post("/internal/test/start", json={"duration": "30m"},
                  headers={"X-CSRF-Token": csrf}).status_code == 200

    async def _count_prod():
        async with worker_session() as s:
            return (await s.execute(select(func.count()).select_from(
                schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == uid))).scalar_one()

    assert _run(_count_prod()) == 0
    # and access is sourced from staging, never production
    d = _run(access_control.conversation_access(uid))
    assert d.allow is True and d.source == "staging"


def test_start_and_end_are_audited_with_server_actor(clean_db, monkeypatch):
    """Start and end each append exactly one content-free CapabilityAudit row whose actor is the
    SERVER-derived internal identity (internal_web:<user_id>) and whose target is the internal user."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    csrf = internal_test._csrf_for_session(_token(uid))
    c.post("/internal/test/start", json={"duration": "15m"}, headers={"X-CSRF-Token": csrf})
    c.post("/internal/test/end", headers={"X-CSRF-Token": csrf})

    async def _audit():
        from bruce_engine.db import admin_session
        async with admin_session() as s:   # capability_audit is admin-read (app_is_admin), append-only
            return (await s.execute(select(schema.CapabilityAudit).order_by(
                schema.CapabilityAudit.created_at))).scalars().all()

    rows = _run(_audit())
    actions = sorted(r.action for r in rows)
    assert actions == ["enroll_staging", "revoke_staging"]
    assert all(r.actor == f"internal_web:{uid}" for r in rows)
    assert all(r.target_user_id == uid for r in rows)


# --------------------------------------------------------------------------- HTML page


def test_page_login_view_when_unauthenticated(clean_db):
    """GET the page with no session -> the login view (200 HTML), not a stack trace / not the dashboard."""
    c = _client()
    r = c.get("/internal/test")
    assert r.status_code == 200
    assert "Sign in" in r.text and "Test controls" not in r.text


def test_page_dashboard_embeds_csrf_when_internal(clean_db, monkeypatch):
    """GET the page as an internal user -> the dashboard with the session-bound CSRF token embedded."""
    uid = uuid4()
    _make_internal(monkeypatch, uid)
    _run(_seed_identity(uid))
    c = _client()
    _login(c, uid)
    r = c.get("/internal/test")
    assert r.status_code == 200
    assert "Test controls" in r.text
    assert internal_test._csrf_for_session(_token(uid)) in r.text


# --------------------------------------------------------------------------- magic-link browser sign-in (A4/E1 UX)


def test_magic_link_signs_in_internal_user(clean_db, monkeypatch):
    """An operator-minted magic link exchanges for a fresh Secure/HttpOnly/SameSite session cookie and
    lands the founder in the authenticated view — no token paste."""
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    tok = asyncio.run(internal_test.mint_magic_link_token(uid, ttl_seconds=600))
    r = c.get(f"/internal/test/auth?t={tok}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == internal_test.COOKIE_PATH
    setc = r.headers.get("set-cookie", "").lower()
    assert "httponly" in setc and "secure" in setc and "samesite=strict" in setc
    # the cookie now authenticates the page (authenticated view, not the sign-in landing)
    page = c.get("/internal/test")
    assert page.status_code == 200 and "sign-in link" not in page.text.lower()


def test_magic_link_non_internal_is_generic_denied(clean_db, monkeypatch):
    """A magic token for a NON-internal user is refused with a generic denied page (no enumeration)."""
    internal, outsider = uuid4(), uuid4()
    _make_internal(monkeypatch, internal)                      # allowlist has ONLY `internal`
    c = _client()
    outsider_tok = asyncio.run(internal_test.mint_magic_link_token(outsider))
    r = c.get(f"/internal/test/auth?t={outsider_tok}", follow_redirects=False)
    assert r.status_code == 403 and "not authorized" in r.text.lower()
    assert internal_test.SESSION_COOKIE not in r.headers.get("set-cookie", "")


def test_magic_link_invalid_or_expired_is_denied(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    assert c.get("/internal/test/auth?t=not-a-token", follow_redirects=False).status_code == 403
    expired = asyncio.run(internal_test.mint_magic_link_token(uid, ttl_seconds=-5))
    assert c.get(f"/internal/test/auth?t={expired}", follow_redirects=False).status_code == 403


def test_a_plain_session_jwt_is_not_accepted_as_a_magic_token(clean_db, monkeypatch):
    """The magic endpoint requires the e1_magic scope — a normal session JWT cannot sign in via /auth
    (it is not a general bearer channel)."""
    uid = uuid4(); _make_internal(monkeypatch, uid)
    assert internal_test._verify_magic_token(_token(uid)) is None   # no e1_magic claim -> rejected


def test_login_landing_has_no_token_paste_and_shows_env_banner(clean_db, monkeypatch):
    uid = uuid4(); _make_internal(monkeypatch, uid)
    c = _client()
    body = c.get("/internal/test").text.lower()               # unauthenticated -> the sign-in landing
    assert "envbanner" in body and "environment" in body       # visible staging environment banner
    assert 'type="password"' not in body and "paste your bruce session token" not in body


def test_mint_magic_requires_signing_secret(monkeypatch):
    monkeypatch.delenv("BRUCE_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(internal_test.mint_magic_link_token(uuid4()))
