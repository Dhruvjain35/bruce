"""MUTATION PROOF for the language-evaluation integrity gate (DEFECT-17).

A test that passes both with and without the fix proves nothing. This runner damages the fix in ten
specific ways and requires `tests/test_language_eval_integrity.py` to go RED for every one of them. A
mutation that SURVIVES is a hole in the suite, reported as a failure of this runner — not a pass.

WHY A SEPARATE RUNNER FROM scripts/mutation_runner.py. That one rsyncs a tree and mutates cluster-wide
Postgres roles (`bruce_app`, `bruce_shadow_recon`) which outlive `DROP DATABASE`; a failed reset silently
disables RLS for every subsequent test in the session, which is why CONTRIBUTING tells you not to run it
casually. This seam is pure in-process evaluator logic. It needs no database, no roles and no tree copy,
so it takes none of that risk.

SAFETY. Every target file's original bytes are held in memory and restored in a `finally`, and the run
ends by re-hashing all of them against the originals. If a restore ever fails the runner says so loudly
and exits non-zero — a mutated file left on disk is a far worse outcome than a failed proof.

    cd engine && .venv/bin/python scripts/mutation_language_eval.py
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ENGINE = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ENGINE / "eval" / "language" / "harness.py"
EXECUTIVE = ENGINE / "bruce_engine" / "semantic_executive.py"
SUITE = "tests/test_language_eval_integrity.py"


class Mutation:
    def __init__(self, name: str, path: pathlib.Path, old: str, new: str, why: str) -> None:
        self.name, self.path, self.old, self.new, self.why = name, path, old, new, why


MUTATIONS = [
    Mutation(
        "defect_17_predicate_restored", HARNESS,
        "            if outcome not in _RETRYABLE:",
        '            if not any("reader unavailable" in n for n in turn.validation_notes):',
        "the original bug verbatim: retry on a prose substring the executive never emits"),
    Mutation(
        "classify_read_always_valid", HARNESS,
        "    seen = [_OUTCOME_BY_CODE[c] for c in codes if c in _OUTCOME_BY_CODE]",
        "    seen = []",
        "every failure classified as a genuine reading"),
    Mutation(
        "run_always_valid", HARNESS,
        "    valid = len(observations) == expected_total",
        "    valid = True",
        "a run always claims it measured something"),
    Mutation(
        "rates_reported_on_invalid_run", HARNESS,
        '        "conversation_vs_action": ((conv_correct / conv_total) if conv_total else 0.0) if valid else None,',
        '        "conversation_vs_action": (conv_correct / conv_total) if conv_total else 0.0,',
        "an invalidated run still hands out a comprehension percentage"),
    Mutation(
        "failed_reads_are_scored", HARNESS,
        "    observations = [o for o in recorded if o.outcome is ReadOutcome.valid]",
        "    observations = recorded",
        "failures counted as wrong answers — the exact shape of the 0.1739 report"),
    Mutation(
        "failure_reasons_keyed_on_prose", HARNESS,
        "    reasons = Counter(c for o in recorded for c in o.codes)",
        "    reasons = Counter(n for o in recorded for n in o.notes)",
        "the diagnostic fragments on interpolated latency again"),
    Mutation(
        "timeout_collapsed_into_provider_failure", HARNESS,
        '    "triage_failed_timeout": ReadOutcome.timeout,',
        '    "triage_failed_timeout": ReadOutcome.provider_failure,',
        "a latency fact is billed to the provider owner"),
    Mutation(
        "retry_disabled", HARNESS,
        "        for attempt in range(TRANSPORT_RETRIES):",
        "        for attempt in range(1):",
        "a transient blip is no longer absorbed"),
    Mutation(
        "outcome_classes_pruned_when_zero", HARNESS,
        '        "read_outcomes": outcome_counts,',
        '        "read_outcomes": {k: v for k, v in outcome_counts.items() if v},',
        "a zero becomes a missing key, which .get() silently turns back into a zero"),
    Mutation(
        "executive_taxonomy_collapsed", EXECUTIVE,
        "                             TRIAGE_FAILURE_CODES.get(outcome.reason, CODE_TRIAGE_FAILED))",
        "                             CODE_TRIAGE_FAILED)",
        "timeout / transport / malformed collapse back into one unusable label"),
]


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _suite_is_red() -> bool:
    proc = subprocess.run([sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:randomly", "-x"],
                          cwd=ENGINE, capture_output=True, text=True)
    return proc.returncode != 0


def main() -> int:
    targets = sorted({m.path for m in MUTATIONS})
    originals = {p: p.read_bytes() for p in targets}
    before = {p: _sha(p) for p in targets}

    print(f"MUTATION PROOF — {len(MUTATIONS)} mutations against {SUITE}\n")

    # The suite must be GREEN before anything is damaged, or every result below is meaningless.
    if _suite_is_red():
        print("ABORT: the suite is already red on an unmutated tree. Fix that first.")
        return 2
    print("baseline: suite GREEN on the unmutated tree\n")

    caught, survived = [], []
    try:
        for m in MUTATIONS:
            text = m.path.read_text()
            if m.old not in text:
                print(f"  [ERROR   ] {m.name}: anchor not found in {m.path.name} — mutation is stale")
                survived.append(m)
                continue
            if text.count(m.old) != 1:
                print(f"  [ERROR   ] {m.name}: anchor is ambiguous ({text.count(m.old)} matches)")
                survived.append(m)
                continue
            try:
                m.path.write_text(text.replace(m.old, m.new, 1))
                if _suite_is_red():
                    caught.append(m)
                    print(f"  [CAUGHT  ] {m.name}\n              {m.why}")
                else:
                    survived.append(m)
                    print(f"  [SURVIVED] {m.name}  <-- HOLE IN THE SUITE\n              {m.why}")
            finally:
                m.path.write_bytes(originals[m.path])
    finally:
        for p in targets:
            p.write_bytes(originals[p])

    after = {p: _sha(p) for p in targets}
    drifted = [p for p in targets if before[p] != after[p]]
    if drifted:
        print("\nFATAL: a mutated file was not restored: " + ", ".join(str(p) for p in drifted))
        return 3
    print("\nrestore verified: every target file matches its original sha256")

    print(f"\n{len(caught)} caught, {len(survived)} survived, {len(MUTATIONS)} total")
    if survived:
        print("\nSURVIVING MUTATIONS (each is a property the suite does not actually test):")
        for m in survived:
            print(f"  * {m.name} — {m.why}")
        return 1
    print("every mutation was caught: the suite constrains the fix it claims to.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
