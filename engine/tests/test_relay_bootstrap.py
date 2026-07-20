"""Bite 1.5 A4 gap 1 — secure device-registration bootstrap (server side), against REAL Postgres.

The installer registers the relay device over a short-lived, single-use, operator-minted bootstrap token
and the permanent credential moves straight into the Keychain — never shown. These tests prove the
device-registration THREAT MODEL: authenticated operator mint, single-use + expiry + env/device binding,
replay fails, rate limiting, rotation revokes the previous credential, idempotent reinstall, audit
without secrets, and identity-from-credential. HTTP surface covered via TestClient.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import relay_auth, schema
from bruce_engine.db import worker_session

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


ENV = "local"   # tests run with BRUCE_ENV unset -> current_environment() == "local"


def _mint(device="mac-alpha", ttl=600, env=ENV):
    return _run(relay_auth.mint_bootstrap_token(device, environment=env, ttl_seconds=ttl, actor="op@host"))


# --------------------------------------------------------------------------- happy path + identity


def test_register_returns_credential_and_identity_is_derived_from_it(clean_db):
    token = _mint("mac-alpha")
    dev_id, secret = _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))
    # the returned credential authenticates AS that device (identity derived from the credential)
    dev = _run(relay_auth.authenticate(secret))
    assert str(dev.id) == str(dev_id) and dev.name == "mac-alpha"


# --------------------------------------------------------------------------- single-use / replay / expiry


def test_bootstrap_token_is_single_use(clean_db):
    token = _mint("mac-alpha")
    _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))
    with pytest.raises(relay_auth.BootstrapError):
        _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))   # replay


def test_expired_token_is_rejected(clean_db):
    token = _mint("mac-alpha", ttl=-1)   # already expired
    with pytest.raises(relay_auth.BootstrapError):
        _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))


def test_unknown_token_is_rejected(clean_db):
    with pytest.raises(relay_auth.BootstrapError):
        _run(relay_auth.register_with_bootstrap("not-a-real-token", device_name="mac-alpha", environment=ENV))


# --------------------------------------------------------------------------- env / device binding


def test_env_mismatch_rejected(clean_db):
    token = _mint("mac-alpha", env="staging")
    with pytest.raises(relay_auth.BootstrapError):
        _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))   # env=local


def test_device_mismatch_rejected(clean_db):
    token = _mint("mac-alpha")
    with pytest.raises(relay_auth.BootstrapError):
        _run(relay_auth.register_with_bootstrap(token, device_name="mac-OTHER", environment=ENV))


# --------------------------------------------------------------------------- rotation / idempotent reinstall


def test_reinstall_rotates_and_revokes_previous_credential(clean_db):
    dev1, secret1 = _run(relay_auth.register_with_bootstrap(_mint("mac-alpha"), device_name="mac-alpha", environment=ENV))
    dev2, secret2 = _run(relay_auth.register_with_bootstrap(_mint("mac-alpha"), device_name="mac-alpha", environment=ENV))
    assert str(dev1) == str(dev2)                       # SAME device (idempotent reinstall, no duplicate)
    assert secret2 != secret1
    assert _run(relay_auth.authenticate(secret2)).name == "mac-alpha"   # new credential works
    with pytest.raises(relay_auth.RelayAuthError):
        _run(relay_auth.authenticate(secret1))         # old credential no longer authenticates (rotated)

    async def _count():
        async with worker_session() as s:
            return (await s.execute(select(func.count()).select_from(schema.RelayDevice).where(
                schema.RelayDevice.name == "mac-alpha"))).scalar_one()
    assert _run(_count()) == 1                          # not silently rebound into a second device row


# --------------------------------------------------------------------------- rate limiting


def test_registration_is_rate_limited(clean_db):
    # exceed the per-env window with denied attempts (bad tokens still count), then a good token is refused
    for _ in range(relay_auth._REGISTER_MAX_PER_WINDOW):
        with pytest.raises(relay_auth.BootstrapError):
            _run(relay_auth.register_with_bootstrap("bad", device_name="mac-alpha", environment=ENV))
    with pytest.raises(relay_auth.BootstrapError) as ei:
        _run(relay_auth.register_with_bootstrap(_mint("mac-alpha"), device_name="mac-alpha", environment=ENV))
    assert "rate limit" in str(ei.value)


# --------------------------------------------------------------------------- audit has no secret


def test_registration_audit_records_without_secrets(clean_db):
    token = _mint("mac-alpha")
    _, secret = _run(relay_auth.register_with_bootstrap(token, device_name="mac-alpha", environment=ENV))

    async def _audit():
        async with worker_session() as s:
            return (await s.execute(select(schema.RelayRegistrationAudit).order_by(
                schema.RelayRegistrationAudit.created_at))).scalars().all()
    rows = _run(_audit())
    actions = {r.action for r in rows}
    assert "mint" in actions and "register" in actions
    for r in rows:
        assert r.result in ("ok", "denied") and r.environment == ENV
        blob = f"{r.actor}|{r.reason}|{r.device_name}"
        assert secret not in blob and token not in blob     # never a secret / token in the audit


# --------------------------------------------------------------------------- HTTP surface


def _reg_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_http_register_then_self_revoke(clean_db):
    token = _mint("mac-alpha")
    r = client.post("/v1/relay/register", headers=_reg_headers(token), json={"device_name": "mac-alpha"})
    assert r.status_code == 200 and r.json()["secret"]
    secret = r.json()["secret"]
    # the device can revoke ITSELF (installer cleanup path) -> no active orphan
    hdr = {"Authorization": f"Bearer {secret}", "X-Bruce-Timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    assert client.post("/v1/relay/self-revoke", headers=hdr).status_code == 200
    with pytest.raises(relay_auth.RelayAuthError):
        _run(relay_auth.authenticate(secret))              # revoked


def test_http_register_missing_token_is_401(clean_db):
    assert client.post("/v1/relay/register", json={"device_name": "mac-alpha"}).status_code == 401


def test_http_register_replay_is_403_without_secret(clean_db):
    token = _mint("mac-alpha")
    client.post("/v1/relay/register", headers=_reg_headers(token), json={"device_name": "mac-alpha"})
    r = client.post("/v1/relay/register", headers=_reg_headers(token), json={"device_name": "mac-alpha"})
    assert r.status_code == 403 and "secret" not in r.json()   # denied, no credential leaked
