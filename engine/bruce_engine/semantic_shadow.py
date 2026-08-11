"""SHADOW MODE — run the semantic executive beside the live router and record the difference, DURABLY.

WHY SHADOW BEFORE AUTHORITY. The synthetic corpus says the executive reads held-out phrasings at 97-99%.
That is a claim about a corpus someone wrote, and the thing it cannot tell you is how the executive behaves
on real founder traffic: how often it would refuse something that currently works, how often it would deny
a capability that is live, and how often it disagrees with the router that is answering students today.
Turning on authority from a synthetic score is the "unproven flag" this whole change exists to avoid.

WHY AN OUTBOX AND NOT A DETACHED TASK. The first durable-enough version scheduled the read as a detached
asyncio task with a strong reference and a hard budget. That never delayed or broke a turn — that property
is preserved below and is load-bearing — but it was BEST EFFORT: a process restart, a container shutdown or
a worker cancellation dropped the record. And it did not drop records uniformly. A fast successful read had
already finished; the ones in flight when the container went away were the SLOW ones and the ones retrying
a transport failure. So the sample that decides whether the executive may be given authority was biased
toward fast successful reads, which are the least informative reads in it. Slow and failing reads are the
evidence that matters most, and they are exactly what a best-effort pipeline loses.

So observation is now a ROW, on the pattern `intake_jobs` already established here:

    inbound turn persisted
      -> `enqueue` writes ONE idempotent job row       (request path; NO model call, ever)
      -> the student's reply continues, unaffected
      -> `claim_and_observe` claims it under a lease   (worker)
      -> the semantic read runs under a hard budget
      -> the reading is stored ON the job row with a TYPED outcome
      -> retry ONLY for infrastructure failure

WHAT SHADOW IS ALLOWED TO DO. Read, compute, and write ONE row. The constraint is structural rather than
disciplinary — nothing here takes a session that could transition a run, holds no gateway, and returns
nothing the response layer reads. It cannot:

  * create or continue a goal          (it never calls the agent run store)
  * create or resolve a Decision       (it never calls the transition machine or approvals)
  * authorize anything                 (it never calls the authorization evidence store)
  * call a provider                    (it never touches an adapter or the mutation gateway)
  * send an alternate reply            (it returns nothing the response layer reads)

`scripts/run_gates.py` asserts those imports are absent from THIS FILE. That is also why the job store
lives here rather than in a `semantic_shadow_jobs` module of its own: a second file would put half the
pipeline outside the gate that proves the observer cannot act.

TYPED OUTCOMES, BECAUSE A FAILURE IS DATA. `ok | timeout | transport | provider_rejected | invalid_schema
| budget_exceeded | turn_missing | exhausted_infrastructure_retries`. A failed observation is stored with
its reason and is NEVER marked `completed` — a "completed" row with no reading would quietly restore the
very bias the outbox removes. Only `transport` is retried: a lost packet is infrastructure, whereas a
timeout, a rejection or an unparseable answer is a FACT ABOUT THE EXECUTIVE on this turn, and re-rolling
it until it succeeds would resample the population until it looked healthy.

RETRIES ARE BOUNDED BY THE CLAIM, NOT BY THE RECORD. `attempts` advances when a job is CLAIMED. Advancing
it only when recording succeeded left one path unbounded: a worker that dies after claiming and before
recording (an OOM kill, a container eviction, a crash inside the read) leaves an expired lease on a row
whose attempt counter never moved, so the next worker starts from zero. A turn that reliably kills its
reader is then re-claimed forever. Counting on claim makes every path converge, and a job that runs out
of attempts becomes `exhausted_infrastructure_retries` — a TYPED terminal outcome, visible in the health
numbers, never `completed`, and never claimed again.

THE LEASE IS FENCED WITH A TOKEN, NOT A NAME. The completion writes used to require
`AND lease_owner = :worker`, and the worker id is `cloudrun-<nodename>-<pid>`: on one container two
overlapping /process invocations produce the SAME string, so the guard admitted precisely the write it
existed to stop — a worker whose lease expired mid-read overwriting the observation of the worker that
legitimately reclaimed the row. Every claim now mints a fresh `lease_token`, and every completion write
requires id AND user_id AND the processing state AND THAT token, so a stale worker updates zero rows.

WHAT IT RECORDS. The executive's reading, the live router's decision, and whether they AGREE — plus the
four numbers that decide whether authority is safe:

  false action            the executive proposed a WRITE the router did not, on a turn that is not a request
  false refusal           the executive refused where the router proceeded
  false capability denial PRODUCTION denied or disclaimed a capability that was registered, reachable and
                          identified by the executive — the founder's own turn, where Bruce said it could
                          not schedule anything while `calendar.create_event` was live
  disagreement            any divergence, so the rate is visible rather than inferred from the other three

The third one was NOT BEING COMPUTED AT ALL. `compare` accepted a `reachable` argument and discarded it,
so the number the authority decision most depends on was never produced on a single turn and nothing
failed. It is classified IN PROCESS now (see `compare` / `_capability_denial`) because it cannot be
recovered afterwards: the row stores no message body, by design, so a metric that is not decided while
the turn's reachable set is in hand cannot be decided later at all.

NO TURN TEXT LIVES ON THE JOB. The row REFERENCES the turn (channel + provider_message_id) and the worker
reads the student's words from `conversation_turns` under the owner's session at observation time. A
second copy of the most sensitive free-text Bruce holds would be a second thing to protect and a second
thing a deletion has to reach. Logging is labels, ids and latencies only — never message text, never an
exception's text, which can echo the message that caused it.

CAPABILITY TRUTH IS A FACT ABOUT A USER AND A MOMENT, NOT A CONSTANT. The reachable set used to come
from `semantic_executive.mini_context`, which takes no user_id and defaults to
`tool_registry.specs(None) if s.live` — a nine-element static table, identical for every user and every
turn, from a function whose own docstring says it does NOT check the user's connection. The consequence
ran in the worst possible direction. A user with NO Google connection asks for a calendar event; the
router truthfully answers that Bruce has no hands; the executive proposes `calendar.create_event`
because it is globally live; a FALSE CAPABILITY DENIAL is recorded against a router that was RIGHT. The
one number the authority decision most depends on was over-counting in the direction that argues FOR
granting authority.

So the reachable set is computed at the REQUEST BOUNDARY through the real per-user ToolBroker
(registration + liveness + connection + granted scopes), PERSISTED on the job row as a PII-free list of
registry ids, and the worker CONSUMES THAT SNAPSHOT. Not a worker-time recomputation from user_id:
capability truth has to describe THAT EXACT TURN, and an integration that connects or disconnects
between the request and the drain must not change what the comparison says happened.

EVERY TRUSTED INBOUND TURN IS OFFERED, NOT JUST THE ROUTED ONES. `enqueue` was called from inside the
`else` of `if resolved_continuation:`, so every resolved continuation and every ambiguous goal selection
skipped shadow entirely — no disposition, no row, no trace. That is the approve / reject / continuation
population: the safety-critical turns, and precisely the ones an authority decision must be made on.
Intake is now classified ABOVE the routing branches and written by ONE unconditional call, and a turn
that is not observed needs a TYPED, PERSISTED exclusion reason (see `classify_turn`).

AND RECONCILIATION IS INDEPENDENT. `eligible` used to be `sum(counts.values())` — the sum of the
dispositions the ledger recorded — so `reconciled` was TRUE BY CONSTRUCTION and stayed true while an
entire population never reached the function at all. The denominator now comes from the CANONICAL
INBOUND-TURN LEDGER (`conversation_turns`, role='user', the one row every trusted turn writes), so a
missing intake call is a GAP rather than an invariant that agrees with itself. See `reconcile`.

AND SO IS ITS POPULATION — WHICH IS WHERE THE SAME BUG SURFACED FOR THE THIRD TIME. `reconcile` was
honest. What was not honest was WHO IT WAS CALLED FOR: `worker_api.process` built its user list out of
the jobs `claim_and_observe` had just returned, because a worker session could not see `conversation_turns`
at all and those user ids were the only ones it had. A user whose intake call was MISSING has no job, so
was never in the list, so was never reconciled — and an empty queue produced `reconcile_users=()`, which
made `reconciled: True` the answer EVERY wake emitted. First `eligible = sum(counts)`, then
`intake.reconciled`, then the population selection: the same defect one layer further out each time. An
invariant whose population is derived from the thing being checked cannot fail.

So the population is now DERIVED FROM THE CANONICAL LEDGER ITSELF (`reconcile_window`), over a fixed
window with a stable, lagged upper watermark. Every affected turn is reconciled, INCLUDING the turns of
owners with zero shadow jobs — they are the whole point. `health()` no longer accepts a caller-supplied
user list at all, because a parameter is exactly how the population got derived from the jobs.

AND THE PRICE OF THAT COUNT WAS PAID TWICE BEFORE IT WAS PAID RIGHT. What follows is DESIGN history, not
deployment history: all of it happened on an undeployed chain that was collapsed to 0033/0034/0035 before
anything shipped, so the revision numbers this passage used to cite no longer exist. Neither wrong design
is reachable in the shipped chain, and `_population_refusal` below refuses at runtime if either returns.

THE FIRST ATTEMPT bought the count with `CREATE POLICY shadow_reconciliation_worker ON conversation_turns
FOR SELECT USING (app_is_worker())`. RLS policies are ROW-level: they cannot narrow a row to a column, so
that grant handed every worker session every column of every student's turn — `text` first among them,
and `SELECT text FROM conversation_turns` is already in this file, two lines below, on the per-job path.
Per-turn authorized access and global read access are different powers, and one was traded for the other
to compute a number.

THE SECOND ATTEMPT replaced the policy with `shadow_turn_population(window_start, window_end)`: SECURITY
DEFINER, owned by a dedicated NOLOGIN BYPASSRLS role, aggregate, no message text. That closed the COLUMN
exposure.

AND THEN IT WIDENED THE ENUMERATION, WHICH IS THE FOURTH TIME THIS SHAPE HAS APPEARED. That function
returned ONE ROW PER OWNER, carrying `user_id`. The worker then looped those owners and called
`observed_for(user_id)`, which opened `user_session(user_id)` and read that tenant's turns — so the
comparison was finished OUTSIDE the boundary, and finishing it required the owner list. An audit executed
exactly that path and read every turn row in the window, one authorized tenant at a time. Before the row
grant the worker learned an owner id only from a job it had CLAIMED; after the per-owner function it
received every owner present in the window, including the owners with no jobs. "Every individual read was
authorized" is not least privilege. Least privilege is that the read was never available.

WHAT SHIPS (migration 0035) MOVES THE JOIN INSIDE THE DEFINER. `public.shadow_reconciliation(window_start,
window_end)` inspects the canonical ledger and the shadow bookkeeping together and returns ONE ROW OF
NUMBERS plus its own verdict: no user ids, no turn ids, no per-user rows, nothing a model or a student
wrote. There is no list to loop and no per-tenant read left to open, so `observed_for` is gone rather
than narrowed.

THE TRUE PROPERTY, STATED NARROWLY, because the previous one was overstated: global reconciliation
exposes aggregate metadata only; raw conversation content remains accessible only through the existing
per-job authorized read — one CLAIMED job, under `user_session(job.user_id)`, for the single turn that
job is about (`trusted_text`).

AND THE GAP IS TWO ANTI-JOINS, NEVER TWO TOTALS. `unobserved_turns` is not `turns - eligible - excluded`.
Two totals that happen to agree is this module's entire history: `eligible = sum(counts)`, then
`intake.reconciled`, then the population selection — each an equation whose two sides came from the same
place. The function counts the gap directly from the canonical side: a trusted turn with NO intake
disposition at all, OR an eligible intake holding no shadow job in any known queue state. A subtraction
can come out zero from two wrong numbers; a `NOT EXISTS` cannot.

AND THREE STATES, NEVER TWO. `conversation_turns` is tenant-isolated, so a worker that lost the aggregate
path would see zero rows WITHOUT ERRORING — "the ledger is empty" and "I cannot see the ledger" would be
the same observation, and the third state would silently collapse into the first. So `reconcile_window`
proves its authority from DATABASE STATE before it believes a single count: a worker context, a function
that exists and is executable, owned by a NOLOGIN BYPASSRLS role, SECURITY DEFINER, with a fixed
search_path, no worker-admitting SELECT policy back on the table, and no surviving copy of the retired
owner-enumerating function. Anything else is `unknown`, never `clean` and never a fabricated zero.

AND NOTHING FREE-TEXT REACHES THE JSONB EITHER, which is a narrower rule than "no turn text" and was
being broken. The observation used to store `missing_information` and `validation_notes` verbatim. Both
are MODEL PROSE: the model is asked for an operation id and may answer with a sentence, so a note reading
`dropped unknown operation 'email mrs patel about the retake'` put a teacher's name and the substance of
the message into a telemetry table that no deletion reaches — and `missing_information` is free to say
whose address is missing. So the row now carries a COUNT and a closed vocabulary of validation CODES
(`semantic_executive.CODE_*`) instead. The rule for this table is: booleans, counts, latencies, ids from
the registry, and labels from a vocabulary this repo owns. Nothing a model wrote.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text as sa_text

from .db import user_session, worker_session

log = logging.getLogger("bruce.semantic_shadow")   # CONTENT-FREE: labels and latency only

_TRUTHY = {"1", "true", "yes", "on"}

TABLE = "semantic_shadow_jobs"

# The absolute ceiling on ONE shadow read. It is no longer a student's latency budget — nobody is waiting
# on this — it exists so a hung read cannot hold a worker container open or sit on a lease forever.
# It must stay comfortably ABOVE the triage deadline plus its one transport retry (2 x 2.5s), or every
# slow read would be recorded as `budget_exceeded` and the timeout/transport distinction — the reason
# these outcomes are typed at all — would be lost.
SHADOW_BUDGET_S = float(os.environ.get("BRUCE_SHADOW_BUDGET_S", "20"))

# --- the reconciliation window ---------------------------------------------------------------------
#
# HOW FAR BACK each wake re-checks the canonical ledger. A window rather than "everything" because the
# check runs on every wake and must not become a full scan of the most-written table in the schema; a
# window rather than "since the last wake" because a wake that is lost would take its slice of the
# population with it, and a population that depends on the drain having run is the defect this whole
# round is about.
RECONCILE_WINDOW_S = float(os.environ.get("BRUCE_SHADOW_RECONCILE_WINDOW_S", "3600"))

# THE UPPER WATERMARK'S LAG, and it is not a fudge factor. `conversation_store.persist_user_turn` commits
# the canonical turn and `semantic_shadow.intake` writes the job a few hundred milliseconds later, in a
# different transaction. A turn caught between the two is legitimately unobserved for an instant, and
# counting it would make the check cry wolf on every wake — which is how a real gap ends up being ignored.
# The watermark is read ONCE per check and passed to every query, so rows arriving mid-check cannot make
# two of them disagree about which population they described.
RECONCILE_LAG_S = float(os.environ.get("BRUCE_SHADOW_RECONCILE_LAG_S", "60"))

# --- the three reconciliation states, which must never collapse into two ----------------------------
RECON_CLEAN = "clean"      # the ledger was read, and every trusted turn in the window is accounted for
RECON_FAILED = "failed"    # the ledger was read, and turns in it have no shadow row at all
# THE THIRD STATE. The query raised, or it ran without the authority to see the population — a worker
# session on a tenant-isolated table returns zero rows and no error, so "empty" and "invisible" are the
# same observation unless something else distinguishes them. It is NEVER `clean` and it NEVER carries
# invented zero counts: a metric that reports a healthy empty ledger when it could not read the ledger is
# the original sin of this whole module, one layer up.
RECON_UNKNOWN = "unknown"

# --- job states (the intake_jobs vocabulary, unchanged on purpose) --------------------------------
PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
RETRYABLE_FAILED = "retryable_failed"
TERMINAL_FAILED = "terminal_failed"
# A trusted inbound turn that was deliberately NOT offered for observation, with the reason on the row.
# It is a status rather than an absent row because reconciliation counts turns against the canonical
# inbound-turn ledger: an exclusion that leaves nothing behind is indistinguishable from a lost turn, and
# "indistinguishable from a lost turn" is the entire failure this table exists to make impossible.
EXCLUDED = "excluded"

_TERMINAL = (COMPLETED, TERMINAL_FAILED)
# Statuses that are not waiting for a worker. `excluded` is not backlog: nobody is ever going to claim it.
_NOT_QUEUED = (*_TERMINAL, EXCLUDED)

DEFAULT_LEASE_SECONDS = 60
DEFAULT_RETRY_BACKOFF_SECONDS = 30

# --- typed outcomes --------------------------------------------------------------------------------
OK = "ok"
TIMEOUT = "timeout"                    # the triage deadline elapsed — a fact about the read, not a fault
TRANSPORT = "transport"                # a lost packet / an unreachable provider — infrastructure
PROVIDER_REJECTED = "provider_rejected"  # a 4xx: configuration or policy, retrying returns the same answer
INVALID_SCHEMA = "invalid_schema"      # the model answered something unusable
BUDGET_EXCEEDED = "budget_exceeded"    # the shadow's own ceiling fired before triage's did
TURN_MISSING = "turn_missing"          # the referenced turn is gone (deleted / never persisted)
# The job was claimed `max_attempts` times and never produced a recorded outcome — the shape a worker
# that dies mid-read leaves behind. It is an outcome rather than a silence because a turn nobody could
# ever read is EVIDENCE (a class of message that kills the reader), and because a row stuck at
# `processing` forever counts as backlog and makes the drain look broken.
EXHAUSTED = "exhausted_infrastructure_retries"

# THE ONLY RETRYABLE OUTCOME. Everything else is a measurement, and a measurement that is retried until it
# succeeds is the biased sample this whole design exists to prevent.
RETRYABLE_OUTCOMES = frozenset({TRANSPORT})

# Which health bucket each outcome belongs to. One table, because "how many model failures" must mean the
# same thing in the worker's emission and in whatever reads it later; two ad-hoc groupings would drift and
# the drift would look like a change in the executive.
#   malformed       the model ANSWERED and the answer was unusable — an output-contract problem
#   model_failures  the model did not answer usably in time, or refused — a fact about the executive
#   infra_failures  nothing about the executive: a lost packet, or our own ceiling firing
#   turn_missing    neither; the referenced turn was deleted, which is the privacy design working
#   exhausted       the read never completed at all, across every attempt
HEALTH_BUCKETS: dict[str, str] = {
    OK: "completed",
    INVALID_SCHEMA: "malformed",
    TIMEOUT: "model_failures",
    PROVIDER_REJECTED: "model_failures",
    TRANSPORT: "infrastructure_failures",
    BUDGET_EXCEEDED: "infrastructure_failures",
    TURN_MISSING: "turn_missing",
    EXHAUSTED: "exhausted",
}


def enabled() -> bool:
    """Shadow recording, off by default.

    Separate from `BRUCE_ROUTER_SEMANTIC` on purpose: that flag is about AUTHORITY, this one is about
    OBSERVATION, and conflating them would mean the only way to measure the executive is to let it decide.
    """
    return os.environ.get("BRUCE_SEMANTIC_SHADOW", "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ReachableOperations:
    """THIS user's capability truth AS OF THIS TURN, carried from the request boundary to the worker.

    Two fields rather than one set, because "this user can reach nothing" and "we could not establish
    what this user can reach" are different facts that would otherwise both be an empty set. Only the
    first is evidence; the second must never be counted as a denial, or an outage manufactures the
    metric. `established=False` is what a NULL column means when the worker rehydrates it.

    The operations are registry ids. Nothing here is PII, which is why a snapshot may be persisted on a
    telemetry row at all.
    """

    operations: frozenset[str] = frozenset()
    established: bool = False

    def as_json(self) -> list[str] | None:
        """NULL for an unestablished snapshot — a JSON `[]` would claim we looked and found nothing."""
        return sorted(self.operations) if self.established else None

    @staticmethod
    def from_json(blob: Any) -> ReachableOperations:
        if blob is None or not isinstance(blob, (list, tuple)):
            return ReachableOperations()
        return ReachableOperations(frozenset(str(x) for x in blob), established=True)


async def turn_capability_truth(user_id: UUID) -> ReachableOperations:
    """What this ONE user can actually run, right now, from the real per-user broker.

    Called at the REQUEST BOUNDARY and persisted, never recomputed at drain time. Two independent
    reasons, and both are about the metric rather than about cost:

      * a global list is not capability truth. `tool_registry.specs(None)` says what EXISTS; it says
        nothing about whether this student ever connected Google, which is the difference between the
        router lying to them and the router telling them the truth.
      * capability truth MOVES. An integration connected (or revoked) between the request and the drain
        would silently rewrite what the comparison says happened on a turn that is already over.

    Failure is UNKNOWN, never empty-and-established: an unknown truth may not be read as a denial.
    """
    try:
        from . import tool_broker
        snap = await tool_broker.reachable_operations(user_id)
        return ReachableOperations(frozenset(snap.operations), established=snap.established)
    except Exception:
        log.info("shadow_capability_truth_failed")   # a label; never the exception text
        return ReachableOperations()


@dataclass(frozen=True)
class ShadowRecord:
    """One turn, read both ways. Pure data — nothing here can act.

    No `text` field, deliberately: the reading is compared against the turn it came from by joining
    `conversation_turns` on (user_id, channel, provider_message_id). Carrying a copy would duplicate the
    student's words into a telemetry table that a deletion does not reach.
    """

    user_id: str
    channel: str
    message_id: str

    # what the executive understood
    exec_mode: str
    exec_operation: str | None
    exec_polarity: str
    exec_confidence: float
    exec_goal_id: str | None
    # HOW MANY things the reading said it still needed, never WHICH. `missing_information` is model prose
    # and is free to name the person whose address is missing; the count answers the question this table
    # is for ("did the executive think it could proceed") without storing anyone's name.
    exec_missing_count: int
    # The closed vocabulary from `semantic_executive.CODE_*` — never `validation_notes`, which interpolate
    # whatever the model proposed and have carried a whole sentence of a student's message.
    exec_validation_codes: tuple[str, ...]
    exec_latency_ms: int

    # what production actually did
    router_class: str | None
    router_action: str | None
    router_domain: str | None
    router_capabilities: tuple[str, ...]
    router_source: str | None

    # the comparison
    agrees: bool
    divergence: str | None

    # FALSE CAPABILITY DENIAL — classified here, in process, never inferred later. There are no stored
    # message bodies to re-derive it from and there must never be, so if it is not decided at observation
    # time while the turn's reachable set is still known, it cannot be decided at all.
    false_capability_denial: bool = False
    denied_operation_id: str | None = None      # a registry id, not a description
    denial_reason: str | None = None            # a short code from DENIAL_*, never prose
    # THE DENOMINATOR OF THE DENIAL METRIC. A denial cannot be counted on a turn whose capability truth
    # was never established, so how often that happened has to be readable from the rows themselves —
    # otherwise "no denials" and "we could not tell" look identical in the total.
    reachable_established: bool = False
    reachable_count: int = 0

    def as_json(self) -> dict:
        """The JSONB payload stored on the job row. Labels, ids, counts and latency — nothing free-text.

        Deliberately NOT "whatever the reading contained, minus the obvious": everything here is either a
        number, a boolean, an id from Bruce's own registry, or a label from a vocabulary this repo owns.
        The previous version stored the model's `missing_information` and the executive's prose validation
        notes, both of which can contain a person's name or the substance of the student's message.
        """
        return {
            "exec": {
                "mode": self.exec_mode, "operation": self.exec_operation,
                "polarity": self.exec_polarity, "confidence": self.exec_confidence,
                "goal_id": self.exec_goal_id, "missing_count": int(self.exec_missing_count),
                "validation_codes": list(self.exec_validation_codes)[:8],
                "latency_ms": self.exec_latency_ms,
            },
            "router": {
                "execution_class": self.router_class, "action": self.router_action,
                "domain": self.router_domain, "capabilities": list(self.router_capabilities),
                "source": self.router_source,
            },
            "agrees": self.agrees, "divergence": self.divergence,
            "capability": {"false_denial": self.false_capability_denial,
                           "operation": self.denied_operation_id, "reason": self.denial_reason,
                           # ids are NOT stored back here: they are already on the row's own snapshot
                           # column, and a second copy is a second thing to keep true.
                           "reachable_established": self.reachable_established,
                           "reachable_count": int(self.reachable_count)},
        }


# WHICH PRODUCTION LANE DECIDED THIS TURN. A turn is not always routed: a resolved continuation and an
# ambiguous goal selection are answered from STATE, deliberately, and never reach the classifier. Those
# turns are now observed too (they are the approve / reject / continuation population), and the lane has
# to be on the row — otherwise the worker reads an empty router snapshot and concludes production wanted
# no work, which would file every "yeah send it" as `executive_found_work_router_missed` and, worse, as a
# FALSE CAPABILITY DENIAL against a lane that denied nothing and was busy doing the work.
PATH_ROUTED = "routed"                        # the FastRouter classified this turn
PATH_CONTINUATION = "continuation"            # production acted on work already in flight
PATH_GOAL_AMBIGUOUS = "goal_selection_ambiguous"   # production asked WHICH open goal this is about
PATH_ROUTER_ERROR = "router_error"            # the router raised; production fell through to the reasoner

# The lanes in which production is dealing with work that already exists. Nothing here can be a capability
# denial: the student was not told Bruce has no hands, they were answered about work in progress.
_WORK_IN_FLIGHT_PATHS = (PATH_CONTINUATION, PATH_GOAL_AMBIGUOUS)

# What the executive reading looks like when it, too, understood the turn as being about work in flight.
# `answer_question` is included because a status question about a draft IS a reading of the continuation;
# `clarify` is not, because it means the executive did not place the turn at all.
_CONTINUATION_MODES = ("continue_goal", "approve", "reject", "cancel", "answer_question")


def _router_missed(router_source: str | None) -> bool:
    """Did the live router fall through to the silent default?

    `router_default` is the source Stage 1 stamps when it had no provider — which in deployed staging is
    EVERY turn Stage 0's regexes did not match. This is the population the executive exists to rescue, so
    it is counted separately from ordinary disagreement.
    """
    return router_source == "router_default"


# Why the production path offered no capability, when a reachable one existed. Short codes, because this
# is stored: `router_default` is Stage 1's silent fall-through (the founder incident — Bruce said it could
# not do outside actions while calendar.create_event was live), `router_decided` is a router that produced
# a real decision and still named nothing.
DENIAL_ROUTER_DEFAULT = "router_default_no_capability"
DENIAL_ROUTER_DECIDED = "router_decided_no_capability"


@dataclass(frozen=True)
class Comparison:
    """One turn, read both ways, reduced to the numbers the authority decision is made on.

    A dataclass rather than a tuple because it grew a third answer: `compare` used to ACCEPT `reachable`
    and silently discard it, so false capability denial — one of the four metrics — was never computed at
    all. A tuple return is exactly what let a parameter sit unused without anything noticing.
    """

    agrees: bool
    divergence: str | None = None
    false_capability_denial: bool = False
    denied_operation_id: str | None = None
    denial_reason: str | None = None


def compare(turn: Any, decision: Any, *, reachable: frozenset[str]) -> Comparison:
    """Do the executive and the live router agree about this turn, and did production deny a live capability?

    "Agree" is deliberately coarse: whether both concluded that WORK IS WANTED, and if so whether they
    named the same capability. Comparing finer than that would report disagreement on distinctions the
    student never made — the router has no notion of `answer_question`, and calling that a divergence
    would bury the divergences that matter.

    FALSE CAPABILITY DENIAL is narrower than disagreement and is the metric this whole module was built
    for. It is recorded only when ALL THREE hold:

      (a) the operation was REGISTERED and REACHABLE for that turn. `reachable` comes from the broker at
          observation time; an EMPTY set means capability truth could not be established, and an unknown
          truth may never be counted as a denial — that would manufacture the metric out of an outage.
      (b) the PRODUCTION path denied or disclaimed the capability: it named no capability at all and
          routed the turn to conversation. A router that named a DIFFERENT capability is a disagreement
          (`different_capability`), not a denial — it did not tell the student it had no hands.
      (c) the SEMANTIC result identified the compatible operation, i.e. it survived validation with a real
          operation id on it.

    All three are decided HERE, while the turn's reachable set is still in hand. Nothing about this can be
    reconstructed later: the row stores no message body, and it never will.

    `reachable` IS THAT TURN'S PERSISTED SNAPSHOT, from the per-user broker at the request boundary. It is
    never the global live registry: `calendar.create_event` is live for the product and unreachable for a
    student who never connected Google, and reading the first as the second turns every unconnected
    account into evidence against a router that told them the truth.
    """
    path = getattr(decision, "production_path", None) or PATH_ROUTED
    exec_mode = turn.mode.value
    exec_wants_work = exec_mode in ("new_goal", "continue_goal") or turn.proposed_operation_id
    router_caps = tuple(getattr(decision, "candidate_capabilities", ()) or ())

    if path in _WORK_IN_FLIGHT_PATHS:
        # PRODUCTION WAS ADVANCING WORK, not classifying a request. The comparison worth making is
        # whether the executive ALSO read the turn as being about work already open — an executive that
        # reads "yeah send it" as a brand-new goal would have created a SECOND goal, which is the
        # divergence that matters most on this population and is invisible if it is lumped in with
        # "router found work, executive did not".
        if exec_mode in _CONTINUATION_MODES:
            # AND WHETHER THE TWO READERS MEANT THE SAME OPERATION. `RouterSnapshot.work_in_flight`
            # carries `pending_capability` — the registry id off the still-pending Decision, i.e. the
            # operation the student was actually being asked about — and `compare` used to DISCARD it:
            # the snapshot's whole reason for existing was documented as load-bearing and consumed by
            # nothing, so on the entire approval population "agrees" meant only "both readers knew this
            # was a continuation". An executive that approves a DIFFERENT capability than the one the
            # student was shown is the single most dangerous reading on this population — it is consent
            # transferred to an action nobody consented to — and it was scoring as agreement.
            #
            # Only when BOTH are known. A reading that named no operation is agreeing that the turn is
            # about work in flight and claiming nothing more; a lane with no pending capability (an
            # ambiguous goal selection has no Decision open) is not asking about an operation at all.
            # Counting either as divergence would manufacture the metric out of a missing value.
            pending = router_caps[0] if router_caps else None
            if pending and turn.proposed_operation_id and turn.proposed_operation_id != pending:
                return Comparison(agrees=False, divergence="different_capability_in_flight", **_NO_DENIAL)
            return Comparison(agrees=True, **_NO_DENIAL)
        divergence = ("executive_started_new_work" if exec_mode == "new_goal"
                      else "executive_missed_the_work_in_flight")
        return Comparison(agrees=False, divergence=divergence, **_NO_DENIAL)

    router_wants_work = getattr(decision, "execution_class", None) in (
        "direct_action", "foreground_agent", "background_mission") or bool(router_caps)
    denial = _capability_denial(turn, decision, reachable=reachable, router_wants_work=router_wants_work,
                                path=path)

    if not exec_wants_work and not router_wants_work:
        return Comparison(agrees=True, **denial)
    if exec_wants_work and not router_wants_work:
        # THE POPULATION THIS CHANGE EXISTS FOR: the router fell to chat, the executive read a request.
        return Comparison(agrees=False, divergence=("executive_found_work_router_missed" if _router_missed(
            getattr(decision, "source", None)) else "executive_found_work_router_did_not"), **denial)
    if router_wants_work and not exec_wants_work:
        return Comparison(agrees=False, divergence="router_found_work_executive_did_not", **denial)
    if turn.proposed_operation_id and router_caps and turn.proposed_operation_id not in router_caps:
        return Comparison(agrees=False, divergence="different_capability", **denial)
    return Comparison(agrees=True, **denial)


_NO_DENIAL = {"false_capability_denial": False, "denied_operation_id": None, "denial_reason": None}


def _capability_denial(turn: Any, decision: Any, *, reachable: frozenset[str],
                       router_wants_work: bool, path: str = PATH_ROUTED) -> dict:
    """Conditions (a), (b) and (c) of a false capability denial, in that order. Returns the three fields
    `Comparison` stores, all falsey when any condition fails.

    The registry check is not redundant with `reachable`. `reachable` is the BROKER's answer (what this
    user can run now, on this turn) and the registry is what exists at all; a capability that is reachable
    but no longer registered is a configuration fault, and filing it as a false denial would blame the
    router for the executive naming something Bruce cannot run.

    ONLY THE ROUTED LANE CAN DENY A CAPABILITY. A continuation was answered from state and a router error
    is an outage; neither told the student Bruce has no hands, and counting either would manufacture this
    metric out of a lane that never spoke about capabilities at all — the same mistake as reading an
    unestablished reachable set as permission to count.
    """
    op = getattr(turn, "proposed_operation_id", None)
    none = dict(_NO_DENIAL)
    if path != PATH_ROUTED:                                  # (b) — no other lane disclaims anything
        return none
    if not op or not reachable or op not in reachable:      # (a) and (c)
        return none
    if router_wants_work:                                    # (b) — production offered work, so no denial
        return none
    from . import semantic_executive                         # registry truth, never the prompt's
    if semantic_executive.canonical_operation(op) != op:     # (a) — registered under this exact id
        return none
    return {"false_capability_denial": True, "denied_operation_id": op,
            "denial_reason": (DENIAL_ROUTER_DEFAULT if _router_missed(getattr(decision, "source", None))
                              else DENIAL_ROUTER_DECIDED)}


@dataclass(frozen=True)
class RouterSnapshot:
    """What production decided, as it was at the turn — the comparison baseline, rehydrated at worker time.

    Shape-compatible with RouterDecision so `compare` reads it the same way it reads the live object.
    Snapshotted rather than recomputed because re-running the router minutes later would compare the
    executive against a decision no student ever received.
    """

    execution_class: str | None = None
    action: str | None = None
    domain: str | None = None
    candidate_capabilities: tuple[str, ...] = ()
    source: str | None = None
    # WHICH LANE ANSWERED THIS TURN. Not every turn is routed — a resolved continuation and an ambiguous
    # goal selection are answered from state and never reach the classifier — and without this the worker
    # cannot tell "production named no capability" from "production was not being asked to".
    production_path: str = PATH_ROUTED

    @staticmethod
    def of(decision: Any) -> RouterSnapshot:
        """Flatten a live RouterDecision (enums and all) into labels."""
        def _val(name):
            v = getattr(decision, name, None)
            return getattr(v, "value", v)
        return RouterSnapshot(
            execution_class=_val("execution_class"), action=_val("action"),
            domain=getattr(decision, "domain", None),
            candidate_capabilities=tuple(getattr(decision, "candidate_capabilities", ()) or ()),
            source=getattr(decision, "source", None),
            production_path=getattr(decision, "production_path", None) or PATH_ROUTED)

    @staticmethod
    def work_in_flight(path: str, *, pending_capability: str | None = None,
                       evidence: str | None = None) -> RouterSnapshot:
        """The baseline for a turn production answered from STATE rather than from the classifier.

        `pending_capability` is the operation the student was actually being asked about, read off the
        pending Decision — a registry id, so it is safe on this row and it is what makes "did the two
        readers mean the same operation" answerable for the approval population at all.

        IT IS CONSUMED, and saying so is the correction. This field was documented exactly as above and
        read by nothing: `compare` returned agreement for the whole work-in-flight lane on `exec_mode`
        alone, so an executive approving a DIFFERENT capability than the one the student was shown scored
        as agreement. `compare` now files that as `different_capability_in_flight`, and
        `test_shadow_capability_denial` fails if the field goes back to being decorative.
        """
        return RouterSnapshot(execution_class=None, action=None, domain=None,
                              candidate_capabilities=((pending_capability,) if pending_capability else ()),
                              source=evidence, production_path=path)

    @staticmethod
    def from_json(blob: Any) -> RouterSnapshot:
        d = blob if isinstance(blob, dict) else {}
        return RouterSnapshot(
            execution_class=d.get("execution_class"), action=d.get("action"), domain=d.get("domain"),
            candidate_capabilities=tuple(d.get("candidate_capabilities") or ()), source=d.get("source"),
            # Rows written before the lane existed all came from the routed branch — it was the only
            # branch that could reach `enqueue` at all, which is the defect this field closes.
            production_path=d.get("production_path") or PATH_ROUTED)

    def as_json(self) -> dict:
        return {"execution_class": self.execution_class, "action": self.action, "domain": self.domain,
                "candidate_capabilities": list(self.candidate_capabilities), "source": self.source,
                "production_path": self.production_path}


def status_for(outcome: str, *, can_retry: bool) -> str:
    """The job status a typed outcome resolves to. Pure, so the "never completed on failure" rule is
    testable without a database — it is the rule the whole outbox exists to protect."""
    if outcome == OK:
        return COMPLETED
    if outcome in RETRYABLE_OUTCOMES and can_retry:
        return RETRYABLE_FAILED
    return TERMINAL_FAILED


@dataclass
class ClaimedShadowJob:
    """Everything the worker needs to observe one turn — no ORM object escapes the store."""

    id: UUID
    user_id: UUID
    channel: str
    provider_message_id: str
    attempts: int
    max_attempts: int
    router: RouterSnapshot
    context_flags: dict
    # THE TURN'S OWN CAPABILITY TRUTH, rehydrated from the row. The worker builds the executive's context
    # from this and compares against this; it never asks the broker again and never falls back to the
    # global registry, because an integration that connected or was revoked in between would rewrite what
    # the comparison says happened on a turn that is already over.
    reachable: ReachableOperations = dataclasses.field(default_factory=ReachableOperations)
    # WHICH worker holds this claim — for logs only. It cannot fence anything: `cloudrun-<node>-<pid>` is
    # shared by every overlapping invocation in one container, so two live workers satisfy it at once.
    lease_owner: str | None = None
    # WHICH CLAIM this is. Minted fresh by `claim`, required by every completion write, and the only thing
    # here a stale worker cannot reproduce.
    lease_token: str | None = None

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts


# THE FIELDS THE DATABASE DECIDES. Named once, here, because they travel as a set: the function's return
# columns, the dataclass below, the verdict's dependent counts and the emitted block are all this tuple,
# and a name that exists in three of the four is how a count gets quietly dropped from the emission (see
# `UNCLASSIFIED_OUTCOME`, which was computed and then omitted by a hand-written key list).
RECONCILIATION_COUNTS = (
    "canonical_trusted_turns",        # trusted inbound turns in the window, from the canonical ledger
    "trusted_turns_with_intake",      # ... of those, the ones an intake disposition row exists for
    "trusted_turns_without_intake",   # ... and the ones it does not: ANTI-JOIN A, counted directly
    # The next two partition `trusted_turns_with_intake` on `intake_disposition` — what INTAKE decided —
    # and NOT on `status`, which is what the queue currently holds. Splitting on the queue status instead
    # would let a row edited out of the queue move itself from `eligible` to `excluded`, leaving the
    # totals balanced and the anti-join below unsatisfiable by construction.
    "explicitly_excluded_turns",      # deliberately not observed, with the typed reason on the row
    "eligible_turns",                 # offered for observation
    "shadow_jobs",                    # job rows reached THROUGH the canonical conversation_turn_id
    "pending_jobs",
    "processing_jobs",
    "retryable_jobs",
    "terminal_jobs",
    "unobserved_turns",               # THE GAP: anti-join A or anti-join B. Never a subtraction.
)


@dataclass(frozen=True)
class LedgerReconciliation:
    """WHAT THE DATABASE CONCLUDED about the window — numbers and a verdict, and nothing that identifies
    anyone.

    This replaces `TurnPopulation`, and the deletion is the point rather than a rename. That dataclass
    carried `users` and `per_user`: one entry PER OWNER, because the comparison was finished in Python
    and finishing it required the owner list. The worker looped it, opened a session per tenant, and read
    each tenant's turns — every read authorized, and the whole set an enumeration the worker had no
    business holding. The join now happens inside the SECURITY DEFINER function, so there is nothing per
    owner to carry and no list to loop.

    `visible` is what keeps three states from becoming two. `conversation_turns` is tenant-isolated: a
    session without the aggregate path sees ZERO rows and raises NOTHING, so a missing grant is
    indistinguishable from an empty hour unless the query separately establishes that it had the
    authority to look. False here means `unknown` — never `clean`, and never the zero counts that would
    make an unreadable ledger look like a healthy one.

    `status` is the DATABASE's verdict, decided in the same statement and the same snapshot as the counts
    it describes. It is carried rather than recomputed for the reason `reconciliation_verdict` exists at
    all: a status derived twice from the same numbers agrees today and drifts the first time either copy
    is edited, and the drift is invisible because both look reasonable.
    """

    visible: bool = False
    window_start: datetime.datetime | None = None
    window_end: datetime.datetime | None = None
    # WHEN the answer was computed, as the database saw it. Distinct from `window_end`, which is the
    # lagged ceiling that was asked about: the distance between them is the lag actually in force, and
    # without it a stale answer and a quiet hour look identical.
    watermark: datetime.datetime | None = None
    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    status: str | None = None
    # WHY it was not readable, when it was not — a label from the closed vocabulary below, so it is safe
    # to emit into a log line and a response body.
    unreadable_reason: str | None = None


# --- the aggregate read path, and every way it can stop being one ------------------------------------
#
# THE COMPARISON HAPPENS INSIDE THE FUNCTION (migration 0035). Two things were wrong with doing it
# outside. A row-level grant cannot narrow a row to a column, so the row grant's `FOR SELECT USING
# (app_is_worker())` gave a worker session every column of every student's turn in exchange for a count.
# And the per-owner replacement returned one row PER OWNER, so the worker still had to loop those owners and
# read each tenant's turns to finish the join — closing the column exposure and widening the enumeration.
# This function joins the canonical ledger to the shadow bookkeeping itself and returns one row of
# counts, so the worker holds EXECUTE and learns nothing about who is in the sample.
RECONCILIATION_FUNCTION = "shadow_reconciliation"

# the per-owner function, which returned `user_id` per owner. It is never created. Its ABSENCE is checked at
# runtime rather than trusted to the migration: it is an owner-enumeration surface the app role holds
# EXECUTE on, and a hand-restored copy would put the removed capability back with nothing to notice.
RETIRED_POPULATION_FUNCTION = "shadow_turn_population"

# The widest window the caller will ask for. The function refuses anything wider; this is the same
# ceiling stated on the calling side so a misconfigured env var fails here rather than as a database
# error nobody attributes to configuration.
MAX_RECONCILE_SPAN_S = 7 * 24 * 3600.0

# WHY the ledger could not be read. Each is a distinct, actionable fact — collapsing them into one
# "unknown" would leave an operator unable to tell a missing deploy step from a privilege regression.
NOT_A_WORKER = "no_worker_context"                       # this is not a worker session at all
NO_POPULATION_FUNCTION = "no_population_function"        # 0035 never ran here, or EXECUTE was revoked
FUNCTION_NOT_HARDENED = "population_function_not_hardened"   # it exists but is no longer the safe shape
# A worker-admitting SELECT policy is BACK on conversation_turns. That is the incident the aggregate closed, and
# a reconciliation that quietly kept working over it would be the reason nobody noticed.
LEDGER_ROW_GRANT_PRESENT = "ledger_row_grant_present"
# The per-owner function is executable again. It hands the worker a list of user ids, which is the
# capability the shipped design removed and the only reason a per-tenant read of the ledger was ever reachable.
OWNER_ENUMERATION_PATH_PRESENT = "owner_enumeration_path_present"
BAD_WINDOW = "invalid_reconciliation_window"             # the configured window/lag is not usable
# The function answered with no row at all. Distinct from an empty ledger on purpose: an aggregate over
# zero turns still returns ONE row of zeros, so "no row" means the function did not do what it claims to
# and its silence must not be read as a clean, empty window.
NO_RECONCILIATION_ROW = "reconciliation_returned_no_row"


class ShadowJobStore(Protocol):
    async def enqueue(self, user_id: UUID, *, channel: str, provider_message_id: str,
                      router: RouterSnapshot, context_flags: dict,
                      reachable: ReachableOperations = ReachableOperations(),
                      disposition: str = "", status: str = PENDING,
                      conversation_turn_id: UUID | None = None) -> bool: ...
    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ClaimedShadowJob | None: ...
    # THE WHOLE RECONCILIATION, AS ONE AGGREGATE ANSWER. The canonical ledger is joined to the shadow
    # bookkeeping BEHIND the boundary and what comes back is counts and a verdict — never a population to
    # iterate. There is deliberately no `observed_for` beside it any more: that method existed only to
    # finish the join out here, and finishing it out here is what required the owner ids.
    #
    # It raises rather than returning an empty answer when it cannot read, because a swallowed failure
    # here is the exact shape of the bug it replaces.
    async def reconcile_window(self) -> LedgerReconciliation: ...
    # The INDEPENDENT per-owner reconciliation, for a NAMED owner a caller already holds. Not on the
    # global path: nothing in the reconciliation pass calls it, because taking a user id is precisely how
    # the pass came to need a list of them.
    async def reconcile(self, user_id: UUID) -> dict: ...
    async def trusted_text(self, job: ClaimedShadowJob) -> str | None: ...
    # The completion writes RETURN whether they landed. A fenced-out write is not an error and must not
    # raise — another worker legitimately owns the job — but it must not be reported as a recorded
    # observation either, which is what a `None` return quietly allowed.
    async def record(self, job: ClaimedShadowJob, *, outcome: str, status: str,
                     record: ShadowRecord | None) -> bool: ...
    async def mark_retryable(self, job: ClaimedShadowJob, *, outcome: str,
                             backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> bool: ...
    async def terminalize_exhausted(self) -> int: ...
    async def counts(self) -> dict: ...


def _short(reason: str) -> str:
    """last_error stores a TYPE/short reason only — never student content (column is 200 chars)."""
    return (reason or "")[:200]


# --- Postgres ---------------------------------------------------------------------------------------

# THE AUTHORITY PROOF, ASKED OF THE CATALOG. Every clause below is a way the aggregate path can quietly
# stop being one, and each of them would otherwise present as a perfectly clean, perfectly empty ledger.
# It reads `pg_proc` / `pg_roles` / `pg_policies` — none of which the policies being checked can affect —
# so unlike a check that reads this repository's source, it cannot be satisfied by a file.
_POPULATION_GUARD = sa_text(
    """
    SELECT app_is_worker()                                     AS worker_ctx,
           p.oid IS NOT NULL                                   AS present,
           coalesce(p.prosecdef, false)                        AS security_definer,
           coalesce(array_to_string(p.proconfig, ','), '')     AS settings,
           coalesce(r.rolbypassrls, false)                     AS owner_bypassrls,
           coalesce(r.rolcanlogin, true)                       AS owner_can_login,
           coalesce(r.rolsuper, true)                          AS owner_is_super,
           coalesce(has_function_privilege(current_user, p.oid, 'EXECUTE'), false) AS may_execute,
           coalesce(has_function_privilege('public', p.oid, 'EXECUTE'), true)      AS public_may_execute,
           EXISTS (SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public' AND tablename = 'conversation_turns'
                      AND cmd IN ('SELECT', 'ALL')
                      AND strpos(coalesce(qual, ''), 'app_is_worker') > 0)          AS row_grant,
           EXISTS (SELECT 1 FROM pg_proc rp
                     JOIN pg_namespace rn ON rn.oid = rp.pronamespace
                    WHERE rn.nspname = 'public' AND rp.proname = :retired
                      AND has_function_privilege(current_user, rp.oid, 'EXECUTE'))  AS retired_present
      FROM (SELECT 1) one
      LEFT JOIN (pg_proc p
                 JOIN pg_namespace n ON n.oid = p.pronamespace
                 JOIN pg_roles r ON r.oid = p.proowner)
             ON n.nspname = 'public' AND p.proname = :fn
    """
)


def _population_refusal(guard: Any) -> str | None:
    """The reason this session must NOT believe a reconciliation, or None if it may.

    FAIL CLOSED, CLAUSE BY CLAUSE. A SECURITY DEFINER function with no pinned search_path runs the
    CALLER's path as a BYPASSRLS role, which is a privilege-escalation hole rather than an untidiness; an
    owner that can log in is a login this deployment did not intend; an owner that is a SUPERUSER makes
    the narrow table grants decorative; EXECUTE still held by PUBLIC means the REVOKE was lost; and a
    worker-admitting SELECT policy back on `conversation_turns` is the incident the aggregate function closed.

    AND THE RETIRED FUNCTION MUST BE GONE. `shadow_turn_population` returned one row per owner and the
    app role held EXECUTE on it — the enumeration the shipped design removes. Checking only that the new function is
    present would let the old one sit beside it, which is a capability nobody decided to keep and nothing
    would ever report.

    Every clause degrades to `unknown`, because a reconciliation that keeps reporting `clean` across a
    privilege regression is how the regression survives.
    """
    if guard is None:                       # a one-row query that returned nothing is not "empty"
        return NOT_A_WORKER
    if not guard["worker_ctx"]:
        return NOT_A_WORKER
    if not guard["present"] or not guard["may_execute"]:
        return NO_POPULATION_FUNCTION
    if not (guard["security_definer"] and "search_path=" in guard["settings"]
            and guard["owner_bypassrls"] and not guard["owner_can_login"]
            and not guard["owner_is_super"] and not guard["public_may_execute"]):
        return FUNCTION_NOT_HARDENED
    if guard["row_grant"]:
        return LEDGER_ROW_GRANT_PRESENT
    if guard["retired_present"]:
        return OWNER_ENUMERATION_PATH_PRESENT
    return None


class PostgresShadowStore:
    """The real durable store. Enqueue runs under the OWNER's session (the request already has one);
    claiming is the only cross-user step and runs under a worker session, exactly like intake_jobs.
    Every write of an observation goes back through user_session(job.user_id)."""

    async def enqueue(self, user_id: UUID, *, channel: str, provider_message_id: str,
                      router: RouterSnapshot, context_flags: dict,
                      reachable: ReachableOperations = ReachableOperations(),
                      disposition: str = "", status: str = PENDING,
                      conversation_turn_id: UUID | None = None) -> bool:
        # ON CONFLICT DO NOTHING against the UNIQUE constraints IS the idempotency. A SELECT-then-INSERT
        # would race two deliveries of the same webhook into two observations of one turn.
        #
        # NO CONSTRAINT IS NAMED, deliberately, and the change is the point of migration 0033. There are
        # now TWO keys and they mean different things: `uq_semantic_shadow_job_conversation_turn` is the
        # INVARIANT (one job per canonical conversation turn) and
        # `uq_semantic_shadow_job_turn` (user, channel, provider_message_id) is INGRESS DEDUPE, still
        # needed for the instant before a canonical id is known and for rows written before that table existed. Naming
        # one constraint would let a violation of the other raise into the swallowing except below and be
        # filed as `enqueue_failed`, which is the counter that is supposed to mean observations are being
        # LOST. An untargeted DO NOTHING treats both as what they are: this turn is already queued.
        #
        # ONE STATEMENT WRITES BOTH KINDS OF ROW. An excluded turn is the same INSERT with
        # status='excluded' and its typed reason, because a second write path for exclusions is a second
        # path that can be forgotten — and a forgotten exclusion is exactly the invisible gap the
        # reconciliation below exists to catch.
        sql = sa_text(
            f"""
            INSERT INTO {TABLE} (user_id, conversation_turn_id, channel, provider_message_id, status,
                                 intake_disposition, router_snapshot, context_flags, reachable_operations)
            VALUES (:uid, CAST(:turn_id AS uuid), :ch, :pmid, :status, :disposition,
                    CAST(:router AS jsonb), CAST(:flags AS jsonb), CAST(:reachable AS jsonb))
            ON CONFLICT DO NOTHING
            """
        )
        async with user_session(user_id) as s:
            res = await s.execute(sql, {"uid": str(user_id), "ch": channel, "pmid": provider_message_id,
                                        "turn_id": (str(conversation_turn_id)
                                                    if conversation_turn_id else None),
                                        "status": status, "disposition": disposition or ELIGIBLE,
                                        "router": json.dumps(router.as_json()),
                                        "flags": json.dumps(context_flags),
                                        # NULL, not '[]': "we could not establish capability truth" is a
                                        # different fact from "this user can reach nothing".
                                        "reachable": (json.dumps(reachable.as_json())
                                                      if reachable.established else None)})
            # Read inside the session: rowcount belongs to the cursor, and 0 here means "already queued",
            # which is a normal outcome (a redelivered webhook), not a failure.
            return bool(res.rowcount)

    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ClaimedShadowJob | None:
        # SKIP LOCKED lets N workers claim concurrently without ever grabbing the same row. Claimable =
        # pending, OR processing/retryable whose lease has expired (a crashed or backed-off job).
        #
        # `attempts < max_attempts` IS THE BOUND, and it is in the SELECT rather than left to the worker
        # because the unbounded path was the one nobody wrote code for: a worker killed between claiming
        # and recording never reaches any worker-side check, so an ever-expiring lease was claimed
        # forever. `attempts = attempts + 1` in the same statement is what makes that path converge —
        # advancing the counter only on a successful record left a crash costing nothing.
        sql = sa_text(
            f"""
            UPDATE {TABLE} SET
                status = '{PROCESSING}',
                lease_owner = :worker,
                lease_token = gen_random_uuid(),
                lease_expires_at = now() + make_interval(secs => :lease),
                attempts = attempts + 1,
                version = version + 1,
                updated_at = now()
            WHERE id = (
                SELECT id FROM {TABLE}
                WHERE attempts < max_attempts
                  AND ((status = '{PENDING}')
                       OR (status IN ('{PROCESSING}', '{RETRYABLE_FAILED}')
                           AND lease_expires_at IS NOT NULL AND lease_expires_at < now()))
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, user_id, channel, provider_message_id, attempts, max_attempts,
                      router_snapshot, context_flags, reachable_operations, lease_owner, lease_token
            """
        )
        async with worker_session() as s:
            row = (await s.execute(sql, {"worker": worker_id[:64], "lease": lease_seconds})).mappings().first()
        if row is None:
            return None
        return ClaimedShadowJob(
            id=row["id"], user_id=row["user_id"], channel=row["channel"],
            provider_message_id=row["provider_message_id"], attempts=row["attempts"],
            max_attempts=row["max_attempts"], router=RouterSnapshot.from_json(row["router_snapshot"]),
            context_flags=dict(row["context_flags"] or {}),
            reachable=ReachableOperations.from_json(row["reachable_operations"]),
            lease_owner=row["lease_owner"], lease_token=str(row["lease_token"]))

    async def trusted_text(self, job: ClaimedShadowJob) -> str | None:
        """The student's words, read from where they actually live, under the OWNER's session.

        Returning None is a real answer, not an error: the turn can have been deleted between the request
        and the drain, and a shadow that resurrected deleted text would be a privacy defect, not a
        thorough observer.
        """
        async with user_session(job.user_id) as s:
            return (await s.execute(
                sa_text("SELECT text FROM conversation_turns WHERE user_id = :uid AND channel = :ch "
                        "AND provider_message_id = :pmid AND role = 'user' LIMIT 1"),
                {"uid": str(job.user_id), "ch": job.channel, "pmid": job.provider_message_id})).scalar()

    async def record(self, job: ClaimedShadowJob, *, outcome: str, status: str,
                     record: ShadowRecord | None) -> bool:
        # ONE UPDATE on the job row — never a second INSERT — so a retried job converges to one record.
        #
        # THE FENCE, and it is four conditions rather than one. `AND lease_owner = :worker` was NOT a
        # fence: the worker id is `cloudrun-<node>-<pid>` and one container runs overlapping /process
        # invocations, so a worker whose lease expired mid-read satisfies it at the same instant as the
        # worker that legitimately reclaimed the row. It could therefore overwrite a newer observation, or
        # RESURRECT a job another worker had already completed, and the row would look perfectly clean
        # afterwards while holding the wrong reading — worse than a missing row, because nothing flags it.
        # `status = processing` blocks the resurrection; `lease_token` blocks the overwrite, and it is the
        # only one of the four a stale worker cannot reproduce.
        async with user_session(job.user_id) as s:
            res = await s.execute(
                sa_text(
                    f"UPDATE {TABLE} SET status = :st, outcome = :outcome, "
                    "observation = CAST(:obs AS jsonb), agrees = :agrees, divergence = :divergence, "
                    "false_capability_denial = :denial, "
                    "latency_ms = :ms, observed_at = now(), last_error = :err, "
                    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                    "version = version + 1, updated_at = now() "
                    f"WHERE id = :id AND user_id = :uid AND status = '{PROCESSING}' "
                    "AND lease_token = CAST(:token AS uuid)"),
                {"id": str(job.id), "uid": str(job.user_id), "st": status, "outcome": outcome,
                 "token": job.lease_token,
                 "obs": json.dumps(record.as_json()) if record else None,
                 "agrees": record.agrees if record else None,
                 "divergence": record.divergence if record else None,
                 "denial": record.false_capability_denial if record else None,
                 "ms": record.exec_latency_ms if record else None,
                 "err": None if outcome == OK else _short(outcome)})
            return bool(res.rowcount)

    async def mark_retryable(self, job: ClaimedShadowJob, *, outcome: str,
                             backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> bool:
        # The outcome is written even mid-retry: an observation stuck retrying transport failures is
        # itself a finding, and a NULL outcome would make it look like an idle row.
        async with user_session(job.user_id) as s:
            res = await s.execute(
                sa_text(
                    f"UPDATE {TABLE} SET status = '{RETRYABLE_FAILED}', outcome = :outcome, "
                    "last_error = :err, lease_expires_at = now() + make_interval(secs => :backoff), "
                    # The same fence as `record`, for the same reason: a worker that lost its lease may
                    # not reschedule a job another worker is already holding.
                    "version = version + 1, updated_at = now() "
                    f"WHERE id = :id AND user_id = :uid AND status = '{PROCESSING}' "
                    "AND lease_token = CAST(:token AS uuid)"),
                {"id": str(job.id), "uid": str(job.user_id), "outcome": outcome,
                 "token": job.lease_token,
                 "err": _short(outcome), "backoff": backoff_seconds})
            return bool(res.rowcount)

    async def terminalize_exhausted(self) -> int:
        """Close out jobs that ran out of attempts without ever producing a recorded outcome.

        A worker that dies between claiming and recording leaves a row at `processing` with an expiring
        lease. `claim` will not take it back once `attempts >= max_attempts`, so without this sweep it
        would sit in the backlog forever and the drain would look permanently behind.

        IDEMPOTENT BY THE WHERE CLAUSE, not by a caller remembering to run it once: the predicate selects
        only non-terminal rows, so a second sweep — a retried Cloud Task, two containers waking together —
        matches nothing and updates zero rows. It takes no lease and calls no provider; it is one UPDATE
        that relabels rows nobody can be holding, which is why it needs no fence of its own.

        The expired-lease condition is what keeps it off a job that is legitimately mid-read on its final
        attempt. `lease_expires_at IS NULL` is included so a row that somehow lost its lease is rescued
        into a typed terminal state instead of counting as backlog for the life of the table.
        """
        async with worker_session() as s:
            res = await s.execute(
                sa_text(
                    f"UPDATE {TABLE} SET status = '{TERMINAL_FAILED}', outcome = :outcome, "
                    "last_error = :err, observed_at = now(), "
                    "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
                    "version = version + 1, updated_at = now() "
                    f"WHERE status IN ('{PROCESSING}', '{RETRYABLE_FAILED}') "
                    "AND attempts >= max_attempts "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < now())"),
                {"outcome": EXHAUSTED, "err": _short(EXHAUSTED)})
            return int(res.rowcount or 0)

    async def counts(self) -> dict:
        # The ages come back from the SAME scan as the tally so the two cannot disagree about the queue
        # they describe. `created_at` for a pending row is how long it has waited to be read at all;
        # `updated_at` for a leased row is how long THIS claim has been held, which is the number that
        # says a worker is stuck rather than that the turn is old.
        async with worker_session() as s:
            rows = (await s.execute(
                sa_text(f"SELECT status, outcome, count(*) AS n, "
                        "max(EXTRACT(EPOCH FROM (now() - created_at))) AS oldest_created_s, "
                        "max(EXTRACT(EPOCH FROM (now() - updated_at))) AS oldest_updated_s "
                        f"FROM {TABLE} GROUP BY status, outcome"))
            ).mappings().all()
        return _tally([(r["status"], r["outcome"], int(r["n"]),
                        float(r["oldest_created_s"] or 0.0), float(r["oldest_updated_s"] or 0.0))
                       for r in rows])

    async def reconcile_window(self) -> LedgerReconciliation:
        """THE WHOLE RECONCILIATION, computed BEHIND the SECURITY DEFINER boundary, returned as numbers.

        THIS IS THE FIX, THREE TIMES OVER, AND THE THIRD TIME IS WHY THIS METHOD EXISTS AT ALL.

        FIRST, THE POPULATION. `worker_api.process` used to assemble the user list out of the jobs
        `claim_and_observe` returned — the only user ids a worker could obtain, because
        `conversation_turns` is tenant-isolated and a worker session saw nothing in it. A user whose
        intake call was MISSING therefore had no job, was never in the list, and was never checked; with
        an empty queue the list was empty and `reconciled: True` was the answer every wake emitted. A
        population derived from the thing being checked cannot produce a failure.

        SECOND, WHAT THE FIRST FIX COST. The first attempt bought the cross-tenant count with a row-level
        policy, `FOR SELECT USING (app_is_worker())`. RLS admits ROWS; it cannot narrow a row to a
        column. So the count was paid for with every column of every student's turn — `text` included,
        and `trusted_text` above is already a `SELECT text FROM conversation_turns`.

        THIRD, WHAT THE SECOND FIX COST. The per-owner function returned ONE ROW PER OWNER. The
        comparison was still finished out here: this method looped those owners and called
        `observed_for(user_id)`, which opened a session per tenant and read that tenant's turns. Column
        exposure closed, ENUMERATION widened — the drain now received every owner in the window,
        including the owners with no jobs, where before it learned an owner id only from a job it had
        claimed. An audit walked that list and read every turn body in the window.

        So migration 0035 moved THE JOIN INSIDE. `shadow_reconciliation(window_start, window_end)`
        inspects the canonical ledger and the shadow bookkeeping together and returns one row of counts
        and a verdict. There is no list to loop, `observed_for` is deleted rather than narrowed, and the
        narrow true statement about this process is: global reconciliation exposes aggregate metadata
        only; raw conversation content remains accessible only through the existing per-job authorized
        read.

        THE BOUNDS ARE STILL THE CALLER'S, AND STILL READ ONCE. `now()` is the transaction timestamp, so
        the single statement below fixes `lo`/`hi` before anything is asked. Computing them inside the
        function would tie them to that call's own `now()`, and the guard query and the reconciliation
        would then describe two different windows — which is exactly where a real gap hides.

        THE WINDOW IS LAGGED. `persist_user_turn` commits the canonical turn and `intake` writes the job
        moments later in a different transaction, so a turn caught between them is legitimately
        unobserved for an instant. Without the lag the check would fail on every wake, and a check that
        always fails is a check nobody reads.

        AND THE AUTHORITY IS PROVED FROM DATABASE STATE BEFORE ANY NUMBER IS BELIEVED. A tenant-isolated
        table returns zero rows and no error to a session that may not see it, so "the ledger is empty"
        and "I cannot see the ledger" are the same observation unless something outside the policy's
        reach says otherwise. `pg_proc`, `pg_roles` and `pg_policies` are outside it. Any failed clause
        reports `unknown` — never `clean`, and never a manufactured zero.
        """
        # THE WINDOW, VALIDATED ON THIS SIDE TOO. The function refuses a window it cannot honour; asking
        # for one is a configuration error, and a configuration error must not read as an unreadable
        # ledger with no explanation.
        if not (RECONCILE_WINDOW_S > 0 and RECONCILE_LAG_S >= 0
                and RECONCILE_WINDOW_S <= MAX_RECONCILE_SPAN_S):
            log.warning("shadow_reconciliation_bad_window window_s=%s lag_s=%s",
                        RECONCILE_WINDOW_S, RECONCILE_LAG_S)
            return LedgerReconciliation(visible=False, unreadable_reason=BAD_WINDOW)

        columns = ", ".join(RECONCILIATION_COUNTS)
        async with worker_session() as s:
            bounds = (await s.execute(
                sa_text("SELECT (now() - make_interval(secs => :lag)) AS hi, "
                        "       (now() - make_interval(secs => :lag) "
                        "               - make_interval(secs => :window)) AS lo"),
                {"lag": RECONCILE_LAG_S, "window": RECONCILE_WINDOW_S})).mappings().first()
            lo, hi = bounds["lo"], bounds["hi"]
            guard = (await s.execute(
                _POPULATION_GUARD,
                {"fn": RECONCILIATION_FUNCTION,
                 "retired": RETIRED_POPULATION_FUNCTION})).mappings().first()
            refusal = _population_refusal(guard)
            if refusal is not None:
                log.warning("shadow_reconciliation_unauthorized reason=%s", refusal)
                return LedgerReconciliation(visible=False, unreadable_reason=refusal,
                                            window_start=lo, window_end=hi)
            # EVERY column named here is a count, a timestamp or a status label from a vocabulary this
            # repo owns. `user_id` and the turn ids are not in the function's return type at all, so this
            # is not a self-restraint that a future edit can quietly widen — see the privilege suite,
            # which reads the declared type out of pg_proc.
            row = (await s.execute(
                sa_text(f"SELECT window_start, window_end, watermark, {columns}, "
                        f"reconciliation_status FROM public.{RECONCILIATION_FUNCTION}(:lo, :hi)"),
                {"lo": lo, "hi": hi})).mappings().first()

        if row is None:
            # An aggregate over an empty window still returns ONE row of zeros, so no row means the
            # function did not do what it claims. Reported as unreadable rather than as a clean, empty
            # hour: that substitution is the original failure of this whole module.
            log.warning("shadow_reconciliation_empty_result")
            return LedgerReconciliation(visible=False, unreadable_reason=NO_RECONCILIATION_ROW,
                                        window_start=lo, window_end=hi)

        return LedgerReconciliation(
            visible=True,
            window_start=row["window_start"], window_end=row["window_end"],
            watermark=row["watermark"],
            counts={name: int(row[name] or 0) for name in RECONCILIATION_COUNTS},
            status=row["reconciliation_status"])

    async def reconcile(self, user_id: UUID) -> dict:
        """Count this user's trusted inbound turns from the CANONICAL LEDGER and hold shadow to it.

        `conversation_turns` with role='user' is that ledger: `conversation_store.persist_user_turn` is
        its only writer and the conversation runtime calls it exactly once per trusted inbound turn,
        before any branch. So it is the one count that does not come from shadow's own bookkeeping —
        which is the entire point. The previous invariant defined `eligible` as the sum of the
        dispositions shadow had recorded, so it was TRUE BY CONSTRUCTION and stayed true while every
        continuation and approval turn skipped the function completely.

        THE MATCH IS ON THE CANONICAL TURN ID, not on a re-derived (channel, provider_message_id). The
        two ledgers are joined on the identity of the thing being counted, so a shadow row that carries
        no canonical id matches no turn and shows up as UNOBSERVED — which is the truth about it.

        BOTH TABLES UNDER THE OWNER'S SESSION, for a caller that already holds this owner's id. The
        narrow true statement about the reconciliation posture is not "the worker never reads a turn
        row": it is that GLOBAL reconciliation exposes aggregate metadata only, and raw conversation
        content remains accessible only through the existing per-job authorized read. This method is a
        per-tenant read of that second kind, and it is deliberately NOT on the global path — nothing in
        `reconcile_population` calls it. The previous arrangement did call a method like this one per
        owner, and needing to is exactly what made the owner list cross the aggregate boundary.

        WHOLE HISTORY, deliberately, and one named owner. The windowed, population-wide form is
        `reconcile_window`, which takes no user id because there is no longer one to take.
        """
        async with user_session(user_id) as s:
            row = (await s.execute(
                sa_text(
                    f"""
                    WITH w AS (
                        SELECT id FROM conversation_turns
                         WHERE user_id = :uid AND role = 'user'
                    )
                    SELECT (SELECT count(*) FROM w) AS turns,
                           count(j.id) FILTER (WHERE j.status <> '{EXCLUDED}') AS eligible,
                           count(j.id) FILTER (WHERE j.status = '{EXCLUDED}') AS excluded
                      FROM w LEFT JOIN {TABLE} j ON j.conversation_turn_id = w.id
                    """),
                {"uid": str(user_id)})).mappings().first()
            by_exclusion = {r["intake_disposition"] or EXCLUDED: int(r["n"]) for r in (await s.execute(
                sa_text(f"SELECT intake_disposition, count(*) AS n FROM {TABLE} "
                        f"WHERE user_id = :uid AND status = '{EXCLUDED}' GROUP BY intake_disposition"),
                {"uid": str(user_id)})).mappings().all()}
        return _reconciliation(int(row["turns"] or 0), int(row["eligible"] or 0),
                               int(row["excluded"] or 0), by_exclusion)


_KNOWN_STATUSES = (PENDING, PROCESSING, RETRYABLE_FAILED, COMPLETED, TERMINAL_FAILED, EXCLUDED)

# An outcome no bucket claims. It has a NAME because it was being computed and dropped: `_tally` routed
# unknown outcomes here and `health()` enumerated a fixed list of keys that did not include it, so a
# typo'd or newly-added outcome vanished from the emission entirely — the same silent-loss shape as the
# rest of this file's history, one level up in the telemetry.
UNCLASSIFIED_OUTCOME = "unclassified_outcome"


def _tally(rows: list[tuple[str, str | None, int, float, float]]) -> dict:
    """(status, outcome, n, oldest_created_s, oldest_updated_s) -> the queue's shape. Shared by both
    stores so the numbers mean one thing.

    `queued` counts everything that has NOT reached a terminal state, which is what makes a dropped or
    stalled observation VISIBLE: a backlog that only grows is the signal that the drain is not running.

    RECONCILIATION IS COMPUTED, NOT ASSUMED. `rows` is the whole table, so `enqueued` is the total row
    count and the invariant `enqueued == pending + leased + terminal` is checked against statuses this
    module actually knows about. A status outside the vocabulary — a half-finished migration, a hand-edited
    row — lands in `unaccounted` instead of quietly disappearing from every bucket, which is exactly how
    dropped work stayed invisible before.
    """
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    oldest_pending = 0.0
    oldest_leased = 0.0
    for status, outcome, n, oldest_created_s, oldest_updated_s in rows:
        by_status[status] = by_status.get(status, 0) + n
        if outcome:
            by_outcome[outcome] = by_outcome.get(outcome, 0) + n
        if status == PENDING:
            oldest_pending = max(oldest_pending, oldest_created_s)
        if status == PROCESSING:
            oldest_leased = max(oldest_leased, oldest_updated_s)

    total = sum(by_status.values())
    excluded = by_status.get(EXCLUDED, 0)
    queued = sum(n for st, n in by_status.items() if st not in _NOT_QUEUED)
    accounted = sum(by_status.get(st, 0) for st in _KNOWN_STATUSES)
    # `unclassified_outcome` is seeded rather than created on demand: a bucket that only appears when it
    # is non-zero cannot be alerted on, and this one existed and was never emitted at all.
    buckets = {b: 0 for b in (*set(HEALTH_BUCKETS.values()), UNCLASSIFIED_OUTCOME)}
    for outcome, n in by_outcome.items():
        buckets[HEALTH_BUCKETS.get(outcome, UNCLASSIFIED_OUTCOME)] = \
            buckets.get(HEALTH_BUCKETS.get(outcome, UNCLASSIFIED_OUTCOME), 0) + n

    return {
        # The per-outcome buckets go FIRST so the status counts below win the one name they share:
        # `completed` is a STATUS, and the `ok` outcome is only supposed to imply it. If those two ever
        # disagree the status is the truth, because it is what `claim` and the terminal writes act on.
        **buckets,
        "queued": queued,
        "pending": by_status.get(PENDING, 0),
        "processing": by_status.get(PROCESSING, 0),
        "leased": by_status.get(PROCESSING, 0),        # the emitted name; `processing` is the status name
        "retryable_failed": by_status.get(RETRYABLE_FAILED, 0),
        "retryable": by_status.get(RETRYABLE_FAILED, 0),
        "completed": by_status.get(COMPLETED, 0),
        "terminal_failed": by_status.get(TERMINAL_FAILED, 0),
        "terminal": by_status.get(COMPLETED, 0) + by_status.get(TERMINAL_FAILED, 0),
        # A turn that was deliberately not observed, with its reason on the row. Counted apart from
        # `enqueued` so the queue invariant below stays a statement about work, not about bookkeeping.
        "excluded": excluded,
        "rows": total,
        "enqueued": total - excluded,
        "outcomes": by_outcome,
        "oldest_pending_age_s": int(oldest_pending),
        "oldest_leased_age_s": int(oldest_leased),
        # enqueued == pending + leased + retryable + terminal, with anything else named rather than lost.
        "unaccounted": total - accounted,
        "reconciled": total == accounted,
    }


# --- reconciliation against the canonical inbound-turn ledger ---------------------------------------

# What a turn's row says shadow DID with it. Stored on the row (`intake_disposition`) rather than only
# counted in memory, because the two ledgers being reconciled must not share a source: an "excluded"
# count that came from the same in-process counter as the "eligible" count proves nothing.
ELIGIBLE = "eligible"


def _reconciliation(turns: int, eligible: int, excluded: int,
                    by_exclusion: dict[str, int] | None = None) -> dict:
    """trusted inbound turns == eligible shadow jobs + explicitly excluded turns.

    `unobserved` is the gap, and it is the whole reason this function does not compute the denominator
    from the numerator. A turn that never reached intake leaves NO row of any kind — no disposition, no
    exclusion, nothing — so the only thing that can notice it is a count taken from somewhere else.
    A missing intake call therefore FAILS this check instead of agreeing with itself.

    `eligible` and `excluded` are counted by JOINING shadow rows to the canonical turns, so they can
    never exceed the turn count: the canonical id is unique per turn. A negative gap is still reported
    (as a non-zero `unobserved` and `reconciled=False`) rather than clamped, because if it ever appears
    the constraint has been lost and that is worth failing on rather than hiding.
    """
    return {"trusted_inbound_turns": turns, "eligible": eligible, "excluded": excluded,
            "observed": eligible + excluded, "unobserved": turns - eligible - excluded,
            "by_exclusion": dict(by_exclusion or {}), "reconciled": turns == eligible + excluded}


# --- the ONE place a reconciliation status is published ----------------------------------------------

# Whether the canonical ledger was demonstrably read. `proven` is the only value that permits a verdict
# of `clean`, and it is a positive proof (the aggregate function exists, is hardened, is executable, no
# row-level grant has reappeared and the retired per-owner function is gone) rather than the absence of
# an error.
LEDGER_PROVEN = "proven"
LEDGER_UNPROVEN = "unproven"

# The counts that mean nothing without a proven read. They are NULL — not zero — whenever the ledger was
# not read, because a zero here says "the sample is complete" at the exact moment nobody can tell. They
# are exactly the function's aggregate columns, named once in `RECONCILIATION_COUNTS`, so a count cannot
# be computed in SQL and then dropped by a hand-written key list on the way out.
DEPENDENT_COUNTS = RECONCILIATION_COUNTS

# The only two verdicts the database is allowed to hand back. Anything else — `unknown`, a typo, a NULL,
# a value from a future migration this build does not understand — is not a verdict this process may
# publish, and it degrades to `unknown` rather than being coerced toward either end.
_DATABASE_VERDICTS = frozenset({RECON_CLEAN, RECON_FAILED})


def reconciliation_verdict(*, ledger_visibility_status: str,
                           database_status: str | None = None,
                           **counts: int | None) -> dict:
    """PUBLISH the database's reconciliation status. This function does not decide one.

    WHY IT NO LONGER DECIDES, WHICH IS A CHANGE AND NOT A DEMOTION. The status used to be computed here
    from counts the worker had assembled, and before that inline at the report site as
    `clean if unobserved == 0 and unreconciled == 0 else failed`. Both are re-derivations, and a
    re-derivation is only ever as good as the numbers that reach it: when the population was empty every
    sum was an honest zero, and the expression read a collapsed population as a clean one.

    Migration 0035 computes the verdict in the SAME STATEMENT and the SAME SNAPSHOT as the counts, from
    anti-joins the caller cannot see. Re-deriving it out here from the summary numbers would be strictly
    worse information pretending to be a second opinion — and the two would agree until the day one of
    them was edited, at which point the disagreement would be silent because both look reasonable. So the
    database's verdict is AUTHORITATIVE and travels through untouched, all the way into the response body
    `worker_api.process` returns.

    WHAT IS STILL ENFORCED HERE, because passthrough is not the same as credulity:

      1. visibility unproven                -> `unknown`, every dependent count NULL. Never `clean`.
         The counts are meaningless without a proven read, and a fabricated zero says the sample is
         complete precisely when nobody can tell.
      2. a status outside the vocabulary    -> `unknown`, every dependent count NULL. This covers the
         database saying `unknown`, saying nothing at all, or saying something a future migration
         introduced that this build does not understand. It is a VALIDATION, not a derivation: it can
         only ever refuse a verdict, and it can never invent or upgrade one.
      3. otherwise                          -> exactly what the database said, with its counts.

    Note what rule 2 deliberately does NOT do. It does not "correct" a `clean` that arrives beside
    `unobserved_turns > 0`, and it does not soften a `failed` whose counts look tidy. Either would be
    this function deciding again, from the weaker numbers, which is the thing that has now gone wrong
    three times. A database verdict that disagrees with its own counts is a defect in the function and
    belongs in a test against the function, not in a silent fix-up at the report site.
    """
    if ledger_visibility_status != LEDGER_PROVEN or database_status not in _DATABASE_VERDICTS:
        return {"reconciliation_status": RECON_UNKNOWN, **{k: None for k in DEPENDENT_COUNTS}}
    return {"reconciliation_status": database_status,
            **{k: int(counts.get(k) or 0) for k in DEPENDENT_COUNTS}}


# --- In-memory (offline tests) ----------------------------------------------------------------------


@dataclass
class _MemTurn:
    """The `conversation_turns` stand-in: an IDENTITY plus the words, never just the words.

    It used to be a `(uid, channel, pmid) -> text` dict, which made the offline reconciliation match jobs
    to turns by re-deriving the provider triple — the same reconstruction the canonical id replaces. A
    fake that models a weaker key than production's cannot fail on the bug production had.
    """

    id: UUID
    user_id: UUID
    channel: str
    provider_message_id: str
    text: str
    created_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


@dataclass
class _MemShadowJob:
    id: UUID
    user_id: UUID
    conversation_turn_id: UUID | None
    channel: str
    provider_message_id: str
    router: RouterSnapshot
    context_flags: dict
    reachable: ReachableOperations = dataclasses.field(default_factory=ReachableOperations)
    intake_disposition: str = ELIGIBLE
    text: str | None = None                 # stands in for the conversation_turns row
    status: str = PENDING
    outcome: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_token: str | None = None          # the per-claim fence, mirrored from Postgres
    lease_expires_at: datetime.datetime | None = None
    observation: dict | None = None
    agrees: bool | None = None
    divergence: str | None = None
    false_capability_denial: bool | None = None
    last_error: str | None = None
    created_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))


class InMemoryShadowStore:
    """Mirrors the Postgres claim/lease/record machine for fast offline tests, including the UNIQUE
    constraint — so "exactly one job per turn" and "a retry converges to one record" are testable without
    standing up Postgres. Time is injected so an expired lease needs no sleep."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str, str], _MemShadowJob] = {}
        self.turns: dict[tuple[str, str, str], _MemTurn] = {}   # the conversation_turns stand-in
        # WHETHER THE LEDGER CAN BE READ AT ALL. Settable so the offline suite can reproduce the state a
        # missing aggregate function produces in Postgres — zero rows, no error — and prove it reports
        # `unknown` rather than a clean empty ledger. Without a way to reach that state offline, the
        # third reconciliation state would only ever be exercised where Postgres is available.
        self.population_visible = True
        # WHY it is invisible, when it is. Defaults to the reason a real deployment would give: the
        # function migration 0035 creates is not there, or this session may not execute it.
        self.unreadable_reason = NO_POPULATION_FUNCTION

    def _key(self, user_id, channel, pmid) -> tuple[str, str, str]:
        return (str(user_id), channel, pmid)

    def add_turn(self, user_id: UUID, *, channel: str, provider_message_id: str, text: str,
                 turn_id: UUID | None = None,
                 created_at: datetime.datetime | None = None) -> UUID:
        """Write a canonical turn and RETURN ITS ID, exactly as `persist_user_turn` now does."""
        turn = _MemTurn(id=turn_id or uuid4(), user_id=user_id, channel=channel,
                        provider_message_id=provider_message_id, text=text,
                        created_at=created_at or datetime.datetime.now(datetime.timezone.utc))
        self.turns[self._key(user_id, channel, provider_message_id)] = turn
        return turn.id

    async def enqueue(self, user_id: UUID, *, channel: str, provider_message_id: str,
                      router: RouterSnapshot, context_flags: dict,
                      reachable: ReachableOperations = ReachableOperations(),
                      disposition: str = "", status: str = PENDING,
                      conversation_turn_id: UUID | None = None) -> bool:
        key = self._key(user_id, channel, provider_message_id)
        if key in self.jobs:                                   # the ingress-dedupe constraint, in memory
            return False
        if conversation_turn_id is not None and any(
                j.conversation_turn_id == conversation_turn_id for j in self.jobs.values()):
            # THE CANONICAL UNIQUE CONSTRAINT, modelled. Without it the fake would happily hold two jobs
            # for one turn whenever the provider metadata differed, and the offline suite would prove
            # nothing about the invariant the database enforces.
            return False
        self.jobs[key] = _MemShadowJob(id=uuid4(), user_id=user_id,
                                       conversation_turn_id=conversation_turn_id, channel=channel,
                                       provider_message_id=provider_message_id, router=router,
                                       context_flags=dict(context_flags), reachable=reachable,
                                       intake_disposition=(disposition or ELIGIBLE), status=status)
        return True

    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, *,
                    now: datetime.datetime | None = None) -> ClaimedShadowJob | None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        for key, j in self.jobs.items():
            # `attempts < max_attempts` mirrors the SQL: an exhausted job is never claimed again, whatever
            # its lease says. Without it here the offline suite could not see the unbounded retry at all.
            claimable = j.attempts < j.max_attempts and (j.status == PENDING or (
                j.status in (PROCESSING, RETRYABLE_FAILED) and j.lease_expires_at is not None
                and j.lease_expires_at < now))
            if not claimable:
                continue
            j.status = PROCESSING
            j.lease_owner = worker_id
            j.lease_token = str(uuid4())      # a NEW token per claim — this is the fence
            j.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
            j.attempts += 1                   # ON CLAIM, so a crash before recording still costs an attempt
            j.updated_at = now
            return ClaimedShadowJob(
                id=j.id, user_id=j.user_id, channel=j.channel,
                provider_message_id=j.provider_message_id, attempts=j.attempts,
                max_attempts=j.max_attempts, router=j.router, context_flags=dict(j.context_flags),
                reachable=j.reachable, lease_owner=j.lease_owner, lease_token=j.lease_token)
        return None

    def _row(self, job: ClaimedShadowJob) -> _MemShadowJob:
        return self.jobs[self._key(job.user_id, job.channel, job.provider_message_id)]

    def _holds_lease(self, job: ClaimedShadowJob, row: _MemShadowJob) -> bool:
        """The same four conditions as the SQL fence. Modelled rather than assumed, because the offline
        suite is where a zombie write is cheap to reproduce: the store the tests run against has to be
        able to REJECT one, or the fence is only proved on the path that needs a database."""
        return (row.id == job.id and row.user_id == job.user_id and row.status == PROCESSING
                and row.lease_token is not None and row.lease_token == job.lease_token)

    async def trusted_text(self, job: ClaimedShadowJob) -> str | None:
        turn = self.turns.get(self._key(job.user_id, job.channel, job.provider_message_id))
        return turn.text if turn is not None else None

    async def record(self, job: ClaimedShadowJob, *, outcome: str, status: str,
                     record: ShadowRecord | None) -> bool:
        j = self._row(job)
        if not self._holds_lease(job, j):
            return False
        j.status, j.outcome = status, outcome
        j.observation = record.as_json() if record else None   # ONE record, overwritten, never appended
        j.agrees = record.agrees if record else None
        j.divergence = record.divergence if record else None
        j.false_capability_denial = record.false_capability_denial if record else None
        j.last_error = None if outcome == OK else outcome
        j.lease_owner = j.lease_token = None
        j.lease_expires_at = None
        j.updated_at = datetime.datetime.now(datetime.timezone.utc)
        return True

    async def mark_retryable(self, job: ClaimedShadowJob, *, outcome: str,
                             backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
                             now: datetime.datetime | None = None) -> bool:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        j = self._row(job)
        if not self._holds_lease(job, j):
            return False
        j.status, j.outcome, j.last_error = RETRYABLE_FAILED, outcome, outcome
        j.lease_expires_at = now + datetime.timedelta(seconds=backoff_seconds)
        j.updated_at = now
        return True

    async def terminalize_exhausted(self, *, now: datetime.datetime | None = None) -> int:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        swept = 0
        for j in self.jobs.values():
            if j.status not in (PROCESSING, RETRYABLE_FAILED) or j.attempts < j.max_attempts:
                continue
            if j.lease_expires_at is not None and j.lease_expires_at >= now:
                continue
            j.status, j.outcome, j.last_error = TERMINAL_FAILED, EXHAUSTED, EXHAUSTED
            j.lease_owner = j.lease_token = None
            j.lease_expires_at = None
            j.updated_at = now
            swept += 1
        return swept

    async def counts(self, *, now: datetime.datetime | None = None) -> dict:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return _tally([(j.status, j.outcome, 1,
                        (now - j.created_at).total_seconds(), (now - j.updated_at).total_seconds())
                       for j in self.jobs.values()])

    def _matched(self, turn_ids: set[UUID]) -> tuple[int, int]:
        """(eligible, excluded) shadow rows JOINED to those canonical turns — the same join as the SQL.

        Matching on the turn id rather than on (channel, provider_message_id) is what makes a job with no
        canonical id count as a MISS here, exactly as the LEFT JOIN does in Postgres.
        """
        matched = [j for j in self.jobs.values()
                   if j.conversation_turn_id is not None and j.conversation_turn_id in turn_ids]
        return (sum(1 for j in matched if j.status != EXCLUDED),
                sum(1 for j in matched if j.status == EXCLUDED))

    async def reconcile_window(self) -> LedgerReconciliation:
        """The WHOLE reconciliation, offline — the same two anti-joins the SQL performs, and the verdict
        decided here beside them exactly as the database decides it beside its own.

        NO PER-OWNER STRUCTURE, deliberately, and this is the property the fake has to model. The real
        function returns one row of counts because a row per owner is what let the worker enumerate the
        ledger; a fake that still handed back a user list would let a re-introduced loop pass offline.

        No window filtering: `_MemTurn.created_at` exists so a test can make the ledger's timestamps
        meaningful, but the fake holds only what a test put in it, so every turn in it is in scope by
        construction.
        """
        if not self.population_visible:
            return LedgerReconciliation(visible=False, unreadable_reason=self.unreadable_reason)

        # ONE ROW PER CANONICAL TURN, labelled by what the bookkeeping says about IT — the LEFT JOIN.
        # Matching on the canonical id and nothing else is what makes a job that lost its reference a
        # MISS here, exactly as it is in SQL.
        by_turn = {j.conversation_turn_id: j for j in self.jobs.values()
                   if j.conversation_turn_id is not None}
        rows = [(t, by_turn.get(t.id)) for t in self.turns.values()]
        queue_states = (PENDING, PROCESSING, RETRYABLE_FAILED, COMPLETED, TERMINAL_FAILED)

        def _n(pred) -> int:
            return sum(1 for t, j in rows if pred(j))

        # ANTI-JOIN A: no intake disposition of any kind. ANTI-JOIN B: an eligible intake holding no job
        # in any known queue state. Counted directly, never as `turns - eligible - excluded`: a
        # subtraction can come out zero from two wrong numbers and a membership test cannot.
        #
        # B READS TWO COLUMNS, exactly as the SQL does. `intake_disposition` is what intake decided;
        # `status` is what the queue holds. A fake that took both sides from `status` would let a row
        # parked in `excluded` with an `eligible` disposition pass offline while the database calls it a
        # gap — and a fake that models a weaker check than production's cannot fail on production's bug.
        def _gap(j) -> bool:
            return j is None or (j.intake_disposition == ELIGIBLE and j.status not in queue_states)

        counts = {
            "canonical_trusted_turns": len(rows),
            "trusted_turns_with_intake": _n(lambda j: j is not None),
            "trusted_turns_without_intake": _n(lambda j: j is None),
            # WHAT INTAKE DECIDED, from the column intake writes it in: a turn is deliberately excluded
            # only when a typed reason says so. A row in the `excluded` status whose disposition still
            # reads `eligible` carries no reason and is a gap, not an exclusion.
            "explicitly_excluded_turns": _n(lambda j: j is not None and j.intake_disposition != ELIGIBLE),
            "eligible_turns": _n(lambda j: j is not None and j.intake_disposition == ELIGIBLE),
            "shadow_jobs": _n(lambda j: j is not None),
            "pending_jobs": _n(lambda j: j is not None and j.status == PENDING),
            "processing_jobs": _n(lambda j: j is not None and j.status == PROCESSING),
            "retryable_jobs": _n(lambda j: j is not None and j.status == RETRYABLE_FAILED),
            "terminal_jobs": _n(lambda j: j is not None and j.status in _TERMINAL),
            "unobserved_turns": _n(_gap),
        }
        now = datetime.datetime.now(datetime.timezone.utc)
        return LedgerReconciliation(
            visible=True, watermark=now, counts=counts,
            status=RECON_FAILED if counts["unobserved_turns"] else RECON_CLEAN)

    async def reconcile(self, user_id: UUID) -> dict:
        """`self.turns` stands in for conversation_turns, exactly as it does for the worker's read of the
        student's words — so the reconciliation is exercised against a ledger the job table does not own
        here too, and a test can make the two disagree without standing up Postgres."""
        ids = {t.id for t in self.turns.values() if str(t.user_id) == str(user_id)}
        eligible, excluded = self._matched(ids)
        by_exclusion: dict[str, int] = {}
        for j in self.jobs.values():
            if j.status == EXCLUDED and j.conversation_turn_id in ids:
                key = j.intake_disposition or EXCLUDED
                by_exclusion[key] = by_exclusion.get(key, 0) + 1
        return _reconciliation(len(ids), eligible, excluded, by_exclusion)


# --- the request path -------------------------------------------------------------------------------


# The dispositions a turn offered to `intake` can end in. Every one is EXPLICIT: a turn cannot leave that
# function without landing in a named bucket. "Disabled" in particular is a disposition and not an
# absence — a kill switch that makes turns silently stop existing is indistinguishable from a bug that
# does, which is the shape of the failure this ledger exists to catch.
ENQUEUED = "enqueued"
DUPLICATE = "duplicate"                          # the unique constraint said this turn is already queued
INELIGIBLE_DISABLED = "ineligible_disabled"      # BRUCE_SEMANTIC_SHADOW is off
INELIGIBLE_NO_MESSAGE_ID = "ineligible_no_message_id"
ENQUEUE_FAILED = "enqueue_failed"                # the insert raised and was swallowed to protect the turn

# --- WHY A TRUSTED TURN MIGHT NOT BE OBSERVED, as a closed vocabulary that is PERSISTED on the row.
#
# The excluded set is deliberately tiny and deliberately structural. It used to be enormous and implicit:
# every resolved continuation, approval, rejection, cancellation and ambiguous goal selection skipped
# shadow because the call sat inside the `else` of the routing branch. Those are the safety-critical
# turns — the ones where Bruce acts — and a sample that omits them over-represents easy turns in exactly
# the direction that argues for granting authority. So work-in-flight turns are ELIGIBLE, and the only
# exclusions left are inputs that are not a student speaking to Bruce in language at all.
EXCLUDED_NO_TRUSTED_TEXT = "excluded_no_trusted_text"
"""A transport or system event: a tapback, a delivery receipt, an attachment with no message. There are
no student words for a language reader to read, and filing it as eligible would record a stream of
`turn_missing` outcomes that look exactly like deleted turns."""

EXCLUDED_UNTRUSTED_ONLY = "excluded_untrusted_only"
"""Every word in the turn was written by somebody else — a pasted quote, a forwarded mail, OCR. The
worker reads `conversation_turns.text` RAW, so observing this turn would hand a stranger's sentences to
the executive as if the student had typed them. That is the precise failure `input_envelope` exists to
prevent, and a telemetry path is not entitled to an exemption from it."""

EXCLUDED_LINK_PROTOCOL = "excluded_link_protocol"
"""A six-character invite code (`messaging_inbound._CODE_RE`). Identity-linking protocol, not language."""

EXCLUDED_PLATFORM_COMMAND = "excluded_platform_command"
"""A slash-prefixed platform command. Addressed to the platform rather than to Bruce, so there is no
reading of it to compare against a router decision."""

_EXCLUSIONS = (EXCLUDED_NO_TRUSTED_TEXT, EXCLUDED_UNTRUSTED_ONLY, EXCLUDED_LINK_PROTOCOL,
               EXCLUDED_PLATFORM_COMMAND)

_DISPOSITIONS = (ENQUEUED, DUPLICATE, *_EXCLUSIONS, INELIGIBLE_DISABLED, INELIGIBLE_NO_MESSAGE_ID,
                 ENQUEUE_FAILED)

_LINK_CODE = re.compile(r"^[A-Za-z0-9]{6}$")


def classify_turn(*, trusted_text: str | None, has_untrusted: bool = False) -> str:
    """ELIGIBLE, or the ONE typed reason this trusted inbound turn is not observed. PURE.

    Called ABOVE the routing branches, before anything knows whether this turn will be routed, continued
    or answered from a pending decision — because that distinction is precisely what must NOT decide
    whether a turn is observed. It is pure so the eligibility rule can be tested exhaustively without a
    runtime, and so the durable write below has exactly one caller and one place to go wrong.
    """
    text = (trusted_text or "").strip()
    if not text:
        # No student words. `has_untrusted` splits the two reasons apart because they are different
        # findings: a tapback is a transport event, and a message that is nothing but a forwarded email
        # is untrusted content the observer must not read as the student's own turn.
        return EXCLUDED_UNTRUSTED_ONLY if has_untrusted else EXCLUDED_NO_TRUSTED_TEXT
    if text.startswith("/") and " " not in text:
        return EXCLUDED_PLATFORM_COMMAND
    if _LINK_CODE.match(text):
        return EXCLUDED_LINK_PROTOCOL
    return ELIGIBLE


@dataclass
class EnqueueLedger:
    """What happened to every turn this PROCESS offered for observation.

    IN-PROCESS ON PURPOSE, and the limitation is stated rather than papered over. `intake` runs in the
    API container and the drain runs in the worker container, so no counter can span them; each process
    emits its own.

    AND IT IS NOT THE RECONCILIATION. This counter can only see turns that reached `intake`; a turn that
    never did leaves nothing here by construction, which is exactly how an entire population stayed
    invisible while these numbers added up perfectly. The claim "every trusted turn was accounted for"
    belongs to `reconcile`, which counts the turn ledger this module does not write.

    Reset only by a test. A process-lifetime counter is the right scope: these numbers answer "is this
    container queueing what it sees", and a rate is read from the deltas between emissions.
    """

    counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: {d: 0 for d in _DISPOSITIONS})

    def record(self, disposition: str) -> str:
        self.counts[disposition] = self.counts.get(disposition, 0) + 1
        return disposition

    @property
    def offered(self) -> int:
        """Every turn this process offered, however it ended.

        DELIBERATELY NOT CALLED `eligible`, and this rename is the point of the whole reconciliation
        change. It used to be `eligible = sum(counts.values())`, which made `eligible == enqueued +
        everything else` an identity: the numerator and the denominator were the same numbers added up
        twice, so `reconciled` was TRUE BY CONSTRUCTION and stayed true while every continuation and
        approval turn never reached this function at all. What this counter can honestly report is what
        this process SAW; whether that is all the turns there were is answered by `reconcile`, against a
        ledger shadow does not write.
        """
        return sum(self.counts.values())

    def as_json(self) -> dict:
        accounted = sum(self.counts.get(d, 0) for d in _DISPOSITIONS)
        return {"offered": self.offered, **{d: self.counts.get(d, 0) for d in _DISPOSITIONS},
                # `unaccounted` is non-zero only if someone adds a disposition without adding it to
                # _DISPOSITIONS — a self-consistency check on this counter, and NOT a claim that the
                # turns it counted were all the turns that happened. That claim belongs to `reconcile`.
                "unaccounted": self.offered - accounted,
                "self_consistent": self.offered == accounted}


_LEDGER = EnqueueLedger()


def ledger() -> EnqueueLedger:
    """This process's intake accounting, for the health emission."""
    return _LEDGER


def reset_ledger() -> None:
    """Tests only — a process-lifetime counter otherwise leaks between them."""
    _LEDGER.counts = {d: 0 for d in _DISPOSITIONS}


async def intake(user_id: UUID, *, channel: str, provider_message_id: str | None, decision: Any,
                 disposition: str = ELIGIBLE, reachable: ReachableOperations | None = None,
                 has_open_goal: bool = False, has_pending_decision: bool = False,
                 conversation_turn_id: UUID | None = None,
                 store: ShadowJobStore | None = None,
                 tally: EnqueueLedger | None = None) -> bool:
    """Record what happens to this turn. ONE INSERT. NO MODEL CALL. Returns True if a JOB was created.

    ONE CALL SITE, UNCONDITIONAL. The previous version was called from inside the `else` of the routing
    branch, so a whole population — every resolved continuation, approval, rejection, cancellation and
    ambiguous goal selection — left no disposition, no row and no trace, and the in-process invariant
    agreed with itself the entire time. The eligibility decision now happens above the branches
    (`classify_turn`) and the write happens once for every trusted turn, whichever lane answered it.

    AN EXCLUDED TURN IS STILL WRITTEN. `disposition` other than ELIGIBLE inserts a row in status
    `excluded` carrying the typed reason, and no worker will ever claim it. An exclusion that left
    nothing behind would be indistinguishable from a turn that was dropped, and telling those two apart
    is the only thing the reconciliation can actually do.

    Each remaining property is load-bearing:

      * NO SEMANTIC READ HAPPENS HERE. The executive is never invoked on a student's turn; a slow or
        failing model therefore cannot add latency to a reply. Awaiting the read inline was the first
        wiring and was wrong — "authority is off" says nothing about latency, and a shadow that makes
        Bruce slower has changed Bruce.
      * THE INSERT IS AWAITED, and that is not the same mistake. It is one bounded statement on a
        connection the turn already holds, and it is the point at which the observation becomes durable.
        Detaching it would restore precisely the loss this table exists to remove.
      * EVERY EXCEPTION IS SWALLOWED. Telemetry that can break a student's turn is worse than no
        telemetry. A failure here costs one observation and is logged as a label.
      * THE KILL SWITCH IS CHECKED FIRST, so "off" means no row is ever created.

    `conversation_turn_id` IS THE IDENTITY OF THE THING BEING COUNTED, and it is why this row can be
    reconciled at all. Passing it is not optional in production — `conversation_runtime` reads it back
    from `persist_user_turn`, which runs above every branch — and omitting it does not fail quietly: the
    row then joins to no turn, so its turn counts as UNOBSERVED and the check goes red. That is the
    intended direction. A missing canonical id degrading to the provider triple would be a silent
    downgrade to the weaker key this column replaced.

    The two context booleans AND the reachable snapshot are captured NOW rather than re-derived at drain
    time: whether a goal was open, and what this user could actually run, are facts about the turn.
    Re-reading either minutes later would compare the executive against a world that has moved on — and
    for capability truth that is not a subtlety, it is the difference between recording that the router
    denied a live capability and recording that it truthfully said Bruce had no hands.
    """
    tally = tally if tally is not None else _LEDGER
    reachable = reachable if reachable is not None else ReachableOperations()
    if not enabled():
        # COUNTED, not ignored. "Shadow is off" and "shadow is on and losing turns" produce the same
        # empty table, and only the ledger tells them apart.
        tally.record(INELIGIBLE_DISABLED)
        return False
    if not provider_message_id:
        # Without the provider's message id there is no turn identifier, so no idempotent row and no way
        # to find the text again. Skipped rather than inserted un-keyed, which would let one turn be
        # observed twice.
        tally.record(INELIGIBLE_NO_MESSAGE_ID)
        log.info("shadow_intake_skipped reason=no_message_id")
        return False
    excluded = disposition != ELIGIBLE
    try:
        store = store or PostgresShadowStore()
        # A RouterSnapshot even for an excluded turn: the lane that answered it is a label, it costs
        # nothing, and "which lane produced the turns nobody observes" is a question worth being able
        # to answer without a redeploy.
        created = await store.enqueue(
            user_id, channel=channel, provider_message_id=provider_message_id,
            router=RouterSnapshot.of(decision) if decision is not None else RouterSnapshot(),
            context_flags={"has_open_goal": bool(has_open_goal),
                           "has_pending_decision": bool(has_pending_decision)},
            # An excluded turn gets NO snapshot: capability truth was never asked for, and writing an
            # empty one would claim we looked.
            reachable=(ReachableOperations() if excluded else reachable),
            disposition=disposition, status=(EXCLUDED if excluded else PENDING),
            # THE CANONICAL TURN, carried for BOTH kinds of row. An excluded turn needs it just as much
            # as an eligible one: reconciliation joins on this id, and an exclusion that cannot be joined
            # back to its turn reads as a dropped turn — which is the single thing the exclusion status
            # exists to prevent.
            conversation_turn_id=conversation_turn_id)
        tally.record((disposition if excluded else ENQUEUED) if created else DUPLICATE)
        # CONTENT-FREE, and it carries the running tally because THIS is the request side's emission
        # point: the worker cannot see these numbers (different container, different process), so a
        # health line that only the worker emitted would report `offered=0` forever and the intake
        # accounting would be unreadable in the process that actually decides it.
        log.info("shadow_intake disposition=%s created=%s reachable=%d established=%s offered=%d "
                 "enqueued=%d duplicate=%d disabled=%d no_message_id=%d failed=%d",
                 disposition, created, len(reachable.operations), reachable.established, tally.offered,
                 tally.counts[ENQUEUED], tally.counts[DUPLICATE], tally.counts[INELIGIBLE_DISABLED],
                 tally.counts[INELIGIBLE_NO_MESSAGE_ID], tally.counts[ENQUEUE_FAILED])
        return bool(created and not excluded)
    except Exception:
        tally.record(ENQUEUE_FAILED)
        log.info("shadow_intake_failed")   # never the exception text: it can echo the student's message
        return False


# --- the worker -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedJob:
    """What one claim produced. Returned for telemetry; nothing in the runtime consumes it."""

    job_id: UUID
    outcome: str
    status: str
    # WHOSE turn this was, for the drain's own logging. IT IS NOT A RECONCILIATION POPULATION, and that
    # sentence used to be the opposite: `worker_api.process` collected these ids and handed them to
    # `health()` as the set of users to check, which meant a user whose intake never ran had no job, no
    # id here, and no check. The reconciliation reads the canonical ledger itself now
    # (`reconcile_window`), and never receives a user id at all; this field must never become an input
    # to it again.
    user_id: UUID | None = None
    agrees: bool | None = None
    divergence: str | None = None
    latency_ms: int | None = None
    false_capability_denial: bool = False
    # Did the write actually land, or was this worker fenced out by a newer claim? False is not an error
    # — the job belongs to someone else and they will record it — but reporting it as an observation
    # would count a turn that was never stored.
    recorded: bool = True


class _RecordingTriage:
    """Wraps the Stage-1 provider so the shadow can NAME why a read failed.

    `semantic_executive.interpret` deliberately never raises — an unreadable turn is a question, not an
    outage — so by the time it returns, the reason has become a sentence in a validation note. A shadow
    whose failure column said "clarify" would be exactly the bias the outbox removes, so the reason is
    captured at the seam where it is still typed, using the SAME classifier the router path uses.

    Three signals, and the third is why `returned` exists. A model that ANSWERS with something unusable
    raises nothing the wrapper sees (the answer falls over later, when triage reads its confidence), and
    without `returned` that case is indistinguishable from a deadline — an unparseable answer would have
    been filed as `timeout`, which points an investigation at latency instead of at the output contract.
    A real timeout leaves both unset: `asyncio.wait_for` cancels the read, and CancelledError is not an
    Exception, so "nothing returned and nothing raised" IS the timeout signature.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failure: str | None = None
        self.returned = False
        self.provider = getattr(inner, "provider", None)
        self.model = getattr(inner, "model", None)

    async def read(self, body):
        from . import semantic_triage
        try:
            out = await self._inner.read(body)
        except Exception as exc:
            self.failure = semantic_triage.classify_failure(exc)
            raise
        self.returned = True
        return out


async def claim_and_observe(*, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                            store: ShadowJobStore | None = None,
                            triage: Any = None) -> ObservedJob | None:
    """Claim ONE queued turn, read it with the executive under a hard budget, and store the result.

    Returns None when the queue is idle (or shadow is off), so a drain loop can stop. Safe to retry: the
    claim is atomic under a lease, the observation is an UPDATE on the claimed row, and a crash mid-read
    leaves an expired lease that any worker reclaims.

    KILL SWITCH. With `BRUCE_SEMANTIC_SHADOW` off, an already-queued job NO-OPS: it is not claimed, no
    model is called, and the row is left exactly as it is. Deliberately not "drain it as skipped" — that
    would destroy queued turns that the switch was only temporarily off for, and `counts()` keeps the
    backlog visible in the meantime.

    Failures here are NOT swallowed. This is a worker, not a student's turn: a swallowed infrastructure
    error would be indistinguishable from an empty queue, which is the exact shape of bug that made
    dropped observations invisible in the first place. The drain loop counts and reports them.
    """
    import asyncio
    import time

    from . import semantic_executive, semantic_triage

    if not enabled():
        return None
    store = store or PostgresShadowStore()
    job = await store.claim(worker_id, lease_seconds)
    if job is None:
        return None

    text = await store.trusted_text(job)
    if not text:
        # The turn is gone or was never persisted. Terminal, not retryable — waiting will not bring it
        # back — and recorded as its own outcome so "we observed nothing" never looks like agreement.
        landed = await store.record(job, outcome=TURN_MISSING, status=TERMINAL_FAILED, record=None)
        log.info("shadow_job outcome=%s recorded=%s", TURN_MISSING, landed)
        return ObservedJob(job_id=job.id, user_id=job.user_id, outcome=TURN_MISSING,
                           status=TERMINAL_FAILED, recorded=landed)

    flags = job.context_flags or {}
    # THE TURN'S OWN CAPABILITY TRUTH, from the row — never `tool_registry.specs(None)`, never a fresh
    # broker call. `operations` is passed EXPLICITLY (an empty tuple included) because the default is the
    # global live table: nine capabilities identical for every user, which is what made a student with no
    # Google connection produce a false capability denial against a router that had told them the truth.
    # An unestablished snapshot therefore yields no live families, and understanding may describe what the
    # student wants but may not name an operation — the safe direction, and the honest one.
    context = semantic_executive.mini_context(
        text, has_open_goal=bool(flags.get("has_open_goal")),
        has_pending_decision=bool(flags.get("has_pending_decision")),
        operations=tuple(sorted(job.reachable.operations)))

    turn, outcome, elapsed_ms = None, None, 0
    t0 = time.perf_counter()
    try:
        recorder = _RecordingTriage(triage or semantic_triage.default_provider())
        # THE HARD BUDGET, still. Nobody is waiting on this read, but a hung one would hold the lease and
        # the container; the ceiling makes "stuck" resolve into a recorded outcome instead of silence.
        turn = await asyncio.wait_for(semantic_executive.interpret(context, triage=recorder),
                                      timeout=SHADOW_BUDGET_S)
        # `reading is None` is the executive's structural signal that no usable model read happened — a
        # fact, not a parsed sentence. The recorder then says WHICH failure it was: an answer that came
        # back and still produced no reading is an unusable answer; an exception is whatever the shared
        # classifier calls it; neither means the deadline simply elapsed.
        if getattr(turn, "reading", None) is not None:
            outcome = OK
        elif recorder.returned:
            outcome = INVALID_SCHEMA
        else:
            outcome = recorder.failure or TIMEOUT
    except asyncio.TimeoutError:
        outcome = BUDGET_EXCEEDED
    except Exception as exc:
        # `interpret` is documented never to raise; if it ever does, the shadow still names the failure
        # instead of losing the row to an unhandled error.
        outcome = semantic_triage.classify_failure(exc)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if outcome in RETRYABLE_OUTCOMES and job.can_retry:
        # INFRASTRUCTURE ONLY. A timeout or a rejection is a measurement of the executive and is kept as
        # it fell; retrying those until they succeed is the sampling bias this outbox exists to remove.
        landed = await store.mark_retryable(job, outcome=outcome)
        log.info("shadow_job outcome=%s status=%s attempt=%d ms=%d recorded=%s", outcome,
                 RETRYABLE_FAILED, job.attempts, elapsed_ms, landed)
        return ObservedJob(job_id=job.id, user_id=job.user_id, outcome=outcome,
                           status=RETRYABLE_FAILED, latency_ms=elapsed_ms, recorded=landed)

    record = None
    if outcome == OK:
        # THE COMPARISON USES THE PERSISTED SNAPSHOT, not a worker-time answer. Re-asking the broker here
        # would be cheap and wrong: an integration connected or revoked between the request and the drain
        # would rewrite what this row says happened on a turn that is already over, and the row is the
        # only evidence the authority decision will ever have.
        cmp = compare(turn, job.router, reachable=job.reachable.operations)
        record = ShadowRecord(
            user_id=str(job.user_id), channel=job.channel, message_id=job.provider_message_id,
            exec_mode=turn.mode.value, exec_operation=turn.proposed_operation_id,
            exec_polarity=turn.operation_polarity, exec_confidence=turn.confidence,
            exec_goal_id=turn.target_goal_id,
            # COUNT AND CODES, never the model's prose — see ShadowRecord.as_json.
            exec_missing_count=len(turn.missing_information or ()),
            exec_validation_codes=tuple(turn.validation_codes), exec_latency_ms=elapsed_ms,
            router_class=job.router.execution_class, router_action=job.router.action,
            router_domain=job.router.domain, router_capabilities=job.router.candidate_capabilities,
            router_source=job.router.source, agrees=cmp.agrees, divergence=cmp.divergence,
            false_capability_denial=cmp.false_capability_denial,
            denied_operation_id=cmp.denied_operation_id, denial_reason=cmp.denial_reason,
            reachable_established=job.reachable.established,
            reachable_count=len(job.reachable.operations))

    status = status_for(outcome, can_retry=job.can_retry)
    landed = await store.record(job, outcome=outcome, status=status, record=record)
    # CONTENT-FREE. The row carries the reading (RLS-scoped, and it references rather than copies the
    # turn); the LOG carries only labels, because logs travel further and are read by more people.
    log.info("shadow_job outcome=%s status=%s agrees=%s divergence=%s denial=%s ms=%d recorded=%s",
             outcome, status, record.agrees if record else None,
             record.divergence if record else None,
             record.false_capability_denial if record else None, elapsed_ms, landed)
    return ObservedJob(job_id=job.id, user_id=job.user_id, outcome=outcome, status=status,
                       agrees=record.agrees if record else None,
                       divergence=record.divergence if record else None, latency_ms=elapsed_ms,
                       false_capability_denial=bool(record and record.false_capability_denial),
                       recorded=landed)


# --- measurement ------------------------------------------------------------------------------------


async def counts(store: ShadowJobStore | None = None) -> dict:
    """Queued / completed / per-outcome numbers for the whole queue.

    The point of publishing these is that a DROPPED observation is visible: a backlog that only grows
    says the drain is not running, and an outcome breakdown says whether the reads that did happen are
    healthy. Deliberately NOT wrapped in a try/except that returns zeros — a metric that reports an empty
    queue when it actually failed to read is worse than no metric, and is the exact failure shape that
    let best-effort shadow look fine while losing records.
    """
    return await (store or PostgresShadowStore()).counts()


async def backlog(store: ShadowJobStore | None = None) -> int:
    """How many turns are waiting to be observed (everything not yet terminal, excluded rows aside).

    Read by the worker BEFORE it drains, which is what makes it a different number from the `queued` in
    the health block emitted after: the two together say whether the wake actually made progress, and a
    drain that keeps up with arrivals looks identical to one that is doing nothing if you only ever read
    the backlog it leaves behind.
    """
    return int((await counts(store))["queued"])


async def reconcile(user_id: UUID, store: ShadowJobStore | None = None) -> dict:
    """Hold this user's shadow rows to the CANONICAL inbound-turn ledger.

        trusted inbound turns == eligible shadow jobs + explicitly excluded turns

    The denominator is `conversation_turns` (role='user'), which shadow does not write. That is the whole
    design: the previous check defined `eligible` as the sum of shadow's own dispositions, so it could
    not fail, and it did not fail while an entire population of turns never reached intake at all. A
    missing intake call now shows up here as `unobserved > 0`.

    ONE USER, and this function was never the bug. It is honest and always was. What was dishonest was
    who it got CALLED FOR — see `reconcile_population`, which no longer lets a caller choose.
    """
    return await (store or PostgresShadowStore()).reconcile(user_id)


async def reconcile_population(store: ShadowJobStore | None = None) -> dict:
    """THE INDEPENDENT CHECK, over a population the checked thing did not choose and this process cannot see.

    The canonical inbound-turn ledger is held to the shadow bookkeeping over a fixed, lagged window,
    INSIDE the SECURITY DEFINER function, and what comes back is counts and a verdict. Nothing here is
    derived from claimed jobs, completed jobs, shadow dispositions or a caller-supplied list; `health()`
    deliberately takes no population at all, because a parameter is exactly how the population came to be
    derived from the jobs in the first place.

    AND NO OWNER IDS COME BACK EITHER, which is the change migration 0035 makes. The previous function
    returned one row per owner and this function looped them, opening a session per tenant to finish the
    join — so the drain held every owner in the window, including the owners with no jobs. There is now
    nothing to loop: the join happens where the data already is, and only the verdict leaves.

    THREE STATES, AND THIS FUNCTION DECIDES NONE OF THEM:

      `clean`    the database read both ledgers and found no gap. Zero turns in the window is clean too —
                 but only once visibility has said the zero is real.
      `failed`   the database found a trusted turn with no intake disposition, or an eligible intake with
                 no shadow job. Both are anti-joins, computed there, in one snapshot.
      `unknown`  the call raised, ran without a proven authority, or answered with something outside the
                 verdict vocabulary. NEVER `clean`, and the counts are NULL rather than zero — a
                 fabricated zero would say the sample is complete at the exact moment nobody can tell.

    Every value below is a count, a timestamp, or a label from a vocabulary this repo owns. No user id,
    no turn id and no message text can reach it, so the block is safe in a log and in a response body.
    """
    store = store or PostgresShadowStore()

    def _block(verdict: dict, *, report: LedgerReconciliation | None, reason: str | None) -> dict:
        """The verdict, plus the window metadata that makes its numbers interpretable. NOTHING here
        recomputes, upgrades or downgrades a status: the database decided it, `reconciliation_verdict`
        validated the vocabulary, and this function is a serializer."""
        return {
            **verdict,
            "window_start": _iso(report.window_start) if report else None,
            "window_end": _iso(report.window_end) if report else None,
            # WHEN the database computed this, so a reader can tell a quiet hour from a stale answer.
            "watermark": _iso(report.watermark) if report else None,
            "unreadable_reason": reason,
        }

    try:
        report = await store.reconcile_window()
    except Exception as exc:
        # NOT swallowed into zeros. The type only — a ledger error can quote the row that caused it.
        log.warning("shadow_reconciliation_failed err=%s", type(exc).__name__)
        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_UNPROVEN),
                      report=None, reason=type(exc).__name__)
    if not report.visible:
        log.warning("shadow_reconciliation_invisible reason=%s", report.unreadable_reason)
        return _block(reconciliation_verdict(ledger_visibility_status=LEDGER_UNPROVEN),
                      report=report, reason=report.unreadable_reason)

    # THE DATABASE'S VERDICT, CARRIED. It was decided in the same statement and the same snapshot as
    # these counts, from anti-joins nothing out here can see. Re-deriving it from the summary numbers
    # would be weaker information wearing the authority of a second opinion — and that re-derivation is
    # precisely what read a collapsed population as a clean one, twice.
    verdict = reconciliation_verdict(ledger_visibility_status=LEDGER_PROVEN,
                                     database_status=report.status, **report.counts)
    return _block(verdict, report=report, reason=None)


def _iso(ts: datetime.datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


async def sweep_exhausted(store: ShadowJobStore | None = None) -> int:
    """Close out every job that ran out of attempts without producing a reading. Returns how many.

    Called once per worker wake, BEFORE the drain, so an exhausted row is relabelled rather than left in
    the backlog. It performs no read, takes no lease, calls no provider and touches no goal — it is the
    bookkeeping half of the retry bound, and running it twice is a no-op (see terminalize_exhausted).
    """
    if not enabled():
        # The kill switch means "do not touch the queue", including this. A sweep under a disabled switch
        # would terminalize jobs that were only waiting for the switch to come back on.
        return 0
    return await (store or PostgresShadowStore()).terminalize_exhausted()


QUEUE_OK = "ok"
QUEUE_UNKNOWN = "unknown"


async def health(store: ShadowJobStore | None = None, *, tally: EnqueueLedger | None = None) -> dict:
    """The PII-FREE shadow health numbers, for the worker's telemetry path.

    `counts()` and `backlog()` existed with ZERO production callers, which meant the durable queue was
    measurable in principle and measured nowhere — and an outbox whose backlog nobody looks at has the
    same failure mode as the detached task it replaced, just slower. Both are wired now: the worker reads
    `backlog()` before the drain and emits this block after it (`worker_api.process`).

    THREE SCOPES, NAMED, because they genuinely are three:

      `intake`  what THIS process did with the turns it was offered (offered / enqueued / each explicit
                exclusion and ineligible reason). Process-scoped by necessity: intake runs in the API
                container and the drain in the worker, and no in-memory counter crosses that. On the
                worker every number here is legitimately zero — the worker is offered no turns — and the
                request side emits its own on the `shadow_intake` line. Its `self_consistent` flag is a
                check on the counter, deliberately NOT a claim that it saw every turn.
      `queue`   the durable table, table-wide and cross-user, which is the only ledger both containers
                share.
      `turns`   THE INDEPENDENT ONE, and the only one that can catch a turn that never reached intake:
                trusted inbound turns counted from `conversation_turns` against the rows shadow wrote,
                over a population read from that ledger rather than handed in. It takes NO argument —
                `reconcile_users` used to be a parameter, and the caller filled it from the jobs the
                drain had returned, which made the check unable to see the users it existed to find.

    NEITHER BLOCK FAKES A ZERO. `queue.status` is `unknown` when the tally could not be read and `turns.
    reconciliation_status` is `unknown` when the ledger could not be read, and in both cases the numbers
    are ABSENT rather than zeroed. "The queue is empty" and "I could not read the queue" being the same
    emission is the original failure of this whole module, and a health block is the last place it should
    reappear.

    Every value is a count, an age in seconds, an id, a timestamp, a label from a vocabulary this repo
    owns, or a boolean — nothing here can carry a student's words.
    """
    try:
        q = await counts(store)
    except Exception as exc:
        log.warning("shadow_counts_failed err=%s", type(exc).__name__)
        # NO NUMBERS AT ALL, not zeros. A dashboard reading this must be unable to draw a healthy empty
        # queue out of a failed read, and the only way to guarantee that is to publish nothing to draw.
        queue = {"status": QUEUE_UNKNOWN}
    else:
        queue = {
            "status": QUEUE_OK,
            "rows": q["rows"], "enqueued": q["enqueued"], "excluded": q["excluded"],
            "pending": q["pending"],
            # `processing` is the STATUS the row actually holds and `leased` is what this block has always
            # called it. Both are emitted: renaming a published metric silently is how a dashboard starts
            # reading zero without anything failing.
            "processing": q["processing"], "leased": q["leased"],
            "retryable": q["retryable"], "terminal": q["terminal"],
            "completed": q["completed"], "malformed": q["malformed"],
            # Singular and plural for the same reason as processing/leased — `model_failed` /
            # `infrastructure_failed` are the names the outcome buckets are read by downstream.
            "model_failed": q["model_failures"], "model_failures": q["model_failures"],
            "infrastructure_failed": q["infrastructure_failures"],
            "infrastructure_failures": q["infrastructure_failures"],
            "turn_missing": q["turn_missing"], "exhausted": q["exhausted"],
            # Emitted EXPLICITLY. It was computed in `_tally` and then dropped by a hand-written key list
            # here, so an outcome outside the bucket table disappeared from the emission entirely — a
            # metric that silently omits the thing it cannot classify is the failure this file is about.
            UNCLASSIFIED_OUTCOME: q[UNCLASSIFIED_OUTCOME],
            "queued": q["queued"],
            "oldest_pending_age_s": q["oldest_pending_age_s"],
            "oldest_processing_age_s": q["oldest_leased_age_s"],
            "oldest_leased_age_s": q["oldest_leased_age_s"],
            "unaccounted": q["unaccounted"], "reconciled": q["reconciled"],
        }
    return {
        "intake": (tally if tally is not None else _LEDGER).as_json(),
        "queue": queue,
        "turns": await reconcile_population(store),
    }
