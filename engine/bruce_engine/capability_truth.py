"""Capability-claim validator (P0.5) — the last line against Bruce telling the user it CAN'T do
something it actually can.

The failure this guards: a model reply said "i can't actually schedule calendar events from here" while
Google Calendar was connected and the real adapter was deployed. A capability CLAIM ("i can't …") must
be grounded in the live connection state, never invented by the model. This validator detects a calendar
scheduling-denial and, when the calendar is in fact connected + the write action is available, replaces
it with a grounded, honest correction that routes the user to the working path.

Deliberately narrow: it only overrides a FALSE denial (model says can't, reality says can). It never
manufactures a "done" — verification of an actual write lives in the calendar operation graph, not here.
"""

from __future__ import annotations

import re

# "i can't (actually) schedule/add/create/put … calendar/event", "unable to add to your calendar",
# "i don't have access to your calendar", "can't do that from here" (in a calendar context).
_CAL_DENIAL_RE = re.compile(
    r"\bi\s?(?:can'?t|cannot|can\s+not|am\s+unable\s+to|'?m\s+unable\s+to|do\s+not|don'?t)\b"
    r"[^.?!\n]*\b(?:schedule|add|put|create|make|set\s+up|access)\b"
    r"[^.?!\n]*\b(?:calendar|event|cal|invite)\b"
    r"|\bcan'?t\s+(?:actually\s+)?(?:schedule|add|create|make|put)\b[^.?!\n]*\b(?:calendar|event|cal)\b"
    r"|\b(?:no|not)\s+able\s+to\s+(?:schedule|add|create|put)\b[^.?!\n]*\b(?:calendar|event|cal)\b"
    r"|\bschedule\s+calendar\s+events?\s+from\s+here\b",
    re.IGNORECASE,
)


def mentions_calendar_denial(text: str | None) -> bool:
    """Cheap first pass (no DB): does this reply DENY a calendar-scheduling capability? Only when this is
    True does the caller pay for a connection lookup to decide whether the denial is actually false."""
    return bool(_CAL_DENIAL_RE.search(text or ""))


def grounded_calendar_correction() -> str:
    """The honest replacement for a FALSE calendar denial when the calendar is connected. It does NOT
    claim anything is scheduled — it tells the truth (Bruce can) and routes to the working path."""
    return ("actually i can add this to ur google calendar, it's connected. "
            "just say \"schedule this\" (or reply to the flyer with it) and i'll put it on there + "
            "confirm once it's actually on.")
