"""Reference resolution — title match, generic pointer, and FAIL-CLOSED on ambiguity. active_events is
stubbed so this stays a pure-logic test."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from bruce_engine import entity_resolution as er
from bruce_engine import entity_store


def _stub(events):
    async def _f(user_id, *, limit=50):
        return events[:limit]
    return _f


def _run(c):
    return asyncio.run(c)


CHESS = {"id": "1", "title": "Chess Class", "normalized_title": "chess class", "start": "2026-07-24T20:00:00"}
BBALL = {"id": "2", "title": "Basketball Tournament", "normalized_title": "basketball tournament", "start": "2026-07-27T14:00:00"}


def test_title_match_resolves(monkeypatch):
    monkeypatch.setattr(entity_store, "active_events", _stub([BBALL, CHESS]))
    r = _run(er.resolve(uuid4(), "move chess class to 9pm"))
    assert r.status == "resolved" and r.entity["id"] == "1"


def test_not_found_when_no_events(monkeypatch):
    monkeypatch.setattr(entity_store, "active_events", _stub([]))
    assert _run(er.resolve(uuid4(), "delete chess class")).status == "not_found"


def test_generic_pointer_single_event(monkeypatch):
    monkeypatch.setattr(entity_store, "active_events", _stub([CHESS]))
    r = _run(er.resolve(uuid4(), "delete that event"))
    assert r.status == "resolved" and r.entity["id"] == "1"


def test_ambiguous_fails_closed(monkeypatch):
    a = {"id": "3", "title": "Chess Club", "normalized_title": "chess club", "start": "x"}
    b = {"id": "4", "title": "Chess Practice", "normalized_title": "chess practice", "start": "y"}
    monkeypatch.setattr(entity_store, "active_events", _stub([a, b]))
    r = _run(er.resolve(uuid4(), "delete chess"))          # "chess" overlaps BOTH equally
    assert r.status == "ambiguous" and len(r.candidates) == 2


def test_most_recent_for_correction(monkeypatch):
    monkeypatch.setattr(entity_store, "active_events", _stub([BBALL, CHESS]))
    r = _run(er.resolve_most_recent(uuid4()))
    assert r.status == "resolved" and r.entity["id"] == "2"


def test_bare_pronoun_does_not_select_an_event(monkeypatch):
    # THE critical fix: "it"/"that" alone must NOT resolve the only event
    monkeypatch.setattr(entity_store, "active_events", _stub([CHESS]))
    assert _run(er.resolve(uuid4(), "cancel that plan with mike, i can't make it")).status == "not_found"
    assert _run(er.resolve(uuid4(), "make it an hour")).status == "not_found"


def test_explicit_generic_noun_resolves(monkeypatch):
    monkeypatch.setattr(entity_store, "active_events", _stub([CHESS]))
    assert _run(er.resolve(uuid4(), "delete that event")).status == "resolved"
