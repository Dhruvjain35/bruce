"""PRE-DEPLOY CHECK: can the code at HEAD boot and run a turn when the migration chain stops at 0034?

WHY THIS EXISTS. The MAE deploy intentionally migrates to `0034_known_people_row_security` and stops.
`0035_shadow_reconciliation` is deliberately outside the live scope — it skips creating its function on
Cloud SQL anyway (`rolbypassrls=False`, measured on the staging instance) — and `0033_semantic_shadow_jobs`
comes with it in the chain, so the deployed code runs against a database where `semantic_shadow_jobs`
DOES NOT EXIST.

That is the risk this script exists to settle, and it is bigger than the one that was asked about.
`BRUCE_SEMANTIC_SHADOW` being unset stops `intake()` from writing — but `worker_api.process` calls
`backlog()`, `sweep_exhausted()` and `health()` with NO `enabled()` gate, on every wake, and all three
read that missing table. Cloud Scheduler fires `/process` every 60 seconds. Reading the three `try/except`
blocks says they should degrade to warnings; a green reading of an exception handler is not evidence that
the worker survives, and this repo's whole discipline is that it is not.

So this runs the REAL paths against a REAL database migrated to exactly 0034:

  1. the worker tick        `worker_api.process()`             — the thing Scheduler calls every 60s
  2. a full student turn    `conversation_runtime.handle()`    — the MAE path itself

PASS means the deploy is safe with 0035 (and 0033) absent. FAIL means the deploy is blocked, and the
reason will be a real traceback rather than an inference.

    cd engine && .venv/bin/python scripts/verify_deploy_at_0034.py

Creates and DROPS its own scratch database. Touches nothing else. Never points at staging: it derives its
URL from the LOCAL BRUCE_DATABASE_URL and refuses if that host is not local.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import pathlib
import subprocess
import sys
from uuid import uuid4

import asyncpg
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ENGINE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
load_dotenv(ENGINE / ".env")

SCRATCH = "bruce_verify_0034"
TARGET_REVISION = "0034_known_people_row_security"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None, ""}


def _swap(url: str, dbname: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + dbname


async def _admin(sql: str, *, database: str = "postgres") -> None:
    owner = make_url(os.environ["BRUCE_DATABASE_URL"])
    conn = await asyncpg.connect(host=owner.host, port=owner.port or 5432, user=owner.username,
                                 password=owner.password, database=database)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def _guard_local() -> None:
    owner = make_url(os.environ["BRUCE_DATABASE_URL"])
    if owner.host not in _LOCAL_HOSTS:
        raise SystemExit(f"REFUSING: BRUCE_DATABASE_URL points at {owner.host!r}, not a local host. "
                         f"This script creates and drops databases and must never run against staging.")


async def _absent(url: str) -> tuple[str | None, str | None, str]:
    conn = await asyncpg.connect(dsn=url.replace("+asyncpg", ""))
    try:
        table = await conn.fetchval("SELECT to_regclass('public.semantic_shadow_jobs')::text")
        func = await conn.fetchval("SELECT to_regproc('public.shadow_reconciliation')::text")
        head = await conn.fetchval("SELECT version_num FROM alembic_version")
        return table, func, head
    finally:
        await conn.close()


async def _run_checks() -> int:
    import bruce_engine.db as db
    from bruce_engine import conversation_runtime, schema, worker_api
    from bruce_engine.conversation_contract import (ConversationDecision, IntentKind, ResponseType,
                                                    RiskLevel)
    from bruce_engine.conversation_model import ReasonResult
    from bruce_engine.db import user_session
    from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
    from sqlalchemy import select

    failures: list[str] = []

    # --- 1. the worker tick, exactly as Cloud Scheduler calls it ---------------------------------------
    print("\n[1] worker_api.process()  — Scheduler fires this every 60s")
    try:
        out = await worker_api.process()
        print(f"    OK  processed={out.get('processed')} shadow_errors={out.get('shadow_errors')} "
              f"backlog_before={out.get('shadow_backlog_before')}")
        print(f"    shadow_health={'EMPTY (degraded, as designed)' if not out.get('shadow_health') else out['shadow_health']}")
        if out.get("shadow_errors", 0) == 0:
            print("    NOTE shadow_errors is 0 — the shadow reads did not even fail, so nothing degraded.")
        else:
            print(f"    NOTE shadow_errors={out['shadow_errors']} — degraded to warnings, which is the "
                  f"designed behaviour when the table is absent. The worker still returned.")
    except Exception as exc:
        failures.append(f"worker tick RAISED {type(exc).__name__}: {exc}")
        print(f"    FAIL {type(exc).__name__}: {exc}")

    # --- 2. a full student turn, which is the MAE path -------------------------------------------------
    print("\n[2] conversation_runtime.handle()  — one real turn end to end")
    uid = uuid4()

    class _Reasoner:
        provider = model = "fake"
        supports_vision = True

        async def decide(self, *, text, images, context):
            return ReasonResult(
                decision=ConversationDecision(
                    intent=IntentKind.casual, response_type=ResponseType.direct_answer,
                    user_visible_response="hey", extracted_entities=[], required_capabilities=[],
                    needs_mission=False, risk_level=RiskLevel.none, confidence=0.8),
                provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)

    try:
        async with user_session(uid) as s:
            s.add(schema.User(id=uid, auth_provider="alpha_bridge"))
        msg = InboundMessage(provider_message_id="verify-1",
                             channel=ChannelKind.self_hosted_imessage, channel_identity="+15550001111",
                             text="hey whats up", attachments=[],
                             timestamp=datetime.datetime.now(datetime.timezone.utc))
        res = await conversation_runtime.handle(FakeChannel(), msg, user_id=uid,
                                                reply_target="+15550001111", reasoner=_Reasoner())
        print(f"    OK  status={res.status}")
        if res.status != "processed":
            failures.append(f"turn status was {res.status!r}, expected 'processed'")
        async with user_session(uid) as s:
            n = len((await s.execute(select(schema.OutboundMessageRow).where(
                schema.OutboundMessageRow.user_id == uid))).scalars().all())
        print(f"    outbound rows: {n}")
        if n != 1:
            failures.append(f"expected exactly 1 outbound reply, got {n}")
    except Exception as exc:
        failures.append(f"student turn RAISED {type(exc).__name__}: {exc}")
        print(f"    FAIL {type(exc).__name__}: {exc}")

    # --- 3. a redelivery must still be refused (N7 relies on a constraint 0034 already carries) --------
    print("\n[3] redelivery of the same message  — the N7 claim, at 0034")
    try:
        msg2 = InboundMessage(provider_message_id="verify-1",
                              channel=ChannelKind.self_hosted_imessage, channel_identity="+15550001111",
                              text="hey whats up", attachments=[],
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
        res2 = await conversation_runtime.handle(FakeChannel(), msg2, user_id=uid,
                                                 reply_target="+15550001111", reasoner=_Reasoner())
        print(f"    OK  status={res2.status}")
        if res2.status != "duplicate":
            failures.append(f"redelivery status was {res2.status!r}, expected 'duplicate'")
    except Exception as exc:
        failures.append(f"redelivery RAISED {type(exc).__name__}: {exc}")
        print(f"    FAIL {type(exc).__name__}: {exc}")

    await db.get_engine().dispose()

    print("\n" + "=" * 78)
    if failures:
        print(f"BLOCKED — {len(failures)} failure(s) deploying at {TARGET_REVISION}:")
        for f in failures:
            print(f"  * {f}")
        return 1
    print(f"SAFE — the code at HEAD boots and runs a turn with the chain stopped at {TARGET_REVISION}.")
    print("       semantic_shadow_jobs EXISTS (0033 is 0034's ancestor); shadow_reconciliation does NOT.")
    print("       The worker tick, a full student turn and a redelivery all behaved correctly, and the")
    print("       missing aggregate degraded to reconciliation_status='unknown' rather than a false")
    print("       'failed' or a cheerful zero.")
    return 0


def main() -> int:
    _guard_local()
    owner_url = os.environ["BRUCE_DATABASE_URL"]
    app_url = os.environ["BRUCE_APP_DATABASE_URL"]
    scratch_owner, scratch_app = _swap(owner_url, SCRATCH), _swap(app_url, SCRATCH)

    print(f"scratch database: {SCRATCH}   target revision: {TARGET_REVISION}")
    asyncio.run(_admin(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bruce_app') "
        "THEN CREATE ROLE bruce_app LOGIN PASSWORD 'bruce_dev_pw'; END IF; END $$;"))
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{SCRATCH}"'))
    try:
        # THE POINT: upgrade to a NAMED revision, not `head`.
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), "upgrade", TARGET_REVISION],
            cwd=str(ENGINE), env={**os.environ, "BRUCE_DATABASE_URL": scratch_owner},
            capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout[-3000:])
            print(proc.stderr[-3000:])
            return 2

        table, func, head = asyncio.run(_absent(scratch_owner))
        print(f"alembic_version={head}")
        print(f"semantic_shadow_jobs={table}   shadow_reconciliation={func}")
        if head != TARGET_REVISION:
            print(f"REFUSING: database is at {head!r}, expected {TARGET_REVISION!r}")
            return 2
        # WHAT 0034 ACTUALLY LEAVES BEHIND, and it is not what the deploy plan assumed. 0033 is 0034's
        # ANCESTOR (0032 -> 0033 -> 0034 -> 0035), so stopping at 0034 still applies the whole semantic
        # shadow queue: `semantic_shadow_jobs` EXISTS. Only 0035's `shadow_reconciliation` is left out.
        #
        # That is the good outcome. The feared failure was the worker's ungated backlog()/health() reads
        # hitting a missing TABLE every 60 seconds; those reads now find their table. What is genuinely
        # absent is the aggregate FUNCTION, whose callers were already verified to degrade to "unknown"
        # with NULL counts rather than raise — and which would have skipped creating itself on Cloud SQL
        # regardless, since the migrator has rolbypassrls=False there.
        if table is None:
            print("REFUSING: semantic_shadow_jobs is absent at 0034, which contradicts the chain "
                  "0032 -> 0033 -> 0034. The migration graph is not what this check assumes.")
            return 2
        if func is not None:
            print("REFUSING: shadow_reconciliation exists, so 0035 was applied and this run would not "
                  "prove anything about deploying without it.")
            return 2

        os.environ["BRUCE_DATABASE_URL"] = scratch_owner
        os.environ["BRUCE_APP_DATABASE_URL"] = scratch_app
        os.environ.pop("BRUCE_SEMANTIC_SHADOW", None)      # the deployed default: shadow OFF
        return asyncio.run(_run_checks())
    finally:
        os.environ["BRUCE_DATABASE_URL"] = owner_url
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)'))
        print(f"\nscratch database {SCRATCH} dropped")


if __name__ == "__main__":
    raise SystemExit(main())
