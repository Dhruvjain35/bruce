"""The structured facts a notification is allowed to know, and the deterministic floor that words them.

The wording layer never sees a raw provider payload or a whole email thread. It sees a NotificationBrief:
a small set of VERIFIED fields. That separation is the point — a model may choose phrasing and nothing
else. It cannot decide whether a reply exists, who sent it, whether the mission completed, or whether a
summary is grounded. Those are decided here, from the run.

Bruce should sound like a capable friend who noticed something useful, not like a database row. "hey,
dhruvhydrox@gmail.com replied to the email i sent for you" is what a template produces; "coach replied.
practice moved to 6" is what a person would say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Relationship(str, Enum):
    teacher = "teacher"
    coach = "coach"
    professional = "professional"
    counselor = "counselor"
    peer = "peer"
    support = "support"
    self_ = "self"
    unknown = "unknown"


class Urgency(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"


@dataclass(frozen=True)
class NotificationBrief:
    """Everything the wording layer may use. A fact absent here may not appear in the notification."""

    event_type: str                       # reply_received | no_reply | send_failed
    verified: bool                        # the fact itself was verified (read-back / header linkage)
    agent_run_id: str
    receipt_id: str                       # the delivery idempotency key
    actor_name: str | None = None         # "mr. smith", "coach", "maya" — a NAME, not an address
    actor_address: str | None = None      # only used when no name/relationship is known
    actor_relationship: Relationship = Relationship.unknown
    thread_subject: str | None = None
    user_goal: str | None = None          # what the student asked for, in their words
    reply_summary: str | None = None      # GROUNDED summary, or None. Never invented.
    important_dates: tuple[str, ...] = ()
    important_requests: tuple[str, ...] = ()
    urgency: Urgency = Urgency.normal
    suggested_next_action: str | None = None
    confidence: float = 1.0               # confidence in reply_summary specifically
    sensitive_content: bool = False
    source_entity_id: str | None = None
    recent_openings: tuple[str, ...] = field(default=())   # so consecutive texts do not all start alike

    def grounding(self) -> str:
        """Every string a notification may draw concrete claims from."""
        parts = [self.actor_name or "", self.thread_subject or "", self.reply_summary or "",
                 *self.important_dates, *self.important_requests, self.suggested_next_action or ""]
        return "\n".join(p for p in parts if p)


# --- the deterministic floor ------------------------------------------------------------------------

_SUMMARY_MIN_CONFIDENCE = 0.75
MAX_CHARS = 140            # a notification is a text message, not a paragraph


def _who(brief: NotificationBrief) -> str:
    """How to refer to the sender. A real name or relationship beats an address every time; an address is
    a last resort, because "dhruvhydrox@gmail.com replied" is how a database talks."""
    if brief.actor_relationship is Relationship.self_:
        return "ur reply"
    if brief.actor_name:
        return brief.actor_name
    rel = brief.actor_relationship
    if rel is Relationship.teacher:
        return "ur teacher"
    if rel is Relationship.coach:
        return "coach"
    if rel is Relationship.counselor:
        return "ur counselor"
    if rel in (Relationship.professional, Relationship.support, Relationship.peer):
        return "they"
    return "they"


def deterministic_notification(brief: NotificationBrief) -> str:
    """The always-available floor: short, true, and never a fixed template applied to every event.

    Shape is chosen by what is actually KNOWN — a grounded summary if there is one, otherwise the plain
    fact. Nothing here can invent content, because it only ever concatenates fields off the brief.
    """
    if brief.event_type == "no_reply":
        return "no reply yet on that email. want me to keep waiting?"
    if brief.event_type == "send_failed":
        return "gmail didn't send it, so i'm not waiting on a reply yet"

    who = _who(brief)
    summary = brief.reply_summary if (brief.reply_summary and brief.confidence >= _SUMMARY_MIN_CONFIDENCE
                                      and not brief.sensitive_content) else None

    if who == "ur reply":
        head = "ur reply came in"
    elif who == "they":
        head = "they replied"
    else:
        head = f"{who} replied"

    if summary:
        text = f"{head}. {summary.rstrip('.')}"
    elif brief.reply_summary and not brief.sensitive_content:
        # we have something but are not confident enough to assert it — offer, never claim
        text = f"{head}. want me to pull out the important part?"
    else:
        text = head

    if brief.suggested_next_action and len(text) + len(brief.suggested_next_action) + 2 <= MAX_CHARS:
        text = f"{text}. {brief.suggested_next_action.rstrip('.')}"
    return text[:MAX_CHARS]


# --- the deterministic truth validator ---------------------------------------------------------------

_BANNED = (
    re.compile(r"\bi detected\b", re.I),
    re.compile(r"\bnotification\b", re.I),
    re.compile(r"\bmonitored thread\b", re.I),
    re.compile(r"\bon your behalf\b", re.I),
    re.compile(r"\bthe email i sent for you\b", re.I),
    re.compile(r"\breply_detected\b", re.I),
    re.compile(r"\bagent ?run\b", re.I),
    re.compile(r"\bthread id\b", re.I),
)
_EM_DASH = "—"
_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NUM = re.compile(r"\d+")


@dataclass(frozen=True)
class NotificationVerdict:
    ok: bool
    issues: tuple[str, ...] = ()


def validate_notification(text: str, brief: NotificationBrief) -> NotificationVerdict:
    """Deterministic. The model may phrase; it may not introduce facts, system language, or an address.

    Fails closed on anything ungrounded, because the alternative is a confident text about something that
    did not happen — which is the exact failure this whole subsystem exists to prevent.
    """
    issues: list[str] = []
    t = (text or "").strip()
    if not t:
        return NotificationVerdict(False, ("empty",))
    if len(t) > MAX_CHARS:
        issues.append("too_long")
    if _EM_DASH in t:
        issues.append("em_dash")
    for rx in _BANNED:
        if rx.search(t):
            issues.append("system_language")
            break

    grounding = brief.grounding().lower()
    for addr in _ADDR.findall(t):
        # an address may only appear when it is the ONLY identity we have
        if brief.actor_name or brief.actor_relationship is not Relationship.unknown:
            issues.append("address_when_name_known")
            break
        if addr.lower() != (brief.actor_address or "").lower():
            issues.append("unknown_address")
            break

    # numbers must be grounded (times, dates, room numbers) — a hallucinated "6pm" is a real-world error
    for n in _NUM.findall(t):
        if n not in grounding:
            issues.append("ungrounded_number")
            break

    # a claim of a reply requires a verified reply
    if re.search(r"\breplied\b|\breply came in\b|\bgot back to (you|u)\b", t, re.I):
        if brief.event_type != "reply_received" or not brief.verified:
            issues.append("unverified_reply_claim")

    # a summary may only be asserted when one exists and we are confident in it
    if brief.reply_summary is None and re.search(r"\bthey said\b|\bhe said\b|\bshe said\b", t, re.I):
        issues.append("invented_summary")
    if brief.sensitive_content and brief.reply_summary and brief.reply_summary.lower()[:20] in t.lower():
        issues.append("sensitive_leak")

    return NotificationVerdict(not issues, tuple(dict.fromkeys(issues)))
