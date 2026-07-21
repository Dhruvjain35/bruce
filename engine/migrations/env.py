"""Alembic environment — async, owner-run migrations for the Bruce schema + security objects."""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make bruce_engine importable and load local env (owner DB url lives in engine/.env).
ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))
load_dotenv(ENGINE_DIR / ".env")

from bruce_engine.schema import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations run as the OWNER (privileged), never the restricted app role.
DB_URL = os.environ["BRUCE_DATABASE_URL"]
config.set_main_option("sqlalchemy.url", DB_URL)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL, target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # The migration role owns the tables but is NOT necessarily a superuser (e.g. Cloud SQL),
        # and several control tables ENABLE + FORCE ROW LEVEL SECURITY *before* seeding their
        # singleton row. FORCE subjects even the owner to RLS, so those seeds are rejected unless
        # the session carries the admin/worker context the policies check:
        #   * 0013 capability_global_state — state_insert WITH CHECK (app_is_admin())
        #   * 0014 relay_control          — worker_only  WITH CHECK (app_is_worker())
        # Grant that context for THIS migration transaction only. set_config(..., is_local=True)
        # is SET LOCAL, so the values are scoped to the transaction and vanish on COMMIT or
        # ROLLBACK — no persistent role/database state, and the runtime bruce_app role is never
        # affected. (In CI the owner is the postgres superuser, which bypasses RLS regardless.)
        connection.exec_driver_sql("SELECT set_config('app.admin', 'on', true)")
        connection.exec_driver_sql("SELECT set_config('app.worker', 'on', true)")
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
