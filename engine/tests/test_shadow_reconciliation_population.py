"""THE RECONCILIATION IS DECIDED IN THE DATABASE, OVER A POPULATION NOTHING OUT HERE CAN SEE OR NAME.

THE DEFECT, which is the same one four times, one layer further out each time.

  ROUND 1   `eligible = sum(counts.values())` — the denominator was the sum of its own numerators.
  ROUND 2   `intake.reconciled` — a self-consistency check on a counter, published as if it were a claim
            that every turn had reached the counter.
  ROUND 3   THE POPULATION SELECTION. `worker_api.process` built `shadow_users` out of the jobs
            `claim_and_observe` returned, because `conversation_turns` is tenant-isolated and those user
            ids were the only ones a worker could obtain. A user whose intake call was MISSING has no
            job, so was never in the list, so was never reconciled — and an idle queue produced
            `reconcile_users=()`, which made `reconciled: True` the answer EVERY wake emitted.
  ROUND 4   THE FIX FOR ROUND 3. Migration 0037's aggregate returned one row PER OWNER, so the worker
            looped those owners and opened a session per tenant to finish the join. The population was
            honest and the ENUMERATION was worse than before: the drain now held every owner in the
            window, including the owners with no jobs, where previously it learned an owner id only from
            a job it had claimed.

Migration 0038 moves the join inside the SECURITY DEFINER function. What crosses the boundary is one row
of counts and one verdict. There is no population out here to collapse, no user id to loop, and nothing
for this process to re-derive.

WHAT THIS FILE PROVES, through the REAL `worker_api.process`, because the defect WAS the production
caller, on real Postgres throughout:

  * the three states, kept apart — and the invisible one produced by actually removing the grant
  * a student whose intake never ran is found, with no shadow row of any kind and an empty queue
  * the emission carries no message text and no user id
  * THE VERDICT IS THE DATABASE'S. A `failed` whose published counts would independently re-derive as
    `clean` must still serialize `failed`; a database verdict of `unknown` beside clean-looking counts
    must stay `unknown`. Nothing between the function and the response body is allowed an opinion.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_store, semantic_shadow, worker_api
from bruce_engine.db import user_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

CHANNEL = "imessage"
IDENTITY = "+15550199"
# The policy migration 0037 REMOVED. Named here so the tests below can put it back and prove the
# reconciliation refuses to run over it — see test_shadow_population_privilege.py for why it went.
WORKER_POLICY = "shadow_reconciliation_worker"
RECON_FN = "shadow_reconciliation(timestamptz, timestamptz)"
# The per-owner function migration 0038 DROPPED. Same treatment: putting it back must stop the check.
RETIRED_FN = "shadow_turn_population(timestamptz, timestamptz)"
APP_ROLE = "bruce_app"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(
        db, "create_async_engine",
        lambda url, **kw: (kw.pop("poolclass", None),
                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    # AUTHORITY STAYS OFF. This file is about what the sample MEASURES; the moment the executive can
    # decide, none of these rows are evidence about anything.
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


@pytest.fixture()
def quiet_worker(monkeypatch):
    """Silence the intake and mission drains so `/process` is about the shadow block alone.

    The shadow path itself is NOT stubbed — it is the thing under test, and the defect was in this
    function rather than in anything it calls.
    """
    async def _no_intake(*a, **kw):
        return False

    monkeypatch.setattr(worker_api.worker, "process_one", _no_intake)
    monkeypatch.setattr(worker_api, "background_runner_enabled", lambda: False)


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


def _run(c):
    return asyncio.run(c)


async def _turn(uid: UUID, *, pmid: str, text: str, age_s: int = 300) -> UUID:
    """A trusted inbound turn on the CANONICAL ledger, backdated into the reconciliation window.

    The backdating is not a trick, it is the contract. The upper watermark lags real time on purpose:
    `persist_user_turn` commits the turn and `intake` writes the job moments later in another
    transaction, so a turn caught between the two is legitimately unobserved for an instant and counting
    it would make the check cry wolf on every single wake.
    """
    turn_id = await conversation_store.persist_user_turn(
        uid, channel=CHANNEL, channel_identity=IDENTITY, provider_message_id=pmid, text=text)
    async with user_session(uid) as s:
        await s.execute(
            sa_text("UPDATE conversation_turns SET created_at = now() - make_interval(secs => :age) "
                    "WHERE id = :id"), {"age": age_s, "id": str(turn_id)})
    return turn_id


async def _intake(uid: UUID, *, pmid: str, turn_id: UUID | None,
                  disposition: str = semantic_shadow.ELIGIBLE) -> bool:
    return await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=pmid,
                                        decision=_Decision(), conversation_turn_id=turn_id,
                                        disposition=disposition,
                                        tally=semantic_shadow.EnqueueLedger())


def _owner_conn_params():
    o = make_url(os.environ["BRUCE_DATABASE_URL"])
    return {"host": o.host, "port": o.port or 5432, "user": o.username,
            "password": o.password, "database": o.database}


def _as_owner(*statements: str):
    async def _go():
        conn = await asyncpg.connect(**_owner_conn_params())
        try:
            for s in statements:
                await conn.execute(s)
        finally:
            await conn.close()

    return _run(_go())


def _turns_block() -> dict:
    return _run(worker_api.process())["shadow_health"]["turns"]


# --- state 1: zero canonical turns ------------------------------------------------------------------

def test_an_empty_ledger_is_clean_with_honest_zeros(clean_db, quiet_worker):
    """No traffic in the window: `clean`, and the counts are honest zeros.

    This state is only allowed to be `clean` BECAUSE the caller established it could reach the function
    at all. That ordering is the entire difference between this and the check it replaces, which reported
    exactly these numbers on every wake regardless of what the ledger held.
    """
    out = _run(worker_api.process())
    turns = out["shadow_health"]["turns"]

    assert turns["reconciliation_status"] == semantic_shadow.RECON_CLEAN
    assert turns["canonical_trusted_turns"] == 0
    assert turns["unobserved_turns"] == 0 and turns["trusted_turns_without_intake"] == 0
    assert turns["window_start"] and turns["window_end"] and turns["watermark"]
    assert out["shadow_errors"] == 0


# --- state 2: turns exist and intake did not happen -------------------------------------------------

def test_a_user_with_turns_and_no_shadow_job_at_all_fails_the_check(clean_db, quiet_worker):
    """THE BUG, reproduced exactly, through the production caller.

    This user's intake call never happened: they have trusted turns on the canonical ledger and ZERO
    shadow rows of any kind. They therefore appear in no claimed job, no completed job, no disposition
    and no shadow table — every source the old population was built from. And the queue is EMPTY, which
    is the case that made the old check emit `reconciled: True` unconditionally.

    Nothing about this test tells the worker who to look at, and the worker could not act on it if it
    did: no user id reaches this process at all any more. That is the assertion.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-1", text="can u email alvarez a thank you note"))
    _run(_turn(uid, pmid="pm-2", text="make it heartfelt"))

    out = _run(worker_api.process())
    turns = out["shadow_health"]["turns"]

    assert out["observed"] == 0, "the queue was supposed to be empty — this is the case that always passed"
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "a user with two trusted turns and no shadow row at all reconciled cleanly. That is the exact "
        "state the whole continuation population was in, undetected, for three rounds")
    assert turns["canonical_trusted_turns"] == 2
    assert turns["trusted_turns_without_intake"] == 2, (
        "the gap was not found by the anti-join that is supposed to find it")
    assert turns["trusted_turns_with_intake"] == 0 and turns["shadow_jobs"] == 0
    assert turns["unobserved_turns"] == 2


def test_a_user_with_zero_shadow_jobs_is_still_counted_beside_one_that_has_them(clean_db, quiet_worker):
    """The mixed population, which is what a real wake looks like.

    One owner is fully observed; the other's intake never ran. A population taken from the jobs would
    contain the first and not the second, report no gap, and be exactly wrong. The check has to find the
    second one while the first is present and clean — and it has to do it without either owner's id
    reaching this process.
    """
    good, missing = uuid4(), uuid4()
    _run(users.ensure(good, auth_provider="test"))
    _run(users.ensure(missing, auth_provider="test"))

    observed_turn = _run(_turn(good, pmid="pm-good", text="hey bruce"))
    _run(_intake(good, pmid="pm-good", turn_id=observed_turn))
    _run(_turn(missing, pmid="pm-missing", text="can you check my assignments"))

    turns = _turns_block()

    assert turns["canonical_trusted_turns"] == 2, "an affected owner's turns were left out of the count"
    # `shadow_jobs` counts the rows reached THROUGH the canonical turn id, so it is the half of the
    # population that is accounted for. Its queue-state breakdown is deliberately not asserted here: the
    # same wake drains the queue, so which state the observed job has landed in is this test's business
    # only insofar as it exists at all.
    assert turns["eligible_turns"] == 1 and turns["shadow_jobs"] == 1
    assert turns["trusted_turns_with_intake"] == 1
    assert turns["trusted_turns_without_intake"] == 1 and turns["unobserved_turns"] == 1
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED

    blob = json.dumps(turns, default=str)
    for uid in (good, missing):
        assert str(uid) not in blob, "an owner id reached the emission; the block is counts, not a roster"


def test_an_explicitly_excluded_turn_reconciles_and_a_dropped_one_does_not(clean_db, quiet_worker):
    """The distinction the exclusion status exists for, over the canonical join.

    A turn deliberately not observed leaves a row saying so and is ACCOUNTED FOR. A turn nobody wrote
    anything about is a GAP. If those two looked the same the check could not do the only thing it does.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    skipped = _run(_turn(uid, pmid="pm-slash", text="/status"))
    _run(_intake(uid, pmid="pm-slash", turn_id=skipped,
                 disposition=semantic_shadow.EXCLUDED_PLATFORM_COMMAND))

    turns = _turns_block()
    assert turns["explicitly_excluded_turns"] == 1 and turns["unobserved_turns"] == 0
    assert turns["reconciliation_status"] == semantic_shadow.RECON_CLEAN

    _run(_turn(uid, pmid="pm-dropped", text="and this one nobody offered"))
    turns = _turns_block()
    assert turns["unobserved_turns"] == 1 and turns["explicitly_excluded_turns"] == 1
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "a turn with no row of any kind was folded in with the ones deliberately excluded")


def test_a_shadow_row_that_lost_its_canonical_reference_reads_as_unobserved(clean_db, quiet_worker):
    """The degradation is loud, not silent.

    `conversation_turn_id` is nullable — rows written before migration 0036 have none. A row without it
    joins to no turn, so its turn counts as UNOBSERVED. That is the correct direction: if the runtime
    ever stopped passing the canonical id, the check goes red rather than quietly falling back to the
    weaker (channel, provider_message_id) match that this column replaced.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-legacy", text="hey"))
    assert _run(_intake(uid, pmid="pm-legacy", turn_id=None)) is True, "the row was not written at all"

    turns = _turns_block()
    assert turns["trusted_turns_without_intake"] == 1 and turns["unobserved_turns"] == 1
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "a shadow row with no canonical reference was matched to a turn anyway — which means something "
        "is still re-deriving the provider triple")


# --- state 3: the ledger cannot be seen -------------------------------------------------------------

def test_an_invisible_ledger_reports_unknown_and_never_clean(clean_db, quiet_worker):
    """THE RLS TRAP, produced by actually removing the grant.

    `conversation_turns` is tenant-isolated, so a worker session that has lost its aggregate path sees
    zero rows and NOTHING RAISES. Every number the check computes is then a real, correct, completely
    meaningless zero, and the report is `clean` — the third state collapsing into the first, at the exact
    moment the check has gone blind.

    The grant removed here is the one migration 0038 actually gives: EXECUTE on `shadow_reconciliation`.
    It is revoked and restored, and while it is gone the ledger holds real traffic that is really
    unobserved, so a wrong answer has something to be wrong about.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-invisible", text="this turn is never observed"))

    _as_owner(f"REVOKE EXECUTE ON FUNCTION public.{RECON_FN} FROM {APP_ROLE}")
    try:
        turns = _turns_block()
    finally:
        _as_owner(f"GRANT EXECUTE ON FUNCTION public.{RECON_FN} TO {APP_ROLE}")

    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
        "the worker could not reach the reconciliation and reported a clean, fully accounted sample. "
        "'Empty' and 'invisible' being the same emission is the original sin of this whole module")
    assert turns["reconciliation_status"] != semantic_shadow.RECON_CLEAN
    for absent in semantic_shadow.DEPENDENT_COUNTS:
        assert turns[absent] is None, (
            f"{absent} was reported as {turns[absent]!r} by a check that could not read the ledger — a "
            "fabricated zero says the sample is complete precisely when nobody can tell")
    assert turns["unreadable_reason"] == semantic_shadow.NO_POPULATION_FUNCTION

    # and the restored grant makes the SAME ledger legible again, so the failure above was the privilege
    # and not an empty database.
    after = _turns_block()
    assert after["reconciliation_status"] == semantic_shadow.RECON_FAILED
    assert after["canonical_trusted_turns"] == 1 and after["unobserved_turns"] == 1


def test_putting_the_broad_worker_read_policy_back_stops_the_reconciliation(clean_db, quiet_worker):
    """THE FIRST HALF OF THE INCIDENT, RESTORED, through the production caller.

    Migration 0036 bought this reconciliation's cross-tenant count with
    `FOR SELECT USING (app_is_worker())` on `conversation_turns`. RLS is row-level: that hands a worker
    session every COLUMN of every student's turn, `text` included, to compute a number.

    If it ever comes back — a reverted migration, a hand-applied hotfix, a copied policy — the numbers
    would still be right and nothing would fail, which is precisely how it would stay. So the read path
    REFUSES over it: the worker reports `unknown` and names the reason, and an operator gets an alarm
    instead of a clean dashboard sitting on top of a reopened incident.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-regressed", text="nobody should be reading this in bulk"))

    _as_owner(f"CREATE POLICY {WORKER_POLICY} ON conversation_turns "
              "FOR SELECT USING (app_is_worker())")
    try:
        turns = _turns_block()
    finally:
        _as_owner(f"DROP POLICY IF EXISTS {WORKER_POLICY} ON conversation_turns")

    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
        "the global row grant was restored and the reconciliation carried on reporting over it — the "
        "one arrangement in which reopening the incident costs nothing and is noticed by nobody")
    assert turns["unreadable_reason"] == semantic_shadow.LEDGER_ROW_GRANT_PRESENT
    for absent in semantic_shadow.DEPENDENT_COUNTS:
        assert turns[absent] is None

    after = _turns_block()
    assert after["reconciliation_status"] == semantic_shadow.RECON_FAILED
    assert after["canonical_trusted_turns"] == 1


def test_putting_the_per_owner_population_function_back_stops_the_reconciliation(clean_db, quiet_worker):
    """THE SECOND HALF OF THE INCIDENT, RESTORED, through the production caller.

    This is how it would actually come back: not by reverting a migration, but by someone recreating the
    per-user helper because a dashboard wanted a breakdown. The aggregate beside it keeps working, every
    number stays correct, and the worker quietly regains a list of every owner in the window — which is
    the one capability that made a per-tenant read of the ledger reachable at all.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-enumerable", text="nobody should be able to list who said this"))

    _as_owner(
        "CREATE FUNCTION public.shadow_turn_population(p_lo timestamptz, p_hi timestamptz) "
        "RETURNS TABLE (user_id uuid, trusted_turn_count bigint) LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $x$ "
        "  SELECT ct.user_id, pg_catalog.count(*)::bigint FROM public.conversation_turns ct "
        "   WHERE ct.role = 'user' AND ct.created_at >= p_lo AND ct.created_at < p_hi "
        "   GROUP BY ct.user_id $x$",
        f"GRANT EXECUTE ON FUNCTION public.{RETIRED_FN} TO {APP_ROLE}")
    try:
        turns = _turns_block()
    finally:
        _as_owner(f"DROP FUNCTION IF EXISTS public.{RETIRED_FN}")

    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
        "an owner-enumeration path was restored beside a perfectly good aggregate and the reconciliation "
        "kept reporting clean numbers over it. Correct numbers are why the first version of this "
        "incident survived")
    assert turns["unreadable_reason"] == semantic_shadow.OWNER_ENUMERATION_PATH_PRESENT

    after = _turns_block()
    assert after["reconciliation_status"] == semantic_shadow.RECON_FAILED


def test_a_function_that_answers_with_no_row_is_unknown_rather_than_clean(clean_db, quiet_worker):
    """THE ZERO-ROW TRAP, which is specific to an aggregate.

    An aggregate over an empty window returns ONE row of zeros, so `no rows` and `no traffic` are
    different facts — but they arrive through the same call and the second one is legitimately `clean`.
    A function that answers with nothing at all has not reconciled anything, and reading its silence as
    a quiet hour would rebuild the exact confusion the three states exist to prevent.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-silent", text="this turn is never observed either"))

    original = _run(_function_definition())
    _as_owner(
        "CREATE OR REPLACE FUNCTION public.shadow_reconciliation("
        "  p_window_start timestamptz, p_window_end timestamptz) "
        "RETURNS TABLE (window_start timestamptz, window_end timestamptz, watermark timestamptz, "
        "  canonical_trusted_turns bigint, trusted_turns_with_intake bigint, "
        "  trusted_turns_without_intake bigint, explicitly_excluded_turns bigint, eligible_turns bigint, "
        "  shadow_jobs bigint, pending_jobs bigint, processing_jobs bigint, retryable_jobs bigint, "
        "  terminal_jobs bigint, unobserved_turns bigint, reconciliation_status text) "
        "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public "
        "AS $silent$ SELECT p_window_start, p_window_end, pg_catalog.now(), "
        "  0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, "
        "  0::bigint, 0::bigint, 0::bigint, 'clean'::text WHERE false $silent$")
    try:
        turns = _turns_block()
    finally:
        _as_owner("DROP FUNCTION IF EXISTS public.shadow_reconciliation(timestamptz, timestamptz)",
                  original,
                  "ALTER FUNCTION public.shadow_reconciliation(timestamptz, timestamptz) "
                  "OWNER TO bruce_shadow_recon",
                  "REVOKE ALL ON FUNCTION public.shadow_reconciliation(timestamptz, timestamptz) "
                  "FROM PUBLIC",
                  f"GRANT EXECUTE ON FUNCTION public.{RECON_FN} TO {APP_ROLE}")

    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
        "a reconciliation that returned no row at all was published as a clean, empty window")
    assert turns["unreadable_reason"] == semantic_shadow.NO_RECONCILIATION_ROW
    for absent in semantic_shadow.DEPENDENT_COUNTS:
        assert turns[absent] is None

    after = _turns_block()
    assert after["reconciliation_status"] == semantic_shadow.RECON_FAILED, "the restore failed"


async def _function_definition() -> str:
    conn = await asyncpg.connect(**_owner_conn_params())
    try:
        return str(await conn.fetchval(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'shadow_reconciliation'"))
    finally:
        await conn.close()


def test_a_ledger_query_that_raises_is_unknown_rather_than_clean(clean_db, quiet_worker, monkeypatch):
    """The other way to be blind. A raising reconciliation must not degrade into zeros either — that is
    the same confusion arriving through a different door."""
    async def _boom(self):
        raise RuntimeError("the ledger is unreachable")

    monkeypatch.setattr(semantic_shadow.PostgresShadowStore, "reconcile_window", _boom)
    turns = _turns_block()

    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN
    assert turns["canonical_trusted_turns"] is None and turns["unobserved_turns"] is None
    assert turns["unreadable_reason"] == "RuntimeError"


def test_the_offline_store_reaches_the_unknown_state_too():
    """The three states must be exercisable without Postgres, or the third one is only ever proved where
    a database is available — which is exactly the environment where nobody looks."""
    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        uid = uuid4()
        turn = store.add_turn(uid, channel=CHANNEL, provider_message_id="pm", text="hey")
        assert turn is not None
        store.population_visible = False
        return await semantic_shadow.reconcile_population(store)

    turns = asyncio.run(_go())
    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN
    assert turns["canonical_trusted_turns"] is None and turns["unobserved_turns"] is None


# --- THE VERDICT IS THE DATABASE'S ---------------------------------------------------------------------
#
# The previous test for this was invalid, and invalid in the most comfortable way: its fixture was a
# population where re-deriving the status from the published counts produced the SAME answer. A report
# site that recomputed — or overrode — the verdict would have passed it. The two fixtures below are built
# so that a re-derivation gives the OPPOSITE answer, in both directions.


def _would_look_clean_locally(turns: dict) -> bool:
    """THE RE-DERIVATION THIS MODULE KEEPS PRODUCING, written out once so a fixture can be checked for
    actually discriminating. It is the equation every previous round used: the canonical total equals the
    accounted total, therefore nothing is missing. It is not used to decide anything."""
    return (turns["canonical_trusted_turns"]
            == turns["eligible_turns"] + turns["explicitly_excluded_turns"]
            and turns["trusted_turns_without_intake"] == 0)


def test_a_database_failure_survives_counts_that_would_re_derive_as_clean(clean_db, quiet_worker):
    """THE DISCRIMINATING FIXTURE. The database says FAILED; the published counts say CLEAN.

    Every trusted turn here HAS an intake disposition, so the first anti-join finds nothing and the
    equation `turns == eligible + excluded` balances exactly. But one of those rows sits in a status no
    worker will ever claim and no terminal state accounts for — a half-applied migration, a hand-edited
    row, the class `_tally.unaccounted` exists to name. Its turn will never be observed, the second
    anti-join finds it inside the definer, and the verdict is FAILED.

    Two totals that happen to match is not a reconciliation. So if anything between the function and the
    response body re-derived the status from the numbers in this block, it would publish `clean` — and
    it would look completely reasonable doing so.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    for pmid, body in (("pm-one", "can you email mrs alvarez"), ("pm-two", "and add it to my calendar")):
        turn = _run(_turn(uid, pmid=pmid, text=body))
        assert _run(_intake(uid, pmid=pmid, turn_id=turn)) is True
    _as_owner("UPDATE semantic_shadow_jobs SET status = 'quarantined' "
              "WHERE id = (SELECT id FROM semantic_shadow_jobs ORDER BY created_at LIMIT 1)")

    turns = _turns_block()

    assert _would_look_clean_locally(turns), (
        f"the fixture no longer discriminates: {turns!r} does not re-derive as clean, so a report site "
        "that recomputed the verdict would pass this test for the wrong reason")
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "the database found an eligible intake with no shadow job in any queue state and said FAILED, "
        "and the response published something else. The verdict is decided in the same statement as the "
        "counts, from anti-joins nothing out here can see; re-deriving it from the summary is strictly "
        "worse information wearing the authority of a second opinion")
    assert turns["unobserved_turns"] == 1


def test_a_database_unknown_survives_counts_that_would_re_derive_as_clean(clean_db, quiet_worker,
                                                                          monkeypatch):
    """THE INVERSE, and it is the direction a well-meaning fix-up would take.

    The database hands back a status this build does not recognise as a verdict, beside counts that are
    completely, plausibly clean. The temptation is to fill in the obvious answer. The response must stay
    `unknown` with NULL counts: a verdict the publisher invented is exactly the fabricated zero the third
    state exists to prevent, and it would be indistinguishable from a real clean hour forever after.
    """
    clean_counts = {
        "canonical_trusted_turns": 4, "trusted_turns_with_intake": 4,
        "trusted_turns_without_intake": 0, "explicitly_excluded_turns": 1, "eligible_turns": 3,
        "shadow_jobs": 4, "pending_jobs": 1, "processing_jobs": 0, "retryable_jobs": 0,
        "terminal_jobs": 2, "unobserved_turns": 0,
    }

    async def _unknown(self):
        return semantic_shadow.LedgerReconciliation(
            visible=True, counts=dict(clean_counts),
            status=semantic_shadow.RECON_UNKNOWN,
            window_start=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1),
            window_end=datetime.datetime.now(datetime.timezone.utc),
            watermark=datetime.datetime.now(datetime.timezone.utc))

    monkeypatch.setattr(semantic_shadow.PostgresShadowStore, "reconcile_window", _unknown)
    turns = _turns_block()

    assert _would_look_clean_locally({**clean_counts}), "the fixture's counts do not look clean"
    assert turns["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
        "a database verdict of `unknown` was upgraded to a status the publisher worked out for itself")
    assert turns["reconciliation_status"] != semantic_shadow.RECON_CLEAN
    for absent in semantic_shadow.DEPENDENT_COUNTS:
        assert turns[absent] is None, (
            f"{absent} was published as {turns[absent]!r} beside a verdict this build could not read")


def test_the_worker_report_site_publishes_the_verdict_it_was_given(clean_db, quiet_worker, monkeypatch):
    """One authority, and the report site is not it.

    Whatever `reconciliation_verdict` returns for a database answer is what `/process` publishes,
    key for key. If the worker (or `reconcile_population`, or `health`) ever recomputed the status or any
    count from the same numbers, the two would agree today and drift the first time one of them was
    edited — and the drift would be invisible, because both would look reasonable.
    """
    captured: dict = {}
    real = semantic_shadow.reconciliation_verdict

    def _spy(**kw):
        out = real(**kw)
        captured.update(out)
        return out

    monkeypatch.setattr(semantic_shadow, "reconciliation_verdict", _spy)

    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-unobserved", text="no shadow row for this one"))

    turns = _turns_block()
    assert captured, "the worker reached a reconciliation status without asking the one function that publishes it"
    for key, value in captured.items():
        assert turns[key] == value, (
            f"{key} was published as {turns[key]!r} but the verdict said {value!r} — something between "
            "the decision and the emission is recomputing it")


# --- the window ---------------------------------------------------------------------------------------

def test_the_window_excludes_turns_below_it_and_the_watermark_excludes_the_ones_above(clean_db,
                                                                                      quiet_worker,
                                                                                      monkeypatch):
    """The bounds are real bounds, and the lag protects the check from its own timing.

    A turn older than the window is out of scope — it was reconciled on an earlier wake and re-checking
    it forever would turn one historical gap into a permanent alarm. A turn NEWER than the watermark is
    also out: `persist_user_turn` commits and `intake` writes moments later, so counting a turn that is
    seconds old would fail on every wake, and a check that always fails is a check nobody reads.
    """
    monkeypatch.setattr(semantic_shadow, "RECONCILE_WINDOW_S", 600.0)
    monkeypatch.setattr(semantic_shadow, "RECONCILE_LAG_S", 60.0)
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_turn(uid, pmid="pm-ancient", text="a turn from last week", age_s=7 * 24 * 3600))
    _run(_turn(uid, pmid="pm-just-now", text="a turn from a second ago", age_s=0))

    turns = _turns_block()
    assert turns["canonical_trusted_turns"] == 0, (
        "a turn outside the window was counted; the window's bounds are not being applied")
    assert turns["reconciliation_status"] == semantic_shadow.RECON_CLEAN

    _run(_turn(uid, pmid="pm-inside", text="and one inside the window", age_s=300))
    turns = _turns_block()
    assert turns["canonical_trusted_turns"] == 1 and turns["unobserved_turns"] == 1, (
        "the window excluded a turn that is squarely inside it")
    assert turns["window_start"] and turns["window_end"], (
        "the window's endpoints are not published, so a reader cannot tell a quiet hour from a window "
        "that was computed wrong")


def test_the_watermark_says_when_the_answer_was_computed(clean_db, quiet_worker):
    """The third timestamp, and it is not decoration. `window_end` is the lagged ceiling the caller asked
    about; `watermark` is when the database actually answered. The distance between them is the lag in
    force, and without it a stale answer and a quiet hour are the same block."""
    turns = _turns_block()
    window_end = datetime.datetime.fromisoformat(turns["window_end"])
    watermark = datetime.datetime.fromisoformat(turns["watermark"])
    assert watermark > window_end, (
        "the watermark is not after the window's ceiling, so the window is not lagged at all — a turn "
        "committed between `persist_user_turn` and `intake` would be counted as a gap on every wake")
    assert (watermark - window_end).total_seconds() >= semantic_shadow.RECONCILE_LAG_S - 1


# --- privacy ------------------------------------------------------------------------------------------

def test_the_reconciliation_block_carries_no_message_text_and_no_identities(clean_db, quiet_worker):
    """What the block emits is counts, three timestamps and one word — and it travels into a log line and
    into a Cloud Tasks response body, both of which go further than the row does.

    The identity half of this is no longer a matter of restraint at the emission site: no user id reaches
    this process at all. That is what the assertion is really checking.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    secret = "alvarez@school.edu about the retake on tuesday"
    _run(_turn(uid, pmid="pm-secret", text=f"can you email {secret}"))

    out = _run(worker_api.process())
    blob = json.dumps(out["shadow_health"], default=str).lower()
    for fragment in ("alvarez", "retake", "tuesday", "@school.edu"):
        assert fragment not in blob, (
            f"{fragment!r} reached the health emission — the reconciliation reads the ledger the "
            "student's words live in, so the one rule it cannot break is carrying any of them out")
    assert str(uid) not in blob, "a user id reached the emission; the block is counts, not a roster"
