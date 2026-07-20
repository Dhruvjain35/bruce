"""Bite 1.5 A2 hardening — durable DELIVERY-PHASE semantics for outbound sends.

A single pre-send boolean mark can silently LOSE a message (mark -> crash before imsg -> reclaim treats
it as attempted -> never delivered) or double-send. The relay records the explicit delivery PHASE of each
outbound id so restart recovery is derived from durable state:

    send_intent_recorded / handoff_outcome_unknown -> AMBIGUOUS (crash straddled the external handoff):
        never blindly resent, never reported confirmed-sent, surfaced as terminal_failed:handoff_unknown.
    handed_to_imsg / server_acknowledged -> bytes already handed to imsg: converge the row to sent, never
        resend (at-most-once imsg INVOCATION; a lost server ack recovers as sent).
    send_failed_before_handoff / blocked_before_send / (none) -> bytes definitely did not go: safe retry.

Terminology (accurate, per review): at-most-once imsg invocation; exactly-once server-row transition
where enforceable; AMBIGUOUS delivery state across the external boundary — NOT end-to-end exactly-once.

Each required adversarial scenario has a named test below. A crash is simulated by a hook that raises
between two durable operations; "restart" = a fresh Relay reading the SAME on-disk ledger.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from relay.backend import AuthError, BackendError
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.outbound_ledger import (
    BLOCKED_BEFORE_SEND,
    HANDED_TO_IMSG,
    HANDOFF_OUTCOME_UNKNOWN,
    SEND_FAILED_BEFORE_HANDOFF,
    SEND_INTENT_RECORDED,
    SERVER_ACKNOWLEDGED,
    OutboundLedger,
)
from relay.relay import RUN, Relay


def _run(coro):
    return asyncio.run(coro)


class PhaseBackend:
    """Minimal directive=run backend with a reclaimable queue + a controllable ack failure."""

    def __init__(self) -> None:
        self.directive_value = RUN
        self.queue: list[dict] = []
        self.acks: list[dict] = []
        self.status: dict[str, str] = {}
        self.ack_fails = 0                 # first N acks raise BackendError (server-ack failure)
        self.last_retry_after = None

    def enqueue(self, job: dict) -> None:
        self.queue.append(dict(job))

    async def claim(self) -> dict | None:
        if self.directive_value != RUN or not self.queue:
            return None
        return dict(self.queue[0])

    async def directive(self) -> str:
        return self.directive_value

    async def ack(self, oid, status, guid, error) -> None:
        if self.ack_fails > 0:
            self.ack_fails -= 1
            raise BackendError("ack failed")
        self.acks.append({"id": oid, "status": status, "guid": guid, "error": error})
        self.status[oid] = status
        if status in ("sent", "terminal_failed"):
            self.queue = [j for j in self.queue if j["id"] != oid]

    async def heartbeat(self) -> dict:
        return {"ok": True, "directive": self.directive_value}

    async def post_inbound(self, event) -> dict:
        return {"status": "processed"}

    async def upload(self, data, media_type, filename) -> str:
        return "ref"


def _relay(tmp_path, imsg, be, ledger_path=None):
    return Relay(imsg=imsg, backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.005,
                 sent_ledger=OutboundLedger(str(ledger_path or (tmp_path / "sent.json"))))


def _statuses(be):
    return [a["status"] for a in be.acks]


# ------------------------------------------------- 1: crash after send-intent, before imsg invocation


def test_1_crash_after_intent_before_imsg_is_ambiguous(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p1", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)

    async def _crash():                                        # runs AFTER intent record, BEFORE imsg
        raise RuntimeError("process crash")
    r._send_barrier = _crash
    with pytest.raises(RuntimeError):
        _run(r.process_one_outbound())
    # durable phase is send_intent_recorded; imsg was never invoked
    assert r.sent_ledger.phase("p1") == SEND_INTENT_RECORDED and imsg.calls == 0

    # restart + reclaim: AMBIGUOUS -> surfaced terminal, never resent, never reported sent
    imsg2 = InProcessImsg(); r2 = _relay(tmp_path, imsg2, be)
    assert _run(r2.process_one_outbound()) is True
    assert imsg2.calls == 0 and be.acks[-1]["status"] == "terminal_failed"
    assert be.acks[-1]["error"] == "handoff_unknown"


# ------------------------------------------------- 2: imsg rejects before accepting bytes


def test_2_imsg_reject_before_handoff_is_retryable(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p2", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(send_rejects=1); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert r.sent_ledger.phase("p2") == SEND_FAILED_BEFORE_HANDOFF
    assert be.acks[-1]["status"] == "retryable_failed" and r.sent_ledger.is_retryable("p2")


# ------------------------------------------------- 3: imsg subprocess exits before handoff


def test_3_imsg_exit_before_handoff_is_retryable(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p3", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(exit_before_handoff=True); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert r.sent_ledger.phase("p3") == SEND_FAILED_BEFORE_HANDOFF
    assert be.acks[-1]["status"] == "retryable_failed"


# ------------------------------------------------- 4: crash during imsg invocation, unknown outcome


def test_4_crash_during_imsg_is_unknown(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p4", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(crash_unknown=True); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert r.sent_ledger.phase("p4") == HANDOFF_OUTCOME_UNKNOWN
    assert be.acks[-1]["status"] == "terminal_failed" and be.acks[-1]["error"] == "handoff_unknown"


# ------------------------------------------------- 5: imsg succeeds, then server ack fails


def test_5_send_succeeds_then_ack_fails_recovers_without_resend(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p5", "to": "+1555", "text": "hi"}); be.ack_fails = 1
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())                            # send ok, ack fails -> phase handed_to_imsg
    assert imsg.calls == 1 and r.sent_ledger.phase("p5") == HANDED_TO_IMSG
    # reclaim: re-ack sent, imsg NOT re-invoked (server-ack failure never duplicates the send)
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1 and be.status["p5"] == "sent"
    assert r.sent_ledger.phase("p5") == SERVER_ACKNOWLEDGED


# ------------------------------------------------- 6: restart after EACH durable phase


@pytest.mark.parametrize("phase,expect_send,expect_ack", [
    (SEND_INTENT_RECORDED, False, "terminal_failed"),
    (HANDOFF_OUTCOME_UNKNOWN, False, "terminal_failed"),
    (HANDED_TO_IMSG, False, "sent"),
    (SERVER_ACKNOWLEDGED, False, "sent"),
    (SEND_FAILED_BEFORE_HANDOFF, True, "sent"),
    (BLOCKED_BEFORE_SEND, True, "sent"),
])
def test_6_restart_recovery_is_derived_from_durable_phase(tmp_path, phase, expect_send, expect_ack):
    """For every durable phase, a fresh relay reclaiming the row does the phase-correct thing."""
    ledger_path = tmp_path / "sent.json"
    seed = OutboundLedger(str(ledger_path)); seed.record("p6", phase, guid="g-old")
    be = PhaseBackend(); be.enqueue({"id": "p6", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be, ledger_path=ledger_path)
    _run(r.process_one_outbound())
    assert (imsg.calls == 1) == expect_send                   # retryable phases (re)send; others never do
    assert be.acks[-1]["status"] == expect_ack


# ------------------------------------------------- 7: no blind resend after handed_to_imsg


def test_7_no_blind_resend_after_handed_to_imsg(tmp_path):
    ledger_path = tmp_path / "sent.json"
    OutboundLedger(str(ledger_path)).record("p7", HANDED_TO_IMSG, guid="g7")
    be = PhaseBackend(); be.enqueue({"id": "p7", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be, ledger_path=ledger_path)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and be.acks[-1]["status"] == "sent" and be.acks[-1]["guid"] == "g7"


# ------------------------------------------------- 8: safe retry after definite pre-handoff failure


def test_8_safe_retry_after_pre_handoff_failure(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p8", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(send_rejects=1); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())                            # declined -> retryable
    assert imsg.calls == 1 and be.acks[-1]["status"] == "retryable_failed"
    # retry: imsg now accepts -> sent exactly once more
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 2 and imsg.sent and be.status["p8"] == "sent"


# ------------------------------------------------- 9: no false "sent" for ambiguous outcomes


def test_9_no_false_sent_for_ambiguous_outcome(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p9", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(crash_unknown=True); r = _relay(tmp_path, imsg, be)
    _run(r.process_one_outbound())
    assert all(a["status"] != "sent" for a in be.acks)        # ambiguous is NEVER reported confirmed sent


# ------------------------------------------------- 10: pause before final gate stays retryable


def test_10_pause_before_final_gate_is_retryable(tmp_path):
    be = PhaseBackend(); be.enqueue({"id": "p10", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)

    async def _prep(job):
        be.directive_value = "pause_outbound"                 # pause arrives during prep, before final gate
    r._prepare_hook = _prep
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and r.sent_ledger.phase("p10") == BLOCKED_BEFORE_SEND
    assert r.sent_ledger.is_retryable("p10")
    # resume -> reclaimed and sent exactly once (clear the re-pausing prep hook)
    be.directive_value = RUN; r._prepare_hook = None
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1 and be.status["p10"] == "sent"


# ------------------------------------------------- 11: pause in the irreducible post-gate window


def test_11_pause_in_irreducible_window_is_classified_sent(tmp_path):
    """A pause landing AFTER the final gate but before/at the imsg call cannot be honored — the send
    completes and is accurately classified `sent` (not lost, not ambiguous)."""
    be = PhaseBackend(); be.enqueue({"id": "p11", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be)

    async def _barrier():
        be.directive_value = "pause_outbound"                 # too late: intent already recorded
    r._send_barrier = _barrier
    assert _run(r.process_one_outbound()) is True
    assert imsg.calls == 1 and r.sent_ledger.phase("p11") == SERVER_ACKNOWLEDGED
    assert be.status["p11"] == "sent"


# ------------------------------------------------- 12: legacy ledger migration / compatibility


def test_12_legacy_ledger_entries_migrate_to_handed_to_imsg(tmp_path):
    """An existing A2 boolean ledger ({"processed": [...]}) is migrated so those ids are treated as
    already-handed (no sudden resend), rewritten in the phase format."""
    path = tmp_path / "sent.json"
    path.write_text(json.dumps({"processed": ["old-1", "old-2"]}))   # legacy format on disk
    led = OutboundLedger(str(path))
    assert led.phase("old-1") == HANDED_TO_IMSG and led.phase("old-2") == HANDED_TO_IMSG
    assert json.loads(path.read_text()).get("version") == 2          # rewritten in the new format

    # a reclaim of a migrated id re-acks sent and never resends
    be = PhaseBackend(); be.enqueue({"id": "old-1", "to": "+1555", "text": "hi"})
    imsg = InProcessImsg(); r = _relay(tmp_path, imsg, be, ledger_path=path)
    _run(r.process_one_outbound())
    assert imsg.calls == 0 and be.acks[-1]["status"] == "sent"
