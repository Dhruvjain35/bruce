"""The gate runner — the thing that says whether Bruce may be deployed.

WHY THIS EXISTS. Every serious defect this project shipped was invisible to a green suite (see
CONTRIBUTING.md), and the reports that hid them all had the same shape: a total. "3159 passed" tells you
nothing about whether the 202 adversarial turns that must never reach an adapter still cannot, or whether
the acceptance scenario proving a student's experience still exists. A number that moves whenever anyone
adds a test teaches people to bump the number.

SO NO GLOBAL TOTALS ARE PINNED HERE, deliberately. What is pinned is BEHAVIOUR:

  * every gate command runs and must have ZERO failures;
  * every corpus case id is still present AND still declares the same allow/block expectation — so a
    case cannot be quietly reclassified to make CI green, which is the cheapest way to fake this gate;
  * the zero-adapter-call proof and the over-block ceiling both ran;
  * every pinned acceptance scenario is still collected — deleting one is how "16 passed" stays true
    while the thing it proved disappears;
  * the migration chain has one head and the lane is reserved;
  * no debug or scratch test files were committed.

CHANGING A SAFETY BASELINE REQUIRES SAYING SO. If anything under `safety` in the manifest differs from
what is committed, the runner refuses unless `--accept-safety-change` is passed with a reason. Relaxing a
boundary should cost a sentence in a review, not a silent diff in a JSON file.

    python -m scripts.run_gates                 # every gate
    python -m scripts.run_gates --gate boundary # one of them
    python -m scripts.run_gates --accept-safety-change "corpus gained 4 cases for the new lane"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
MANIFEST = ENGINE / "ci" / "gates.json"
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly"]

_SUMMARY = re.compile(r"(?P<n>\d+) (?P<word>passed|failed|error)")


class GateFailure(Exception):
    """A gate did not hold. Never swallowed — the whole point is that this stops a deploy."""


def _run_pytest(args: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(PYTEST + args, cwd=ENGINE, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)


def _collected(args: list[str]) -> set[str]:
    """Test ids pytest can SEE. A gate that passes because its tests vanished is the failure mode this
    guards: deleting a scenario keeps every count honest and the proof gone."""
    proc = subprocess.run(PYTEST + ["--collect-only"] + args, cwd=ENGINE,
                          capture_output=True, text=True)
    return {line.split("::")[-1].strip() for line in proc.stdout.splitlines() if "::" in line}


def check_gates(manifest: dict, only: str | None) -> list[str]:
    problems: list[str] = []
    for gate in manifest["gates"]:
        if only and gate["id"] != only:
            continue
        ok, out = _run_pytest(list(gate["cmd"]))
        counts = {m.group("word"): int(m.group("n")) for m in _SUMMARY.finditer(out)}
        failed = counts.get("failed", 0) + counts.get("error", 0)
        passed = counts.get("passed", 0)
        status = "OK " if (ok and not failed) else "FAIL"
        print(f"  [{status}] {gate['id']:24} {passed} passed, {failed} failed   ({gate['why']})")
        if not ok or failed:
            problems.append(f"gate {gate['id']}: {failed} failed")
        elif passed == 0:
            # A gate that collected nothing is not a gate. This has happened: a renamed path made a
            # command match no files and the runner cheerfully reported success.
            problems.append(f"gate {gate['id']}: collected NOTHING — the command matches no tests")
    return problems


def check_corpus(manifest: dict) -> list[str]:
    """The corpus's own expectations, pinned OUTSIDE the corpus.

    `forbids_writes` lives on each case, so a case can be flipped from must-block to may-authorize in one
    character and the suite stays green while the property evaporates. This compares the live corpus
    against the manifest and reports every id that moved, in either direction.
    """
    sys.path.insert(0, str(ENGINE / "eval"))
    from authorization import corpus                       # noqa: E402

    pinned = manifest["safety"]["authorization_corpus"]
    live_block = {c.cid for c in corpus.CASES if c.forbids_writes}
    live_allow = {c.cid for c in corpus.CASES if not c.forbids_writes}
    problems: list[str] = []

    if len(corpus.CASES) != pinned["total_cases"]:
        problems.append(f"corpus size {len(corpus.CASES)} != pinned {pinned['total_cases']}")
    for cid in pinned["must_block"]:
        if cid not in live_block:
            problems.append(f"corpus case {cid!r} no longer declares it must be BLOCKED"
                            + (" (it is now allowed)" if cid in live_allow else " (it is gone)"))
    for cid in pinned["may_authorize"]:
        if cid not in live_allow:
            problems.append(f"corpus case {cid!r} no longer declares it may be AUTHORIZED"
                            + (" (it is now blocked)" if cid in live_block else " (it is gone)"))
    print(f"  [{'OK ' if not problems else 'FAIL'}] corpus expectations      "
          f"{len(live_block)} must-block, {len(live_allow)} may-authorize")
    return problems


def check_named_tests(manifest: dict) -> list[str]:
    """The specific proofs that must have RUN — not merely that their file passed."""
    problems: list[str] = []
    pinned = manifest["safety"]["authorization_corpus"]
    for key in ("zero_forbidden_adapter_calls_test", "over_block_ceiling_test"):
        target = pinned[key]
        ok, out = _run_pytest([target])
        counts = {m.group("word"): int(m.group("n")) for m in _SUMMARY.finditer(out)}
        if not ok or counts.get("failed", 0) or counts.get("error", 0):
            problems.append(f"{key}: {target} did not pass")
        elif counts.get("passed", 0) == 0:
            problems.append(f"{key}: {target} collected nothing — the proof no longer exists")
    print(f"  [{'OK ' if not problems else 'FAIL'}] adapter/over-block proofs ran")
    return problems


def check_acceptance_scenarios(manifest: dict) -> list[str]:
    pinned = set(manifest["safety"]["acceptance_scenarios"])
    live = _collected(["tests/test_acceptance_cross_tool.py"])
    missing = sorted(pinned - live)
    print(f"  [{'OK ' if not missing else 'FAIL'}] acceptance scenarios     "
          f"{len(live)} collected, {len(pinned)} pinned")
    return [f"acceptance scenario {s!r} is pinned but no longer collected" for s in missing]


def check_migration(manifest: dict) -> list[str]:
    """One head, and a lane reserved for it. A fork is only visible on merge day otherwise."""
    sys.path.insert(0, str(ENGINE))
    from migrations import lanes                            # noqa: E402

    pinned = manifest["safety"]["migration"]
    problems: list[str] = []
    if lanes.lane_for(pinned["lane"]) != pinned["lane_owner"]:
        problems.append(f"migration lane {pinned['lane']} is not reserved to {pinned['lane_owner']!r}")
    head = pinned["pinned_head"]
    if not (ENGINE / "migrations" / "versions" / f"{head}.py").exists():
        problems.append(f"pinned head {head} has no migration file")
    rls = (ENGINE / "tests" / "test_migration_rls_context.py").read_text()
    if head not in rls:
        problems.append(f"pinned head {head} is not asserted in test_migration_rls_context.py")
    print(f"  [{'OK ' if not problems else 'FAIL'}] migration chain          head={head}")
    return problems


def check_language_corpus(manifest: dict) -> list[str]:
    """The generalization corpus must still have teeth.

    Pinned OUTSIDE the corpus for the same reason the authorization case ids are: a corpus that grades
    itself can be made green by editing the corpus. The cheapest ways to fake this gate are deleting a
    family and moving a failing phrasing into `known_gaps`, so both are checked directly.
    """
    pinned = manifest["safety"].get("language_generalization")
    if not pinned:
        return []
    problems: list[str] = []
    corpus_path = ENGINE / pinned["corpus_file"]
    if not corpus_path.exists():
        return [f"language corpus {pinned['corpus_file']} is gone"]
    corpus = json.loads(corpus_path.read_text())
    live = {f["id"]: f for f in corpus.get("families", [])}

    for fam_id in pinned["required_families"]:
        fam = live.get(fam_id)
        if fam is None:
            problems.append(f"language family {fam_id!r} was DELETED from the corpus")
            continue
        held = len(fam.get("held_out", []))
        if held < pinned["min_held_out_per_family"]:
            problems.append(f"family {fam_id!r} has {held} held-out phrasings, "
                            f"below the pinned floor of {pinned['min_held_out_per_family']} — a family "
                            f"tested only on its seeds measures memorization")
    gaps = len(corpus.get("known_gaps", {}).get("cases", []))
    if gaps > pinned["max_known_gaps"]:
        problems.append(f"known_gaps grew to {gaps} (max {pinned['max_known_gaps']}) — phrasings are "
                        f"being moved out of the families instead of being understood")

    iso = pinned.get("isolation")
    if iso:
        harness = (ENGINE / "eval" / "language" / "harness.py")
        body = harness.read_text() if harness.exists() else ""
        # AST, not substring. Three of these names appear in this file's own comments EXPLAINING why they
        # are forbidden, and a check that cannot tell code from prose about the code pressures people to
        # delete the explanation — destroying exactly the knowledge that stops the bug recurring. Match
        # what EXECUTES.
        import ast as _ast

        def _dotted(node) -> str:
            """Full dotted name of a call target. Walks the WHOLE attribute chain.

            The first version of this stopped one level up, so `os.environ.setdefault` resolved to
            `environ.setdefault` and matched nothing — the check ran, printed OK, and caught nothing.
            Found by mutation-proving the gate rather than by reading it.
            """
            parts = []
            while isinstance(node, _ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, _ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))

        called: set[str] = set()
        try:
            for node in _ast.walk(_ast.parse(body)):
                if isinstance(node, _ast.Call):
                    name = _dotted(node.func)
                    if name:
                        called.add(name)
                        called.add(name.rsplit(".", 1)[-1])       # bare form, for `default_provider()`
        except SyntaxError as exc:
            problems.append(f"eval/language/harness.py does not parse: {exc}")

        for banned in iso["forbidden_in_harness"]:
            needle = banned.rstrip("()")
            if banned in called or f"{needle}()" in called or needle in called:
                problems.append(
                    f"eval/language/harness.py CALLS {banned!r} — the language gate must own its "
                    f"dependencies. Reaching for process-global state is how a leaked API key made this "
                    f"gate report 0.86 for a system measuring 0.99, with nothing near the leak failing.")
        ok, out = _run_pytest([iso["regression_test"]])
        counts = {m.group("word"): int(m.group("n")) for m in _SUMMARY.finditer(out)}
        if not ok or counts.get("failed", 0) or counts.get("passed", 0) == 0:
            problems.append(f"isolation regression test did not pass: {iso['regression_test']}")

    for key in ("corpus_purity_test", "authority_contract_test"):
        ok, out = _run_pytest([pinned[key]])
        counts = {m.group("word"): int(m.group("n")) for m in _SUMMARY.finditer(out)}
        if not ok or counts.get("failed", 0) or counts.get("passed", 0) == 0:
            problems.append(f"{key}: {pinned[key]} did not pass")

    print(f"  [{'OK ' if not problems else 'FAIL'}] language corpus         "
          f"{len(live)} families, {gaps} known gap(s)")
    return problems


def check_shadow_is_inert(manifest: dict) -> list[str]:
    """Shadow mode is only trustworthy while it CANNOT act.

    The whole "measure before authority" argument rests on the observer being unable to become an actor,
    and that is exactly the kind of property that erodes quietly — one import at a time, each individually
    reasonable. So it is asserted structurally rather than trusted to review.
    """
    pinned = manifest["safety"].get("semantic_shadow")
    if not pinned:
        return []
    problems: list[str] = []
    mod = ENGINE / pinned["module"]
    if not mod.exists():
        return [f"{pinned['module']} is gone — shadow mode cannot be verified inert"]
    body = mod.read_text()
    for forbidden in pinned["forbidden_imports"]:
        if f"import {forbidden}" in body or f"from .{forbidden}" in body:
            problems.append(f"semantic_shadow imports {forbidden!r} — an observer that can act is not a "
                            f"shadow, and every metric collected under it becomes unsafe to trust")
    if pinned.get("flags_must_be_distinct") and pinned["authority_flag"] == pinned["observation_flag"]:
        problems.append("the authority and observation flags are the same — the only way to measure the "
                        "executive would be to let it decide")
    print(f"  [{'OK ' if not problems else 'FAIL'}] shadow inert            "
          f"{len(pinned['forbidden_imports'])} forbidden imports checked")
    return problems


def check_hygiene(manifest: dict) -> list[str]:
    """Debug tests do not ship, and source inspection is not behavioural proof."""
    problems: list[str] = []
    for pattern in manifest["hygiene"]["forbidden_test_globs"]:
        for found in ENGINE.glob(pattern):
            problems.append(f"debug/scratch test committed: {found.relative_to(ENGINE)}")
    # SOURCE INSPECTION IS A RATCHET, not a ban. Thirteen files already did this when the gate landed,
    # and blocking every change until they are all rewritten would just get the gate deleted. So the debt
    # is recorded and may only SHRINK: a new file using it fails, and paying one down is free.
    debt = manifest["hygiene"]["source_inspection_debt"]
    known = set(debt["files"])
    live = {f"tests/{p.name}" for p in (ENGINE / "tests").glob("test_*.py")
            if "inspect.getsource" in p.read_text()}
    for rel in sorted(live - known):
        problems.append(f"{rel} is a NEW file using inspect.getsource as behavioural proof — "
                        f"source inspection cannot tell you the code runs")
    if len(live) > debt["max_files"]:
        problems.append(f"source-inspection debt grew: {len(live)} > {debt['max_files']}")
    paid = sorted(known - live)
    print(f"  [{'OK ' if not problems else 'FAIL'}] hygiene                  "
          f"source-inspection debt {len(live)}/{debt['max_files']}"
          + (f", paid down: {', '.join(paid)}" if paid else ""))
    return problems


def check_safety_baseline_unchanged(accepted: str | None) -> list[str]:
    """Relaxing a safety baseline must cost a sentence, not a silent diff."""
    proc = subprocess.run(["git", "diff", "HEAD", "--", str(MANIFEST.relative_to(ENGINE.parent))],
                          cwd=ENGINE.parent, capture_output=True, text=True)
    changed = [ln for ln in proc.stdout.splitlines()
               if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    touches_safety = any('"must_block"' in ln or '"may_authorize"' in ln or '"boundary_suites"' in ln
                         or '"acceptance_scenarios"' in ln or '"pinned_head"' in ln
                         or '"total_cases"' in ln for ln in changed)
    if changed and touches_safety and not accepted:
        print("  [FAIL] safety baseline           changed without acknowledgement")
        return ["ci/gates.json safety baseline changed; rerun with "
                "--accept-safety-change '<why this is correct>'"]
    label = "unchanged" if not changed else f"changed, accepted: {accepted}"
    print(f"  [OK ] safety baseline           {label}")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Bruce's deploy gates.")
    ap.add_argument("--gate", help="run only this gate id")
    ap.add_argument("--accept-safety-change", metavar="REASON",
                    help="acknowledge a deliberate change to a safety baseline")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    print(f"Bruce gates (manifest v{manifest['version']})\n")
    problems: list[str] = []
    problems += check_gates(manifest, args.gate)
    if not args.gate:
        problems += check_corpus(manifest)
        problems += check_named_tests(manifest)
        problems += check_acceptance_scenarios(manifest)
        problems += check_migration(manifest)
        problems += check_language_corpus(manifest)
        problems += check_shadow_is_inert(manifest)
        problems += check_hygiene(manifest)
        problems += check_safety_baseline_unchanged(args.accept_safety_change)

    print()
    if problems:
        print(f"GATES FAILED ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("ALL GATES GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
