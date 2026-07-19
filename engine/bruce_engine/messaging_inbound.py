"""Phase 6 — inbound handoff: a normalized message becomes the SAME durable intake the app creates.

There is no messaging-only extraction pipeline. A texted flyer/screenshot/PDF/link/instruction goes
through exactly `intake_store.create_pending_intake` + the existing Cloud Tasks worker — same source,
same grounding, same canonical mission the HandoffSheet produces. This file is only the routing +
idempotency + immediate acknowledgement at the messaging boundary.

Idempotent on (channel, provider_message_id): webhooks are redelivered, and a redelivery must never
create a second mission. Unlinked senders get a link prompt (or their texted code is redeemed) — no
intake happens until a channel identity is bound to a Bruce user.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import intake_store, messaging_outbound, messaging_store, schema, task_dispatch
from .db import user_session, worker_session
from .messaging import Attachment, AttachmentKind, ChannelKind, InboundMessage, MessagingChannel, OutboundMessage
from .models import IntakeSourceKind

ACK_TEXT = "Got it — I'm understanding this now. I'll message you when it needs review."
# PRIVATE-ALPHA linking copy. No iPhone app or profile screen exists yet, so we NEVER reference one.
# A code is issued out of band by the Bruce team (operator CLI: scripts/create_link_code). Failure
# replies are deliberately generic — they never reveal whether a given number/account exists.
LINK_PROMPT = ("This is Bruce (private alpha). To connect this number, reply with the 6-character "
               "invite code the Bruce team gave you. Codes expire quickly and are single-use.")
LINKED_TEXT = "You're linked. Text me a flyer, screenshot, PDF, link, or a note and I'll track it."
BAD_CODE_TEXT = ("That invite code isn't valid or has expired. Invite codes are single-use and short-"
                 "lived — reply with a current one, or ask the Bruce team for a fresh code.")
RATE_LIMITED_TEXT = "Too many attempts. Please wait a few minutes before trying another invite code."
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")


@dataclasses.dataclass
class InboundOutcome:
    status: str            # processed | duplicate | linked | bad_code | rate_limited | unlinked_prompt | blocked
    user_id: UUID | None = None
    mission_id: UUID | None = None


def _content(msg: InboundMessage) -> tuple[IntakeSourceKind, str | None, bytes | None, str | None]:
    """Pick the primary content of a message -> (source_kind, text, bytes, mime). First attachment
    wins (a texted flyer is usually one thing); else the text body."""
    for a in msg.attachments:
        if a.kind is AttachmentKind.image and a.data:
            return IntakeSourceKind.image, None, a.data, a.media_type or "image/png"
        if a.kind is AttachmentKind.pdf and a.data:
            return IntakeSourceKind.pdf, None, a.data, "application/pdf"
        if a.kind is AttachmentKind.link and a.url:
            return IntakeSourceKind.text, a.url, None, None  # link submitted as text (no server fetch yet)
    return IntakeSourceKind.text, (msg.text or "").strip() or None, None, None


async def _send(channel: MessagingChannel, *, to: str, user_id: UUID | None, kind: str, text: str,
                mission_id: UUID | None = None, dedup_key: str) -> None:
    """Queue an outbound reply durably (the relay claims it) + notify the channel. Idempotent on
    dedup_key so a redelivery/retry never double-sends. In production the channel is a QueueChannel
    (send is a no-op — the durable row IS the queue); FakeChannel records for tests."""
    await messaging_outbound.enqueue(
        user_id=user_id, to_handle=to, channel=ChannelKind.self_hosted_imessage, kind=kind, text=text,
        idempotency_key=dedup_key, mission_id=mission_id)
    await channel.send_message(to=to, message=OutboundMessage(text=text))


async def handle_inbound(channel: MessagingChannel, msg: InboundMessage) -> InboundOutcome:
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Resolve the sender's identity (server-side linkage — never trust the sender field).
    async with worker_session() as s:
        ident = (await s.execute(
            select(schema.MessagingIdentity).where(
                schema.MessagingIdentity.channel == msg.channel.value,
                schema.MessagingIdentity.channel_identity == msg.channel_identity)
        )).scalar_one_or_none()
        blocked = ident is not None and ident.blocked_at is not None
        user_id = ident.user_id if (ident and ident.disconnected_at is None) else None

    if blocked:
        return InboundOutcome(status="blocked")

    # Replies go to the CONVERSATION (group chat_guid via thread_id, else the direct sender).
    reply_target = msg.thread_id or msg.channel_identity

    # 2. Unlinked sender: redeem a texted code, or prompt to link. No intake for an unlinked sender.
    if user_id is None:
        text = (msg.text or "").strip()
        if _CODE_RE.match(text):
            r = await messaging_store.redeem_link_code(text, msg.channel, msg.channel_identity, now=now)
            if r.status == "linked":
                await _send(channel, to=reply_target, user_id=r.user_id, kind="acknowledged",
                            text=LINKED_TEXT, dedup_key=f"linked:{msg.provider_message_id}")
                return InboundOutcome(status="linked", user_id=r.user_id)
            if r.status == "rate_limited":
                await _send(channel, to=reply_target, user_id=None, kind="prompt",
                            text=RATE_LIMITED_TEXT, dedup_key=f"ratelimited:{msg.provider_message_id}")
                return InboundOutcome(status="rate_limited")
            # invalid / expired / locked / conflict all get the SAME generic reply — never reveal
            # whether the number is already linked or an account exists.
            await _send(channel, to=reply_target, user_id=None, kind="acknowledged",
                        text=BAD_CODE_TEXT, dedup_key=f"badcode:{msg.provider_message_id}")
            return InboundOutcome(status="bad_code")
        await _send(channel, to=reply_target, user_id=None, kind="prompt", text=LINK_PROMPT,
                    dedup_key=f"prompt:{msg.provider_message_id}")
        return InboundOutcome(status="unlinked_prompt")

    # 2b. CONVERSATION RUNTIME (Bite 1) — flag-gated, 1:1 staging test handles only. A LINKED inbound
    # goes to the multimodal conversation brain instead of the legacy intake + hard-coded ACK. Gate on
    # the SENDER identity (reply_target is the chat guid in groups); the runtime also refuses groups.
    # When the flag is off / the handle isn't allow-listed, fall through to the unchanged legacy path.
    from . import conversation_runtime  # local import: breaks the runtime<->inbound circular import
    if not msg.is_group and conversation_runtime.enabled_for(msg.channel_identity):
        return await conversation_runtime.handle(channel, msg, user_id=user_id, reply_target=reply_target)

    # 3. Idempotency: has this exact provider message already been handled?
    async with worker_session() as s:
        seen = (await s.execute(
            select(schema.InboundMessageRow).where(
                schema.InboundMessageRow.channel == msg.channel.value,
                schema.InboundMessageRow.provider_message_id == msg.provider_message_id)
        )).scalar_one_or_none()
        if seen is not None:
            return InboundOutcome(status="duplicate", user_id=user_id, mission_id=seen.mission_id)

    # 4. Hand off to the EXISTING durable intake (create source + mission + job).
    kind, text, data, mime = _content(msg)
    pending = await intake_store.create_pending_intake(
        user_id=user_id, source_kind=kind, text=text, input_bytes=data, mime=mime,
        idempotency_key=f"msg:{msg.channel.value}:{msg.provider_message_id}",
    )

    # 5. Persist the inbound record + attachment lineage (idempotent on the unique provider msg id).
    async with worker_session() as s:
        row = schema.InboundMessageRow(
            user_id=user_id, channel=msg.channel.value, provider_message_id=msg.provider_message_id,
            channel_identity=msg.channel_identity, text=msg.text, reply_to_message_id=msg.reply_to_message_id,
            provider_timestamp=msg.timestamp, source_id=pending.source_id, mission_id=pending.mission_id)
        s.add(row)
        try:
            await s.flush()
        except IntegrityError:
            return InboundOutcome(status="duplicate", user_id=user_id, mission_id=pending.mission_id)
        for a in msg.attachments:
            s.add(schema.MessageAttachment(
                user_id=user_id, inbound_message_id=row.id, kind=a.kind.value, media_type=a.media_type,
                url=a.url, filename=a.filename, source_id=pending.source_id))

    # 6. Wake the worker (same Cloud Tasks path) + acknowledge immediately (no promise of success).
    await task_dispatch.enqueue_intake(pending.job_id, user_id)
    await _send(channel, to=reply_target, user_id=user_id, kind="acknowledged", text=ACK_TEXT,
                mission_id=pending.mission_id, dedup_key=f"ack:{msg.provider_message_id}")
    return InboundOutcome(status="processed", user_id=user_id, mission_id=pending.mission_id)
