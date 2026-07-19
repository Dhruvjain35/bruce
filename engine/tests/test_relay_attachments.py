"""Relay attachment transport + DELAYED-attachment recovery via subsequent watch events.

imsg 0.13.1 has NO message.get, so a still-downloading attachment is resolved by a later watch event
(imsg re-emits the message when the file lands) or by a re-stat sweep, and times out honestly if it
never arrives. Fake-imsg + fake backend, offline. LIVE NOTE: fake-verified only until the dedicated-
Mac dry-run confirms imsg's real watch-event shapes (see docs/self-hosted-imessage-alpha.md).
"""

from __future__ import annotations

import asyncio
import logging
import os

from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.imsg import SubprocessImsg
from relay.pending import PendingStore
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


def _relay(tmp_path, imsg, be, *, max_wait=60.0, max_events=8):
    return Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.01,
                 pending=PendingStore(str(tmp_path / "pending.json")),
                 attachment_max_wait_s=max_wait, attachment_sweep_interval_s=0.01,
                 attachment_max_events=max_events)


def _att(path, missing):
    return {"mime_type": "image/png", "original_path": str(path), "missing": missing}


def _run(coro):
    return asyncio.run(coro)


class YieldingBackend(FakeBackend):
    """upload/post yield control so a genuine watch-vs-sweep interleave is forced (proves the lock)."""

    async def upload(self, data, media_type, filename):
        await asyncio.sleep(0)
        return await super().upload(data, media_type, filename)

    async def post_inbound(self, event):
        await asyncio.sleep(0)
        return await super().post_inbound(event)


# --- imsg contract + hardening (unchanged behavior) -----------------------------------------------

def test_watch_subscribes_with_attachments(monkeypatch):
    import relay.imsg as imod

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass

    class _Stdout:
        def __init__(self): self.n = 0
        async def readline(self):
            self.n += 1
            return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n' if self.n == 1 else b''

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


def test_symlink_attachment_rejected(tmp_path):
    be = FakeBackend()
    real = tmp_path / "real.png"; real.write_bytes(PNG)
    link = tmp_path / "link.png"; os.symlink(real, link)
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "s1", "sender": "+1", "text": "see pic",
                                 "attachments": [_att(link, False)]}))
    assert be.uploads == [] and be.inbound[0]["attachments"] == [] and be.inbound[0]["text"] == "see pic"
    assert r.checkpoint.has("s1")


# --- 1: available on first event ------------------------------------------------------------------

def test_1_attachment_available_first_event(tmp_path):
    be = FakeBackend(); img = tmp_path / "a.png"; img.write_bytes(PNG)
    r = _relay(tmp_path, InProcessImsg(), be)
    assert _run(r.process_inbound_dict({"guid": "g1", "sender": "+1", "attachments": [_att(img, False)]})) == "processed"
    assert len(be.inbound) == 1 and be.inbound[0]["attachments"][0]["kind"] == "image"
    assert r.checkpoint.has("g1") and r.pending.get("g1") is None


# --- 2: unavailable first, resolved on second event ----------------------------------------------

def test_2_resolved_on_second_watch_event(tmp_path):
    be = FakeBackend(); late = tmp_path / "late.png"     # not on disk yet (downloading)
    r = _relay(tmp_path, InProcessImsg(), be)
    assert _run(r.process_inbound_dict({"guid": "g2", "text": "pic", "attachments": [_att(late, True)]})) == "deferred"
    assert be.inbound == [] and r.pending.has("g2")       # not posted, recorded, text kept
    late.write_bytes(PNG)                                 # download completes
    assert _run(r.process_inbound_dict({"guid": "g2", "text": "pic", "attachments": [_att(late, False)]})) == "processed"
    assert len(be.inbound) == 1 and be.inbound[0]["attachments"][0]["kind"] == "image"
    assert r.checkpoint.has("g2") and r.pending.get("g2") is None


# --- 2b: resolved by the re-stat SWEEP (file lands without a new event) ----------------------------

def test_2b_resolved_by_sweep_restat(tmp_path):
    be = FakeBackend(); late = tmp_path / "sweep.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "g2b", "attachments": [_att(late, True)]}))
    assert r.pending.has("g2b") and be.inbound == []
    late.write_bytes(PNG)                                 # file lands; no new watch event
    _run(r.sweep_pending())
    assert len(be.inbound) == 1 and r.checkpoint.has("g2b") and r.pending.get("g2b") is None


# --- 3: duplicate second event -------------------------------------------------------------------

def test_3_duplicate_resolved_event_posts_once(tmp_path):
    be = FakeBackend(); img = tmp_path / "d.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "g3", "attachments": [_att(img, True)]}))
    img.write_bytes(PNG)
    resolved = {"guid": "g3", "attachments": [_att(img, False)]}
    assert _run(r.process_inbound_dict(resolved)) == "processed"
    assert _run(r.process_inbound_dict(resolved)) == "duplicate"    # duplicate second watch event
    assert len(be.inbound) == 1


# --- 4: relay restart between first and second event ----------------------------------------------

def test_4_relay_restart_between_events(tmp_path):
    be = FakeBackend(); late = tmp_path / "r.png"
    pend, cp = str(tmp_path / "pending.json"), str(tmp_path / "cp.json")

    def mk():
        return Relay(imsg=InProcessImsg(), backend=be, checkpoint=FileCheckpoint(cp),
                     spool_dir=str(tmp_path / "spool"), pending=PendingStore(pend),
                     attachment_max_wait_s=60, attachment_sweep_interval_s=0.01)

    r1 = mk()
    assert _run(r1.process_inbound_dict({"guid": "g4", "attachments": [_att(late, True)]})) == "deferred"
    assert PendingStore(pend).has("g4")                  # persisted to disk (restart-safe)
    r2 = mk()                                            # simulate a full relay restart
    assert r2.pending.has("g4")
    late.write_bytes(PNG)
    assert _run(r2.process_inbound_dict({"guid": "g4", "attachments": [_att(late, False)]})) == "processed"
    assert len(be.inbound) == 1 and r2.pending.get("g4") is None


# --- 5: never resolves -> timeout, exactly one honest reply --------------------------------------

def test_5_never_resolves_times_out_once(tmp_path):
    be = FakeBackend(); gone = tmp_path / "gone.png"     # never created
    r = _relay(tmp_path, InProcessImsg(), be, max_wait=60.0)
    assert _run(r.process_inbound_dict({"guid": "g5", "text": "see pic", "attachments": [_att(gone, True)]})) == "deferred"
    assert be.inbound == [] and r.pending.has("g5")
    future = r.pending.get("g5")["first_seen"] + 1000
    _run(r.sweep_pending(now=future))                    # time passes, still no file -> terminal
    assert len(be.inbound) == 1 and be.inbound[0]["attachment_unavailable"] is True
    assert be.inbound[0]["text"] == "see pic"            # text NOT lost
    assert r.checkpoint.has("g5") and r.pending.get("g5") is None
    # a late duplicate is now suppressed -> still exactly ONE reply, never two
    assert _run(r.process_inbound_dict({"guid": "g5", "text": "see pic", "attachments": [_att(gone, True)]})) == "duplicate"
    assert len(be.inbound) == 1


# --- 5b: timeout via too many events (event cap) --------------------------------------------------

def test_5b_timeout_via_event_cap(tmp_path):
    be = FakeBackend(); gone = tmp_path / "cap.png"
    r = _relay(tmp_path, InProcessImsg(), be, max_wait=1e9, max_events=3)
    ev = {"guid": "g5b", "attachments": [_att(gone, True)]}
    assert _run(r.process_inbound_dict(ev)) == "deferred"    # events=1
    assert _run(r.process_inbound_dict(ev)) == "deferred"    # events=2
    assert _run(r.process_inbound_dict(ev)) == "attachment_unavailable"   # events=3 -> cap -> terminal
    assert len(be.inbound) == 1 and be.inbound[0]["attachment_unavailable"] is True
    assert r.pending.get("g5b") is None


# --- 6: two attachments resolve at different times ------------------------------------------------

def test_6_two_attachments_resolve_independently(tmp_path):
    be = FakeBackend(); a = tmp_path / "a6.png"; b = tmp_path / "b6.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "gA", "attachments": [_att(a, True)]}))
    _run(r.process_inbound_dict({"guid": "gB", "attachments": [_att(b, True)]}))
    assert r.pending.has("gA") and r.pending.has("gB") and be.inbound == []
    a.write_bytes(PNG)
    _run(r.process_inbound_dict({"guid": "gA", "attachments": [_att(a, False)]}))
    assert len(be.inbound) == 1 and r.pending.get("gA") is None and r.pending.has("gB")
    b.write_bytes(PNG)
    _run(r.process_inbound_dict({"guid": "gB", "attachments": [_att(b, False)]}))
    assert len(be.inbound) == 2 and r.pending.get("gB") is None


# --- 7: text-only never delayed ------------------------------------------------------------------

def test_7_text_only_not_delayed(tmp_path):
    be = FakeBackend()
    r = _relay(tmp_path, InProcessImsg(), be)
    assert _run(r.process_inbound_dict({"guid": "t1", "sender": "+1", "text": "just text"})) == "processed"
    assert len(be.inbound) == 1 and r.pending.get("t1") is None


# --- 8/9: exactly one post (=> one conversation turn, one outbound) across the whole flow ---------

def test_8_9_exactly_one_post_across_delayed_flow(tmp_path):
    be = FakeBackend(); img = tmp_path / "one.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    miss = {"guid": "one", "attachments": [_att(img, True)]}
    _run(r.process_inbound_dict(miss)); _run(r.process_inbound_dict(miss))   # dup pending events
    img.write_bytes(PNG)
    ok = {"guid": "one", "attachments": [_att(img, False)]}
    _run(r.process_inbound_dict(ok)); _run(r.process_inbound_dict(ok))       # dup resolved events
    _run(r.sweep_pending(now=1e18))                                          # sweep must not re-post
    assert len(be.inbound) == 1                                             # exactly one


# --- 8b: watch event and sweep race to resolve the same guid -> still exactly one post -----------

def test_8b_watch_and_sweep_race_posts_once(tmp_path):
    be = YieldingBackend(); img = tmp_path / "race.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "race", "attachments": [_att(img, True)]}))   # pending
    img.write_bytes(PNG)                                                               # file lands

    async def both():   # second watch event AND the sweep fire concurrently in ONE loop
        await asyncio.gather(
            r.process_inbound_dict({"guid": "race", "attachments": [_att(img, False)]}),
            r.sweep_pending(),
        )

    _run(both())
    assert len(be.inbound) == 1 and r.pending.get("race") is None    # lock prevented a double-post


# --- 10: no stale pending records after completion -----------------------------------------------

def test_10_no_stale_pending_after_resolve_or_timeout(tmp_path):
    be = FakeBackend(); f = tmp_path / "x.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    _run(r.process_inbound_dict({"guid": "p1", "attachments": [_att(f, True)]}))
    f.write_bytes(PNG)
    _run(r.process_inbound_dict({"guid": "p1", "attachments": [_att(f, False)]}))     # resolved
    _run(r.process_inbound_dict({"guid": "p2", "attachments": [_att(tmp_path / "none.png", True)]}))
    _run(r.sweep_pending(now=1e18))                                                    # p2 times out
    assert r.pending.items() == []                                                    # nothing stale


# --- 11: no sensitive local paths / content in logs ----------------------------------------------

def test_11_delayed_flow_logs_content_free(tmp_path, caplog):
    be = FakeBackend(); img = tmp_path / "secret_photo.png"
    r = _relay(tmp_path, InProcessImsg(), be)
    with caplog.at_level(logging.INFO, logger="bruce.relay"):
        _run(r.process_inbound_dict({"guid": "L1", "sender": "+15551234567", "text": "private note",
                                     "attachments": [_att(img, True)]}))
        img.write_bytes(PNG)
        _run(r.process_inbound_dict({"guid": "L1", "sender": "+15551234567", "text": "private note",
                                     "attachments": [_att(img, False)]}))
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    for leak in ("secret_photo", "private note", "+15551234567", str(img)):
        assert leak not in logs
