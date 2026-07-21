"""imsg binary binding — RelayConfig validation + the misconfigured -> park exit wiring.

The installer pins an ABSOLUTE imsg path into the LaunchAgent env (launchd's PATH excludes Homebrew). At
runtime RelayConfig validates it and fails CLEARLY — path-free, so no filesystem detail reaches remote
telemetry — and the entrypoint exits with MISCONFIGURED_EXIT so the supervisor PARKS instead of
crash-looping when the binary is missing.
"""

from __future__ import annotations

import os

import pytest

from relay import __main__ as relay_main
from relay.config import ConfigError, RelayConfig
from relay.imsg import SubprocessImsg
from relay.relay import MISCONFIGURED_EXIT


def _base_env(monkeypatch, imsg=None):
    monkeypatch.setenv("BRUCE_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("BRUCE_RELAY_SECRET", "dev-secret-not-real")   # dev fallback (no Keychain in CI)
    monkeypatch.delenv("BRUCE_IMSG_BIN", raising=False)
    if imsg is not None:
        monkeypatch.setenv("BRUCE_IMSG_BIN", imsg)


def _exe(tmp_path, name="imsg") -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(p), 0o755)
    return str(p)


def test_from_env_reads_absolute_imsg_and_relay_drives_it(tmp_path, monkeypatch):
    imsg = _exe(tmp_path)
    _base_env(monkeypatch, imsg=imsg)
    cfg = RelayConfig.from_env()
    assert cfg.imsg_bin == imsg
    assert SubprocessImsg(cfg.imsg_bin).binary == imsg    # the relay drives exactly the pinned absolute path


def test_from_env_allows_bare_imsg_default(monkeypatch):
    _base_env(monkeypatch)                                 # unset -> bare "imsg" (dev/test PATH lookup / fake)
    assert RelayConfig.from_env().imsg_bin == "imsg"


def test_from_env_rejects_missing_absolute_imsg_path_free(tmp_path, monkeypatch):
    missing = str(tmp_path / "gone" / "imsg")
    _base_env(monkeypatch, imsg=missing)
    with pytest.raises(ConfigError) as e:
        RelayConfig.from_env()
    assert missing not in str(e.value)                     # filesystem path redacted (remote-telemetry safe)


def test_from_env_rejects_non_executable_absolute_imsg(tmp_path, monkeypatch):
    p = tmp_path / "imsg"; p.write_text("x"); os.chmod(str(p), 0o644)
    _base_env(monkeypatch, imsg=str(p))
    with pytest.raises(ConfigError):
        RelayConfig.from_env()


def test_main_exits_misconfigured_on_bad_imsg_so_supervisor_parks(tmp_path, monkeypatch):
    _base_env(monkeypatch, imsg=str(tmp_path / "gone" / "imsg"))
    with pytest.raises(SystemExit) as e:
        relay_main.main()
    assert e.value.code == MISCONFIGURED_EXIT              # supervisor treats this as PARK, not crash-loop
