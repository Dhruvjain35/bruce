"""Fast response composer (Phase F) — turn VERIFIED structured runtime state into Bruce's short reply.

Two layers, deterministic-first:
  * render_deterministic(spec) is the always-available, fact-locked authority (sub-millisecond). EVERY fact —
    "sent ✅", "done, chess is moved to 9", "school or personal?", "gmail rejected it, so nothing was sent" —
    comes from the runtime's ResponseSpec, never from a model. This is what ships if the model is off/slow.
  * a CHEAP response model may only REPHRASE that text in Bruce's voice within a hard latency budget. It never
    decides whether an action happened / succeeded / verified / connected — it only expresses the structured
    truth it was handed, and its output is fact-checked + run through the completion-truth guard before it
    ships; on timeout / error / any fabrication it falls back to the deterministic text.

The response model receives ONLY the ResponseSpec's structured fields — no credentials, no full provider
payloads, no unrelated history, no capability list, no chain-of-thought.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from . import response_composer
from .conversation_style import VoiceProfile, assert_facts_preserved, enforce_no_dashes, strip_redundant_offer

log = logging.getLogger("bruce.response_render")   # content-free: kinds/latency only

RESPONSE_TIMEOUT_S = float(os.environ.get("BRUCE_RESPONSE_TIMEOUT_S", "0.7"))
_TRUTHY = {"1", "true", "yes", "on"}


def response_model_enabled() -> bool:
    return os.environ.get("BRUCE_RESPONSE_MODEL", "").strip().lower() in _TRUTHY


class ResponseKind(str, Enum):
    verified_success = "verified_success"   # a write the runtime VERIFIED
    pending_decision = "pending_decision"   # one precise choice is needed
    retrying = "retrying"                   # a transient failure; runtime is retrying (nothing done yet)
    terminal_failure = "terminal_failure"   # honestly gave up (nothing done)
    disconnected = "disconnected"           # capability live but the provider isn't connected
    unsupported = "unsupported"             # not live yet
    answer = "answer"                       # a plain chat/tutoring answer (already fact-checked upstream)


@dataclass(frozen=True)
class ResponseSpec:
    """The runtime's VERIFIED structured truth — the only thing the response layer may express."""
    kind: ResponseKind
    action: str | None = None               # "sent" | "moved" | "deleted" | "added" | ...
    subject: str | None = None              # the thing: "chess", "the test email"
    detail: str | None = None               # a verified detail: "to 9pm", "to coach@school.edu"
    decision_options: tuple[str, ...] = ()  # for pending_decision
    blocker: str | None = None              # for retrying/terminal: "gmail timed out" / "gmail rejected it"
    provider: str | None = None             # for disconnected/unsupported honesty: "gmail" / "google calendar"
    proof: str | None = None                # optional proof ref (id/link), only when useful
    freeform: str | None = None             # for `answer` — the model reply, already fact-checked upstream
    verified: bool = False                  # the runtime's authoritative verification bit


def render_deterministic(spec: ResponseSpec, *, profile: VoiceProfile | None = None) -> str:
    """The authoritative fact-locked text. Result-first, concise, no em dash, never a claim the spec didn't
    carry. `verified_success` is the ONLY kind that may say done/✅ — and only when spec.verified is True."""
    k = spec.kind
    subj = (spec.subject or "that").strip()
    if k is ResponseKind.verified_success and spec.verified:
        act = (spec.action or "done").strip()
        if act == "sent":
            base = "sent ✅"
        elif act in ("moved", "updated", "rescheduled", "changed"):
            base = f"done, {subj} is {act} {spec.detail}".rstrip() if spec.detail else f"done, {subj} {act} ✅"
        elif act in ("deleted", "removed", "canceled", "cancelled"):
            base = f"done, deleted {subj} ✅"
        elif act in ("added", "created", "scheduled", "booked"):
            base = f"done, {subj} is on ur calendar for {spec.detail} ✅" if spec.detail else f"added {subj} ✅"
        else:
            base = f"done, {subj} {act}".rstrip() + " ✅"
        if spec.proof:
            base += f" ({spec.proof})"
        return enforce_no_dashes(base)
    if k is ResponseKind.pending_decision:
        opts = " or ".join(o for o in spec.decision_options if o) or "which one"
        return enforce_no_dashes(f"{opts}?")
    if k is ResponseKind.retrying:
        b = spec.blocker or "that didn't go through"
        return enforce_no_dashes(f"{b}, nothing {spec.action or 'done'} yet. i'm retrying")
    if k is ResponseKind.terminal_failure:
        b = spec.blocker or "it didn't go through"
        tail = f"so nothing was {spec.action}" if spec.action else "so nothing happened"
        return enforce_no_dashes(f"{b}, {tail}")
    if k is ResponseKind.disconnected:
        p = spec.provider or "that"
        return enforce_no_dashes(f"ur {p} isn't connected yet, so i can't do that. connect it and i'll handle it")
    if k is ResponseKind.unsupported:
        return enforce_no_dashes("i can't do that one yet")
    # answer: freeform is already fact-checked upstream; just voice it (strip a trailing redundant offer)
    return enforce_no_dashes(strip_redundant_offer((spec.freeform or "").strip()))


class ResponseModel(Protocol):
    """A CHEAP model that only rephrases the deterministic text in Bruce's voice — no new facts."""
    async def rephrase(self, *, deterministic: str, spec: ResponseSpec, profile: VoiceProfile | None) -> str: ...


def _safe(candidate: str, deterministic: str, spec: ResponseSpec) -> bool:
    """The model output ships ONLY if it changes no fact (same numbers/emails/✅) and invents no completion."""
    if not candidate or not candidate.strip():
        return False
    try:
        assert_facts_preserved(deterministic, candidate)     # no dropped/altered number/date/email/price/url
    except Exception:
        return False
    if "✅" in candidate and "✅" not in deterministic:        # never mint a receipt the runtime didn't
        return False
    # completion-truth: a non-verified-success reply may never claim an action landed
    if spec.kind is not ResponseKind.verified_success and response_composer.claims_action_completion(candidate):
        return False
    return True


async def compose(spec: ResponseSpec, *, profile: VoiceProfile | None = None, model: ResponseModel | None = None,
                  timeout_s: float | None = None) -> tuple[str, bool, float]:
    """Compose the reply. Returns (text, used_model, latency_ms). Deterministic text is always the floor: the
    model only gets to improve phrasing within the budget, and only if its output is fact-safe."""
    t0 = time.perf_counter()
    det = render_deterministic(spec, profile=profile)
    if model is None:
        return det, False, (time.perf_counter() - t0) * 1000.0
    try:
        cand = await asyncio.wait_for(model.rephrase(deterministic=det, spec=spec, profile=profile),
                                      timeout=timeout_s if timeout_s is not None else RESPONSE_TIMEOUT_S)
    except Exception:
        log.info("response fell_back=timeout_or_error kind=%s", spec.kind.value)
        return det, False, (time.perf_counter() - t0) * 1000.0
    ms = (time.perf_counter() - t0) * 1000.0
    if not _safe(cand, det, spec):
        log.info("response fell_back=unsafe kind=%s", spec.kind.value)
        return det, False, ms
    return enforce_no_dashes(cand.strip()), True, ms


class CheapResponseModel:
    """The production rephraser (gated by BRUCE_RESPONSE_MODEL) — a fast call that voices the deterministic
    text, handed ONLY the deterministic string (no credentials/payloads/history/capabilities/CoT). Its output
    is fact-checked + completion-guarded by compose() before it ships; a slow call is dropped by the timeout."""
    provider = "openai"

    async def rephrase(self, *, deterministic: str, spec: ResponseSpec, profile: VoiceProfile | None) -> str:
        from pydantic import BaseModel
        from pydantic_ai import Agent, PromptedOutput

        from . import llm

        class _Out(BaseModel):
            text: str

        system = ("Rephrase this into Bruce's casual, warm, lowercase texting voice. Keep EVERY fact exactly "
                  "(numbers, times, emails, names, and a ✅ if present) — add NO new claim, NO 'done'/'sent' "
                  "unless it's already there. 1-3 short bubbles, result first, no em dash, no filler, no "
                  "markdown, no fake enthusiasm.")
        agent = Agent(llm.routing_model(), output_type=PromptedOutput(_Out), system_prompt=system)
        result = await agent.run(deterministic)
        return result.output.text


# --- naturalness / safety metrics (Phase F) — runnable against a fake or the live model ----------

@dataclass(frozen=True)
class ResponseMetrics:
    n: int
    p50_ms: float
    p95_ms: float
    deterministic_fallback_rate: float
    false_completion_rate: float          # replies claiming a completion the spec didn't carry (MUST be 0)
    avg_bubbles: float
    avg_chars: float
    em_dash_rate: float                   # MUST be 0


async def evaluate_responses(specs: list[ResponseSpec], *, model: ResponseModel | None = None,
                             profile: VoiceProfile | None = None) -> ResponseMetrics:
    lat: list[float] = []
    fell = false_comp = em = 0
    bubbles = chars = 0
    for spec in specs:
        text, used, ms = await compose(spec, model=model, profile=profile)
        lat.append(ms)
        fell += (not used)
        if spec.kind is not ResponseKind.verified_success and response_composer.claims_action_completion(text):
            false_comp += 1
        em += ("—" in text)
        bubbles += (text.count("\n") + 1)
        chars += len(text)
    n = len(specs) or 1
    s = sorted(lat)
    def _p(p):
        return s[max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))] if s else 0.0
    return ResponseMetrics(n=len(specs), p50_ms=_p(50), p95_ms=_p(95),
                           deterministic_fallback_rate=fell / n, false_completion_rate=false_comp / n,
                           avg_bubbles=bubbles / n, avg_chars=chars / n, em_dash_rate=em / n)
