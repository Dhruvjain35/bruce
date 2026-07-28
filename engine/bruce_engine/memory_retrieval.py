"""Memory retrieval — two stages, and the second one is not allowed to be a model call yet.

THE FAILURE THIS IS SHAPED AGAINST. The obvious way to use memory is to load what Bruce knows and put it
in the prompt. That produces a system that gets slower and less accurate the longer someone uses it: the
context fills with true, irrelevant facts, and the model's attention is spent on a teacher's name while
the student is asking about a bus time. Retrieval's job is not "find what is true" — it is "find what
could change THIS decision", and everything outside that set is a cost with no benefit.

STAGE 1 IS DETERMINISTIC AND INDEXED, AND IT IS FIRST FOR A REASON. Nearly every real question is already
scoped by something exact — the entity the turn resolved to, the domain a mission is running in, what is
live right now — and those scopes are index lookups, not similarity. Starting with similarity would mean
the guard rails (right student, still true, not forgotten) become filters applied AFTER a ranking that
has already decided what matters, which is the arrangement in which one of them eventually gets dropped
and nobody notices until a forgotten fact turns up in a reply.

STAGE 2 IS ALSO DETERMINISTIC, FOR NOW. A remote call would put a model on the hot path of every turn to
reorder six rows, against a 100ms p95 budget for the whole of retrieval. `rank()` is the seam a learned
ranker replaces — and only when measured recall says the ordering is what is holding quality back, which
the harness reports rather than an intuition. There is no stub reranker here: a stub that returns its
input unchanged is indistinguishable from a working one in every test that is not specifically looking
for it, and that is the kind of thing that ships.

CROSS-USER RETRIEVAL IS NOT EXPRESSIBLE. Three independent mechanisms, because this is the one failure
with no acceptable rate: `MemoryRetriever` is frozen with a required `user_id` and no method that accepts
another one; every query opens `db.user_session(user_id)`, which sets `app.user_id` transaction-locally,
and also filters on it; and `memory_records` runs FORCE ROW LEVEL SECURITY, so a query that lost its
WHERE clause still returns nothing.

MEMORY NEVER AUTHORIZES. Nothing here produces consent and nothing here is consulted by the execution
gate. Memory can tell Bruce who "coach" is; only the student's own trusted words in the current turn can
tell Bruce to email him. That separation is why this module does not import `authorization_evidence`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import or_, select

from . import memory_record as mr
from . import schema
from .db import user_session

log = logging.getLogger("bruce.memory.retrieval")   # CONTENT-FREE: ids, counts, latencies

RETRIEVAL_VERSION = "r1-deterministic"

# Token budgets by what the turn is doing. Not one number: the cost of an extra fact is not the same in a
# two-word chat reply as in a multi-domain action turn.
BUDGET_CONVERSATION = 300
BUDGET_ACTION = 500
BUDGET_COMPLEX = 900

# Stage 1 pulls at most this many rows before ranking. A cap rather than a page: the ranker is O(n) and
# the query is indexed, so an unbounded shortlist only buys a slow turn on the account that has used
# Bruce longest — precisely the account that must not get slower.
SHORTLIST_LIMIT = 60

# The ONE definition of "a row ordinary response generation may see". `quarantined` is deliberately
# absent: a record held for review must be structurally unreachable, and keeping the definition in one
# place is what stops a new query from remembering four of the five exclusions.
RETRIEVABLE = ("active",)

_FRESHNESS_WEIGHT = {"fresh": 1.0, "aging": 0.7, "stale": 0.25, "expired": 0.0}
_SOURCE_TRUST = {"trusted_user_text": 1.0, "provider": 0.8, "forwarded": 0.5, "quoted": 0.5,
                 "attachment": 0.5, "model": 0.2}


def _est_tokens(s: str) -> int:
    """The same crude ratio `context_compiler` uses, deliberately rather than a real tokenizer: the
    budget is a guard rail, and one that costs a tokenizer call per candidate is a worse trade than one
    that is 15% out."""
    return max(1, len(s) // 4)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TurnCue:
    """The deterministic description of what this turn is about, built from state the runtime already
    has. Never by asking a model what might be relevant — that would be a second inference on the hot
    path, to decide what to feed the first one."""
    text: str = ""
    domains: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()          # raw subject strings mentioned or resolved this turn
    subjects: tuple[str, ...] = ()          # canonical subjects (SELF, a person, an entity key)
    active_run_domain: str | None = None
    has_pending_decision: bool = False
    turn_class: str = "conversation"        # conversation | action | complex

    def budget(self) -> int:
        return {"action": BUDGET_ACTION, "complex": BUDGET_COMPLEX}.get(
            self.turn_class, BUDGET_CONVERSATION)

    def keys(self) -> tuple[str, ...]:
        raw = list(self.entities) + list(self.subjects)
        return tuple(dict.fromkeys(k for k in (mr.entity_key(x) for x in raw) if k))

    def fingerprint(self) -> str:
        """What makes two turns the same retrieval question. The cache keys on this, so it must contain
        everything `shortlist` and `rank` read and nothing they do not — including the free text would
        make every turn a cache miss."""
        return "|".join((self.turn_class, ",".join(sorted(self.domains)), ",".join(sorted(self.keys())),
                         self.active_run_domain or "", "p" if self.has_pending_decision else ""))


@dataclass(frozen=True)
class MemoryContextItem:
    memory_id: str
    fact: str
    kind: str
    confidence: float
    freshness: str
    provenance: str                 # human-readable; never a table name or a column
    reason_relevant: str
    applicable_entity: str | None
    token_cost: int
    score: float = 0.0


@dataclass(frozen=True)
class MemoryContext:
    """What one turn is allowed to know, plus the accounting for what it was NOT shown.

    `omitted_count` and `stale_count` exist because a context that silently drops things is impossible to
    debug: "Bruce forgot my teacher's name" and "Bruce was never shown it" look identical from outside
    and have entirely different fixes.
    """
    items: tuple[MemoryContextItem, ...] = ()
    omitted_count: int = 0
    retrieval_latency_ms: float = 0.0
    token_count: int = 0
    stale_count: int = 0
    retrieval_version: str = RETRIEVAL_VERSION
    cache_hit: bool = False

    def render(self) -> str:
        if not self.items:
            return ""
        return "\n".join(["what i know about you that matters here:"]
                         + [f"- {i.fact} ({i.provenance})" for i in self.items])

    def ids(self) -> tuple[str, ...]:
        return tuple(i.memory_id for i in self.items)


# --- stage 1: deterministic candidate selection ---------------------------------------------------------

async def shortlist(user_id: UUID, cue: TurnCue, *, limit: int = SHORTLIST_LIMIT,
                    now: datetime | None = None) -> list:
    """The indexed query: owner, still believed, not expired, and plausibly about this turn."""
    now = now or datetime.now(timezone.utc)
    R = schema.MemoryRecordRow
    q = (select(R)
         .where(R.user_id == user_id, R.status.in_(RETRIEVABLE), R.forgotten_at.is_(None))
         # STYLE IS NOT A FACT. Style records are about the student and therefore carry the SELF entity
         # key, which makes them permanent candidates for every turn — so excluding them by kind here is
         # what keeps "writes in lowercase" out of a context the model reasons over. The separation has
         # to be in the query, not in the caller: a factual retriever that COULD return a style record is
         # one refactor away from Bruce concluding something about a person from how they type.
         .where(R.kind != "style")
         # Expiry is enforced at READ time as well as by any sweeper. A record whose window closed an
         # hour ago has not been marked `expired` by anything yet, and showing it would be showing a fact
         # Bruce itself considers stale.
         .where(or_(R.expires_at.is_(None), R.expires_at > now)))

    keys = cue.keys()
    domains = tuple(d for d in (*cue.domains, cue.active_run_domain) if d)
    if keys or domains:
        # Facts about the student themselves are ALWAYS candidates. Timezone, preferred name and
        # notification timing can change any turn's answer, and no turn mentions them by name.
        scope = [R.entity_key == mr.SELF]
        if keys:
            scope.append(R.entity_key.in_(keys))
        if domains:
            scope.append(R.domain.in_(domains))
        q = q.where(or_(*scope))

    q = q.order_by(R.observed_at.desc()).limit(limit)
    async with user_session(user_id) as s:
        return list((await s.execute(q)).scalars().all())


# --- stage 2: relevance ranking --------------------------------------------------------------------------

def _reason(row, cue: TurnCue) -> str:
    """Why this fact could change THIS turn. Carried on the item so a human reading a trace can tell
    whether retrieval was wrong or the model ignored what it was handed."""
    keys = cue.keys()
    if row.entity_key and row.entity_key in keys and row.entity_key != mr.SELF:
        return f"this turn is about {row.subject}"
    if row.domain and row.domain == cue.active_run_domain:
        return f"work is running in {row.domain}"
    if row.domain and row.domain in cue.domains:
        return f"the turn touches {row.domain}"
    if row.entity_key == mr.SELF:
        return "a standing fact about you"
    return row.reason_it_matters or "recently learned"


def score(row, cue: TurnCue, *, now: datetime) -> float:
    """Deterministic relevance. Every term is something the runtime knows for certain.

    Ordered by how much each predicts "this could change the answer": naming the entity outranks sharing
    a domain, which outranks being a standing fact, which outranks being recent. Confidence, freshness
    and source trust are MULTIPLIERS rather than additions — a stale third-hand fact should be pushed
    down the list, not merely given a smaller bonus.
    """
    keys = cue.keys()
    base = 0.0
    if row.entity_key and row.entity_key in keys and row.entity_key != mr.SELF:
        base += 3.0
    if row.domain and row.domain == cue.active_run_domain:
        base += 2.0
    elif row.domain and row.domain in cue.domains:
        base += 1.5
    if row.entity_key == mr.SELF:
        base += 1.0
    if row.kind == "profile":
        base += 0.5
    if base == 0.0:
        base = 0.2          # eligible but unconnected: rank it honestly, do not pretend it scores

    age_days = max(0.0, (now - _aware(row.observed_at)).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + age_days / 30.0)
    return (base * (0.5 + 0.5 * recency) * max(0.05, row.confidence)
            * _SOURCE_TRUST.get(row.source_type, 0.5)
            * _FRESHNESS_WEIGHT.get(row.freshness_class, 0.5))


def rank(rows: list, cue: TurnCue, *, now: datetime) -> list:
    """THE SEAM. Total and pure: candidates in, candidates ordered, no I/O. A learned ranker replaces
    exactly this, and may reorder or drop — never widen the scope stage 1 decided on."""
    return sorted(rows, key=lambda r: (-score(r, cue, now=now), _aware(r.observed_at)))


def _fact(row) -> str:
    predicate = (row.predicate or "").split(".", 1)[-1].replace("_", " ")
    subject = row.subject or "you"
    value = row.normalized_value or ""
    if subject == mr.SELF or row.kind == "profile":
        return f"your {predicate} is {value}".strip()
    return f"{subject}: {predicate} is {value}".strip()


def _item(row, cue: TurnCue, *, now: datetime) -> MemoryContextItem:
    from . import memory_provenance
    fact = _fact(row)
    return MemoryContextItem(
        memory_id=str(row.memory_id), fact=fact, kind=row.kind, confidence=row.confidence,
        freshness=row.freshness_class, provenance=memory_provenance.phrase(row),
        reason_relevant=_reason(row, cue), applicable_entity=row.subject,
        token_cost=_est_tokens(fact), score=score(row, cue, now=now))


@dataclass(frozen=True)
class MemoryRetriever:
    """Bound to ONE student for its whole life. Frozen, and no method takes a user id — so a caller
    holding a retriever for A has no API through which to name B."""
    user_id: UUID

    async def context(self, cue: TurnCue, *, now: datetime | None = None,
                      use_cache: bool = True) -> MemoryContext:
        return await retrieve(self.user_id, cue, now=now, use_cache=use_cache)

    async def everything(self, *, limit: int = 200) -> list:
        return await all_active(self.user_id, limit=limit)


async def retrieve(user_id: UUID, cue: TurnCue, *, now: datetime | None = None,
                   use_cache: bool = True) -> MemoryContext:
    """The whole pipeline, budgeted.

    Selection is greedy over the ranked list rather than top-N: one early long fact must not crowd out
    three short ones that together matter more, and a fixed N would make the budget a lie in both
    directions.
    """
    from . import memory_cache
    now = now or datetime.now(timezone.utc)
    started = time.perf_counter()

    if use_cache:
        cached = memory_cache.get(user_id, cue)
        if cached is not None:
            return cached

    ranked = rank(await shortlist(user_id, cue, now=now), cue, now=now)

    budget = cue.budget()
    items: list[MemoryContextItem] = []
    used = omitted = stale = 0
    for row in ranked:
        if row.freshness_class in ("stale", "expired"):
            stale += 1
        item = _item(row, cue, now=now)
        if used + item.token_cost > budget:
            omitted += 1
            continue
        items.append(item)
        used += item.token_cost

    ctx = MemoryContext(items=tuple(items), omitted_count=omitted,
                        retrieval_latency_ms=(time.perf_counter() - started) * 1000.0,
                        token_count=used, stale_count=stale)
    if use_cache:
        memory_cache.put(user_id, cue, ctx)
    log.info("memory_retrieved n=%d omitted=%d tokens=%d ms=%.1f", len(items), omitted, used,
             ctx.retrieval_latency_ms)
    return ctx


async def all_active(user_id: UUID, *, limit: int = 200) -> list:
    """Everything Bruce currently believes — for "what do you remember about me?".

    A separate entry point on purpose. `retrieve` answers "what matters now" and is budgeted; this one
    answers "what do you have", and a budget here would turn an honest answer to a direct question into
    a partial one without saying so.
    """
    R = schema.MemoryRecordRow
    async with user_session(user_id) as s:
        return list((await s.execute(
            select(R).where(R.user_id == user_id, R.status.in_(RETRIEVABLE), R.forgotten_at.is_(None),
                            R.kind != "style")
            .order_by(R.kind, R.observed_at.desc()).limit(limit))).scalars().all())
