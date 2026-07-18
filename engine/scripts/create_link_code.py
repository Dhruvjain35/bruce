"""Mint a one-time iMessage invite code for a Bruce user (operator tool — NOT a public endpoint).

PRIVATE-ALPHA bridge: until the native app + Sign in with Apple ships, there is no in-app way to get
a link code. The operator runs this against the staging DB (via the Cloud SQL Auth Proxy) to mint a
short-lived, single-use code for a specific user, then texts that code to Bruce from the target
number to bind the handle. The code is stored hashed; the plaintext is shown here ONCE.

    # existing user by id:
    BRUCE_APP_DATABASE_URL=... python -m scripts.create_link_code --user <uuid>
    # stable alpha user derived from a label (reproducible id; creates the user if needed):
    BRUCE_APP_DATABASE_URL=... python -m scripts.create_link_code --label dhruv-alpha

This tool does NOT expose whether an account already existed — it ensures the user row and mints a
code either way. Guard access to it like any admin credential.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from bruce_engine import messaging_store, schema
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind

# Fixed namespace so `--label X` always maps to the SAME user_id (reproducible for the operator).
ALPHA_NS = uuid.UUID("b0b0a1fa-0000-4000-8000-000000000001")


async def _ensure_user(user_id: uuid.UUID) -> None:
    async with user_session(user_id) as s:
        exists = (await s.execute(select(schema.User).where(schema.User.id == user_id))).scalar_one_or_none()
        if exists is None:
            s.add(schema.User(id=user_id, auth_provider="alpha_bridge"))


async def _main(user_id: uuid.UUID) -> None:
    await _ensure_user(user_id)
    code, expires_at = await messaging_store.create_link_code(
        user_id, channel=ChannelKind.self_hosted_imessage)
    print("Invite code minted (single-use, expires ~10 min). Text it to Bruce from the target number:")
    print(f"  code    : {code}")
    print(f"  expires : {expires_at.isoformat()}")
    print(f"  user_id : {user_id}")
    print(f"  channel : {ChannelKind.self_hosted_imessage.value}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Mint a private-alpha iMessage invite code.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="existing Bruce user_id (UUID)")
    g.add_argument("--label", help="stable label -> reproducible user_id (creates the user if needed)")
    a = p.parse_args()
    uid = uuid.UUID(a.user) if a.user else uuid.uuid5(ALPHA_NS, f"alpha:{a.label}")
    asyncio.run(_main(uid))
