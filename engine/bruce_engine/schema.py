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
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
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
)
