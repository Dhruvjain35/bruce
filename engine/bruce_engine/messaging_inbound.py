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
import logging
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import intake_store, messaging_outbound, messaging_store, schema, task_dispatch
from .db import user_session, worker_session
from .messaging import Attachment, AttachmentKind, ChannelKind, InboundMessage, MessagingChannel, OutboundMessage
from .models import IntakeSourceKind

log = logging.getLogger(__name__)

# Bruce-voiced copy (casual, honest, no em dash, no corporate tone). Also gated at enqueue as a backstop.
# This is the UN-enrolled fallback ack, and it may ONLY be sent when a real intake mission was created for
# real content. It is a claim of active work, so anything that did not produce work must not use it.
ACK_TEXT = "gotchu, i've got it. give me a sec to look and i'll get back to u 👀"
# Said INSTEAD of ACK_TEXT when the conversation runtime is not available to this user and the message is
# not something to track. Manufacturing an intake mission out of "yo" and then claiming to be working on it
# is a false claim of active work: it produced a real mission row that meant nothing.
NO_RUNTIME_CHATTER_TEXT = ("hey. i can't chat on this number yet, but send me a flyer, screenshot, pdf, "
                           "link, or a note and i'll track it.")
# Said when the message is an EXECUTABLE goal (send an email, watch for a reply) that only the conversation
# runtime can carry out. Naming the blocker beats a cheerful ack for something that will never happen.
NO_RUNTIME_GOAL_TEXT = "i can't send email from this number yet, so i'm not going to pretend i started that."
# Conversational chatter with nothing to track. Deliberately tight and anchored to the WHOLE message, so a
# real note that merely opens with "hey" ("hey, track this: essay due friday") is still intake.
_CHATTER_RE = re.compile(
    r"^\s*(?:yo+|hey+|hi+|hello+|sup|wsg|wyd|hru|hbu|good\s+(?:morning|afternoon|evening|night)|"
    r"what['’]?s\s+up|i['’]?m\s+saying\s+\w+|ok(?:ay)?|k|thx|thanks?|ty|np|lol|lmao|nvm|bet|word)"
    r"[\s!.?,]*$", re.IGNORECASE)
# PRIVATE-ALPHA linking copy. No iPhone app or profile screen exists yet, so we NEVER reference one.
# A code is issued out of band by the Bruce team (operator CLI: scripts/create_link_code). Failure
# replies are deliberately generic — they never reveal whether a given number/account exists.
LINK_PROMPT = ("hey, this is bruce (private alpha). reply with the 6-char invite code the team gave u "
               "to connect this number. codes are single-use and expire quick.")
LINKED_TEXT = "you're in 🎉 text me a flyer, screenshot, pdf, link, or just a note and i'll track it."
BAD_CODE_TEXT = ("that code isn't valid or it expired. reply with a current one, or grab a fresh code "
                 "from the bruce team.")
RATE_LIMITED_TEXT = "too many tries, give it a few mins before trying another code."
# A redemption that FAILED for an infrastructure reason. Deliberately not the bad-code copy: telling
# someone their code is invalid when the database was unreachable sends them to mint another one that
# will fail the same way, and it hides an operator problem behind a user-facing explanation.
LINK_TEMP_FAIL_TEXT = "something on my end broke while checking that code. try again in a minute."
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")


@dataclasses.dataclass
class InboundOutcome:
    status: str            # processed | duplicate | linked | bad_code | rate_limited | link_error |
                           # unlinked_prompt |
    #                        blocked | no_runtime (gate denied AND nothing real to do -> honest refusal)
    user_id: UUID | None = None
    mission_id: UUID | None = None
    # G0.1 observability: the FastRouter's chosen path + wall-clock, so latency harness/telemetry and later
    # execution lanes can read the decision. Populated only for processed conversation turns; None otherwise.
    execution_class: str | None = None
    router_ms: float | None = None
    # G0.3 observability: the ToolBroker's shortlisted capabilities for a tool-bearing path (None for chat).
    shortlisted_capabilities: tuple[str, ...] | None = None
    # C1 observability: the durable mission this turn enqueued, and the honest reason when it did not.
    # `mission_run_id` is set ONLY on a real enqueue; `mission_status` always carries the planner's verdict
    # (ok | no_tool | disconnected | insufficient_scope | no_recipient | invalid) for a mission-routed turn.
    mission_run_id: str | None = None
    mission_status: str | None = None
    # Founder-alpha semantic rescue: what rescue made of a Stage-0 UNKNOWN turn, or None when rescue did
    # not run at all. Present so "did rescue fire, and what did it decide" is answerable from the turn's
    # own result instead of by reading a log line — including when it read the turn and handed it back.
    rescue_outcome: str | None = None


def _is_executable_goal(text: str) -> bool:
    """Does this ask Bruce to DO something only the conversation runtime can carry out (send an email,
    watch a thread)? Reuses the FastRouter's own send-intent pattern so the fallback and the router agree
    on what an executable goal looks like, instead of drifting apart with a second copy of the rule."""
    from . import fast_router
    return bool(text and fast_router._SEND_INTENT.search(text))


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


# The actor recorded on the CapabilityAudit row. Named for the PATH, not for "system", so an audit reader
# can tell an automatic link-redemption grant from an operator running the recovery CLI.
GRANT_ACTOR = "system:link_redemption"


async def _grant_conversation_access(user_id: UUID) -> None:
    """Give a freshly linked user the entitlement their next message needs.

    WHY HERE. `activate_production_entitlement` describes itself as "the AUTOMATIC path D1 calls on
    verified signup — never an operator action", and D1 does not exist: outside tests its only caller was
    `scripts/capability_admin`. So `conversation_access` returned `no_grant` for everyone, and a person
    who had just been told "you're in" had their next message fall past `conversation_runtime.handle`
    into the legacy intake path — a canned acknowledgement from a system that cannot send mail.

    WHY THIS DOES NOT WIDEN ACCESS. The invitation is the code, not this call. Link codes are minted by
    an operator, are single-use and expire, so granting on redemption automates the grant without
    changing who can obtain one. A number that was never given a code still gets `LINK_PROMPT`.

    WHY IT IS OUTSIDE THE REDEMPTION SESSION. `admin_session()` refuses to open when a tenant
    `app.user_id` is already set on the connection. `redeem_link_code` uses a `worker_session` and never
    sets one, and has committed and exited before this runs — so the ordering here is the requirement,
    not an accident.

    WHY A FAILURE IS NOT FATAL. The link is already committed and the single-use code is already spent.
    Raising here would tell the student nothing happened while their code is gone. They stay linked and
    honestly un-entitled, and the operator gets an exception-level alert carrying no message content and
    no handle.
    """
    from . import access_control      # local import: same conversation_runtime cycle-avoidance as below

    try:
        await access_control.activate_production_entitlement(
            user_id, capability="conversation", reason="link code redeemed", actor=GRANT_ACTOR)
    except Exception:
        log.exception("entitlement_grant_failed_after_link")


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
            try:
                r = await messaging_store.redeem_link_code(
                    text, msg.channel, msg.channel_identity, now=now)
            except Exception:
                # AN UNLINKED SENDER NEVER REACHES CONVERSATION INTAKE, INCLUDING ON FAILURE.
                #
                # Every branch below already returns, so the only way this path could fall through was an
                # exception escaping redemption — and it would have escaped `handle_inbound` entirely,
                # leaving the caller to decide what an unrecognised number gets told. Nothing here is
                # allowed to become a conversation turn, a mission, an AgentRun, or a claim that Bruce is
                # working on something.
                #
                # Logged at exception level with no message content and no handle: this is an operator
                # alert, and the operator needs to know it happened without being handed the text.
                log.exception("link_redemption_failed channel=%s", msg.channel.value)
                await _send(channel, to=reply_target, user_id=None, kind="prompt",
                            text=LINK_TEMP_FAIL_TEXT,
                            dedup_key=f"linkfail:{msg.provider_message_id}")
                return InboundOutcome(status="link_error")
            if r.status == "linked":
                await _grant_conversation_access(r.user_id)
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

    # 2a. CONVERSATION CONTEXT GRAPH (Bite 2 A2) — persistence only. Upsert the canonical inbound node +
    # reply/thread edges for this LINKED message so A3 can later resolve a replied-to message/image
    # without a resend. Idempotent; produces NO turn and NO reply. Runs for both runtime + legacy paths.
    from . import conversation_graph  # local import: avoid an import cycle via api/runtime
    msg.user_id = user_id
    await conversation_graph.ingest_inbound_message(msg)

    # 2b. CONVERSATION RUNTIME (Bite 1) — DB-gated per user (Bite 1.5 keystone). A LINKED inbound goes to
    # the multimodal conversation brain instead of the legacy intake + hard-coded ACK, but only if the DB
    # access gate allows THIS user (an active production entitlement or a live staging enrollment, unless a
    # global kill / hard-off overrides). The gate is by user_id, not a fragile handle allow-list; the
    # runtime also refuses groups. When access is denied, fall through to the unchanged legacy path.
    from . import access_control  # local import: same cycle-avoidance as the runtime import below
    from . import conversation_runtime  # local import: breaks the runtime<->inbound circular import
    access = await access_control.conversation_access(user_id, "conversation")
    if not msg.is_group and access.allow:
        return await conversation_runtime.handle(channel, msg, user_id=user_id, reply_target=reply_target)

    # 2c. RUNTIME NOT AVAILABLE. Log WHY at warning: a lapsed enrollment used to be invisible here, so the
    # system degraded to the legacy intake path in silence and every turn came back as a canned ack. A
    # 15-minute enrollment expiring mid-conversation should be loud, not a mystery.
    log.warning("conversation_runtime_unavailable user=%s source=%s reason=%s is_group=%s",
                user_id, access.source, access.reason, msg.is_group)

    # A claim of active work has to be TRUE. Without the runtime, conversational chatter and executable
    # goals produce nothing real, so they must not be turned into an intake mission and must not be
    # answered with "i've got it, give me a sec". Content with something to track still goes to intake:
    # that is what this fallback is FOR, and its ack is honest because a real mission is created.
    if not msg.attachments:
        body = (msg.text or "").strip()
        if _CHATTER_RE.match(body):
            await _send(channel, to=reply_target, user_id=user_id, kind="acknowledged",
                        text=NO_RUNTIME_CHATTER_TEXT, dedup_key=f"noruntime:{msg.provider_message_id}")
            return InboundOutcome(status="no_runtime", user_id=user_id, mission_status="chatter")
        if _is_executable_goal(body):
            await _send(channel, to=reply_target, user_id=user_id, kind="acknowledged",
                        text=NO_RUNTIME_GOAL_TEXT, dedup_key=f"noruntime:{msg.provider_message_id}")
            return InboundOutcome(status="no_runtime", user_id=user_id, mission_status="executable_goal")

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
