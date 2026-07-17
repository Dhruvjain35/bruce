"""Run the intake eval over a corpus and print a per-(provider, model, doc_type) report.

    python -m eval.run                 # runs eval/cases/*.json
    python -m eval.run path/to/cases   # runs a different corpus dir
    python -m eval.run --json out.json # also writes the raw aggregate

Needs OPENAI_API_KEY (+ FEATHERLESS_API_KEY for the text extractor) in engine/.env or the env.
Each case that raises a read/provider error is recorded as a failure row rather than crashing the
run — an eval that dies on the first 503 tells you nothing about the other 39 documents.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from bruce_engine.extraction import (
    ExtractionError,
    extract_from_image_traced,
    extract_from_pdf_traced,
    extract_from_text_traced,
)
from bruce_engine.provider_status import ProviderUnavailable

from .schema import GoldCase, load_cases
from .score import CaseScore, aggregate, score_case

_ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = _ENGINE_ROOT / ".env"
    if not env.exists():
        return
    import os

    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def _run_one(case_path: Path, gold: GoldCase):
    if gold.inline_text is not None:
        return await extract_from_text_traced(gold.inline_text)
    data = (case_path.parent / gold.source)
    if gold.source_kind == "text":
        return await extract_from_text_traced(data.read_text())
    if gold.source_kind == "image":
        return await extract_from_image_traced(data.read_bytes(), mime=gold.mime)
    if gold.source_kind == "pdf":
        return await extract_from_pdf_traced(data.read_bytes())
    raise ValueError(f"unknown source_kind {gold.source_kind!r}")


async def main(cases_dir: Path, json_out: Path | None) -> int:
    _load_dotenv()
    cases = load_cases(cases_dir)
    if not cases:
        print(f"no cases in {cases_dir} — see eval/README.md for the corpus spec (40 real documents).")
        return 1

    scores: list[CaseScore] = []
    failures: list[tuple[str, str]] = []
    for path, gold in cases:
        name = path.stem
        try:
            intake, telem = await _run_one(path, gold)
        except (ExtractionError, ProviderUnavailable) as exc:
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        scores.append(score_case(name, gold, intake, telem))

    print(f"\n=== intake eval: {len(scores)} scored, {len(failures)} failed ===\n")
    for s in scores:
        print(
            f"{s.case:32.32}  {s.provider}/{s.model} [{s.doc_type}]  "
            f"acc={s.grounded_field_accuracy:.0%}  missed={s.missed}  unsupported={s.unsupported}  "
            f"{s.latency_ms}ms  ${s.est_cost_usd:.5f}"
            + (f"  fallback={s.fallback_reason}" if s.fallback_reason else "")
        )
    for name, why in failures:
        print(f"{name:32.32}  FAILED  {why}")

    agg = aggregate(scores)
    print("\n=== aggregate by provider/model/doc_type ===")
    print(json.dumps(agg, indent=2))
    if json_out:
        json_out.write_text(json.dumps({"aggregate": agg, "failures": failures}, indent=2))
        print(f"\nwrote {json_out}")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = None
    if "--json" in argv:
        i = argv.index("--json")
        out = Path(argv[i + 1])
        del argv[i : i + 2]  # drop the flag AND its value before reading positionals
    positionals = [a for a in argv if not a.startswith("--")]
    cases_dir = Path(positionals[0]) if positionals else _ENGINE_ROOT / "eval" / "cases"
    raise SystemExit(asyncio.run(main(cases_dir, out)))
