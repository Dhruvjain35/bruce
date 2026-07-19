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
)
