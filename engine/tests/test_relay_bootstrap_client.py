"""Bite 1.5 A4 gap 1 — secure device bootstrap (client side), offline injected fakes.

Proves the client flow: register -> store the credential in the Keychain -> verify it authenticates ->
return the device_id only (never the secret); and that ANY failure after registration SELF-REVOKES the
just-created credential so no active orphan is left. Plus Keychain-helper input validation.
"""

from __future__ import annotations

import io
import sys

import pytest

from relay import bootstrap, keychain


def _reg(secret="perm-secret-xyz", device_id="dev-1"):
    def register():
        return device_id, secret
    return register


def test_happy_path_stores_verifies_and_returns_device_id_not_secret():
    stored = {}
    calls = {"revoke": 0}
    out = bootstrap.bootstrap_device(
        register=_reg(),
        store=lambda s: stored.__setitem__("secret", s),
        verify=lambda: stored.get("secret") == "perm-secret-xyz",   # the STORED credential authenticates
        revoke=lambda s: calls.__setitem__("revoke", calls["revoke"] + 1))
    assert out == "dev-1"                       # returns the device_id, not the secret
    assert stored["secret"] == "perm-secret-xyz" and calls["revoke"] == 0


def test_verify_failure_self_revokes_no_orphan():
    revoked = []
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.bootstrap_device(
            register=_reg(),
            store=lambda s: None,
            verify=lambda: False,               # stored credential does NOT authenticate
            revoke=lambda s: revoked.append(s))
    assert revoked == ["perm-secret-xyz"]       # the just-created credential was revoked (no orphan)


def test_store_failure_self_revokes_no_orphan():
    revoked = []

    def _boom(_):
        raise RuntimeError("keychain write failed")
    with pytest.raises(RuntimeError):
        bootstrap.bootstrap_device(
            register=_reg(), store=_boom, verify=lambda: True,
            revoke=lambda s: revoked.append(s))
    assert revoked == ["perm-secret-xyz"]       # registration succeeded but storing failed -> revoke


# --------------------------------------------------------------------------- Keychain helper validation


def test_keychain_rejects_empty_or_null_inputs():
    with pytest.raises(keychain.KeychainError):
        keychain._validate("", "x")
    with pytest.raises(keychain.KeychainError):
        keychain._validate("acct", "sec\x00ret")
    keychain._validate("acct", "fine")          # ok


@pytest.mark.skipif(sys.platform == "darwin", reason="on macOS the frameworks load")
def test_keychain_unavailable_off_macos():
    with pytest.raises(keychain.KeychainUnavailable):
        keychain.set_password("acct", "secret")


# --------------------------------------------------------------------------- bootstrap token via STDIN (A4 patch)


def test_main_reads_token_from_stdin_not_env(monkeypatch, capsys):
    seen = {}

    def _fake_run(*, base_url, bootstrap_token, device_name, account, **kw):
        seen.update(token=bootstrap_token, device=device_name)
        return "dev-9"
    monkeypatch.setattr(bootstrap, "run", _fake_run)
    monkeypatch.delenv("BRUCE_RELAY_BOOTSTRAP_TOKEN", raising=False)   # never from the environment
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok-abc-123\n"))   # token arrives on stdin
    rc = bootstrap.main(["--base-url", "https://x", "--device", "mac-alpha"])
    assert rc == 0 and seen["token"] == "tok-abc-123" and seen["device"] == "mac-alpha"
    out = capsys.readouterr().out
    assert "tok-abc-123" not in out                                    # the token is never printed


def test_main_empty_stdin_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))                 # no token piped
    assert bootstrap.main(["--base-url", "https://x", "--device", "mac-alpha"]) == 64


def test_main_failure_is_generic_no_token_echo(monkeypatch, capsys):
    def _boom(**kw):
        raise RuntimeError("bootstrap denied: tok-secret-should-not-appear")
    monkeypatch.setattr(bootstrap, "run", _boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok-secret-should-not-appear\n"))
    rc = bootstrap.main(["--base-url", "https://x", "--device", "mac-alpha"])
    err = capsys.readouterr().err
    assert rc == 1 and "tok-secret-should-not-appear" not in err        # generic error, no token/secret echo
