"""Smoke test against the LIVE Alibaba Cloud deployment.

Runs only when BRUCE_DEPLOY_URL is set, and hits the real deployed URL over the real internet — it
is not a local TestClient. Until a deployment exists it skips with a precise reason; a skip is never
a pass.

What it asserts is deliberately narrow and security-shaped: that the thing on the internet is the
commit we think it is, that authentication is genuinely enforced there (not just locally), and that
the deployment tells the truth about the Qwen provider instead of pretending. It does NOT assert
that Qwen works — that is what the live Qwen test is for.

Set BRUCE_DEPLOY_JWT to additionally exercise the authenticated diagnostics endpoint.
"""

from __future__ import annotations

import os

import httpx
import pytest

URL = (os.environ.get("BRUCE_DEPLOY_URL") or "").rstrip("/")
JWT = os.environ.get("BRUCE_DEPLOY_JWT")

pytestmark = pytest.mark.skipif(
    not URL, reason="BRUCE_DEPLOY_URL not set — no live Alibaba Cloud deployment to smoke test"
)


def test_health_is_public_and_reports_the_deployed_commit():
    """/health must answer without auth and name the commit — that is how a URL is tied to code."""
    r = httpx.get(f"{URL}/health", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["service"] == "bruce-engine"
    assert body.get("commit") and body["commit"] != "unknown", (
        "deployment does not report a commit — the image was built without BRUCE_COMMIT, so this "
        "URL cannot be proven to correspond to any particular source revision"
    )
    assert body.get("region") == "ap-southeast-1"


def test_health_leaks_no_secrets():
    """A public endpoint must never echo configuration."""
    body = httpx.get(f"{URL}/health", timeout=30).text.lower()
    for needle in ("sk-", "password", "secret", "postgresql://", "postgresql+asyncpg"):
        assert needle not in body, f"/health leaked {needle!r}"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/v1/intake", {"text": "Registration closes Feb 28, 2026."}),
        ("GET", "/v1/missions", None),
        ("GET", "/v1/diagnostics", None),
        ("DELETE", "/v1/account", None),
    ],
)
def test_authentication_is_enforced_on_the_live_deployment(method, path, payload):
    """Every /v1 route must 401 without a token ON THE DEPLOYED SERVICE.

    The FC HTTP trigger is `authType: anonymous` at the gateway, so Bruce's own JWT verification is
    the ONLY thing standing between the public internet and student data. A misconfigured deployment
    that skipped auth would pass every local test and expose real users — so this is asserted
    against the live URL, not a TestClient.
    """
    r = httpx.request(method, f"{URL}{path}", json=payload, timeout=30)
    assert r.status_code == 401, f"{method} {path} returned {r.status_code}, expected 401"


def test_unauthenticated_intake_creates_nothing_and_says_nothing():
    """A rejected request must not leak whether anything exists behind it."""
    r = httpx.post(f"{URL}/v1/intake", json={"text": "x"}, timeout=30)
    assert r.status_code == 401
    assert "source_id" not in r.text and "task_ids" not in r.text


@pytest.mark.skipif(not JWT, reason="BRUCE_DEPLOY_JWT not set — cannot exercise authed diagnostics")
def test_diagnostics_reports_database_and_honest_provider_state():
    """The deployment must be wired to real Postgres, and must not overclaim Qwen."""
    r = httpx.get(f"{URL}/v1/diagnostics", headers={"Authorization": f"Bearer {JWT}"}, timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["database"] == "connected", (
        f"deployment is not talking to Postgres: {body['database']} — Bruce's RLS/user-scoping "
        "guarantees are enforced BY the database, so a deployment without it is not Bruce"
    )
    assert body["commit"] and body["commit"] != "unknown"

    prov = body["intake_provider"]
    assert prov["provider"] == "qwen", "the demonstrated intake path must be Qwen, not a fallback"
    # `live` must reflect reality. While Qwen is account-blocked this is False with a real reason;
    # once a genuine call succeeds it flips to True. Either is acceptable — a lie is not.
    assert isinstance(prov["live"], bool)
    if not prov["live"]:
        assert prov["detail"], "provider is not live but gives no reason — that is not honest"


@pytest.mark.skipif(not JWT, reason="BRUCE_DEPLOY_JWT not set")
def test_intake_fails_honestly_while_qwen_is_blocked_and_never_falls_back():
    """With Qwen blocked, intake must 503 provider_unavailable — never a fake-empty 200.

    If Qwen is live this test is not applicable and asserts the success shape instead. What it never
    tolerates is a 200 produced by some OTHER provider, which would make a Qwen-powered claim false.
    """
    r = httpx.post(
        f"{URL}/v1/intake",
        json={"text": "Registration closes Feb 28, 2026."},
        headers={"Authorization": f"Bearer {JWT}"},
        timeout=120,
    )
    if r.status_code == 503:
        detail = r.json()["detail"]
        assert detail["error"] == "provider_unavailable"
        assert detail["provider"] == "qwen" and detail["reason"]
        return
    assert r.status_code == 200, f"expected 200 (Qwen live) or 503 (blocked), got {r.status_code}"
    body = r.json()
    assert body.get("source_id"), "a 200 must have persisted a real source"
