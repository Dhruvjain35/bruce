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
import json
import os
import sys

import pytest

from relay.backend import AuthError, BackendError
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.imsg import SubprocessImsg, stream_event
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
        return {"ok": True, "directive": "run"}

    async def directive(self) -> str:
        # default: normal operation (the A2 fail-closed enforcement is exercised in
        # test_relay_send_enforcement.py with a directive-controllable backend)
        return "run"


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


def test_outbound_definite_pre_handoff_decline_is_retryable(tmp_path):
    """imsg DEFINITELY declined before accepting bytes (ImsgSendRejected) -> safely retryable, never
    suppressed forever by the ledger."""
    be = FakeBackend()
    be.claims = [{"id": "job2", "to": "+1555", "text": "hi"}]
    imsg = InProcessImsg(send_rejects=1)     # explicit pre-handoff rejection
    r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.sent == []                    # nothing delivered
    assert be.acks[0]["status"] == "retryable_failed" and be.acks[0]["error"] == "rejected_pre_handoff"


def test_outbound_ambiguous_transport_crash_is_surfaced(tmp_path):
    """A transport crash (generic exception) is AMBIGUOUS — bytes may or may not have gone. It is never
    reported confirmed-sent and never blindly resent; it is surfaced as terminal_failed:handoff_unknown."""
    be = FakeBackend()
    be.claims = [{"id": "job3", "to": "+1555", "text": "hi"}]
    imsg = InProcessImsg(crash_unknown=True)
    r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert be.acks[0]["status"] == "terminal_failed"
    assert be.acks[0]["error"] == "handoff_unknown" and be.acks[0]["guid"] is None


def test_outbound_empty_claim_is_noop(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    assert _run(r.process_one_outbound()) is False
    assert be.acks == []


# --- imsg watch-stream framing (contract with the real `imsg rpc`) ---------------------------------
# imsg 0.13.x frames rpc I/O as JSON-RPC 2.0, so watch pushes arrive as NOTIFICATIONS. stream_event
# must accept that framing AND a bare object, while skipping our own request responses — otherwise a
# live relay would silently yield no messages.

def test_stream_event_jsonrpc_notification_params_is_message():
    ev = stream_event({"jsonrpc": "2.0", "method": "watch.event",
                       "params": {"guid": "g1", "sender": "+1555", "text": "hi", "is_from_me": False}})
    assert ev is not None and ev.guid == "g1" and ev.sender == "+1555" and ev.text == "hi"


def test_stream_event_jsonrpc_notification_params_wraps_message():
    ev = stream_event({"jsonrpc": "2.0", "method": "watch.event",
                       "params": {"subscription": 1, "message": {"guid": "g2", "text": "yo"}}})
    assert ev is not None and ev.guid == "g2" and ev.text == "yo"


def test_stream_event_bare_message_object():
    ev = stream_event({"guid": "g3", "sender": "+1555", "is_group": True, "chat_guid": "c1"})
    assert ev is not None and ev.guid == "g3" and ev.is_group is True and ev.chat_guid == "c1"


def test_stream_event_skips_request_responses_and_noise():
    assert stream_event({"jsonrpc": "2.0", "result": {"subscription": 1}, "id": 1}) is None  # subscribe ack
    assert stream_event({"jsonrpc": "2.0", "result": {"guid": "out-1"}, "id": 2}) is None      # send response
    assert stream_event({"jsonrpc": "2.0", "error": {"code": -32601}, "id": 3}) is None        # error
    assert stream_event({"jsonrpc": "2.0", "method": "ping", "params": {}}) is None            # no guid
    assert stream_event("not-a-dict") is None
    assert stream_event({"hello": "world"}) is None


# --- REGRESSION: the 30-second real-message resend loop -------------------------------------------
# Incident: `run_inbound` (watch) and `run_outbound` (send) shared ONE `imsg rpc` subprocess. A send's
# stdout.readline() collided with the watch loop's readline on the same stream -> asyncio RuntimeError
# AFTER the send request had already reached imsg. The message was delivered but recorded as failed,
# so the durable queue reclaimed + resent it every retry-backoff (30s), up to max_attempts. Two fixes:
# (1) send uses its OWN subprocess; (2) a durable at-most-once ledger so a reclaimed id is never
# re-sent to a real person.


def _fake_imsg_shim(tmp_path) -> str:
    """A one-line executable that runs the in-repo fake imsg via this interpreter (works in CI)."""
    import relay as _relaypkg
    engine_root = os.path.dirname(os.path.dirname(os.path.abspath(_relaypkg.__file__)))
    shim = tmp_path / "fake-imsg"
    shim.write_text(f'#!/bin/sh\nexport PYTHONPATH="{engine_root}"\nexec "{sys.executable}" -m relay.fake_imsg "$@"\n')
    shim.chmod(0o755)
    return str(shim)


def _relay_with_ledger(tmp_path, imsg, be):
    from relay.outbound_ledger import OutboundLedger
    return Relay(imsg=imsg, backend=be,
                 checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.01,
                 sent_ledger=OutboundLedger(str(tmp_path / "sent.json")))


def test_outbound_reclaim_sends_exactly_once(tmp_path):
    """The SAME outbound id, claimed twice (lease-expiry reclaim), must be delivered only once."""
    be = FakeBackend()
    job = {"id": "ob-1", "to": "+15551230000", "text": "reply"}
    be.claims = [dict(job), dict(job)]           # server hands out the same id twice
    imsg = InProcessImsg()
    r = _relay_with_ledger(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is True    # 1st: actually sends
    assert _run(r.process_one_outbound()) is True    # reclaim: MUST NOT resend
    assert len(imsg.sent) == 1                        # <-- delivered exactly once
    assert [a["status"] for a in be.acks] == ["sent", "sent"]


def test_outbound_reclaim_idempotent_across_relay_restart(tmp_path):
    """The at-most-once guard is durable: a reclaim after a relay restart still doesn't resend."""
    be = FakeBackend()
    job = {"id": "ob-9", "to": "+15551230000", "text": "reply"}
    be.claims = [dict(job)]
    imsg = InProcessImsg()
    _run(_relay_with_ledger(tmp_path, imsg, be).process_one_outbound())
    assert len(imsg.sent) == 1
    # brand-new relay objects, SAME ledger file on disk
    be.claims = [dict(job)]
    r2 = _relay_with_ledger(tmp_path, imsg, be)
    assert _run(r2.process_one_outbound()) is True
    assert len(imsg.sent) == 1                        # STILL once after restart


def test_outbound_send_exception_never_resends(tmp_path):
    """The incident's exact condition (send raises = ambiguous) must not cause repeated real sends. It is
    surfaced as handoff_unknown once, and a reclaim NEVER re-invokes imsg (at-most-once invocation)."""
    be = FakeBackend()
    job = {"id": "ob-2", "to": "+15551230000", "text": "x"}
    be.claims = [dict(job), dict(job)]
    imsg = InProcessImsg(crash_unknown=True)     # ambiguous transport crash (like the old RuntimeError)
    r = _relay_with_ledger(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is True
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1                            # invoked at most once; reclaim does NOT re-invoke
    assert imsg.sent == []                            # never confirmed delivered
    assert [a["status"] for a in be.acks] == ["terminal_failed", "terminal_failed"]
    assert all(a["error"] == "handoff_unknown" for a in be.acks)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim")
def test_send_uses_its_own_process_not_the_watch_stream(tmp_path):
    """send_text must spawn its OWN imsg process and never touch the watch subprocess (self._proc).
    This is the direct fix for the shared-stdout RuntimeError that caused the resend loop."""
    im = SubprocessImsg(_fake_imsg_shim(tmp_path))
    assert im._proc is None
    guid = _run(im.send_text("+15551230000", "reply"))
    assert guid is not None                           # sent via a dedicated process
    assert im._proc is None                           # watch process was never created/used by send


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim")
def test_watch_and_send_run_concurrently_without_collision(tmp_path):
    """Reproduce the incident: send WHILE the watch loop is blocked on readline. Pre-fix this raised
    RuntimeError (concurrent readline on one stream); now send uses a separate process and succeeds."""
    shim = _fake_imsg_shim(tmp_path)
    events = tmp_path / "events.json"
    events.write_text(json.dumps([{"guid": "cev-1", "sender": "+1555", "text": "hi"}]))
    os.environ["BRUCE_FAKE_IMSG_EVENTS"] = str(events)
    try:
        im = SubprocessImsg(shim)
        got: list = []

        async def scenario():
            watch_task = asyncio.create_task(_collect(im, got))
            await asyncio.sleep(0.6)                   # watch yields the event, then blocks on readline
            guid = await im.send_text("+15551230000", "reply")   # concurrent send: must not collide
            watch_task.cancel()
            if im._proc and im._proc.returncode is None:
                im._proc.kill()
            return guid

        guid = _run(asyncio.wait_for(scenario(), timeout=15))
        assert guid is not None
        assert got and got[0].guid == "cev-1"
    finally:
        os.environ.pop("BRUCE_FAKE_IMSG_EVENTS", None)


async def _collect(im, got):
    async for ev in im.watch():
        got.append(ev)


# --- entrypoint wiring (regression: `python -m relay` crashed on an UnboundLocalError) ------------
# main() was never covered, so a local `import os.path` that shadowed the module `os` shipped and
# crashed the relay on startup. build_relay() runs the same os.makedirs/os.path lines, unit-testable.

def test_build_relay_wires_config_without_crashing(tmp_path):
    from relay.config import RelayConfig
    from relay.__main__ import build_relay
    cfg = RelayConfig(
        base_url="https://example.test", secret="dummy-not-real",
        spool_dir=str(tmp_path / "spool"),
        checkpoint_path=str(tmp_path / "state" / "checkpoint.json"),
        imsg_bin="imsg", poll_interval=0.01, reconnect_delay=0.01,
    )
    relay = build_relay(cfg)                              # would raise UnboundLocalError pre-fix
    assert isinstance(relay, Relay)
    assert relay.sent_ledger is not None                 # at-most-once ledger is wired
    assert os.path.isdir(os.path.dirname(cfg.checkpoint_path))   # state dir created
    assert relay.sent_ledger.path == str(tmp_path / "state" / "outbound_sent.json")
