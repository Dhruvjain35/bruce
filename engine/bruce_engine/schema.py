"""Persistent schema (SQLAlchemy 2.0 ORM) for Phase 1.5.

Structured columns for the fields we query/scope on; JSONB only for flexible evidence/metadata
(never a single blob of the whole object). Every user-owned row carries user_id + version (for
optimistic concurrency) + created_at/updated_at. Evidence lineage is real FKs:

    sources -> source_spans -> tasks / opportunities -> calendar_proposals
    missions -> mission_phase_events / approvals / receipts

Alembic owns this schema (see migrations/); nothing here is created at app startup.
"""

from __future__ import annotations

import datetime
from enum import Enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


class TSV:
    """created_at / updated_at / version mixin (optimistic concurrency via version)."""

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


def _owner() -> Mapped[uuid.UUID]:
    """user_id FK -> users.id, indexed, cascade on user delete."""
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )


class User(Base, TSV):
    __tablename__ = "users"
    # id == the IdP subject (Supabase/Apple `sub`); we upsert on first authenticated request.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    auth_provider: Mapped[str] = mapped_column(String(64), nullable=False, server_default="supabase")
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Source(Base, TSV):
    """A source document (email/PDF/screenshot/text). Raw content is temporary; hash is durable."""

    __tablename__ = "sources"
    id = _pk()
    user_id = _owner()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # IntakeSourceKind
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # temporary; cleared per retention
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Derived+minimized extraction (ExtractedIntake JSON): replayed on idempotent retry so a retry
    # can't return a fresh LLM result contradicting the spans/tasks already stored. Durable like
    # spans/tasks — the retention sweep clears raw_text only.
    extracted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # The DB-level guarantee that one intake can never create two sources (see 0003).
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_source_idem"),)


class SourceSpan(Base, TSV):
    """A verbatim span extracted from a source — the grounding anchor for a deadline/fact."""

    __tablename__ = "source_spans"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    span_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Which extracted item (0-based) this span grounds. The ONLY stable ordering for a source's
    # spans: they are all written in one transaction, so created_at ties exactly and id is random.
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Opportunity(Base, TSV):
    __tablename__ = "opportunities"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, server_default="other")
    deadline_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date
    cost: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_opportunity_idem"),)


class TaskRow(Base, TSV):
    __tablename__ = "tasks"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    span_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_spans.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    course_or_org: Mapped[str | None] = mapped_column(String(300), nullable=True)
    due: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open")
    workload_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_items: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_task_idem"),)


class CalendarProposal(Base, TSV):
    __tablename__ = "calendar_proposals"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    start: Mapped[str] = mapped_column(String(32), nullable=False)
    end: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tentative: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    ics: Mapped[str | None] = mapped_column(Text, nullable=True)


class Brief(Base, TSV):
    __tablename__ = "briefs"
    id = _pk()
    user_id = _owner()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    lines: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


class Mission(Base, TSV):
    __tablename__ = "missions"
    id = _pk()
    user_id = _owner()
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="outreach")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="running")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="created")
    short_status: Mapped[str] = mapped_column(String(200), nullable=False, server_default="Starting…")
    goal: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # verified output; complex -> JSONB
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_mission_idem"),)


class MissionPhaseEvent(Base):
    __tablename__ = "mission_phase_events"
    id = _pk()
    user_id = _owner()
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    short_status: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base, TSV):
    __tablename__ = "approvals"
    id = _pk()
    user_id = _owner()
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_approval_idem"),)


class Receipt(Base, TSV):
    __tablename__ = "receipts"
    id = _pk()
    user_id = _owner()
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class KnownPerson(Base, TSV):
    """Someone the student TOLD Bruce about, and the address they gave for them.

    THE ONLY SOURCE OF A RECIPIENT BRUCE DID NOT READ IN THE CURRENT SENTENCE. `goal_slots.Fill.resolvable`
    says a recipient may be looked up and never invented; this is the thing it looks up, and it holds
    nothing Bruce inferred — a row exists only because the student said, in their own trusted words, that
    a person has an address.

    Every column is there because a wrong recipient is a letter to a real stranger:

      * `source_message_id` + `stated_span` — WHICH message said this, and the verbatim words. A row that
        cannot point at the sentence that created it is a row nobody can audit.
      * `confidence` — how sure the extraction was. Resolution has a floor; a low-confidence row is kept
        (it is still what the student said) and does not answer "who is my teacher" on its own.
      * `superseded_by_id` — a correction does not edit history. "actually her email is X" writes a NEW
        row and points the old one at it, so "what did Bruce think last tuesday" stays answerable.
      * `forgotten_at` — forgetting removes a person from RESOLUTION without deleting the audit trail.
        A hard delete would make "why did it stop knowing that" unanswerable.
    """

    __tablename__ = "known_people"
    id = _pk()
    user_id = _owner()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    relationship: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Provenance. `stated_span` is the student's own words — held because the address is acted on later,
    # far from the turn that supplied it, and "where did this come from" must be answerable then.
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stated_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, server_default="user_stated")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("known_people.id", ondelete="SET NULL"), nullable=True)
    forgotten_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_known_people_user_live", "user_id", "normalized_name"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # redacted
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Integration(Base, TSV):
    """A connected external account (Google Calendar today). Holds the ENCRYPTED refresh token.

    Security shape, deliberate:
      * ``refresh_token_encrypted`` is Fernet ciphertext (see bruce_engine.crypto) — never plaintext,
        never logged, never returned by any endpoint, never put in a model prompt. The DB is
        encrypted at rest by the provider, but a refresh token is a bearer credential for a
        student's real calendar; a dump or a stray log line must not hand it over.
      * There is deliberately NO access_token column. Access tokens are short-lived and are fetched
        on demand from the refresh token — persisting them widens the blast radius for no gain.
      * RLS scopes rows to the owning user like every other table; account deletion cascades.
    """

    __tablename__ = "integrations"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # "google_calendar"
    # Who the provider says this is (e.g. the Google account email). Shown in Settings so a student
    # can see WHICH account Bruce is writing to — a real safety property, not decoration.
    provider_account_id: Mapped[str | None] = mapped_column(String(320), nullable=True)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="connected")
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),)


class OAuthState(Base):
    """One-time CSRF state for an OAuth authorization-code flow.

    This table IS the security boundary of the connect flow. The callback arrives from the user's
    browser and its query parameters are attacker-controllable, so identity is NEVER read from them
    — it is read from the row this state points at. Each row is:
      * bound to the authenticated user who started the flow,
      * short-lived (``expires_at``),
      * single-use (``consumed_at`` — a replayed state must fail, not re-authorize),
      * carrier of the PKCE ``code_verifier``, which never leaves the server.
    """

    __tablename__ = "oauth_states"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The opaque value handed to Google. Unique so a replay cannot create a second row.
    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)  # PKCE; server-only
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserWorldState(Base, TSV):
    """Per-user world state for the general agent runtime (R3) — starts with the ONE fact the calendar
    needs: the user's IANA timezone. Stored canonically (America/Chicago), never the abbreviation ("CST"),
    with the evidence for how it was inferred so the user can inspect/clear it. One row per user."""

    __tablename__ = "user_world_state"
    id = _pk()
    user_id = _owner()
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)          # IANA, e.g. America/Chicago
    timezone_source: Mapped[str | None] = mapped_column(String(32), nullable=True)   # user_stated | device | calendar | inferred
    locale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_world_state_user"),)


class AgentRun(Base, TSV):
    """A durable agent run (R2) — the general runtime's working state across messages / restarts /
    corrections. Holds the GoalSpec + plan + last tool result + active decision so execution state is
    NEVER reconstructed from recent chat text. tenant_or_worker RLS (a resuming worker writes it)."""

    __tablename__ = "agent_runs"
    id = _pk()
    user_id = _owner()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True, index=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default="calendar")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="understanding")
    goal: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))          # GoalSpec
    temporal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)                                     # TemporalSpec
    selected_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)         # CalendarEventEntity
    selected_provider_account: Mapped[str | None] = mapped_column(String(320), nullable=True)
    current_action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)                               # NextAction
    last_tool_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)                             # ToolResult
    verification_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    active_decision: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recovery_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 0025 — background-mission lease (a durable run advanced by the worker, claimed FOR UPDATE SKIP LOCKED
    # like intake_jobs). next_run_at gates when a queued/rescheduled run becomes claimable.
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_agent_run_idem"),)


class AgentRunEvent(Base):
    """Append-only transition log for an AgentRun (status changes, tool calls, verifications)."""

    __tablename__ = "agent_run_events"
    id = _pk()
    user_id = _owner()
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CalendarEventEntity(Base, TSV):
    """A canonical calendar event (R7) — one row per VERIFIED provider event, so "move guitar class" /
    "delete that" resolve to a real entity instead of re-parsing. Owner-scoped; unique per provider event."""

    __tablename__ = "calendar_event_entities"
    id = _pk()
    user_id = _owner()
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    start: Mapped[str] = mapped_column(String(40), nullable=False)          # ISO (date or datetime)
    end: Mapped[str | None] = mapped_column(String(40), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="google_calendar")
    provider_account_id: Mapped[str | None] = mapped_column(String(320), nullable=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False, server_default="primary")
    source_message_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipts.id", ondelete="SET NULL"), nullable=True)
    provider_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_event_id", name="uq_cal_entity_provider_event"),)


class GmailSentLedger(Base):
    """The exactly-once ledger for Gmail sends (Phase G) — one row per (user, marker), written after a
    verified send. Google assigns the message id and Gmail can't be searched by a custom header, so this row
    (not a client-set id) is what makes a retry resolve to the already-sent message instead of a duplicate.
    Owner-scoped (tenant_isolation); UNIQUE(user_id, marker) is the concurrency guard."""

    __tablename__ = "gmail_sent_ledger"
    id = _pk()
    user_id = _owner()
    marker: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_addr: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "marker", name="uq_gmail_ledger_user_marker"),)


class MemoryStatus(str, Enum):
    """The lifecycle of one remembered fact. Exactly one of these is true at a time, and it lives in a
    COLUMN rather than being re-derived from a combination of nullable timestamps — the old shape made
    "is this still believed" a three-way join of `forgotten_at`, `superseded_by` and `contradicted_by`,
    and every read path had to remember all three."""

    active = "active"
    superseded = "superseded"        # a later trusted statement replaced it
    contradicted = "contradicted"    # a later trusted statement conflicts and neither is resolved
    forgotten = "forgotten"          # the student asked; content is redacted, tombstone remains
    expired = "expired"              # its own retention window elapsed
    quarantined = "quarantined"      # withheld from ordinary retrieval pending review


class MemoryRecordRow(Base, TSV):
    """One typed memory. THE canonical definition — there is no second table object anywhere.

    #121b queried a private SQLAlchemy Core table declared inside the memory module, which meant column
    truth lived in two places (that file and the migration) and diverged from every other table in the
    engine. A schema with two definitions has no definition.

    `value_json` holds the structured value; `normalized_value` is the folded form the deterministic
    shortlist matches on. Current state is NOT duplicated inside the JSON — `status`, `superseded_by_id`,
    `contradicted_by_id` and `forgotten_at` are columns, and a reader never has to parse JSON to find out
    whether Bruce still believes something.
    """

    __tablename__ = "memory_records"
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = _owner()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    predicate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    value_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    freshness_class: Mapped[str] = mapped_column(String(16), nullable=False, server_default="fresh")
    retention_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    user_editable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active", index=True)
    contradicted_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    forgotten_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Denormalized shortlist keys. Stage 1 of retrieval has to be an index scan, not a scan plus a
    # Python normalize, so the folded subject and the predicate's namespace are stored rather than
    # computed. Derived by the writer and by nothing else.
    entity_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # CLAIM LINEAGE. Every version of one claim shares a `claim_root_id` (the first version's id) and a
    # `claim_key` (a digest of kind + subject + predicate). Without them "forget that" reaches only the
    # newest row, and the value the student asked Bruce to forget survives one superseded row below it —
    # readable through provenance, correction history and any audit view.
    #
    # `claim_key` is also what the partial unique index keys on, so the database, not a read-then-compare,
    # is what guarantees at most one active version of a claim.
    claim_root_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    claim_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The future question this memory answers, captured at write time because it cannot be recovered
    # later. Redacted on forget along with the rest of the content: "so Bruce knows who her therapist
    # is" is itself content.
    reason_it_matters: Mapped[str | None] = mapped_column(String(300), nullable=True)


class MemoryCorrectionRow(Base):
    """One correction, kept forever. The corrected record is superseded rather than edited, and this row
    is the link between the two — so "that's wrong" produces an auditable before and after instead of a
    silent overwrite that nobody can explain afterwards."""

    __tablename__ = "memory_corrections"
    id = _pk()
    user_id = _owner()
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    replacement_memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    corrected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class MemoryForgetEventRow(Base):
    """One forget request, and the minimum needed to keep it enforced.

    Content-free by construction. It records WHAT SCOPE was forgotten and how many records it reached,
    never the values — a forget log that quoted the thing being forgotten would be the same leak with
    extra steps. The `source_message_id` scope is what lets a re-intake of the same message be refused,
    which is the difference between forgetting and forgetting-until-you-read-that-email-again.
    """

    __tablename__ = "memory_forget_events"
    id = _pk()
    user_id = _owner()
    scope: Mapped[str] = mapped_column(String(16), nullable=False)   # fact | subject | kind | source
    target: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    forgotten_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class AuthorizationEvidenceRow(Base):
    """Durable consent (#121a). One row per authorization, bound to one trusted message, one user, one
    operation, and one arguments fingerprint.

    #120 kept this in memory for the length of a turn, which is safe and cannot serve a background
    mission: a mission executes long after the turn that planned it, so it must carry an
    authorization_id and have the executor RELOAD and RECHECK this row. It must never carry a copied
    verdict — a boolean that was true once is not evidence that it is true now.

    Append-mostly. `invalidated_at`, `superseded_by_authorization_id` and `consumed_at` are one-way
    transitions; nothing else about a row may change after it is written, or the audit trail is worthless.
    """

    __tablename__ = "authorization_evidence"
    authorization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = _owner()
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    trusted_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    arguments_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity: Mapped[str] = mapped_column(String(24), nullable=False)
    authorization_type: Mapped[str] = mapped_column(String(32), nullable=False)
    explicit_operation_request: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    invalidated_by_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_by_authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_attempt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operation_receipt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuthorizationRefusal(Base):
    """The refusal timeline (#121a). One row every time the deterministic boundary blocks a turn.

    "No later refusal" cannot be answered from the evidence table alone: a refusal that arrives while
    nothing is outstanding marks no authorization row, and yet it still has to stop a mission that was
    authorized before it and wakes up after it. So refusals are recorded on their own timeline and
    compared against an authorization's own timestamp at execution.
    """

    __tablename__ = "authorization_refusals"
    id = _pk()
    user_id = _owner()
    refused_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    polarity: Mapped[str] = mapped_column(String(24), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ModelCost(Base):
    __tablename__ = "model_costs"
    id = _pk()
    user_id = _owner()
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _owner_nullable() -> Mapped[uuid.UUID | None]:
    """user_id FK that may be NULL — a channel identity / inbound message exists BEFORE it is linked
    to a Bruce user. Only a worker/service session (or the owner once linked) can see such a row."""
    return mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)


# ================================================================================================
# MESSAGING DOMAIN (provider-neutral). NO provider (Linq/Apple) object may enter these — the adapter
# normalizes into these rows at the boundary. Processed server-side (webhook -> worker), so RLS is
# worker-or-owner (see migration 0006): a service session may handle a pre-link/unlinked row; the app
# reads only the user's own once linked.
# ================================================================================================


class MessagingIdentity(Base, TSV):
    """A channel handle (e.g. a phone number). NOT an identity claim on its own — user_id is NULL
    until an AccountLinkCode binds it. One handle per channel binds to at most one user."""

    __tablename__ = "messaging_identities"
    id = _pk()
    user_id = _owner_nullable()
    channel: Mapped[str] = mapped_column(String(32), nullable=False)   # ChannelKind
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="linq")
    channel_identity: Mapped[str] = mapped_column(String(255), nullable=False)  # phone/handle
    blocked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("channel", "channel_identity", name="uq_msg_identity"),)


class MessagingConversation(Base, TSV):
    __tablename__ = "messaging_conversations"
    id = _pk()
    user_id = _owner_nullable()
    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messaging_identities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    last_message_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundMessageRow(Base, TSV):
    """A received message, normalized. Idempotent on (channel, provider_message_id) — webhooks are
    redelivered, and a redelivery must never create a second mission."""

    __tablename__ = "inbound_messages"
    id = _pk()
    user_id = _owner_nullable()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messaging_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)  # transient; not the durable copy
    reply_to_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_timestamp: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lineage into the SAME intake the app uses — not a second pipeline.
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    mission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (UniqueConstraint("channel", "provider_message_id", name="uq_inbound_provider_msg"),)


class MessageAttachment(Base, TSV):
    """An inbound attachment. Bytes are NOT stored here — image/pdf route into the intake source; a
    link stores its url. source_id links to the intake source it produced."""

    __tablename__ = "message_attachments"
    id = _pk()
    user_id = _owner_nullable()
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image | pdf | link
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)


class OutboundMessageRow(Base, TSV):
    """A reply Bruce sends (ack/needs_review/receipt/…). Idempotent on idempotency_key so a retry
    never double-sends. provider_message_id is filled after the provider accepts the send."""

    __tablename__ = "outbound_messages"
    id = _pk()
    user_id = _owner_nullable()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messaging_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # acknowledged|needs_review|blocked|failed|succeeded|decision|receipt
    text: Mapped[str] = mapped_column(Text, nullable=False)
    to_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)  # recipient handle/chat_guid for the relay
    deep_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Queue state machine (Phase 8): pending|leased|sending|sent|retryable_failed|terminal_failed.
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Lease fields so the Mac relay claims one message at a time (crash-safe, like intake_jobs).
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NOTE: this class has a `text` column, so the module-level `text()` is shadowed here — use a
    # plain-string server_default for these integers.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    relay_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbound_idem"),
        Index("ix_outbound_claimable", "status", "lease_expires_at"),
    )


class MessageDeliveryEvent(Base, TSV):
    """A delivery/read/failure event from the provider. Idempotent on provider_event_id (dedup)."""

    __tablename__ = "message_delivery_events"
    id = _pk()
    user_id = _owner_nullable()
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)  # sent|delivered|read|failed
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)  # TYPE/short cause only, no content
    __table_args__ = (UniqueConstraint("provider_event_id", name="uq_delivery_event"),)


class AccountLinkCode(Base, TSV):
    """A one-time code an AUTHENTICATED app user generates and texts to Bruce to bind their channel
    identity. Hashed at rest (never the plaintext), short-lived, single-use, rate-limited by attempts."""

    __tablename__ = "account_link_codes"
    id = _pk()
    user_id = _owner()  # NOT nullable — an authenticated user creates it
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bound_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messaging_identities.id", ondelete="SET NULL"), nullable=True
    )


class MessagingLinkAttempt(Base, TSV):
    """Per-handle brute-force guard for account linking (private-alpha bridge). Keyed by the channel
    identity, NOT a user — a handle that texts many wrong invite codes is locked out for a window,
    independent of the per-code attempt cap. INFRASTRUCTURE (no user_id): worker-only RLS (migration
    0010). Holds no message content — only a failure counter and a lockout timestamp."""

    __tablename__ = "messaging_link_attempts"
    id = _pk()
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    window_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("channel", "channel_identity", name="uq_link_attempt_handle"),)


class RelayDevice(Base, TSV):
    """A dedicated Mac relay's credential. INFRASTRUCTURE, not user-owned — the server stores only a
    HASH of the device secret (the secret is shown once at registration, held in the Mac Keychain).
    Remotely revocable (revoked_at). Accessed only in a worker/service session (see migration 0007)."""

    __tablename__ = "relay_devices"
    id = _pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default="self_hosted_imessage")
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 of the secret
    bruce_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)  # the Bruce iMessage identity
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bite 1.5 A1 — per-device control plane (added by migration 0014). directive is the intended state
    # (run|pause_outbound|stop); outbound_paused/paused_* are the resolved pause. supervisor_seen_at +
    # agent_commit are CONTENT-FREE supervisor telemetry (a pinned relay commit, a liveness stamp).
    directive: Mapped[str] = mapped_column(String(16), nullable=False, server_default="run")  # run|pause_outbound|stop
    outbound_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    paused_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paused_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supervisor_seen_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pinned relay commit (content-free)


class RelayControl(Base):
    """Singleton-per-environment server-side switch that AUTHORITATIVELY gates NEW outbound CLAIMS for
    the self-hosted iMessage relay (Bite 1.5 A1). When ``outbound_paused`` is true for the running
    ``BRUCE_ENV``, ``/v1/relay/outbound/claim`` hands out NOTHING — no new or reclaimed message is ever
    leased to a device, enforced in the claim path server-side and independent of any relay client.

    SCOPE (see docs/relay-emergency-stop.md): this gates the CLAIM. It does NOT retract a message that a
    device already claimed before the pause — a distributed system cannot recall bytes already handed to
    iMessage. Preventing the in-flight send is A2's pre-send directive re-check (fail-closed). An
    already-claimed message that is not sent recovers WITHOUT duplication: while paused it is never
    re-handed out, and on resume it is reclaimable exactly once.

    INFRASTRUCTURE, not user-owned: worker-only RLS (like relay_devices), UPSERT-seeded per environment
    by migration 0014."""

    __tablename__ = "relay_control"
    id = _pk()
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    outbound_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("environment", name="uq_relay_control_environment"),)


class RelayControlAudit(Base):
    """Append-only audit of every relay control-plane CHANGE (pause/resume/stop, global or per-device),
    added by migration 0015. Records the actor (server-derived operator, never client-supplied), the
    action, the environment, the optional device, an optional reason, and the previous/new state — so a
    kill or a device pause is always attributable. Enforced append-only by 0015 (worker SELECT+INSERT
    policies, NO update/delete policy, plus a BEFORE UPDATE/DELETE trigger that raises). CONTENT-FREE:
    previous_state/new_state carry only directive/pause booleans — never message content, handles, or
    paths; ``reason`` is operator-supplied free text and must likewise never carry payload content."""

    __tablename__ = "relay_control_audit"
    id = _pk()
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)  # server-derived operator@host
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # pause_all|resume_all|pause_device|resume_device|stop_device
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    previous_state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    new_state: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelayBootstrapToken(Base):
    """Short-lived, SINGLE-USE device-registration token (Bite 1.5 A4). Only the sha256 HASH is stored;
    the token is bound to (environment, device_name), expires quickly, and is consumed on first successful
    use so a replay fails. Worker-only RLS (infra); added by migration 0016."""

    __tablename__ = "relay_bootstrap_tokens"
    id = _pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)   # sha256(bootstrap token)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("token_hash", name="uq_relay_bootstrap_token_hash"),)


class RelayRegistrationAudit(Base):
    """Append-only audit of relay device registration (mint / register / rotate / revoke / deny /
    rate_limited / replay) with actor / environment / device / result / time — NEVER a secret. Enforced
    append-only by migration 0016. Worker-only RLS."""

    __tablename__ = "relay_registration_audit"
    id = _pk()
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True)
    result: Mapped[str] = mapped_column(String(24), nullable=False)   # ok | denied
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MagicLinkToken(Base):
    """Short-lived, SINGLE-USE E1 internal-test sign-in link. Only the sha256 HASH of the token's random
    jti is stored — never the raw JWT or the sign-in URL. Bound to (user, environment) with issued/expiry
    times; ``/internal/test/auth`` atomically consumes the matching UNUSED row (a conditional UPDATE on
    ``consumed_at IS NULL``), so a replay — or a concurrent double-open — yields exactly one session.
    Admin-only RLS (mint + consume run in an ``admin_session``), added by migration 0017. Content-free:
    no handle / token / secret is ever stored."""

    __tablename__ = "magic_link_tokens"
    id = _pk()
    jti_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)     # sha256 of the token jti
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)    # founder the link signs in
    environment: Mapped[str] = mapped_column(String(24), nullable=False)              # binds the link to an env
    issued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # short-lived
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # set once
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("jti_hash", name="uq_magic_link_jti_hash"),)


class RelayUpload(Base, TSV):
    """A file the relay uploaded from an inbound message, staged until the intake source is created.
    Bytes live here transiently (cleared on consume); infra, so worker-only RLS. content_hash lets
    the relay skip re-uploading a duplicate."""

    __tablename__ = "relay_uploads"
    id = _pk()
    relay_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # cleared on consume
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryAttempt(Base, TSV):
    """One relay attempt to deliver an outbound message. Records the provider result for audit; carries
    no message content."""

    __tablename__ = "delivery_attempts"
    id = _pk()
    user_id = _owner_nullable()
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relay_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # sent | failed
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)  # TYPE/short cause only, no content


class IntakeJob(Base, TSV):
    """A durable unit of intake work: transcribe + extract, done OUTSIDE the request lifecycle.

    The request commits this row (status='pending') plus its source + mission, then returns 202. A
    worker later claims it with a lease, runs the model work, and persists results — so a process
    restart never loses accepted work (the row survives; the lease expires and any worker reclaims
    it). The raw input bytes/text live HERE (transient, cleared when the job finishes) so no new blob
    table is needed; the durable content lands in sources/spans/tasks under the owner's RLS context.

    RLS is custom (see migration 0005): the owner sees their own jobs (API status reads), and a
    worker session (app.worker='on', set only by server worker code, never from a request) may claim
    across users. Content writes still happen under user_session(user_id), fully tenant-scoped.
    """

    __tablename__ = "intake_jobs"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # pending -> processing -> completed | retryable_failed (-> reclaimed) | terminal_failed
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending", index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # IntakeSourceKind
    mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # text sources; cleared on finish
    input_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # image/pdf; cleared on finish
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)  # TYPE/reason only, no content
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_intake_job_idem"),
        # The claim query filters on status + lease_expires_at; index it for the worker hot path.
        Index("ix_intake_jobs_claimable", "status", "lease_expires_at"),
    )


class SemanticShadowJob(Base, TSV):
    """One turn queued for SHADOW observation by the semantic executive — durable, never best effort.

    Shadow used to be a detached asyncio task. It could not delay or break a turn (that part was right
    and is preserved: the request path still only writes this row), but a restart or a cancelled worker
    dropped the observation — and dropped the SLOW ones specifically, since the fast ones had already
    finished. The sample deciding whether the executive may be given authority would have been biased
    toward fast successful reads, which are the least informative reads it contains.

    Same lease/claim machine as IntakeJob, deliberately: pending -> processing -> completed |
    retryable_failed | terminal_failed, with attempts bounding retries and an expired lease making a
    crashed worker's row reclaimable. RLS is intake_jobs' tenant_or_worker (migration 0033).

    TWO PROPERTIES THIS SHAPE BUYS STRUCTURALLY:
      * UNIQUE(user_id, channel, provider_message_id) — exactly one job per turn, enforced by the
        constraint rather than by a SELECT-then-INSERT that races two webhook deliveries.
      * the observation lives ON this row, so a retried job UPDATEs one record instead of inserting a
        second one. Convergence is a property of the table, not of the worker's care.

    Holds NO turn text: `channel` + `provider_message_id` reference conversation_turns, which is where
    the student's words live and where a deletion reaches them. `outcome` is the typed result (ok,
    timeout, transport, provider_rejected, invalid_schema, budget_exceeded, turn_missing) — a failed
    observation is recorded as data and is NEVER marked completed.
    """

    __tablename__ = "semantic_shadow_jobs"
    id = _pk()
    user_id = _owner()
    # THE CANONICAL TURN (migration 0033), and the idempotency key that matters. The invariant this table
    # protects is "exactly one shadow job per eligible CONVERSATION TURN", and until this column existed
    # the only way to say so was (user_id, channel, provider_message_id) — a key made of the PROVIDER's
    # metadata rather than of the thing being counted. Two consequences, both measured:
    #   * reconciliation had to match shadow rows to turns by re-deriving the same triple, so the two
    #     ledgers were joined on a guess instead of on an identity;
    #   * any narrowing of that triple (dropping `channel`, which no test caught) silently swallowed the
    #     second channel's turn — a LOSS, and losses bias the authority sample toward the turns that
    #     happened to arrive on the channel that won the race.
    # ON DELETE CASCADE because the row exists only to describe that turn: a deletion that reaches the
    # student's words must reach the telemetry that points at them.
    conversation_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_turns.id", ondelete="CASCADE"), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending", index=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # THE RETRY BOUND, and its default is load-bearing in a way a bound usually is not. `claim` requires
    # `attempts < max_attempts`, so a max_attempts of 0 makes a BRAND NEW row unclaimable — every fresh
    # observation is lost, the backlog reads empty, and nothing raises. The CHECK in __table_args__ makes
    # that state unrepresentable rather than merely unintended (migration 0033).
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # THE FENCE (migration 0034). `lease_owner` is `cloudrun-<node>-<pid>` and two workers on one
    # container share it, so it can never prove WHICH claim a write belongs to; a token minted per claim
    # can. Every completion write requires it, so a stale worker updates zero rows.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)  # TYPE/reason only, no content
    router_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    context_flags: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # WHAT SHADOW DID WITH THIS TURN (migration 0035): `eligible`, or a typed exclusion reason. Persisted
    # rather than counted in memory because reconciliation compares this table against conversation_turns:
    # an exclusion that leaves nothing behind is indistinguishable from a turn that was dropped, and
    # telling those two apart is the only thing the reconciliation can actually do.
    intake_disposition: Mapped[str] = mapped_column(String(40), nullable=False, server_default="eligible")
    # THE TURN'S CAPABILITY TRUTH (migration 0035): the registry ids THIS user could actually run at the
    # moment of the turn, from the per-user broker at the request boundary. NULL means it could not be
    # established, which is deliberately NOT the same as `[]` — an unknown truth may never be counted as a
    # capability denial. Registry ids only, so nothing here is PII.
    reachable_operations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    observation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agrees: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    divergence: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # One of the four numbers the authority decision rests on, so it is a COLUMN like `agrees` and
    # `divergence` rather than a JSONB field: it has to be countable in SQL. NULL means the turn was
    # never read (a failed observation), which is deliberately not the same as False.
    false_capability_denial: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        # THE CANONICAL IDEMPOTENCY KEY (migration 0033). One shadow job per conversation turn, stated
        # over the turn itself rather than over the provider metadata that happens to identify it.
        UniqueConstraint("conversation_turn_id", name="uq_semantic_shadow_job_conversation_turn"),
        # INGRESS DEDUPE, kept and demoted. It still stops a redelivered webhook from racing two inserts
        # in the window before the canonical id is known, and it still covers rows written before that column existed —
        # but it is source metadata, not the invariant.
        UniqueConstraint("user_id", "channel", "provider_message_id", name="uq_semantic_shadow_job_turn"),
        # SILENT TOTAL LOSS, made unrepresentable (migration 0033). `claim` takes a row only while
        # `attempts < max_attempts`; at 0 that is false the moment the row is written, so every fresh job
        # is permanently unclaimable — no error, no backlog, no observations, and a queue that looks
        # perfectly drained. A bound whose zero value means "never try" belongs in the database.
        CheckConstraint("max_attempts > 0", name="ck_semantic_shadow_job_max_attempts_positive"),
        Index("ix_semantic_shadow_jobs_claimable", "status", "lease_expires_at"),
    )


class EventCandidate(Base, TSV):
    """A structured event extracted from a message, captured for REVIEW (Bite 1 conversation brain).

    Bruce persists this and honestly tells the user calendar isn't wired yet — it NEVER claims the
    event was added (status stays 'proposed'). provenance holds the verbatim source span + inbound
    message id so the extraction is grounded and auditable. Tenant-isolated."""

    __tablename__ = "event_candidates"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    starts_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # e.g. ["end_time"]
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)       # {"span":..., "inbound_message_id":...}
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="proposed")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_event_candidate_idem"),)


class ConversationTurn(Base, TSV):
    """One turn of a LINKED user's iMessage conversation with Bruce (Bite 1 runtime).

    role='user' or 'assistant'. The assistant turn stores the VALIDATED 13-field ConversationDecision
    as JSONB — and NOTHING else: no chain-of-thought / scratchpad is ever persisted. Holds the most
    sensitive student free-text, so it is tenant-isolated and cascades on account delete."""

    __tablename__ = "conversation_turns"
    id = _pk()
    user_id = _owner()
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)               # user | assistant
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)               # user text OR styled reply
    decision: Mapped[dict | None] = mapped_column(JSONB, nullable=True)         # 13-field contract (assistant only)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    mission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True)
    event_candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("event_candidates.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "channel", "provider_message_id", "role", name="uq_turn_msg_role"),)


# ================================================================================================
# CONVERSATION CONTEXT GRAPH (Bite 2 A2 — persistence only; retrieval is A3). A THIN provider-neutral
# graph OVER the existing content rows: it never duplicates message text or attachment bytes — it points
# at inbound_messages / conversation_turns / outbound_messages / message_attachments / messaging_identities
# by id and stores only the relationships (reply / thread-root / edit / reaction). Ingested by the worker
# path for a LINKED user, so tenant_or_worker RLS (worker writes across users; a user reads only their
# own; FORCE RLS). Account-deletion erasure is free via the users.id CASCADE. See migration 0018.
# ================================================================================================


class ConversationMessage(Base, TSV):
    """Canonical node: exactly ONE row per (provider, provider_message_id), globally unique so a provider
    message id can never be reused across users. Content lives in the referenced rows (SET NULL so pruning
    content never deletes the node). Reaction/edit/unsent are carried, but relationship-only events never
    become a turn (relay_inbound guard + conversation_graph)."""

    __tablename__ = "conversation_messages"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)               # e.g. self_hosted_imessage
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)   # chat/conversation id
    sender_identity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messaging_identities.id", ondelete="SET NULL"), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)              # inbound | outbound | system
    service: Mapped[str | None] = mapped_column(String(16), nullable=True)          # imessage | sms | unknown
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True)
    conversation_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True)
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="SET NULL"), nullable=True)
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    edited_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unsent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id", name="uq_conv_msg_provider"),
        Index("ix_conv_messages_owner_chat_time", "user_id", "provider_chat_id", "received_at"),
    )


class ConversationMessageRelationship(Base, TSV):
    """A directed edge between messages. The target may not be ingested yet:
    unresolved_target_provider_message_id holds the provider guid until the target arrives, then
    conversation_graph.reconcile links it (never across owners). One edge per (source, type)."""

    __tablename__ = "conversation_message_relationships"
    id = _pk()
    user_id = _owner()
    source_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    target_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True)
    unresolved_target_provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)     # reply_to|thread_root|edit_of|replacement_for|mission_source|decision_source|attachment_caption_for
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (
        UniqueConstraint("source_message_id", "relationship_type", name="uq_conv_rel_source_type"),
        Index("ix_conv_rel_unresolved", "user_id", "unresolved_target_provider_message_id"),
    )


class ConversationMessageAttachment(Base, TSV):
    """Join from a graph message to an EXISTING message_attachments row, with the relationship (attached
    here vs quoted/replied-to/generated) and order. Never copies attachment metadata or bytes."""

    __tablename__ = "conversation_message_attachments"
    id = _pk()
    user_id = _owner()
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    attachment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("message_attachments.id", ondelete="CASCADE"), nullable=False)
    relationship: Mapped[str] = mapped_column(String(16), nullable=False, server_default="attached")  # attached|quoted|replied_to|generated
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    __table_args__ = (UniqueConstraint("message_id", "attachment_id", "relationship", name="uq_conv_msg_att"),)


class ConversationReactionEvent(Base, TSV):
    """A tapback add/remove. NEVER creates a reasoning turn; a removal is its own event (not a delete).
    Idempotent on (provider, provider_event_id). Target may resolve later like a relationship edge."""

    __tablename__ = "conversation_reaction_events"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)    # deterministic idempotency key
    actor_identity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messaging_identities.id", ondelete="SET NULL"), nullable=True)
    target_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True)
    unresolved_target_provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reaction_type: Mapped[str] = mapped_column(String(16), nullable=False)         # love|like|dislike|laugh|emphasis|question|sticker|unknown
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    occurred_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_conv_reaction_event"),
        Index("ix_conv_reaction_target", "user_id", "target_message_id"),
    )


# ================================================================================================
# SCHOOL CONNECTOR DOMAIN (provider-neutral canonical academic graph). NO provider (Canvas/Classroom)
# field may enter these — the adapter normalizes into these rows at the boundary (see canvas_fake.py,
# school_store.py). Every synced object is tenant-owned and processed under user_session(user_id), so
# RLS is tenant_isolation (owner-only, no worker path). Conditional-create + policies in migration 0012.
#
# Provenance is not optional: every object row carries the ORIGINAL provider id + URL, the provider's own
# source_timestamp, Bruce's last_synced_at, the capability_state it was produced under, a content_hash
# (change detection), and a source_id link to the school_sources evidence anchor. Change history lives in
# school_object_changes. This is what lets the query layer cite every item it returns.
# ================================================================================================


class _SchoolProv:
    """Provenance columns shared by every persisted canonical school object (mixin, copied per table).

    Mirrors the TSV mixin pattern already used above. source_id links to the school_sources evidence
    anchor (SET NULL so pruning a source never deletes the object). Uniqueness on (user_id, provider,
    provider_id) is declared per-table so a re-sync UPSERTS the same logical object instead of duplicating.
    """

    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="canvas")
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_timestamp: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    capability_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="supported")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("school_sources.id", ondelete="SET NULL"), nullable=True
    )


class SchoolSyncCursor(Base, TSV):
    """A restart-safe incremental-sync position, one row per (user, provider, resource).

    cursor_value is the OPAQUE provider token (Canvas: an ISO updated_since). The store persists it after
    each sync and hands it back on the next one — so a crash mid-sync loses no progress; the next run
    resumes from the last committed cursor."""

    __tablename__ = "school_sync_cursors"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource: Mapped[str] = mapped_column(String(48), nullable=False)  # assignments | announcements | courses | ...
    cursor_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", "resource", name="uq_school_cursor"),)


class SchoolSource(Base, TSV):
    """The evidence anchor for a synced provider object (the canonical "source"). Holds the minimized
    normalized payload + a content_hash; every canonical object row points back here via source_id."""

    __tablename__ = "school_sources"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)  # course|assignment|announcement|...
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_timestamp: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    capability_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="supported")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # minimized
    __table_args__ = (UniqueConstraint("user_id", "provider", "object_type", "provider_id", name="uq_school_source"),)


class SchoolSourceSpan(Base, TSV):
    """A verbatim span of a provider object (the canonical "source_span") — the grounding anchor a fact
    (e.g. a due date) traces back to. Mirrors source_spans in the intake domain."""

    __tablename__ = "school_source_spans"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("school_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    span_text: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # what the span grounds (e.g. "due_at")
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SchoolInstitution(Base, TSV, _SchoolProv):
    __tablename__ = "school_institutions"
    id = _pk()
    user_id = _owner()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_institution"),)


class SchoolTerm(Base, TSV, _SchoolProv):
    __tablename__ = "school_terms"
    id = _pk()
    user_id = _owner()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    start_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_term"),)


class SchoolInstructor(Base, TSV, _SchoolProv):
    __tablename__ = "school_instructors"
    id = _pk()
    user_id = _owner()
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_instructor"),)


class SchoolCourse(Base, TSV, _SchoolProv):
    """A course. Sections + instructors are folded into ``detail`` JSONB (thin sub-objects that only ever
    render with their course), matching the schema's "structured columns for what we query, JSONB for the
    rest" rule. They remain first-class canonical objects when read back."""

    __tablename__ = "school_courses"
    id = _pk()
    user_id = _owner()
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    course_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    workflow_state: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # available|completed
    term_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    term_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    institution_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # instructors+sections
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_course"),)


class SchoolAssignment(Base, TSV, _SchoolProv):
    """An assignment. The submission STATE is denormalized onto structured columns (the due-buckets pivot
    on due_at + submission_state, so they must be queryable); full submission detail lives in
    school_submissions."""

    __tablename__ = "school_assignments"
    id = _pk()
    user_id = _owner()
    course_provider_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    unlock_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    points_possible: Mapped[float | None] = mapped_column(Float, nullable=True)
    submission_types: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    submission_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown", index=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_assignment"),)


class SchoolMaterial(Base, TSV, _SchoolProv):
    __tablename__ = "school_materials"
    id = _pk()
    user_id = _owner()
    course_provider_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="file")  # file|page|link
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_material"),)


class SchoolAnnouncement(Base, TSV, _SchoolProv):
    __tablename__ = "school_announcements"
    id = _pk()
    user_id = _owner()
    course_provider_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_announcement"),)


class SchoolSubmission(Base, TSV, _SchoolProv):
    """Full submission detail. grade / rubric / feedback are facets of the submission's assessment (that IS
    the Canvas data model — score, rubric_assessment, and submission_comments all hang off the submission),
    so they are persisted here (structured columns + JSONB) and re-exposed as their own canonical objects."""

    __tablename__ = "school_submissions"
    id = _pk()
    user_id = _owner()
    assignment_provider_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    points_possible: Mapped[float | None] = mapped_column(Float, nullable=True)
    graded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rubric: Mapped[dict | None] = mapped_column(JSONB, nullable=True)     # canonical Rubric dump
    feedback: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))  # Feedback dumps
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_submission"),)


class SchoolScheduleEvent(Base, TSV, _SchoolProv):
    """A schedule/calendar event. Canvas declares this capability unsupported (no ICS feed wired), so this
    table stays empty for Canvas — it exists for a provider (e.g. an ICS feed) that DOES expose events."""

    __tablename__ = "school_schedule_events"
    id = _pk()
    user_id = _owner()
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    start_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    end_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    course_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_schedule_event"),)


class SchoolObjectChange(Base):
    """The change-history log: one row per created/updated/deleted classification observed during a sync.

    Powers "what changed at school?" and the ``changes`` provenance field on every object. changed_fields
    lists the canonical field names that differed on an update (empty for create/delete)."""

    __tablename__ = "school_object_changes"
    id = _pk()
    user_id = _owner()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)  # created|updated|deleted
    cursor_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_fields: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    detected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_school_change_lookup", "user_id", "object_type", "provider_id"),)


# ================================================================================================
# CAPABILITY ACCESS MODEL (Bite 1.5 keystone). The DB-backed replacement for per-user Cloud Run env
# editing as the conversation-capability access gate. These are the ADMIN-WRITE / WORKER-READ class:
# readable in a worker_session (the runtime gate reads them ACROSS users, filtered by user_id in the
# query) and an admin_session (operator management), and writable ONLY in an admin_session — a tenant
# must NEVER see or write who is entitled, who is enrolled, or the global kill state. Policies + FORCE
# RLS + the append-only audit trigger live in migration 0013 (never tenant_isolation).
#
# Two distinct concepts that must NOT be conflated: a ProductionAccountEntitlement is PERSISTENT
# production access (no expiry), while a StagingTestEnrollment is TEMPORARY, internal-only, and must
# NEVER gate production.
# ================================================================================================


class ProductionAccountEntitlement(Base, TSV):
    """PERSISTENT production access for an onboarded user. There is deliberately NO temporary/expiry
    field — access persists until exactly one explicit event: the user unlinks their messaging identity,
    deletes the account, the account is suspended, the subscription/entitlement ends, security/abuse
    enforcement blocks the account, or a global emergency shutdown fires. One row per user."""

    __tablename__ = "production_account_entitlements"
    id = _pk()
    user_id = _owner()
    account_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")  # active|suspended|closed
    plan: Mapped[str] = mapped_column(String(32), nullable=False, server_default="alpha")
    messaging_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    verified_identity: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    suspended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entitlement_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Which capabilities this account may use through messaging (e.g. ["conversation"]). JSONB so a new
    # capability is added without a migration; the access gate checks membership, never rollout_state.
    capability_availability: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    __table_args__ = (UniqueConstraint("user_id", name="uq_prod_entitlement_user"),)


class StagingTestEnrollment(Base, TSV):
    """TEMPORARY, internal-only enrollment for a staging / canary / unreleased capability. Optional
    expiry (expires_at), immediate revoke (revoked_at), full audit. MUST NEVER gate production usage:
    a production entitlement is never expired by this TTL, and a live enrollment never makes production
    access persistent. Multiple rows over time are allowed (re-enroll after revoke keeps the history)."""

    __tablename__ = "staging_test_enrollments"
    id = _pk()
    user_id = _owner()
    capability: Mapped[str] = mapped_column(String(48), nullable=False)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    enabled_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled_by: Mapped[str | None] = mapped_column(String(200), nullable=True)  # server-derived operator actor
    audit_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_staging_enrollment_lookup", "user_id", "capability", "environment"),)


class CapabilityGlobalState(Base):
    """Singleton per (capability, environment): the global rollout + emergency-kill switch, UPSERT-seeded
    by migration 0013. ``killed`` wins over everything in the access gate. ``rollout_state`` is
    informational in this bite — ``rolled_out`` must NEVER mass-enable; per-user access always comes from
    an entitlement or an enrollment, never from the rollout state."""

    __tablename__ = "capability_global_state"
    id = _pk()
    capability: Mapped[str] = mapped_column(String(48), nullable=False)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    rollout_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="default_off")  # default_off|enrolling|rolled_out
    killed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("capability", "environment", name="uq_capability_global_state"),)


class CapabilityAudit(Base):
    """Append-only audit of every capability-access mutation (grant / enroll / revoke / kill). Enforced
    append-only by migration 0013: admin INSERT+SELECT policies with NO UPDATE/DELETE policy, plus a
    BEFORE UPDATE/DELETE trigger that raises (so it holds even for a BYPASSRLS/owner role). ``actor`` is
    SERVER-DERIVED (the operating shell user@host), never a client-supplied string; ``detail`` is
    redacted and never carries a secret."""

    __tablename__ = "capability_audit"
    id = _pk()
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)  # grant_production|enroll_staging|revoke_staging|kill_on|kill_off
    capability: Mapped[str | None] = mapped_column(String(48), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(24), nullable=True)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # redacted
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# user-owned tables that get row-level security (users handled separately: a user sees only self)
RLS_TABLES: tuple[str, ...] = (
    "sources",
    "source_spans",
    "opportunities",
    "tasks",
    "calendar_proposals",
    "briefs",
    "missions",
    "mission_phase_events",
    "approvals",
    "receipts",
    "model_costs",
    "event_candidates",       # added 0011 — conversation brain (most sensitive student free-text)
    "conversation_turns",     # added 0011
    # added 0012 — SchoolConnector canonical academic graph (per-student synced school data)
    "school_sync_cursors",
    "school_sources",
    "school_source_spans",
    "school_institutions",
    "school_terms",
    "school_instructors",
    "school_courses",
    "school_assignments",
    "school_materials",
    "school_announcements",
    "school_submissions",
    "school_schedule_events",
    "school_object_changes",
    # added 0013 — capability access model (Bite 1.5 keystone). NOT tenant_isolation: these carry FORCE
    # RLS with the admin-write / worker-read class (+ append-only audit), so a tenant can never read or
    # write them. Listed here so the all-tables FORCE-RLS guarantee covers them too.
    "production_account_entitlements",
    "staging_test_enrollments",
    "capability_global_state",
    "capability_audit",
    # added 0014 — relay control plane (Bite 1.5 A1). relay_control gates new outbound claims: worker-only
    # (NOT tenant_isolation, like relay_devices), but it carries the same all-tables FORCE-RLS guarantee so
    # a silently-missed policy could never make it default-ALLOW for the app role (a tenant reading or
    # flipping the switch). test_relay_control_rls_default_deny proves denial.
    "relay_control",
    # added 0015 — append-only audit of relay control-plane changes (worker-only, same FORCE-RLS guarantee).
    "relay_control_audit",
    # added 0016 — relay device bootstrap: short-lived single-use registration tokens + append-only
    # registration audit (worker-only, same FORCE-RLS guarantee).
    "relay_bootstrap_tokens",
    "relay_registration_audit",
    # added 0022 — runtime lane: per-user world state (timezone/preferences). tenant_isolation.
    "user_world_state",
    # added 0023/0024 — general agent runtime: durable run state (tenant_or_worker) + append-only events,
    # and canonical verified calendar-event entities (tenant_isolation).
    "agent_runs",
    "agent_run_events",
    "calendar_event_entities",
    # added 0032/0033 — and BACKFILLED into this inventory, which is the point of the entry. These three
    # carried their policies from the day their migrations landed, but they were absent from the enforced
    # list, so the all-tables FORCE-RLS test skipped them: the guarantee existed and nothing checked it.
    # `intake_jobs` and `semantic_shadow_jobs` are the tenant_or_worker class (a worker claims across
    # users; an owner sees only their own rows) and `known_people` is tenant_isolation — every one of them
    # holds or references a student's own data, which is exactly where a silently-missed policy costs the
    # most. See test_postgres_integration::test_rls_is_owner_scoped_for_the_job_and_people_tables.
    "intake_jobs",
    "semantic_shadow_jobs",
    "known_people",
)
