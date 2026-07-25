"""User writing profile + voice memory (email quality layer).

Bruce learns how a specific user writes and persists it, so their emails sound like THEM, not like one
generic assistant voice: preferred greeting and sign-off, normal formality, sentence length, capitalization,
whether they use contractions, phrases they dislike, and per-recipient overrides. Stored under the user's
world-state preferences (no new table). A correction the user types in chat ("make it less formal", "sound
more like me", "keep it short") updates the CURRENT draft's directives and, when they signal it's a standing
preference ("always", "from now on"), the persisted profile too.

This is deliberately NOT texting-slang everywhere: slang belongs in a peer email only when the profile or the
relationship allows it, never in a teacher email unless the user explicitly asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import user_session

_PREF_KEY = "email_writing"


@dataclass(frozen=True)
class UserWritingProfile:
    """User-controlled voice preferences. None = 'no opinion, use the relationship default'."""
    sender_name: str | None = None
    greeting: str | None = None            # e.g. "Hi", "Hey", "Hello"
    sign_off: str | None = None            # e.g. "Thanks", "Best", "" (none)
    formality: int | None = None           # 1..5 override on top of the relationship
    sentence_length: str | None = None     # "short" | "medium"
    capitalization: str | None = None      # "standard" | "lowercase"
    contractions: bool | None = None       # True = prefer contractions (i'll, can't)
    disliked_phrases: tuple[str, ...] = ()  # phrases to never use for THIS user
    recipient_style: dict = field(default_factory=dict)   # {recipient_key: {field: value}} overrides

    def for_recipient(self, recipient_key: str | None) -> "UserWritingProfile":
        """Fold a recipient-specific override (e.g. always formal with 'coach_smith') over the base profile."""
        if not recipient_key:
            return self
        ov = self.recipient_style.get(recipient_key) or {}
        if not ov:
            return self
        base = {k: v for k, v in self.__dict__.items() if k not in ("recipient_style",)}
        base.update({k: v for k, v in ov.items() if k in base})
        return replace(self, **base)

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict | None) -> "UserWritingProfile":
        d = dict(d or {})
        if "disliked_phrases" in d and d["disliked_phrases"] is not None:
            d["disliked_phrases"] = tuple(d["disliked_phrases"])
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in allowed})


# --- persistence (user_world_state.preferences[_PREF_KEY]) ----------------------------------------

async def get_writing_profile(user_id: UUID) -> UserWritingProfile:
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.UserWorldState).where(
            schema.UserWorldState.user_id == user_id))).scalar_one_or_none()
        prefs = (row.preferences or {}) if row is not None else {}
        return UserWritingProfile.from_dict(prefs.get(_PREF_KEY))


async def save_writing_profile(user_id: UUID, profile: UserWritingProfile) -> None:
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.UserWorldState).where(
            schema.UserWorldState.user_id == user_id))).scalar_one_or_none()
        if row is None:
            row = schema.UserWorldState(user_id=user_id, preferences={})
            s.add(row)
        prefs = dict(row.preferences or {})
        prefs[_PREF_KEY] = profile.to_dict()
        row.preferences = prefs        # reassign so SQLAlchemy sees the JSONB change
        await s.flush()


# --- corrections ("make it less formal", "sound more like me", "keep it short") -------------------

_PERSIST_SIGNAL = re.compile(r"\b(always|from now on|going forward|every time|by default)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CorrectionResult:
    profile: UserWritingProfile        # profile to recompose the current draft with (transient unless persisted)
    persist: bool                      # whether the user asked to make it a standing preference
    directives: tuple[str, ...]        # extra composer directives for this recompose ("shorter", "warmer")
    understood: bool                   # False -> we could not map the instruction to a change


def apply_correction(profile: UserWritingProfile, instruction: str) -> CorrectionResult:
    """Map a natural correction to profile changes + recompose directives. Deterministic (no model): the
    common corrections are recognized; anything else is passed through as a free directive for the composer."""
    t = (instruction or "").lower()
    changes: dict = {}
    directives: list[str] = []
    understood = False

    if re.search(r"\b(less formal|not (so |too )?formal|more casual|loosen)\b", t):
        changes["formality"] = max(1, (profile.formality or 3) - 1); directives.append("less formal, warmer"); understood = True
    if re.search(r"\b(more formal|too casual|more professional|more polished)\b", t):
        changes["formality"] = min(5, (profile.formality or 3) + 1); directives.append("more polished"); understood = True
    if re.search(r"\b(shorter|keep it short|too long|tighten|trim|concise|cut it down)\b", t):
        changes["sentence_length"] = "short"; directives.append("shorter, tighter"); understood = True
    if re.search(r"\b(sound more like me|more like me|in my voice|my style)\b", t):
        directives.append("match the user's own voice from their profile"); understood = True
    if re.search(r"\b(less ai|don'?t (make it )?sound (like )?ai|not robotic|less generic|more human)\b", t):
        directives.append("more human, remove any generic or AI-sounding phrasing"); understood = True
    if re.search(r"\b(respectful but not stiff|polite but not stiff|not stiff)\b", t):
        directives.append("respectful but relaxed, not stiff"); understood = True
    if re.search(r"\b(warmer|friendlier|nicer)\b", t):
        directives.append("a touch warmer"); understood = True
    if re.search(r"\b(more direct|get to the point|blunter)\b", t):
        directives.append("more direct, lead with the ask"); understood = True

    if not understood:
        # unknown instruction -> hand it to the composer verbatim as a directive, don't silently drop it
        directives.append(instruction.strip())

    persist = bool(_PERSIST_SIGNAL.search(t)) and bool(changes)
    new_profile = replace(profile, **changes) if changes else profile
    return CorrectionResult(profile=new_profile, persist=persist,
                            directives=tuple(d for d in directives if d), understood=understood)
