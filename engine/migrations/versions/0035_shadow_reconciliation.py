"""The reconciliation is an AGGREGATE-ONLY function. It returns numbers; no owner ids ever leave it.

WHAT THE RECONCILIATION HAS TO DO. Shadow bookkeeping cannot be checked against itself. `worker_api`
once built its reconciliation population out of the jobs `claim_and_observe` had returned — the only user
ids it could get — so a user whose intake call was MISSING had no job, was never in the list, was never
reconciled, and an empty queue reported `reconciled: True` on every single wake. A check whose POPULATION
comes from the thing being checked cannot fail. The denominator has to come from `conversation_turns`,
the canonical ledger, which shadow does not write.

WHY THAT COUNT IS A FUNCTION AND NOT A POLICY, AND WHY THE JOIN IS INSIDE IT. Two incidents, in order,
both about what it costs to compute that number:

  1. The obvious way to let a worker count turns it cannot see is
     `CREATE POLICY ... ON conversation_turns FOR SELECT USING (app_is_worker())`. RLS policies are
     ROW-level. They admit or deny a ROW; they cannot narrow a row to a column. So the price of a count
     is SELECT on EVERY COLUMN of EVERY user's turn for any session that can set `app.worker` —
     including `text`, which holds the student's own words, the people they name, the addresses they use.
     `semantic_shadow.PostgresShadowStore.trusted_text` already contains
     `SELECT text FROM conversation_turns`: the read path was one query away and needed neither a new
     grant nor a code review to reach. THAT POLICY MUST NEVER EXIST, and this chain never creates it.

  2. A definer function that returns ONE ROW PER OWNER closes the column exposure and widens ENUMERATION
     instead. An adversarial audit EXECUTED that shape: given the user_id list, the worker looped the
     owners, opened `user_session(user_id)` per owner, and read every turn row in the window — one
     authorized tenant at a time. Before it, the worker learned an owner id only from a job it had
     CLAIMED; after it, it received every owner present in the window, including owners with no jobs at
     all. Least privilege is not "each read was authorized", it is "the read was never available".

So the COMPARISON happens inside the definer, where both tables already are, and only the verdict leaves.
`public.shadow_reconciliation(window_start, window_end)` returns ONE ROW OF NUMBERS: no user ids, no turn
ids, no per-user rows, no text, addresses, handles, drafts or raw slots. The worker learns whether the
sample is complete and nothing whatsoever about who is in it, so there is no list to loop and no
per-tenant read to open.

TWO ANTI-JOINS, NEVER TWO TOTALS. `unobserved_turns` is NOT `turns - eligible - excluded`. Two totals
that happen to agree is the failure this area has produced three times over: `eligible = sum(counts)`,
then `intake.reconciled`, then the population selection — each an equation whose two sides came from the
same place. The gap is counted DIRECTLY from the canonical side as a disjunction of two anti-join
predicates over the turns themselves:

  A. a trusted turn for which NO intake disposition row exists at all — a turn nobody wrote anything about
  B. a trusted turn whose intake claimed ELIGIBLE but which holds no shadow job in any known queue state
     — a half-applied migration, a hand-edited row

Both are evaluated row by row against the canonical ledger. Neither can be satisfied by arithmetic, and
cancellation is not expressible: a subtraction can come out zero, a `NOT EXISTS` cannot.

`reconciliation_status` IS DECIDED HERE, in the same statement and the same snapshot as the counts it
describes, and returned so the application cannot re-derive it. A status computed twice from the same
numbers agrees today and drifts the first time either copy is edited, and the drift is invisible because
both look reasonable.

LEAST PRIVILEGE, CONCRETELY:

  * SECURITY DEFINER, owned by `bruce_shadow_recon` — a dedicated NOLOGIN role whose ONLY reason to exist
    is that this aggregate genuinely crosses tenants. It has BYPASSRLS because a canonical count that RLS
    has filtered to nothing is indistinguishable from a quiet hour, and "empty looks like invisible" is
    the failure this whole area exists to end. It cannot log in, it is not a superuser, and it holds
    SELECT on exactly the two tables this function reads and nothing else in the schema.
  * the WORKER (`bruce_app`) gets NO BYPASSRLS and NO table grant on `conversation_turns`. It gets
    EXECUTE on this function, and that is the whole of the new authority. Its only remaining read of
    message text is the existing per-job path: one CLAIMED job, under `user_session(job.user_id)`, for
    the one turn that job is about. That read is authorized, narrow, and untouched here.
  * REVOKE ALL FROM PUBLIC, because a function is EXECUTE-to-PUBLIC by default and a privilege that
    arrives by default is a privilege nobody decided to give.
  * `SET search_path = pg_catalog, public` pinned on the function, every table and function inside it
    schema-qualified. A SECURITY DEFINER function running the CALLER's path as a BYPASSRLS role lets
    anyone who can create a schema choose which `conversation_turns` it reads. That is the classic
    privilege-escalation hole, not a lint.
  * NO dynamic SQL, and no identifier ever comes from a parameter. The only parameters are the two window
    bounds, and they are VALIDATED (present, ordered, not in the future, no wider than seven days) so a
    caller cannot turn the reconciliation into a full-history export of the turn ledger.
  * it refuses outright unless `public.app_is_worker()` is true. Bruce has ONE non-owner role, so "only
    the worker may call this" cannot be said with a GRANT: the API container holds the same credentials
    as the drain, and only the transaction-local `app.worker` GUC that `db.worker_session` sets tells
    them apart.

THE WATERMARK IS THE CALLER'S. The bounds are parameters rather than computed inside, because the caller
decides one window per pass; a function that picked its own `now()` would describe a different population
on every call, which is how a real gap hides in a rounding difference.

THE INDEX. The comparison scans a fixed window of trusted inbound turns — role='user' inside
[watermark - window, watermark) — on every worker wake. Without an index that is a sequential scan of the
most-written table in the schema once a minute. It is created BEFORE the privilege gate below, because it
is plain schema and has nothing to do with whether a role can be provisioned.

WHERE THE ROLE CANNOT BE CREATED. Provisioning a BYPASSRLS role needs superuser (or CREATEROLE held by a
role that itself has BYPASSRLS). Cloud SQL's migration role, and the non-superuser owner
`test_migration_rls_context` deliberately reproduces, may have neither. Rather than fail the chain, this
migration SKIPS the function and says so — and the worker then reports `reconciliation_status: unknown`,
never `clean`, because `semantic_shadow` proves the function's existence and hardening from database
state before it believes a single number. A missing privilege degrades to "I cannot tell", which is the
answer the three-state design exists to keep available.

Revision ID: 0035_shadow_reconciliation
Revises: 0034_known_people_row_security
Create Date: 2026-08-08
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0035_shadow_reconciliation"
down_revision = "0034_known_people_row_security"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

TURNS = "conversation_turns"
JOBS = "semantic_shadow_jobs"
APP_ROLE = "bruce_app"
RECON_OWNER = "bruce_shadow_recon"

FUNCTION = "shadow_reconciliation"
FUNCTION_ARGS = "timestamptz, timestamptz"
FUNCTION_SIG = f"public.{FUNCTION}({FUNCTION_ARGS})"

POPULATION_INDEX = "ix_conversation_turns_role_created"

# The widest window the function will answer. Seven days is far beyond any reconciliation pass
# (BRUCE_SHADOW_RECONCILE_WINDOW_S defaults to one hour) and far short of "export the ledger".
MAX_WINDOW = "7 days"

# The shadow job vocabulary, spelled here so the SQL below is readable. These are literals in the
# generated function body — no identifier and no value in this function ever comes from a parameter.
#
# TWO COLUMNS, AND THEY SAY DIFFERENT THINGS. `intake_disposition` is what INTAKE decided about the turn
# (`eligible`, or a typed reason it was not offered). `status` is what the QUEUE currently holds. Both
# sides of "an eligible intake still has its job" must not be read off `status`, or the check agrees with
# itself one column deep — see the anti-join below.
D_ELIGIBLE = "eligible"                       # semantic_shadow.ELIGIBLE, the intake_disposition value
S_QUEUE = ("pending", "processing", "retryable_failed", "completed", "terminal_failed")
_QUEUE_LIST = ", ".join(f"'{s}'" for s in S_QUEUE)

# The three states, as the DATABASE names them. `unknown` is never produced here: this function either
# answers or raises, and "it raised" is the application's evidence for unknown.
R_CLEAN = "clean"
R_FAILED = "failed"

_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION public.{FUNCTION}(p_window_start timestamptz, p_window_end timestamptz)
RETURNS TABLE (window_start                 timestamptz,
               window_end                   timestamptz,
               watermark                    timestamptz,
               canonical_trusted_turns      bigint,
               trusted_turns_with_intake    bigint,
               trusted_turns_without_intake bigint,
               explicitly_excluded_turns    bigint,
               eligible_turns               bigint,
               shadow_jobs                  bigint,
               pending_jobs                 bigint,
               processing_jobs              bigint,
               retryable_jobs               bigint,
               terminal_jobs                bigint,
               unobserved_turns             bigint,
               reconciliation_status        text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $shadow_reconciliation$
BEGIN
    -- ONE NON-OWNER ROLE EXISTS, so "only the worker may call this" cannot be said with a GRANT alone:
    -- the API container holds the same role as the drain. The worker CONTEXT is the transaction-local
    -- app.worker GUC that only db.worker_session sets, so the refusal lives here, inside the definer.
    IF NOT public.app_is_worker() THEN
        RAISE EXCEPTION '{FUNCTION} requires a worker session'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- THE BOUNDS ARE THE ONLY PARAMETERS AND THEY ARE CHECKED. This function runs as a BYPASSRLS role;
    -- an unvalidated window is the difference between "reconcile the last hour" and "walk every turn
    -- ever written". None of these can be satisfied by a caller who is merely careless.
    IF p_window_start IS NULL OR p_window_end IS NULL THEN
        RAISE EXCEPTION 'reconciliation window bounds must both be provided'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_window_start >= p_window_end THEN
        RAISE EXCEPTION 'reconciliation window start must precede its end'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_window_end > pg_catalog.now() THEN
        RAISE EXCEPTION 'reconciliation watermark must not be in the future'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_window_end - p_window_start > INTERVAL '{MAX_WINDOW}' THEN
        RAISE EXCEPTION 'reconciliation window exceeds the {MAX_WINDOW} ceiling'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN QUERY
    -- THE CANONICAL POPULATION. `conversation_turns` role='user' is the one row every trusted inbound
    -- turn writes, and `conversation_store.persist_user_turn` is its only writer — which is precisely
    -- why the denominator comes from here and not from anything shadow maintains about itself.
    WITH t AS (
        SELECT ct.id AS turn_id
          FROM public.conversation_turns ct
         WHERE ct.role = 'user'
           AND ct.created_at >= p_window_start
           AND ct.created_at <  p_window_end
    ),
    -- THE JOIN, INSIDE THE BOUNDARY. This is the whole point: the comparison happens where both tables
    -- already are, so no owner id has to cross out to a caller that would then open a session per tenant
    -- to finish the job.
    --
    -- A LEFT JOIN, on the canonical turn id and nothing else. "No row" must be REPRESENTABLE, because a
    -- turn nobody wrote anything about is the entire class of failure being looked for; an inner join
    -- would silently reduce the population to the turns shadow already knows about, which is the same
    -- self-agreeing shape one layer down.
    --
    -- Matching on `conversation_turn_id` rather than on a re-derived (channel, provider_message_id) is
    -- deliberate: a shadow row that lost its canonical reference joins to NOTHING, so its turn reads as
    -- unobserved and the check goes red. That is the correct direction for a degradation.
    --
    -- `is_gap` IS COMPUTED ONCE, HERE, because it has TWO readers below: the count and the verdict. Two
    -- spelled-out copies of the definition of "unaccounted" agree until the day somebody edits one, and
    -- the disagreement is a window reporting `clean` beside a non-zero `unobserved_turns` — a shape a
    -- reader resolves in favour of the reassuring half.
    j AS (
        SELECT t.turn_id                                       AS turn_id,
               (sj.id IS NOT NULL)                             AS has_intake,
               coalesce(sj.intake_disposition, '')::text       AS intake_disposition,
               coalesce(sj.status, '')::text                   AS job_status,
               -- ANTI-JOIN A OR ANTI-JOIN B, per canonical turn:
               --   A  no intake disposition of any kind exists for this trusted turn — nobody wrote
               --      anything about it
               --   B  INTAKE said eligible and the QUEUE holds no job in any known state. Note the two
               --      columns: reading eligibility off `status` instead would make B unsatisfiable by
               --      construction, because a row edited out of the queue would leave `eligible` at the
               --      same moment it left the queue and the totals would still balance.
               (sj.id IS NULL
                OR (sj.intake_disposition = '{D_ELIGIBLE}'
                    AND sj.status NOT IN ({_QUEUE_LIST})))     AS is_gap
          FROM t
          LEFT JOIN public.semantic_shadow_jobs sj
                 ON sj.conversation_turn_id = t.turn_id
    )
    SELECT
        p_window_start,
        p_window_end,
        -- WHEN this answer was computed, as the database saw it. Distinct from window_end on purpose:
        -- window_end is the lagged ceiling the caller asked about, and the distance between the two is
        -- the lag actually in force. Without it a stale answer and a quiet hour look identical.
        pg_catalog.now(),
        pg_catalog.count(*),
        pg_catalog.count(*) FILTER (WHERE j.has_intake),
        -- ANTI-JOIN A, counted directly. Not `total - matched`: a subtraction can come out zero from two
        -- wrong numbers, and this predicate cannot.
        pg_catalog.count(*) FILTER (WHERE NOT j.has_intake),
        -- WHAT INTAKE DECIDED, from the column intake writes its decision in. A turn is "deliberately
        -- excluded" only when a TYPED REASON says so; a row sitting in the `excluded` status while its
        -- disposition still reads `eligible` carries no reason at all, and an exclusion with no reason is
        -- indistinguishable from a dropped turn — the single thing that status exists to prevent. So it
        -- is NOT counted here, and the anti-join above counts it as the gap it is.
        pg_catalog.count(*) FILTER (WHERE j.has_intake AND j.intake_disposition <> '{D_ELIGIBLE}'),
        pg_catalog.count(*) FILTER (WHERE j.has_intake AND j.intake_disposition = '{D_ELIGIBLE}'),
        -- The jobs reached THROUGH the canonical relationship, counted from the job side. Equal to
        -- trusted_turns_with_intake while `uq_semantic_shadow_job_conversation_turn` holds; if that
        -- constraint is ever lost the two diverge, which is worth being able to see.
        (SELECT pg_catalog.count(*)
           FROM public.semantic_shadow_jobs sj2
           JOIN t ON t.turn_id = sj2.conversation_turn_id),
        pg_catalog.count(*) FILTER (WHERE j.job_status = 'pending'),
        pg_catalog.count(*) FILTER (WHERE j.job_status = 'processing'),
        pg_catalog.count(*) FILTER (WHERE j.job_status = 'retryable_failed'),
        pg_catalog.count(*) FILTER (WHERE j.job_status IN ('completed', 'terminal_failed')),
        -- THE GAP: the disjunction of the two anti-joins, computed per turn in `j` and merely counted
        -- here. Never arithmetic — a subtraction can come out zero from two wrong numbers (a missing
        -- intake plus an unrelated stray job cancel exactly), and a per-row predicate cannot.
        pg_catalog.count(*) FILTER (WHERE j.is_gap),
        -- THE VERDICT, decided in the same statement and the same snapshot as the numbers it describes,
        -- so nothing downstream has to re-derive it from them — and off THE SAME `is_gap` column the
        -- count above reads, so the two cannot disagree about what a gap is. An empty window is genuinely
        -- clean: zero turns cannot have a gap. "I could not read the ledger" is NOT expressible here and
        -- must not be — it is the caller's observation that this function did not answer at all.
        CASE WHEN pg_catalog.count(*) FILTER (WHERE j.is_gap) > 0
             THEN '{R_FAILED}' ELSE '{R_CLEAN}' END
      FROM j;
END
$shadow_reconciliation$;
"""


def _can_provision_bypassrls_role(bind) -> bool:
    """Whether THIS migration role may create a BYPASSRLS role at all.

    Postgres requires superuser, or CREATEROLE held by a role that itself has BYPASSRLS. Asked rather
    than attempted, because an attempt that fails inside the migration transaction aborts the whole
    upgrade — and the honest degradation for a missing privilege is a reconciliation that reports
    `unknown`, not a deployment that cannot move.
    """
    row = bind.execute(sa.text(
        "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )).mappings().first()
    if row is None:
        return False
    return bool(row["rolsuper"] or (row["rolcreaterole"] and row["rolbypassrls"]))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    # 1. THE WINDOW INDEX. Plain schema, unconditional on privilege — the scan happens on every worker
    #    wake whether or not the definer below could be provisioned.
    if TURNS in tables and POPULATION_INDEX not in {i["name"] for i in insp.get_indexes(TURNS)}:
        op.create_index(POPULATION_INDEX, TURNS, ["role", "created_at"])

    if not _can_provision_bypassrls_role(bind):
        log.warning(
            "0035: this migration role cannot create a BYPASSRLS role, so %s was NOT created. The "
            "shadow reconciliation will report reconciliation_status=unknown (never clean) until an "
            "operator provisions %s and re-runs this migration.", FUNCTION, RECON_OWNER)
        return

    # 2. THE DEDICATED OWNER. NOLOGIN: nothing may ever connect as it. BYPASSRLS for one reason only —
    #    the aggregate genuinely crosses tenants, and a count RLS has filtered is indistinguishable from
    #    a quiet hour. Note what it is NOT given: no login, no CREATEDB, no CREATEROLE, no superuser.
    #
    #    A ROLE IS CLUSTER-WIDE, AND THIS MIGRATION IS NOT. Databases are the unit an upgrade runs
    #    against, but `pg_authid` is shared by every database in the cluster, so two upgrades of two
    #    DIFFERENT databases contend for one catalog row. `CREATE ROLE` was already guarded; the
    #    `ALTER ROLE` beside it was not, and an unconditional ALTER is a write whether or not it
    #    changes anything — the loser of the race died with `tuple concurrently updated` and took the
    #    whole upgrade with it. That is a real deployment hazard, not only a test one: api and worker
    #    booting together, or a retried migration job, is the same race.
    #
    #    So: write ONLY when the attributes are actually wrong, and treat a concurrent writer as a
    #    reason to look again rather than to fail. The re-check is what makes the retry safe — the
    #    winner has already set the attributes, so the loser's next pass finds them correct and skips.
    op.execute(
        f"DO $$ "
        f"DECLARE attempts int := 0; "
        f"BEGIN "
        f"  LOOP "
        f"    BEGIN "
        f"      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RECON_OWNER}') THEN "
        f"        CREATE ROLE {RECON_OWNER} NOLOGIN; "
        f"      END IF; "
        f"      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RECON_OWNER}' AND NOT ("
        f"            rolcanlogin = false AND rolsuper = false AND rolcreatedb = false "
        f"        AND rolcreaterole = false AND rolinherit = false "
        f"        AND rolreplication = false AND rolbypassrls = true)) THEN "
        f"        ALTER ROLE {RECON_OWNER} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"                                 NOINHERIT NOREPLICATION BYPASSRLS; "
        f"      END IF; "
        f"      EXIT; "
        f"    EXCEPTION WHEN duplicate_object OR internal_error THEN "
        f"      attempts := attempts + 1; "
        f"      IF attempts >= 5 THEN RAISE; END IF; "
        f"      PERFORM pg_sleep(0.05 * attempts); "
        f"    END; "
        f"  END LOOP; "
        f"END $$")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RECON_OWNER}")
    # SELECT on exactly the two tables the comparison needs, and no others. The definer's owner is a
    # principal like any other: giving it the blanket grant `bruce_app` holds would make the function's
    # return type the only thing between a bug and the whole schema.
    op.execute(f"GRANT SELECT ON {TURNS} TO {RECON_OWNER}")
    op.execute(f"GRANT SELECT ON {JOBS} TO {RECON_OWNER}")

    # 3. THE FUNCTION, owned by that role, executable by the app role and by nobody else.
    op.execute(_FUNCTION_SQL)
    op.execute(f"ALTER FUNCTION {FUNCTION_SIG} OWNER TO {RECON_OWNER}")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_SIG} FROM PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {FUNCTION_SIG} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIG} TO {APP_ROLE}")


def downgrade() -> None:
    # The function goes; the role is deliberately NOT dropped (it owns nothing else, and dropping a role
    # is a cluster-wide act inside a per-database rollback). Without the function the reconciliation
    # reports `unknown`, which is the honest state for a system that has just removed its own ability to
    # look — and nothing here re-opens a cross-tenant read of the student's words, because a downgrade
    # that restores an incident is not a rollback.
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_SIG}")
    op.execute(f"DROP INDEX IF EXISTS {POPULATION_INDEX}")
