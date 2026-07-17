"""Per-intake routing telemetry.

Every intake now goes through a small router (transcribe -> extract, with a bounded fallback), and
the user's requirement is that we can answer, per document: which provider/model ran, how long it
took, how many tokens it burned, what it cost, whether it fell back and why, and whether the result
was actually grounded. That is what this module records. It holds NO student content — only
provider/model identifiers, counts, and short reason strings — so a telemetry row is always safe to
log, ship to metrics, or attach to an eval result.

Cost is an ESTIMATE. Prices move and this repo's rule is never to hard-trust a memorized number, so
the per-model rates below are documented defaults, overridable by env, and clearly labelled
approximate. Featherless is a flat-rate subscription: its marginal per-token cost is ~0, so we still
count tokens (for the eval set and capacity planning) but estimate $0 spend.
"""

from __future__ import annotations

import dataclasses
import os


@dataclasses.dataclass(frozen=True)
class ModelRate:
    """Approximate USD per 1M tokens. Verify against live pricing before trusting the dollar figure."""

    input_per_m: float
    output_per_m: float
    flat_rate: bool = False  # True => marginal cost ~0 (subscription), tokens still counted


# Documented defaults. Override any value with BRUCE_RATE_<MODEL>_{IN,OUT} (USD per 1M tokens).
# These are estimates for the routing metric, NOT a billing source of truth.
_DEFAULT_RATES: dict[str, ModelRate] = {
    "gpt-5.4-mini": ModelRate(input_per_m=0.25, output_per_m=2.00),
    # Featherless open models bill flat-rate; marginal token cost is ~0.
    "Qwen/Qwen3-32B": ModelRate(input_per_m=0.0, output_per_m=0.0, flat_rate=True),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": ModelRate(input_per_m=0.0, output_per_m=0.0, flat_rate=True),
}


def _rate_for(model: str) -> ModelRate:
    base = _DEFAULT_RATES.get(model, ModelRate(input_per_m=0.0, output_per_m=0.0))
    key = model.replace("/", "_").replace("-", "_").replace(".", "_").upper()
    try:
        in_m = float(os.environ.get(f"BRUCE_RATE_{key}_IN", base.input_per_m))
        out_m = float(os.environ.get(f"BRUCE_RATE_{key}_OUT", base.output_per_m))
    except ValueError:
        return base
    return ModelRate(input_per_m=in_m, output_per_m=out_m, flat_rate=base.flat_rate)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    r = _rate_for(model)
    if r.flat_rate:
        return 0.0
    return (input_tokens / 1_000_000) * r.input_per_m + (output_tokens / 1_000_000) * r.output_per_m


@dataclasses.dataclass
class IntakeTelemetry:
    """One row per intake, covering the WHOLE pipeline. Safe to log/persist: identifiers, counts,
    and short reasons only.

    An image or scanned-PDF intake runs TWO models: a vision transcriber (OpenAI) and a structured
    extractor (Featherless, or the OpenAI fallback). ``provider``/``model`` name the EXTRACTOR — the
    model that made the structured decision, and the axis routing is chosen on. The transcription
    leg is recorded separately in the ``transcriber_*`` fields so it is never hidden: total latency
    and total cost sum both legs.
    """

    doc_type: str  # image | screenshot | pdf_text | pdf_scanned | text
    provider: str  # extractor provider: openai | featherless | local
    model: str  # extractor model id, or "none" for an empty direct-text intake
    # "production" (a synchronous student-facing request — must be OpenAI) or "evaluation" (offline
    # eval/batch, where Featherless is allowed). Lets dashboards separate the two cleanly.
    traffic: str = "production"
    latency_ms: int = 0  # extractor leg only; see total_latency_ms
    input_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    # "grounded" (all surfaced deadlines verified against source), "partial" (some dropped),
    # "ungrounded" (nothing survived the source-span check), or "n/a".
    grounding_result: str = "n/a"
    # None on the happy path; otherwise why the OpenAI fallback ran: "invalid_output" |
    # "failed_grounding" | "complexity". A silent provider swap is never allowed — if it fell
    # back, this field names the reason and the model field reflects who actually answered.
    fallback_reason: str | None = None
    # Vision transcription leg (image / scanned-PDF only). None when no transcription ran.
    transcriber_provider: str | None = None
    transcriber_model: str | None = None
    transcriber_latency_ms: int = 0
    transcriber_input_tokens: int = 0
    transcriber_output_tokens: int = 0

    @property
    def extractor_cost_usd(self) -> float:
        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)

    @property
    def transcriber_cost_usd(self) -> float:
        if not self.transcriber_model:
            return 0.0
        return estimate_cost_usd(
            self.transcriber_model, self.transcriber_input_tokens, self.transcriber_output_tokens
        )

    @property
    def est_cost_usd(self) -> float:
        """Total estimated spend for the whole intake — extractor + transcription legs."""
        return self.extractor_cost_usd + self.transcriber_cost_usd

    @property
    def total_latency_ms(self) -> int:
        """Wall-clock the student waits — both model legs."""
        return self.latency_ms + self.transcriber_latency_ms

    def as_dict(self) -> dict:
        return {
            "doc_type": self.doc_type,
            "traffic": self.traffic,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retries": self.retries,
            "grounding_result": self.grounding_result,
            "fallback_reason": self.fallback_reason,
            "transcriber_provider": self.transcriber_provider,
            "transcriber_model": self.transcriber_model,
            "transcriber_latency_ms": self.transcriber_latency_ms,
            "transcriber_input_tokens": self.transcriber_input_tokens,
            "transcriber_output_tokens": self.transcriber_output_tokens,
            "total_latency_ms": self.total_latency_ms,
            "est_cost_usd": round(self.est_cost_usd, 6),
        }
