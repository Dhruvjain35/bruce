"""Relay entrypoint — `python -m relay`. Wires the real imsg subprocess + HTTP backend and runs.

Transport only. Content-free logging is configured here (message ids + statuses, never text/paths).
Live behaviour is UNVERIFIED until the dedicated-Mac test passes.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .backend import HttpBackend
from .checkpoint import FileCheckpoint
from .config import RelayConfig
from .imsg import SubprocessImsg
from .relay import Relay


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",  # ids + statuses only
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log request URLs (keep logs focused)
    cfg = RelayConfig.from_env()
    os.makedirs(os.path.dirname(cfg.checkpoint_path), exist_ok=True)
    relay = Relay(
        imsg=SubprocessImsg(cfg.imsg_bin),
        backend=HttpBackend(cfg.base_url, cfg.secret),
        checkpoint=FileCheckpoint(cfg.checkpoint_path),
        spool_dir=cfg.spool_dir,
        poll_interval=cfg.poll_interval,
        reconnect_delay=cfg.reconnect_delay,
    )
    logging.getLogger("bruce.relay").info("relay_start base=%s", cfg.base_url)
    try:
        asyncio.run(relay.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
