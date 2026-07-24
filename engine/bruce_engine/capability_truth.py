"""Capability-claim validator (R10) — the last line against Bruce lying about what it can do.

Every capability claim derives from the ToolRegistry + live connection state, never a hard-coded belief.
This kills the contradiction from the live test: `create_event` works, yet "can u update my calendar"
returned "i can't update your calendar from here" — a flat denial that sounds like Bruce can't touch the
calendar at all. The registry knows create is live and update isn't, so the honest reply is "i can add
events rn, but updating existing ones isn't live yet" — and the moment update is wired (flip one registry
flag) this validator stops producing that line.

Two jobs, both registry-driven:
  1. If a reply DENIES a calendar capability that is actually live for this user, override it with the
     grounded truth (never a false "i can't").
  2. Make the override SPECIFIC to what the user asked: create is live -> affirm; update/delete not live
     -> say exactly that, not a blanket denial.

It never manufactures a "done" — verification of a real write lives in the operation graph, not here.
"""

from __future__ import annotations

import re

from . import tool_registry

# "i can't (actually) schedule/add/update/put/create/delete … calendar/event", "unable to add to your
# calendar", "i don't have access to your calendar", "can't … from here" in a calendar context.
_CAL_DENIAL_RE = re.compile(
    r"\bi\s?(?:can'?t|cannot|can\s+not|am\s+unable\s+to|'?m\s+unable\s+to|do\s+not|don'?t)\b"
    r"[^.?!\n]*\b(?:schedule|add|put|create|make|set\s+up|update|change|move|edit|delete|remove|cancel|access)\b"
    r"[^.?!\n]*\b(?:calendar|event|cal|invite)\b"
    r"|\bcan'?t\s+(?:actually\s+)?(?:schedule|add|create|make|put|update|change|move|delete)\b[^.?!\n]*\b(?:calendar|event|cal)\b"
    r"|\bschedule\s+calendar\s+events?\s+from\s+here\b",
    re.IGNORECASE,
)
# What the user was asking Bruce to DO (drives the specific, honest answer).
_MUTATE_RE = re.compile(r"\b(update|updating|change|changing|move|moving|reschedul\w*|edit|editing|"
                        r"delete|deleting|remove|removing|cancel\w*)\b", re.IGNORECASE)


def mentions_calendar_denial(text: str | None) -> bool:
    """Cheap first pass (no DB): does this reply DENY a calendar capability? Only then does the caller pay
    for a connection lookup to decide whether the denial is actually false."""
    return bool(_CAL_DENIAL_RE.search(text or ""))


def grounded_calendar_correction(user_text: str | None = None) -> str:
    """The honest replacement for a calendar denial when the calendar is connected — SPECIFIC to what the
    user asked and to what the registry says is live. Never claims anything is scheduled."""
    wants_mutation = bool(_MUTATE_RE.search(user_text or ""))
    update_live = tool_registry.is_live("calendar.update_event")
    create_live = tool_registry.is_live("calendar.create_event")
    if wants_mutation and update_live:
        return "yeah i can move or delete events on ur calendar too. which one do u mean?"
    if wants_mutation and create_live:      # update not wired yet
        return ("i can add stuff to ur calendar rn, but updating or deleting existing events isn't live "
                "yet, that's next. want me to add something?")
    if create_live:
        return ("actually i can add this to ur google calendar, it's connected. just tell me what + when "
                "and i'll put it on there + confirm once it's on.")
    # create itself not live -> honest about the whole capability
    return "adding to ur google calendar isn't live for u yet."
