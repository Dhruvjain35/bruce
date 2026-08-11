"""THE RECONCILIATION MAY COUNT THE LEDGER. IT MAY NOT READ IT, AND IT MAY NOT LEARN WHO IS IN IT.

THE INCIDENT, IN TWO HALVES. Both halves were built, and both were removed before anything was deployed:
the undeployed chain that contained them was collapsed to 0033/0034/0035, so the revision numbers this
file used to cite no longer exist. What follows is the DESIGN history, which is what the tests are about —
every property below is asserted against the database, so none of it depends on a revision number.

FIRST HALF, THE ROW GRANT. The worker needed to count trusted turns across tenants and it was bought with
a row-level grant:

    CREATE POLICY shadow_reconciliation_worker ON conversation_turns FOR SELECT USING (app_is_worker())

RLS policies admit ROWS. There is no column list in a policy and there cannot be one. So the price of a
COUNT was SELECT on every column of every user's turn for any session that can set `app.worker` — `text`
first among them. It was replaced with an aggregate SECURITY DEFINER function.

SECOND HALF, WHICH IS WHY THIS FILE WAS REWRITTEN. That first function returned ONE ROW PER OWNER,
carrying `user_id`. The comparison was still finished outside the boundary: the worker looped those
owners and opened `user_session(uid)` on each to read that tenant's turns. An audit EXECUTED that path
and read every turn body in the window, one authorized tenant at a time. Column exposure closed;
ENUMERATION widened — before the row grant the worker learned an owner id only from a job it had CLAIMED,
and after the per-owner function it received every owner in the window, including the owners with no jobs
at all.

WHAT SHIPS INSTEAD puts the join INSIDE the definer. `public.shadow_reconciliation(window_start,
window_end)` inspects the canonical ledger and the shadow bookkeeping together and returns ONE ROW of
counts plus its own verdict. No user ids, no turn ids, no per-user rows, no text.

WHAT THIS FILE PROVES, in the database, as the roles that actually run:

   1. the worker role cannot SELECT conversation_turns.text globally
   2. the worker role cannot enumerate conversation_turns' rows or its user_ids
   3. the function's DECLARED result type contains no user_id and no turn id — and no uuid at all
   4. nothing in a returned row can be turned back into an owner identity
   5. the worker CAN execute it, and only with a worker context
   6. a student with ZERO shadow rows whose intake never ran still produces unobserved_turns > 0
   7. TWO such students produce the correct aggregate and neither identity appears anywhere
   8. an empty canonical window reports `clean`
   9. EXECUTE on the function grants no direct table access
  10. PUBLIC cannot execute it
  11. NO per-owner function exists — the shipped chain never creates one, and putting it back is refused
  12. a function changed to return `text` FAILS the privacy contract              (mutation)
  13. a function changed to return `user_id` FAILS the privacy contract           (mutation)
  14. a function that loses its fixed search_path FAILS the security gate         (mutation)
  15. restoring the broad app_is_worker() SELECT policy FAILS the privilege test  (mutation)
  16. restoring `shadow_turn_population` FAILS the privilege test                 (mutation)

The mutations MUTATE the live database and then restore it, because a contract nobody has watched fail is
a contract nobody has tested. Everything here reads pg_catalog or executes real SQL as a real role;
nothing reads this repository's source.
"""

from __future__ import annotations

import asyncio
import os
import re
from uuid import UUID, uuid4

import asyncpg
import pytest
from asyncpg import exceptions as pgerr
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from bruce_engine import semantic_shadow

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

APP_ROLE = "bruce_app"
APP_PW = "bruce_dev_pw"
RECON_OWNER = "bruce_shadow_recon"
TURNS = "conversation_turns"
JOBS = "semantic_shadow_jobs"
FN = "shadow_reconciliation"
FN_SIG = f"public.{FN}(timestamptz, timestamptz)"
RETIRED_FN = "shadow_turn_population"
RETIRED_SIG = f"public.{RETIRED_FN}(timestamptz, timestamptz)"
WORKER_POLICY = "shadow_reconciliation_worker"

# THE PRIVACY CONTRACT, stated once. These are the ONLY columns the function may hand back: three
# timestamps that bound and date the answer, eleven counts, and a verdict from a closed vocabulary. Not
# one of them can name a person, and not one of them is any part of a turn.
ALLOWED_RETURN_COLUMNS = {
    "window_start": "timestamp with time zone",
    "window_end": "timestamp with time zone",
    "watermark": "timestamp with time zone",
    "canonical_trusted_turns": "bigint",
    "trusted_turns_with_intake": "bigint",
    "trusted_turns_without_intake": "bigint",
    "explicitly_excluded_turns": "bigint",
    "eligible_turns": "bigint",
    "shadow_jobs": "bigint",
    "pending_jobs": "bigint",
    "processing_jobs": "bigint",
    "retryable_jobs": "bigint",
    "terminal_jobs": "bigint",
    "unobserved_turns": "bigint",
    "reconciliation_status": "text",
}

# Columns of conversation_turns that must never appear in the function's result type. `text` and
# `decision` are the student's words; `channel_identity` is their phone number; `provider_message_id` is
# a handle into the provider's copy of the conversation; `user_id` is the enumeration this round removes.
CONTENT_COLUMNS = ("text", "decision", "channel_identity", "provider_message_id", "intent", "user_id")

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _run(c):
    return asyncio.run(c)


def _url(key: str):
    url = os.environ.get(key)
    if not url:
        pytest.skip(f"{key} not configured")
    return make_url(url)


async def _owner_conn():
    o = _url("BRUCE_DATABASE_URL")
    return await asyncpg.connect(host=o.host, port=o.port or 5432, user=o.username,
                                 password=o.password, database=o.database)


async def _app_conn():
    a = _url("BRUCE_APP_DATABASE_URL")
    return await asyncpg.connect(host=a.host, port=a.port or 5432, user=APP_ROLE,
                                 password=APP_PW, database=a.database)


def _owner_exec(*statements: str) -> None:
    async def _go():
        c = await _owner_conn()
        try:
            for s in statements:
                await c.execute(s)
        finally:
            await c.close()

    _run(_go())


def _owner_fetch(sql: str, *args) -> list[asyncpg.Record]:
    async def _go():
        c = await _owner_conn()
        try:
            return await c.fetch(sql, *args)
        finally:
            await c.close()

    return _run(_go())


# --- the fixtures the proofs run against --------------------------------------------------------------


SECRET = "alvarez@school.edu about the retake on tuesday"


async def _seed(*owners: tuple[UUID, list[str], bool]) -> None:
    """Owners with trusted inbound turns in the window, written as they really are: under each owner's
    own RLS context, with real message text in them. `observed` says whether shadow's intake ran — an
    owner seeded with False is a student whose turns reached the canonical ledger and nothing else, which
    is the population the whole reconciliation exists to find.
    """
    c = await _owner_conn()
    try:
        for uid, texts, observed in owners:
            async with c.transaction():
                await c.execute("SELECT set_config('app.user_id', $1, true)", str(uid))
                await c.execute("INSERT INTO users (id, auth_provider) VALUES ($1, 'test')", uid)
                for i, body in enumerate(texts):
                    pmid = f"pm-{uid}-{i}"
                    turn_id = await c.fetchval(
                        f"INSERT INTO {TURNS} (user_id, channel, channel_identity, "
                        "provider_message_id, role, text, created_at) "
                        "VALUES ($1, 'imessage', '+15550100', $2, 'user', $3, "
                        "now() - interval '5 minutes') RETURNING id",
                        uid, pmid, body)
                    if observed:
                        await c.execute(
                            f"INSERT INTO {JOBS} (user_id, conversation_turn_id, channel, "
                            "provider_message_id, status, intake_disposition) "
                            "VALUES ($1, $2, 'imessage', $3, 'pending', 'eligible')",
                            uid, turn_id, pmid)
    finally:
        await c.close()


@pytest.fixture()
def two_owners(clean_db):
    a, b = uuid4(), uuid4()
    _run(_seed((a, [f"can you email {SECRET}", "make it heartfelt"], True),
               (b, ["hey bruce what's on friday"], True)))
    return a, b


async def _as_worker(fn):
    """Run `fn(conn)` on a bruce_app connection carrying the transaction-local worker context — which is
    exactly what `db.worker_session` gives the drain, and the only context the function will answer."""
    c = await _app_conn()
    try:
        async with c.transaction():
            await c.execute("SELECT set_config('app.worker', 'on', true)")
            return await fn(c)
    finally:
        await c.close()


LO, HI = "now() - interval '1 hour'", "now() - interval '60 seconds'"


def _reconcile() -> asyncpg.Record:
    """One reconciliation, as the worker really performs it."""
    async def _call(c):
        return await c.fetchrow(f"SELECT * FROM public.{FN}({LO}, {HI})")

    return _run(_as_worker(_call))


# --- 1 + 2: the worker role has no read of the ledger --------------------------------------------------


def test_the_worker_role_cannot_select_turn_text_globally(two_owners):
    """THE FIRST HALF OF THE INCIDENT, asserted directly. A worker session asking for the message bodies
    gets nothing — not a filtered subset, not an error it could catch and route around: zero rows,
    because the policy that used to admit it is gone."""
    async def _read(c):
        return await c.fetch(f"SELECT text FROM {TURNS}")

    rows = _run(_as_worker(_read))
    assert rows == [], (
        "a worker session read message text out of conversation_turns. That is the row grant this "
        "boundary removed, and every column of every student's turn came with it")


def test_the_worker_role_cannot_enumerate_conversation_turn_owners(two_owners):
    """THE SECOND HALF. Not just `text` — WHO. Counting ids, listing user ids, reading timestamps: a
    worker sees nothing in this table, which is what makes the aggregate function the only cross-tenant
    path and leaves it nothing to loop."""
    async def _enumerate(c):
        return (await c.fetchval(f"SELECT count(*) FROM {TURNS}"),
                await c.fetch(f"SELECT DISTINCT user_id FROM {TURNS}"),
                await c.fetch(f"SELECT id, created_at FROM {TURNS} ORDER BY created_at LIMIT 5"))

    n, owners, sample = _run(_as_worker(_enumerate))
    assert n == 0 and owners == [] and sample == [], (
        f"a worker session enumerated conversation_turns ({n} rows, {len(owners)} owners) — the "
        "reconciliation is supposed to COUNT through a function, never to walk the ledger")


# --- 3 + 4: nothing that comes back can name anyone ---------------------------------------------------


def test_the_declared_result_type_has_no_user_id_and_no_turn_id(clean_db):
    """THE PRIVACY CONTRACT AS THE DATABASE HOLDS IT, checked before anything is ever called.

    A policy could not express a column list and a function can; the column list IS the contract. The
    previous function's list contained `user_id`, and that single column is what the worker turned into
    a per-tenant read of every turn in the window. So the assertion is not merely "no message text": it
    is that NO IDENTIFIER OF ANY KIND leaves this function — no uuid column at all, of any name.
    """
    declared = _return_columns()
    assert declared == ALLOWED_RETURN_COLUMNS, (
        f"the function's result type is {declared!r}. It may return the window's bounds, the watermark, "
        "the agreed counts and a verdict — nothing else. A column added here is a column the "
        "reconciliation exports on every worker wake, into a log line and a response body")
    assert "user_id" not in declared, (
        "the aggregate returns user_id again. That column is not a privacy detail, it is the whole "
        "enumeration: a worker holding the owner list can open a session as each of them")
    assert not [c for c in declared if c.endswith("turn_id")], "a turn id is back in the result type"
    assert not [c for c, t in declared.items() if t == "uuid"], (
        f"the result type carries a uuid column ({declared!r}) — every identifier in this schema is one, "
        "so 'no uuid' is the only form of this rule that cannot be worked around by renaming")


def test_no_value_in_the_result_can_be_turned_back_into_an_owner(two_owners):
    """THE SAME PROPERTY, ASSERTED ON THE DATA. A declared type can be right while the values are wrong —
    a uuid rendered into a text column, an id smuggled into a status label. The worker gets numbers, the
    window's three timestamps and one word; nothing here is anybody's identity."""
    a, b = two_owners
    row = _reconcile()
    assert row is not None, "the worker could not obtain a reconciliation at all"

    blob = " ".join(str(v) for v in row.values())
    assert str(a) not in blob and str(b) not in blob, (
        "an owner id came back from the reconciliation. The worker is not entitled to know who is in the "
        "sample — that list is exactly what let it read every tenant's turns one at a time")
    assert not _UUID_RE.search(blob), (
        f"a uuid appeared in the reconciliation result ({blob!r}). Any identifier is an enumeration "
        "surface; the check is the SHAPE, so it cannot be evaded by choosing a different column name")
    assert row["reconciliation_status"] in (semantic_shadow.RECON_CLEAN, semantic_shadow.RECON_FAILED)


def test_the_result_carries_no_message_content(two_owners):
    """The older half of the contract, still asserted. The block travels into a log line and into a Cloud
    Tasks response body, both of which go further than the row does."""
    blob = " ".join(str(v) for v in _reconcile().values()).lower()
    for fragment in ("alvarez", "retake", "tuesday", "@school.edu", "heartfelt", "friday", "+1555",
                     "imessage"):
        assert fragment not in blob, (
            f"{fragment!r} came back from the reconciliation — it returns metadata about turns, never "
            "any part of one")


# --- 5: who may call it -------------------------------------------------------------------------------


def test_the_worker_can_execute_it_and_only_with_a_worker_context(two_owners):
    """The one power that replaced the grant. It is granted to the app role, and the function itself
    additionally demands a worker CONTEXT — Bruce has one non-owner role, so the API container holds the
    same credentials as the drain and only the transaction-local `app.worker` tells them apart."""
    assert _reconcile() is not None, "the worker could not reconcile at all"

    async def _no_context():
        c = await _app_conn()
        try:
            return await c.fetch(f"SELECT * FROM public.{FN}({LO}, {HI})")
        finally:
            await c.close()

    with pytest.raises(pgerr.InsufficientPrivilegeError):
        _run(_no_context())


# --- 6 + 7 + 8: the reconciliation actually reconciles, across tenants, without naming them -----------


def test_a_student_with_zero_shadow_rows_still_produces_a_gap(clean_db):
    """THE BUG THIS WHOLE AREA EXISTS FOR, now measured entirely inside the function.

    This student's intake call never happened: trusted turns on the canonical ledger and ZERO shadow rows
    of any kind. They appear in no claimed job, no completed job and no disposition — every source the
    old population was built from — and nothing outside the definer knows they exist. The gap must still
    be counted, and it must be counted by an ANTI-JOIN rather than by `turns - eligible - excluded`.
    """
    missing = uuid4()
    _run(_seed((missing, ["can u email alvarez a thank you note", "make it heartfelt"], False)))

    row = _reconcile()
    assert row["canonical_trusted_turns"] == 2
    assert row["trusted_turns_without_intake"] == 2, (
        "the anti-join found no missing intake for a student who has none at all")
    assert row["trusted_turns_with_intake"] == 0 and row["shadow_jobs"] == 0
    assert row["unobserved_turns"] == 2
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "two trusted turns with no shadow row of any kind reconciled cleanly — the exact state the whole "
        "continuation population was in, undetected, for three rounds")


def test_two_different_students_missing_intake_aggregate_without_naming_either(clean_db):
    """THE CROSS-TENANT COUNT IS REAL, AND IT IS STILL ONLY A COUNT.

    A cross-tenant count that RLS has filtered to one tenant is worse than no count: it is a partial
    report wearing a complete one's clothes. The definer's BYPASSRLS exists for this line. What must NOT
    come with it is any way to tell the two students apart, or to learn that there were two.
    """
    one, two, fine = uuid4(), uuid4(), uuid4()
    _run(_seed((one, ["did mr patel post the retake"], False),
               (two, ["can you move tutoring to thursday", "and tell coach"], False),
               (fine, ["hey bruce"], True)))

    row = _reconcile()
    assert row["canonical_trusted_turns"] == 4, (
        "the reconciliation saw fewer turns than the ledger holds — a count RLS filtered to one tenant "
        "is indistinguishable from a quiet hour, which is the failure this function exists to end")
    assert row["unobserved_turns"] == 3 and row["trusted_turns_without_intake"] == 3
    assert row["trusted_turns_with_intake"] == 1 and row["eligible_turns"] == 1
    assert row["pending_jobs"] == 1
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED

    blob = " ".join(str(v) for v in row.values())
    for uid in (one, two, fine):
        assert str(uid) not in blob
    assert not _UUID_RE.search(blob), (
        "three affected tenants were correctly aggregated and one of them was named. The number is the "
        "product; the roster is not")


def test_two_students_missing_intake_aggregate_to_exactly_two_without_naming_either(clean_db):
    """THE CROSS-TENANT SUM IS A SUM, AND IT IS STILL ONLY A NUMBER.

    Two students, one unobserved turn each. The answer must be exactly 2, and each way it could be wrong
    is a distinct failure this boundary is built to prevent:

      1  the count was silently filtered to ONE tenant. The definer's owner holds BYPASSRLS for this
         single line; without it a cross-tenant aggregate reports one student's half of the problem as
         the whole of it, and "half the gap" is indistinguishable from "a smaller gap".
      0  the population came from the shadow bookkeeping, which neither of these students appears in.

    And the 2 arrives with nothing attached to it. Not the ids, not a uuid in any column under any name,
    and not a column outside the agreed contract — an `affected_owners` or a `first_turn_at` would each
    be a fresh correlation handle on a row that travels into a log line.
    """
    one, two = uuid4(), uuid4()
    _run(_seed((one, ["did mr patel post the retake"], False),
               (two, ["can you move tutoring to thursday"], False)))

    row = _reconcile()
    assert row["canonical_trusted_turns"] == 2, (
        f"the reconciliation saw {row['canonical_trusted_turns']} of the 2 turns two different students "
        "wrote — a count RLS has filtered to one tenant is a partial report wearing a complete one's "
        "clothes")
    assert row["unobserved_turns"] == 2 and row["trusted_turns_without_intake"] == 2, (
        "two students whose intake never ran produced a gap of "
        f"{row['unobserved_turns']} — neither of them owns a shadow row, so any population built from "
        "the shadow tables contains neither and reports zero")
    assert row["trusted_turns_with_intake"] == 0 and row["shadow_jobs"] == 0
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED

    blob = " ".join(str(v) for v in row.values())
    assert str(one) not in blob and str(two) not in blob, (
        "the aggregate named one of the two affected students. The number is the product; the roster is "
        "the enumeration, and a worker holding it can open a session as each of them")
    assert not _UUID_RE.search(blob), (
        f"a uuid came back beside the count ({blob!r}) — the rule is the SHAPE, so it cannot be evaded "
        "by choosing a column name that does not read as an identifier")
    assert set(row.keys()) == set(ALLOWED_RETURN_COLUMNS), (
        f"the reconciliation returned {sorted(row.keys())}. Two affected students were correctly summed "
        "and something else rode along on the row that says so")


def test_an_empty_canonical_window_reports_clean(clean_db):
    """No traffic in the window is genuinely clean, and every count is an honest zero.

    This state is only allowed to be `clean` because the caller separately established that the function
    exists, is hardened and is executable — see the runtime guard. Absent that ordering it is the exact
    emission an invisible ledger produces, which is the original sin of this whole module.
    """
    row = _reconcile()
    assert row["canonical_trusted_turns"] == 0
    assert row["unobserved_turns"] == 0 and row["trusted_turns_without_intake"] == 0
    assert row["reconciliation_status"] == semantic_shadow.RECON_CLEAN


def test_an_excluded_turn_is_accounted_for_and_a_dropped_one_is_not(clean_db):
    """The distinction the exclusion status exists for, over the canonical join.

    A turn deliberately not observed leaves a row saying so and is ACCOUNTED FOR. A turn nobody wrote
    anything about is a GAP. If those two looked the same the check could not do the only thing it does.
    """
    uid = uuid4()
    _run(_seed((uid, ["/status"], False)))
    _owner_exec(
        "DO $$ DECLARE t uuid; u uuid; BEGIN "
        f"  SELECT id, user_id INTO t, u FROM {TURNS} WHERE role = 'user' LIMIT 1; "
        f"  INSERT INTO {JOBS} (user_id, conversation_turn_id, channel, provider_message_id, status, "
        "                      intake_disposition) "
        "  VALUES (u, t, 'imessage', 'pm-slash', 'excluded', 'excluded_platform_command'); END $$")

    row = _reconcile()
    assert row["explicitly_excluded_turns"] == 1
    assert row["unobserved_turns"] == 0 and row["eligible_turns"] == 0
    assert row["reconciliation_status"] == semantic_shadow.RECON_CLEAN

    _run(_seed((uuid4(), ["and this one nobody offered"], False)))
    row = _reconcile()
    assert row["explicitly_excluded_turns"] == 1 and row["unobserved_turns"] == 1
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "a turn with no row of any kind was folded in with the ones deliberately excluded")


def test_an_eligible_intake_with_no_job_in_any_queue_state_is_a_gap(two_owners):
    """ANTI-JOIN B, AND THE REASON THERE ARE TWO OF THEM.

    Here every trusted turn HAS an intake disposition, so the first anti-join finds nothing and the old
    equation `turns == eligible + excluded` balances perfectly. But one of those rows sits in a status no
    worker will ever claim and no terminal state accounts for — a half-applied migration, a hand-edited
    row, the class `_tally.unaccounted` was added to name. Its turn is not observed and never will be.

    Two totals that happen to match is not a reconciliation. This is the case that proves it.
    """
    _owner_exec(f"UPDATE {JOBS} SET status = 'quarantined' "
                f"WHERE id = (SELECT id FROM {JOBS} ORDER BY created_at LIMIT 1)")
    row = _reconcile()

    assert row["trusted_turns_without_intake"] == 0, "this fixture is only interesting with intake present"
    assert (row["canonical_trusted_turns"]
            == row["eligible_turns"] + row["explicitly_excluded_turns"]), (
        "the fixture no longer balances under the old equation, so it no longer discriminates")
    assert row["unobserved_turns"] == 1, (
        "an eligible intake holding no shadow job in any queue state was counted as observed. The gap "
        "was derived by subtracting two totals that agree, which is the defect being closed")
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED


def test_an_eligible_intake_parked_in_the_excluded_status_is_a_gap(two_owners):
    """ANTI-JOIN B ACROSS TWO COLUMNS, WHICH IS THE ONLY THING THAT MAKES IT AN ANTI-JOIN.

    `intake_disposition` is what INTAKE decided about the turn. `status` is what the QUEUE holds. Taking
    BOTH sides of "an eligible intake still has its job" from `status` alone is this module's own bug one
    column deep: flip a single row to `excluded` and the turn simply moves from `eligible_turns` into
    `explicitly_excluded_turns`, every total still balances, and a turn no worker will ever claim reports
    `clean`.

    And it reports clean while carrying NO typed exclusion reason — `intake_disposition` still says
    `eligible`. An exclusion that leaves no reason behind is indistinguishable from a dropped turn, which
    is the single thing the excluded status was added to prevent.
    """
    _owner_exec(f"UPDATE {JOBS} SET status = 'excluded' "
                f"WHERE id = (SELECT id FROM {JOBS} ORDER BY created_at LIMIT 1)")
    row = _reconcile()

    assert row["trusted_turns_without_intake"] == 0, "this fixture is only interesting with intake present"
    assert row["explicitly_excluded_turns"] == 0, (
        "a row whose intake_disposition is 'eligible' was counted as deliberately excluded. Nothing on it "
        "says why it would be excluded, because intake never excluded it")
    assert row["eligible_turns"] == row["canonical_trusted_turns"], (
        "intake called every one of these turns eligible, so every one of them belongs in eligible_turns "
        "however the queue has since been edited")
    assert row["unobserved_turns"] == 1, (
        "an eligible intake parked in a status no worker claims was counted as observed, because the "
        "eligible side and the has-a-job side were read from the same column")
    assert row["reconciliation_status"] == semantic_shadow.RECON_FAILED


# --- 9 + 10 + 11: the boundary is a boundary ----------------------------------------------------------


def test_execute_on_the_function_grants_no_table_access(two_owners):
    """The two are unrelated privileges and must stay that way. Having just used the function
    successfully in the same session, the same role still cannot touch the tables it read."""
    async def _both(c):
        counted = await c.fetchrow(f"SELECT * FROM public.{FN}({LO}, {HI})")
        turns = await c.fetch(f"SELECT id, user_id, text FROM {TURNS}")
        return counted, turns

    counted, turns = _run(_as_worker(_both))
    assert counted is not None, "the function did not answer, so this proves nothing about the table"
    assert turns == [], (
        "EXECUTE on the aggregate came with a readable table. The function is the boundary; if the table "
        "is readable beside it, the boundary is decorative")


def test_public_cannot_execute_the_function():
    """A function is EXECUTE-to-PUBLIC by default in Postgres. A privilege that arrives by default is a
    privilege nobody decided to grant, so the migration revokes it — and this is the check that the
    revoke actually happened rather than being written down."""
    row = _owner_fetch(
        "SELECT has_function_privilege('public', p.oid, 'EXECUTE') AS pub, "
        "       has_function_privilege($2, p.oid, 'EXECUTE') AS app "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = $1", FN, APP_ROLE)
    assert row, f"{FN} does not exist"
    assert row[0]["pub"] is False, (
        "PUBLIC can execute the reconciliation — every role in the cluster, present and future, holds "
        "the cross-tenant read")
    assert row[0]["app"] is True, "the app role lost EXECUTE, so the worker cannot reconcile at all"


def test_the_retired_per_owner_function_is_gone():
    """NO SUCH FUNCTION IS EVER BUILT, which is a stronger fact than one that is built and then dropped.

    The undeployed chain that introduced `shadow_turn_population` was collapsed before it reached any
    database: the shipped revisions create the aggregate and nothing else, so there is no window — not
    even one migration wide — in which a per-owner function exists for something to be pointed at it.
    This asserts the RESULT in the catalog rather than the absence of a statement in a file, because the
    property that matters is that the app role cannot reach a function returning one row per owner —
    however it might get there.
    """
    rows = _owner_fetch(
        "SELECT p.oid, has_function_privilege($2, p.oid, 'EXECUTE') AS app "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = $1", RETIRED_FN, APP_ROLE)
    assert rows == [], (
        "a per-owner population function exists. It hands the worker every owner in the window, which is "
        "the enumeration this whole boundary exists to remove")


def test_the_owner_holds_select_on_only_the_two_tables_it_reads():
    """LEAST PRIVILEGE FOR THE DEFINER'S OWNER, which is a principal like any other.

    It reads the canonical ledger and the shadow bookkeeping, so it holds SELECT on those two and no
    others. Giving it the blanket grant `bruce_app` has would make the function's return type the only
    thing standing between a bug and the whole schema.
    """
    granted = {r["table_name"] for r in _owner_fetch(
        "SELECT table_name FROM information_schema.table_privileges "
        "WHERE table_schema = 'public' AND grantee = $1 AND privilege_type = 'SELECT'", RECON_OWNER)}
    assert granted == {TURNS, JOBS}, (
        f"{RECON_OWNER} holds SELECT on {sorted(granted)}. It is a BYPASSRLS role: every table on that "
        "list is a table it can read across every tenant")

    role = _owner_fetch(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = $1", RECON_OWNER)[0]
    assert role["rolcanlogin"] is False, "the definer's owner can log in"
    assert role["rolsuper"] is False, "the definer's owner is a superuser, so its grants mean nothing"
    assert role["rolcreatedb"] is False and role["rolcreaterole"] is False
    assert role["rolbypassrls"] is True, (
        "without BYPASSRLS the cross-tenant count is silently filtered to nothing, which is the "
        "'empty looks like invisible' failure this whole area exists to end")


# --- the hardening gate, and the mutations that must break it -----------------------------------------


def _return_columns() -> dict[str, str]:
    """The function's declared OUT columns, from pg_proc. This is the privacy contract as the database
    holds it, not as a docstring describes it."""
    rows = _owner_fetch(
        "SELECT a.name, format_type(a.typ, NULL) AS type, a.mode::text AS mode "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "     LATERAL unnest(p.proargnames, p.proallargtypes, p.proargmodes) "
        "             AS a(name, typ, mode) "
        "WHERE n.nspname = 'public' AND p.proname = $1", FN)
    return {r["name"]: r["type"] for r in rows if r["mode"] in ("t", "o")}


def _hardening_violations() -> list[str]:
    """Every way the cross-tenant read path can stop being an aggregate-only one, read from the catalog.

    This is the same set of clauses `semantic_shadow._population_refusal` applies at runtime — asserted
    here against the database directly so a regression fails as a NAMED privilege defect and not merely
    as a reconciliation that went quiet.
    """
    bad: list[str] = []
    rows = _owner_fetch(
        "SELECT p.prosecdef, coalesce(array_to_string(p.proconfig, ','), '') AS cfg, "
        "       r.rolname AS owner, r.rolbypassrls, r.rolcanlogin, r.rolsuper, "
        "       has_function_privilege('public', p.oid, 'EXECUTE') AS pub_exec "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_roles r ON r.oid = p.proowner "
        "WHERE n.nspname = 'public' AND p.proname = $1", FN)
    if not rows:
        return ["the reconciliation function does not exist"]
    f = rows[0]
    declared = _return_columns()
    if not f["prosecdef"]:
        bad.append("not SECURITY DEFINER, so it cannot see across tenants at all")
    if "search_path=" not in f["cfg"]:
        bad.append("no fixed search_path — a definer running the CALLER's path is a privilege escalation")
    if f["owner"] in (APP_ROLE, "public"):
        bad.append(f"owned by {f['owner']}, which is the role the application already runs as")
    if not f["rolbypassrls"]:
        bad.append("the owner has no BYPASSRLS, so the count is silently filtered to nothing")
    if f["rolcanlogin"]:
        bad.append(f"the owner {f['owner']} can log in")
    if f["rolsuper"]:
        bad.append(f"the owner {f['owner']} is a superuser")
    if f["pub_exec"]:
        bad.append("PUBLIC holds EXECUTE")
    # BY COLUMN NAME, not by substring of the rendered result type. `reconciliation_status text` contains
    # the string "text" and is perfectly legitimate; a substring check would call the shipped function a
    # leak and would then be relaxed, which is how a real check gets turned off.
    for column in CONTENT_COLUMNS:
        if column in declared:
            bad.append(f"the result type exposes {column!r}, which is not aggregate metadata")
    for column, typ in declared.items():
        if typ == "uuid":
            bad.append(f"the result type exposes {column!r} (uuid) — any identifier is an enumeration")
    if set(declared) != set(ALLOWED_RETURN_COLUMNS):
        bad.append(f"the result columns are {sorted(declared)}, not the agreed contract")
    return bad


def _privilege_violations() -> list[str]:
    """Whether anything has re-granted the worker a way to reach rows or owners of the turn ledger."""
    rows = _owner_fetch(
        "SELECT policyname, cmd, coalesce(qual, '') AS qual FROM pg_policies "
        "WHERE schemaname = 'public' AND tablename = $1", TURNS)
    bad = [f"{r['policyname']} ({r['cmd']}) admits a worker session: {r['qual']}"
           for r in rows if r["cmd"] in ("SELECT", "ALL") and "app_is_worker" in r["qual"]]
    grantees = _owner_fetch(
        "SELECT grantee, privilege_type FROM information_schema.table_privileges "
        "WHERE table_schema = 'public' AND table_name = $1 AND privilege_type = 'SELECT'", TURNS)
    for g in grantees:
        if g["grantee"] not in (RECON_OWNER, APP_ROLE) and g["grantee"] != _owner_name():
            bad.append(f"{g['grantee']} holds SELECT on {TURNS}")
    # AND THE ENUMERATION PATH ITSELF. A per-owner function the app role can execute is a privilege
    # defect even while the aggregate beside it is perfect, and it is not visible in any policy.
    for r in _owner_fetch(
            "SELECT has_function_privilege($2, p.oid, 'EXECUTE') AS app FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = $1", RETIRED_FN, APP_ROLE):
        bad.append(f"{RETIRED_FN} exists (app EXECUTE={r['app']}) and returns one row per owner")
    return bad


def _owner_name() -> str:
    return str(_url("BRUCE_DATABASE_URL").username)


def test_the_hardening_and_privilege_gates_pass_as_shipped(clean_db):
    """The gates, asserted green before anything is mutated. Without this the mutation tests below would
    each prove only that SOMETHING is wrong."""
    assert _hardening_violations() == []
    assert _privilege_violations() == []


def test_a_function_that_returns_message_text_fails_the_privacy_contract(clean_db):
    """MUTATION. The whole argument for a function over a policy is that a function names its columns. If
    someone adds `text` to the return type — to debug, to enrich a dashboard, because it was easy — the
    contract has to be what stops them, and a contract nobody has watched fail is not one."""
    original = _function_definition()
    try:
        _owner_exec(f"DROP FUNCTION {FN_SIG}", _leaky("text text", "pg_catalog.max(ct.text)"))
        violations = _hardening_violations()
    finally:
        # RESTORED FROM INSIDE THE TRY'S FINALLY, not after a bare install. A mutation that fails to
        # install has already dropped the real function, and a restore that only runs on the happy path
        # would leave every later test in this file measuring a database with no reconciliation in it.
        _owner_exec(f"DROP FUNCTION IF EXISTS {FN_SIG}", original, *_regrant())

    assert any("exposes 'text'" in v for v in violations), (
        f"the function was changed to return message text and the privacy contract passed: "
        f"{violations!r}. The column list IS the contract")
    assert _hardening_violations() == [], "the restore failed; later tests would be meaningless"


def test_a_function_that_returns_user_ids_fails_the_privacy_contract(clean_db):
    """MUTATION, AND IT IS THE INCIDENT ITSELF IN ITS SECOND FORM.

    This is precisely what the per-owner definer had: no message text anywhere, every column defensible on
    its own, and one `user_id` — which the worker turned into a per-tenant read of every turn in the
    window. The contract has to reject an identifier as firmly as it rejects a sentence.
    """
    original = _function_definition()
    try:
        # `max()` has no uuid overload, so the id is aggregated as text and cast back — the point is the
        # COLUMN's type in the result, which is what the contract reads.
        _owner_exec(f"DROP FUNCTION {FN_SIG}",
                    _leaky("user_id uuid", "pg_catalog.max(ct.user_id::text)::uuid"))
        violations = _hardening_violations()
        runtime = _run(_population_refusal_now())
    finally:
        _owner_exec(f"DROP FUNCTION IF EXISTS {FN_SIG}", original, *_regrant())

    assert any("exposes 'user_id'" in v for v in violations), (
        f"the aggregate was changed to hand back an owner id and the privacy contract passed: "
        f"{violations!r}. That column is not a leak of degree, it is the enumeration")
    assert runtime is not None, (
        "the runtime reconciled over a function whose result type had changed shape entirely")
    assert _hardening_violations() == [], "the restore failed; later tests would be meaningless"


def test_a_function_without_a_fixed_search_path_fails_the_security_gate(clean_db):
    """MUTATION. `SET search_path` is not tidiness on a SECURITY DEFINER function: without it the definer
    resolves `conversation_turns` through the CALLER's path while running as a BYPASSRLS role, so anyone
    who can create a schema chooses which table it reads."""
    original = _function_definition()
    _owner_exec(f"ALTER FUNCTION {FN_SIG} RESET search_path")
    try:
        violations = _hardening_violations()
        runtime = _run(_population_refusal_now())
    finally:
        _owner_exec(f"DROP FUNCTION IF EXISTS {FN_SIG}", original, *_regrant())

    assert any("search_path" in v for v in violations), (
        f"the fixed search_path was removed and the security gate passed: {violations!r}")
    assert runtime == semantic_shadow.FUNCTION_NOT_HARDENED, (
        f"the runtime reconciled anyway (reason={runtime!r}). A definer with an unpinned path must be "
        "refused, not used")
    assert _hardening_violations() == [], "the restore failed; later tests would be meaningless"


def test_restoring_the_broad_worker_select_policy_fails_the_privilege_test(clean_db):
    """MUTATION. The first half of the incident, put back exactly as the row-grant design wrote it. The
    privilege gate must name it — because with it in place every number the reconciliation reports would
    still be correct, and correct numbers are why it survived the first time."""
    _owner_exec(f"CREATE POLICY {WORKER_POLICY} ON {TURNS} FOR SELECT USING (app_is_worker())")
    try:
        violations = _privilege_violations()
        runtime = _run(_population_refusal_now())
    finally:
        _owner_exec(f"DROP POLICY IF EXISTS {WORKER_POLICY} ON {TURNS}")

    assert any(WORKER_POLICY in v for v in violations), (
        f"the global worker SELECT policy was restored on {TURNS} and the privilege test passed: "
        f"{violations!r}. RLS cannot narrow a row to a column, so that policy is a read of every "
        "student's message text in exchange for a count")
    assert runtime == semantic_shadow.LEDGER_ROW_GRANT_PRESENT, (
        f"the runtime kept reconciling over the restored row grant (reason={runtime!r})")
    assert _privilege_violations() == [], "the restore failed; later tests would be meaningless"


def test_restoring_the_per_owner_population_function_fails_the_privilege_test(clean_db):
    """MUTATION. The SECOND half of the incident, put back the way it would really come back: not by
    reverting a migration, but by someone recreating the helper because a dashboard wanted a per-user
    breakdown. The aggregate beside it stays perfect and every number stays correct, which is exactly why
    nothing would notice — so the privilege gate names it and the runtime refuses to reconcile over it."""
    _owner_exec(_RETIRED_FUNCTION_SQL,
                f"REVOKE ALL ON FUNCTION {RETIRED_SIG} FROM PUBLIC",
                f"GRANT EXECUTE ON FUNCTION {RETIRED_SIG} TO {APP_ROLE}")
    try:
        violations = _privilege_violations()
        runtime = _run(_population_refusal_now())
    finally:
        _owner_exec(f"DROP FUNCTION IF EXISTS {RETIRED_SIG}")

    assert any(RETIRED_FN in v for v in violations), (
        f"the per-owner population function was restored and the privilege test passed: {violations!r}. "
        "One row per owner is the enumeration; the worker needs no more than that to open a session as "
        "each of them")
    assert runtime == semantic_shadow.OWNER_ENUMERATION_PATH_PRESENT, (
        f"the runtime reconciled happily beside a restored owner-enumeration path (reason={runtime!r})")
    assert _privilege_violations() == [], "the restore failed; later tests would be meaningless"


# --- the machinery the mutations need ------------------------------------------------------------------


def _function_definition() -> str:
    return str(_owner_fetch(
        "SELECT pg_get_functiondef(p.oid) AS def FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = $1", FN)[0]["def"])


def _regrant() -> tuple[str, ...]:
    """`pg_get_functiondef` reproduces the body, not the ownership or the grants. Restoring without these
    would leave the suite in a state the mutation tests themselves would then fail on."""
    return (f"ALTER FUNCTION {FN_SIG} OWNER TO {RECON_OWNER}",
            f"REVOKE ALL ON FUNCTION {FN_SIG} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION {FN_SIG} TO {APP_ROLE}")


async def _population_refusal_now() -> str | None:
    """What the RUNTIME does with the mutated database — the behavioural half of each mutation. It must
    refuse and name the reason, rather than quietly producing numbers over a broken boundary."""
    import bruce_engine.db as db

    db._engine = None
    db._sessionmaker = None
    try:
        report = await semantic_shadow.PostgresShadowStore().reconcile_window()
        return report.unreadable_reason if not report.visible else None
    finally:
        if db._engine is not None:
            await db._engine.dispose()
        db._engine = None
        db._sessionmaker = None


def _leaky(extra_column: str, extra_expression: str) -> str:
    """A deliberately leaky version of the function: identical in shape except that it hands back one
    thing it must not. Parameterised because there are two ways to break the contract and both have
    happened — a sentence (the row grant's policy) and an identifier (the per-owner function's return
    type)."""
    return f"""
CREATE FUNCTION public.{FN}(p_window_start timestamptz, p_window_end timestamptz)
RETURNS TABLE (window_start timestamptz, window_end timestamptz, watermark timestamptz,
               canonical_trusted_turns bigint, trusted_turns_with_intake bigint,
               trusted_turns_without_intake bigint, explicitly_excluded_turns bigint,
               eligible_turns bigint, shadow_jobs bigint, pending_jobs bigint, processing_jobs bigint,
               retryable_jobs bigint, terminal_jobs bigint, unobserved_turns bigint,
               reconciliation_status text, {extra_column})
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $leaky$
    SELECT p_window_start, p_window_end, pg_catalog.now(),
           pg_catalog.count(*), 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint,
           0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 'clean'::text,
           {extra_expression}
      FROM public.conversation_turns ct
     WHERE ct.role = 'user' AND ct.created_at >= p_window_start AND ct.created_at < p_window_end
$leaky$;
"""


# The per-owner function, verbatim in shape: aggregate, no message text, ONE ROW PER OWNER. It exists to be
# REJECTED. Nothing imports it, and the test that installs it drops it again.
_RETIRED_FUNCTION_SQL = f"""
CREATE FUNCTION public.{RETIRED_FN}(p_window_start timestamptz, p_window_end timestamptz)
RETURNS TABLE (user_id uuid, trusted_turn_count bigint, min_turn_id uuid, min_turn_at timestamptz,
               max_turn_id uuid, max_turn_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $retired$
    SELECT ct.user_id, pg_catalog.count(*)::bigint, NULL::uuid, pg_catalog.min(ct.created_at),
           NULL::uuid, pg_catalog.max(ct.created_at)
      FROM public.conversation_turns ct
     WHERE ct.role = 'user' AND ct.created_at >= p_window_start AND ct.created_at < p_window_end
     GROUP BY ct.user_id
$retired$;
"""
