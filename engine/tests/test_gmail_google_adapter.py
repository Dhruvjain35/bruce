"""Real GoogleGmailAdapter (Phase G) — the LIVE Gmail I/O, proven WITHOUT credentials via a mock httpx
transport for the Gmail API + real Postgres for the marker ledger. This is the path a connected user hits;
it is not a fake. Proves: a send shapes the right request + records the ledger, a retry on the same marker
resolves to the ledger (NO second network send), reads normalize the raw Google payload, a reply is detected,
and HTTP errors map to the honest taxonomy. OAuth token exchange is stubbed (covered by test_oauth_google)."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import gmail_adapter, gmail_store, oauth_google
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _user():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    return uid


class _GmailAPI:
    """A stand-in Gmail REST endpoint that enforces just enough of the real contract."""

    def __init__(self):
        self.sends = 0
        self.sent = {}          # message_id -> stored message
        self.thread = "t_1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "POST" and p.endswith("/messages/send"):
            self.sends += 1
            body = json.loads(request.content.decode())
            raw = base64.urlsafe_b64decode(body["raw"]).decode(errors="ignore")
            assert "X-Bruce-Idempotency:" in raw and "To:" in raw       # the send is shaped as real MIME
            mid = f"m_{self.sends}"
            self.sent[mid] = {"id": mid, "threadId": self.thread, "labelIds": ["SENT"],
                              "payload": {"headers": [{"name": "To", "value": "coach@school.edu"},
                                                      {"name": "Subject", "value": "hi"},
                                                      {"name": "Message-Id", "value": f"<{mid}@mail.gmail.com>"}]}}
            return httpx.Response(200, json={"id": mid, "threadId": self.thread})
        if m == "GET" and "/messages/" in p:
            mid = p.rsplit("/", 1)[-1]
            msg = self.sent.get(mid)
            return httpx.Response(200, json=msg) if msg else httpx.Response(404, json={})
        if m == "GET" and p.endswith("/messages"):                      # recovery scan -> nothing to recover
            return httpx.Response(200, json={"messages": []})
        if m == "GET" and "/threads/" in p:
            msgs = list(self.sent.values()) + [
                {"id": "in_1", "threadId": self.thread, "labelIds": ["INBOX"],
                 "payload": {"headers": [{"name": "From", "value": "coach@school.edu"},
                                         {"name": "Subject", "value": "re: hi"},
                                         {"name": "Message-Id", "value": "<in1@school.edu>"},
                                         {"name": "In-Reply-To", "value": "<m_1@mail.gmail.com>"}]}}]
            return httpx.Response(200, json={"messages": msgs})
        return httpx.Response(404, json={})


def _adapter(uid, api):
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return gmail_adapter.real_adapter(uid, http_client=client), client


def _stub_google():
    integ = SimpleNamespace(provider_account_id="me@example.com")
    return patch.multiple(oauth_google,
                          access_token_for=lambda *a, **k: _async("tok"),
                          get_integration=lambda *a, **k: _async(integ))


async def _async(v):
    return v


def test_send_records_ledger_and_shapes_request():
    uid = _user(); api = _GmailAPI(); adapter, client = _adapter(uid, api)
    with _stub_google():
        ref = _run(adapter.send(to="coach@school.edu", subject="hi", body="hello", thread_id=None, marker="mk1"))
    _run(client.aclose())
    assert api.sends == 1 and ref.message_id == "m_1" and ref.already_sent is False
    assert _run(gmail_store.get_by_marker(uid, "mk1"))["message_id"] == "m_1"    # ledger persisted


def test_retry_same_marker_no_second_network_send():
    uid = _user(); api = _GmailAPI(); adapter, client = _adapter(uid, api)
    with _stub_google():
        r1 = _run(adapter.send(to="coach@school.edu", subject="hi", body="hello", thread_id=None, marker="mk2"))
        r2 = _run(adapter.send(to="coach@school.edu", subject="hi", body="hello", thread_id=None, marker="mk2"))
    _run(client.aclose())
    assert api.sends == 1                                    # the retry hit the ledger, not the network
    assert r1.message_id == r2.message_id and r2.already_sent is True


def test_send_and_verify_end_to_end_on_real_adapter():
    uid = _user(); api = _GmailAPI(); adapter, client = _adapter(uid, api)
    with _stub_google():
        res = _run(gmail_adapter.send_and_verify(adapter, uid, to="coach@school.edu", subject="hi",
                                                 body="hello", idempotency_key="req-x"))
    _run(client.aclose())
    assert res.verified is True and res.outcome.value == "ok"


def test_reads_normalize_and_detect_reply():
    uid = _user(); api = _GmailAPI(); adapter, client = _adapter(uid, api)
    with _stub_google():
        _run(adapter.send(to="coach@school.edu", subject="hi", body="hello", thread_id=None, marker="mk3"))
        got = _run(adapter.get("m_1"))
        reply = _run(adapter.find_reply(api.thread, after_message_id="m_1"))
    _run(client.aclose())
    assert got["to"] == "coach@school.edu" and "SENT" in got["labelIds"]
    assert reply is not None and reply["id"] == "in_1" and "SENT" not in reply["labelIds"]


def test_http_error_maps_to_taxonomy():
    uid = _user()
    def _403(_req): return httpx.Response(403, json={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(_403))
    adapter = gmail_adapter.real_adapter(uid, http_client=client)
    with _stub_google():
        with pytest.raises(gmail_adapter.InsufficientScope):
            _run(adapter.send(to="a@b.com", subject="x", body="y", thread_id=None, marker="mk4"))
    _run(client.aclose())
