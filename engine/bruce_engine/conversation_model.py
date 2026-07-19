"""Provider-neutral multimodal reasoner seam for the conversation brain (Bite 1).

Turns text + optional image/pdf + bounded context into a VALIDATED ConversationDecision (the 13-field
contract). Vision-capable, bounded latency (timeout + one retry), fails LOUDLY (never empty-success),
stores no chain-of-thought. Mirrors intake_providers — the domain never sees a vendor type. Tests
inject a fake ConversationReasoner; this production impl is OpenAI-only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Protocol

from pydantic_ai import Agent, PromptedOutput

try:
    from pydantic_ai import BinaryContent
except ImportError:  # pragma: no cover - older pydantic-ai layout
    from pydantic_ai.messages import BinaryContent

from . import llm
from .conversation_contract import ConversationDecision
from .intake_providers import _pa_tokens
from .provider_status import classify_provider_error


@dataclasses.dataclass
class VisionInput:
    data: bytes
    media_type: str


@dataclasses.dataclass
class ReasonResult:
    decision: ConversationDecision
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ConversationReasoner(Protocol):
    provider: str
    model: str
    supports_vision: bool

    async def decide(self, *, text: str | None, images: list[VisionInput], context: str) -> ReasonResult: ...


_SYSTEM = """You are Bruce, a student's assistant that lives in iMessage. You reason about ONE
message and return a STRUCTURED decision — you never write chain-of-thought.

Voice for user_visible_response: talk like a helpful friend who texts — lowercase, short, natural,
result-first. No corporate phrases, no "as an AI", no fake enthusiasm. Never pretend to be human.

Rules:
- Understand text, images, screenshots, PDFs. Describe an attachment ONLY from what you actually see.
  If you cannot read it, say so and set attachment_summary null — NEVER invent its contents.
- Answer immediately when no external action is needed (casual chat, explaining, tutoring).
- ACADEMIC BOUNDARY: for graded/homework help, TEACH — offer a hint, a walkthrough, or to check the
  student's own answers (response_type=tutoring). NEVER hand over completed graded work as theirs.
- For an event/flyer/ticket, put title/date(s)/location in extracted_entities, each with the verbatim
  source_span it came from. Set needs_mission only when a durable task is truly warranted.
- Only claim a capability Bruce actually has. If asked to do something not supported (add to calendar,
  send email, browse the web), set intent=unsupported (or actionable) with unsupported_reason, and
  NEVER claim it was done.
- risk_level: sensitive/high for money, identity, sending to people, deadlines — anything consequential.
- user_visible_response is the reply to send (before styling). Be honest about uncertainty.
Return ONLY the structured fields."""


class OpenAIConversationReasoner:
    provider = "openai"
    model = llm.MODEL_CONVERSATION
    supports_vision = True

    async def decide(self, *, text: str | None, images: list[VisionInput], context: str) -> ReasonResult:
        agent = Agent(llm.conversation_model(), output_type=PromptedOutput(ConversationDecision),
                      system_prompt=_SYSTEM)
        parts: list = [f"{context}\n\nMESSAGE:\n{text or '(no text — see the attached image/document)'}"]
        for img in images:
            parts.append(BinaryContent(data=img.data, media_type=img.media_type))
        last: Exception | None = None
        t0 = time.perf_counter()
        for _ in range(llm.CONVERSATION_MAX_RETRIES + 1):
            try:
                result = await asyncio.wait_for(agent.run(parts), timeout=llm.CONVERSATION_TIMEOUT_S)
                in_tok, out_tok = _pa_tokens(result)
                return ReasonResult(decision=result.output, provider=self.provider, model=self.model,
                                    input_tokens=in_tok, output_tokens=out_tok,
                                    latency_ms=int((time.perf_counter() - t0) * 1000))
            except Exception as exc:                      # timeout OR validation OR transport
                last = exc
                continue
        raise classify_provider_error(self.provider, self.model, last) from last


def production_reasoner() -> OpenAIConversationReasoner:
    return OpenAIConversationReasoner()
