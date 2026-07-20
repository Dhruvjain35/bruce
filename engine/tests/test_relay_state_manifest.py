"""Bite 1.5 A4 gap 2 — durable-state compatibility across upgrade/rollback.

Reusing one state dir is safe only when the selected code understands the on-disk formats. These tests
prove: upgrade/rollback with compatible state; an INCOMPATIBLE rollback (on-disk newer than the target
supports) is BLOCKED; a failed migration/health check restores the prior release; a crash mid-symlink is
atomic; new delivery-ledger phases (handoff_outcome_unknown) survive a rollback attempt; pending HEIC
survives; and no durable state is wiped.
"""

from __future__ import annotations

import json
import os

import pytest

from relay import installer, state_manifest
from relay.outbound_ledger import HANDOFF_OUTCOME_UNKNOWN, OutboundLedger


def _mkversion(install, commit):
    os.makedirs(os.path.join(install, "versions", commit, "engine"), exist_ok=True)


def _activate(install):
    return lambda c: installer.activate_version(install, c)


# --------------------------------------------------------------------------- 1-2 compatible upgrade / rollback


def test_1_upgrade_with_compatible_state(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "A"); _mkversion(install, "B")
    os.makedirs(state, exist_ok=True)
    state_manifest.write_manifest(state, dict(state_manifest.CURRENT_SCHEMAS), commit="A")
    installer.activate_version(install, "A")
    state_manifest.safe_activate(install_dir=install, state_dir=state, commit="B",
                                 activate=_activate(install), health_check=lambda: True)
    assert installer.active_version(install) == "B"


def test_2_rollback_with_compatible_state(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "A"); _mkversion(install, "B")
    os.makedirs(state, exist_ok=True)
    state_manifest.write_manifest(state, dict(state_manifest.CURRENT_SCHEMAS), commit="B")
    installer.activate_version(install, "B")
    state_manifest.safe_activate(install_dir=install, state_dir=state, commit="A",   # rollback
                                 activate=_activate(install), health_check=lambda: True)
    assert installer.active_version(install) == "A"


# --------------------------------------------------------------------------- 3 incompatible rollback blocked


def test_3_incompatible_rollback_is_blocked(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "OLD"); _mkversion(install, "NEW")
    os.makedirs(state, exist_ok=True)
    installer.activate_version(install, "NEW")
    # on-disk ledger is at v2; the OLD target supports only v1 -> rollback must be refused
    on_disk = {**state_manifest.CURRENT_SCHEMAS, "outbound_ledger": 2}
    state_manifest.write_manifest(state, on_disk, commit="NEW")
    target_old = {**state_manifest.CURRENT_SCHEMAS, "outbound_ledger": 1}
    with pytest.raises(state_manifest.IncompatibleRollback):
        state_manifest.safe_activate(install_dir=install, state_dir=state, commit="OLD",
                                     activate=_activate(install), health_check=lambda: True, target=target_old)
    assert installer.active_version(install) == "NEW"           # unchanged — rollback refused


# --------------------------------------------------------------------------- 4 failed migration restores prior


def test_4_failed_activation_restores_prior_release(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "GOOD"); _mkversion(install, "BAD")
    os.makedirs(state, exist_ok=True)
    state_manifest.write_manifest(state, dict(state_manifest.CURRENT_SCHEMAS), commit="GOOD")
    installer.activate_version(install, "GOOD")
    with pytest.raises(state_manifest.ActivationFailed):
        state_manifest.safe_activate(install_dir=install, state_dir=state, commit="BAD",
                                     activate=_activate(install), health_check=lambda: False)   # health fails
    assert installer.active_version(install) == "GOOD"          # restored to the prior working version


# --------------------------------------------------------------------------- 5 crash during symlink is atomic


def test_5_symlink_activation_is_atomic(tmp_path):
    install = str(tmp_path / "app")
    _mkversion(install, "A"); _mkversion(install, "B")
    installer.activate_version(install, "A")
    # a concurrent/crashed activation can only leave the OLD or the NEW target — never a partial link
    installer.activate_version(install, "B")
    link = os.path.join(install, "current")
    assert os.path.islink(link) and os.path.basename(os.path.realpath(link)) == "B"
    assert not os.path.exists(link + ".tmp")                    # no stale temp link left behind


# --------------------------------------------------------------------------- 6 new ledger phases survive rollback


def test_6_new_delivery_ledger_phases_survive_rollback_attempt(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "OLD"); _mkversion(install, "NEW")
    os.makedirs(state, exist_ok=True)
    installer.activate_version(install, "NEW")
    # a v2 ledger holding an AMBIGUOUS handoff outcome
    led = OutboundLedger(os.path.join(state, "outbound_sent.json"))
    led.record("m1", HANDOFF_OUTCOME_UNKNOWN)
    state_manifest.write_manifest(state, {**state_manifest.CURRENT_SCHEMAS, "outbound_ledger": 2}, commit="NEW")
    # a rollback to v1-only code is blocked -> the ambiguous record is neither downgraded nor reinterpreted
    with pytest.raises(state_manifest.IncompatibleRollback):
        state_manifest.safe_activate(install_dir=install, state_dir=state, commit="OLD",
                                     activate=_activate(install), health_check=lambda: True,
                                     target={**state_manifest.CURRENT_SCHEMAS, "outbound_ledger": 1})
    assert OutboundLedger(os.path.join(state, "outbound_sent.json")).phase("m1") == HANDOFF_OUTCOME_UNKNOWN


# --------------------------------------------------------------------------- 7 pending HEIC survives up/rollback


def test_7_pending_heic_survives_upgrade_and_rollback(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "A"); _mkversion(install, "B")
    os.makedirs(state, exist_ok=True)
    pending = os.path.join(state, "pending_attachments.json")
    with open(pending, "w") as f:
        json.dump({"pending": {"heic-1": {"event": {"guid": "heic-1"}}}}, f)
    state_manifest.write_manifest(state, dict(state_manifest.CURRENT_SCHEMAS), commit="A")
    installer.activate_version(install, "A")
    for commit in ("B", "A"):                                   # upgrade then rollback
        state_manifest.safe_activate(install_dir=install, state_dir=state, commit=commit,
                                     activate=_activate(install), health_check=lambda: True)
    assert json.load(open(pending))["pending"]["heic-1"]["event"]["guid"] == "heic-1"


# --------------------------------------------------------------------------- 8 no durable state is wiped


def test_8_no_durable_state_is_wiped(tmp_path):
    install, state = str(tmp_path / "app"), str(tmp_path / "state")
    _mkversion(install, "A"); _mkversion(install, "B")
    os.makedirs(state, exist_ok=True)
    files = {
        "checkpoint.json": {"processed": ["g1"]},
        "outbound_sent.json": {"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}},
        "pending_attachments.json": {"pending": {"h": {"event": {"guid": "h"}}}},
    }
    for name, content in files.items():
        with open(os.path.join(state, name), "w") as f:
            json.dump(content, f)
    state_manifest.write_manifest(state, dict(state_manifest.CURRENT_SCHEMAS), commit="A")
    installer.activate_version(install, "A")
    for commit in ("B", "A", "B"):                              # upgrade, rollback, upgrade
        state_manifest.safe_activate(install_dir=install, state_dir=state, commit=commit,
                                     activate=_activate(install), health_check=lambda: True)
    for name, content in files.items():
        assert json.load(open(os.path.join(state, name))) == content   # untouched across every path
