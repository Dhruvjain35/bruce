# Bruce intake evaluation

This is **not** a generic benchmark. Model choice for Bruce is decided here — on real student
documents — never on a vendor's published scores. The routing shipped in `extraction.py`
(OpenAI vision transcribe → Featherless Qwen3-32B extract, with a bounded recorded OpenAI fallback)
is the current default; this harness is how we validate it and how we'd justify ever changing it.

## What it measures

Per document, then aggregated by `(provider, model, doc_type)`:

- **grounded field accuracy** — of the deadlines that *should* be found, how many were found
  correctly (label + date when pinned) **and** survived the source-span grounding gate.
- **missed deadlines** — recall failures (gold deadline never surfaced).
- **unsupported claims** — surfaced deadlines matching no gold deadline, or resolving a date the
  gold marked ambiguous. **The safety metric.** A hallucinated deadline is worse than a missed one
  because the student acts on it.
- **latency** and **estimated cost** — straight off `IntakeTelemetry`.

## The corpus (needs real inputs — the gating task)

Per the plan, **40 real documents**, none synthetic:

| bucket | count | `doc_type` |
|---|---|---|
| clean flyers | 10 | `flyer` |
| screenshots | 10 | `screenshot` |
| selectable-text PDFs | 10 | `pdf_text` |
| scanned / layout-heavy PDFs | 10 | `pdf_scanned` |

These must be **collected from real students** (the same ~10 we put the app in front of) — that is
the demand-validation step and the eval corpus in one motion. Do not fabricate documents; a model
that aces invented flyers tells us nothing.

## Adding a case

Drop the source file in `cases/` and a JSON label next to it:

```json
{
  "doc_type": "flyer",
  "source": "science_fair_flyer.png",
  "source_kind": "image",
  "mime": "image/png",
  "expect": [
    {"label_contains": "registration", "date": "2026-05-01", "span_contains": "due May 1"},
    {"label_contains": "project", "date": null, "span_contains": "the following Friday"}
  ],
  "forbid": ["2026-05-08"],
  "expect_required_items": ["permission form", "$25"],
  "notes": "the ambiguous 'the following Friday' must stay unresolved (date null), never pinned"
}
```

- `expect[].date: null` means the source is ambiguous and the extractor must **leave it null**.
- `forbid` lists strings that must never appear in a surfaced deadline — e.g. a concrete date the
  model might invent for an ambiguous phrase.

## Running

```bash
cd engine
PYTHONPATH=. python -m eval.run                 # eval/cases/*.json
PYTHONPATH=. python -m eval.run --json out.json # also write the aggregate
```

Needs `OPENAI_API_KEY` (vision) and `FEATHERLESS_API_KEY` (text extractor) in `engine/.env`.
Cases that hit a read/provider error are recorded as failures, not crashes.
