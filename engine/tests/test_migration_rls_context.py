"""Regression test for the migration-time RLS context fix (env.py).

Reproduces the Cloud SQL failure that a superuser-owner CI never exercised: the migration role
owns the tables but is NOT a superuser, and several control tables ENABLE + FORCE ROW LEVEL
SECURITY *before* seeding their singleton row. FORCE subjects even the owner to RLS, so the seeds
in 0013 (capability_global_state, app_is_admin()) and 0014 (relay_control, app_is_worker()) are
rejected unless the migration transaction carries that context.

`migrations/env.py` now sets `app.admin`/`app.worker` transaction-locally (SET LOCAL via
set_config(..., is_local=True)) for the migration transaction only. These tests prove:

  1. the migration role used here is non-superuser (real reproduction, not a superuser shortcut)
  2. that role owns the tables it creates
  3. FORCE ROW LEVEL SECURITY is actually enabled on the seeded control tables
  4. a FORCE-RLS seed INSERT is denied without the migration-local context
  5. the same seed succeeds with the transaction-local context (real 0011 -> 0016 upgrade)
  6. the context is absent again after COMMIT
  7. the context is absent again after ROLLBACK
  8. the runtime bruce_app role still cannot bypass the policies
  9. the full real migration chain reaches head 0016 (exactly one head) as the non-super owner
 10. downgrade + re-upgrade round-trips unchanged under the fix

It also asserts a failed FORCE-RLS seed transaction is atomic (no partial schema left behind).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from asyncpg import exceptions as pgerr
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ENGINE = Path(__file__).resolve().parents[1]
load_dotenv(ENGINE / ".env")

MIG_DB = "bruce_migctx_test"
MIGRATOR = "bruce_migrator_rlstest"
MIGRATOR_PW = "mig_rls_test_pw"
APP_ROLE = "bruce_app"
APP_PW = "bruce_dev_pw"

_ADMIN_FN = """
CREATE OR REPLACE FUNCTION app_is_admin() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN coalesce(current_setting('app.admin', true), '') = 'on';
EXCEPTION WHEN others THEN
    RETURN false;
END $$;
"""


def _owner():
    url = os.environ.get("BRUCE_DATABASE_URL")
    if not url:
        pytest.skip("BRUCE_DATABASE_URL (owner) not configured")
    return make_url(url)


async def _connect(*, database, user=None, password=None):
    o = _owner()
    return await asyncpg.connect(
        host=o.host,
        port=o.port or 5432,
        user=user if user is not None else o.username,
        password=password if password is not None else o.password,
        database=database,
    )


def _migrator_dsn() -> str:
    o = _owner()
    return f"postgresql+asyncpg://{MIGRATOR}:{MIGRATOR_PW}@{o.host}:{o.port or 5432}/{MIG_DB}"


def _run_alembic(*argv: str, dsn: str) -> subprocess.CompletedProcess:
    """Run alembic against `dsn` (as the non-super migration role). load_dotenv in env.py does not
    override an explicitly-passed BRUCE_DATABASE_URL, so this DSN wins over engine/.env."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), *argv],
        cwd=str(ENGINE),
        env={**os.environ, "BRUCE_DATABASE_URL": dsn},
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def rls_migbed():
    """A fresh database OWNED BY a real non-superuser, non-BYPASSRLS role — the Cloud SQL shape."""
    _owner()
    try:
        asyncio.run(_ping())
    except Exception:
        pytest.skip("Postgres not reachable")

    async def _setup():
        admin = await _connect(database="postgres")
        try:
            await admin.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{APP_ROLE}') "
                f"THEN CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PW}'; END IF; END $$;"
            )
            await admin.execute(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)")
            await admin.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{MIGRATOR}') "
                f"THEN CREATE ROLE {MIGRATOR} LOGIN PASSWORD '{MIGRATOR_PW}'; END IF; END $$;"
            )
            # enforce the invariant that makes this a real reproduction, even on a reused role
            await admin.execute(f"ALTER ROLE {MIGRATOR} NOSUPERUSER NOBYPASSRLS NOCREATEROLE LOGIN PASSWORD '{MIGRATOR_PW}'")
            await admin.execute(f"CREATE DATABASE {MIG_DB} OWNER {MIGRATOR}")
            row = await admin.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=$1", MIGRATOR
            )
        finally:
            await admin.close()
        # (req 1) the migration role must genuinely lack superuser + BYPASSRLS
        assert row is not None and not row["rolsuper"] and not row["rolbypassrls"], (
            "test invalid: migration role must be non-superuser and non-BYPASSRLS"
        )

    asyncio.run(_setup())
    yield {"db": MIG_DB, "migrator": MIGRATOR}

    async def _teardown():
        admin = await _connect(database="postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)")
            await admin.execute(f"DROP ROLE IF EXISTS {MIGRATOR}")
        finally:
            await admin.close()

    asyncio.run(_teardown())


async def _ping():
    c = await _connect(database="postgres")
    await c.close()


@pytest.fixture(scope="session")
def migrated_db(rls_migbed):
    """Run the REAL 0001 -> 0016 chain as the non-super owner. Fails loudly if the seeds are
    rejected (i.e. if the env.py context fix regresses)."""
    dsn = _migrator_dsn()
    r = _run_alembic("upgrade", "head", dsn=dsn)
    if r.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed as a non-superuser owner — the migration-time RLS "
            "context fix has regressed.\nSTDOUT:\n" + r.stdout[-2000:] + "\nSTDERR:\n" + r.stderr[-4000:]
        )
    return {"dsn": dsn}


# (reqs 2, 3, 5, 9) real migrations reach head 0016 (exactly one head) under the non-super owner,
# the owner really owns the tables, and FORCE RLS is on.
def test_full_chain_upgrades_and_seeds_as_nonsuperuser_owner(migrated_db):
    async def _check():
        c = await _connect(user=MIGRATOR, password=MIGRATOR_PW, database=MIG_DB)
        try:
            heads = [r["version_num"] for r in await c.fetch("SELECT version_num FROM alembic_version")]
            owner = await c.fetchval(
                "SELECT tableowner FROM pg_tables WHERE tablename='capability_global_state'"
            )
            forced = await c.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname='capability_global_state'"
            )
            # The seeded rows are themselves behind the FORCE-RLS read policies (worker/admin only),
            # so grant this read session the same context to observe that the seeds landed.
            await c.execute("SELECT set_config('app.admin', 'on', false)")
            await c.execute("SELECT set_config('app.worker', 'on', false)")
            admin_seed = await c.fetchval("SELECT count(*) FROM capability_global_state")  # 0013 seed
            worker_seed = await c.fetchval("SELECT count(*) FROM relay_control")            # 0014 seed
        finally:
            await c.close()
        return heads, owner, forced, admin_seed, worker_seed

    heads, owner, forced, admin_seed, worker_seed = asyncio.run(_check())
    assert heads == ["0016_relay_bootstrap"], f"expected exactly one head at 0016, got {heads}"
    assert owner == MIGRATOR, f"migration role must OWN the tables, owner={owner}"  # req 2
    assert forced is True, "capability_global_state must have FORCE ROW LEVEL SECURITY"  # req 3
    assert admin_seed >= 1, "0013 capability_global_state seed missing (app_is_admin path)"  # req 5
    assert worker_seed >= 1, "0014 relay_control seed missing (app_is_worker path)"          # req 5


# (reqs 3, 4 + atomicity) without the transaction-local context, a FORCE-RLS seed INSERT is denied,
# and the failed DDL+seed transaction rolls back with no partial schema.
def test_forced_rls_seed_denied_without_context_is_atomic(rls_migbed):
    async def _run():
        c = await _connect(user=MIGRATOR, password=MIGRATOR_PW, database=MIG_DB)
        try:
            await c.execute(_ADMIN_FN)
            denied = False
            tx = c.transaction()
            await tx.start()
            try:
                await c.execute("CREATE TABLE rls_probe (id int PRIMARY KEY)")
                await c.execute("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
                await c.execute("ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY")
                await c.execute("CREATE POLICY p_ins ON rls_probe FOR INSERT WITH CHECK (app_is_admin())")
                await c.execute("INSERT INTO rls_probe (id) VALUES (1)")  # no app.admin -> RLS denies
                await tx.commit()
            except pgerr.InsufficientPrivilegeError:
                denied = True
                await tx.rollback()
            leftover = await c.fetchval("SELECT to_regclass('public.rls_probe')")
        finally:
            await c.close()
        return denied, leftover

    denied, leftover = asyncio.run(_run())
    assert denied, "FORCE-RLS seed INSERT must be denied without app.admin context"       # req 4
    assert leftover is None, "failed seed transaction must be atomic (no partial schema)"  # atomicity


# (reqs 5, 6, 7) the transaction-local context enables the seed, then disappears on COMMIT and
# on ROLLBACK (never persisted, exactly like SET LOCAL).
def test_transaction_local_context_enables_seed_then_vanishes(rls_migbed):
    async def _run():
        c = await _connect(user=MIGRATOR, password=MIGRATOR_PW, database=MIG_DB)
        try:
            await c.execute(_ADMIN_FN)
            await c.execute("DROP TABLE IF EXISTS rls_probe2")
            await c.execute("CREATE TABLE rls_probe2 (id int PRIMARY KEY)")
            await c.execute("ALTER TABLE rls_probe2 ENABLE ROW LEVEL SECURITY")
            await c.execute("ALTER TABLE rls_probe2 FORCE ROW LEVEL SECURITY")
            await c.execute("CREATE POLICY p_ins ON rls_probe2 FOR INSERT WITH CHECK (app_is_admin())")
            await c.execute("CREATE POLICY p_sel ON rls_probe2 FOR SELECT USING (true)")

            tx = c.transaction()
            await tx.start()
            await c.execute("SELECT set_config('app.admin', 'on', true)")
            in_txn = await c.fetchval("SELECT current_setting('app.admin', true)")
            await c.execute("INSERT INTO rls_probe2 (id) VALUES (1)")
            await tx.commit()
            seeded = await c.fetchval("SELECT count(*) FROM rls_probe2")
            after_commit = await c.fetchval("SELECT current_setting('app.admin', true)")

            tx2 = c.transaction()
            await tx2.start()
            await c.execute("SELECT set_config('app.admin', 'on', true)")
            await tx2.rollback()
            after_rollback = await c.fetchval("SELECT current_setting('app.admin', true)")
        finally:
            await c.close()
        return in_txn, seeded, after_commit, after_rollback

    in_txn, seeded, after_commit, after_rollback = asyncio.run(_run())
    assert in_txn == "on", "context must be active inside the migration transaction"          # req 5
    assert seeded == 1, "seed with transaction-local app.admin must be inserted"              # req 5
    assert after_commit in ("", None), f"app.admin must be gone after COMMIT, got {after_commit!r}"    # req 6
    assert after_rollback in ("", None), f"app.admin must be gone after ROLLBACK, got {after_rollback!r}"  # req 7


# (req 8) the fix touches migration-time owner sessions only; the runtime bruce_app role still
# cannot read or write the admin-only control table.
def test_runtime_bruce_app_still_cannot_bypass_policies(migrated_db):
    async def _run():
        c = await _connect(user=APP_ROLE, password=APP_PW, database=MIG_DB)
        try:
            visible = await c.fetchval("SELECT count(*) FROM capability_global_state")
            insert_denied = False
            try:
                await c.execute(
                    "INSERT INTO capability_global_state (capability, environment, rollout_state, killed) "
                    "VALUES ('evil', 'test', 'default_off', false)"
                )
            except pgerr.InsufficientPrivilegeError:
                insert_denied = True
        finally:
            await c.close()
        return visible, insert_denied

    visible, insert_denied = asyncio.run(_run())
    assert visible == 0, "bruce_app must not see admin-only rows (state_read is worker/admin only)"
    assert insert_denied, "bruce_app must be denied INSERT into the admin-only control table"


# (req 10) downgrade one step then re-upgrade to head still works under the fix.
def test_downgrade_and_reupgrade_roundtrip(migrated_db):
    dsn = migrated_db["dsn"]
    down = _run_alembic("downgrade", "-1", dsn=dsn)
    assert down.returncode == 0, "downgrade -1 failed:\n" + down.stderr[-2000:]
    up = _run_alembic("upgrade", "head", dsn=dsn)
    assert up.returncode == 0, "re-upgrade to head failed:\n" + up.stderr[-2000:]

    async def _head():
        c = await _connect(user=MIGRATOR, password=MIGRATOR_PW, database=MIG_DB)
        try:
            return [r["version_num"] for r in await c.fetch("SELECT version_num FROM alembic_version")]
        finally:
            await c.close()

    assert asyncio.run(_head()) == ["0016_relay_bootstrap"], "round-trip must return to head 0016"
