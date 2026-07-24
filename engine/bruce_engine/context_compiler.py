"""ContextCompiler (G0.2) — assemble a BOUNDED, typed, prioritized context from the layered memory
(profile / world / entity / operational / episodic) instead of dumping raw recent history at the model.

Bruce must not shove the whole conversation window at the reasoner every turn. The compiler decides WHAT
grounds a decision and in what priority, under a token budget, so the load-bearing facts always survive:
the student's timezone (WORLD) and any open agent run (OPERATIONAL) outrank the calendar entities (ENTITY),
which outrank the raw conversation window (EPISODIC) — episodic yields budget FIRST and is bounded, never
the entire history. Nothing is cut silently: whatever the budget dropped is recorded on the result.

Provider-neutral and model-free: it READS stores (world_state / entity_store / agent_run_store /
conversation_store) and formats prose. It never calls a model — summarizing long history is a separate,
more expensive concern left as a seam. The reasoner still consumes an opaque `context: str`; this module
owns how that string is built. A store hiccup degrades ONE layer to omitted — it never drops the turn.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from . import agent_run_store, entity_store, world_state
from .calendar_schedule import DEFAULT_TZ
from .conversation_style import VoiceProfile

# ~4 chars/token is the standard rough estimate; we have no tokenizer in-tree and do not want the dep on the
# hot path. A generous budget: 8 recent turns (~1.6k chars) plus world/entity/operational comfortably fit,
# so truncation only fires on genuinely large state — exactly when prioritization must decide what survives.
_CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = 1200
_MAX_ENTITIES = 8            # newest active events shown; more than this is noise for one decision
_MAX_TURNS = 8               # episodic window ceiling (budget may trim below this)

# layer priorities — higher survives truncation. World/operational are tiny and decision-critical; the raw
# conversation window is the largest and least essential, so it yields budget first.
_P_WORLD, _P_OPERATIONAL, _P_ENTITY, _P_EPISODIC = 100, 90, 80, 50

_NO_HISTORY = "No prior conversation."


@dataclass(frozen=True)
class ContextBlock:
    layer: str            # "world" | "operational" | "entity" | "episodic"
    priority: int
    text: str
    est_tokens: int


@dataclass(frozen=True)
class CompiledContext:
    """The single typed context object for one turn. `text` is the bounded prose the reasoner consumes;
    `blocks` is what survived (highest priority first); `dropped` names any layer/segment the budget cut
    (honesty: no silent truncation); `profile` carries the PROFILE layer structurally (it drives styling,
    not model reasoning, so it is never flattened into `text`)."""
    text: str
    blocks: tuple[ContextBlock, ...] = ()
    dropped: tuple[str, ...] = ()
    est_tokens: int = 0
    profile: VoiceProfile | None = None


def _est_tokens(s: str) -> int:
    return (len(s) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN if s else 0


def _fmt_when(start: str | None, end: str | None) -> str:
    """Absolute, now-independent compact time for an entity line ("Jul 25 3:00–4:00pm", "Jul 25 (all day)").
    Absolute (not "tomorrow") so the model isn't handed a relative anchor — the pipeline's TemporalResolver
    stays the authority on relative math. Deterministic: no dependence on the current clock."""
    if not isinstance(start, str) or not start:
        return "time tbd"
    try:
        if len(start) == 10:                                  # all-day date
            d = _dt.date.fromisoformat(start)
            return f"{d:%b %-d} (all day)"
        s = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "time tbd"

    def _clock(t: _dt.datetime) -> str:
        return f"{t:%-I:%M%p}".lower().replace(":00", "")     # "3pm", "3:30pm"

    out = f"{s:%b %-d} {_clock(s)}"
    if isinstance(end, str) and len(end) > 10:
        try:
            e = _dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
            out = f"{s:%b %-d} {_clock(s)}–{_clock(e)}" if e.date() == s.date() else f"{out}–{e:%b %-d} {_clock(e)}"
        except (ValueError, TypeError):
            pass
    return out


# Each builder fault-isolates its WHOLE body, not just the store read: a malformed row (a non-dict JSONB
# `goal`, a non-string `start`) must degrade ITS layer to omitted, never throw past the guard and collapse
# the entire compiled context to the legacy fallback. One bad layer ≠ losing world+entity+episodic.
async def _world_block(user_id) -> str | None:
    try:
        tz = await world_state.get_timezone(user_id)
        if not tz:
            return None
        return f"The student's timezone is {world_state.friendly_name(tz)}."
    except Exception:
        return None


async def _operational_block(user_id) -> str | None:
    try:
        run = await agent_run_store.latest_active(user_id)
        if not run:
            return None
        goal = run.get("goal")
        goal = goal if isinstance(goal, dict) else {}         # JSONB can be a str/list/number — coerce
        what = goal.get("desired_outcome") or goal.get("title") or run.get("domain") or "a request"
        line = f"Open task: you're partway through {what} (status: {run.get('status') or 'in progress'})."
        tail = run.get("blocked_reason") or run.get("current_action")
        if tail:
            line += f" Next: {tail}."
        return line
    except Exception:
        return None


async def _entity_block(user_id) -> str | None:
    try:
        events = await entity_store.active_events(user_id, limit=_MAX_ENTITIES)
        if not events:
            return None
        lines = [f"- {e.get('title', 'untitled')} — {_fmt_when(e.get('start'), e.get('end'))}"
                 for e in events[:_MAX_ENTITIES]]
        return "On the student's calendar:\n" + "\n".join(lines)
    except Exception:
        return None


def _episodic_block(recent, *, include: bool) -> str | None:
    """The bounded conversation window. When history is deliberately withheld (an explicit reply-target owns
    the context), render the honest marker so the model knows it has none — never fabricate turns."""
    if not include:
        return _NO_HISTORY
    lines = [f"{t.role}: {t.text}" for t in (recent or []) if getattr(t, "text", None)]
    if not lines:
        return None
    return "Recent conversation (oldest first):\n" + "\n".join(lines[-_MAX_TURNS:])


def _trim_episodic(text: str, budget_tokens: int) -> str | None:
    """Drop the OLDEST turn lines until the block fits the remaining budget (keep the header + newest turns).
    Returns None rather than a header with zero turns — a section promising recent conversation followed by
    nothing is misleading. An atomic block (no header/body split, e.g. the withheld marker) is all-or-nothing."""
    if budget_tokens <= 0:
        return None
    if "\n" not in text:                                      # atomic (e.g. "No prior conversation.")
        return text if _est_tokens(text) <= budget_tokens else None
    head, _, body = text.partition("\n")
    lines = body.split("\n") if body else []
    while lines:
        candidate = head + "\n" + "\n".join(lines)
        if _est_tokens(candidate) <= budget_tokens:
            return candidate
        lines.pop(0)                                          # drop oldest
    return None                                               # no turn line fit -> drop, no dangling header


async def compile(user_id, recent, *, include_episodic: bool = True,
                  profile: VoiceProfile | None = None,
                  token_budget: int = DEFAULT_TOKEN_BUDGET) -> CompiledContext:
    """Build the bounded context for one turn. `recent` is the episodic window
    (conversation_store.TurnBrief list); `include_episodic=False` withholds it (an explicit reply-target
    owns the context) while world/entity/operational still ground the reply."""
    candidates: list[tuple[str, int, str]] = []
    for layer, prio, text in (
        ("world", _P_WORLD, await _world_block(user_id)),
        ("operational", _P_OPERATIONAL, await _operational_block(user_id)),
        ("entity", _P_ENTITY, await _entity_block(user_id)),
        ("episodic", _P_EPISODIC, _episodic_block(recent, include=include_episodic)),
    ):
        if text:
            candidates.append((layer, prio, text))

    included: list[ContextBlock] = []
    dropped: list[str] = []
    used = 0
    for layer, prio, text in sorted(candidates, key=lambda c: -c[1]):
        tk = _est_tokens(text)
        if used + tk <= token_budget:
            included.append(ContextBlock(layer, prio, text, tk))
            used += tk
        elif layer == "episodic":                             # the one trimmable layer — keep the newest
            trimmed = _trim_episodic(text, token_budget - used)
            if trimmed:
                tk2 = _est_tokens(trimmed)
                included.append(ContextBlock(layer, prio, trimmed, tk2))
                used += tk2
                dropped.append("episodic:trimmed")
            else:
                dropped.append(layer)
        else:
            dropped.append(layer)

    body = "\n\n".join(b.text for b in included) if included else _NO_HISTORY
    return CompiledContext(text=body, blocks=tuple(included), dropped=tuple(dropped),
                           est_tokens=used, profile=profile)
