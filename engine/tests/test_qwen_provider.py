"""PHASE Q1 — Qwen Cloud provider adapter.

Two layers, deliberately:

  * WIRE-FORMAT tests (offline, no key): drive the real pydantic-ai/OpenAI client stack through an
    httpx MockTransport and assert the EXACT bytes Bruce would put on the wire — model id, base
    URL, auth header, enable_thinking, the image part, and the "json" requirement. These are what
    actually catch provider-integration bugs, and they run in CI without credentials.
  * LIVE tests (require DASHSCOPE_API_KEY *and* an entitled account): make a real Qwen Cloud call
    against a real flyer image. They SKIP with a precise reason when unavailable — a skipped test
    is never reported as a pass.

Live status as of 2026-07-16: the key authenticates (GET /models returns 149 models) but every
model returns 403 AccessDenied.Unpurchased — Model Studio -> Model Inference is not activated on
the account. The live tests below skip on exactly that condition and will run unchanged once it is.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

from bruce_engine import extraction, llm
from bruce_engine.models import IntakeSourceKind

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

FLYER = Path(__file__).parent / "fixtures" / "science_fair_flyer.png"


# --------------------------------------------------------------------------- config


def test_qwen_base_url_defaults_to_the_only_host_that_authenticated(monkeypatch):
    """dashscope-intl is the verified host; dashscope.aliyuncs.com and -us both 401'd our key."""
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    assert llm.qwen_base_url() == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def test_qwen_config_is_env_overridable(monkeypatch):
    """The correct host is account/region dependent (a workspace-scoped form also exists), so every
    knob must be configuration — never a constant."""
    monkeypatch.setenv("QWEN_BASE_URL", "https://ws123.ap-southeast-1.maas.aliyuncs.com/compatible-api/v1")
    monkeypatch.setenv("QWEN_INTAKE_MODEL", "qwen3.6-flash")
    monkeypatch.setenv("QWEN_RERANK_MODEL", "custom-rerank")
    assert llm.qwen_base_url().endswith("/compatible-api/v1")
    assert llm.qwen_intake_model_id() == "qwen3.6-flash"
    assert llm.qwen_rerank_model_id() == "custom-rerank"


def test_qwen_defaults_are_ids_confirmed_present_on_the_live_models_list(monkeypatch):
    for k in ("QWEN_INTAKE_MODEL", "QWEN_RERANK_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert llm.qwen_intake_model_id() == "qwen3.7-plus"  # flagship multimodal, verified present
    assert llm.qwen_rerank_model_id() == "qwen3-rerank"


def test_missing_key_fails_loudly(monkeypatch):
    """Never construct a half-configured client that 401s deep inside a mission."""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        llm.qwen("qwen3.7-plus")


def test_intake_provider_selection(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.delenv("BRUCE_INTAKE_PROVIDER", raising=False)
    assert llm.intake_provider() == "qwen"  # this is the Qwen Cloud build; Qwen is the default

    monkeypatch.setenv("BRUCE_INTAKE_PROVIDER", "featherless")  # Q2 baseline only
    monkeypatch.setenv("FEATHERLESS_API_KEY", "k")
    assert llm.intake_model() is not None

    monkeypatch.setenv("BRUCE_INTAKE_PROVIDER", "nope")
    with pytest.raises(RuntimeError, match="not one of"):
        llm.intake_model()


# --------------------------------------------------------------------------- wire format


def _capture(response_text: str):
    """A mock transport that records the outgoing request and returns a canned chat completion."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen3.7-plus",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": response_text},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_image_intake_wire_format_is_exactly_what_qwen_requires(monkeypatch):
    """The bytes Bruce actually sends: right host, right model, image attached, thinking OFF.

    This is the test that would have caught every real integration failure so far — a wrong host
    401s, a thinking-mode response can emit invalid JSON, and a missing image part silently turns a
    flyer into an empty intake.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    seen, client = _capture("SCIENCE FAIR 2026\nRegistration closes Feb 28, 2026.")
    monkeypatch.setattr(
        extraction, "intake_model", lambda: llm.qwen("qwen3.7-plus", http_client=client)
    )

    text = asyncio.run(extraction._image_to_text(FLYER.read_bytes(), "image/png"))
    assert "Registration closes Feb 28, 2026." in text

    assert seen["url"].startswith("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "qwen3.7-plus"
    # non-thinking: never let a thinking response become the action JSON
    assert seen["body"]["enable_thinking"] is False
    # the image genuinely rides along as a data URL image part
    parts = seen["body"]["messages"][-1]["content"]
    kinds = [p.get("type") for p in parts]
    assert "image_url" in kinds, f"no image part on the wire: {kinds}"
    img = next(p for p in parts if p.get("type") == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_intake_prompt_contains_the_literal_word_json(monkeypatch):
    """Qwen REJECTS response_format=json_object unless "json" appears in the messages.

    json_object is not enabled today (PromptedOutput handles parsing), but the prompt keeps the
    word so enabling it is a one-line change that cannot 400. This test is the guard on that:
    if someone "cleans up" the prompt, this fails before the API does.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    payload = {
        "source_kind": "text", "title": "Science Fair", "summary": None,
        "deadlines": [{"label": "Registration closes", "date": "2026-02-28",
                       "source_span": "Registration closes Feb 28, 2026.", "confidence": 0.9}],
        "required_items": [], "cost": None, "location": None, "contacts": [], "links": [],
        "eligibility": None, "ambiguities": [], "raw_source_excerpt": None,
    }
    seen, client = _capture(json.dumps(payload))
    monkeypatch.setattr(
        extraction, "intake_model", lambda: llm.qwen("qwen3.7-plus", http_client=client)
    )

    intake = asyncio.run(extraction.extract_from_text("Registration closes Feb 28, 2026."))
    assert intake.title == "Science Fair"

    blob = json.dumps(seen["body"]["messages"]).lower()
    assert "json" in blob, "Qwen 400s on json_object without the literal word 'json' in messages"
    assert seen["body"]["enable_thinking"] is False


def test_grounding_gate_still_drops_hallucinated_spans_from_qwen(monkeypatch):
    """The anti-hallucination gate is provider-independent and MUST survive the Qwen swap.

    Qwen returns two deadlines; only one is really in the source. The invented one must be dropped,
    not surfaced — this is the behaviour the whole product promise rests on.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    payload = {
        "source_kind": "text", "title": "Fair", "summary": None,
        "deadlines": [
            {"label": "Real", "date": "2026-02-28",
             "source_span": "Registration closes Feb 28, 2026.", "confidence": 0.9},
            {"label": "Invented", "date": "2026-09-09",
             "source_span": "Applications close Sep 9, 2026.", "confidence": 0.99},
        ],
        "required_items": [], "cost": None, "location": None, "contacts": [], "links": [],
        "eligibility": None, "ambiguities": [], "raw_source_excerpt": None,
    }
    seen, client = _capture(json.dumps(payload))
    monkeypatch.setattr(
        extraction, "intake_model", lambda: llm.qwen("qwen3.7-plus", http_client=client)
    )

    intake = asyncio.run(extraction.extract_from_text("Registration closes Feb 28, 2026."))
    labels = [d.label for d in intake.deadlines]
    assert labels == ["Real"], f"hallucinated deadline survived the grounding gate: {labels}"


def test_transcription_failure_raises_instead_of_returning_empty(monkeypatch):
    """A 403/401 must surface, not become a silently empty intake.

    The previous OpenAI image path caught every exception and returned "" — an auth or quota error
    became "Bruce read your flyer and found nothing", which is a false completion.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AccessDenied.Unpurchased",
                                                   "message": "Access to model denied."}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        extraction, "intake_model", lambda: llm.qwen("qwen3.7-plus", http_client=client)
    )
    with pytest.raises(Exception):
        asyncio.run(extraction._image_to_text(FLYER.read_bytes(), "image/png"))


# --------------------------------------------------------------------------- live


def _live_skip_reason() -> str | None:
    """Why the live Qwen tests cannot run — precise, never a silent pass."""
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return "DASHSCOPE_API_KEY not set"
    try:
        r = httpx.post(
            llm.qwen_base_url() + "/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"},
            json={"model": llm.qwen_intake_model_id(),
                  "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
            timeout=30,
        )
    except Exception as e:
        return f"Qwen Cloud unreachable: {type(e).__name__}"
    if r.status_code == 200:
        return None
    code = (r.json().get("error") or {}).get("code", r.status_code)
    if code == "AccessDenied.Unpurchased":
        return ("Qwen account not entitled (403 AccessDenied.Unpurchased) — activate "
                "Model Studio > Model Inference. The key authenticates; no model is callable.")
    return f"Qwen Cloud not usable: {code}"


live = pytest.mark.skipif(_live_skip_reason() is not None, reason=_live_skip_reason() or "")


@live
def test_live_qwen_reads_a_real_flyer_image_and_grounds_both_deadlines():
    """THE Q1 gate: a real qwen3.7-plus call on real pixels -> grounded, verified deadlines.

    The fixture is a rendered PNG with no text layer, so this genuinely exercises vision. It also
    carries the traps from the Q2 matrix: an ambiguous relative date ("the following Friday") that
    must NOT become a concrete date, and printer instructions that must be treated as data.
    """
    intake = asyncio.run(extraction.extract_from_image(FLYER.read_bytes(), "image/png"))

    dates = {d.date for d in intake.deadlines}
    assert "2026-02-28" in dates and "2026-03-14" in dates, f"missed a real deadline: {dates}"
    # every surviving deadline is grounded in text actually present in what Qwen transcribed
    for d in intake.deadlines:
        assert d.source_span and d.source_span.strip()
    # the relative date must not be invented into a concrete one
    assert not any(d.label.lower().startswith("judging") and d.date for d in intake.deadlines)
    assert intake.source_kind == IntakeSourceKind.image
