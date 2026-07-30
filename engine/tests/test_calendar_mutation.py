"""calendar_mutation classify + recompute (pure) — merge only what the user changed onto the entity."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from bruce_engine import calendar_mutation as cm

NOW = dt.datetime(2026, 7, 23, 15, 0, tzinfo=ZoneInfo("America/Chicago"))   # Thu Jul 23 2026


def test_classify():
    assert cm.classify("delete chess class") == "delete"
    assert cm.classify("cancel the meeting") == "delete"
    assert cm.classify("move chess class to 9pm") == "update"
    assert cm.classify("reschedule practice to friday") == "update"
    assert cm.classify("not today, i said 4 days from now") == "repair"
    assert cm.classify("i meant tomorrow") == "repair"
    assert cm.classify("add chess class tomorrow") is None      # a create, not a mutation


def test_recompute_time_only_keeps_date():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    start, end, tz = cm.recompute(entity, "move it to 9pm", now=NOW)
    assert start == "2026-07-24T21:00:00"                        # date kept, time changed


def test_recompute_midnight():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    start, _e, _t = cm.recompute(entity, "change it to midnight", now=NOW)
    assert start == "2026-07-24T00:00:00"


def test_recompute_date_only_keeps_time():
    # "not today, 4 days from now" on a timed event keeps the clock, fixes the date
    entity = {"start": "2026-07-23T14:00:00", "timezone": "America/Chicago"}
    start, _e, _t = cm.recompute(entity, "not today, i said 4 days from now", now=NOW)
    assert start == "2026-07-27T14:00:00"                        # +4 days, 2pm kept


def test_recompute_none_when_no_temporal():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    assert cm.recompute(entity, "move chess class", now=NOW) is None


def test_classify_ignores_create_with_make_it():
    assert cm.classify("add chess class friday, make it 5pm") is None      # create, not a mutation
    assert cm.classify("put lunch on my calendar and make it an hour") is None
    assert cm.classify("cancel that plan with mike") == "delete"           # verb present (referent gate handles it)


def test_recompute_preserves_duration():
    entity = {"start": "2026-07-24T15:00:00", "end": "2026-07-24T17:00:00", "timezone": "America/Chicago"}
    start, end, _tz = cm.recompute(entity, "move it to 9pm", now=NOW)
    assert start == "2026-07-24T21:00:00" and end == "2026-07-24T23:00:00"  # 2h duration kept


# --- the deictic referent: "move it to friday" ------------------------------------------------------------
# `entity_resolution` refuses a lone "it" and is RIGHT to — a bare "it" is in almost any sentence, and
# letting it select the student's only event would turn "i'll deal with it later" into a provider write.
# What it cannot see is that a mutation verb was already classified in the student's own trusted words.
# `resolve_target` is that layer, and every widening below is asserted against the case it must NOT cover.

import pytest as _pytest  # noqa: E402

from bruce_engine import entity_resolution as _er  # noqa: E402


class _FakeStore:
    """`entity_store.active_events`, scripted — the resolution is what is under test, not the query.
    Everything else on the module is delegated to the real one, so title matching stays real."""

    def __init__(self, events):
        self.events = events
        from bruce_engine import entity_store as _real
        self.normalize_title = _real.normalize_title

    async def active_events(self, user_id, limit=None):
        return self.events[:limit] if limit else list(self.events)


def _event(title, eid="evt_1"):
    return {"id": eid, "title": title, "normalized_title": title.lower(),
            "provider_event_id": eid, "start": "2026-07-24T16:00:00", "end": "2026-07-24T17:00:00"}


@_pytest.fixture
def _one(monkeypatch):
    monkeypatch.setattr(_er, "entity_store", _FakeStore([_event("chess club")]))


@_pytest.fixture
def _two(monkeypatch):
    monkeypatch.setattr(_er, "entity_store",
                        _FakeStore([_event("chess club"), _event("dentist", "evt_2")]))


def _resolve(kind, text):
    import asyncio
    from uuid import uuid4
    return asyncio.run(cm.resolve_target(uuid4(), kind, text))


def test_a_bare_pointer_on_a_move_resolves_to_the_one_active_event(_one):
    """THE DEFECT. "move it to friday" reached no handler at all: the goal seam has no calendar goal to
    patch and entity resolution refused the pointer, so the student's move was answered by the model."""
    res = _resolve("update", "move it to friday")
    assert res.status == "resolved" and res.entity["title"] == "chess club"


def test_the_same_pointer_with_two_events_open_asks_instead_of_guessing(_two):
    """A pointer that could mean either IS a question. Answering it by recency is how the wrong event
    gets moved with nothing ever saying so."""
    assert _resolve("update", "move it to friday").status == "ambiguous"


def test_a_mutation_verb_with_no_pointer_still_resolves_nothing(_one):
    """A verb alone is not a reference. "reschedule everything" names nothing, and the fallback must not
    quietly turn it into the student's only event."""
    assert _resolve("update", "reschedule everything").status == "not_found"


def test_a_bare_pointer_never_resolves_a_delete(_one):
    """Deletion cannot be taken back, so it keeps having to name its target — "delete it" stays a
    question even when there is only one event it could mean."""
    assert _resolve("delete", "delete it").status == "not_found"


def test_a_named_event_still_wins_over_the_fallback(_two):
    """The positive control for the whole ordering: a title resolves directly and the pointer rules are
    never consulted, so naming the event is still the unambiguous way to say which one."""
    res = _resolve("update", "move chess club to friday")
    assert res.status == "resolved" and res.entity["title"] == "chess club"


def test_a_title_less_correction_keeps_its_own_most_recent_fallback(_two):
    """A repair points at the operation just performed, and that rule is unchanged — including with
    several events open, where a MOVE would ask."""
    assert _resolve("repair", "not today, i said friday").status == "resolved"
