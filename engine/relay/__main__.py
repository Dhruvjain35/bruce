"""Relay entrypoint — `python -m relay`. Wires the real imsg subprocess + HTTP backend and runs.

Transport only. Content-free logging is configured here (message ids + statuses, never text/paths).
Live behaviour is UNVERIFIED until the dedicated-Mac dry-run passes.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .backend import HttpBackend
from .checkpoint import FileCheckpoint
from .config import RelayConfig
from .imsg import SubprocessImsg
from .outbound_ledger import OutboundLedger
from .pending import PendingStore
from .relay import Relay


def build_relay(cfg: RelayConfig) -> Relay:
    """Construct the Relay from config (separated from main() so the wiring is unit-testable)."""
    state = os.path.dirname(cfg.checkpoint_path)
    os.makedirs(state, exist_ok=True)
    return Relay(
        imsg=SubprocessImsg(cfg.imsg_bin),
        backend=HttpBackend(cfg.base_url, cfg.secret),
        checkpoint=FileCheckpoint(cfg.checkpoint_path),
        spool_dir=cfg.spool_dir,
        poll_interval=cfg.poll_interval,
        reconnect_delay=cfg.reconnect_delay,
        sent_ledger=OutboundLedger(os.path.join(state, "outbound_sent.json")),   # phase-tracked outbound (migrates the legacy format)
        pending=PendingStore(os.path.join(state, "pending_attachments.json")),   # restart-safe delayed attachments
        attachment_max_wait_s=cfg.attachment_max_wait_s,
        attachment_sweep_interval_s=cfg.attachment_sweep_interval_s,
        attachment_max_events=cfg.attachment_max_events,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",  # ids + statuses only
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log request URLs (keep logs focused)
    cfg = RelayConfig.from_env()
    relay = build_relay(cfg)
    logging.getLogger("bruce.relay").info("relay_start base=%s", cfg.base_url)
    try:
        asyncio.run(relay.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
