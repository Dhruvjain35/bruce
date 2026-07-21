"""Relay entrypoint — `python -m relay`. Wires the real imsg subprocess + HTTP backend and runs.

Transport only. Content-free logging is configured here (message ids + statuses, never text/paths).
Live behaviour is UNVERIFIED until the dedicated-Mac dry-run passes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .backend import HttpBackend
from .checkpoint import FileCheckpoint
from .config import ConfigError, RelayConfig
from .imsg import SubprocessImsg
from .outbound_ledger import OutboundLedger
from .pending import PendingStore
from .relay import MISCONFIGURED_EXIT, Relay


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


async def _run_with_signals(relay: Relay) -> int:
    """Run the relay, translating SIGTERM/SIGINT into a graceful stop (park), and returning the process
    exit code the SUPERVISOR reads: 0 = clean park (stop directive / signal), 78 = revoked credential."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, relay.stop)      # graceful: set the stop event, let run() unwind
        except (NotImplementedError, ValueError):
            pass
    await relay.run()
    return relay.exit_code


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",  # ids + statuses only
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log request URLs (keep logs focused)
    try:
        cfg = RelayConfig.from_env()
    except ConfigError as exc:
        # Fatal, non-transient misconfig (e.g. the pinned imsg binary is gone). Exit with the PARK code
        # so the supervisor stays ALIVE and parks (no crash-loop), resuming on a kickstart once fixed.
        # The message is path-free — no filesystem detail reaches remote telemetry.
        logging.getLogger("bruce.relay").error("relay_misconfigured: %s", exc)
        sys.exit(MISCONFIGURED_EXIT)
    relay = build_relay(cfg)
    logging.getLogger("bruce.relay").info("relay_start base=%s", cfg.base_url)
    try:
        code = asyncio.run(_run_with_signals(relay))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
