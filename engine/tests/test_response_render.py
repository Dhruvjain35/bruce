"""Fast response composer (Phase F) — the deterministic renderer produces the exact fact-locked replies
(result-first, no em dash, no false completion), it is the sub-ms authoritative floor, and the cheap model
may only rephrase within the latency budget: a timeout, an error, a dropped fact, a minted receipt, or any
fabricated completion ALL fall back to the deterministic text. Fakes stand in for the model."""

from __future__ import annotations

import asyncio
import time

from bruce_engine import response_render as rr
from bruce_engine.response_render import ResponseKind, ResponseSpec


def _run(c):
    return asyncio.run(c)


class FakeModel:
    def __init__(self, text=None, *, delay=0.0, raises=False):
        self._text, self._delay, self._raises = text, delay, raises
    async def rephrase(self, *, deterministic, spec, profile):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("model down")
        return self._text if self._text is not None else deterministic


# --- deterministic renderer matches the spec's examples -------------------------------------------

def test_deterministic_examples():
    assert rr.render_deterministic(ResponseSpec(ResponseKind.verified_success, action="sent", verified=True)) == "sent ✅"
    assert rr.render_deterministic(ResponseSpec(ResponseKind.verified_success, action="moved", subject="chess",
                                                detail="to 9", verified=True)) == "done, chess is moved to 9"
    assert rr.render_deterministic(ResponseSpec(ResponseKind.pending_decision,
                                                decision_options=("school email", "personal"))) == "school email or personal?"
    assert rr.render_deterministic(ResponseSpec(ResponseKind.retrying, blocker="gmail timed out", action="sent")) \
        == "gmail timed out, nothing sent yet. i'm retrying"
    assert rr.render_deterministic(ResponseSpec(ResponseKind.terminal_failure, blocker="gmail rejected it",
                                                action="sent")) == "gmail rejected it, so nothing was sent"
    assert "connect" in rr.render_deterministic(ResponseSpec(ResponseKind.disconnected, provider="gmail"))


def test_deterministic_never_claims_unverified_completion():
    # a non-verified 'success' spec must NOT render a receipt (verified=False)
    txt = rr.render_deterministic(ResponseSpec(ResponseKind.verified_success, action="sent", verified=False))
    assert "✅" not in txt and "sent ✅" != txt        # falls through to the generic (unverified) form


def test_no_em_dash_and_concise():
    for spec in (ResponseSpec(ResponseKind.verified_success, action="sent", verified=True),
                 ResponseSpec(ResponseKind.retrying, blocker="gmail timed out", action="sent")):
        t = rr.render_deterministic(spec)
        assert "—" not in t and t.count("\n") <= 2      # no em dash; at most a few bubbles


def test_deterministic_latency_is_negligible():
    spec = ResponseSpec(ResponseKind.verified_success, action="moved", subject="chess", detail="to 9", verified=True)
    t0 = time.perf_counter()
    for _ in range(1000):
        rr.render_deterministic(spec)
    per_ms = (time.perf_counter() - t0) * 1000.0 / 1000
    assert per_ms < 20.0


# --- the cheap model may only IMPROVE phrasing, never add a fact ----------------------------------

def _sent():
    return ResponseSpec(ResponseKind.verified_success, action="sent", verified=True)


def test_compose_without_model_is_deterministic():
    text, used, _ms = _run(rr.compose(_sent()))
    assert text == "sent ✅" and used is False


def test_compose_uses_a_safe_rephrase():
    text, used, _ms = _run(rr.compose(_sent(), model=FakeModel("sent it ✅")))
    assert used is True and "✅" in text


def test_compose_timeout_falls_back_to_deterministic():
    text, used, _ms = _run(rr.compose(_sent(), model=FakeModel("...", delay=0.5), timeout_s=0.02))
    assert text == "sent ✅" and used is False


def test_compose_error_falls_back():
    text, used, _ms = _run(rr.compose(_sent(), model=FakeModel(raises=True)))
    assert text == "sent ✅" and used is False


def test_model_cannot_mint_a_receipt():
    # a RETRYING spec (nothing sent) — model tries to claim success with a ✅ -> rejected -> deterministic
    spec = ResponseSpec(ResponseKind.retrying, blocker="gmail timed out", action="sent")
    text, used, _ms = _run(rr.compose(spec, model=FakeModel("sent it ✅")))
    assert used is False and "✅" not in text and "retrying" in text


def test_model_cannot_fabricate_completion_on_a_question():
    spec = ResponseSpec(ResponseKind.pending_decision, decision_options=("school", "personal"))
    text, used, _ms = _run(rr.compose(spec, model=FakeModel("done, sent it to your school email ✅")))
    assert used is False and text == "school or personal?"     # fabrication rejected -> deterministic question


def test_model_cannot_drop_a_verified_fact():
    spec = ResponseSpec(ResponseKind.verified_success, action="moved", subject="chess", detail="to 9pm", verified=True)
    # a rephrase that drops the time "9pm" alters a fact -> rejected
    text, used, _ms = _run(rr.compose(spec, model=FakeModel("done, moved chess ✅")))
    assert used is False and "9pm" in text


def test_response_metrics_machinery():
    specs = [ResponseSpec(ResponseKind.verified_success, action="sent", verified=True),
             ResponseSpec(ResponseKind.retrying, blocker="gmail timed out", action="sent"),
             ResponseSpec(ResponseKind.pending_decision, decision_options=("school", "personal")),
             ResponseSpec(ResponseKind.terminal_failure, blocker="gmail rejected it", action="sent")]
    m = _run(rr.evaluate_responses(specs))                    # deterministic (no model)
    assert m.n == 4 and m.deterministic_fallback_rate == 1.0
    assert m.false_completion_rate == 0.0 and m.em_dash_rate == 0.0   # HARD gates
    assert m.avg_bubbles <= 3.0 and m.p95_ms < 20.0                   # concise + fast
