"""I0.1 — product/integrations.yaml is well-formed and HONEST (per-action status, no fake 'implemented')."""

from __future__ import annotations

import pathlib

import yaml

_REG = pathlib.Path(__file__).resolve().parents[2] / "product" / "integrations.yaml"


def _load():
    return yaml.safe_load(_REG.read_text())


def test_registry_parses_and_has_vocab():
    d = _load()
    assert d["meta"]["connection_status_vocab"] and d["meta"]["action_status_vocab"]
    assert isinstance(d["integrations"], list) and d["integrations"]


def test_integration_ids_unique_and_shaped():
    d = _load()
    ids = [i["integration_id"] for i in d["integrations"]]
    assert len(ids) == len(set(ids)), f"duplicate integration_id: {[x for x in ids if ids.count(x) > 1]}"
    conn_vocab = set(d["meta"]["connection_status_vocab"])
    for i in d["integrations"]:
        for k in ("display_name", "category", "provider", "connection_status", "actions"):
            assert k in i, f"{i['integration_id']} missing {k}"
        assert i["connection_status"] in conn_vocab, f"{i['integration_id']} bad connection_status"
        assert isinstance(i["actions"], list) and i["actions"]


def test_every_action_status_is_in_vocab_and_recorded_independently():
    d = _load()
    act_vocab = set(d["meta"]["action_status_vocab"])
    op_classes = set(d["meta"]["operation_classes"])
    for i in d["integrations"]:
        for a in i["actions"]:
            assert "id" in a and "status" in a, f"{i['integration_id']} action missing id/status"
            assert a["status"] in act_vocab, f"{i['integration_id']}.{a['id']} bad status {a['status']}"
            for c in a.get("classes", []):
                assert c in op_classes, f"{i['integration_id']}.{a['id']} bad class {c}"


_LIVE_STATUSES = {"real_connected", "live_read_verified", "live_write_verified", "live_execution_verified"}


def test_honesty_no_live_status_unless_integration_live_verified():
    # A real-connection status may NEVER be claimed unless the integration is live_verification: true.
    # A fake test is NOT a real integration, so 'fake_tested' can never stand in for a live_* status.
    # This holds now (nothing is live) AND guards every future edit — a false live claim fails CI.
    d = _load()
    offenders = []
    for i in d["integrations"]:
        if i.get("live_verification"):
            continue
        for a in i["actions"]:
            if a["status"] in _LIVE_STATUSES:
                offenders.append(f"{i['integration_id']}.{a['id']}={a['status']}")
    assert not offenders, f"live status claimed on a not-live-verified integration: {offenders}"


def test_credential_blocked_integrations_declare_their_blockers():
    d = _load()
    for i in d["integrations"]:
        blocked = i["connection_status"] == "credential_blocked" or any(
            a["status"] == "code_exists_credential_blocked" for a in i["actions"])
        if blocked:
            assert i.get("credential_requirements"), f"{i['integration_id']} blocked but no credential_requirements"
