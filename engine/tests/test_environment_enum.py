"""BRUCE_ENV strict-enum resolution (Bite 1.5 A1 hardening, gate 4).

current_environment() is the SINGLE source the kill switch, the runtime access gate, and the migration
seed all resolve. A value outside the strict enum must FAIL CLOSED (raise), never silently fall back to
'local' — a typo'd env would otherwise resolve a different (capability_global_state / relay_control)
singleton than the one the operator flipped, silently defeating a kill.
"""

from __future__ import annotations

import pytest

from bruce_engine import access_control
from bruce_engine.access_control import InvalidEnvironment, current_environment


def test_unset_defaults_to_local(monkeypatch):
    monkeypatch.delenv("BRUCE_ENV", raising=False)
    assert current_environment() == "local"


def test_blank_defaults_to_local(monkeypatch):
    monkeypatch.setenv("BRUCE_ENV", "   ")
    assert current_environment() == "local"


@pytest.mark.parametrize("env", access_control.ENVIRONMENTS)
def test_each_valid_env_resolves_to_itself(monkeypatch, env):
    monkeypatch.setenv("BRUCE_ENV", env)
    assert current_environment() == env


@pytest.mark.parametrize("bad", ["prod", "stage", "staging1", "PRODUCTION", "test", "bogus"])
def test_invalid_env_raises_and_never_falls_back(monkeypatch, bad):
    monkeypatch.setenv("BRUCE_ENV", bad)
    with pytest.raises(InvalidEnvironment):
        current_environment()
