"""C1 — turn a ROUTED background mission into a durable, exactly-once enqueued AgentRun.

This closes the seam the runtime was missing. The FastRouter already classifies "email X and tell me when
they reply" as `background_mission`, and the BackgroundRunner already drives a plan of steps — but nothing
in the live path ever turned the first into the second. Until now every mission existed only because a
controlled job inserted one by hand, which is why `enqueue_background` had no non-test caller.

Invariants this module holds (each one is a test):

  * CAPABILITY TRUTH COMES FROM THE BROKER. The send capability is taken from `tool_broker.shortlist` and
    the built action is re-validated with `planner.validate_action`, so a mission can only ever contain a
    tool the broker actually offered AND marked actionable for THIS user. If nothing is actionable the
    mission is NOT enqueued and the broker's honest status is returned instead.
  * EXACTLY-ONCE ENQUEUE. The idempotency key is derived from the inbound message, and `create_run` treats
    a duplicate key as a reference to the existing run. A webhook redelivery, a retry, or the student
    sending the same text twice resolves to the SAME run — never a second email.
  * NO FABRICATED RECIPIENT. An address is used only when it is explicit in the text or is the student's
    own connected account. An unresolved recipient returns `no_recipient`; it never guesses.
  * THE QUALITY LAYER IS ON THIS PATH. Bodies are built by `email_compose.compose_email`, so the validator
    (including the no-em-dash rule) governs a mission send exactly as it governs a direct send.
  * NO MODEL IS REQUIRED. Composition and recipient resolution are deterministic. A planner model is never
    needed to build the mission, and the wait step itself costs no model call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from . import agent_run_store, email_compose, email_voice, oauth_google, planner, tool_broker
from .email_brief import EmailBrief, Relationship
from .runtime_contracts import ActionType, ExecutionClass, GoalAction, NextAction, Risk

log = logging.getLogger(__name__)

SEND_CAPABILITY = "gmail.send_message"

# A poll cadence that is cheap and honest: one metadata read per tick, no model, and a ceiling that gives
# a real person ~24h to answer before the mission finishes with an honest "no reply".
POLL_SECONDS = 60
MAX_POLLS = 1440

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# "email me", "send myself a note", "shoot me an email" -> the student's OWN connected account.
# The pronoun must be the OBJECT OF THE SEND, never a bare "me" anywhere in the sentence: almost every
# follow-up phrasing contains "tell me" / "let me know" / "notify me", so a loose \bme\b would resolve
# "send THEM an email and let me know" to the student's own inbox and mail the wrong person entirely.
_SELF_REF = re.compile(
    r"\b(?:e-?mail|send|shoot|drop|text)\s+(?:me|myself)\b"
    r"|\be-?mail\s+(?:it\s+|that\s+)?to\s+(?:me|myself)\b"
    r"|\bmy\s+(?:own\s+)?(?:email|inbox|gmail|address)\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class MissionPlan:
    """The outcome of trying to turn a routed decision into a queued mission."""

    enqueued: bool
    status: str                 # ok | not_a_mission | no_tool | disconnected | insufficient_scope |
                                # unsupported | no_recipient | invalid
    reason: str = ""
    run_id: str | None = None
    to: str | None = None
    subject: str | None = None
    body: str | None = None
    already_existed: bool = False


def is_enqueueable(decision) -> bool:
    """Only a routed BACKGROUND mission carrying a send intent becomes a durable mission here. Everything
    else (chat, direct action, a plan with no provider tool) is someone else's lane."""
    return bool(
        decision is not None
        and decision.execution_class == ExecutionClass.background_mission
        and decision.action == GoalAction.send
        and decision.domain == "gmail"
    )


async def resolve_recipient(user_id: UUID, text: str) -> tuple[str | None, str]:
    """Resolve WHO to email, or nothing. Explicit address wins; a self-reference resolves to the connected
    Google account (the only address Bruce can know without guessing). Returns (address, how)."""
    explicit = _EMAIL_RE.search(text or "")
    if explicit:
        return explicit.group(0), "explicit"
    if _SELF_REF.search(text or ""):
        integ = await oauth_google.get_integration(user_id)
        account = getattr(integ, "provider_account_id", None) if integ else None
        if account:
            return account, "self"
        return None, "self_unknown"      # connected but the account was never learned -> ask, don't guess
    return None, "unresolved"


def build_steps(action_args: dict) -> dict:
    """The mission plan the BackgroundRunner already knows how to drive: send, then wait for a real reply,
    then notify exactly once. `await_reply` is a poll, not a busy loop — no model call while waiting."""
    return {
        "steps": [
            {"kind": "action",
             "action": {"capability": SEND_CAPABILITY, "provider": "gmail", "operation": "send_message",
                        "arguments": dict(action_args)}},
            {"kind": "await_reply", "from_step": 0, "poll_seconds": POLL_SECONDS, "max_polls": MAX_POLLS},
        ],
        "notify": True,
        "to": action_args.get("to"),
    }


async def plan_mission(user_id: UUID, decision, *, text: str, idempotency_key: str,
                       sender_name: str | None = None) -> MissionPlan:
    """Router decision -> broker capability truth -> composed email -> exactly-once durable enqueue.

    Returns without enqueueing (and says why) whenever the honest answer is "I can't do this yet": no
    actionable tool, no resolvable recipient, or an action the broker's own schema rejects.
    """
    if not is_enqueueable(decision):
        return MissionPlan(False, "not_a_mission")

    # 1. capability truth — the broker decides what is callable for THIS user, not the registry, not us.
    sl = await tool_broker.shortlist(user_id, domain=decision.domain, action=decision.action,
                                     candidate_capabilities=decision.candidate_capabilities
                                     or (SEND_CAPABILITY,))
    cand = next((c for c in sl.actionable() if c.capability == SEND_CAPABILITY), None)
    if cand is None:
        status = "no_tool"
        if SEND_CAPABILITY in sl.insufficient_scope:
            status = "insufficient_scope"
        elif SEND_CAPABILITY in sl.unavailable:
            status = "disconnected"
        elif SEND_CAPABILITY in sl.excluded_dead:
            status = "unsupported"
        elif sl.candidates:
            status = sl.candidates[0].status
        return MissionPlan(False, status, reason="send capability is not actionable for this user")

    # 2. recipient — explicit or the student's own account; never invented.
    to, how = await resolve_recipient(user_id, text)
    if not to:
        return MissionPlan(False, "no_recipient",
                           reason="no address in the message and no connected account to fall back to"
                           if how == "self_unknown" else "no recipient could be resolved from the message")

    # 3. body — through the quality layer, so the validator (and the no-em-dash rule) governs this path too.
    profile = await email_voice.get_writing_profile(user_id)
    brief = EmailBrief(
        sender_name=sender_name or profile.sender_name or "",
        recipient_relationship=Relationship.peer if how == "self" else Relationship.professional,
        purpose="a message the student asked Bruce to send",
        requested_outcome="reply to this so I know it reached you",
        recipient_email=to,
        source_context=text or None,
    )
    composed = await email_compose.compose_email(brief, profile=profile)

    # 4. fail closed against the broker's own compact schema before anything durable is written.
    args = {"to": to, "subject": composed.subject, "body": composed.body}
    action = NextAction(type=ActionType.call_tool, capability=SEND_CAPABILITY, provider="gmail",
                        operation="send_message", arguments=args, risk=Risk.medium)
    ok, why = planner.validate_action(action, sl)
    if not ok:
        return MissionPlan(False, "invalid", reason=why)

    # 5. exactly-once enqueue. A duplicate key returns the EXISTING run instead of queuing a second send.
    goal = build_steps(args)
    run = await agent_run_store.enqueue_background(user_id, domain="gmail", goal=goal,
                                                   idempotency_key=idempotency_key)
    existed = str(run.get("status")) not in {"queued"} or bool(run.get("recovery_state"))
    log.info("mission_enqueued user=%s run=%s recipient_kind=%s existed=%s", user_id, run.get("id"),
             how, existed)
    return MissionPlan(True, "ok", run_id=str(run.get("id")), to=to, subject=composed.subject,
                       body=composed.body, already_existed=existed)


def mission_idempotency_key(channel: str, provider_message_id: str) -> str:
    """One inbound message -> at most one mission. Mirrors the inbound `msg:` key so the two are readable
    side by side in the runs table."""
    return f"mission:{channel}:{provider_message_id}"
