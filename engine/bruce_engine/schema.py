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
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class SourceSpan(Base, TSV):
    """A verbatim span extracted from a source — the grounding anchor for a deadline/fact."""

    __tablename__ = "source_spans"
    id = _pk()
    user_id = _owner()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    span_text: Mapped[str] = mapped_column(Text, nullable=False)


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
