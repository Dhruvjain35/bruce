"""Provider availability must be reported HONESTLY — and never papered over with a fallback.

These tests exist because the two tempting shortcuts here are both product-destroying:
  1. turning a provider outage into an empty-but-200 intake ("Bruce read your flyer and found
     nothing") — a false completion;
  2. silently retrying on OpenAI while the submission claims a Qwen-powered workflow — a lie.
"""

from __future__ import annotations

import time
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.exceptions import ModelHTTPError

import bruce_engine.api as api
from bruce_engine import provider_status
from bruce_engine.repositories import InMemoryMissionRepository, InMemoryStore

SECRET = "test-secret"
client = TestClient(api.app)

# The exact error Qwen Cloud returns today under the account's risk-control hold.
QWEN_403 = ModelHTTPError(
    status_code=403,
    model_name="qwen3.7-plus",
    body={"error": {"code": "AccessDenied.Unpurchased", "message": "Access to model denied."}},
)


class _NoopUserRepo:
    async def ensure(self, user_id, **k):
        return None

    async def delete(self, user_id):
        return None


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", SECRET)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(api, "_mission_repo", InMemoryMissionRepository(InMemoryStore()))
    monkeypatch.setattr(api, "_user_repo", _NoopUserRepo())


def _auth(uid):
    tok = jwt.encode({"sub": str(uid), "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


# --------------------------------------------------------------------------- classification


def test_qwen_access_denied_is_classified_as_provider_unavailable():
    u = provider_status.classify(QWEN_403, provider="qwen", model="qwen3.7-plus")
    assert u is not None and u.status_code == 403
    assert "AccessDenied.Unpurchased" in u.reason and "not entitled" in u.reason


@pytest.mark.parametrize(
    "exc,expect",
    [
        (ModelHTTPError(401, "qwen3.7-plus", None), "credentials"),
        (ModelHTTPError(429, "qwen3.7-plus", None), "rate limit"),
        (ModelHTTPError(503, "qwen3.7-plus", None), "server error"),
        (httpx.ConnectError("no route"), "unreachable"),
        (RuntimeError("DASHSCOPE_API_KEY not set — load engine/.env"), "not configured"),
    ],
)
def test_provider_failures_are_classified_with_a_real_reason(exc, expect):
    u = provider_status.classify(exc, provider="qwen", model="qwen3.7-plus")
    assert u is not None and expect in u.reason


def test_our_own_bugs_are_not_disguised_as_a_provider_outage():
    """A ValueError in our parsing is OUR bug. Blaming the provider would hide a real defect."""
    assert provider_status.classify(ValueError("bad json"), provider="qwen", model="m") is None
    assert provider_status.classify(KeyError("x"), provider="qwen", model="m") is None


def test_detail_payload_carries_no_secrets_or_content():
    u = provider_status.classify(QWEN_403, provider="qwen", model="qwen3.7-plus")
    blob = str(u.as_detail()).lower()
    assert "sk-" not in blob and "authorization" not in blob and "bearer" not in blob


# --------------------------------------------------------------------------- endpoint behaviour


def test_intake_returns_503_provider_unavailable_when_qwen_is_blocked(monkeypatch):
    """The honest failure: 503 naming provider, model and cause — not a generic 502."""
    async def blocked(**kw):
        raise QWEN_403

    monkeypatch.setattr(api, "_persist_intake", blocked)
    r = client.post("/v1/intake", json={"text": "Registration closes Feb 28, 2026."},
                    headers=_auth(uuid4()))
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["error"] == "provider_unavailable"
    assert detail["provider"] == "qwen" and detail["model"] == "qwen3.7-plus"
    assert "AccessDenied.Unpurchased" in detail["reason"]


def test_intake_never_returns_an_empty_success_when_the_provider_is_down(monkeypatch):
    """The failure mode this whole module exists to prevent: a fake-empty 200."""
    async def blocked(**kw):
        raise QWEN_403

    monkeypatch.setattr(api, "_persist_intake", blocked)
    r = client.post("/v1/intake", json={"text": "x"}, headers=_auth(uuid4()))
    assert r.status_code != 200
    assert "source_id" not in r.text and "task_ids" not in r.text


def test_qwen_outage_does_not_silently_fall_back_to_another_provider(monkeypatch):
    """If Qwen is down the request FAILS. It must not be answered by OpenAI/Featherless.

    A 200 here would mean the "Qwen-powered" demonstration is answering with a different model —
    the single most dishonest thing this codebase could do.
    """
    calls: list[str] = []

    async def blocked(**kw):
        calls.append("qwen")
        raise QWEN_403

    monkeypatch.setattr(api, "_persist_intake", blocked)
    monkeypatch.setenv("BRUCE_INTAKE_PROVIDER", "qwen")
    r = client.post("/v1/intake", json={"text": "x"}, headers=_auth(uuid4()))

    assert r.status_code == 503
    assert calls == ["qwen"], f"the request was retried elsewhere: {calls}"


def test_provider_unavailable_still_requires_auth():
    """Outage handling must not become an unauthenticated information leak."""
    r = client.post("/v1/intake", json={"text": "x"})
    assert r.status_code == 401
    assert "provider" not in r.text.lower()


def test_health_is_public_and_reports_commit(monkeypatch):
    monkeypatch.setenv("BRUCE_COMMIT", "abc1234")
    monkeypatch.setenv("BRUCE_REGION", "ap-southeast-1")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["commit"] == "abc1234" and r.json()["region"] == "ap-southeast-1"
    assert r.json()["service"] == "bruce-engine"


def test_diagnostics_requires_auth():
    assert client.get("/v1/diagnostics").status_code == 401
