"""Auth boundary tests — user identity comes from a verified JWT only (no network)."""

import time
from uuid import uuid4

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from bruce_engine.auth import AuthenticatedUser, current_user

SECRET = "test-secret"

app = FastAPI()


@app.get("/me")
async def me(user: AuthenticatedUser = Depends(current_user)):
    return {"user_id": str(user.user_id)}


client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", SECRET)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)


def _tok(sub, *, exp_delta=3600, secret=SECRET, **extra):
    payload = {"sub": str(sub), "exp": int(time.time()) + exp_delta, **extra}
    return jwt.encode(payload, secret, algorithm="HS256")


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_valid_token_yields_user_id_from_sub():
    uid = uuid4()
    r = client.get("/me", headers=_auth(_tok(uid)))
    assert r.status_code == 200
    assert r.json()["user_id"] == str(uid)


def test_missing_token_401():
    assert client.get("/me").status_code == 401


def test_expired_token_401():
    assert client.get("/me", headers=_auth(_tok(uuid4(), exp_delta=-10))).status_code == 401


def test_bad_signature_401():
    assert client.get("/me", headers=_auth(_tok(uuid4(), secret="wrong-secret"))).status_code == 401


def test_missing_sub_401():
    tok = jwt.encode({"exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    assert client.get("/me", headers=_auth(tok)).status_code == 401


def test_non_uuid_sub_401():
    assert client.get("/me", headers=_auth(_tok("not-a-uuid"))).status_code == 401


def test_no_verification_configured_fails_closed(monkeypatch):
    monkeypatch.delenv("BRUCE_JWT_SECRET", raising=False)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    # even a well-formed token must be rejected when no verification is configured
    assert client.get("/me", headers=_auth(_tok(uuid4()))).status_code == 401
