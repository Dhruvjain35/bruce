"""Secure one-command device bootstrap — client side (Bite 1.5 A4 gap 1).

install_relay.sh runs this instead of asking the operator to paste a permanent credential. It:

  1. registers the device over TLS using the SHORT-LIVED, SINGLE-USE bootstrap token (in argv/env of THIS
     step only — it is consumed immediately and is NOT the permanent credential);
  2. receives the permanent credential in process MEMORY and writes it straight into the login Keychain
     (Security.framework) — never displayed, copied, pasted, written to disk, placed in argv/env, or logged;
  3. verifies the stored credential authenticates (a heartbeat via the Keychain, not the in-memory value);
  4. if anything after registration fails, self-revokes the just-created credential so no active ORPHAN
     remains.

The core (``bootstrap_device``) is dependency-injected for tests; ``run`` wires the real HTTP + Keychain.
"""

from __future__ import annotations

import sys

SERVICE = "com.bruce.relay.device-secret"


class BootstrapError(Exception):
    pass


def bootstrap_device(*, register, store, verify, revoke) -> str:
    """Run the bootstrap with injected steps. ``register()`` -> (device_id, secret); ``store(secret)``
    puts it in the Keychain; ``verify()`` -> bool (does the STORED credential authenticate?);
    ``revoke(secret)`` self-revokes on failure. The permanent secret is held only in a local variable and
    is NEVER returned or logged — this returns the device_id only."""
    device_id, secret = register()
    try:
        store(secret)
        if not verify():
            raise BootstrapError("stored credential failed to authenticate")
        return device_id
    except Exception:
        try:
            revoke(secret)                 # no active orphan credential is left behind
        except Exception:
            pass
        raise


def run(*, base_url: str, bootstrap_token: str, device_name: str, account: str, timeout: float = 20.0) -> str:
    """Wire the real HTTP + Keychain. Reads nothing secret from argv; the permanent credential lives only
    in memory until SecItemAdd. Returns the device_id."""
    import httpx

    from . import keychain

    base = base_url.rstrip("/")

    def register():
        r = httpx.post(f"{base}/v1/relay/register",
                       headers={"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"},
                       json={"device_name": device_name}, timeout=timeout)
        if r.status_code != 200:
            raise BootstrapError(f"registration refused (HTTP {r.status_code})")   # never log the body
        body = r.json()
        return body["device_id"], body["secret"]

    def store(secret):
        keychain.set_password(account, secret, service=SERVICE)   # secret -> Keychain (in-memory only)

    def _heartbeat_ok(secret) -> bool:
        import datetime
        import uuid as _uuid
        headers = {"Authorization": f"Bearer {secret}",
                   "X-Bruce-Timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "X-Bruce-Nonce": _uuid.uuid4().hex, "X-Bruce-Request-Id": _uuid.uuid4().hex}
        r = httpx.post(f"{base}/v1/relay/heartbeat", headers=headers, json={}, timeout=timeout)
        return r.status_code == 200

    def verify():
        stored = keychain.get_password(account, service=SERVICE)   # read BACK from the Keychain
        return bool(stored) and _heartbeat_ok(stored)

    def revoke(secret):
        import datetime
        import uuid as _uuid
        headers = {"Authorization": f"Bearer {secret}",
                   "X-Bruce-Timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "X-Bruce-Nonce": _uuid.uuid4().hex, "X-Bruce-Request-Id": _uuid.uuid4().hex}
        httpx.post(f"{base}/v1/relay/self-revoke", headers=headers, json={}, timeout=timeout)
        try:
            keychain.delete_password(account, service=SERVICE)     # drop the unusable local item too
        except Exception:
            pass

    return bootstrap_device(register=register, store=store, verify=verify, revoke=revoke)


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Relay device secure bootstrap (invoked by install_relay.sh).")
    p.add_argument("--base-url", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--account", default="default")
    args = p.parse_args(argv)
    # The short-lived, single-use bootstrap token is read ONCE from STDIN — never from argv or an
    # environment variable (which could leak via shell history, `ps`, or a copied command). It is not
    # printed or persisted, and the local reference is cleared right after registration.
    token = sys.stdin.readline().strip()
    if not token:
        print("error: no bootstrap token on stdin (pipe the token minted by scripts.relay_bootstrap)",
              file=sys.stderr)
        return 64
    try:
        device_id = run(base_url=args.base_url, bootstrap_token=token, device_name=args.device,
                        account=args.account)
    except Exception as exc:
        # generic failure — never echo the token or a credential
        print(f"error: bootstrap failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        token = None                                            # drop the local reference asap
    print(f"device registered + credential stored in Keychain (device_id={device_id}); credential never shown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
