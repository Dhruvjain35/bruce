"""M1 — one canonical decision truth.

OBSERVED IN PRODUCTION during the P0 verification: mission f5f32407 was cancelled by a refusal and
correctly moved to status=cancelled / phase=blocked / short_status=approval_rejected, while the embedded
`goal.decision.status` still read "pending". Two copies of the same fact, one of them wrong.

Nothing consumed the embedded field, so it was never a live safety bug — it was a trap, and the exact
divergent-worldview class that has now caused several defects in this system. The fix removes the
duplicate at the source rather than keeping two copies in sync, and adds ONE accessor so future callers
cannot reach into the JSON by accident.
"""

from __future__ import annotations

import pytest

from bruce_engine.mission_kernel import decision_status

PENDING = {"phase": "awaiting_approval", "status": "running", "short_status": "awaiting ok: add x"}
REJECTED = {"phase": "blocked", "status": "cancelled", "short_status": "approval_rejected"}
SUCCEEDED = {"phase": "succeeded", "status": "done", "short_status": "verified"}


def test_pending_decision_reads_pending():
    assert decision_status(PENDING) == "pending"


def test_refused_decision_reads_rejected():
    assert decision_status(REJECTED) == "rejected"


def test_completed_decision_is_resolved_not_pending():
    assert decision_status(SUCCEEDED) == "resolved"


def test_a_legacy_row_with_the_stale_embedded_status_still_reads_correctly():
    """THE regression. A row written before the fix carries goal.decision.status == "pending" even though
    the mission is cancelled. The canonical read must ignore the JSON and use the row."""
    legacy = dict(REJECTED, goal={"decision": {"type": "approve_calendar_create", "status": "pending"}})
    assert decision_status(legacy) == "rejected"


def test_cancelled_status_wins_even_if_the_phase_was_not_moved():
    assert decision_status({"phase": "awaiting_approval", "status": "cancelled",
                            "short_status": "x"}) == "rejected"


def test_accepts_an_orm_row_not_only_a_dict():
    class _Row:
        phase, status, short_status = "blocked", "cancelled", "approval_rejected"
    assert decision_status(_Row()) == "rejected"


def test_new_offers_no_longer_embed_a_duplicate_status():
    """The offer path must stop writing a second copy of the truth."""
    import inspect
    from bruce_engine import mission_kernel
    src = inspect.getsource(mission_kernel.create_pending_calendar_approval)
    assert '"status": "pending"' not in src, "the embedded decision status was reintroduced"
    assert '"decision": {"type": "approve_calendar_create"}' in src


@pytest.mark.parametrize("phase,status", [
    ("blocked", "cancelled"), ("succeeded", "done"), ("failed", "failed"),
])
def test_no_resolved_decision_ever_reads_pending(phase, status):
    """The property that matters: a closed decision must never look open to any caller."""
    assert decision_status({"phase": phase, "status": status, "short_status": ""}) != "pending"
