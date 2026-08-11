"""MIGRATION 0033 IS TESTED AS A MIGRATION — its DDL, run by Alembic, inspected in the database.

WHY THIS FILE HAS TO EXIST. `0001_initial_schema` builds every table with `Base.metadata.create_all()`.
`semantic_shadow_jobs` is an ORM class, so on every database anyone has ever built, the table already
exists by the time 0033 runs — and 0033 opens with `if TABLE not in present:`. Its `create_table` has
therefore never executed anywhere. The suite was green the whole time because `create_all` produced a
table that happened to look right; the DDL that a real migration would run against a real deployed
database was under NO test at all. A constraint that is only ever created by `create_all` is a constraint
no migration has been shown to create — and staging, which is at 0032, is exactly the database where
`create_table` WILL run.

SO THE DATABASE IS PUT INTO THE STATE 0033 IS WRITTEN FOR. Upgrade to 0032, DROP the table `create_all`
left behind — on a database built by migrations alone it would not be there, because 0033 is the
migration that introduces it — and then upgrade one step. What runs is `op.create_table`, and everything
below inspects the RESULT in the catalog rather than reading the migration file.

Then the chain is taken to head, so the rest of the intended final schema is asserted against a table a
migration built rather than one the ORM built: the retry bound the database will not let reach zero, the
cascade to the canonical turn, the reconciliation window index — and the ABSENCE of any worker-admitting
read policy on conversation_turns, which is the property here that is about a privilege NOT existing.

Everything here reads pg_catalog / information_schema. Nothing reads source text.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ENGINE = Path(__file__).resolve().parents[1]
load_dotenv(ENGINE / ".env")

# Session-scoped name, for the reason conftest spells out: a constant would let two concurrent pytest
# runs drop each other's database mid-transaction.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER") or f"p{os.getpid()}"
MIG_DB = f"bruce_mig0033_{_WORKER}"

TABLE = "semantic_shadow_jobs"
TURNS = "conversation_turns"

BEFORE = "0032_known_people"
UNDER_TEST = "0033_semantic_shadow_jobs"


def _owner_url():
    url = os.environ.get("BRUCE_DATABASE_URL")
    if not url:
        pytest.skip("BRUCE_DATABASE_URL (owner) not configured")
    return make_url(url)


async def _connect(database: str):
    o = _owner_url()
    return await asyncpg.connect(host=o.host, port=o.port or 5432, user=o.username,
                                 password=o.password, database=database)


def _alembic(*argv: str) -> subprocess.CompletedProcess:
    o = _owner_url()
    dsn = f"postgresql+asyncpg://{o.username}:{o.password}@{o.host}:{o.port or 5432}/{MIG_DB}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), *argv],
        cwd=str(ENGINE), env={**os.environ, "BRUCE_DATABASE_URL": dsn},
        capture_output=True, text=True)


@pytest.fixture(scope="module")
def at_0033():
    """A database at 0032 with 0033's table ABSENT, then 0033 applied. Yields nothing but the name.

    The DROP is the whole setup. It is not fabricating a state: it is REMOVING the artefact of 0001's
    `create_all()`, which is the only reason the table is present at 0032 and the exact reason 0033's
    `create_table` has never run. What 0033 is then asked to do is what it would do on a database built
    the way its own docstring assumes.
    """
    _owner_url()
    try:
        asyncio.run(_ping())
    except Exception:
        pytest.skip("Postgres not reachable")

    async def _setup():
        admin = await _connect("postgres")
        try:
            await admin.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bruce_app') "
                "THEN CREATE ROLE bruce_app LOGIN PASSWORD 'bruce_dev_pw'; END IF; END $$;")
            await admin.execute(f'DROP DATABASE IF EXISTS "{MIG_DB}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{MIG_DB}"')
        finally:
            await admin.close()

    asyncio.run(_setup())

    up = _alembic("upgrade", BEFORE)
    assert up.returncode == 0, "upgrade to 0032 failed:\n" + up.stderr[-2000:]

    async def _drop():
        conn = await _connect(MIG_DB)
        try:
            present = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=$1)",
                TABLE)
            # If this is ever False, 0001 has stopped building future tables and the guard below is the
            # thing that tells you — silently skipping the drop would make the test weaker without saying
            # so, which is the failure mode this whole file is about.
            assert present, (
                "semantic_shadow_jobs was NOT present at 0032. That is the state 0033 assumes, so the "
                "drop below is now a no-op — check that 0001_initial_schema still runs create_all(), and "
                "delete this assertion rather than letting it pass by accident")
            await conn.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        finally:
            await conn.close()

    asyncio.run(_drop())

    step = _alembic("upgrade", UNDER_TEST)
    assert step.returncode == 0, "0033 failed to apply as a migration:\n" + step.stderr[-2000:]

    yield MIG_DB

    async def _teardown():
        admin = await _connect("postgres")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{MIG_DB}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(_teardown())


async def _ping():
    c = await _connect("postgres")
    await c.close()


def _fetch(sql, *args):
    async def _go():
        conn = await _connect(MIG_DB)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(_go())


def _unique_constraints(table: str) -> dict[str, list[str]]:
    """{constraint name -> ordered column list}, from the catalog."""
    rows = _fetch(
        """
        SELECT c.conname, a.attname, k.ord
          FROM pg_constraint c
          JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conrelid = $1::regclass AND c.contype = 'u'
         ORDER BY c.conname, k.ord
        """, table)
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["conname"], []).append(r["attname"])
    return out


def _indexes(table: str) -> set[str]:
    return {r["indexname"] for r in _fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=$1", table)}


def _policies(table: str) -> dict[str, tuple[str, str | None, str | None]]:
    return {r["policyname"]: (r["cmd"], r["qual"], r["with_check"]) for r in _fetch(
        "SELECT policyname, cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname='public' AND tablename=$1", table)}


def _row_security(table: str) -> tuple[bool, bool]:
    r = _fetch("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
               "WHERE relnamespace='public'::regnamespace AND relname=$1", table)
    assert r, f"{table} does not exist"
    return r[0]["relrowsecurity"], r[0]["relforcerowsecurity"]


# --- what 0033's own DDL builds ---------------------------------------------------------------------

def test_0033_creates_the_table_when_it_is_genuinely_absent(at_0033):
    """The migration's `create_table` branch, executed for the first time.

    Until this test, the only thing that had ever created this table was `create_all()` from the ORM
    model. If 0033's column list had drifted from the model — a missing column, a wrong type, a forgotten
    server_default — nothing would have noticed, because no environment ran the branch.
    """
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"])
            for r in _fetch("SELECT column_name, data_type, is_nullable, column_default "
                            "FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=$1", TABLE)}
    assert cols, "0033 did not create semantic_shadow_jobs — its create_table branch is still dead code"

    # The columns the claim/lease machine and the typed-outcome contract cannot work without.
    for required in ("id", "user_id", "channel", "provider_message_id", "status", "outcome",
                     "attempts", "max_attempts", "lease_owner", "lease_expires_at", "last_error",
                     "router_snapshot", "context_flags", "observation", "agrees", "divergence",
                     "latency_ms", "observed_at", "created_at", "updated_at", "version"):
        assert required in cols, f"0033 built the table without {required!r}"

    assert cols["status"][1] == "NO" and "pending" in (cols["status"][2] or ""), (
        "status must default to pending and never be NULL — a row with no status is claimable by nobody "
        "and counts as nothing, which is a dropped observation wearing a different mask")
    assert cols["attempts"][1] == "NO" and cols["max_attempts"][1] == "NO", (
        "the retry bound lives in these two columns; a NULL in either makes `attempts < max_attempts` "
        "NULL, and a job whose bound does not evaluate is claimed forever")
    assert cols["observation"][1] == "YES" and cols["outcome"][1] == "YES", (
        "a job that has not been read yet has no observation and no outcome")


def test_0033_creates_the_intended_unique_constraint_with_exactly_those_columns(at_0033):
    """The idempotency key, by NAME and by COLUMN LIST.

    `enqueue` writes `ON CONFLICT DO NOTHING`, so this constraint is the entire reason a redelivered
    webhook cannot become a second observation of one turn. And the columns are asserted exactly, in
    order: a narrower key — dropping `channel`, say — still satisfies "a unique constraint exists" while
    silently swallowing the second channel's turn, which is a LOSS, and losses bias the authority sample.
    """
    got = _unique_constraints(TABLE)
    assert "uq_semantic_shadow_job_turn" in got, (
        "the turn-identity unique constraint does not exist after 0033 — enqueue's ON CONFLICT has "
        "nothing to conflict against and two deliveries of one webhook become two observations")
    assert got["uq_semantic_shadow_job_turn"] == ["user_id", "channel", "provider_message_id"], (
        f"the unique key is over {got['uq_semantic_shadow_job_turn']}, not "
        "(user_id, channel, provider_message_id)")


def test_0033_creates_the_claim_hot_path_index(at_0033):
    """`claim` selects on status + lease_expires_at on every worker wake. Without the index that is a
    sequential scan of the queue per claim, and the queue is the thing that must not fall behind."""
    assert "ix_semantic_shadow_jobs_claimable" in _indexes(TABLE), (
        "the claim index is missing, so the worker's hot path scans the whole table")


def test_0033_enables_and_forces_row_level_security(at_0033):
    """ENABLED is not enough. FORCE is what subjects the TABLE OWNER to the policy — without it the
    migration role, and anything running as it, reads every student's shadow rows."""
    enabled, forced = _row_security(TABLE)
    assert enabled, "row level security is not enabled on semantic_shadow_jobs"
    assert forced, "row level security is not FORCED — the owner bypasses the policy"


def test_0033_creates_the_tenant_or_worker_policy(at_0033):
    """The policy, and what it actually says.

    FORCE RLS with a policy of `USING (true)` would satisfy every flag test in the suite and isolate
    nothing. So the predicate is asserted: the owner (`app_current_user()`) or a worker session
    (`app_is_worker()`), on both the read and the write side — a WITH CHECK that admitted more than the
    USING clause would let a tenant insert rows they could not then see.
    """
    pol = _policies(TABLE)
    assert "tenant_or_worker" in pol, "0033 did not create the tenant_or_worker policy"
    _cmd, qual, with_check = pol["tenant_or_worker"]
    for clause, name in ((qual, "USING"), (with_check, "WITH CHECK")):
        assert clause, f"the policy has no {name} clause, so it constrains nothing on that side"
        assert "app_current_user()" in clause and "app_is_worker()" in clause, (
            f"the {name} clause is {clause!r} — it must admit the owner and a worker session, and "
            "nothing else")


# --- and the rest of the intended final schema, on a table a migration built -------------------------

def test_the_chain_to_head_leaves_the_intended_final_schema(at_0033):
    """Take the chain to head from a table `op.create_table` built, and read the result.

    Every column, constraint and policy below is asserted from the catalog on a database where the ORM's
    `create_all()` never touched this table. That is the only way to know the migration path and the ORM
    path agree — and it is the path staging will take, because staging is at 0032 and does not have this
    table at all.
    """
    head = _alembic("upgrade", "head")
    assert head.returncode == 0, "the chain from 0033 to head failed:\n" + head.stderr[-2000:]

    cols = {r["column_name"]: r["is_nullable"] for r in _fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1", TABLE)}
    assert cols.get("lease_token") == "YES", (
        "the per-claim lease fence column never landed — `lease_owner` alone cannot prove WHICH claim a "
        "write belongs to, because two workers on one container share that string")
    assert cols.get("false_capability_denial") == "YES", (
        "the capability-denial column never landed — the metric the authority decision most depends on "
        "would not be countable in SQL")
    assert cols.get("reachable_operations") == "YES", (
        "the per-turn capability snapshot never landed. NULLABLE deliberately: NULL is 'we could not "
        "establish capability truth', which is not the same fact as an empty list")
    assert cols.get("intake_disposition") == "NO", (
        "the intake disposition column never landed — an exclusion that leaves nothing behind is "
        "indistinguishable from a dropped turn")
    assert cols.get("conversation_turn_id") == "YES", "the canonical turn reference never landed"

    got = _unique_constraints(TABLE)
    assert got.get("uq_semantic_shadow_job_conversation_turn") == ["conversation_turn_id"], (
        f"the canonical idempotency key is {got.get('uq_semantic_shadow_job_conversation_turn')}, not "
        "(conversation_turn_id) — the invariant is one job per CONVERSATION TURN")
    assert got.get("uq_semantic_shadow_job_turn") == ["user_id", "channel", "provider_message_id"], (
        "the ingress-dedupe key was dropped or narrowed. It is still what wins the race between two "
        "simultaneous deliveries in the instant before a canonical id is known")

    # THE RETRY BOUND, at the database. A max_attempts of 0 makes `attempts < max_attempts` false on a
    # row that has never been claimed, so no fresh job is ever claimable: total silent loss of every
    # observation, with an empty backlog and no error anywhere.
    checks = {r["conname"]: r["def"] for r in _fetch(
        "SELECT c.conname, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "JOIN pg_class rel ON rel.oid = c.conrelid WHERE rel.relname = $1 AND c.contype = 'c'", TABLE)}
    assert "ck_semantic_shadow_job_max_attempts_positive" in checks, (
        "nothing stops max_attempts from being 0, which makes every newly written job permanently "
        "unclaimable — 100% loss of observations, an empty queue, and not one error")
    assert "max_attempts > 0" in checks["ck_semantic_shadow_job_max_attempts_positive"].replace("(", "").replace(")", "")
    default = _fetch("SELECT column_default FROM information_schema.columns "
                     "WHERE table_name=$1 AND column_name='max_attempts'", TABLE)[0]["column_default"]
    assert default is not None and int(str(default).split("::")[0]) > 0, (
        f"the max_attempts DEFAULT is {default!r}; a default of 0 loses every observation silently")

    # THE CASCADE. Telemetry pointing at a deleted turn is a deletion that did not finish.
    fks = {r["conname"]: r["def"] for r in _fetch(
        "SELECT c.conname, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "JOIN pg_class rel ON rel.oid = c.conrelid WHERE rel.relname = $1 AND c.contype = 'f'", TABLE)}
    turn_fks = [d for d in fks.values() if "conversation_turn_id" in d]
    assert turn_fks and all("ON DELETE CASCADE" in d for d in turn_fks), (
        f"the canonical turn FK is {turn_fks!r} — without ON DELETE CASCADE a shadow row outlives the "
        "student content it describes")

    # THE READ PATH IS A FUNCTION, AND THE POLICY THAT WOULD BE THE OBVIOUS ALTERNATIVE MUST NOT EXIST.
    # `FOR SELECT USING (app_is_worker())` on conversation_turns is how you would buy the reconciliation
    # count with a grant. RLS admits ROWS, not columns, so it hands every worker session every column of
    # every student's turn — `text` included, and `SELECT text FROM conversation_turns` already exists in
    # the module that would have used it. The count comes from an aggregate-only definer instead.
    turns_pol = _policies(TURNS)
    assert "shadow_reconciliation_worker" not in turns_pol, (
        "a global worker SELECT policy is on conversation_turns after the chain reached head — the "
        "reconciliation's count is being bought with a read of every student's message text")
    assert not [n for n, (cmd, qual, _wc) in turns_pol.items()
                if cmd in ("SELECT", "ALL") and qual and "app_is_worker" in qual], (
        f"a worker-admitting read policy exists on conversation_turns: {turns_pol!r}. A worker may reach "
        "one CLAIMED turn under its owner's session and may COUNT through the aggregate function; it may "
        "never enumerate the ledger")
    assert "tenant_isolation" in turns_pol, (
        "the owner-scoped policy on conversation_turns is gone — asserting that a worker policy does NOT "
        "exist is worthless if the policy that DOES the isolating went with it")

    enabled, forced = _row_security(TURNS)
    assert (enabled, forced) == (True, True), (
        "conversation_turns lost ENABLE/FORCE row security, which would make every policy above "
        "decorative")
    assert "ix_conversation_turns_role_created" in _indexes(TURNS), (
        "the reconciliation window scans role='user' over a time range on every wake; without this "
        "index that is a sequential scan of the most-written table in the schema")


# ---------------------------------------------------------------------------------------------------
# A ROLE IS CLUSTER-WIDE AND AN UPGRADE IS NOT.
#
# 0035 provisions `bruce_shadow_recon`. Databases are the unit an upgrade runs against, but `pg_authid`
# is shared by the whole cluster, so two upgrades of two DIFFERENT databases write the same catalog row.
# `CREATE ROLE` was guarded from the start. The `ALTER ROLE` beside it was not, and an unconditional
# ALTER writes whether or not it changes anything: the loser of the race died with `tuple concurrently
# updated` and took its entire upgrade down with it.
#
# It surfaced as `test_db_isolation` failing only inside the full suite, which is the tell — the defect
# needs two upgrades overlapping, and running the file alone never produces two. It is not a test-only
# hazard. Api and worker booting together, or a migration job that gets retried while the first attempt
# is still running, is the same race against the same row.
#
# This test fails on the unconditional ALTER and passes on the conditional one. It does NOT assert the
# retry loop's existence, only the property the loop is there for: concurrent upgrades all succeed and
# the role still ends up with exactly the attributes the privilege boundary depends on.
# ---------------------------------------------------------------------------------------------------

CONCURRENT_DBS = [f"bruce_migconc_{_WORKER}_{i}" for i in range(3)]


def _upgrade_head(database: str) -> subprocess.CompletedProcess:
    o = _owner_url()
    dsn = f"postgresql+asyncpg://{o.username}:{o.password}@{o.host}:{o.port or 5432}/{database}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), "upgrade", "head"],
        cwd=str(ENGINE), env={**os.environ, "BRUCE_DATABASE_URL": dsn},
        capture_output=True, text=True)


@pytest.fixture
def three_empty_databases():
    async def _make():
        c = await _connect("postgres")
        try:
            for d in CONCURRENT_DBS:
                await c.execute(f'DROP DATABASE IF EXISTS "{d}"')
                await c.execute(f'CREATE DATABASE "{d}"')
        finally:
            await c.close()

    async def _drop():
        c = await _connect("postgres")
        try:
            for d in CONCURRENT_DBS:
                await c.execute(f'DROP DATABASE IF EXISTS "{d}"')
        finally:
            await c.close()

    asyncio.run(_make())
    try:
        yield CONCURRENT_DBS
    finally:
        asyncio.run(_drop())


def test_concurrent_upgrades_of_different_databases_all_succeed(three_empty_databases):
    """Three upgrades, three databases, one shared `pg_authid` row. All three must reach head."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(three_empty_databases)) as pool:
        results = list(pool.map(_upgrade_head, three_empty_databases))

    failed = {db: (r.stderr or r.stdout)[-1200:]
              for db, r in zip(three_empty_databases, results) if r.returncode != 0}
    assert not failed, (
        "a concurrent upgrade of a SEPARATE database failed. If the message is `tuple concurrently "
        f"updated`, an unconditional role write is back in the chain:\n{failed}")

    role = _fetch("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                  "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = $1",
                  "bruce_shadow_recon")
    assert role, "the reconciliation owner does not exist after three upgrades reached head"
    attrs = dict(role[0])
    assert attrs == {"rolcanlogin": False, "rolsuper": False, "rolcreatedb": False,
                     "rolcreaterole": False, "rolinherit": False, "rolreplication": False,
                     "rolbypassrls": True}, (
        f"the race left the reconciliation owner with the wrong attributes: {attrs}. Surviving a "
        "concurrent upgrade is worth nothing if the survivor is a role the privilege boundary does "
        "not describe")
