"""Trace correctness — a baseline you cannot trust is worse than no baseline.

Every percentile in the next four PRs is computed from these records. If a trace can report a stage that
did not happen, or a duration of zero for work that was skipped, or timestamps out of order, then the
"before" number in a before-and-after comparison is fiction and the optimisation that follows it is
justified by fiction.

So this file tests the instrument, not the system.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from bruce_engine import turn_trace as tt


@pytest.fixture(autouse=True)
def _clean():
    tt.clear()
    tt.reset_process_state()
    yield
    tt.clear()


def _trace(**kw):
    return tt.start(user_id="u-1", conversation_id="c-1", message_id="m-1", **kw)


# --- ordering + completeness -----------------------------------------------------------------------------

def test_timestamps_are_monotonic_within_one_trace():
    t = _trace()
    for stage in ("trusted_input_ready", "state_reads_started", "router_started", "router_finished",
                  "model_started", "model_finished", "response_ready"):
        t.mark(stage)
    t.finish()
    assert tt.monotonic_violations(t) == []
    observed = [t.marks[s] for s in tt.STAGES if s in t.marks]
    assert observed == sorted(observed)


def test_durations_use_a_monotonic_clock_not_the_wall_clock():
    """A wall clock can step backwards — NTP, a leap second, a VM resuming. Subtracting two wall
    readings to get a duration is how a negative latency reaches a dashboard."""
    import inspect
    src = inspect.getsource(tt.TurnTrace.mark)
    assert "perf_counter" in src
    assert "datetime.now" in src, "correlation timestamps must still be wall-clock"
    t = _trace()
    t.mark("router_started")
    assert isinstance(t.wall["router_started"], datetime)
    assert isinstance(t.marks["router_started"], float)


def test_every_completed_turn_has_completed_at():
    t = _trace().finish()
    assert t.at("completed") is not None and t.total_ms is not None


def test_errors_still_finalize_the_trace():
    """The turn that fails is exactly the turn whose timing matters. A trace that only completes on
    success measures a system that never has incidents."""
    t = _trace()
    t.mark("router_started")
    t.finish(error_class="ProviderTimeout")
    assert t.error_class == "ProviderTimeout" and t.total_ms is not None
    assert t.absent_stages["model_started"] == tt.NOT_REACHED


# --- absence is explicit ---------------------------------------------------------------------------------

def test_a_tool_free_turn_does_not_pretend_a_tool_stage_happened():
    """The failure this prevents: an unset timestamp defaulting to zero, so a conversation reports a
    0ms tool call and the tool-latency percentile is computed over turns that never called a tool."""
    t = _trace()
    t.mark("model_started")
    t.mark("model_finished")
    t.absent("tool_started", tt.NOT_APPLICABLE)
    t.absent("tool_finished", tt.NOT_APPLICABLE)
    t.finish()
    assert t.at("tool_started") is None
    assert t.duration("tool_started", "tool_finished") is None, "a skipped stage reported a duration"
    assert t.absent_stages["tool_started"] == tt.NOT_APPLICABLE
    assert "tool_started" not in t.stage_breakdown()


def test_a_missing_stage_is_never_a_zero_duration():
    t = _trace()
    t.mark("router_started")
    t.finish()
    for stage in tt.STAGES:
        assert t.at(stage) is None or t.at(stage) >= 0.0
    assert t.duration("model_started", "model_finished") is None


def test_every_unrecorded_stage_carries_a_reason_after_finish():
    t = _trace().finish()
    for stage in tt.STAGES:
        assert stage in t.marks or stage in t.absent_stages, f"{stage} is silently missing"


def test_not_applicable_and_not_reached_are_different_answers():
    """"this path has no tool" and "the turn died before the tool" need different fixes, and a single
    null cannot tell them apart."""
    t = _trace()
    t.absent("tool_started", tt.NOT_APPLICABLE)
    t.finish()
    assert t.absent_stages["tool_started"] == tt.NOT_APPLICABLE
    assert t.absent_stages["model_started"] == tt.NOT_REACHED


# --- model + provider stages -----------------------------------------------------------------------------

def test_a_model_call_records_start_and_finish_or_a_failure():
    ok = _trace()
    ok.mark("model_started")
    ok.mark("model_finished")
    ok.finish()
    assert ok.duration("model_started", "model_finished") is not None

    failed = _trace()
    failed.mark("model_started")
    failed.absent("model_finished", tt.FAILED)
    failed.finish(error_class="ModelHTTPError")
    assert failed.absent_stages["model_finished"] == tt.FAILED
    assert failed.duration("model_started", "model_finished") is None


def test_a_provider_call_records_start_and_finish_or_a_failure():
    t = _trace()
    t.mark("tool_started")
    t.absent("tool_finished", tt.FAILED)
    t.finish(error_class="CalendarError")
    assert t.absent_stages["tool_finished"] == tt.FAILED


def test_relay_completion_requires_a_real_guid():
    """Enqueueing is not delivering. Marking `relay_guid_received` at enqueue time would report a
    delivery latency the system never observed."""
    t = _trace()
    t.mark("relay_send_started")
    t.absent("relay_guid_received", tt.NOT_APPLICABLE)
    t.finish()
    assert t.at("relay_guid_received") is None
    assert t.duration("relay_send_started", "relay_guid_received") is None


def test_background_enqueue_ends_after_the_durable_commit():
    t = _trace()
    t.mark("tool_started")
    time.sleep(0.002)
    t.mark("tool_finished")          # the durable commit
    t.mark("response_ready")         # the acknowledgement, after it
    t.finish()
    assert t.at("response_ready") > t.at("tool_finished")
    assert tt.monotonic_violations(t) == []


# --- identity + linkage ----------------------------------------------------------------------------------

def test_one_inbound_message_produces_one_root_trace():
    a, b = _trace(), _trace()
    assert a.trace_id != b.trace_id
    assert a.parent_trace_id is None and b.parent_trace_id is None


def test_a_wake_links_to_the_turn_that_planned_it():
    """A provider wake is a separate latency event. Averaging it into the turn that caused it would
    make an eight-hour email wait look like an eight-hour response time."""
    root = _trace().finish()
    wake = tt.start(user_id="u-1", parent_trace_id=root.trace_id)
    wake.finish()
    assert wake.parent_trace_id == root.trace_id and wake.trace_id != root.trace_id


def test_retries_remain_distinguishable():
    root = _trace().finish()
    retry = tt.start(user_id="u-1", message_id="m-1", parent_trace_id=root.trace_id, attempt=2)
    retry.finish()
    assert retry.attempt == 2 and retry.trace_id != root.trace_id


def test_a_stage_marked_twice_keeps_the_first_time():
    """A retry loop re-marking `model_started` would otherwise report only the last attempt and make a
    slow turn look fast."""
    t = _trace()
    t.mark("model_started")
    first = t.marks["model_started"]
    time.sleep(0.003)
    t.mark("model_started")
    assert t.marks["model_started"] == first


# --- privacy ---------------------------------------------------------------------------------------------

def test_no_user_content_appears_in_a_persisted_trace():
    t = _trace()
    t.mark("router_started")
    t.finish()
    payload = t.as_dict()
    tt.assert_content_free(payload)
    flat = str(payload)
    for leak in ("hey coach", "ms delgado", "america/chicago"):
        assert leak not in flat


def test_the_content_free_check_actually_catches_a_leak():
    """A guard nobody has seen fail is a guard nobody knows works."""
    with pytest.raises(AssertionError):
        tt.assert_content_free({"trace_id": "x", "message_text": "hey coach"})


# --- failure handling ------------------------------------------------------------------------------------

def test_tracing_failures_are_counted_not_swallowed():
    before = tt.TRACING_FAILURES
    t = _trace()
    t.mark("not_a_real_stage")
    assert tt.TRACING_FAILURES == before + 1
    assert "not_a_real_stage" not in t.marks


def test_a_tracing_fault_never_breaks_the_turn():
    class Exploding:
        def mark(self, stage):
            raise RuntimeError("boom")
    before = tt.TRACING_FAILURES
    tt.guard(Exploding(), "router_started")          # must not raise
    assert tt.TRACING_FAILURES == before + 1


def test_guard_and_note_tolerate_a_missing_trace():
    tt.guard(None, "router_started")
    tt.note(None, execution_path="x")


# --- cold vs warm ----------------------------------------------------------------------------------------

def test_cold_is_a_property_of_the_process_and_only_the_first_turn_is_cold():
    """Reporting cold and warm together is how a p95 hides a 4.5s cold start — measured on this system,
    in Cloud Run, during the semantic-router work."""
    first, second, third = _trace(), _trace(), _trace()
    assert first.cold is True
    assert second.cold is False and third.cold is False


def test_recent_can_separate_cold_from_warm_and_path_from_path():
    a = _trace()
    a.execution_path = "fast_conversation"
    tt.record(a.finish())
    b = _trace()
    b.execution_path = "direct_action"
    tt.record(b.finish())
    assert len(tt.recent(cold=True)) == 1
    assert len(tt.recent(path="direct_action")) == 1
    assert len(tt.recent()) == 2


# --- the arithmetic --------------------------------------------------------------------------------------

def test_stage_breakdown_only_contains_stages_that_happened():
    t = _trace()
    t.mark("router_started")
    time.sleep(0.002)
    t.mark("router_finished")
    t.finish()
    breakdown = t.stage_breakdown()
    assert "model_started" not in breakdown
    assert breakdown["router_started"] > 0
    assert abs(sum(breakdown.values()) - t.total_ms) < 1.0, "the gaps do not sum to the total"


def test_percentiles_report_the_sample_size_and_do_not_interpolate():
    assert tt.percentiles([]) == {"n": 0}
    p = tt.percentiles([float(i) for i in range(1, 101)])
    assert p["n"] == 100 and p["p50"] == 50 and p["p95"] == 95 and p["p99"] == 99 and p["max"] == 100


# --- overhead --------------------------------------------------------------------------------------------

def test_tracing_overhead_is_negligible():
    """Instrumentation on the hot path of every turn has to be cheap enough that the baseline it produces
    is not mostly itself."""
    n = 2000
    started = time.perf_counter()
    for _ in range(n):
        t = tt.start(user_id="u-1")
        for stage in ("trusted_input_ready", "router_started", "router_finished", "response_ready"):
            t.mark(stage)
        t.finish()
    per_turn_us = ((time.perf_counter() - started) / n) * 1_000_000
    assert per_turn_us < 200, f"{per_turn_us:.1f}us per traced turn"


def test_tracing_can_be_switched_off_without_a_deploy(monkeypatch):
    monkeypatch.setenv("BRUCE_TURN_TRACE_OFF", "1")
    assert tt.enabled() is False
    tt.record(_trace().finish())
    assert tt.recent() == []


def test_tracing_is_on_by_default():
    assert tt.enabled() is True
