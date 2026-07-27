"""Gmail adapter (Phase G foundation) — the FakeGmailAdapter enforces the provider's real rules so the
execute-once send + fetch-back verification runs without network/OAuth: a retry with the same idempotency
marker never double-sends, a send is 'verified' ONLY when the read-back shows it in SENT to the intended
recipient with the intended subject, and a reply in the thread is detectable. No Gmail-specific routing here."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from bruce_engine import gmail_adapter as ga
from bruce_engine.runtime_contracts import ToolOutcome

ACCOUNT = "me@example.com"


def _run(c):
    return asyncio.run(c)


def test_send_is_idempotent_no_double_send():
    adapter = ga.FakeGmailAdapter(account=ACCOUNT)
    uid = uuid4()
    r1 = _run(ga.send_and_verify(adapter, uid, to="coach@school.edu", subject="hi", body="test",
                                 idempotency_key="run1:step0"))
    r2 = _run(ga.send_and_verify(adapter, uid, to="coach@school.edu", subject="hi", body="test",
                                 idempotency_key="run1:step0"))   # a RETRY with the same key
    assert r1.verified and r2.verified
    assert r1.provider_entity_id == r2.provider_entity_id         # same message — resolved to the existing send
    assert adapter.send_calls == 2 and len(adapter.messages) == 1  # send() called twice, ONE message exists


def test_send_is_verified_by_read_back():
    adapter = ga.FakeGmailAdapter(account=ACCOUNT)
    r = _run(ga.send_and_verify(adapter, uuid4(), to="coach@school.edu", subject="bruce gmail is live",
                                body="hello", idempotency_key="k1"))
    assert r.outcome is ToolOutcome.ok and r.verified is True
    assert r.read_back["to"] == "coach@school.edu" and "SENT" in r.read_back["labelIds"]


def test_verification_fails_closed_on_mismatch():
    # matches_sent is the authority — a read-back missing SENT / wrong recipient / wrong subject is NOT verified
    assert ga.matches_sent(None, to="a@b.com", subject="x")[0] is False
    assert ga.matches_sent({"labelIds": ["DRAFT"], "to": "a@b.com", "subject": "x"}, to="a@b.com", subject="x")[0] is False
    assert ga.matches_sent({"labelIds": ["SENT"], "to": "other@b.com", "subject": "x"}, to="a@b.com", subject="x")[0] is False
    assert ga.matches_sent({"labelIds": ["SENT"], "to": "a@b.com", "subject": "wrong"}, to="a@b.com", subject="x")[0] is False
    assert ga.matches_sent({"labelIds": ["SENT"], "to": "a@b.com", "subject": "x here"}, to="a@b.com", subject="x")[0] is True


def test_reply_detection_in_thread():
    adapter = ga.FakeGmailAdapter(account=ACCOUNT)
    r = _run(ga.send_and_verify(adapter, uuid4(), to="me@personal.com", subject="ping",
                                body="reply when you can", idempotency_key="k2"))
    tid = r.read_back["threadId"]
    assert _run(adapter.find_reply(tid, after_message_id=r.provider_entity_id)) is None   # no reply yet
    adapter.inject_incoming(tid, from_addr="me@personal.com", subject="re: ping",
                            body="here's my reply", in_reply_to=adapter.rfc_of(r.provider_entity_id))
    reply = _run(adapter.find_reply(tid, after_message_id=r.provider_entity_id))
    assert reply is not None and "SENT" not in reply["labelIds"]   # an inbound reply, not our own send


def test_deterministic_marker_is_stable_and_keyed():
    uid = uuid4()
    assert ga.deterministic_marker(uid, "run:step0") == ga.deterministic_marker(uid, "run:step0")
    assert ga.deterministic_marker(uid, "run:step0") != ga.deterministic_marker(uid, "run:step1")


# --- execution boundary -------------------------------------------------------------------------------
# THIS FILE DOES NOT EXERCISE AUTHORIZATION. Every test above is about provider semantics — read-back
# verification, 409 handling, idempotent retry, account binding, run bookkeeping — and calls the verified
# I/O directly rather than through the turn that would mint consent for it. Suspending the gate keeps
# those assertions about the thing they are actually testing.
#
# The boundary itself is proven in test_authorization_evidence.py and test_authorization_zero_call.py,
# which never import this seam. `unchecked_provider_writes_for_test` raises outside pytest, so this is a
# statement about a test file, not a hole in the engine.
import pytest as _pytest_for_boundary_fixture


@_pytest_for_boundary_fixture.fixture(autouse=True)
def _provider_semantics_not_authorization():
    from bruce_engine import execution_gate
    with execution_gate.unchecked_provider_writes_for_test():
        yield
