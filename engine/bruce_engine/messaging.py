"""Messaging channel boundary — the replaceable layer in front of Bruce's mission engine.

THE THESIS THIS PROTECTS: a student should hand Bruce a flyer through Messages, leave, watch the
mission in Dynamic Island, approve once, and get a verified receipt. The app is the control center,
not the only command surface. That only survives if mission logic NEVER learns which channel it
came from.

So the shape is:

    provider webhook -> MessagingChannel.parse_inbound -> InboundMessage (canonical)
                                                       -> the SAME intake service the app uses
                                                       -> policy/approval -> execution -> verify
                                                       -> MessagingChannel.send_receipt

Rules encoded here:
  * Provider payloads STOP at the adapter. Nothing below this file may see a Linq/Apple field.
  * Inbound is idempotent on the PROVIDER's message id — webhooks are redelivered as a matter of
    course, and a redelivery must never create a second mission or a second calendar event.
  * Signature verification and replay protection are boundary concerns, not mission concerns.
  * Account linking is explicit and one-time: a phone number is not an identity claim. Anyone can
    text a number, so a channel identity may only bind to a Bruce user via a short-lived code the
    AUTHENTICATED user generated in the app.
  * Bruce does not initiate contact. Outbound is a reply to a student-started mission, or a
    decision/receipt for one. No unsolicited messaging, ever.

NOTHING HERE IS LIVE. No provider is connected; there is no Linq adapter, because the repo has no
Linq API contract to build against and inventing provider fields would produce confident fiction.
FakeChannel exists so the whole path is deterministically testable today, and so a real adapter is
a drop-in later. Do NOT describe iMessage as functional until a real message has passed through a
supported provider.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field


class ChannelKind(str, Enum):
    in_app = "in_app"
    share_extension = "share_extension"
    linq = "linq"                    # planned — no adapter; no API contract in this repo
    apple_business = "apple_business"  # planned — requires Apple approval
    push_action = "push_action"
    fake = "fake"                    # tests only


class AttachmentKind(str, Enum):
    image = "image"
    pdf = "pdf"
    link = "link"


class Attachment(BaseModel):
    kind: AttachmentKind
    media_type: str | None = None
    # Bytes for image/pdf; url for link. Attachments route into the SAME persistent intake service
    # the app uses — there is no second, weaker path for messaging.
    data: bytes | None = None
    url: str | None = None
    filename: str | None = None


class InboundMessage(BaseModel):
    """The canonical inbound message. The mission engine sees ONLY this — never a provider payload."""

    provider_message_id: str          # idempotency key: webhooks WILL be redelivered
    channel: ChannelKind
    channel_identity: str             # e.g. a phone number/handle. NOT an identity claim by itself.
    user_id: UUID | None = None       # resolved by account linking; None = unlinked sender
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    timestamp: datetime.datetime
    reply_to_message_id: str | None = None
    thread_id: str | None = None


class OutboundMessage(BaseModel):
    text: str
    # A deep link into the app for a real decision. Never a raw approval URL: approving must
    # happen behind authentication, not by anyone who can open a link.
    deep_link: str | None = None


class MessagingError(Exception):
    pass


class SignatureInvalid(MessagingError):
    """The webhook did not come from the provider. Reject BEFORE parsing anything."""


class ReplayDetected(MessagingError):
    """A timestamp outside the tolerance window — a captured request being replayed."""


class MessagingChannel(Protocol):
    """Every channel implements exactly this. Mission logic depends on the Protocol, never a class."""

    kind: ChannelKind

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> None: ...
    async def parse_inbound(self, *, body: bytes, headers: dict[str, str]) -> InboundMessage: ...
    async def send_message(self, *, to: str, message: OutboundMessage) -> str: ...
    async def send_decision(self, *, to: str, summary: str, deep_link: str) -> str: ...
    async def send_receipt(self, *, to: str, summary: str, deep_link: str | None = None) -> str: ...


# --------------------------------------------------------------------------- boundary helpers


REPLAY_WINDOW = datetime.timedelta(minutes=5)


def verify_hmac_signature(*, body: bytes, provided: str | None, secret: str) -> None:
    """Constant-time HMAC-SHA256 check. Used by real adapters; shared so each one can't get it wrong.

    compare_digest, not `==`: a byte-by-byte comparison leaks the correct prefix through timing,
    which is enough to forge a signature given patience.
    """
    if not provided:
        raise SignatureInvalid("missing signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise SignatureInvalid("signature does not match")


def reject_replays(*, sent_at: datetime.datetime, now: datetime.datetime | None = None) -> None:
    """A valid signature on an OLD request is still an attack — signatures don't expire by themselves."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if abs((now - sent_at).total_seconds()) > REPLAY_WINDOW.total_seconds():
        raise ReplayDetected("message timestamp is outside the accepted window")


# --------------------------------------------------------------------------- account linking


LINK_CODE_TTL = datetime.timedelta(minutes=10)


def generate_link_code() -> str:
    """A short, one-time code the AUTHENTICATED app user reads out and texts to Bruce.

    Short enough to type from a Lock Screen, random enough not to guess, and short-lived. This is
    the ONLY way a channel identity becomes a Bruce user: a phone number proves nothing on its own,
    because anyone can text a number and claim to be someone.
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/O/0/1 — these get misread and mistyped
    return "".join(secrets.choice(alphabet) for _ in range(6))


class LinkRequest(BaseModel):
    code: str
    user_id: UUID
    channel: ChannelKind
    expires_at: datetime.datetime
    consumed_at: datetime.datetime | None = None

    def is_usable(self, now: datetime.datetime | None = None) -> bool:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return self.consumed_at is None and now < self.expires_at


# --------------------------------------------------------------------------- fake channel (tests)


class FakeChannel:
    """Deterministic in-memory channel. Models the parts of a real provider that matter:
    signatures, redelivery, and attachments. Lets the whole messaging path be tested today, with no
    provider connected and no invented provider fields."""

    kind = ChannelKind.fake

    def __init__(self, secret: str = "test-secret") -> None:
        self.secret = secret
        self.sent: list[tuple[str, OutboundMessage]] = []

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> None:
        verify_hmac_signature(body=body, provided=headers.get("x-signature"), secret=self.secret)

    async def parse_inbound(self, *, body: bytes, headers: dict[str, str]) -> InboundMessage:
        import json

        self.verify_signature(body=body, headers=headers)
        raw = json.loads(body)
        sent_at = datetime.datetime.fromisoformat(raw["timestamp"])
        reject_replays(sent_at=sent_at)
        atts = [
            Attachment(
                kind=AttachmentKind(a["kind"]),
                media_type=a.get("media_type"),
                data=bytes.fromhex(a["hex"]) if a.get("hex") else None,
                url=a.get("url"),
                filename=a.get("filename"),
            )
            for a in raw.get("attachments", [])
        ]
        return InboundMessage(
            provider_message_id=raw["id"],
            channel=self.kind,
            channel_identity=raw["from"],
            text=raw.get("text"),
            attachments=atts,
            timestamp=sent_at,
            reply_to_message_id=raw.get("reply_to"),
        )

    async def send_message(self, *, to: str, message: OutboundMessage) -> str:
        self.sent.append((to, message))
        return f"fake-out-{len(self.sent)}"

    async def send_decision(self, *, to: str, summary: str, deep_link: str) -> str:
        return await self.send_message(to=to, message=OutboundMessage(text=summary, deep_link=deep_link))

    async def send_receipt(self, *, to: str, summary: str, deep_link: str | None = None) -> str:
        return await self.send_message(to=to, message=OutboundMessage(text=summary, deep_link=deep_link))
