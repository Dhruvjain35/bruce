"""Bite 1.5 A4 — Mac installer core (testable logic behind install_relay.sh).

Covers plist rendering (secret-free), durable state-dir layout + permissions (never wiped), pinned-commit
version activation for upgrade AND rollback, the secret-free Keychain/launchctl command generation, and
an end-to-end `prepare` (install -> upgrade -> rollback) that preserves durable state — all without the
Mac-only side effects (Keychain prompt / launchctl), which are asserted to be secret-free and are covered
in the runbook as the on-device approval steps.
"""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from relay import installer


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# --------------------------------------------------------------------------- plist rendering (secret-free)


def test_render_plist_substitutes_and_is_secret_free():
    out = installer.render_plist(python="/opt/py", engine_dir="/app/engine", state_dir="/state",
                                 api_base_url="https://api.example", pinned_commit="abc123sha")
    assert "@PYTHON@" not in out and "@PINNED_COMMIT@" not in out and "@API_BASE_URL@" not in out
    assert "/opt/py" in out and "abc123sha" in out and "https://api.example" in out
    for marker in ("BRUCE_RELAY_SECRET", "Bearer ", "password", "Authorization"):
        assert marker not in out                          # no secret in the plist


def test_render_plist_refuses_a_secret_marker():
    tmpl = installer.load_template().replace("@API_BASE_URL@", "@API_BASE_URL@ BRUCE_RELAY_SECRET")
    with pytest.raises(ValueError):
        installer.render_plist(python="p", engine_dir="e", state_dir="s", api_base_url="u",
                               pinned_commit="c", template=tmpl)


def test_assert_plist_secret_free_rejects_bearer():
    with pytest.raises(ValueError):
        installer.assert_plist_secret_free("<string>Authorization: Bearer sk-xyz</string>")


# --------------------------------------------------------------------------- state dir: 0700, never wiped


def test_ensure_state_dir_permissions_and_no_wipe(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    durable = state / "outbound_sent.json"
    durable.write_text('{"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}}')
    installer.ensure_state_dir(str(state))
    assert _mode(str(state)) == 0o700 and _mode(str(state / "spool")) == 0o700
    assert durable.read_text().startswith('{"version": 2')   # existing durable state untouched


# --------------------------------------------------------------------------- version activate / rollback


def _make_version(install_dir, commit):
    os.makedirs(os.path.join(install_dir, "versions", commit, "engine"), exist_ok=True)


def test_activate_version_upgrade_then_rollback(tmp_path):
    install = str(tmp_path / "app")
    _make_version(install, "shaA"); _make_version(install, "shaB")
    installer.activate_version(install, "shaA")
    assert installer.active_version(install) == "shaA"
    installer.activate_version(install, "shaB")              # upgrade
    assert installer.active_version(install) == "shaB"
    installer.activate_version(install, "shaA")              # rollback
    assert installer.active_version(install) == "shaA"


def test_activate_version_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        installer.activate_version(str(tmp_path / "app"), "nope")


# --------------------------------------------------------------------------- Mac-only commands are secret-free


def test_keychain_add_argv_has_no_secret_and_is_interactive():
    argv = installer.keychain_add_argv("default")
    assert "-w" not in argv                                 # no password on the command line (interactive)
    assert argv[0] == "security" and "com.bruce.relay.device-secret" in argv
    assert not any("secret" in a.lower() and a != "com.bruce.relay.device-secret" for a in argv)


def test_launchctl_commands_reference_the_label(tmp_path):
    dest = installer.launchagent_path(str(tmp_path))
    assert dest.endswith("Library/LaunchAgents/com.bruce.relay.supervisor.plist")
    load = installer.load_argv(dest, uid=501)
    assert ["launchctl", "bootstrap", "gui/501", dest] in load
    assert installer.kickstart_argv(501) == ["launchctl", "kickstart", "-k", "gui/501/com.bruce.relay.supervisor"]


# --------------------------------------------------------------------------- end-to-end prepare (no Mac side effects)


def test_prepare_install_upgrade_rollback_preserves_state(tmp_path, monkeypatch):
    install = str(tmp_path / "app"); state = str(tmp_path / "state"); home = str(tmp_path / "home")
    _make_version(install, "shaA"); _make_version(install, "shaB")
    os.makedirs(state, exist_ok=True)
    durable = os.path.join(state, "outbound_sent.json")
    with open(durable, "w") as f:
        f.write('{"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}}')

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda c, **k: calls.append(c))   # no real launchctl

    def _prepare(commit):
        return installer.main(["prepare", "--install-dir", install, "--state-dir", state,
                               "--commit", commit, "--python", "/usr/bin/python3",
                               "--api-base-url", "https://api.example", "--home", home, "--uid", "501"])

    # install shaA
    assert _prepare("shaA") == 0
    plist_path = installer.launchagent_path(home)
    assert os.path.exists(plist_path) and _mode(plist_path) == 0o644
    body = open(plist_path).read()
    assert "shaA" in body and "https://api.example" in body
    assert "BRUCE_RELAY_SECRET" not in body and "Bearer " not in body      # secret-free
    assert installer.active_version(install) == "shaA"
    assert _mode(state) == 0o700
    assert any("launchctl" in c[0] for c in calls)

    # upgrade to shaB — durable state untouched, current repointed, plist pinned to shaB
    assert _prepare("shaB") == 0
    assert installer.active_version(install) == "shaB"
    assert "shaB" in open(plist_path).read()
    assert open(durable).read().startswith('{"version": 2')

    # rollback to shaA — durable state STILL untouched
    assert _prepare("shaA") == 0
    assert installer.active_version(install) == "shaA"
    assert open(durable).read().startswith('{"version": 2')


def test_prepare_dry_run_writes_nothing(tmp_path):
    install = str(tmp_path / "app"); state = str(tmp_path / "state"); home = str(tmp_path / "home")
    _make_version(install, "shaA")
    rc = installer.main(["prepare", "--install-dir", install, "--state-dir", state, "--commit", "shaA",
                         "--python", "/usr/bin/python3", "--api-base-url", "https://x", "--home", home,
                         "--uid", "501", "--dry-run"])
    assert rc == 0
    assert not os.path.exists(installer.launchagent_path(home))   # dry-run wrote no plist
    assert not os.path.islink(os.path.join(install, "current"))   # and flipped no symlink
