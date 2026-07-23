"""UserWorldState (R3) — timezone resolution + persistence. The "i'm in cst" -> America/Chicago fix.
The DB persistence path is exercised in the PG suite; this covers the pure resolvers."""

from __future__ import annotations

from bruce_engine import world_state as ws


def test_abbreviations_resolve_to_iana():
    assert ws.canonical_timezone("cst") == "America/Chicago"
    assert ws.canonical_timezone("central time") == "America/Chicago"
    assert ws.canonical_timezone("i'm on pacific") == "America/Los_Angeles"
    assert ws.canonical_timezone("est") == "America/New_York"
    assert ws.canonical_timezone("America/Chicago") == "America/Chicago"
    assert ws.canonical_timezone("no timezone here") is None


def test_first_person_statement_only():
    assert ws.detect_user_timezone_statement("yo i'm in cst time zone gng") == "America/Chicago"
    assert ws.detect_user_timezone_statement("my timezone is eastern") == "America/New_York"
    assert ws.detect_user_timezone_statement("set my tz to pacific") == "America/Los_Angeles"
    # NOT the user's own tz -> don't capture
    assert ws.detect_user_timezone_statement("the game is in central time") is None
    assert ws.detect_user_timezone_statement("cool") is None
