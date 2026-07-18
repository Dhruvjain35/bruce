"""Register a self-hosted iMessage relay device (operator tool — NOT a public endpoint).

Prints the device id and secret ONCE. Put the secret in the Mac relay's Keychain; the server keeps
only its hash. There is deliberately no HTTP registration endpoint — provisioning a relay is an
operator action, not something reachable over the network.

    BRUCE_APP_DATABASE_URL=... python -m scripts.register_relay_device "mac-alpha" --handle "+15550000000"
"""

from __future__ import annotations

import asyncio
import sys

from bruce_engine import relay_auth


async def _main(name: str, handle: str | None) -> None:
    device_id, secret = await relay_auth.register_device(name, bruce_handle=handle)
    print("Relay device registered.")
    print(f"  device_id : {device_id}")
    print(f"  secret    : {secret}")
    print("Store the secret in the Mac Keychain now — it is not recoverable (server keeps only a hash).")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    name = args[0] if args else "mac-alpha"
    handle = None
    if "--handle" in sys.argv:
        handle = sys.argv[sys.argv.index("--handle") + 1]
    asyncio.run(_main(name, handle))
