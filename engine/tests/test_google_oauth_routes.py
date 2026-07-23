"""I0.6 — real Google OAuth routes: auth required, honest degradation when uncredentialed, no leaks.

These cover the route wiring + the not-configured / bad-input paths (no credentials, no DB round-trip). The
live authorize->callback->token-store flow is live-verified once GOOGLE_CLIENT_ID/SECRET land (it can't be
faked, per the no-fake-integrations rule).
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

import bruce_engine.api as api

SECRET = "test-secret-at-least-32-bytes-long-1234"
client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", SECRET)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    # UNcredentialed: is_configured() must be False so we test honest degradation, never a fake
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)


def _auth(uid=None):
    tok = jwt.encode({"sub": str(uid or uuid4()), "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def test_status_requires_auth():
    assert client.get("/v1/integrations/google/status").status_code in (401, 403)


def test_status_reports_credential_blocked_when_unconfigured():
    r = client.get("/v1/integrations/google/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["connection_status"] == "credential_blocked"


def test_connect_503_when_unconfigured():
    r = client.get("/v1/integrations/google/connect", headers=_auth(), follow_redirects=False)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "google_not_configured"


def test_connect_requires_auth():
    assert client.get("/v1/integrations/google/connect", follow_redirects=False).status_code in (401, 403)


def test_callback_missing_state_is_honest_error_no_leak():
    # no user auth on the callback BY DESIGN; a missing/invalid state -> honest 400 page, no state/code echoed
    r = client.get("/v1/integrations/google/callback")
    assert r.status_code == 400
    body = r.text.lower()
    assert "couldn't connect google" in body
    assert "state" not in body and "code" not in body and "token" not in body


def test_callback_consent_denied_is_honest_error():
    r = client.get("/v1/integrations/google/callback", params={"error": "access_denied"})
    assert r.status_code == 400 and "couldn't connect" in r.text.lower()


def test_callback_unexpected_error_never_500_branded_page_with_ref(monkeypatch):
    # the exact live failure: an owner-connection RuntimeError (NOT an OAuthError) must NOT escape as a 500
    async def boom(**kw):
        raise RuntimeError("BRUCE_DATABASE_URL not set — owner connection required for retention sweeps.")
    monkeypatch.setattr(api.oauth_google, "handle_callback", boom)
    r = client.get("/v1/integrations/google/callback", params={"state": "xx", "code": "yy"})
    assert r.status_code == 400                                   # branded, NEVER 500
    body = r.text.lower()
    assert "couldn't connect google" in body and "nothing was saved" in body
    assert "ref:" in body                                         # privacy-safe error reference
    # no exception detail / oauth data leaked into the page
    for leak in ("bruce_database_url", "runtimeerror", "traceback", "retention", "state=", "code="):
        assert leak not in body


def test_callback_success_page_copy(monkeypatch):
    async def ok(**kw):
        return None
    monkeypatch.setattr(api.oauth_google, "handle_callback", ok)
    r = client.get("/v1/integrations/google/callback", params={"state": "xx", "code": "yy"})
    assert r.status_code == 200
    body = r.text.lower()
    assert "google connected" in body and "close this tab" in body


def test_oauth_error_category_mapping():
    import sqlalchemy.exc as sae
    from bruce_engine import oauth_google as og
    from bruce_engine.crypto import EncryptionUnavailable
    cat = lambda e: api._oauth_error_category(e)[1]
    assert cat(og.InvalidState("x")) == "invalid_or_expired_state"
    assert cat(og.TokenExchangeFailed("x")) == "token_exchange_rejected"
    assert cat(og.InsufficientScope("x")) == "insufficient_scope"
    assert cat(og.ConsentDenied("x")) == "consent_denied"
    assert cat(EncryptionUnavailable("x")) == "encryption_failed"
    assert cat(sae.IntegrityError("s", {}, Exception("o"))) == "duplicate_connection_conflict"
    assert cat(sae.ProgrammingError("s", {}, Exception("o"))) == "integration_schema_mismatch"
    assert cat(RuntimeError("x")) == "unknown"
    # invalid_or_expired_state is retryable; token_exchange_rejected is not
    assert api._oauth_error_category(og.InvalidState("x"))[2] is True
    assert api._oauth_error_category(og.TokenExchangeFailed("x"))[2] is False


def _connect_token(uid, purpose="google_connect", secret=SECRET):
    return jwt.encode({"sub": str(uid), "purpose": purpose, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")


def test_connect_start_no_token_branded_401():
    r = client.get("/v1/integrations/google/connect/start", follow_redirects=False)
    assert r.status_code == 401
    b = r.text.lower()
    assert "isn't valid" in b and "ref:" in b and "connect link" in b


def test_connect_start_plain_user_jwt_rejected():
    # a valid user JWT WITHOUT purpose=google_connect must NOT act as a connect link
    tok = jwt.encode({"sub": str(uuid4()), "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    r = client.get("/v1/integrations/google/connect/start", params={"t": tok}, follow_redirects=False)
    assert r.status_code == 401


def test_connect_start_forged_token_rejected():
    tok = _connect_token(uuid4(), secret="a-different-wrong-secret-at-least-32b")
    r = client.get("/v1/integrations/google/connect/start", params={"t": tok}, follow_redirects=False)
    assert r.status_code == 401


def test_connect_start_valid_token_unconfigured_503():
    # a genuine connect token, but Google isn't credentialed in the test env -> honest 503 (never a redirect)
    r = client.get("/v1/integrations/google/connect/start", params={"t": _connect_token(uuid4())}, follow_redirects=False)
    assert r.status_code == 503
