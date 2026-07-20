"""Bite 1.5 A2 — FAIL-CLOSED relay send enforcement (client side), offline fakes.

The relay SENDS only when an authenticated directive check returns exactly `run`. These tests use a
directive-controllable fake backend + an in-process fake imsg with an invocation counter and a
controllable barrier immediately before the send, so a directive change can land deterministically at
each point in the send flow (after claim, during slow preparation, and in the smallest race window
right before the imsg invocation). No network, no subprocess (except the reap test) — offline suite.

Mapping to the required scenarios is in each test's docstring. See docs/relay-emergency-stop.md for the
in-flight-window analysis and the exact final-send algorithm.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from relay.backend import AuthError, BackendError
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.imsg import SubprocessImsg
from relay.pending import PendingStore
from relay.relay import PAUSE_OUTBOUND, RUN, STOP, Relay


def _run(coro):
    return asyncio.run(coro)


class GateBackend:
    """In-memory backend with a controllable directive + failure modes, and an outbound queue that models
    the A1 server claim gate (204 while paused/stopped) so exactly-once recovery is honest.

    directive() is the authenticated pre-send check the relay calls. claim() hands out queue[0] only when
    the directive is `run` (mirrors A1); `flip_after_claim` simulates a pause/stop that trips right AFTER
    a run-time claim. A released (retryable_failed) row stays reclaimable; a sent row leaves the queue."""

    def __init__(self, directive: str = RUN) -> None:
        self.directive_value = directive
        self.directive_auth_fail = False     # directive() raises AuthError (revoked/expired credential)
        self.directive_net_fail = False      # directive() raises BackendError (network/TLS/timeout)
        self.directive_malformed = False     # directive() raises BackendError (malformed body)
        self.claim_auth_fail = False         # claim() itself raises AuthError (revoked at claim time)
        self.flip_after_claim: str | None = None
        self.paused_retry_after: float | None = None   # Retry-After the server would send on a paused 204
        self.queue: list[dict] = []
        self.status: dict[str, str] = {}
        self.acks: list[dict] = []
        self.claim_calls = 0
        self.directive_calls = 0
        self.last_retry_after: float | None = None

    def enqueue(self, job: dict) -> None:
        self.queue.append(dict(job))

    async def claim(self) -> dict | None:
        self.claim_calls += 1
        if self.claim_auth_fail:
            raise AuthError("revoked")
        if self.directive_value != RUN:            # models the A1 server claim gate (204 while paused/stop)
            self.last_retry_after = self.paused_retry_after
            return None
        self.last_retry_after = None
        if not self.queue:
            return None
        job = dict(self.queue[0])                  # stays in queue until a 'sent'/'terminal' ack removes it
        if self.flip_after_claim is not None:      # a pause/stop tripped right after this run-time claim
            self.directive_value = self.flip_after_claim
            self.flip_after_claim = None
        return job

    async def directive(self) -> str:
        self.directive_calls += 1
        if self.directive_auth_fail:
            raise AuthError("revoked")
        if self.directive_net_fail:
            raise BackendError("network")
        if self.directive_malformed:
            raise BackendError("malformed directive response")
        return self.directive_value

    async def ack(self, oid, status, guid, error) -> None:
        self.acks.append({"id": oid, "status": status, "guid": guid, "error": error})
        self.status[oid] = status
        if status in ("sent", "terminal_failed"):
            self.queue = [j for j in self.queue if j["id"] != oid]   # delivered/terminal -> leaves the queue

    async def heartbeat(self) -> dict:
        return {"ok": True, "directive": self.directive_value}

    async def post_inbound(self, event) -> dict:
        return {"status": "processed"}

    async def upload(self, data, media_type, filename) -> str:
        return "ref"


def _relay(tmp_path, imsg, be, *, ledger=True, paused_backoff=30.0):
    r = Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
              spool_dir=str(tmp_path / "spool"), poll_interval=0.005,
              sent_ledger=FileCheckpoint(str(tmp_path / "sent.json")) if ledger else None)
    r._paused_backoff_s = paused_backoff
    return r


def _sent_acks(be):
    return [a for a in be.acks if a["status"] == "sent"]


# ------------------------------------------------- 1-3: pause/stop AFTER claim, BEFORE send


def test_1_claimed_then_global_pause_before_send(tmp_path):
    """Claimed while run, then a GLOBAL pause trips before the send -> no imsg send; row released."""
    be = GateBackend(); be.enqueue({"id": "j1", "to": "+1555", "text": "hi"}); be.flip_after_claim = PAUSE_OUTBOUND
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is False
    assert imsg.calls == 0                                   # never handed to imsg
    assert _sent_acks(be) == []                              # never marked sent
    assert be.acks[-1]["status"] == "retryable_failed"       # lease released for safe recovery


def test_2_claimed_then_per_device_pause_before_send(tmp_path):
    """Per-device pause reaches the client as pause_outbound — same fail-closed refusal for that device."""
    be = GateBackend(); be.enqueue({"id": "j2", "to": "+1555", "text": "hi"}); be.flip_after_claim = PAUSE_OUTBOUND
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []


def test_3_claimed_then_stop_before_send(tmp_path):
    """A stop directive before the send -> no send, the relay stops claiming, and the imsg child is reaped."""
    be = GateBackend(); be.enqueue({"id": "j3", "to": "+1555", "text": "hi"}); be.flip_after_claim = STOP
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []
    assert r._stop.is_set() and imsg.closed is True          # stopped + reaped


# ------------------------------------------------- 4-5: pause/stop DURING slow preparation


def _slow_prep_flip(be, to_directive, delay=0.02):
    async def _hook(job):
        await asyncio.sleep(delay)                            # simulate slow attachment preparation
        be.directive_value = to_directive                     # a pause/stop lands DURING preparation
    return _hook


def test_4_pause_during_slow_attachment_preparation(tmp_path):
    """A pause that arrives DURING slow preparation is caught by the post-preparation re-check -> no send."""
    be = GateBackend(); be.enqueue({"id": "j4", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    r._prepare_hook = _slow_prep_flip(be, PAUSE_OUTBOUND)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []


def test_5_stop_during_slow_attachment_preparation(tmp_path):
    """A stop during slow preparation -> no send + the relay stops."""
    be = GateBackend(); be.enqueue({"id": "j5", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    r._prepare_hook = _slow_prep_flip(be, STOP)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and r._stop.is_set()


# ------------------------------------------------- 6-10: fail-closed on every lookup failure


def test_6_directive_lookup_network_failure_before_send(tmp_path):
    """A network/TLS/timeout failure on the directive lookup is NEVER permission to send -> hold, no send."""
    be = GateBackend(); be.enqueue({"id": "j6", "to": "+1555", "text": "hi"}); be.directive_net_fail = True
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []


def test_7_authentication_failure_before_send(tmp_path):
    """An auth failure on the directive lookup -> no send + stop (a rejected credential can't send)."""
    be = GateBackend(); be.enqueue({"id": "j7", "to": "+1555", "text": "hi"}); be.directive_auth_fail = True
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and r._stop.is_set()


def test_8_malformed_directive_response(tmp_path):
    """A malformed directive response (bad JSON / missing field) -> fail closed, no send."""
    be = GateBackend(); be.enqueue({"id": "j8", "to": "+1555", "text": "hi"}); be.directive_malformed = True
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []


def test_9_unknown_directive_value(tmp_path):
    """An UNKNOWN directive value (e.g. a newer server directive) is treated as blocked, never as run."""
    be = GateBackend(); be.enqueue({"id": "j9", "to": "+1555", "text": "hi"}); be.flip_after_claim = "drain-mode"
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and _sent_acks(be) == []


def test_10_revoked_device_credential(tmp_path):
    """A revoked credential (claim rejected) -> no send and the relay stops."""
    be = GateBackend(); be.enqueue({"id": "j10", "to": "+1555", "text": "hi"}); be.claim_auth_fail = True
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is False
    assert imsg.calls == 0 and be.acks == [] and r._stop.is_set()


# ------------------------------------------------- 11-15: lease + recovery, exactly once, durable


def test_11_resume_reclaims_the_row_exactly_once(tmp_path):
    """A message blocked by a pause is reclaimed EXACTLY ONCE after resume — sent once, no second row."""
    be = GateBackend(); be.enqueue({"id": "j11", "to": "+1555", "text": "hi"}); be.flip_after_claim = PAUSE_OUTBOUND
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())                            # claim(run) -> pause -> release, no send
    assert imsg.calls == 0
    assert _run(r.process_one_outbound()) is False            # still paused: claim 204s, nothing sent
    be.directive_value = RUN                                  # resume
    assert _run(r.process_one_outbound()) is True             # reclaimed once -> sent
    assert imsg.calls == 1 and be.status["j11"] == "sent" and len(be.queue) == 0


def test_12_process_restart_while_blocked_recovers_once(tmp_path):
    """A block leaves the at-most-once ledger UNMARKED; after a relay restart (same files) the message is
    reclaimed once on resume and sent exactly once."""
    be = GateBackend(); be.enqueue({"id": "j12", "to": "+1555", "text": "hi"}); be.flip_after_claim = PAUSE_OUTBOUND
    imsg = InProcessImsg()
    r1 = _relay(tmp_path, imsg, be)
    _run(r1.process_one_outbound())                           # blocked -> released, ledger NOT marked
    assert r1.sent_ledger is not None and not r1.sent_ledger.has("j12")
    # full restart: brand-new relay objects, SAME ledger/checkpoint files on disk
    be.directive_value = RUN
    r2 = _relay(tmp_path, imsg, be)
    assert _run(r2.process_one_outbound()) is True
    assert imsg.calls == 1 and r2.sent_ledger.has("j12")


def test_13_durable_ledger_survives_restart(tmp_path):
    """After a real send the ledger persists; a reclaim of the same id after restart does NOT resend."""
    be = GateBackend(); be.enqueue({"id": "j13", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg()
    _run(_relay(tmp_path, imsg, be).process_one_outbound())   # sends; ledger marked on disk
    assert imsg.calls == 1
    be.enqueue({"id": "j13", "to": "+1555", "text": "hi"})    # server reclaims (lost ack) the SAME id
    r2 = _relay(tmp_path, imsg, be)                           # restart: same ledger file
    assert _run(r2.process_one_outbound()) is True
    assert imsg.calls == 1                                    # STILL once — ledger short-circuits the resend


def test_14_pending_attachment_survives_restart(tmp_path):
    """The restart-safe pending-attachment store persists across a new instance (inbound recovery state)."""
    path = str(tmp_path / "pending.json")
    p1 = PendingStore(path); p1.upsert("gA", {"guid": "gA"}, now=1.0)
    p2 = PendingStore(path)                                   # reload from disk (simulated restart)
    assert p2.has("gA") and p2.get("gA")["event"]["guid"] == "gA"


def test_15_no_duplicate_imsg_invocation_on_reclaim(tmp_path):
    """The same id claimed twice (lease-expiry reclaim) is handed to imsg exactly once."""
    be = GateBackend(); be.enqueue({"id": "j15", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is True             # sends
    be.enqueue({"id": "j15", "to": "+1555", "text": "hi"})    # reclaim
    assert _run(r.process_one_outbound()) is True             # re-ack, no resend
    assert imsg.calls == 1 and [a["status"] for a in be.acks] == ["sent", "sent"]


# ------------------------------------------------- 16: never ack sent while blocked


def test_16_no_sent_acknowledgement_while_blocked(tmp_path):
    """A blocked message is NEVER acked 'sent' — only released (retryable_failed)."""
    be = GateBackend(); be.enqueue({"id": "j16", "to": "+1555", "text": "hi"}); be.flip_after_claim = PAUSE_OUTBOUND
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert all(a["status"] != "sent" for a in be.acks) and be.acks[-1]["status"] == "retryable_failed"


# ------------------------------------------------- 17-18: Retry-After honored, no tight loop


def test_17_retry_after_is_honored(tmp_path):
    """While paused the loop backs off the server's Retry-After (not the fast poll interval), so it does
    not hot-poll."""
    be = GateBackend(directive=PAUSE_OUTBOUND); be.paused_retry_after = 0.1
    imsg = InProcessImsg()
    r = Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
              spool_dir=str(tmp_path / "spool"), poll_interval=0.005)   # poll is 20x faster than Retry-After

    async def drive():
        task = asyncio.create_task(r.run_outbound())
        await asyncio.sleep(0.35)
        r.stop(); await asyncio.wait_for(task, timeout=1.0)

    _run(drive())
    # with a 0.1s Retry-After over ~0.35s, expect ~4 polls, NOT the ~70 a 0.005s poll would produce
    assert be.claim_calls <= 8


def test_18_no_tight_polling_loop_while_paused(tmp_path):
    """A paused relay's claim loop is bounded (never a tight spin), even with no Retry-After hint."""
    be = GateBackend(directive=PAUSE_OUTBOUND)              # no paused_retry_after -> falls back to poll_interval
    imsg = InProcessImsg()
    r = Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
              spool_dir=str(tmp_path / "spool"), poll_interval=0.05)

    async def drive():
        task = asyncio.create_task(r.run_outbound())
        await asyncio.sleep(0.3)
        r.stop(); await asyncio.wait_for(task, timeout=1.0)

    _run(drive())
    assert be.claim_calls <= 12 and imsg.calls == 0          # bounded polls, nothing ever sent


# ------------------------------------------------- 19: stop reaps imsg children


def test_19_stop_reaps_imsg_child_inprocess(tmp_path):
    """A stop directive reaps the imsg child (in-process fake records the reap)."""
    be = GateBackend(); be.enqueue({"id": "j19", "to": "+1555", "text": "hi"}); be.flip_after_claim = STOP
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert imsg.closed is True


def _fake_imsg_shim(tmp_path) -> str:
    import relay as _relaypkg
    engine_root = os.path.dirname(os.path.dirname(os.path.abspath(_relaypkg.__file__)))
    shim = tmp_path / "fake-imsg"
    shim.write_text(f'#!/bin/sh\nexport PYTHONPATH="{engine_root}"\nexec "{sys.executable}" -m relay.fake_imsg "$@"\n')
    shim.chmod(0o755)
    return str(shim)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim")
def test_19b_stop_reaps_real_watch_subprocess(tmp_path):
    """aclose() actually terminates the real imsg watch subprocess (no orphaned child on stop)."""
    events = tmp_path / "events.json"
    events.write_text(json.dumps([{"guid": "e1", "sender": "+1555", "text": "hi"}]))
    os.environ["BRUCE_FAKE_IMSG_EVENTS"] = str(events)
    try:
        im = SubprocessImsg(_fake_imsg_shim(tmp_path))

        async def scenario():
            async def _drain():
                async for _ in im.watch():
                    pass
            t = asyncio.create_task(_drain())
            await asyncio.sleep(0.5)                          # watch child is up, blocked on readline
            proc = im._proc
            assert proc is not None and proc.returncode is None
            await im.aclose()                                # reap
            t.cancel()
            return proc

        proc = _run(asyncio.wait_for(scenario(), timeout=15))
        assert proc.returncode is not None                   # child was reaped
        assert im._proc is None
    finally:
        os.environ.pop("BRUCE_FAKE_IMSG_EVENTS", None)


# ------------------------------------------------- 20: normal path still sends exactly once


def test_20_normal_run_path_sends_exactly_once(tmp_path):
    """The existing happy path is unchanged: directive run throughout -> sent exactly once, acked sent."""
    be = GateBackend(); be.enqueue({"id": "j20", "to": "+15551234567", "text": "Ready for review"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1 and imsg.sent[0]["text"] == "Ready for review"
    assert _sent_acks(be) == [{"id": "j20", "status": "sent", "guid": "fake-out-1", "error": None}]


# ------------------------------------------------- ADVERSARIAL: the irreducible local race


def test_adversarial_pause_in_the_irreducible_window_still_sends(tmp_path):
    """The smallest possible race: a pause activates AFTER the final directive check but BEFORE the imsg
    invocation (via the barrier that runs only in that window). Bytes are already committed to imsg and
    cannot be recalled, so the send DOES happen — this is the IRREDUCIBLE local race documented in
    docs/relay-emergency-stop.md. Paired with test_1 (pause BEFORE the final check -> no send), it pins
    the exact boundary. Only the ledger append + the send call execute in this window."""
    be = GateBackend(); be.enqueue({"id": "jr", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)

    async def _barrier():                                     # runs AFTER the final gate, BEFORE imsg send
        be.directive_value = PAUSE_OUTBOUND                  # a pause lands in the irreducible window
    r._send_barrier = _barrier

    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1                                    # irreducible: the send could not be recalled
    assert be.status["jr"] == "sent"


# ------------------------------------------------- in-flight window MEASUREMENT


def test_inflight_window_measurement(tmp_path, capsys):
    """Measure the send-flow segments and assert the irreducible window (final gate -> imsg invocation)
    contains only minimal work. Prints the numbers for the report."""
    be = GateBackend(); be.enqueue({"id": "jm", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)

    marks: dict[str, float] = {}
    prep_s = 0.02
    directive_calls: list[float] = []

    orig_directive = be.directive
    async def timed_directive():
        d = await orig_directive()
        directive_calls.append(time.perf_counter())          # timestamp AFTER each check resolves
        return d
    be.directive = timed_directive

    async def prep_hook(job):
        marks["prep_start"] = time.perf_counter()
        await asyncio.sleep(prep_s)
        marks["prep_end"] = time.perf_counter()
    r._prepare_hook = prep_hook

    async def barrier():
        marks["final_gate_to_barrier"] = time.perf_counter()  # entered the irreducible window
    r._send_barrier = barrier

    orig_send = imsg.send_text
    async def timed_send(to, text):
        marks["send_enter"] = time.perf_counter()
        out = await orig_send(to, text)
        marks["send_exit"] = time.perf_counter()
        return out
    imsg.send_text = timed_send

    marks["claim_done"] = time.perf_counter()
    _run(r.process_one_outbound())

    # segments (seconds)
    claim_to_first_check = directive_calls[0] - marks["claim_done"]
    prep_duration = marks["prep_end"] - marks["prep_start"]
    final_check_to_send = marks["send_enter"] - directive_calls[-1]
    send_duration = marks["send_exit"] - marks["send_enter"]
    irreducible = final_check_to_send                        # final gate resolved -> bytes handed to imsg
    preventable = directive_calls[-1] - marks["claim_done"]  # everything up to & incl. the final check

    print(json.dumps({
        "claim_to_first_directive_check_s": round(claim_to_first_check, 6),
        "attachment_preparation_s": round(prep_duration, 6),
        "final_check_to_imsg_invocation_s": round(final_check_to_send, 6),
        "imsg_invocation_s": round(send_duration, 6),
        "total_maximum_preventable_window_s": round(preventable, 6),
        "irreducible_handoff_window_s": round(irreducible, 6),
    }))

    assert imsg.calls == 1
    assert irreducible < 0.01                                # only a ledger append + the call — sub-10ms
    assert prep_duration >= prep_s                           # the slow-prep segment was measured
