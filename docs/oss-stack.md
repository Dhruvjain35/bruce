# Bruce OSS + model stack

_Decided from the OSS research pass (workflow `bruce-oss-stack-research`, 2026-07-13).
Bias: permissive licenses only for anything shipped in a hosted/commercial Bruce._

## Backend (Python) — adopt now (all permissive)

| Component | License | Role |
|---|---|---|
| **httpx + trafilatura** | Apache-2.0 | Static fetch + clean faculty-page text (the workhorse for ~80% of pages) |
| **selectolax** | MIT | Fast targeted DOM extraction (`mailto:` hrefs, "Contact/Email" cells) |
| **playwright** | Apache-2.0 | JS-render **fallback tier only** (gate behind "static fetch failed") |
| **pdfplumber** | MIT | PDF corresponding-author/email extraction (coordinate-aware) |
| **pyalex** | MIT | OpenAlex client (can replace our hand-rolled httpx client later) |
| **python-email-validator** | public domain | Syntax + MX check before any address is emailable — the send-gate |
| **spaCy** | MIT | Deterministic NER cross-check of LLM-claimed names/orgs vs page text |
| **tenacity** | Apache-2.0 | Backoff/jitter on scholarly-API + fetch 429s |
| **pydantic-ai** | MIT | Agent framework / validated structured output (chosen) |
| **Pydantic Logfire SDK** | MIT | One-line tracing for pydantic-ai + FastAPI |

**Adopt later:** Docling (MIT, high-accuracy PDF when messy/scanned), habanero + semanticscholar (secondary grounding channels), self-hosted Langfuse (eval datasets), crawl4ai (only if Playwright glue piles up).

**Avoid (license):** PyMuPDF / pymupdf4llm (**AGPL** — network copyleft over the whole app), marker (**GPL + revenue-gated weights**), Arize Phoenix (Elastic License, not OSI). **Redundant:** `instructor`, `LiteLLM` (pydantic-ai already covers structured output + provider abstraction).

## iOS — native first

**Use native Apple APIs, no library:** Liquid Glass (`glassEffect`/`GlassEffectContainer`), motion (`Spring`/`PhaseAnimator`/`.scrollTransition`), haptics (`.sensoryFeedback`), Swift Charts, ActivityKit/WidgetKit.

**Adopt (targeted, safe licenses):** Pow (MIT — delight transitions, sparingly), Inject (MIT — hot reload, stripped in release), Glur (MIT — progressive blur under bars; native glass doesn't cover this), SFSafeSymbols (MIT — compile-safe symbols), swiftui-introspect (MIT — escape hatch), Kingfisher/Nuke (MIT — cached remote images), Lottie (Apache-2.0 — designer animations only).

**Avoid:** SwiftfulHaptics & LiquidGlassKit (no LICENSE = not shippable), VariableBlur (private CAFilter API → App Store rejection risk; use Glur), full third-party UI kits (read as templated — the opposite of the goal).

## Inference / models

> **PRODUCTION ROUTING UPDATE (2026-07-17) — this supersedes the Featherless-primary plan below.**
> Production runs entirely on **OpenAI `gpt-5.4-mini`**: vision transcription (image/scanned-PDF →
> text), structured task/date extraction, drafting, and verification. Selectable-PDF text is local
> **pdfplumber**; ambiguous fields go to student review. This is a **latency** decision — live
> measurement put the Featherless serverless path at ~34s steady-state with a **252s cold-start
> tail**, and one four-minute wait destroys the "hand it to Bruce and it works" promise; tail, not
> average, is the enemy. **Featherless is now offline-only** (eval, batch, model comparison,
> backfills), **disabled by default** behind `BRUCE_ENABLE_FEATHERLESS`, and never on a synchronous
> request path or a silent fallback. The provider-neutral factory is kept for exactly those offline
> jobs. Alibaba Qwen Cloud is not used. Authoritative routing lives in `engine/bruce_engine/llm.py`.

- **Featherless** (OpenAI-compatible, `https://api.featherless.ai/v1`) for the cheap/high-volume steps. Confirmed live: key valid, 22k+ models, `Qwen/Qwen3-30B-A3B-Instruct-2507` returns completions. Bills by **concurrent units, not tokens** (70B = 4 units). Use **Qwen3 / Kimi-K2** for structured output. Model IDs verified against the live `/v1/models` list (do not trust memorized IDs).
- **Model per engine step:** intent/topic parse → small Qwen3; finding extraction → Qwen3-32B; grounded drafting → Qwen3-32B/larger (or Llama-3.3-70B for warmer prose); **verification/entailment → frontier Claude** (Anthropic direct, separate Agent, low-volume, safety-critical). Claude needs an Anthropic key we don't have yet — until then, run verification on a strong open model and treat it as provisional.
- **Structured-output caveat (empirical, resolved):** PydanticAI's default tool-calling structured output returned a 500 from Featherless on the Qwen3-30B MoE. **Confirmed fix:** PydanticAI **PromptedOutput** mode (prompted JSON, no tool-calling) works on both `Qwen/Qwen3-32B` and `Qwen/Qwen3-30B-A3B-Instruct-2507`; `Qwen3-32B` also handles default tool-calling, but we standardize on **PromptedOutput** for robustness. Codified in `engine/bruce_engine/llm.py`. (Kimi-K2 was 503/cold at test time.)
- **Alternatives / escape hatches:** Together AI (per-token, OpenAI-compatible), Groq (fastest/cheapest for high-volume steps), OpenRouter (route open models *and* Claude through one key). Keep a `featherless()` model factory so switching base_url is one line.

## Grounding reality that shapes everything

None of OpenAlex/Crossref/Semantic Scholar expose personal emails, so email discovery depends on the scrape + PDF tiers — which is exactly why `python-email-validator` (syntax + MX), spaCy NER cross-checks, and the Claude entailment gate are the load-bearing anti-hallucination controls, not optional polish.
