"""Durable relay-state COMPATIBILITY manifest (Bite 1.5 A4 gap 2).

Reusing one state dir across upgrades AND rollbacks is safe only when the selected code understands the
current on-disk state formats. This tracks the schema version of each durable state kind and gates
activation:

  * checkpoint           (inbound dedup)          — checkpoint.json
  * outbound_ledger      (delivery-phase ledger)  — outbound_sent.json
  * pending_attachments  (delayed HEIC/attachments)— pending_attachments.json
  * supervisor_state     (supervisor status)      — supervisor-status.json
  * installer_metadata   (this manifest)          — state-manifest.json

Before activating a target commit:
  - read the on-disk state schema versions;
  - if any is NEWER than the target code supports -> BLOCK (never silently downgrade / reinterpret newer
    records, e.g. a delivery-ledger phase the older code doesn't know — this preserves ambiguous
    handoff_outcome_unknown records across a rollback attempt);
  - if any is OLDER -> run the required FORWARD migration atomically, after a privacy-safe backup;
  - if activation or the post-activation health check fails -> restore the prior version + state.

The backup is content-free (durable files hold only opaque ids / phases / counts — never message content).
"""

from __future__ import annotations

import json
import os
import shutil
import time

# The schema versions THIS code understands (its max-supported version per kind).
CURRENT_SCHEMAS = {
    "checkpoint": 1,
    "outbound_ledger": 2,          # matches relay.outbound_ledger's {"version": 2} format
    "pending_attachments": 1,
    "supervisor_state": 1,
    "installer_metadata": 1,
}

MANIFEST_FILE = "state-manifest.json"
# durable state files copied into the privacy-safe backup before a migration.
_DURABLE_FILES = ("checkpoint.json", "outbound_sent.json", "pending_attachments.json", MANIFEST_FILE)


class IncompatibleRollback(Exception):
    """The on-disk state is NEWER than the target commit supports — the rollback is refused."""


class ActivationFailed(Exception):
    """Activation or the post-activation health check failed; the prior version+state were restored."""


def read_manifest(state_dir: str) -> dict:
    """Return the recorded schema versions (empty when there is no manifest yet — a fresh install)."""
    try:
        with open(os.path.join(state_dir, MANIFEST_FILE)) as f:
            data = json.load(f)
        return dict(data.get("schemas") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(state_dir: str, schemas: dict, *, commit: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = os.path.join(state_dir, MANIFEST_FILE + ".tmp")
    with open(tmp, "w") as f:
        json.dump({"schemas": schemas, "commit": commit, "updated_at": time.time()}, f)
    os.replace(tmp, os.path.join(state_dir, MANIFEST_FILE))


def plan_activation(existing: dict, target: dict = CURRENT_SCHEMAS) -> dict:
    """Compare on-disk versions to what the target supports. Returns {blocked:[...], migrate:[...]}.
    blocked = kinds whose on-disk version EXCEEDS the target (incompatible rollback); migrate = kinds
    whose on-disk version is BEHIND the target (forward migration needed)."""
    blocked, migrate = [], []
    for kind, tv in target.items():
        ev = existing.get(kind)
        if ev is None:
            continue                                   # not yet present -> nothing to reconcile
        if ev > tv:
            blocked.append(kind)
        elif ev < tv:
            migrate.append(kind)
    return {"blocked": blocked, "migrate": migrate}


def backup_state(state_dir: str, backup_dir: str) -> None:
    """Copy the durable state files into a privacy-safe backup (content-free ids/phases/counts only)."""
    os.makedirs(backup_dir, exist_ok=True)
    for name in _DURABLE_FILES:
        src = os.path.join(state_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(backup_dir, name))


def restore_state(state_dir: str, backup_dir: str) -> None:
    for name in _DURABLE_FILES:
        src = os.path.join(backup_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(state_dir, name))


def _migrate_outbound_ledger(state_dir: str) -> None:
    """Forward-migrate the outbound ledger (legacy {"processed":[...]} -> phase format). OutboundLedger's
    loader migrates + rewrites idempotently; ambiguous handoff_outcome_unknown records are preserved."""
    from .outbound_ledger import OutboundLedger
    OutboundLedger(os.path.join(state_dir, "outbound_sent.json"))   # load = migrate + rewrite


_MIGRATIONS = {"outbound_ledger": _migrate_outbound_ledger}


def run_forward_migrations(state_dir: str, kinds: list[str]) -> None:
    for kind in kinds:
        fn = _MIGRATIONS.get(kind)
        if fn is not None:
            fn(state_dir)


def safe_activate(*, install_dir: str, state_dir: str, commit: str, activate, health_check,
                  target: dict = CURRENT_SCHEMAS) -> None:
    """Compatibility-checked, atomic activation with auto-restore:

      1. BLOCK an incompatible rollback (on-disk state newer than the target supports).
      2. Back up durable state (privacy-safe), then run required forward migrations.
      3. Record the prior active version, call ``activate(commit)`` (an ATOMIC symlink swap).
      4. Verify health; if activation or health fails, RESTORE the prior version + state and raise.

    ``activate(commit)`` performs the atomic symlink swap; ``health_check()`` returns True when the
    (re)started relay is healthy. Both are injected so this is deterministically testable."""
    existing = read_manifest(state_dir)
    plan = plan_activation(existing, target)
    if plan["blocked"]:
        raise IncompatibleRollback(
            f"on-disk state newer than commit supports: {sorted(plan['blocked'])} — rollback refused")

    from .installer import active_version   # prior version to restore to on failure
    prior = active_version(install_dir)

    backup_dir = os.path.join(state_dir, ".state-backup")
    backup_state(state_dir, backup_dir)
    try:
        run_forward_migrations(state_dir, plan["migrate"])
        activate(commit)                                  # atomic symlink swap to the target
        write_manifest(state_dir, {**existing, **target}, commit=commit)
        if not health_check():
            raise ActivationFailed("post-activation health check failed")
    except Exception as exc:
        restore_state(state_dir, backup_dir)              # roll durable state back
        if prior is not None:
            try:
                activate(prior)                            # roll the version symlink back to the last good one
            except Exception:
                pass
        if isinstance(exc, ActivationFailed):
            raise
        raise ActivationFailed(f"activation failed and prior version restored: {exc}") from exc
