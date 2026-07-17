"""Production must never touch Featherless.

The routing rule, pinned: every synchronous student-facing step runs on OpenAI. Featherless is
offline-only and disabled by default — a stray production caller must fail LOUDLY, never silently
take the high-tail-latency serverless path (steady ~34s, cold-start 252s). Bruce also runs with no
Featherless key at all.
"""

from __future__ import annotations

import asyncio

import pytest

from bruce_engine import extraction, llm
from bruce_engine.intake_providers import (
    ExtractResult,
    FeatherlessExtractor,
    OpenAIExtractor,
    production_drafter,
    production_extractor,
)
from bruce_engine.models import ExtractedIntake, IntakeSourceKind
from bruce_engine.provider_status import ProviderUnavailable


@pytest.fixture(autouse=True)
def _no_featherless_flag(monkeypatch):
    monkeypatch.delenv("BRUCE_ENABLE_FEATHERLESS", raising=False)


def test_production_extractor_and_drafter_are_openai():
    assert production_extractor().provider == "openai"
    assert production_extractor().model == "gpt-5.4-mini"
    assert production_drafter().provider == "openai"
    assert production_drafter().model == "gpt-5.4-mini"


def test_all_production_role_models_are_openai():
    assert llm.MODEL_EXTRACTION == llm.MODEL_DRAFTING == llm.MODEL_VISION == llm.MODEL_VERIFICATION == "gpt-5.4-mini"


def test_featherless_is_disabled_by_default():
    assert llm.featherless_enabled() is False
    with pytest.raises(RuntimeError, match="disabled"):
        llm.featherless_extraction_model()
    with pytest.raises(RuntimeError, match="disabled"):
        FeatherlessExtractor()._model()


def test_featherless_only_enables_with_the_explicit_flag(monkeypatch):
    monkeypatch.setenv("BRUCE_ENABLE_FEATHERLESS", "1")
    monkeypatch.setenv("FEATHERLESS_API_KEY", "fk")
    assert llm.featherless_enabled() is True
    llm.featherless_extraction_model()  # no raise now


def test_bruce_runs_without_a_featherless_key(monkeypatch):
    """Production extraction must construct with no FEATHERLESS_API_KEY present — only OpenAI's."""
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    production_extractor()._model()  # would raise if it needed a Featherless key


def _canned(monkeypatch):
    """Patch the OpenAI extractor to a no-network stub, and make ANY Featherless construction fail."""
    async def fake_extract(self, text, source_kind):
        return ExtractResult(
            intake=ExtractedIntake(source_kind=source_kind),
            provider="openai", model="gpt-5.4-mini",
            input_tokens=10, output_tokens=5, latency_ms=42,
        )

    monkeypatch.setattr(OpenAIExtractor, "extract", fake_extract)

    def boom(*a, **k):
        raise AssertionError("Featherless was constructed on a production path")

    monkeypatch.setattr(llm, "featherless", boom)


def test_synchronous_text_intake_never_calls_featherless(monkeypatch):
    _canned(monkeypatch)
    intake, telem = asyncio.run(extraction.extract_from_text_traced("Applications due May 1, 2026."))
    assert telem.provider == "openai" and telem.traffic == "production"


def test_intake_hard_timeout_is_a_recoverable_failure(monkeypatch):
    """A slow extraction must fail as a bounded, retryable outage — never an unbounded UI hang."""
    async def slow_extract(self, text, source_kind):
        await asyncio.sleep(0.2)
        raise AssertionError("should have timed out before returning")

    monkeypatch.setattr(OpenAIExtractor, "extract", slow_extract)
    monkeypatch.setattr(extraction, "INTAKE_HARD_TIMEOUT_S", 0.05)
    with pytest.raises(ProviderUnavailable) as e:
        asyncio.run(extraction.extract_from_text_traced("some text with a deadline May 1 2026"))
    assert e.value.status_code == 504 and "budget" in e.value.reason
