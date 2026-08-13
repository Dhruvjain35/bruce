"""MUTATION PROOFS for the fixes whose value is entirely in what they REFUSE to do.

A test that passes both with and without the fix proves nothing. This runner damages each fix in specific
ways and requires the suite that claims to protect it to go RED every time. A mutation that SURVIVES is a
hole in the suite, reported as a failure of this runner — not a pass.

Covered here:
  * DEFECT-17 — the language evaluator must tell a dead account from a dead model
  * DEFECT-4  — the model must be given exact executable operation ids, and only executable ones
  * DEFECT-3  — a redeemed link code must grant the entitlement it implies
  * N7        — one inbound message, at most one active turn, at most one consequential execution

WHY A SEPARATE RUNNER FROM scripts/mutation_runner.py. That one rsyncs a tree and mutates cluster-wide
Postgres roles (`bruce_app`, `bruce_shadow_recon`) which outlive `DROP DATABASE`; a failed reset silently
disables RLS for every subsequent test in the session, which is why CONTRIBUTING tells you not to run it
casually. This seam is pure in-process evaluator logic. It needs no database, no roles and no tree copy,
so it takes none of that risk.

SAFETY, and a `finally` is NOT enough.

An in-memory original restored in a `finally` survives an exception. It does not survive a kill. This
runner was interrupted mid-mutation on 2026-08-11 and left `conversation_runtime.py` on disk with its
claim gate deleted — a working tree that looked exactly like a deliberate edit and would have been
committed as one. SIGKILL does not run `finally`.

So the originals go to DISK before anything is touched:

  * `.mutation-backup/` receives a copy of every target file plus a manifest, flushed and fsynced BEFORE
    the first mutation is written.
  * Startup checks for that directory. If it is there, a previous run died: this run RESTORES from it and
    exits non-zero rather than mutating a tree whose baseline is unknown.
  * A clean run removes it last, after re-hashing every file against its original.

The failure this closes is specific and expensive: a mutation left on disk is indistinguishable from
intent, in a repo whose entire discipline is that a green suite proves nothing by itself.

    cd engine && .venv/bin/python scripts/mutation_proofs.py
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

ENGINE = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ENGINE / "eval" / "language" / "harness.py"
EXECUTIVE = ENGINE / "bruce_engine" / "semantic_executive.py"
SNAPSHOT = ENGINE / "bruce_engine" / "capability_snapshot.py"

INBOUND = ENGINE / "bruce_engine" / "messaging_inbound.py"
STORE = ENGINE / "bruce_engine" / "conversation_store.py"
RUNTIME = ENGINE / "bruce_engine" / "conversation_runtime.py"

GOALH = ENGINE / "bruce_engine" / "goal_handler.py"
OUTBOUND = ENGINE / "bruce_engine" / "messaging_outbound.py"
AE = ENGINE / "bruce_engine" / "authorization_evidence.py"
PAYLOAD = ENGINE / "bruce_engine" / "consequential_payload.py"

EVAL_SUITE = "tests/test_language_eval_integrity.py"
CAPS_SUITE = "tests/test_capability_ids_in_context.py"
LINK_SUITE = "tests/test_link_grants_access.py"
CLAIM_SUITE = "tests/test_inbound_turn_claim.py"
BYTES_SUITE = "tests/test_approved_bytes_are_sent_bytes.py"
FINALIZE_SUITE = "tests/test_finalize_payload_path.py"


class Mutation:
    def __init__(self, name: str, path: pathlib.Path, old: str, new: str, why: str,
                 suite: str = EVAL_SUITE) -> None:
        self.name, self.path, self.old, self.new, self.why = name, path, old, new, why
        self.suite = suite


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
        "    valid = not reasons_invalid",
        "    valid = True",
        "a run always claims it measured something"),
    Mutation(
        "machinery_tolerance_introduced", HARNESS,
        "    if machinery:",
        "    if machinery > 5:",
        "a provider outage or spent deadline is tolerated instead of invalidating"),
    Mutation(
        "model_quality_bound_removed", HARNESS,
        "    if quality_fraction > MAX_MODEL_QUALITY_FRACTION:",
        "    if False:",
        "unbounded garbage: a model failing half the time scores on the half that parsed"),
    Mutation(
        "malformed_reclassified_as_machinery", HARNESS,
        "_MACHINERY = {ReadOutcome.provider_failure, ReadOutcome.timeout}",
        "_MACHINERY = {ReadOutcome.provider_failure, ReadOutcome.timeout, ReadOutcome.malformed}",
        "one rare unparseable response throws away an affordable measurement again"),
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

    # --- DEFECT-4: exact executable operation ids in the model's context ------------------------------
    Mutation(
        "capability_ids_removed_from_context", SNAPSHOT,
        "            ops = advertised_operations(self)",
        "            ops = ()",
        "the ids stop reaching the model — DEFECT-4 restored",
        CAPS_SUITE),
    Mutation(
        "executability_filter_removed", SNAPSHOT,
        "        out.extend(cap for cap in family.capabilities if goal_handler.executable(cap))",
        "        out.extend(family.capabilities)",
        "unexecutable ids advertised: NOT_AN_OPERATION_ID becomes capability_has_no_goal_kind, "
        "same dead end for the student",
        CAPS_SUITE),
    Mutation(
        "broker_truth_ignored", SNAPSHOT,
        "        if not family.usable:\n            continue",
        "        if False:\n            continue",
        "a disconnected account is advertised because its capability happens to have an executor",
        CAPS_SUITE),
    Mutation(
        "executability_hardcoded_instead_of_derived", SNAPSHOT,
        "        out.extend(cap for cap in family.capabilities if goal_handler.executable(cap))",
        '        out.extend(cap for cap in family.capabilities\n'
        '                   if cap in ("gmail.send_message", "calendar.create_event"))',
        "a second hard-coded list that will drift from the executor registry",
        CAPS_SUITE),

    # --- DEFECT-3: a redeemed link code must actually grant the conversation entitlement ---------------
    Mutation(
        "grant_removed_from_redemption", INBOUND,
        "                await _grant_conversation_access(r.user_id)\n",
        "",
        "DEFECT-3 restored: linked, told 'you're in', and refused by the access gate on the next message",
        LINK_SUITE),
    Mutation(
        "grant_swallows_its_own_failure_silently", INBOUND,
        '        await access_control.activate_production_entitlement(\n'
        '            user_id, capability="conversation", reason="link code redeemed", actor=GRANT_ACTOR)',
        "        return",
        "the grant becomes a no-op that still reports success",
        LINK_SUITE),
    Mutation(
        "grant_actor_is_indistinguishable_from_an_operator", INBOUND,
        'GRANT_ACTOR = "system:link_redemption"',
        'GRANT_ACTOR = "system"',
        "the audit log can no longer tell an automatic grant from an operator's CLI recovery",
        LINK_SUITE),
    Mutation(
        "grant_widened_beyond_conversation", INBOUND,
        '            user_id, capability="conversation", reason="link code redeemed", actor=GRANT_ACTOR)',
        '            user_id, capability="calendar", reason="link code redeemed", actor=GRANT_ACTOR)',
        "redeeming a conversation link code confers a capability the code was never for",
        LINK_SUITE),
    Mutation(
        "a_failing_grant_takes_the_link_down_with_it", INBOUND,
        "    except Exception:\n        log.exception(\"entitlement_grant_failed_after_link\")",
        "    except Exception:\n        raise",
        "a spent single-use code plus a transient store outage tells the student nothing happened",
        LINK_SUITE),

    # --- N7: one inbound message -> at most one active turn -> at most one consequential execution ----
    Mutation(
        "claim_gate_removed_from_runtime", RUNTIME,
        "        if not claim.claimed:\n"
        "            turn_trace.record(trace.finish())\n"
        "            return InboundOutcome(status=\"duplicate\", user_id=user_id)\n",
        "",
        "the claim is taken and then ignored: both concurrent deliveries run the model and send",
        CLAIM_SUITE),
    Mutation(
        "claim_always_reports_success", STORE,
        "        if won is not None:\n            return TurnClaim(turn_id=won, claimed=True)",
        "        if True:\n            return TurnClaim(turn_id=won, claimed=True)",
        "every delivery believes it owns the turn — the defect, restored at the primitive",
        CLAIM_SUITE),
    # THE PRE-FIX IMPLEMENTATION, VERBATIM. An earlier version of this mutation put a SELECT in FRONT of
    # the upsert but left `on_conflict_do_nothing` in place — so Postgres still picked the winner and the
    # mutation SURVIVED. It deserved to: it was a redundant read, not a reintroduced defect, and reading
    # it as "a hole in the suite" would have sent someone to strengthen a test that was already correct.
    # A mutation has to actually remove the atomicity to be a test of it.
    Mutation(
        "atomic_claim_reverts_to_check_then_act", STORE,
        "        won = (await s.execute(\n"
        "            pg_insert(table)\n"
        "            .values(user_id=user_id, channel=channel, channel_identity=channel_identity,\n"
        "                    provider_message_id=provider_message_id, role=\"user\", text=text)\n"
        "            .on_conflict_do_nothing(constraint=\"uq_turn_msg_role\")\n"
        "            .returning(table.c.id))).scalar_one_or_none()",
        "        _existing = await _turn_id(s, user_id, channel, provider_message_id, \"user\")\n"
        "        if _existing is not None:\n"
        "            return TurnClaim(turn_id=_existing, claimed=False)\n"
        "        _row = schema.ConversationTurn(\n"
        "            user_id=user_id, channel=channel, channel_identity=channel_identity,\n"
        "            provider_message_id=provider_message_id, role=\"user\", text=text)\n"
        "        s.add(_row)\n"
        "        await s.flush()\n"
        "        won = _row.id",
        "the SELECT-then-INSERT that shipped before N7: concurrent deliveries all read None and all "
        "insert, so the unique index raises instead of electing one winner",
        CLAIM_SUITE),
    Mutation(
        "loser_is_told_it_created_the_row", STORE,
        "        return TurnClaim(turn_id=await _turn_id(s, user_id, channel, provider_message_id, \"user\"),\n"
        "                         claimed=False)",
        "        return TurnClaim(turn_id=await _turn_id(s, user_id, channel, provider_message_id, \"user\"),\n"
        "                         claimed=True)",
        "the redelivery path claims ownership of a turn another delivery already owns",
        CLAIM_SUITE),

    # --- the typed boundary: voice policy owns Bruce's speech, authorization owns approved payloads ----
    Mutation(
        "post_approval_styling_reintroduced", GOALH,
        "        values = {name: SlotValue(value, Source.model_derived,",
        "        from .messaging_outbound import gate_outbound_text as _vg\n"
        "        values = {name: SlotValue(_vg(value, \"self_hosted_imessage\"), Source.model_derived,",
        "the voice gate restyles the payload after approval — the first, WRONG fix for DEFECT-13, which "
        "shipped 'talk about the extension' to the professor",
        BYTES_SUITE),
    # NOT `if False:` on the first guard alone — that falls straight through to the generic non-str
    # guard, which still raises, so the mutation changed nothing observable and SURVIVED on its own
    # redundancy. The boundary is enforced twice on purpose; a mutation of it has to actually let a
    # payload into the gate's body.
    Mutation(
        "voice_gate_accepts_payloads_again", OUTBOUND,
        "    if isinstance(text, ApprovedConsequentialPayload):\n"
        "        raise PayloadEnteredVoicePipeline(",
        "    if isinstance(text, ApprovedConsequentialPayload):\n"
        "        text = text.render_for_display()\n"
        "    if False:\n"
        "        raise PayloadEnteredVoicePipeline(",
        "the boundary stops being structural: the gate silently accepts a payload and styles the bytes "
        "the student approved",
        BYTES_SUITE),
    Mutation(
        "payload_text_normalized_before_hashing", AE,
        "        if key.lower() in EXACT_TEXT_FIELDS:\n            return value",
        "        if False:\n            return value",
        "approval binds a whitespace-flattened body again, so an email reflowed after approval still "
        "passes execution_gate.require",
        BYTES_SUITE),
    Mutation(
        "finalize_drops_the_payload_seam", RUNTIME,
        "            kind=kind, text=reply, idempotency_key=f\"conv:{pmid}\", payload=payload)",
        "            kind=kind, text=reply, idempotency_key=f\"conv:{pmid}\")",
        "the boundary stops being live at the only call site that matters: the proposal reaches the "
        "student as one gated string again, so the draft is styled on its way to the screen",
        FINALIZE_SUITE),
    Mutation(
        "finalize_splices_the_payload_into_bruce_text", RUNTIME,
        "            kind=kind, text=reply, idempotency_key=f\"conv:{pmid}\", payload=payload)",
        "            kind=kind, text=(reply + (payload.render_for_display() if payload else \"\")),\n"
        "            idempotency_key=f\"conv:{pmid}\")",
        "the payload is interpolated upstream, so the gate receives it as a plain string and the type "
        "boundary is gone even though the bytes still reach the student",
        FINALIZE_SUITE),
    Mutation(
        "payload_interpolates_into_text_silently", PAYLOAD,
        "        raise PayloadEnteredVoicePipeline(\n"
        "            \"an ApprovedConsequentialPayload was interpolated into text.",
        "        return self.render_for_display() or (\n"
        "            \"an ApprovedConsequentialPayload was interpolated into text.",
        "a payload interpolated into Bruce text becomes a plain string and the type boundary vanishes",
        BYTES_SUITE),
]


BACKUP_DIR = ENGINE / ".mutation-backup"


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _backup_name(p: pathlib.Path) -> str:
    return str(p.relative_to(ENGINE)).replace("/", "__")


def _write_durably(path: pathlib.Path, data: bytes) -> None:
    """Bytes that must survive the process dying one instruction later."""
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _stage_backups(targets: list[pathlib.Path]) -> None:
    """Copy every target to disk BEFORE the first mutation. This is the whole crash-safety story."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for p in targets:
        _write_durably(BACKUP_DIR / _backup_name(p), p.read_bytes())
    _write_durably(BACKUP_DIR / "MANIFEST.txt",
                   ("files restored by the next run of scripts/mutation_proofs.py\n"
                    + "\n".join(str(p.relative_to(ENGINE)) for p in targets) + "\n").encode())
    dir_fd = os.open(BACKUP_DIR, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _recover_orphaned_backups() -> bool:
    """A backup directory at startup means a previous run was killed mid-mutation. Put the tree back.

    Restoring and REFUSING to continue is deliberate. The alternative — restore and carry on — would run
    a proof whose baseline nobody has looked at, and the entire value of this runner is that its baseline
    is known good.
    """
    if not BACKUP_DIR.exists():
        return False
    print(f"RECOVERY: {BACKUP_DIR.relative_to(ENGINE)} exists, so a previous run died mid-mutation.")
    restored = []
    for backup in sorted(BACKUP_DIR.iterdir()):
        if backup.name == "MANIFEST.txt":
            continue
        target = ENGINE / backup.name.replace("__", "/")
        if target.read_bytes() != backup.read_bytes():
            target.write_bytes(backup.read_bytes())
            restored.append(str(target.relative_to(ENGINE)))
    for f in BACKUP_DIR.iterdir():
        f.unlink()
    BACKUP_DIR.rmdir()
    if restored:
        print("  RESTORED (these were left MUTATED on disk):")
        for r in restored:
            print(f"    {r}")
        print("  Re-run to take the proof on a known-good tree.")
    else:
        print("  every file already matched its backup; nothing needed restoring.")
    return True


# A mutated tree can DEADLOCK a suite rather than fail it — a concurrency test whose fix is removed can
# wait on a signal that now never arrives. Waiting forever is indistinguishable from thinking, so every
# run is bounded twice: pytest's own per-test timeout, and a hard subprocess timeout under it. A hang is
# a RED result, because a suite that cannot finish has not passed.
SUITE_TIMEOUT_S = 300


def _suite_is_red(suite: str) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", suite, "-q", "-p", "no:randomly", "-x",
             f"--timeout={SUITE_TIMEOUT_S // 2}"],
            cwd=ENGINE, capture_output=True, text=True, timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"      (suite exceeded {SUITE_TIMEOUT_S}s and was killed — counted as RED)")
        return True
    return proc.returncode != 0


def main() -> int:
    # `--only <substring>` runs a subset. For iterating on ONE mutation's own correctness — a mutation
    # can be wrong (too weak to reintroduce the defect) just as a test can, and finding that out should
    # not cost a full sweep. A real proof is still the unfiltered run.
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    mutations = [m for m in MUTATIONS if only is None or only in m.name]
    if not mutations:
        print(f"no mutation matches {only!r}")
        return 2

    targets = sorted({m.path for m in mutations})

    # A killed run leaves its backups behind. Put the tree back and refuse to proceed on a baseline
    # nobody has inspected — see the module docstring for the day this mattered.
    if _recover_orphaned_backups():
        return 4

    originals = {p: p.read_bytes() for p in targets}
    before = {p: _sha(p) for p in targets}
    _stage_backups(targets)          # ON DISK, before a single byte is mutated

    suites = sorted({m.suite for m in mutations})
    print(f"MUTATION PROOFS — {len(mutations)} mutations across {len(suites)} suites"
          + (f"  [filtered: {only}]" if only else "") + "\n")

    # Every suite must be GREEN before anything is damaged, or each result below is meaningless.
    for suite in suites:
        if _suite_is_red(suite):
            print(f"ABORT: {suite} is already red on an unmutated tree. Fix that first.")
            return 2
    print("baseline: every suite GREEN on the unmutated tree\n")

    caught, survived = [], []
    try:
        for m in mutations:
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
                if _suite_is_red(m.suite):
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
        print(f"Backups are intact in {BACKUP_DIR.relative_to(ENGINE)}; re-run to restore from them.")
        return 3
    # Only NOW is it safe to drop the backups: the tree is provably back to where it started.
    for f in BACKUP_DIR.iterdir():
        f.unlink()
    BACKUP_DIR.rmdir()
    print("\nrestore verified: every target file matches its original sha256")

    print(f"\n{len(caught)} caught, {len(survived)} survived, {len(mutations)} total")
    if survived:
        print("\nSURVIVING MUTATIONS (each is a property the suite does not actually test):")
        for m in survived:
            print(f"  * {m.name} — {m.why}")
        return 1
    print("every mutation was caught: the suite constrains the fix it claims to.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
