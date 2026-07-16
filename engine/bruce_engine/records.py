"""Domain records returned by repositories (business logic never touches ORM rows or SQL)."""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    id: UUID | None = None
    user_id: UUID
    kind: str
    content_sha256: str | None = None
    raw_text: str | None = None
    expires_at: datetime.datetime | None = None
    idempotency_key: str | None = None
    version: int = 1


class SourceSpanRecord(BaseModel):
    """A verbatim span of a source — the grounding anchor a task points back to."""

    id: UUID | None = None
    user_id: UUID
    source_id: UUID
    span_text: str
    version: int = 1


class TaskRecord(BaseModel):
    id: UUID | None = None
    user_id: UUID
    kind: str
    title: str
    due: str | None = None
    status: str = "open"
    source_id: UUID | None = None
    span_id: UUID | None = None  # the exact span this task was grounded in (source -> span -> task)
    required_items: list = Field(default_factory=list)
    idempotency_key: str | None = None
    version: int = 1


class MissionRecord(BaseModel):
    id: UUID | None = None
    user_id: UUID
    kind: str = "outreach"
    status: str = "running"
    phase: str = "created"
    short_status: str = "Starting…"
    goal: dict = Field(default_factory=dict)
    plan: dict | None = None
    error: str | None = None
    idempotency_key: str | None = None
    version: int = 1


class RepositoryError(Exception):
    """Base class for repository errors."""


class NotFoundError(RepositoryError):
    """The row does not exist for this user (surfaced as 404, never a revealing 403)."""


class ConcurrencyError(RepositoryError):
    """Optimistic version mismatch — the row changed under us."""


class CrossTenantError(RepositoryError):
    """A referenced object (e.g. source_id) is not owned by the acting user."""
