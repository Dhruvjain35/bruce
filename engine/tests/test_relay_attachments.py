"""PR-B1 — relay attachment transport (the multimodal unblocker).

Proves: watch subscribes WITH attachment metadata, get_message runs on a dedicated subprocess (never
the watch stream), a still-downloading attachment resolves OFF the hot path, a never-arriving one
gives up honestly (attachment_unavailable + checkpoint, no infinite retry / no loss), symlink/non-
regular paths are rejected, and relay logs stay content-free. Fake-imsg + fake backend, offline.

LIVE NOTE: the imsg {'attachments': True} / 'message.get' shapes are UNVERIFIED against real Messages
— fake-verified only until the dedicated-Mac dry-run confirms the real flag names.
"""

from __future__ import annotations

import asyncio
import logging
import os

from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.imsg import SubprocessImsg
from relay.relay import Relay

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48


class FakeBackend:
    def __init__(self) -> None:
        self.inbound: list[dict] = []
        self.uploads: list[tuple[str, int]] = []

    async def post_inbound(self, event: dict) -> dict:
        self.inbound.append(event)
        return {"status": "processed", "mission_id": "m1"}

    async def upload(self, data: bytes, media_type: str, filename: str | None) -> str:
        self.uploads.append((media_type, len(data)))
        return f"upl-{len(self.uploads)}"

    async def claim(self):
        return None

    async def ack(self, *a, **k):
        pass

    async def heartbeat(self):
        return {"ok": True}


def _relay(tmp_path, imsg, be):
    return Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.01,
                 attachment_max_retries=3, attachment_retry_delay=0.01)


def _run(coro):
    return asyncio.run(coro)


# --- imsg contract --------------------------------------------------------------------------------

def test_watch_subscribes_with_attachments(monkeypatch):
    import relay.imsg as imod

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass

    class _Stdout:
        def __init__(self): self.n = 0
        async def readline(self):
            self.n += 1
            if self.n == 1:
                return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'   # subscribe response
            return b''                                                       # EOF -> watch returns

    class _Proc:
        def __init__(self): self.stdin, self.stdout = _Stdin(), _Stdout()

    async def fake_exec(*a, **k): return _Proc()
    monkeypatch.setattr(imod.asyncio, "create_subprocess_exec", fake_exec)

    im = SubprocessImsg("imsg")
    calls = []
    orig = im._rpc
    async def spy(method, params=None):
        calls.append((method, params)); return await orig(method, params)
    monkeypatch.setattr(im, "_rpc", spy)

    async def drive():
        async for _ in im.watch():
            pass
    _run(drive())
    assert ("watch.subscribe", {"attachments": True}) in calls


def test_get_message_uses_dedicated_subprocess(monkeypatch):
    im = SubprocessImsg("imsg")
    assert im._proc is None
    async def fake_oneshot(method, params, *, timeout=60.0):
        assert method == "message.get" and params == {"guid": "g1", "attachments": True}
        return {"result": {"guid": "g1", "sender": "+1", "text": "hi"}}
    monkeypatch.setattr(im, "_oneshot_rpc", fake_oneshot)
    ev = _run(im.get_message("g1"))
    assert ev is not None and ev.guid == "g1"
    assert im._proc is None                                  # never touched the watch process


# --- delayed / missing resolution -----------------------------------------------------------------

def test_delayed_attachment_resolves_off_hot_path(tmp_path):
    be = FakeBackend()
    img = tmp_path / "late.png"; img.write_bytes(PNG)
    im = InProcessImsg()
    r = _relay(tmp_path, im, be)
    ev = {"guid": "d1", "sender": "+1555",
          "attachments": [{"mime_type": "image/png", "original_path": str(img), "missing": True}]}
    im.resolve_to("d1", {"guid": "d1", "sender": "+1555",
                         "attachments": [{"mime_type": "image/png", "original_path": str(img), "missing": False}]})

    async def go():
        assert await r.process_inbound_dict(ev) == "deferred"
        assert be.inbound == []                             # nothing posted on the hot path
        await r.drain_pending()
    _run(go())
    assert len(be.inbound) == 1 and len(be.uploads) == 1
    assert be.inbound[0]["attachments"][0]["kind"] == "image"
    assert be.inbound[0]["attachment_unavailable"] is False
    assert r.checkpoint.has("d1")                           # checkpointed exactly once, after resolve


def test_missing_bytes_gives_up_honestly(tmp_path):
    be = FakeBackend()
    img = tmp_path / "never.png"; img.write_bytes(PNG)
    im = InProcessImsg()
    r = _relay(tmp_path, im, be)
    ev = {"guid": "m1", "sender": "+1555",
          "attachments": [{"mime_type": "image/png", "original_path": str(img), "missing": True}]}
    im.resolve_to("m1", {"guid": "m1", "sender": "+1555",   # stays missing past the retry cap
                         "attachments": [{"mime_type": "image/png", "original_path": str(img), "missing": True}]})

    async def go():
        assert await r.process_inbound_dict(ev) == "deferred"
        await r.drain_pending()
    _run(go())
    assert len(be.inbound) == 1                             # posted once, not lost, not looping
    assert be.inbound[0]["attachment_unavailable"] is True
    assert be.inbound[0]["attachments"] == []
    assert r.checkpoint.has("m1")
    assert im.get_message_calls == 3                        # bounded by attachment_max_retries


# --- hardening + logging --------------------------------------------------------------------------

def test_symlink_attachment_rejected(tmp_path):
    be = FakeBackend()
    real = tmp_path / "real.png"; real.write_bytes(PNG)
    link = tmp_path / "link.png"; os.symlink(real, link)
    im = InProcessImsg()
    r = _relay(tmp_path, im, be)
    ev = {"guid": "s1", "sender": "+1555", "text": "see pic",
          "attachments": [{"mime_type": "image/png", "original_path": str(link), "missing": False}]}
    _run(r.process_inbound_dict(ev))
    assert be.uploads == []                                 # symlink never followed/uploaded
    assert be.inbound[0]["attachments"] == []
    assert be.inbound[0]["text"] == "see pic"               # message still delivered
    assert r.checkpoint.has("s1")


def test_relay_logs_are_content_free(tmp_path, caplog):
    be = FakeBackend()
    img = tmp_path / "secret_flyer.png"; img.write_bytes(PNG)
    im = InProcessImsg()
    r = _relay(tmp_path, im, be)
    ev = {"guid": "c1", "sender": "+15551234567", "text": "my private note",
          "attachments": [{"mime_type": "image/png", "original_path": str(img), "missing": False}]}
    with caplog.at_level(logging.INFO, logger="bruce.relay"):
        _run(r.process_inbound_dict(ev))
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    for leak in ("secret_flyer", "my private note", "+15551234567", str(img)):
        assert leak not in logs
