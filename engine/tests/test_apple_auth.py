"""Sign in with Apple -> Bruce JWT: token verification, user derivation, and the exchange endpoint.

Offline: a throwaway RSA key stands in for Apple's signing key (injected via key_resolver), so we
verify the real jwt.decode path — signature, issuer, audience, expiry, nonce — without touching
Apple's network. Endpoint tests use an in-memory user repo (no Postgres) and assert the minted token
carries the DERIVED subject, never anything the client supplied.
"""

from __future__ import annotations

import datetime
import hashlib
import time
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import bruce_engine.api as api
from bruce_engine import apple_auth, auth
from bruce_engine.apple_auth import AppleAuthError, derive_user_id, verify_apple_token

AUD = "com.brucedev.Bruce"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _nonce_pair() -> tuple[str, str]:
    raw = "random-one-time-value-123"
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _apple_token(*, sub="000123.abcdef", email="s@icloud.com", aud=AUD,
                 iss=apple_auth.APPLE_ISSUER, nonce_hash=None, exp_delta=600, key=_KEY) -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + exp_delta}
    if email is not None:
        claims["email"] = email
    if nonce_hash is not None:
        claims["nonce"] = nonce_hash
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "testkid"})


def _resolver(key=_KEY):
    return lambda _token: key.public_key()


# --------------------------------------------------------------------------- verifier


def test_valid_token_yields_identity():
    raw, h = _nonce_pair()
    ident = verify_apple_token(_apple_token(nonce_hash=h), raw, audiences=[AUD], key_resolver=_resolver())
    assert ident.apple_sub == "000123.abcdef"
    assert ident.email == "s@icloud.com"
    assert ident.bruce_user_id == derive_user_id("000123.abcdef")


def test_bad_signature_is_rejected():
    raw, h = _nonce_pair()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AppleAuthError):
        verify_apple_token(_apple_token(nonce_hash=h), raw, audiences=[AUD], key_resolver=_resolver(other))


def test_wrong_issuer_is_rejected():
    raw, h = _nonce_pair()
    tok = _apple_token(nonce_hash=h, iss="https://evil.example.com")
    with pytest.raises(AppleAuthError, match="issuer"):
        verify_apple_token(tok, raw, audiences=[AUD], key_resolver=_resolver())


def test_wrong_audience_is_rejected():
    raw, h = _nonce_pair()
    tok = _apple_token(nonce_hash=h, aud="com.someone.else")
    with pytest.raises(AppleAuthError, match="audience"):
        verify_apple_token(tok, raw, audiences=[AUD], key_resolver=_resolver())


def test_expired_token_is_rejected():
    raw, h = _nonce_pair()
    tok = _apple_token(nonce_hash=h, exp_delta=-10)
    with pytest.raises(AppleAuthError, match="expired"):
        verify_apple_token(tok, raw, audiences=[AUD], key_resolver=_resolver())


def test_nonce_mismatch_is_rejected():
    raw, _ = _nonce_pair()
    tok = _apple_token(nonce_hash=hashlib.sha256(b"a different nonce").hexdigest())
    with pytest.raises(AppleAuthError, match="nonce"):
        verify_apple_token(tok, raw, audiences=[AUD], key_resolver=_resolver())


def test_missing_nonce_claim_is_rejected():
    raw, _ = _nonce_pair()
    with pytest.raises(AppleAuthError, match="nonce"):
        verify_apple_token(_apple_token(nonce_hash=None), raw, audiences=[AUD], key_resolver=_resolver())


def test_missing_email_is_allowed_on_returning_signin():
    raw, h = _nonce_pair()
    ident = verify_apple_token(_apple_token(email=None, nonce_hash=h), raw, audiences=[AUD], key_resolver=_resolver())
    assert ident.email is None and ident.apple_sub == "000123.abcdef"


def test_same_apple_sub_maps_to_same_bruce_id_different_subs_differ():
    assert derive_user_id("aaa") == derive_user_id("aaa")
    assert derive_user_id("aaa") != derive_user_id("bbb")


# --------------------------------------------------------------------------- exchange endpoint


class _FakeUserRepo:
    def __init__(self): self.ensured: list[tuple[UUID, str, str | None]] = []
    async def ensure(self, user_id, *, auth_provider="supabase", email=None):
        self.ensured.append((user_id, auth_provider, email))
    async def delete(self, user_id): ...


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long!!")
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    monkeypatch.setenv("BRUCE_APPLE_CLIENT_ID", AUD)
    repo = _FakeUserRepo()
    monkeypatch.setattr(api, "_user_repo", repo)
    return TestClient(api.app), repo


def _decode(token: str) -> dict:
    return jwt.decode(token, "test-secret-that-is-at-least-32-bytes-long!!", algorithms=["HS256"])


def test_first_sign_in_creates_user_and_mints_token_for_derived_subject(client, monkeypatch):
    c, repo = client
    ident = apple_auth.AppleIdentity(apple_sub="000999.zzz", email="new@icloud.com", bruce_user_id=derive_user_id("000999.zzz"))
    monkeypatch.setattr(api.apple_auth, "verify_apple_token", lambda *a, **k: ident)

    r = c.post("/v1/auth/apple", json={"identity_token": "x", "raw_nonce": "y"})
    assert r.status_code == 200
    body = r.json()
    assert UUID(body["user_id"]) == derive_user_id("000999.zzz")
    # The minted token's subject is the DERIVED id — not client input.
    assert _decode(body["token"])["sub"] == str(derive_user_id("000999.zzz"))
    assert _decode(body["token"])["iss"] == "apple"
    # User upserted with the derived id + email (first sign-in).
    assert repo.ensured == [(derive_user_id("000999.zzz"), "apple", "new@icloud.com")]


def test_returning_sign_in_is_same_user_and_email_not_required(client, monkeypatch):
    c, repo = client
    ident = apple_auth.AppleIdentity(apple_sub="000999.zzz", email=None, bruce_user_id=derive_user_id("000999.zzz"))
    monkeypatch.setattr(api.apple_auth, "verify_apple_token", lambda *a, **k: ident)

    a = c.post("/v1/auth/apple", json={"identity_token": "x", "raw_nonce": "y"}).json()
    b = c.post("/v1/auth/apple", json={"identity_token": "x2", "raw_nonce": "y2"}).json()  # duplicate/retry
    assert a["user_id"] == b["user_id"]                      # same Apple sub -> same user
    assert repo.ensured[-1] == (derive_user_id("000999.zzz"), "apple", None)  # email not required


def test_invalid_apple_token_is_401_with_no_leak(client, monkeypatch):
    c, _ = client
    def boom(*a, **k): raise AppleAuthError("nonce mismatch")
    monkeypatch.setattr(api.apple_auth, "verify_apple_token", boom)
    r = c.post("/v1/auth/apple", json={"identity_token": "x", "raw_nonce": "y"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "apple_auth_failed"


def test_two_apple_accounts_get_isolated_subjects(client, monkeypatch):
    c, _ = client
    def verify(tok, *a, **k):
        sub = "acct.A" if tok == "A" else "acct.B"
        return apple_auth.AppleIdentity(apple_sub=sub, email=None, bruce_user_id=derive_user_id(sub))
    monkeypatch.setattr(api.apple_auth, "verify_apple_token", verify)
    ta = _decode(c.post("/v1/auth/apple", json={"identity_token": "A", "raw_nonce": "n"}).json()["token"])["sub"]
    tb = _decode(c.post("/v1/auth/apple", json={"identity_token": "B", "raw_nonce": "n"}).json()["token"])["sub"]
    assert ta != tb   # different Apple accounts -> different Bruce subjects -> RLS keeps them apart


def test_minting_requires_the_signing_secret(monkeypatch):
    monkeypatch.delenv("BRUCE_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="BRUCE_JWT_SECRET"):
        auth.mint_bruce_jwt(uuid4())
