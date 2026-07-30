"""Continuation — what THIS turn does to the work already in flight, resolved BEFORE anything routes.

THE FAILURE THIS CLOSES. A student asked Bruce to email one named address a thank-you note. Twenty-two
turns produced zero missions and zero agent_runs. Inside that transcript, three separate turns were
continuations of a thing Bruce was already supposed to be doing, and all three evaporated:

  * "send it" — an approval of the draft on screen. `fast_router._SEND_INTENT` does not match it
    (verified: "send it" -> False), because the pattern is looking for the word "email". A student who
    already told Bruce what to do does not repeat the verb; they say "send it", and the router only
    recognises the FIRST statement of an intent.
  * "make it professional" — an amendment of the draft. Same miss ("make it professional" -> False), so
    it re-entered the pipeline as a brand-new, subjectless request.
  * an inline reply of "this" — a pointer at a specific artifact, which resolved to nothing at all.

The shape of the bug: routing ran first and asked "what kind of request is this text", when the
answerable question was "what does this text do to what is already open". Text alone cannot answer the
second one. State can, and state is the thing the transcript never had.

So continuation is resolved from STATE — an open goal, a pending decision, a draft, an inline reply
reference — and never from the student re-typing the original verb. A one-word turn carries almost no
signal; the run it lands on carries all of it.

FOUR RULES THAT ARE SAFETY, NOT ERGONOMICS.

  1. An affirmative with NO pending decision is NOT a confirmation. "send it" against nothing open is a
     stale yes, and `gmail.send_message` is reversible=False: there is no undo for confirming the wrong
     thing. It returns `none` with evidence, and the caller asks.
  2. An amendment is resolved BEFORE an approval, because `decision_resolver` correctly reads
     "move it to friday" and "send it to coach@school.edu" as approvals — they contain "move it" and
     "send it". They are not approvals of what is on screen; they change it. Reading them as a yes would
     send the OLD draft to the OLD address.
  3. If an amendment was clearly intended but maps to no slot on this goal (a temporal change on an
     email goal), the turn resolves to `none` — it does NOT fall through to the approval branch. Failing
     to understand a change must never become permission to proceed unchanged.
  4. Absence of an anchor is reported ("no_anchor"), never guessed. A bare "this" with nothing to point
     at is a question Bruce owes the student, and the transcript's whole texture is Bruce guessing
     instead of asking one specific question.

PURE BY CONSTRUCTION: no DB, no network, no clock, no model, no logging. The caller fetches the open
goal, the pending decision, the recent draft and the last tool result and passes them in — the same
boundary `turn_context` and `transitions` hold, and for the same reason: a resolver that fetches its own
state grows a second, drifting copy of the truth, which IS the defect.

Nothing here is logged and nothing here is stored. Every value in a `slot_patch` is message content.

WHAT THE CALLER STILL OWNS. `slot_patch` carries the student's OWN words for a slot ("friday",
"less formal"), not a typed value. A temporal phrase becomes a real start/end through `temporal.resolve`
against the user's actual timezone — this module refuses to hold a clock, and a resolver that guessed a
zone would place a Central student's Friday on a Pacific one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from . import decision_resolver, directive_scope, goal_slots, temporal
from .goal_slots import GoalKind

_EMPTY_PATCH: Mapping[str, str] = MappingProxyType({})


class ContinuationKind(str, Enum):
    """What this turn DOES to work in flight. Closed set: a caller switches on it exhaustively, and a
    sixth meaning has to be added here rather than smuggled through a confidence score."""

    none = "none"            # nothing in flight is advanced by this turn — ask, don't guess
    confirm = "confirm"      # approves a specific pending decision, by id
    reject = "reject"        # stop / don't proceed
    amend = "amend"          # change what is in flight, carried as a slot patch
    reference = "reference"  # points at a specific artifact ("this", replying to a draft)


# --- evidence: short, machine-usable, countable ------------------------------------------------------
# Typed like `transitions.REASONS`. A caller decides what question to ask from these, and an evaluation
# counts them; neither works on prose, and prose is what "i can't send messages for you" was.

NO_TEXT = "no_text"                                             # nothing the student authored
NOT_A_CONTINUATION = "not_a_continuation"                       # a fresh turn, not about what's open
NO_ANCHOR = "no_anchor"                                         # a pointer with nothing to point at
AFFIRMATIVE_WITHOUT_DECISION = "affirmative_without_pending_decision"   # the stale-yes guard
APPROVAL_OF_DECISION = "approval_of_pending_decision"
REJECTION_OF_DECISION = "rejection_of_pending_decision"
REJECTION_OF_RUN = "rejection_of_open_run"
REJECTION_UNTARGETED = "rejection_without_target"
HALT = "halt_requested"
REFERENCE_REPLY = "inline_reply_reference"
REFERENCE_DRAFT = "recent_draft"
REFERENCE_TOOL_RESULT = "recent_tool_result"
AMBIGUOUS_OPERATION_SCOPE = "ambiguous_operation_scope_asks_one_question"
# The presentation preference is a SLOT on the goal, not a global setting: "dont show me draft"
# applies to the work in flight, and the next task starts from the default again.
SLOT_SHOW_DRAFT = "show_draft"
_SLOT_TONE = "tone"
AMEND_WITHOUT_SLOT = "amend_without_matching_slot"              # understood a change, own no slot for it
AMEND_PREFIX = "amend:"                                         # + the roles that matched, sorted

EVIDENCE: frozenset[str] = frozenset({
    NO_TEXT, NOT_A_CONTINUATION, NO_ANCHOR, AFFIRMATIVE_WITHOUT_DECISION, APPROVAL_OF_DECISION,
    REJECTION_OF_DECISION, REJECTION_OF_RUN, REJECTION_UNTARGETED, HALT, REFERENCE_REPLY,
    REFERENCE_DRAFT, REFERENCE_TOOL_RESULT, AMEND_WITHOUT_SLOT, AMBIGUOUS_OPERATION_SCOPE,
})

# Confidence describes the BINDING — "this text attaches to that decision" — not the plausibility of a
# sentence. The transcript ended with a false denial at 0.97, so a number here has to mean something
# checkable: an id that exists and an anchor that was actually handed in. `none` asserts no binding at
# all, so it is 0.0 rather than a low-but-nonzero shrug a caller might act on.
_CONF_DECISION = 0.95        # bound to a real, open decision id
_CONF_RUN = 0.8              # bound to an open run but not to a decision
_CONF_UNTARGETED = 0.5       # meaning is clear, target is not
_CONF_AMEND = 0.85
_CONF_REPLY = 0.9            # the student explicitly attached the turn to a message
_CONF_DRAFT = 0.7            # inferred from the most recent draft
_CONF_TOOL_RESULT = 0.6      # inferred from the last thing that ran


# --- artifact references -----------------------------------------------------------------------------
# "<kind>:<id>" so a reference is resolvable by the caller without a second field and without a guess
# about what kind of id it holds. The transcript's "this" resolved to nothing; a bare id that could be a
# message OR a draft is barely better.

ARTIFACT_MESSAGE = "message"
ARTIFACT_DRAFT = "draft"
ARTIFACT_TOOL_RESULT = "tool_result"


def artifact_ref(kind: str, identifier: str) -> str:
    return f"{kind}:{identifier}"


def split_artifact(reference: str | None) -> tuple[str, str]:
    """("draft", "d-1") from "draft:d-1". ("", "") for anything unparseable, because a caller that got a
    malformed reference must fall back to asking, not to half of an id."""
    kind, sep, identifier = (reference or "").partition(":")
    return (kind, identifier) if sep and kind and identifier else ("", "")


@dataclass(frozen=True)
class Continuation:
    """The resolved relationship between this turn and the work in flight.

    Frozen, and every field is either a scalar or a read-only mapping: a router that could edit this
    after the fact would put the "each path decides for itself" defect back one call frame later.
    """

    kind: ContinuationKind = ContinuationKind.none
    target_run_id: str | None = None
    target_decision_id: str | None = None
    referenced_artifact: str | None = None
    slot_patch: Mapping[str, str] = _EMPTY_PATCH
    confidence: float = 0.0
    evidence: str = NOT_A_CONTINUATION

    @property
    def resolved(self) -> bool:
        """Did this turn attach to anything? The one question the router asks before it classifies."""
        return self.kind is not ContinuationKind.none


# --- reading the state the caller handed in -----------------------------------------------------------

def _get(src: object, *names: str) -> Any:
    """One field out of a mapping row (`agent_run_store` returns plain dicts) or an object
    (`turn_context.PendingDecision`, an ORM row), so the caller does not have to convert first."""
    for name in names:
        if isinstance(src, Mapping):
            if name in src and src[name] is not None:
                return src[name]
        elif getattr(src, name, None) is not None:
            return getattr(src, name)
    return None


def _text_of(value: object) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip()


# A decision that has already been answered cannot be answered again. Without this, a "yes" typed after
# a decision was resolved would re-approve an irreversible send.
_OPEN_STATUSES = frozenset({"", "pending", "open", "proposed", "asked", "awaiting_approval",
                            "awaiting_response", "needs_decision", "active"})


def _open_decision_id(pending_decision: object) -> str | None:
    if pending_decision is None:
        return None
    status = _text_of(_get(pending_decision, "status", "state")).lower()
    if status and status not in _OPEN_STATUSES:
        return None
    return _text_of(_get(pending_decision, "decision_id", "id", "mission_id")) or None


def _run_id(open_goal: object, pending_decision: object) -> str | None:
    """The run this turn is about. The goal row wins; a decision that names its own run is the fallback,
    so a confirmation is never left pointing at a decision with no run behind it.

    A decision's own `id` is NOT a candidate. Reading it as a run id would hand the caller a run that does
    not exist, and an update against a nonexistent run fails silently in exactly the way this whole program
    is trying to stop.
    """
    from_goal = _text_of(_get(open_goal, "run_id", "agent_run_id", "id")) if open_goal is not None else ""
    if from_goal:
        return from_goal
    from_decision = _text_of(_get(pending_decision, "run_id", "agent_run_id")) \
        if pending_decision is not None else ""
    return from_decision or None


def _goal_dict(open_goal: object) -> dict | None:
    """The GoalSpec blob, whether the caller passed the AgentRun row or the goal JSONB itself. Both shapes
    exist at real call sites (`agent_run_store.latest_active` returns the row), and requiring one of them
    would put a conversion step between the state and the resolver — which is where state gets lost."""
    if open_goal is None:
        return None
    inner = _get(open_goal, "goal")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(open_goal) if isinstance(open_goal, Mapping) else None


def _goal_kind(open_goal: object) -> GoalKind | None:
    """Which typed slot set this goal has. Read from the slot block first; a goal that names its
    capability resolves through the registry mapping rather than through a domain string, because
    `domain="gmail"` is not an operation and the transcript's whole second defect was treating prose as
    one."""
    goal = _goal_dict(open_goal)
    if goal is None:
        return None
    kind, _slots = goal_slots.from_goal_jsonb(goal)
    if kind is not None:
        return kind
    capability = _text_of(goal.get("capability") or goal.get("operation"))
    return goal_slots.kind_for_capability(capability) if capability else None


def _artifact_of(source: object, kind: str) -> str | None:
    if source is None:
        return None
    identifier = _text_of(_get(source, "id", "draft_id", "message_id", "provider_entity_id",
                               "entity_id", "capability"))
    return artifact_ref(kind, identifier) if identifier else None


# --- the amendment table -------------------------------------------------------------------------------
# ROLES are product-neutral on purpose. "the student changed the STYLE" and "the student changed WHEN" are
# facts about the turn; which slot they land in is a property of the goal, and it lives in one table below.
# A per-product branch here would mean the calendar path and the email path could disagree about what
# "make it shorter" is — which is exactly how the transcript ended up with three views of one turn.

ROLE_STYLE = "style"
ROLE_TEMPORAL = "temporal"
ROLE_LABEL = "label"          # what the thing is CALLED: an email subject, an event title
ROLE_CONTENT = "content"      # what it SAYS
ROLE_RECIPIENT = "recipient"  # who it goes TO

_TONE = ("professional|formal|casual|friendly|warm|polite|nice|nicer|chill|serious|enthusiastic|"
         "apologetic|grateful|short|shorter|long|longer|concise|brief|detailed|simple|simpler|"
         "direct|punchy|softer|warmer|friendlier")
# The degree word travels WITH the tone word. "less formal" patched as "formal" inverts the instruction,
# and a drafter cannot recover the difference from a slot that lost the qualifier.
_DEGREE = r"(?:more|less|a bit|a little|way|super|not so|not too|really)\s+"

_STYLE = re.compile(
    rf"\b(?:make|keep|write|rewrite|redo|word|sound)\s+(?:it|this|that|them)?\s*"
    rf"((?:{_DEGREE})?(?:{_TONE}))\b"
    rf"|\b((?:{_DEGREE})(?:{_TONE}))\b"
    rf"|\b(shorter|longer|nicer|simpler|softer|warmer|friendlier)\b")

# The date vocabulary is borrowed from `temporal` rather than retyped. This module only EXTRACTS the
# phrase; `temporal.resolve` is what turns it into a moment. Two lists would drift, and the half that
# drifted would accept a spelling the resolver then failed to understand.
_TEMPORAL = re.compile(
    rf"\b(?:next|this|last|coming)\s+(?:{temporal._WEEKDAY_ALT})\b"
    rf"|\b(?:{temporal._WEEKDAY_ALT})(?:\s+(?:morning|afternoon|evening|night))?\b"
    r"|\b(?:today|tonight|tomorrow|tmr|tmrw|tmw|next week)\b"
    rf"|\b(?:{temporal._MONTH_ALT})\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|\bat\s+\d{1,2}(?::\d{2})?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")

# What may sit BETWEEN two halves of one time expression ("friday at 4", "aug 20 @ 9am"). A day and an
# hour are one fact; handing `temporal.resolve` only the day would silently drop the time the student
# just gave, which is the same class of loss this module exists to stop.
_TEMPORAL_GAP = re.compile(r"\s*(?:at|@|around|by|on|starting)?\s*")

# Matched against the student's UNTOUCHED words: normalization folds "." into a space, which would turn
# an address into "coach@school edu".
_ADDRESS = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_LABEL = re.compile(r"\b(?:call it|name it|title it|subject (?:should be|is))\s+(.+)", re.IGNORECASE)
# Case is preserved for content because the value is words a person will read.
_CONTENT = re.compile(r"\b(?:tell\s+(?:him|her|them)|say\s+that|mention\s+(?:that\s+)?|add\s+that)\s+(.+)",
                      re.IGNORECASE)


@dataclass(frozen=True)
class _AmendRule:
    """One way a student says "change it". `on_raw` selects the untouched text for rules whose VALUE is
    the student's own words (an address, a sentence) rather than a keyword. `contiguous` means one fact
    can be written as several adjacent matches ("friday at 4") and must be carried whole."""

    role: str
    pattern: re.Pattern[str]
    on_raw: bool = False
    contiguous: bool = False


_RULES: tuple[_AmendRule, ...] = (
    _AmendRule(ROLE_STYLE, _STYLE),
    _AmendRule(ROLE_TEMPORAL, _TEMPORAL, contiguous=True),
    _AmendRule(ROLE_RECIPIENT, _ADDRESS, on_raw=True),
    _AmendRule(ROLE_LABEL, _LABEL, on_raw=True),
    _AmendRule(ROLE_CONTENT, _CONTENT, on_raw=True),
)

# Role -> the slot it lands in, per goal kind. DATA, not branches: `send_email` and `schedule_event` run
# the identical code path and differ only by this row. A role with no entry for a kind is a deliberate
# statement that the kind has no such slot (an event has no tone; an email has no start time), and it
# makes the turn resolve to `none` instead of quietly proceeding — see rule 4 in the module docstring.
#
# ROLE_RECIPIENT is email-only on purpose. The calendar equivalent would be `attendees`, which is a LIST
# and an ADD; `slot_patch` is replacement-shaped, so patching it would delete the other attendees.
_SLOT_FOR_ROLE: dict[str, dict[GoalKind, str]] = {
    ROLE_STYLE: {GoalKind.send_email: "tone"},
    ROLE_TEMPORAL: {GoalKind.schedule_event: "start"},
    ROLE_LABEL: {GoalKind.send_email: "subject", GoalKind.schedule_event: "title"},
    ROLE_CONTENT: {GoalKind.send_email: "body"},
    ROLE_RECIPIENT: {GoalKind.send_email: "recipient"},
}


def check_slot_alignment(table: dict[str, dict[GoalKind, str]] | None = None) -> tuple[str, ...]:
    """Every slot this table names must be a slot `goal_slots` actually declares. Returns the drifts.

    This is the anti-drift mechanism, not a test helper — a patch aimed at a slot nobody declares is free
    text wearing a slot's clothes, and free text that joins to nothing ("sending messages") is half of
    the original incident. `table` is injectable so the check itself can be proven to fail.
    """
    problems: list[str] = []
    for role, per_kind in (table if table is not None else _SLOT_FOR_ROLE).items():
        for kind, slot in per_kind.items():
            if goal_slots.spec_for(kind, slot) is None:
                problems.append(f"{role} -> {kind.value} declares no slot {slot!r}")
    return tuple(problems)


# Fatal AT IMPORT, like `goal_slots.check_alignment`. A patch that can never be written is worse than a
# missing feature: the student sees Bruce agree to a change that then does not happen.
_DRIFT = check_slot_alignment()
if _DRIFT:
    raise RuntimeError("continuation slot mapping has drifted from goal_slots: " + "; ".join(_DRIFT))


def _matched_value(match: re.Match[str]) -> str:
    """The captured value, or the whole match for patterns that capture nothing (a date phrase IS the
    value). Empty groups are skipped so a multi-branch pattern reports the branch that fired."""
    for group in match.groups():
        if group:
            return group.strip()
    return match.group(0).strip()


def _contiguous_value(pattern: re.Pattern[str], text: str) -> str:
    """One value spanning consecutive matches — "next tuesday" + "at 3" is a single moment, not two."""
    matches = list(pattern.finditer(text))
    if not matches:
        return ""
    start, end = matches[0].span()
    for match in matches[1:]:
        if not _TEMPORAL_GAP.fullmatch(text[end:match.start()]):
            break                                   # a second, separate mention — carry only the first
        end = match.end()
    return text[start:end].strip()


def patches_a_slot(text: str | None, kind: GoalKind | None) -> bool:
    """Would this turn write a slot on a goal of THAT kind? The public form of `_amend_patch`.

    Exists for `goal_selection`, which has to ask "could this turn's own words live in this goal" while
    choosing between two open ones. It asks THIS function rather than reimplementing the reading, because
    a selector whose idea of compatibility differed from the code that stores the value would pick a goal
    the turn then silently fails to update — the transcript's failure mode, one layer up.
    """
    raw = decision_resolver.trusted_reply_text(text)
    patch, _roles = _amend_patch(decision_resolver.normalize(raw), raw, kind)
    return bool(patch)


def _amend_patch(normalized: str, raw: str, kind: GoalKind | None) -> tuple[dict[str, str], tuple[str, ...]]:
    """(patch, roles that fired). Roles are returned even when they map to no slot, because "a change was
    requested and I own no slot for it" and "no change was requested" are different turns and must not
    produce the same answer."""
    patch: dict[str, str] = {}
    roles: list[str] = []
    for rule in _RULES:
        subject = raw if rule.on_raw else normalized
        match = rule.pattern.search(subject)
        if match is None:
            continue
        roles.append(rule.role)
        slot = _SLOT_FOR_ROLE.get(rule.role, {}).get(kind) if kind is not None else None
        value = _contiguous_value(rule.pattern, subject) if rule.contiguous else _matched_value(match)
        if slot and value:
            patch[slot] = value
    return patch, tuple(roles)


# --- pointing at something ------------------------------------------------------------------------------

# A turn that is nothing but a pointer. Politeness is stripped first so "this please" is still a pointer,
# but nothing else is: a deictic buried in a sentence ("i sent this yesterday") is not a continuation.
_POLITE = re.compile(r"\b(?:please|pls|plz|thanks|thx|ty)\b")
_DEICTIC = re.compile(r"\A(?:this|that|it|these|those|the one)(?:\s+one)?\Z")

# `decision_resolver` calls "wait" AMBIGUOUS, and it is right to: an ambiguous reply must never RESOLVE a
# decision. Continuation asks a different question — "do I keep going?" — where a halt is unambiguous and
# stopping is the safe answer under doubt. This is a stop list, not a second approval list; approval words
# live in `decision_resolver._APPROVE` and are never restated here.
_HALT = re.compile(r"\b(?:wait|hold on|hold up|hang on|pause|one sec|one second|gimme a sec|give me a sec)\b")


def _anchor(reply_ref: str | None, recent_draft: object,
            recent_tool_result: object) -> tuple[str | None, float, str]:
    """What a bare pointer points at, most explicit first. An inline reply is the student naming the
    artifact themselves; a draft or a tool result is Bruce inferring it, and the confidence says so."""
    if reply_ref:
        return reply_ref, _CONF_REPLY, REFERENCE_REPLY
    draft = _artifact_of(recent_draft, ARTIFACT_DRAFT)
    if draft:
        return draft, _CONF_DRAFT, REFERENCE_DRAFT
    result = _artifact_of(recent_tool_result, ARTIFACT_TOOL_RESULT)
    if result:
        return result, _CONF_TOOL_RESULT, REFERENCE_TOOL_RESULT
    return None, 0.0, NO_ANCHOR


def _none(evidence: str) -> Continuation:
    """No continuation. Carries evidence and nothing else: ids on a `none` invite a caller to act on a
    binding this module just declined to make."""
    return Continuation(kind=ContinuationKind.none, evidence=evidence, confidence=0.0)


# --- the resolver -----------------------------------------------------------------------------------------

def resolve(user_id: Any, *, text: str | None, reply_to_message_id: str | None,
            open_goal: object, pending_decision: object, recent_draft: object,
            recent_tool_result: object) -> Continuation:
    """Resolve this turn against work in flight. Pure; the caller fetched every piece of state.

    `user_id` is in the signature and deliberately unused: this is a per-student decision, and a helper
    that did not name the student would eventually be called with one student's text and another's open
    run. It is never logged, and no lookup is performed with it.

    Every state argument is REQUIRED rather than defaulted. A caller that forgets `pending_decision` would
    otherwise silently turn every "send it" into `none` — failing safe, but invisibly, and invisible is
    how twenty-two turns produced nothing while sounding fine.

    ORDER IS THE SAFETY PROPERTY (see the module docstring): refusal, then amendment, then halt, then
    approval, then reference. Approval is LAST among the meanings because "move it to friday" and
    "send it to <address>" both read as approvals to a yes/no resolver and are not approvals of what is
    currently on screen.
    """
    raw = decision_resolver.trusted_reply_text(text)     # the student's own words; quoted text is evidence
    normalized = decision_resolver.normalize(raw)
    if not normalized:
        return _none(NO_TEXT)

    reply_ref = artifact_ref(ARTIFACT_MESSAGE, reply_to_message_id.strip()) \
        if isinstance(reply_to_message_id, str) and reply_to_message_id.strip() else None
    run_id = _run_id(open_goal, pending_decision)
    decision_id = _open_decision_id(pending_decision)
    kind = _goal_kind(open_goal)

    resolution = decision_resolver.resolve_approval(raw)
    presentation_patch: dict = {}

    # 1. A refusal dominates everything, at any position in the message — the P0 rule `decision_resolver`
    #    already enforces. A refusal that also asks for a change is still a refusal: the caller stops and
    #    asks one question, rather than executing a half-understood edit.
    # 0. WHAT IS THE NEGATION ABOUT. `decision_resolver` answers "is there a refusal in here", which is the
    #    right question for its job and the wrong one for this one: "make it a professional email and send
    #    it dont show me draft" contains a refusal of SHOWING THE DRAFT and an approval of the send, and
    #    reading it as a refusal cancelled a real goal in production. `directive_scope` splits the clauses
    #    and says what each one governs. This does NOT weaken refusal detection — a refusal of the
    #    OPERATION still dominates below, unchanged. It only stops a presentation negation from wearing a
    #    refusal's authority.
    scope = directive_scope.interpret(raw)

    if scope.presentation_show_draft is not directive_scope.ShowDraft.unchanged:
        presentation_patch[SLOT_SHOW_DRAFT] = (
            scope.presentation_show_draft is directive_scope.ShowDraft.true)

    #    A tone edit in the SAME breath as an approval has to survive it. "make it a professional email and
    #    send it" is one turn carrying two directives, and honouring only the approval sends the heartfelt
    #    draft the student just asked to replace. Guarded by the kind's own declared slots, so a goal with
    #    no tone slot silently gains nothing rather than a key its executor cannot use.
    if scope.tone_update and kind is not None:
        if any(s.name == _SLOT_TONE for s in goal_slots.slot_specs(kind)):
            presentation_patch[_SLOT_TONE] = scope.tone_update

    #    An ambiguous OPERATION directive authorizes nothing and cancels nothing: the Decision stays
    #    pending, the caller asks exactly one question, and no provider is called. Guessing which half of
    #    a contradiction to honour is guessing about a send.
    if scope.is_ambiguous:
        return _none(AMBIGUOUS_OPERATION_SCOPE)

    if resolution is decision_resolver.Resolution.rejected and scope.approves_operation:
        # `decision_resolver` found A negation; `directive_scope` found that the OPERATION was explicitly
        # approved. Scope wins, because it is the only one of the two that knows what the negation attached
        # to. This is deliberately narrow: it requires an EXPLICIT approval of the operation, so a bare
        # "no" (scope: no operation directive) and "dont send it" (scope: reject) both still refuse,
        # unchanged.
        #
        # The earlier version of this gate additionally required a presentation signal to be present,
        # which meant "YES WRITE IT AND SEND IT NO MORE QUESTIONS" — the most emphatic instruction in the
        # transcript — was still read as a refusal, because it says nothing about the draft. The negation
        # there is "NO MORE QUESTIONS", governing questions. Nothing about it refuses the send.
        resolution = decision_resolver.Resolution.approved
    elif resolution is decision_resolver.Resolution.rejected and not scope.rejects_operation \
            and scope.presentation_show_draft is not directive_scope.ShowDraft.unchanged:
        # The turn's only negation was about PRESENTATION and it said nothing either way about the
        # operation. It must not close the Decision — "dont show me the draft" is an instruction about how
        # to behave, not a withdrawal — so it falls through to the amendment below carrying its show_draft
        # patch. Unresolved rather than approved: changing how Bruce presents something is not permission
        # to do it.
        resolution = decision_resolver.Resolution.unrelated

    if resolution is decision_resolver.Resolution.rejected:
        if decision_id:
            return Continuation(ContinuationKind.reject, run_id, decision_id, reply_ref,
                                confidence=_CONF_DECISION, evidence=REJECTION_OF_DECISION)
        if run_id:
            return Continuation(ContinuationKind.reject, run_id, None, reply_ref,
                                confidence=_CONF_RUN, evidence=REJECTION_OF_RUN)
        return Continuation(ContinuationKind.reject, None, None, reply_ref,
                            confidence=_CONF_UNTARGETED, evidence=REJECTION_UNTARGETED)

    # 2. A change to what is in flight. Ahead of approval on purpose.
    patch, roles = _amend_patch(normalized, raw, kind)
    if presentation_patch:
        # The ROLE-based extractor wins on any key it set: it reads degree words ("a lot less formal")
        # that the clause parser deliberately does not. This only fills what it left empty.
        patch = {**presentation_patch, **dict(patch or {})}
    if patch:
        return Continuation(ContinuationKind.amend, run_id, decision_id, reply_ref,
                            slot_patch=MappingProxyType(dict(patch)), confidence=_CONF_AMEND,
                            evidence=AMEND_PREFIX + "+".join(sorted(patch)))
    if roles:
        # A change was asked for that this goal has no slot for. Deliberately NOT falling through to the
        # approval branch: "move it to friday" against an email goal must not become a send.
        return _none(AMEND_WITHOUT_SLOT)

    # 3. "wait" — stop, without resolving anything.
    if _HALT.search(normalized):
        return Continuation(ContinuationKind.reject, run_id, decision_id, reply_ref,
                            confidence=_CONF_DECISION if decision_id else _CONF_UNTARGETED, evidence=HALT)

    # 4. A clean yes. Only ever bound to a decision that actually exists — the stale-yes guard.
    if resolution is decision_resolver.Resolution.approved:
        if not decision_id:
            return _none(AFFIRMATIVE_WITHOUT_DECISION)
        return Continuation(ContinuationKind.confirm, run_id, decision_id, reply_ref,
                            confidence=_CONF_DECISION, evidence=APPROVAL_OF_DECISION)

    # 5. A bare pointer. With an anchor it names an artifact; without one it is a question Bruce owes the
    #    student, and saying "no_anchor" out loud is what makes the caller ask instead of guess.
    stripped = _POLITE.sub(" ", normalized).strip()
    if _DEICTIC.fullmatch(re.sub(r"\s+", " ", stripped)):
        reference, confidence, evidence = _anchor(reply_ref, recent_draft, recent_tool_result)
        if reference is None:
            return _none(NO_ANCHOR)
        return Continuation(ContinuationKind.reference, run_id, decision_id, reference,
                            confidence=confidence, evidence=evidence)

    return _none(NOT_A_CONTINUATION)
