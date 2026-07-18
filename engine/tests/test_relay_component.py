"""Integration tests for the macOS relay (engine/relay) using an in-process fake imsg + fake backend.

These exercise the RELAY's transport responsibilities with no subprocess and no network, so they run
in the offline suite. Each test maps to a Step-6 scenario. Backend-side guarantees (outbound lease
expiry, duplicate claim, replay rejection, cross-user isolation, account deletion, no false
completion) are enforced and tested server-side — see test_relay_io.py / test_messaging_*.py /
test_relay_upload.py; here we prove the relay never loses, double-sends, leaks content, or acks
before the underlying action actually succeeded.

LIVE iMessage remains UNVERIFIED until the dedicated-Mac dry-run passes; these fakes stand in for
real Messages + the real Bruce API.
"""

from __future__ import annotations

import asyncio

import pytest

from relay.backend import AuthError, BackendError
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.relay import Relay

# 1x1 PNG (valid magic, tiny) — stands in for a screenshot attachment.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class FakeBackend:
    """In-memory Bruce API. Records what the relay forwards; can be told to fail/auth-reject."""

    def __init__(self) -> None:
        self.inbound: list[dict] = []
        self.uploads: list[tuple[str, int, str | None]] = []
        self.acks: list[dict] = []
        self.claims: list[dict] = []      # queued outbound jobs to hand out
        self.fail_inbound = 0             # first N post_inbound calls raise BackendError
        self.fail_upload = 0              # first N upload calls raise BackendError
        self.auth_fail = False
        self._ref = 0

    async def post_inbound(self, event: dict) -> dict:
        if self.auth_fail:
            raise AuthError("revoked")
        if self.fail_inbound > 0:
            self.fail_inbound -= 1
            raise BackendError("5xx")
        self.inbound.append(event)
        return {"status": "processed", "mission_id": f"m{len(self.inbound)}"}

    async def upload(self, data: bytes, media_type: str, filename: str | None) -> str:
        if self.fail_upload > 0:
            self.fail_upload -= 1
            raise BackendError("upload 5xx")
        self._ref += 1
        self.uploads.append((media_type, len(data), filename))
        return f"upl-{self._ref}"

    async def claim(self) -> dict | None:
        return self.claims.pop(0) if self.claims else None

    async def ack(self, outbound_id, status, provider_message_id, error) -> None:
        self.acks.append({"id": outbound_id, "status": status, "guid": provider_message_id, "error": error})

    async def heartbeat(self) -> dict:
        return {"ok": True}


def _relay(tmp_path, imsg, backend):
    return Relay(
        imsg=imsg, backend=backend,
        checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
        spool_dir=str(tmp_path / "spool"),
        poll_interval=0.01, reconnect_delay=0.01,
    )


def _run(coro):
    return asyncio.run(coro)


# --- inbound: text ---------------------------------------------------------------------------------

def test_inbound_direct_text(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    status = _run(r.process_inbound_dict({
        "guid": "g1", "sender": "+15551234567", "is_group": False, "text": "can you help with this?",
    }))
    assert status == "processed"
    assert len(be.inbound) == 1
    ev = be.inbound[0]
    assert ev["provider_message_id"] == "g1"
    assert ev["channel_identity"] == "+15551234567"
    assert ev["text"] == "can you help with this?"
    assert ev["attachments"] == []
    assert r.checkpoint.has("g1")            # checkpointed only after backend ack


def test_inbound_group_text_replies_to_chat(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({
        "guid": "g2", "sender": "+1555", "chat_guid": "chat;+;grp", "is_group": True, "text": "hi",
    }))
    ev = be.inbound[0]
    assert ev["is_group"] is True
    assert ev["chat_guid"] == "chat;+;grp"   # server replies to the chat, not the individual


def test_inbound_url_is_plain_text(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "g3", "sender": "+1555", "text": "https://example.edu/flyer"}))
    assert be.inbound[0]["text"] == "https://example.edu/flyer"
    assert be.inbound[0]["attachments"] == []


# --- inbound: attachments --------------------------------------------------------------------------

def test_inbound_screenshot_uploaded(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    _run(r.process_inbound_dict({
        "guid": "g4", "sender": "+1555", "text": None,
        "attachments": [{"original_path": str(img), "mime_type": "image/png", "missing": False}],
    }))
    assert len(be.uploads) == 1 and be.uploads[0][0] == "image/png"
    atts = be.inbound[0]["attachments"]
    assert atts == [{"kind": "image", "media_type": "image/png", "upload_ref": "upl-1"}]
    # content-free: the local file path must NOT appear anywhere in the forwarded event
    assert str(img) not in str(be.inbound[0])
    # spool copy cleaned up
    assert not any((tmp_path / "spool").glob("*"))


def test_inbound_pdf_uploaded(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF)
    _run(r.process_inbound_dict({
        "guid": "g5", "sender": "+1555",
        "attachments": [{"original_path": str(pdf), "mime_type": "application/pdf", "missing": False}],
    }))
    assert be.inbound[0]["attachments"][0]["kind"] == "pdf"


def test_delayed_attachment_deferred_then_delivered(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    img = tmp_path / "late.png"
    img.write_bytes(PNG)
    # First delivery: still downloading -> defer, do NOT post or checkpoint.
    status = _run(r.process_inbound_dict({
        "guid": "g6", "sender": "+1555",
        "attachments": [{"original_path": str(img), "mime_type": "image/png", "missing": True}],
    }))
    assert status == "deferred"
    assert be.inbound == [] and not r.checkpoint.has("g6")
    # Retry once the file has landed -> now delivered.
    status = _run(r.process_inbound_dict({
        "guid": "g6", "sender": "+1555",
        "attachments": [{"original_path": str(img), "mime_type": "image/png", "missing": False}],
    }))
    assert status == "processed" and len(be.inbound) == 1 and r.checkpoint.has("g6")


def test_executable_attachment_rejected_client_side(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    evil = tmp_path / "payload.png"          # PNG mime but Mach-O magic
    evil.write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 64)
    _run(r.process_inbound_dict({
        "guid": "g7", "sender": "+1555",
        "attachments": [{"original_path": str(evil), "mime_type": "image/png", "missing": False}],
    }))
    assert be.uploads == []                  # never uploaded
    assert be.inbound[0]["attachments"] == []


def test_oversized_and_unsupported_attachments_skipped(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * (16 * 1024 * 1024))
    weird = tmp_path / "clip.mov"
    weird.write_bytes(b"\x00\x00\x00\x18ftypqt")
    _run(r.process_inbound_dict({
        "guid": "g8", "sender": "+1555", "text": "see attached",
        "attachments": [
            {"original_path": str(big), "mime_type": "image/png", "missing": False},
            {"original_path": str(weird), "mime_type": "video/quicktime", "missing": False},
        ],
    }))
    assert be.uploads == []                  # oversized skipped, video mime not allowed
    assert be.inbound[0]["attachments"] == []
    assert be.inbound[0]["text"] == "see attached"   # message still delivered


# --- idempotency / durability ----------------------------------------------------------------------

def test_duplicate_message_processed_once(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    payload = {"guid": "dup", "sender": "+1555", "text": "hello"}
    assert _run(r.process_inbound_dict(payload)) == "processed"
    assert _run(r.process_inbound_dict(payload)) == "duplicate"
    assert len(be.inbound) == 1              # backend hit exactly once


def test_outbound_echo_ignored(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    status = _run(r.process_inbound_dict({"guid": "e1", "is_from_me": True, "text": "Got it"}))
    assert status == "echo"
    assert be.inbound == [] and not r.checkpoint.has("e1")


def test_backend_outage_not_checkpointed_then_retry_succeeds(tmp_path):
    be = FakeBackend()
    be.fail_inbound = 1                       # first post_inbound 5xx
    r = _relay(tmp_path, InProcessImsg(), be)
    payload = {"guid": "o1", "sender": "+1555", "text": "hi"}
    assert _run(r.process_inbound_dict(payload)) == "retry"
    assert not r.checkpoint.has("o1")         # NOT acked -> will be retried
    assert _run(r.process_inbound_dict(payload)) == "processed"
    assert len(be.inbound) == 1 and r.checkpoint.has("o1")


def test_upload_outage_defers_whole_message(tmp_path):
    be = FakeBackend()
    be.fail_upload = 1                        # upload fails -> whole message retried, nothing posted
    r = _relay(tmp_path, InProcessImsg(), be)
    img = tmp_path / "s.png"
    img.write_bytes(PNG)
    payload = {"guid": "u1", "sender": "+1555",
               "attachments": [{"original_path": str(img), "mime_type": "image/png", "missing": False}]}
    assert _run(r.process_inbound_dict(payload)) == "retry"
    assert be.inbound == [] and not r.checkpoint.has("u1")
    assert not any((tmp_path / "spool").glob("*"))    # spool cleaned even on failure
    assert _run(r.process_inbound_dict(payload)) == "processed"
    assert r.checkpoint.has("u1")


def test_relay_restart_skips_acked_message(tmp_path):
    be = FakeBackend()
    cp = str(tmp_path / "cp.json")
    r1 = Relay(imsg=InProcessImsg(), backend=be, checkpoint=FileCheckpoint(cp),
               spool_dir=str(tmp_path / "spool"), poll_interval=0.01)
    _run(r1.process_inbound_dict({"guid": "r1", "sender": "+1555", "text": "hi"}))
    assert len(be.inbound) == 1
    # Simulate a full relay/Mac restart: brand-new objects, same checkpoint file on disk.
    r2 = Relay(imsg=InProcessImsg(), backend=be, checkpoint=FileCheckpoint(cp),
               spool_dir=str(tmp_path / "spool"), poll_interval=0.01)
    assert _run(r2.process_inbound_dict({"guid": "r1", "sender": "+1555", "text": "hi"})) == "duplicate"
    assert len(be.inbound) == 1              # not re-forwarded after restart


# --- watch loop: reconnect + credential ------------------------------------------------------------

def test_watch_processes_stream_and_reconnects(tmp_path):
    be = FakeBackend()
    imsg = InProcessImsg([
        {"guid": "w1", "sender": "+1555", "text": "one"},
        {"guid": "w2", "sender": "+1555", "text": "two"},
    ])
    r = _relay(tmp_path, imsg, be)

    async def drive():
        task = asyncio.create_task(r.run_inbound())
        # let the stream drain + a reconnect cycle happen, then stop
        for _ in range(50):
            await asyncio.sleep(0.005)
            if len(be.inbound) >= 2:
                break
        r.stop()
        await asyncio.wait_for(task, timeout=1.0)

    _run(drive())
    assert {e["provider_message_id"] for e in be.inbound} == {"w1", "w2"}


def test_revoked_credential_stops_relay(tmp_path):
    be = FakeBackend()
    be.auth_fail = True
    imsg = InProcessImsg([{"guid": "x1", "sender": "+1555", "text": "hi"}])
    r = _relay(tmp_path, imsg, be)

    async def drive():
        await asyncio.wait_for(r.run_inbound(), timeout=1.0)   # AuthError -> stop() -> returns

    _run(drive())
    assert r._stop.is_set()
    assert be.inbound == []                  # nothing forwarded under a revoked credential


# --- outbound --------------------------------------------------------------------------------------

def test_outbound_ack_only_after_send(tmp_path):
    be = FakeBackend()
    be.claims = [{"id": "job1", "to": "+15551234567", "text": "Ready for review"}]
    imsg = InProcessImsg()
    r = _relay(tmp_path, imsg, be)
    handled = _run(r.process_one_outbound())
    assert handled is True
    assert imsg.sent[0]["text"] == "Ready for review"
    assert be.acks == [{"id": "job1", "status": "sent", "guid": "fake-out-1", "error": None}]


def test_outbound_send_failure_acks_retryable(tmp_path):
    be = FakeBackend()
    be.claims = [{"id": "job2", "to": "+1555", "text": "hi"}]
    imsg = InProcessImsg(send_fails=1)       # transient send failure
    r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.sent == []                    # nothing delivered
    assert be.acks[0]["status"] == "retryable_failed"   # server re-queues; NOT marked sent


def test_outbound_permanent_send_failure_reported(tmp_path):
    be = FakeBackend()
    be.claims = [{"id": "job3", "to": "+1555", "text": "hi"}]
    imsg = InProcessImsg(send_raises=True)
    r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    # Relay reports the failure; the SERVER caps attempts -> terminal_failed (see test_relay_io.py).
    assert be.acks[0]["status"] == "retryable_failed"
    assert be.acks[0]["guid"] is None


def test_outbound_empty_claim_is_noop(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    assert _run(r.process_one_outbound()) is False
    assert be.acks == []
