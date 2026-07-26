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
  * THE SEND HAPPENS INSIDE THE TURN, VERIFIED, BEFORE ANY ACKNOWLEDGEMENT. Bruce may only say "sent"
    once a real message went out AND read-back verification passed, so the send cannot be a future
    mission step. It runs through `agent_loop.run_direct_action` (its own AgentRun, the same verification
    a direct action gets) and the mission that follows is PURE MONITORING. A mission retry therefore
    cannot re-send: there is no send left in it.
  * NOTHING DURABLE SURVIVES A FAILED SEND. If the send does not verify, no mission is enqueued and the
    caller is told exactly that. If the send verifies but the enqueue fails, that is reported as its own
    state — the mail really did go, so claiming otherwise would be a second kind of lie.
  * EXACTLY-ONCE. The idempotency key is derived from the inbound message: `create_run` treats a duplicate
    key as a reference to the existing run, and the send's key (`<key>:send`) makes a retried turn resolve
    to the SAME Gmail message. A webhook redelivery cannot produce a second email or a second mission.
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
    # The broker's shortlist for this turn. Carried out so the runtime can report WHICH tools were
    # offered on a mission path — the runtime only surfaces its own shortlist for direct/foreground
    # lanes, so without this a mission's capability truth would be invisible from outside.
    shortlisted: tuple[str, ...] = ()
    # Set only after a REAL send that read-back verified. `verified_send` is what entitles a reply to use
    # the word "sent"; enqueue_failed carries it True so the student is told the mail went but the
    # monitoring did not start, rather than one lie in either direction.
    message_id: str | None = None
    thread_id: str | None = None
    verified_send: bool = False


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


# A self-addressed request to be told about the reply IS its own purpose: the student is asking for
# something they can reply to. Nothing is invented. Any other recipient needs a stated ask.
_ROUNDTRIP = re.compile(r"\b(when|if)\s+i\s+(reply|respond|answer|write\s+back)\b|\bround.?trip\b",
                        re.IGNORECASE)


def derive_intent(text: str, how: str) -> tuple[str, str] | None:
    """(purpose, requested_outcome) for the email, or None when the purpose is NOT derivable.

    None is the honest answer for "email coach smith and tell me when he replies": Bruce knows who to
    write to and nothing about what to say, and inventing a message a real person will read is worse
    than asking. A self-send round-trip is the one case where the request states its own content.
    """
    body = text or ""
    if how == "self" and _ROUNDTRIP.search(body):
        return ("a quick check that mail from Bruce reaches you",
                "reply to this so i know it arrived")
    if how == "self":
        return ("a note to yourself", "reply to this so i know it arrived")
    return None


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
    shortlisted = tuple(c.capability for c in sl.candidates)
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
        return MissionPlan(False, status, reason="send capability is not actionable for this user",
                           shortlisted=shortlisted)

    # 2. recipient — explicit or the student's own account; never invented.
    to, how = await resolve_recipient(user_id, text)
    if not to:
        return MissionPlan(False, "no_recipient",
                           reason="no address in the message and no connected account to fall back to"
                           if how == "self_unknown" else "no recipient could be resolved from the message",
                           shortlisted=shortlisted)

    # 3. WHAT should the email say? Never invent content aimed at a real person. A self-addressed
    # round-trip request carries its own purpose ("send me something I can reply to"), so that IS
    # derivable. Anything else without a stated ask returns needs_content and the student is asked.
    intent = derive_intent(text, how)
    if intent is None:
        return MissionPlan(False, "needs_content", to=to, shortlisted=shortlisted,
                           reason="no derivable purpose for a real recipient; ask rather than invent")

    profile = await email_voice.get_writing_profile(user_id)
    brief = EmailBrief(
        sender_name=sender_name or profile.sender_name or "",
        recipient_relationship=Relationship.peer if how == "self" else Relationship.professional,
        purpose=intent[0], requested_outcome=intent[1], recipient_email=to)
    composed = await email_compose.compose_email(brief, profile=profile)

    # 4. fail closed against the broker's own compact schema before anything is sent.
    args = {"to": to, "subject": composed.subject, "body": composed.body}
    action = NextAction(type=ActionType.call_tool, capability=SEND_CAPABILITY, provider="gmail",
                        operation="send_message", arguments=args, risk=Risk.medium)
    ok, why = planner.validate_action(action, sl)
    if not ok:
        return MissionPlan(False, "invalid", reason=why, shortlisted=shortlisted)

    # 5. SEND NOW, VERIFIED, INSIDE THE TURN. The acknowledgement is allowed to say "sent" only after a
    # real send that read-back verified, so the send cannot be a future mission step — it has to have
    # already happened by the time we reply. run_direct_action gives the send its own AgentRun and the
    # same read-back verification a direct action gets; the idempotency key makes a retried turn resolve
    # to the SAME message instead of mailing twice.
    from . import agent_loop
    from .gmail_executor import GmailSendExecutor
    send_key = f"{idempotency_key}:send"
    ex = GmailSendExecutor(to=to, subject=composed.subject, body=composed.body, idempotency_key=send_key)
    res = await agent_loop.run_direct_action(user_id, executor=ex, idempotency_key=send_key)
    if not res.verified:
        # NOTHING durable is created: no mission, no monitoring, and the caller says so plainly.
        log.warning("mission_send_unverified user=%s outcome=%s reason=%s", user_id,
                    res.tool_result.outcome.value, res.tool_result.reason)
        return MissionPlan(False, "send_failed", to=to, shortlisted=shortlisted,
                           reason=res.tool_result.reason or "gmail send was not verified")

    message_id = res.tool_result.provider_entity_id
    thread_id = (res.tool_result.read_back or {}).get("threadId") or message_id

    # 6. only NOW enqueue the durable wait. The mission is pure monitoring — the send already happened,
    # so a mission retry can never re-send. Exactly-once on the inbound message's key.
    goal = {"steps": [{"kind": "await_reply", "thread_id": thread_id, "after_message_id": message_id,
                       "poll_seconds": POLL_SECONDS, "max_polls": MAX_POLLS}],
            "notify": True, "to": to}
    try:
        run = await agent_run_store.enqueue_background(user_id, domain="gmail", goal=goal,
                                                       idempotency_key=idempotency_key)
    except Exception as exc:
        # The email really did go out, so we must NOT pretend monitoring started.
        log.exception("mission_enqueue_failed_after_verified_send user=%s msg=%s", user_id, message_id)
        return MissionPlan(False, "enqueue_failed", to=to, shortlisted=shortlisted,
                           message_id=message_id, thread_id=thread_id, verified_send=True,
                           reason=f"send verified but enqueue failed: {exc}")

    existed = bool(run.get("recovery_state"))
    log.info("mission_enqueued user=%s run=%s recipient_kind=%s msg=%s existed=%s", user_id,
             run.get("id"), how, message_id, existed)
    return MissionPlan(True, "ok", run_id=str(run.get("id")), to=to, subject=composed.subject,
                       body=composed.body, already_existed=existed, shortlisted=shortlisted,
                       message_id=message_id, thread_id=thread_id, verified_send=True)


def reply_for(plan: MissionPlan) -> str | None:
    """The student-facing reply, derived ENTIRELY from what actually happened.

    This exists because the reasoner writes replies without knowing what the runtime did, which produced
    both failure modes we have now seen live: a cheerful "i've got it" for work that was never started,
    and "i can't send emails from here" for a mission that had just been enqueued. When a mission lane
    ran, its outcome — not a model — decides the words.
    """
    if plan is None or plan.status == "not_a_mission":
        return None
    if plan.enqueued and plan.verified_send:
        return "sent. i'll text u when ur reply comes in"
    if plan.status == "send_failed":
        return "gmail didn't send it, so i'm not waiting on a reply yet"
    if plan.status == "enqueue_failed":
        return "the email sent, but reply tracking didn't start. i'm fixing that"
    if plan.status == "needs_content":
        return "what do u want the email to say?"
    if plan.status == "no_recipient":
        return "who should i email? give me their address and i'll send it"
    if plan.status == "insufficient_scope":
        return "i don't have permission to send from ur gmail yet, so i can't start that"
    if plan.status in {"disconnected", "no_tool", "unsupported"}:
        return "ur gmail isn't connected here yet, so i can't send that"
    return "i couldn't start that, so i'm not going to say i did"


def mission_idempotency_key(channel: str, provider_message_id: str) -> str:
    """One inbound message -> at most one mission. Mirrors the inbound `msg:` key so the two are readable
    side by side in the runs table."""
    return f"mission:{channel}:{provider_message_id}"
