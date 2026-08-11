"""DETERMINISTIC MUTATION RUNNER — proof that the shadow suite's assertions are load-bearing.

WHY THIS FILE EXISTS, AND WHY IT IS NOT AN AGENT. Three consecutive verification rounds tried to prove
these properties by reasoning about the diff and stalled on session limits, every time, on work that is
entirely mechanical: break one property, run the tests that claim to guard it, and check that they go
red for the RIGHT reason. A reasoning agent is the wrong instrument for that — it is slow, it is not
reproducible, and its answer cannot be re-run on the next commit. This runner is.

WHAT A MUTATION PROVES, AND WHAT IT DOES NOT. A green suite says the code passes the tests. It says
nothing about whether the tests would notice the code being wrong, and every serious defect this project
shipped hid behind a green suite (CONTRIBUTING.md). A mutation is the missing direction: take one
property the suite claims to protect, remove it from the SOURCE, and demand that the specific test that
claims to protect it fails, with the specific message it claims to fail with. Anything else is a gap.

THE FIVE OUTCOMES, AND WHY FOUR OF THEM ARE FAILURES:

  CAUGHT        the mutation applied, the intended test failed, and it failed for the intended reason.
  ZERO_EDIT     the mutation's anchor matched nothing. This is a VERIFICATION FAILURE, never a pass —
                a stale anchor produces a run in which nothing was broken and the suite is green for the
                least interesting reason imaginable. It is the single most likely way this whole file
                could quietly stop meaning anything, which is why the edit count is asserted before the
                tests are ever started.
  ESCAPED       the mutation applied and the tests stayed green. A BLOCKING test gap. There is no retry
                path that turns this into a pass: the fix is the test (or the property), then a rerun.
  INVALID_PROOF the tests failed, but for something unrelated — an ImportError, a SyntaxError, a dangling
                constraint that stops startup. Those prove the mutated line was load-bearing for the
                PROCESS, which is not the same claim as "the property is guarded", and accepting them
                would let a mutation that deletes a random line count as proof of anything nearby.
  HARNESS_ERROR the runner itself broke. Reported, never silently swallowed.

ISOLATION, AND THE ONE THING IT CANNOT ISOLATE. Every mutation runs in a fresh rsync'd copy of the engine
with `.venv` symlinked in, and the CANONICAL working tree's fingerprint is recorded before the first
mutation and re-checked after the last. The user's tree is never edited; if this runner ever leaves a byte
behind, the final fingerprint says so.

The FILESYSTEM is isolated. THE POSTGRES CLUSTER IS NOT, and this round is the first time that matters.
`conftest` gives each session its own DATABASE, but `bruce_app` and `bruce_shadow_recon` are CLUSTER-level
roles that outlive the database they were used from. Two of the mutations added this round attack exactly
those roles — `secdef-owner-login` makes the definer's owner able to log in, `worker-bypassrls` hands the
app role BYPASSRLS — and neither `DROP DATABASE` nor the tree fingerprint would undo either. Left behind,
`worker-bypassrls` in particular would silently disable RLS for every test that ran afterwards, in this
runner and in the user's own suite: the mutation would escape into the environment and the runs after it
would be green for the worst possible reason. So the two roles are RESET to their shipped shape after
every mutation, and whether that reset succeeded is recorded per mutation rather than assumed.

    python -m scripts.mutation_runner                       # the full inventory
    python -m scripts.mutation_runner --id lease-token-guard # one of them
    python -m scripts.mutation_runner --dry-run             # apply + count edits, run no tests
    python -m scripts.mutation_runner --json report.json    # also write the report to a file

The report goes to stdout as ONE JSON object either way.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

# What is NOT part of the tree's identity, and what is not copied into an isolated run. `.venv` is
# symlinked instead of copied (it is hundreds of megabytes and it is not what is under test); the caches
# are derived artefacts that would make two identical trees fingerprint differently.
_EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 "node_modules", ".mutation_runs"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")

# Ways a test can fail that are NOT evidence about a property. Each of these means the process could not
# get far enough to ask the question — a mutation that trips one has proved that the line it deleted was
# needed for the module to import, which is a different (and much weaker) claim than the one being made.
_INFRA_MARKERS = (
    "ImportError", "ModuleNotFoundError", "cannot import name", "SyntaxError", "IndentationError",
    "INTERNALERROR", "ConftestImportFailure", "error collecting", "errors during collection",
    "unrecognized arguments", "file or directory not found",
)


class HarnessError(RuntimeError):
    """The runner itself could not do its job. Never swallowed — a harness that fails quietly turns
    every mutation after it into an unearned pass."""


# --------------------------------------------------------------------------- the spec


@dataclasses.dataclass(frozen=True)
class Op:
    """ONE deterministic edit. Literal by default; `regex` only where a literal cannot express it.

    `min_count` is asserted, not hoped for. An op that matched fewer times than declared means the source
    moved under the spec, and continuing would run tests against a tree nobody has broken.
    """

    file: str
    find: str
    replace: str
    min_count: int = 1
    regex: bool = False


@dataclasses.dataclass(frozen=True)
class Spec:
    """One property, one way to remove it, and the exact test that must notice."""

    id: str
    prop: str                       # the PROPERTY under attack, in one sentence
    files_allowed: tuple[str, ...]  # nothing outside this may be touched
    ops: tuple[Op, ...]
    min_edits: int
    test_cmd: tuple[str, ...]       # the FOCUSED discriminating command (pytest args)
    expect_test: str                # the test that must fail (bare name; parametrized ids match by prefix)
    expect_contains: str            # a fragment of the intended failure — this is what "for the intended
    #                                 reason" means operationally
    also_expect: tuple[str, ...] = ()   # other tests that must also fail (recorded, and required)
    allow_error_phase: bool = False     # accept a setup/collection <error> as the intended failure


# --------------------------------------------------------------------------- THE INVENTORY
#
# Read against the CURRENT source. Every anchor below was verified to match its declared count in the
# working tree before this file was written, because a stale anchor is exactly how ZERO_EDIT happens and
# ZERO_EDIT reads, at a glance, almost like a pass.

SS = "bruce_engine/semantic_shadow.py"
SCHEMA = "bruce_engine/schema.py"
WORKER = "bruce_engine/worker_api.py"
RUNTIME = "bruce_engine/conversation_runtime.py"
M33 = "migrations/versions/0033_semantic_shadow_jobs.py"
# WHERE EVERYTHING MOVED, and it is the reason this section was rewritten. The undeployed chain 0033..0038
# was collapsed to 0033/0034/0035: the add-then-remove pairs inside it (a broad SELECT policy created in
# 0036 and dropped in 0037, a per-owner population function created in 0037 and dropped in 0038) were
# eliminated rather than carried forward, and every ALTER that patched the shadow table after 0033 was
# folded back into the create_table that builds it. `0036_shadow_canonical_turn.py`,
# `0037_shadow_population_function.py` and `0038_shadow_recon_aggregate.py` DO NOT EXIST.
#
# A SPEC ANCHORED IN A FILE THAT IS NOT THERE IS THE WORST FAILURE THIS RUNNER HAS, and it happened here:
# eleven specs named those three files, four of them died with HARNESS_ERROR and four more went ZERO_EDIT
# on anchors the fold had reformatted. Neither reads as an accusation at a glance, and a run in which a
# third of the inventory silently attacked nothing is a run that certifies nothing. So the anchors below
# are re-read against the current source — the shadow table's DDL at 0033, the aggregate at 0035 — and one
# spec whose target statement no longer exists in ANY file was redesigned rather than repointed (see
# `worker-read-policy-restored`), because repointing a spec at a statement that is gone is how a dead
# anchor comes back wearing a live spec's name.
M35 = "migrations/versions/0035_shadow_reconciliation.py"

T_FENCE = "tests/test_shadow_lease_fencing.py"
T_KEY = "tests/test_shadow_canonical_turn_key.py"
T_ISO = "tests/test_shadow_isolation.py"
T_BOUND = "tests/test_shadow_retry_bound.py"
T_DUR = "tests/test_shadow_retry_durability.py"
T_HEALTH = "tests/test_shadow_health.py"
T_CAP = "tests/test_shadow_capability_denial.py"
T_COV = "tests/test_shadow_intake_coverage.py"
T_RECON = "tests/test_shadow_reconciliation_population.py"
T_PRIV = "tests/test_shadow_population_privilege.py"
T_MIG33 = "tests/test_migration_0033_semantic_shadow_jobs.py"
T_RETRYPG = "tests/test_shadow_retry_state_pg.py"
T_PGINT = "tests/test_postgres_integration.py"


SPECS: tuple[Spec, ...] = (
    # --- the lease fence ----------------------------------------------------------------------------
    Spec(
        id="lease-token-guard",
        prop="a completion write requires the per-claim lease TOKEN, so a worker whose lease expired "
             "mid-read cannot overwrite the observation of the worker that legitimately reclaimed the row",
        files_allowed=(SS,),
        ops=(
            Op(SS, "AND lease_token = CAST(:token AS uuid)", "AND lease_token IS NOT NULL", min_count=2),
            Op(SS,
               "and row.lease_token is not None and row.lease_token == job.lease_token)",
               "and row.lease_token is not None)"),
        ),
        min_edits=3,
        test_cmd=(T_FENCE,),
        expect_test="test_a_stale_worker_sharing_the_lease_owner_writes_nothing",
        expect_contains="a worker that lost its lease wrote its result anyway",
        also_expect=("test_postgres_refuses_a_stale_write_from_an_identical_worker_id",),
    ),
    # --- the two idempotency keys -------------------------------------------------------------------
    Spec(
        id="unique-canonical-key-removed",
        prop="one shadow job per CONVERSATION TURN, stated over the canonical turn id — the only "
             "constraint that can refuse a second observation of one turn offered under different "
             "provider metadata",
        # RE-ANCHORED FROM THE DELETED 0036 INTO 0033. The constraint used to arrive as an
        # `create_unique_constraint` in a later revision; the fold moved it inside `create_table`, so the
        # honest mutation now removes it from the DDL that actually builds the table.
        files_allowed=(SCHEMA, M33),
        ops=(
            Op(SCHEMA,
               '        UniqueConstraint("conversation_turn_id", name="uq_semantic_shadow_job_conversation_turn"),\n',
               ""),
            Op(M33, "            sa.UniqueConstraint(\"conversation_turn_id\", name=CANONICAL_UNIQUE),\n", ""),
        ),
        min_edits=2,
        test_cmd=(T_KEY,),
        expect_test="test_the_canonical_turn_id_alone_stops_a_second_observation_of_one_turn",
        expect_contains="one conversation turn was observed twice under two different provider ids",
    ),
    Spec(
        id="canonical-key-narrowed",
        prop="the ingress-dedupe key keeps `channel`; narrowing it to (user_id, provider_message_id) "
             "collapses two channels' turns into one and SWALLOWS the second — the LOSS bias",
        files_allowed=(SCHEMA, M33),
        ops=(
            Op(SCHEMA,
               'UniqueConstraint("user_id", "channel", "provider_message_id", name="uq_semantic_shadow_job_turn")',
               'UniqueConstraint("user_id", "provider_message_id", name="uq_semantic_shadow_job_turn")'),
            # RE-READ AGAINST THE FOLDED SOURCE: the constraint is one line now and it names the module
            # constant rather than spelling the string out.
            Op(M33,
               'sa.UniqueConstraint("user_id", "channel", "provider_message_id", name=INGRESS_UNIQUE),',
               'sa.UniqueConstraint("user_id", "provider_message_id", name=INGRESS_UNIQUE),'),
        ),
        min_edits=2,
        test_cmd=(T_KEY,),
        expect_test="test_one_message_id_on_two_channels_is_two_turns_and_two_jobs",
        expect_contains="the second channel's turn was swallowed as a duplicate",
    ),
    Spec(
        id="on-conflict-removed",
        prop="enqueue is idempotent by ON CONFLICT DO NOTHING against BOTH keys, so a redelivery is a "
             "`duplicate` and never the `enqueue_failed` counter that means observations are being lost",
        files_allowed=(SS,),
        ops=(Op(SS, "            ON CONFLICT DO NOTHING\n", ""),),
        min_edits=1,
        test_cmd=(T_KEY,),
        expect_test="test_a_duplicate_by_either_key_is_a_duplicate_and_never_a_failed_enqueue",
        expect_contains="recorded as a failed enqueue rather than as a duplicate",
    ),
    # --- the retry machine --------------------------------------------------------------------------
    Spec(
        id="retry-transition",
        prop="a transport failure is REscheduled (a backoff lease it can be reclaimed under), not merely "
             "recorded — a retryable row with no lease is stranded forever and counts as backlog",
        files_allowed=(SS,),
        ops=(Op(SS,
                "        landed = await store.mark_retryable(job, outcome=outcome)",
                "        landed = await store.record(job, outcome=outcome, status=RETRYABLE_FAILED,\n"
                "                                    record=None)"),),
        min_edits=1,
        test_cmd=(T_DUR,),
        expect_test="test_a_transport_failure_comes_back_and_is_eventually_observed",
        expect_contains="the worker never got the job back",
    ),
    Spec(
        id="attempts-on-claim",
        prop="`attempts` advances when a job is CLAIMED, in SQL — a worker that dies before recording "
             "never reaches any Python-side check, so counting on record leaves that path unbounded",
        files_allowed=(SS,),
        ops=(Op(SS, "                attempts = attempts + 1,\n", ""),),
        min_edits=1,
        test_cmd=(T_BOUND + "::test_postgres_stops_claiming_once_the_attempts_are_spent",),
        expect_test="test_postgres_stops_claiming_once_the_attempts_are_spent",
        expect_contains="kept handing out a job that had already spent every attempt",
    ),
    Spec(
        id="exhausted-reclaim",
        prop="`attempts < max_attempts` lives in the claim itself, so a turn that reliably kills its "
             "reader stops being re-claimed instead of burning a slot on every wake forever",
        files_allowed=(SS,),
        ops=(
            Op(SS,
               "            claimable = j.attempts < j.max_attempts and (j.status == PENDING or (",
               "            claimable = (j.status == PENDING or ("),
            Op(SS, "                WHERE attempts < max_attempts\n", ""),
        ),
        min_edits=2,
        test_cmd=(T_BOUND + "::test_a_job_that_crashes_before_recording_still_runs_out_of_attempts",),
        expect_test="test_a_job_that_crashes_before_recording_still_runs_out_of_attempts",
        expect_contains="claimed past max_attempts",
    ),
    Spec(
        id="failed-as-completed",
        prop="a failed observation is NEVER filed as `completed` — a completed row with no reading is "
             "exactly the record that makes the collected sample look cleaner than the executive is",
        files_allowed=(SS,),
        ops=(Op(SS, "    return TERMINAL_FAILED\n", "    return COMPLETED\n"),),
        min_edits=1,
        test_cmd=(T_ISO + "::test_a_broken_read_becomes_a_typed_outcome_and_is_never_completed",),
        expect_test="test_a_broken_read_becomes_a_typed_outcome_and_is_never_completed",
        expect_contains="completed",
    ),
    # --- the request path ---------------------------------------------------------------------------
    Spec(
        id="awaited-read",
        prop="the request path writes ONE row and calls no model — awaiting the read inline put the whole "
             "triage deadline on a student's reply while authority was off the entire time",
        files_allowed=(SS,),
        ops=(Op(SS,
                "    excluded = disposition != ELIGIBLE\n    try:",
                "    excluded = disposition != ELIGIBLE\n"
                "    from . import semantic_executive as _mutation_se\n"
                "    await _mutation_se.interpret(None)\n"
                "    try:"),),
        min_edits=1,
        test_cmd=(T_ISO + "::test_enqueueing_never_invokes_the_executive",),
        expect_test="test_enqueueing_never_invokes_the_executive",
        expect_contains="the request path invoked the semantic executive",
    ),
    Spec(
        id="snapshot-to-global",
        prop="capability truth is the PER-USER broker's answer, not `tool_registry.specs(None)` — a "
             "global list makes an unconnected student's turn evidence against a router that was right",
        files_allowed=(SS,),
        ops=(Op(SS,
                "        from . import tool_broker\n"
                "        snap = await tool_broker.reachable_operations(user_id)\n"
                "        return ReachableOperations(frozenset(snap.operations), established=snap.established)",
                "        from . import tool_registry\n"
                "        return ReachableOperations(\n"
                "            frozenset(s.capability for s in tool_registry.specs(None) if s.live),\n"
                "            established=True)"),),
        min_edits=1,
        test_cmd=(T_CAP + "::test_two_users_on_the_same_turn_get_different_reachable_sets",),
        expect_test="test_two_users_on_the_same_turn_get_different_reachable_sets",
        # The FIRST assertion the global registry trips, and it is the sharpest statement of the
        # property: a calendar-scoped grant reaching `gmail.send_message` is `tool_registry.specs(None)`
        # answering for a user it was never asked about. (The broader "two users got the SAME reachable
        # set" assertion is two lines further down and never reached — pytest stops at the first.)
        expect_contains="a calendar-only grant reached Gmail",
    ),
    Spec(
        id="worker-recompute",
        prop="the comparison uses the turn's PERSISTED reachable snapshot; recomputing at drain time "
             "rewrites what the row says happened on a turn that is already over",
        files_allowed=(SS,),
        ops=(Op(SS,
                "        cmp = compare(turn, job.router, reachable=job.reachable.operations)",
                "        cmp = compare(turn, job.router,\n"
                "                      reachable=(await turn_capability_truth(job.user_id)).operations)"),),
        min_edits=1,
        # WHICH DIRECTION ACTUALLY DISCRIMINATES, and it is worth stating because the obvious choice does
        # not. The CONNECT direction (`test_connecting_an_integration_after_enqueue...`) cannot catch a
        # comparison-only recompute: the executive's CONTEXT still comes from the persisted snapshot, so
        # an operation the snapshot never contained can never be named, and there is nothing for a wider
        # reachable set to reclassify. That is defence in depth working, not a gap. The DISCONNECT
        # direction is the one that bites — the snapshot held the capability, the broker no longer does,
        # and a recomputed set silently drops a real finding about what the router did during the turn.
        test_cmd=(T_CAP,),
        expect_test="test_a_disconnection_after_enqueue_does_not_erase_a_recorded_denial",
        expect_contains="a revocation AFTER the turn erased a real finding",
        also_expect=("test_the_denial_is_classified_during_observation_and_stored_on_the_row",),
    ),
    Spec(
        id="continuation-skips-intake",
        prop="intake is ONE unconditional call above the routing branches — nested in the `else` of "
             "`if resolved_continuation:` it silently excluded every approval, rejection and continuation",
        files_allowed=(RUNTIME,),
        ops=(Op(RUNTIME,
                "        await semantic_shadow.intake(\n"
                "            user_id, channel=ch, provider_message_id=pmid, decision=shadow_production,",
                "        if not resolved_continuation:\n"
                "         await semantic_shadow.intake(\n"
                "            user_id, channel=ch, provider_message_id=pmid, decision=shadow_production,"),),
        min_edits=1,
        test_cmd=(T_COV + "::test_every_trusted_turn_is_observed_including_the_continuation_and_the_approval",),
        expect_test="test_every_trusted_turn_is_observed_including_the_continuation_and_the_approval",
        expect_contains="shadow rows for 3 trusted turns",
    ),
    # --- the telemetry ------------------------------------------------------------------------------
    Spec(
        id="metrics-wiring",
        prop="`/process` actually CALLS the measurement — counts()/backlog() with no production caller "
             "is the same blindness as the detached task they replaced, only slower to notice",
        files_allowed=(WORKER,),
        ops=(
            Op(WORKER, "        shadow_health = await semantic_shadow.health()", "        shadow_health = {}"),
            Op(WORKER, "        backlog_before = await semantic_shadow.backlog()",
               "        backlog_before = backlog_before"),
        ),
        min_edits=2,
        test_cmd=(T_HEALTH + "::test_the_worker_wake_emits_shadow_health_and_sweeps_exhausted_jobs",),
        expect_test="test_the_worker_wake_emits_shadow_health_and_sweeps_exhausted_jobs",
        expect_contains="shadow_health",
    ),
    Spec(
        id="unclassified-emission",
        prop="an outcome no bucket claims is EMITTED under its own name — it was computed in `_tally` and "
             "then dropped by a hand-written key list, so a new outcome vanished from the numbers",
        files_allowed=(SS,),
        ops=(Op(SS, "            UNCLASSIFIED_OUTCOME: q[UNCLASSIFIED_OUTCOME],\n", ""),),
        min_edits=1,
        test_cmd=(T_HEALTH + "::test_an_outcome_no_bucket_claims_is_emitted_under_its_own_name",),
        expect_test="test_an_outcome_no_bucket_claims_is_emitted_under_its_own_name",
        expect_contains="unclassified_outcome",
    ),
    # --- RLS -----------------------------------------------------------------------------------------
    Spec(
        id="rls-inventory",
        prop="`semantic_shadow_jobs` is inside the enforced RLS inventory — it carried its policy from "
             "the day 0033 landed and was in no list, so the all-tables guarantee SKIPPED it entirely",
        files_allowed=(M33,),
        ops=(Op(M33, '    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")\n', ""),),
        min_edits=1,
        test_cmd=(T_PGINT + "::test_08_force_rls_enabled_on_all_user_tables",),
        expect_test="test_08_force_rls_enabled_on_all_user_tables",
        expect_contains="rowsecurity/force",
    ),
    # --- the reconciliation's population -------------------------------------------------------------
    #
    # THE POPULATION MOVED. It used to be a list this process built and looped, so every spec in this
    # section anchored in `semantic_shadow`. The comparison now happens inside the definer, so the
    # population is a CTE in the SQL 0035 generates and the specs that attack it anchor there. Two specs
    # that lived here were RETIRED rather than re-anchored, because the code they attacked is gone and a
    # mutation against a property nobody implements proves nothing:
    #
    #   recon-clean-with-zero-checked             `users == 0 -> FAILED` was a Python rule over a
    #                                             per-owner population. There is no owner count out here
    #                                             any more, and the empty-population collapse it guarded
    #                                             is not expressible: the verdict is decided in SQL from
    #                                             anti-joins, and an empty window is genuinely clean.
    #   recon-clean-with-zero-checked-report-site the same escape, one layer out. Superseded by
    #                                             `verdict-recomputed` below, which attacks the same site
    #                                             against fixtures that actually discriminate — the tests
    #                                             it named no longer exist.
    Spec(
        id="recon-users-from-jobs",
        prop="the population is read from the CANONICAL ledger, never from the shadow jobs — a user whose "
             "intake never ran has no job, so a population built from jobs cannot contain them",
        files_allowed=(M35,),
        # RE-ANCHORED INTO THE SQL. The denominator is now the `t` CTE, and pointing it at
        # `semantic_shadow_jobs` is the same defect the old Python version had: the check would be asking
        # the thing being checked which rows to check.
        ops=(Op(M35,
                "    WITH t AS (\n"
                "        SELECT ct.id AS turn_id\n"
                "          FROM public.conversation_turns ct\n"
                "         WHERE ct.role = 'user'\n"
                "           AND ct.created_at >= p_window_start\n"
                "           AND ct.created_at <  p_window_end\n"
                "    ),",
                "    WITH t AS (\n"
                "        SELECT sj0.conversation_turn_id AS turn_id\n"
                "          FROM public.semantic_shadow_jobs sj0\n"
                "         WHERE sj0.conversation_turn_id IS NOT NULL\n"
                "           AND sj0.created_at >= p_window_start\n"
                "           AND sj0.created_at <  p_window_end\n"
                "    ),"),),
        min_edits=1,
        test_cmd=(T_RECON + "::test_a_user_with_turns_and_no_shadow_job_at_all_fails_the_check",
                  T_RECON + "::test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them",
                  T_PRIV + "::test_two_students_missing_intake_aggregate_to_exactly_two_without_naming_either"),
        expect_test="test_a_user_with_turns_and_no_shadow_job_at_all_fails_the_check",
        expect_contains="no shadow row at all reconciled cleanly",
        # THE CROSS-TENANT SUM IS CO-REQUIRED, and it is a different assertion from the two above rather
        # than a third copy of them. Those go through the production caller and would still fail if the
        # PYTHON side lost the population; this one calls the function as the worker over two tenants that
        # own no shadow row at all, so a denominator taken from the jobs makes it answer 0 out of 2.
        also_expect=("test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them",
                     "test_two_students_missing_intake_aggregate_to_exactly_two_without_naming_either"),
    ),
    Spec(
        id="recon-skip-zero-job-users",
        prop="owners with ZERO shadow rows stay in the population — they are the entire point, and they "
             "are precisely the ones any inner join drops",
        files_allowed=(M35,),
        # The LEFT JOIN is the whole reason "no row" is representable. An inner join silently reduces the
        # population to the turns shadow already knows about — self-agreement again, one layer down.
        ops=(Op(M35, "          LEFT JOIN public.semantic_shadow_jobs sj\n",
                "          JOIN public.semantic_shadow_jobs sj\n"),),
        min_edits=1,
        test_cmd=(T_RECON + "::test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them",),
        expect_test="test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them",
        expect_contains="an affected owner's turns were left out of the count",
    ),
    Spec(
        id="missing-intake-from-shadow",
        prop="THE GAP IS AN ANTI-JOIN OVER THE CANONICAL LEDGER, never `turns - eligible - excluded` — "
             "two totals that come from the same bookkeeping agree with each other while both are wrong, "
             "which is the failure this area has now produced three times",
        files_allowed=(M35,),
        # RE-ANCHORED ONTO `is_gap`. The predicate is computed once per turn in the `j` CTE now, so the
        # subtraction has to replace BOTH readers of it — the count and the verdict — or the two disagree
        # and the mutation is caught by a consistency check rather than by the property under attack.
        ops=(
            Op(M35,
               "        pg_catalog.count(*) FILTER (WHERE j.is_gap),",
               "        (pg_catalog.count(*)\n"
               "         - pg_catalog.count(*) FILTER (WHERE j.has_intake\n"
               "                                         AND j.intake_disposition = '{D_ELIGIBLE}')\n"
               "         - pg_catalog.count(*) FILTER (WHERE j.has_intake\n"
               "                                         AND j.intake_disposition <> '{D_ELIGIBLE}')),"),
            Op(M35,
               "        CASE WHEN pg_catalog.count(*) FILTER (WHERE j.is_gap) > 0",
               "        CASE WHEN (pg_catalog.count(*)\n"
               "                   - pg_catalog.count(*) FILTER (WHERE j.has_intake\n"
               "                                                   AND j.intake_disposition = '{D_ELIGIBLE}')\n"
               "                   - pg_catalog.count(*) FILTER (WHERE j.has_intake\n"
               "                                                   AND j.intake_disposition <> '{D_ELIGIBLE}')) > 0"),
        ),
        min_edits=2,
        # WHY THIS TEST AND NOT THE ZERO-SHADOW-ROW ONE. A student with no intake at all is still caught
        # by the subtraction (nothing was subtracted), so that fixture cannot tell the two formulations
        # apart. The discriminating population is the one where every turn HAS an intake and the equation
        # therefore balances perfectly — an eligible row parked in a status no queue state accounts for.
        test_cmd=(T_PRIV + "::test_an_eligible_intake_with_no_job_in_any_queue_state_is_a_gap",),
        expect_test="test_an_eligible_intake_with_no_job_in_any_queue_state_is_a_gap",
        expect_contains="was counted as observed. The gap",
    ),
    Spec(
        id="eligibility-from-queue-status",
        prop="ELIGIBILITY IS READ FROM `intake_disposition`, NOT FROM `status` — what INTAKE decided and "
             "what the QUEUE holds are two different columns, and taking both sides of the anti-join off "
             "the queue status makes it unsatisfiable by construction",
        files_allowed=(M35,),
        # THE SUBTLEST FORM OF THIS MODULE'S ONE BUG, and the reason it needed its own spec: it is not a
        # subtraction and it is not a population taken from the jobs. It is a genuine per-row anti-join
        # whose two sides happen to be the SAME COLUMN — so a row edited out of the queue leaves
        # `eligible` at the identical instant it leaves the queue, the totals go on balancing, and a turn
        # nobody will ever read reports clean while carrying no exclusion reason at all.
        ops=(
            Op(M35,
               "               (sj.id IS NULL\n"
               "                OR (sj.intake_disposition = '{D_ELIGIBLE}'\n"
               "                    AND sj.status NOT IN ({_QUEUE_LIST})))     AS is_gap",
               "               (sj.id IS NULL\n"
               "                OR (sj.status <> 'excluded'\n"
               "                    AND sj.status NOT IN ({_QUEUE_LIST})))     AS is_gap"),
            Op(M35,
               "        pg_catalog.count(*) FILTER (WHERE j.has_intake AND j.intake_disposition <> '{D_ELIGIBLE}'),\n"
               "        pg_catalog.count(*) FILTER (WHERE j.has_intake AND j.intake_disposition = '{D_ELIGIBLE}'),",
               "        pg_catalog.count(*) FILTER (WHERE j.job_status = 'excluded'),\n"
               "        pg_catalog.count(*) FILTER (WHERE j.has_intake AND j.job_status <> 'excluded'),"),
        ),
        min_edits=2,
        # THE DISCRIMINATING FIXTURE. `quarantined` is caught by both formulations, so it proves nothing
        # here. The population that tells them apart is a row parked in the `excluded` STATUS whose
        # DISPOSITION still says eligible — under the mutation it silently becomes an accounted exclusion.
        test_cmd=(T_PRIV + "::test_an_eligible_intake_parked_in_the_excluded_status_is_a_gap",),
        expect_test="test_an_eligible_intake_parked_in_the_excluded_status_is_a_gap",
        expect_contains="was counted as deliberately excluded",
    ),
    Spec(
        id="recon-failure-as-empty",
        prop="a ledger that could not be read is `unknown` with NULL counts — a fabricated zero says the "
             "sample is complete at the exact moment nobody can tell",
        files_allowed=(SS,),
        # RE-WRITTEN, AND THE OLD FORM IS WHY. It used to flip `LEDGER_UNPROVEN` to `LEDGER_PROVEN` at
        # both call sites. `reconciliation_verdict` now demands a proven read AND a database verdict from
        # the vocabulary, and these two sites pass no verdict at all — so the old flip lands on disk and
        # changes nothing, which would report ESCAPED against a property that is in fact guarded twice
        # over. The honest mutation is the one an author would actually write: fabricate the clean answer.
        ops=(
            Op(SS,
               "        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_UNPROVEN),\n"
               "                      report=None, reason=type(exc).__name__)",
               "        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_PROVEN,\n"
               "                                             database_status=RECON_CLEAN,\n"
               "                                             **{k: 0 for k in DEPENDENT_COUNTS}),\n"
               "                      report=None, reason=type(exc).__name__)"),
            Op(SS,
               "        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_UNPROVEN),\n"
               "                      report=report, reason=report.unreadable_reason)",
               "        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_PROVEN,\n"
               "                                             database_status=RECON_CLEAN,\n"
               "                                             **{k: 0 for k in DEPENDENT_COUNTS}),\n"
               "                      report=report, reason=report.unreadable_reason)"),
        ),
        min_edits=2,
        test_cmd=(T_RECON + "::test_the_offline_store_reaches_the_unknown_state_too",
                  T_RECON + "::test_a_ledger_query_that_raises_is_unknown_rather_than_clean"),
        expect_test="test_the_offline_store_reaches_the_unknown_state_too",
        expect_contains='turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN',
        also_expect=("test_a_ledger_query_that_raises_is_unknown_rather_than_clean",),
    ),
    Spec(
        id="verdict-recomputed",
        prop="THE DATABASE'S VERDICT IS PUBLISHED, NOT RE-DERIVED. It was decided in the same statement "
             "and the same snapshot as the counts, from anti-joins the caller cannot see; a status "
             "computed twice from the same numbers agrees today and drifts the first time either copy is "
             "edited, and the drift is invisible because both look reasonable",
        files_allowed=(SS,),
        ops=(Op(SS,
                "        return {\n"
                "            **verdict,\n"
                '            "window_start": _iso(report.window_start) if report else None,',
                "        published = dict(verdict)\n"
                "        if report is not None and report.counts:\n"
                "            c = report.counts\n"
                "            published = {\n"
                "                **{k: int(c.get(k) or 0) for k in DEPENDENT_COUNTS},\n"
                '                "reconciliation_status": (\n'
                "                    RECON_CLEAN\n"
                '                    if (int(c.get("canonical_trusted_turns") or 0)\n'
                '                        == int(c.get("eligible_turns") or 0)\n'
                '                        + int(c.get("explicitly_excluded_turns") or 0)\n'
                '                        and int(c.get("trusted_turns_without_intake") or 0) == 0)\n'
                "                    else RECON_FAILED)}\n"
                "        return {\n"
                "            **published,\n"
                '            "window_start": _iso(report.window_start) if report else None,'),),
        min_edits=1,
        # BOTH DIRECTIONS ARE REQUIRED, and that is the correction this round makes. The previous version
        # of this spec was satisfied by a fixture where the re-derivation and the verdict AGREED, so a
        # report site that recomputed passed it. These two fixtures are built so a re-derivation gives the
        # OPPOSITE answer: a database `failed` beside counts that balance, and a database `unknown`
        # beside counts that look completely clean.
        test_cmd=(T_RECON + "::test_a_database_failure_survives_counts_that_would_re_derive_as_clean",
                  T_RECON + "::test_a_database_unknown_survives_counts_that_would_re_derive_as_clean"),
        expect_test="test_a_database_failure_survives_counts_that_would_re_derive_as_clean",
        expect_contains="no shadow job in any queue state and said FAILED",
        also_expect=("test_a_database_unknown_survives_counts_that_would_re_derive_as_clean",),
    ),
    Spec(
        id="restore-owner-loop",
        prop="THE INCIDENT ITSELF: no owner list crosses the aggregate boundary and no per-tenant read is "
             "opened to finish the join. A population the worker can assemble is a population derived "
             "from the thing being checked — a user whose intake never ran owns no job and is invisible "
             "to it",
        files_allowed=(SS,),
        # HOW IT WOULD REALLY COME BACK. Not by reverting a migration: `semantic_shadow_jobs` carries the
        # `tenant_or_worker` policy, so a WORKER session can already list its owners — which is exactly
        # the path the row grant opened. Someone re-adds `observed_for`, loops those owners, every read is
        # authorized, every number looks plausible, and the owners with no jobs are gone.
        ops=(
            Op(SS,
               "            row = (await s.execute(\n"
               '                sa_text(f"SELECT window_start, window_end, watermark, {columns}, "\n'
               '                        f"reconciliation_status FROM public.{RECONCILIATION_FUNCTION}(:lo, :hi)"),\n'
               '                {"lo": lo, "hi": hi})).mappings().first()\n',
               "            owners = [r[\"user_id\"] for r in (await s.execute(\n"
               '                sa_text(f"SELECT DISTINCT user_id FROM {TABLE}"))).mappings().all()]\n'
               '        row = {"window_start": lo, "window_end": hi, "watermark": hi,\n'
               "               **{name: 0 for name in RECONCILIATION_COUNTS}}\n"
               "        for _owner in owners:\n"
               "            per = await self.observed_for(_owner)\n"
               '            row["canonical_trusted_turns"] += per["trusted_inbound_turns"]\n'
               '            row["trusted_turns_with_intake"] += per["observed"]\n'
               '            row["shadow_jobs"] += per["observed"]\n'
               '            row["eligible_turns"] += per["eligible"]\n'
               '            row["explicitly_excluded_turns"] += per["excluded"]\n'
               '            row["unobserved_turns"] += per["unobserved"]\n'
               '        row["reconciliation_status"] = (\n'
               '            RECON_FAILED if row["unobserved_turns"] else RECON_CLEAN)\n'),
            Op(SS,
               "    async def reconcile(self, user_id: UUID) -> dict:\n"
               '        """Count this user\'s trusted inbound turns from the CANONICAL LEDGER',
               "    async def observed_for(self, user_id: UUID) -> dict:\n"
               '        """RESTORED BY MUTATION: the per-tenant read the owner loop needs to finish the\n'
               '        join out here. This is the per-tenant read the aggregate boundary removed."""\n'
               "        return await self.reconcile(user_id)\n"
               "\n"
               "    async def reconcile(self, user_id: UUID) -> dict:\n"
               '        """Count this user\'s trusted inbound turns from the CANONICAL LEDGER'),
        ),
        min_edits=2,
        test_cmd=(T_RECON + "::test_a_user_with_turns_and_no_shadow_job_at_all_fails_the_check",
                  T_RECON + "::test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them"),
        expect_test="test_a_user_with_turns_and_no_shadow_job_at_all_fails_the_check",
        expect_contains="no shadow row at all reconciled cleanly",
        also_expect=("test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them",),
    ),
    # --- migration 0033's own DDL, which `create_all` had kept unreachable ---------------------------
    Spec(
        id="mig33-table-ddl",
        prop="0033's create_table branch builds the REAL table — it had never executed anywhere, because "
             "`create_all()` always got there first, so its column list was under no test at all",
        files_allowed=(M33,),
        # RE-TARGETED, AND THE REASON MATTERS. This spec used to delete the `max_attempts` column. After
        # the fold, 0033 also runs `_retry_bound_cannot_be_zero()`, which ALTERs that column — so deleting
        # it makes the MIGRATION raise, alembic exits non-zero, and the fixture blows up in setup. That is
        # an INVALID_PROOF: it shows the line was needed for the chain to RUN, which is a different and
        # much weaker claim than "the create_table branch builds the real table". `lease_owner` is in the
        # same asserted column list, is touched by no later statement, and lets the migration succeed and
        # the ASSERTION fail — which is the evidence this spec is supposed to produce.
        ops=(Op(M33, '            sa.Column("lease_owner", sa.String(64), nullable=True),\n', ""),),
        min_edits=1,
        test_cmd=(T_MIG33 + "::test_0033_creates_the_table_when_it_is_genuinely_absent",),
        expect_test="test_0033_creates_the_table_when_it_is_genuinely_absent",
        expect_contains="lease_owner",
    ),
    Spec(
        id="mig33-unique-weakened",
        prop="0033's unique constraint is over EXACTLY (user_id, channel, provider_message_id) — a "
             "narrower key still satisfies 'a unique constraint exists' while swallowing a turn",
        files_allowed=(M33,),
        ops=(Op(M33,
                'sa.UniqueConstraint("user_id", "channel", "provider_message_id", name=INGRESS_UNIQUE),',
                'sa.UniqueConstraint("user_id", "provider_message_id", name=INGRESS_UNIQUE),'),),
        min_edits=1,
        test_cmd=(T_MIG33 + "::test_0033_creates_the_intended_unique_constraint_with_exactly_those_columns",),
        expect_test="test_0033_creates_the_intended_unique_constraint_with_exactly_those_columns",
        expect_contains="the unique key is over",
    ),
    Spec(
        id="mig33-rls-removed",
        prop="0033 FORCES row level security — ENABLE alone leaves the table owner, and anything running "
             "as it, outside the policy",
        files_allowed=(M33,),
        ops=(Op(M33, '    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")\n', ""),),
        min_edits=1,
        test_cmd=(T_MIG33 + "::test_0033_enables_and_forces_row_level_security",),
        expect_test="test_0033_enables_and_forces_row_level_security",
        expect_contains="not FORCED",
    ),
    Spec(
        id="mig33-policy-removed",
        prop="0033 creates `tenant_or_worker` with a real predicate — FORCE RLS with no policy (or with "
             "`USING (true)`) satisfies every flag test in the suite and isolates nothing",
        files_allowed=(M33,),
        ops=(Op(M33,
                '        op.execute(\n'
                '            f"CREATE POLICY tenant_or_worker ON {TABLE} "\n'
                '            f"USING (user_id = app_current_user() OR app_is_worker()) "\n'
                '            f"WITH CHECK (user_id = app_current_user() OR app_is_worker())"\n'
                '        )',
                "        pass"),),
        min_edits=1,
        test_cmd=(T_MIG33 + "::test_0033_creates_the_tenant_or_worker_policy",),
        expect_test="test_0033_creates_the_tenant_or_worker_policy",
        expect_contains="did not create the tenant_or_worker policy",
    ),
    # --- the retry bound and the cascade, as the DATABASE holds them ---------------------------------
    Spec(
        id="max-attempts-default-zero",
        prop="a job written with only the defaults is CLAIMABLE — at a bound of 0, `attempts < "
             "max_attempts` is false the instant the row exists: 100% silent loss, empty backlog, no error",
        files_allowed=(SCHEMA, M33),
        # ONE CONSTANT NOW DRIVES BOTH OF 0033'S PATHS. The fold replaced the literal `3` in the column
        # definition with `DEFAULT_MAX_ATTEMPTS`, which is also what `_retry_bound_cannot_be_zero()` ALTERs
        # the default to — so flipping the constant poisons the create_table path AND the create_all
        # repair path, which is exactly the reach the real defect would have had.
        ops=(
            Op(SCHEMA,
               '    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))\n'
               "    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)\n"
               "    # THE FENCE (migration 0034).",
               '    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))\n'
               "    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)\n"
               "    # THE FENCE (migration 0034)."),
            Op(M33, "DEFAULT_MAX_ATTEMPTS = 3", "DEFAULT_MAX_ATTEMPTS = 0"),
        ),
        min_edits=2,
        test_cmd=(T_RETRYPG + "::test_a_job_can_never_be_written_with_a_retry_bound_of_zero",),
        expect_test="test_a_job_can_never_be_written_with_a_retry_bound_of_zero",
        expect_contains="ck_semantic_shadow_job_max_attempts_positive",
    ),
    Spec(
        id="max-attempts-check-removed",
        prop="`CHECK (max_attempts > 0)` makes the silent-total-loss state UNREPRESENTABLE rather than "
             "merely unintended — a convention cannot refuse a careless caller",
        # RE-ANCHORED, AND IT NOW TAKES THREE EDITS RATHER THAN TWO. The fold gave 0033 both halves of
        # this constraint — one inside `create_table` for a database the migration builds, and one in
        # `_retry_bound_cannot_be_zero()` for the database `create_all()` built first. Removing either
        # alone leaves the other establishing the property, and the mutation would report ESCAPED against
        # a contract that is in fact enforced twice.
        files_allowed=(SCHEMA, M33),
        ops=(
            Op(SCHEMA,
               '        CheckConstraint("max_attempts > 0", name="ck_semantic_shadow_job_max_attempts_positive"),\n',
               ""),
            Op(M33,
               '        op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {MAX_ATTEMPTS_CHECK} CHECK (max_attempts > 0)")',
               "        pass"),
            Op(M33, '            sa.CheckConstraint("max_attempts > 0", name=MAX_ATTEMPTS_CHECK),\n', ""),
        ),
        min_edits=3,
        test_cmd=(T_RETRYPG + "::test_a_job_can_never_be_written_with_a_retry_bound_of_zero",),
        expect_test="test_a_job_can_never_be_written_with_a_retry_bound_of_zero",
        expect_contains="DID NOT RAISE",
    ),
    Spec(
        id="fk-cascade-removed",
        prop="the shadow row's FK to the canonical turn is ON DELETE CASCADE — telemetry that outlives "
             "the message it describes is a deletion that did not happen",
        # RE-ANCHORED INTO 0033, WHICH NOW HOLDS ALL THREE STATEMENTS. The FK is declared in
        # `create_table`, repaired in `_canonical_turn_fk_cascades()` for the `create_all()` path, and
        # that repair is GUARDED by "is any existing FK already a cascade". Weakening the two declarations
        # without also weakening the guard leaves the repair silently putting CASCADE back, so the tree
        # would be mutated and the property would not be.
        files_allowed=(SCHEMA, M33),
        ops=(
            Op(SCHEMA, 'ForeignKey("conversation_turns.id", ondelete="CASCADE")',
               'ForeignKey("conversation_turns.id", ondelete="RESTRICT")'),
            Op(M33, 'sa.ForeignKey(f"{TURNS}.id", ondelete="CASCADE", name=TURN_FK)',
               'sa.ForeignKey(f"{TURNS}.id", ondelete="RESTRICT", name=TURN_FK)'),
            Op(M33, '["conversation_turn_id"], ["id"], ondelete="CASCADE")',
               '["conversation_turn_id"], ["id"], ondelete="RESTRICT")'),
            Op(M33, '    if any(r["confdeltype"] == "c" for r in rows):', "    if rows:"),
        ),
        min_edits=4,
        test_cmd=(T_KEY + "::test_deleting_the_canonical_turn_deletes_the_telemetry_that_points_at_it",),
        expect_test="test_deleting_the_canonical_turn_deletes_the_telemetry_that_points_at_it",
        expect_contains="violates foreign key constraint",
    ),
    # --- the SECURITY DEFINER boundary that replaced the row grant ------------------------------------
    #
    # EVERY SPEC BELOW ANCHORS IN 0035, which is the only revision that builds the function. It is worth
    # saying why that is not a detail: while the chain still had a per-owner definer in 0037 and a
    # replacement in 0038, a mutation to 0037's body applied cleanly, ran in the chain, and produced NO
    # object in the database under test, because 0038 dropped it unconditionally. Those specs would have
    # reported ESCAPED against contracts that are in fact enforced — a stale anchor arriving in the shape
    # of a test-gap accusation, which is the most expensive way this runner can be wrong.
    Spec(
        id="secdef-returns-text",
        prop="THE PRIVACY CONTRACT IS THE COLUMN LIST — the whole argument for a function over a policy "
             "is that a function names its columns and a policy cannot",
        files_allowed=(M35,),
        ops=(
            Op(M35, "               reconciliation_status        text)",
               "               reconciliation_status        text,\n"
               "               turn_text                    text)"),
            Op(M35, "        SELECT ct.id AS turn_id\n",
               "        SELECT ct.id AS turn_id, ct.text AS turn_text\n"),
            Op(M35, "        SELECT t.turn_id                                       AS turn_id,\n",
               "        SELECT t.turn_id                                       AS turn_id,\n"
               "               t.turn_text                          AS turn_text,\n"),
            Op(M35,
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END\n      FROM j;",
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END,\n"
               "        pg_catalog.max(j.turn_text)\n      FROM j;"),
        ),
        min_edits=4,
        test_cmd=(T_PRIV,),
        expect_test="test_the_result_carries_no_message_content",
        expect_contains="came back from the reconciliation",
        also_expect=("test_the_declared_result_type_has_no_user_id_and_no_turn_id",
                     "test_the_hardening_and_privilege_gates_pass_as_shipped"),
    ),
    Spec(
        id="agg-returns-user-id",
        prop="THE ENUMERATION, IN ITS EXACT HISTORICAL FORM: no user_id leaves the function. The "
             "per-owner definer carried no message text and every column was defensible on its own, and the "
             "one owner id in it is what the worker turned into a per-tenant read of every turn in the "
             "window",
        files_allowed=(M35,),
        ops=(
            Op(M35, "               reconciliation_status        text)",
               "               reconciliation_status        text,\n"
               "               user_id                      uuid)"),
            Op(M35, "        SELECT ct.id AS turn_id\n",
               "        SELECT ct.id AS turn_id, ct.user_id AS owner_id\n"),
            Op(M35, "        SELECT t.turn_id                                       AS turn_id,\n",
               "        SELECT t.turn_id                                       AS turn_id,\n"
               "               t.owner_id                           AS owner_id,\n"),
            # `max()` has no uuid overload, so the id is aggregated as text and cast back. The COLUMN's
            # declared type is what the contract reads, and it is `uuid` either way.
            Op(M35,
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END\n      FROM j;",
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END,\n"
               "        pg_catalog.max(j.owner_id::text)::uuid\n      FROM j;"),
        ),
        min_edits=4,
        test_cmd=(T_PRIV,),
        # THE BEHAVIOURAL ASSERTION, not the declared one. A real owner id comes back in a real row, so
        # this fails on the data rather than on the catalog — the catalog half is co-required below.
        expect_test="test_no_value_in_the_result_can_be_turned_back_into_an_owner",
        expect_contains="an owner id came back from the reconciliation",
        also_expect=("test_the_declared_result_type_has_no_user_id_and_no_turn_id",
                     "test_the_hardening_and_privilege_gates_pass_as_shipped"),
    ),
    Spec(
        id="agg-returns-turn-id",
        prop="NO IDENTIFIER OF ANY KIND leaves the function, not merely no user_id. A turn id names a row "
             "in a tenant-isolated table, and the rule that cannot be evaded by renaming a column is "
             "'no uuid comes back at all'",
        files_allowed=(M35,),
        ops=(
            Op(M35, "               reconciliation_status        text)",
               "               reconciliation_status        text,\n"
               "               min_turn_id                  uuid)"),
            Op(M35,
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END\n      FROM j;",
               "             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END,\n"
               "        pg_catalog.min(j.turn_id::text)::uuid\n      FROM j;"),
        ),
        min_edits=2,
        test_cmd=(T_PRIV,),
        # A DIFFERENT ASSERTION FROM THE ONE ABOVE, deliberately: no owner id is present, so the first
        # assertion in this test passes and the SHAPE check is what fires. That is the whole point of
        # having a shape check beside the name check.
        expect_test="test_no_value_in_the_result_can_be_turned_back_into_an_owner",
        expect_contains="a uuid appeared in the reconciliation result",
        also_expect=("test_the_declared_result_type_has_no_user_id_and_no_turn_id",
                     "test_the_hardening_and_privilege_gates_pass_as_shipped"),
    ),
    Spec(
        id="secdef-search-path-removed",
        prop="a SECURITY DEFINER function without a pinned search_path resolves the CALLER's path while "
             "running as a BYPASSRLS role — the classic privilege-escalation hole, not a lint",
        files_allowed=(M35,),
        ops=(Op(M35, "SET search_path = pg_catalog, public\n", ""),),
        min_edits=1,
        test_cmd=(T_PRIV,),
        expect_test="test_the_hardening_and_privilege_gates_pass_as_shipped",
        expect_contains="search_path",
    ),
    Spec(
        id="secdef-public-execute",
        prop="EXECUTE is REVOKED from PUBLIC — a function is EXECUTE-to-PUBLIC by default, and a "
             "privilege that arrives by default is a privilege nobody decided to grant",
        files_allowed=(M35,),
        ops=(Op(M35,
                '    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_SIG} FROM PUBLIC")\n'
                '    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION_SIG} FROM PUBLIC")\n',
                ""),),
        min_edits=1,
        test_cmd=(T_PRIV,),
        expect_test="test_public_cannot_execute_the_function",
        expect_contains="PUBLIC can execute the reconciliation",
        also_expect=("test_the_hardening_and_privilege_gates_pass_as_shipped",),
    ),
    Spec(
        id="secdef-owner-login",
        prop="the definer's owner is NOLOGIN. It is a BYPASSRLS role that can read every tenant's turns; "
             "a login on it is a credential that reaches the whole ledger and that no deployment step "
             "ever decided to create",
        files_allowed=(M35,),
        ops=(Op(M35, 'f"ALTER ROLE {RECON_OWNER} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "',
                'f"ALTER ROLE {RECON_OWNER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "'),),
        min_edits=1,
        test_cmd=(T_PRIV,),
        expect_test="test_the_owner_holds_select_on_only_the_two_tables_it_reads",
        expect_contains="the definer's owner can log in",
        also_expect=("test_the_hardening_and_privilege_gates_pass_as_shipped",),
    ),
    Spec(
        id="worker-bypassrls",
        prop="THE WORKER ROLE GETS NO BYPASSRLS. The whole aggregate boundary exists so the drain can "
             "count across tenants without being able to read across them; a BYPASSRLS app role makes the "
             "function decorative and re-opens every column of every student's turn",
        files_allowed=(M35,),
        ops=(Op(M35,
                '    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIG} TO {APP_ROLE}")',
                '    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIG} TO {APP_ROLE}")\n'
                '    op.execute(f"ALTER ROLE {APP_ROLE} BYPASSRLS")'),),
        min_edits=1,
        test_cmd=(T_PRIV,),
        expect_test="test_the_worker_role_cannot_select_turn_text_globally",
        expect_contains="a worker session read message text out of conversation_turns",
        also_expect=("test_the_worker_role_cannot_enumerate_conversation_turn_owners",),
    ),
    # RETIRED: `secdef-replaced-by-policy`. It deleted a `DROP POLICY IF EXISTS shadow_reconciliation_worker`
    # statement from 0037 — the removal, in a later revision, of a policy an earlier revision had created.
    # The fold eliminated BOTH halves of that pair, so there is no DROP left to delete and, more to the
    # point, no statement anywhere whose removal restores the policy. REPOINTING IT WOULD HAVE BEEN WRONG:
    # every candidate anchor is a line that does something else, and a spec that deletes an unrelated line
    # and watches the privilege test go red proves only that something broke. The PROPERTY is not retired —
    # it is attacked below in the form it could actually return in, which is somebody ADDING the policy.
    Spec(
        id="worker-read-policy-restored",
        prop="THE INCIDENT ITSELF: no migration in this chain ever creates "
             "`ON conversation_turns FOR SELECT USING (app_is_worker())`. RLS cannot narrow a row to a "
             "column, so that policy is SELECT on every column of every student's turn — `text` first "
             "among them — handed over in exchange for a COUNT",
        files_allowed=(M35,),
        # HOW IT WOULD ACTUALLY COME BACK, and it is not a revert. The chain that created this policy was
        # deleted before it was deployed, so there is nothing to revert TO. It comes back as three lines
        # somebody adds beside the GRANT — because the aggregate is hard, a policy is one statement, and
        # every number the reconciliation reports stays correct either way. Correct numbers are why the
        # first version of this incident survived review.
        ops=(Op(M35,
                '    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIG} TO {APP_ROLE}")',
                '    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIG} TO {APP_ROLE}")\n'
                '    op.execute(f"CREATE POLICY shadow_reconciliation_worker ON {TURNS} "\n'
                '               f"FOR SELECT USING (public.app_is_worker())")'),),
        min_edits=1,
        test_cmd=(T_PRIV,),
        expect_test="test_the_worker_role_cannot_select_turn_text_globally",
        expect_contains="a worker session read message text out of conversation_turns",
        # BOTH DIRECTIONS OF THE CLAIM. The behavioural half says the worker really can read the words;
        # the catalog half says the privilege gate NAMES the policy rather than merely noticing that some
        # number moved. A gate that stays green beside a restored row grant is the whole failure.
        also_expect=("test_the_worker_role_cannot_enumerate_conversation_turn_owners",
                     "test_the_hardening_and_privilege_gates_pass_as_shipped"),
    ),
)


# --------------------------------------------------------------------------- the tree


def _tree_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name.endswith(_EXCLUDE_SUFFIXES):
                continue
            out.append(Path(dirpath) / name)
    return out


def fingerprint(root: Path) -> str:
    """The tree's identity. Recorded before the first mutation and re-checked after the last — the only
    way to be sure an isolated run did not reach back into the user's working copy."""
    h = hashlib.sha256()
    for path in _tree_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            blob = path.read_bytes()
        except OSError as exc:                       # a socket, a broken link: name it, do not guess
            raise HarnessError(f"cannot fingerprint {rel}: {exc}") from exc
        h.update(rel.encode())
        h.update(hashlib.sha256(blob).digest())
    return h.hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolate(dest: Path) -> Path:
    """A full copy of the engine minus `.venv`, with `.venv` symlinked back in.

    Copied rather than mutated-in-place because the canonical tree is the user's working copy and this
    runner is not allowed to touch it; symlinked rather than copied because the virtualenv is not what is
    under test and duplicating it thirty-one times is minutes of IO for no evidence.
    """
    engine_copy = dest / "engine"
    excludes: list[str] = []
    for name in sorted(_EXCLUDE_DIRS):
        excludes += ["--exclude", f"/{name}/", "--exclude", f"{name}/"]
    proc = subprocess.run(
        ["rsync", "-a", "--delete", *excludes, f"{ENGINE}/", f"{engine_copy}/"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise HarnessError(f"rsync failed ({proc.returncode}): {proc.stderr[-2000:]}")
    venv = ENGINE / ".venv"
    if not venv.exists():
        raise HarnessError(f"no virtualenv at {venv}")
    (engine_copy / ".venv").symlink_to(venv, target_is_directory=True)
    return engine_copy


# --------------------------------------------------------------------------- the cluster, which is shared

# WHAT `DROP DATABASE` DOES NOT UNDO. `conftest` gives every pytest session its own database, so table
# state never leaks between mutations. Roles are CLUSTER-level: `secdef-owner-login` and
# `worker-bypassrls` both change a role, both survive the database being dropped, and `worker-bypassrls`
# in particular would leave `bruce_app` able to bypass RLS for every run that followed — the mutation
# escaping into the environment, and every later run green because isolation had quietly stopped existing.
#
# These statements are the shape migrations 0033-0035 ship, re-asserted rather than assumed. `bruce_app`
# is the restricted application role (conftest creates it with LOGIN and a password); `bruce_shadow_recon`
# is the definer's owner, which holds BYPASSRLS deliberately and everything else deliberately not. Both
# are guarded by an existence check so this is a no-op on a cluster that has never run the suite.
_ROLE_RESET_SQL = """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bruce_app') THEN
    EXECUTE 'ALTER ROLE bruce_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bruce_shadow_recon') THEN
    EXECUTE 'ALTER ROLE bruce_shadow_recon NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOINHERIT NOREPLICATION BYPASSRLS';
  END IF;
END $$;
"""

_ROLE_RESET_PY = """
import asyncio, os, sys
import asyncpg
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

load_dotenv(os.path.join(os.getcwd(), ".env"))
url = os.environ.get("BRUCE_DATABASE_URL")
if not url:
    print("no BRUCE_DATABASE_URL", file=sys.stderr); raise SystemExit(3)
u = make_url(url)

async def go():
    c = await asyncpg.connect(host=u.host, port=u.port or 5432, user=u.username,
                              password=u.password, database=u.database)
    try:
        await c.execute(SQL)
    finally:
        await c.close()

asyncio.run(go())
"""


def reset_cluster_roles() -> str:
    """Put `bruce_app` and `bruce_shadow_recon` back the way the migrations ship them.

    Returns "ok", "unavailable" (no reachable Postgres — the same condition under which the PG tests
    skip), or an error string. NEVER raises and never silently succeeds: the result is recorded per
    mutation, because a reset that failed after `worker-bypassrls` means every subsequent result in the
    report was produced against a cluster with RLS disabled, and that is a fact the reader needs.
    """
    python = ENGINE / ".venv" / "bin" / "python"
    if not python.exists():
        return f"no interpreter at {python}"
    script = f"SQL = {_ROLE_RESET_SQL!r}\n{_ROLE_RESET_PY}"
    try:
        proc = subprocess.run([str(python), "-c", script], cwd=str(ENGINE), capture_output=True,
                              text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return "ok"
    tail = (proc.stderr or proc.stdout).strip()[-300:]
    # A cluster nobody can reach is the environment in which every PG test skips anyway, and a run of
    # skips is already classified INVALID_PROOF. It is reported as its own value rather than as a failure
    # so the two conditions are not confused.
    if "ConnectionRefused" in tail or "does not exist" in tail or "no BRUCE_DATABASE_URL" in tail:
        return f"unavailable: {tail}"
    return f"failed: {tail}"


# --------------------------------------------------------------------------- applying a mutation


def apply_ops(tree: Path, spec: Spec) -> tuple[int, list[dict]]:
    """Apply every op, ASSERT the declared edit counts, and prove each file actually changed on disk.

    The two checks are not redundant. The count says the anchor still matches the source; the byte
    comparison says the write landed. A mutation run that skips either is a run in which nothing was
    broken, and a green suite over an unbroken tree is the least informative possible result.
    """
    for op in spec.ops:
        if op.file not in spec.files_allowed:
            raise HarnessError(f"{spec.id}: op touches {op.file}, which is not in files_allowed")

    before: dict[str, tuple[str, str]] = {}          # file -> (sha, text)
    total = 0
    per_file_counts: dict[str, int] = {}
    for op in spec.ops:
        path = tree / op.file
        if not path.exists():
            raise HarnessError(f"{spec.id}: {op.file} does not exist in the isolated tree")
        text = path.read_text()
        if op.file not in before:
            before[op.file] = (_sha(path), text)
        if op.regex:
            new, n = re.subn(op.find, op.replace, text)
        else:
            n = text.count(op.find)
            new = text.replace(op.find, op.replace)
        if n < op.min_count:
            # ZERO_EDIT (or under-edit) is raised as a typed condition, not tolerated: the anchor moved.
            raise _ZeroEdit(f"{spec.id}: op on {op.file} matched {n} time(s), declared minimum "
                            f"{op.min_count}. The anchor no longer matches the source — this run proves "
                            f"nothing until the spec is re-read against the current file.")
        path.write_text(new)
        total += n
        per_file_counts[op.file] = per_file_counts.get(op.file, 0) + n

    if total < spec.min_edits:
        raise _ZeroEdit(f"{spec.id}: {total} edit(s) applied, declared minimum {spec.min_edits}")

    changed: list[dict] = []
    for rel, (sha_before, text_before) in sorted(before.items()):
        path = tree / rel
        sha_after = _sha(path)
        if sha_after == sha_before:
            raise _ZeroEdit(f"{spec.id}: {rel} is byte-identical after the mutation — the edit did not "
                            f"land on disk")
        diff = list(difflib.unified_diff(text_before.splitlines(), path.read_text().splitlines(),
                                         fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=1))
        changed.append({"file": rel, "sha256_before": sha_before[:16], "sha256_after": sha_after[:16],
                        "edits": per_file_counts.get(rel, 0), "diff": diff[:40]})
    return total, changed


class _ZeroEdit(RuntimeError):
    """The mutation did not apply as declared. A VERIFICATION FAILURE, never a pass."""


# --------------------------------------------------------------------------- running the tests


def run_focused(tree: Path, spec: Spec, timeout_s: int) -> dict:
    """Run ONLY the discriminating command, in the isolated tree, and read the outcome per test.

    JUnit XML rather than stdout scraping: the question is "did THIS test fail, and with what message",
    and a regex over `-q` output cannot answer it without guessing.
    """
    xml = tree / ".mutation_junit.xml"
    python = tree / ".venv" / "bin" / "python"
    cmd = [str(python), "-m", "pytest", "-p", "no:randomly", "-q", "--tb=short",
           f"--junitxml={xml}", *spec.test_cmd]
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(tree), capture_output=True, text=True, timeout=timeout_s,
                          env={**os.environ})
    elapsed = time.perf_counter() - started
    results = _parse_junit(xml) if xml.exists() else []
    return {"exit_code": proc.returncode, "duration_s": round(elapsed, 1), "results": results,
            "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-2000:]}


def _parse_junit(xml: Path) -> list[dict]:
    try:
        root = ET.parse(xml).getroot()
    except ET.ParseError as exc:
        raise HarnessError(f"unreadable junit xml: {exc}") from exc
    out: list[dict] = []
    for case in root.iter("testcase"):
        rec = {"name": case.get("name", ""), "classname": case.get("classname", ""),
               "status": "passed", "message": "", "text": ""}
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                rec["status"] = kind
                rec["message"] = node.get("message", "") or ""
                rec["text"] = (node.text or "")
                break
        if case.find("skipped") is not None and rec["status"] == "passed":
            rec["status"] = "skipped"
        out.append(rec)
    return out


def _matches(name: str, wanted: str) -> bool:
    """A parametrized test id is `name[param0-param1]`; the spec names the test, not its parameters."""
    return name == wanted or name.startswith(wanted + "[")


def classify(spec: Spec, run: dict) -> tuple[str, str]:
    """The outcome, and the sentence explaining it. Every branch here is a way a mutation can fail to
    prove what it claims, which is the only reason this function is not a boolean."""
    results = run["results"]
    exit_code = run["exit_code"]
    blob = run["stdout_tail"] + run["stderr_tail"]

    if exit_code in (2, 3, 4):
        return "INVALID_PROOF", (f"pytest exited {exit_code} (interrupted / internal / usage error), so "
                                 f"the discriminating tests never ran to a verdict")
    if not results:
        return "INVALID_PROOF", "no test results at all — the command collected nothing"
    ran = [r for r in results if r["status"] != "skipped"]
    if not ran:
        return "INVALID_PROOF", "every discriminating test SKIPPED (Postgres unreachable?)"

    if exit_code == 0:
        return "ESCAPED", (f"the mutation applied and all {len(ran)} discriminating test(s) stayed green. "
                           f"{spec.expect_test} did not notice the property being removed — this is a "
                           f"blocking test gap, and the fix is the test, not the mutation")

    intended = [r for r in results if _matches(r["name"], spec.expect_test)]
    if not intended:
        return "INVALID_PROOF", (f"{spec.expect_test} is not in the results at all — it was renamed, "
                                 f"deselected, or the module failed to collect")
    failing = [r for r in intended if r["status"] in ("failure", "error")]
    if not failing:
        return "ESCAPED", (f"tests failed, but {spec.expect_test} PASSED. Whatever went red is not the "
                           f"property this mutation attacks")

    hit = failing[0]
    detail = f"{hit['message']}\n{hit['text']}"
    if hit["status"] == "error" and not spec.allow_error_phase:
        return "INVALID_PROOF", (f"{spec.expect_test} errored in setup/collection rather than failing an "
                                 f"assertion — that proves the line was needed to START, not that the "
                                 f"property is guarded: {detail.strip()[:400]}")
    for marker in _INFRA_MARKERS:
        if marker in detail or marker in blob:
            return "INVALID_PROOF", (f"the failure carries {marker!r}, which is infrastructure breakage "
                                     f"rather than the property failing: {detail.strip()[:400]}")
    if spec.expect_contains not in detail:
        return "INVALID_PROOF", (f"{spec.expect_test} failed, but not for the declared reason "
                                 f"({spec.expect_contains!r} absent). Observed: {detail.strip()[:500]}")

    missing = []
    for also in spec.also_expect:
        hits = [r for r in results if _matches(r["name"], also)]
        if not hits or all(r["status"] not in ("failure", "error") for r in hits):
            missing.append(also)
    if missing:
        return "INVALID_PROOF", (f"the intended test failed correctly, but the co-declared proof(s) "
                                 f"{missing} did not fail — the spec's claim is only partly true")

    others = sorted({r["name"] for r in results
                     if r["status"] in ("failure", "error") and not _matches(r["name"], spec.expect_test)})
    return "CAUGHT", (f"{spec.expect_test} failed on {spec.expect_contains!r}"
                      + (f"; collateral failures: {others}" if others else ""))


# --------------------------------------------------------------------------- the run


def run_one(spec: Spec, *, dry_run: bool, timeout_s: int) -> dict:
    record: dict = {"id": spec.id, "property": spec.prop, "files_allowed": list(spec.files_allowed),
                    "test_cmd": list(spec.test_cmd), "expected_failing_test": spec.expect_test,
                    "expected_failure_contains": spec.expect_contains,
                    "co_required_failing": list(spec.also_expect),
                    "declared_min_edits": spec.min_edits}
    tmp = Path(tempfile.mkdtemp(prefix=f"mut-{spec.id}-"))
    try:
        tree = isolate(tmp)
        try:
            edits, changed = apply_ops(tree, spec)
        except _ZeroEdit as exc:
            record |= {"outcome": "ZERO_EDIT", "edits_applied": 0, "files_changed": [],
                       "reason": str(exc)}
            return record
        record |= {"edits_applied": edits, "files_changed": changed}
        if dry_run:
            record |= {"outcome": "DRY_RUN", "reason": f"{edits} edit(s) applied; tests not run"}
            return record
        run = run_focused(tree, spec, timeout_s)
        outcome, reason = classify(spec, run)
        record |= {
            "outcome": outcome, "reason": reason,
            "pytest_exit_code": run["exit_code"], "duration_s": run["duration_s"],
            "observed_failing": sorted({r["name"] for r in run["results"]
                                        if r["status"] in ("failure", "error")}),
            "observed_passing": sorted({r["name"] for r in run["results"] if r["status"] == "passed"}),
        }
        return record
    except subprocess.TimeoutExpired:
        record |= {"outcome": "HARNESS_ERROR", "reason": f"the focused command exceeded {timeout_s}s"}
        return record
    except (HarnessError, OSError) as exc:
        record |= {"outcome": "HARNESS_ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        return record
    finally:
        # THE ISOLATED TREE IS DISCARDED, always. Keeping it "just in case" is how a later run picks up a
        # half-mutated copy and reports something nobody can reproduce.
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic mutation proofs for the shadow suite.")
    ap.add_argument("--id", action="append", help="run only these mutation ids")
    ap.add_argument("--dry-run", action="store_true",
                    help="apply each mutation in isolation and report edit counts; run no tests")
    ap.add_argument("--timeout", type=int, default=900, help="per-mutation test timeout, seconds")
    ap.add_argument("--json", help="also write the report here")
    args = ap.parse_args()

    selected = [s for s in SPECS if not args.id or s.id in set(args.id)]
    unknown = sorted(set(args.id or []) - {s.id for s in SPECS})
    if unknown:
        print(json.dumps({"error": f"unknown mutation id(s): {unknown}"}, indent=2))
        return 2

    started = time.time()
    try:
        before = fingerprint(ENGINE)
    except HarnessError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2

    mutations: list[dict] = []
    for i, spec in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {spec.id} ...", file=sys.stderr, flush=True)
        rec = run_one(spec, dry_run=args.dry_run, timeout_s=args.timeout)
        # THE CANONICAL TREE IS RE-CHECKED AFTER EVERY MUTATION, not only at the end: an isolated run
        # that reached back into the working copy must be attributed to the mutation that did it.
        rec["canonical_tree_unchanged"] = (fingerprint(ENGINE) == before)
        # AND THE CLUSTER, which the fingerprint above cannot see. Two mutations in this inventory change
        # a role, and a role outlives the database it was used from.
        rec["cluster_roles_reset"] = "skipped (dry run)" if args.dry_run else reset_cluster_roles()
        print(f"    -> {rec['outcome']}", file=sys.stderr, flush=True)
        mutations.append(rec)

    after = fingerprint(ENGINE)
    counts: dict[str, int] = {}
    for rec in mutations:
        counts[rec["outcome"]] = counts.get(rec["outcome"], 0) + 1
    for name in ("CAUGHT", "ZERO_EDIT", "ESCAPED", "INVALID_PROOF", "HARNESS_ERROR"):
        counts.setdefault(name, 0)

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "engine": str(ENGINE),
        "mode": "dry-run" if args.dry_run else "full",
        "canonical_fingerprint_before": before,
        "canonical_fingerprint_after": after,
        "canonical_tree_unchanged": before == after,
        "total_mutations": len(mutations),
        "totals": counts,
        # The bar, stated in the report rather than left to a reader to compute: anything but 100% CAUGHT
        # is a finding, and ZERO_EDIT in particular must never be read as "nothing to see here".
        "required": {"CAUGHT": len(mutations), "ZERO_EDIT": 0, "ESCAPED": 0,
                     "INVALID_PROOF": 0, "HARNESS_ERROR": 0},
        "cluster_roles_reset_final": "skipped (dry run)" if args.dry_run else reset_cluster_roles(),
        "requirement_met": (not args.dry_run
                            and counts["CAUGHT"] == len(mutations)
                            and before == after
                            and all(r.get("canonical_tree_unchanged") for r in mutations)
                            # A run whose cluster reset failed cannot claim its later results were
                            # produced under isolation, so it does not get to claim the bar was met.
                            and all(r.get("cluster_roles_reset") == "ok" for r in mutations)),
        "wall_clock_s": round(time.time() - started, 1),
        "mutations": mutations,
    }
    text = json.dumps(report, indent=2)
    if args.json:
        Path(args.json).write_text(text)
    print(text)
    return 0 if (args.dry_run or report["requirement_met"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
