"""Founder-alpha bootstrap — idempotent, audited, and unable to fabricate a credential.

WHY THIS IS A COMMAND AND NOT A SEQUENCE OF SQL. Every piece of state below has an owner and an audit
trail already: link codes bind a phone number to a user through `messaging_store`, Google connection goes
through the real OAuth exchange in `oauth_google`, capability truth comes from `tool_broker`. Hand-written
INSERTs would produce rows that look identical and are not — an integration row with a hand-pasted
refresh token has no scope list the provider agreed to, no expiry, and no audit of who granted it, and
the first thing that would break is the capability snapshot silently claiming Gmail is live.

WHAT THIS COMMAND CANNOT DO, BY DESIGN. It cannot connect Google. `prepare` prints the consent URL and
stops; a human has to authorise it, and the refresh token arrives through the callback that already
exists. There is no flag here that writes a token. That is not an inconvenience to route around — a
bootstrap that could mint provider credentials would be the single most dangerous script in the repo.

IDEMPOTENT MEANS RE-RUNNABLE, NOT "CHECKS FIRST". Every step resolves-or-creates against a stable key:
the user id is derived from the founder label with uuid5, the messaging identity is keyed by channel plus
handle, timezone is an upsert. Running `prepare` five times produces one user, one identity, one
timezone row and five link codes — codes are deliberately not idempotent, because reissuing one is how a
lost code is recovered and each is single-use and expiring.

IT CREATES NO AUTHORITY. No enrollment, no entitlement, no kill-state change, and it sends nothing. The
founder is not able to use Bruce when this finishes; they are able to be *granted* access in a separate,
audited step. Keeping those apart is what makes "the alpha is closed" a checkable claim rather than a
belief about a script.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# The same namespace `create_link_code` uses, so a founder bootstrapped here and a code minted there
# resolve to ONE user rather than two that differ by which tool ran first.
ALPHA_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

OK, BAD, WARN = "  OK   ", "  FAIL ", "  WARN "


def founder_user_id(label: str) -> uuid.UUID:
    return uuid.uuid5(ALPHA_NS, f"alpha:{label}")


async def _prepare(label: str, handle: str, tz: str, locale: str) -> int:
    from bruce_engine import messaging_store, oauth_google, world_state
    from bruce_engine.messaging import ChannelKind
    from bruce_engine.repositories import PostgresUserRepository

    uid = founder_user_id(label)
    print(f"\nFOUNDER BOOTSTRAP — prepare\n  label   {label}\n  user_id {uid}\n  handle  {handle}\n")

    await PostgresUserRepository().ensure(uid, auth_provider="founder_alpha")
    print(f"{OK} user resolved (idempotent on uuid5 of the label)")

    identities = await messaging_store.list_identities(uid)
    bound = [i for i in identities
             if i.channel == ChannelKind.self_hosted_imessage.value and i.channel_identity == handle]
    if bound:
        print(f"{OK} messaging identity already bound to this user ({bound[0].id})")
    else:
        code, expires_at = await messaging_store.create_link_code(
            uid, ChannelKind.self_hosted_imessage)
        print(f"{OK} link code minted — text it to Bruce from {handle} to bind the handle")
        print(f"       CODE: {code}   (expires {expires_at.isoformat()})")
        print("       (single-use and expiring; re-running mints a fresh one, which is how a lost")
        print("        code is recovered rather than a reason to hand-insert an identity row)")

    await world_state.set_timezone(uid, tz, source="user_stated")
    print(f"{OK} timezone set to {tz}")
    if locale:
        print(f"{WARN} locale is not a stored field yet — recorded here only in this output")

    try:
        url = await oauth_google.start_authorization(uid)
        print(f"\n{OK} Google consent URL (open in a browser, sign in as the founder):\n\n  {url}\n")
    except Exception as exc:
        print(f"{BAD} could not build the consent URL: {type(exc).__name__}: {exc}")
        return 1

    print("  This command does NOT complete OAuth and cannot write a refresh token.")
    print("  After consenting, run:  python scripts/founder_bootstrap.py diagnose --label", label)
    print("\n  No enrollment created. No entitlement created. Nothing sent.\n")
    return 0


async def _diagnose(label: str, handle: str) -> int:
    """Read-only. Opens no OAuth flow, writes nothing, and sends nothing."""
    from sqlalchemy import select

    from bruce_engine import capability_snapshot, context_compiler, messaging_store, oauth_google, schema
    from bruce_engine import world_state
    from bruce_engine.db import user_session
    from bruce_engine.messaging import ChannelKind

    uid = founder_user_id(label)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{OK if ok else BAD}{name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    print(f"\nFOUNDER DIAGNOSTIC — non-mutating\n  user_id {uid}\n")

    async with user_session(uid) as s:
        user = (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none()
    check("founder user exists", user is not None)

    identities = await messaging_store.list_identities(uid)
    mine = [i for i in identities
            if i.channel == ChannelKind.self_hosted_imessage.value and i.channel_identity == handle]
    check("messaging identity resolves to this user", bool(mine),
          f"{len(identities)} identity/identities on this user")
    check("identity is not disconnected", bool(mine) and mine[0].disconnected_at is None)

    integ = await oauth_google.get_integration(uid)
    check("google account connected", integ is not None and integ.status == "connected")
    check("google connection not revoked", integ is not None and integ.revoked_at is None)
    check("refresh token present and encrypted",
          integ is not None and bool(integ.refresh_token_encrypted)
          and "://" not in str(integ.refresh_token_encrypted))
    check("provider account owned by this user",
          integ is not None and integ.user_id == uid,
          str(getattr(integ, "provider_account_id", None)))
    scopes = tuple(getattr(integ, "scopes", ()) or ())
    print(f"       scopes read back from provider state: {list(scopes)}")

    snap = await capability_snapshot.snapshot(uid)
    families = {f.family: f for f in snap.families}
    for family, label_ in (("calendar", "Calendar write"), ("email", "Gmail")):
        state = families.get(family)
        check(f"{label_} family usable", bool(state and state.usable),
              (state.reason if state and not state.usable else ""))

    from bruce_engine import tool_broker
    for cap, name in (("gmail.send_message", "Gmail send"),
                      ("gmail.find_reply", "Gmail read"),
                      ("calendar.create_event", "Calendar create"),
                      ("calendar.update_event", "Calendar update")):
        av = await tool_broker.availability(uid, cap)
        check(f"{name} available", av.ok, "" if av.ok else av.status)

    tz = await world_state.resolve_timezone(uid, default="")
    check("timezone present", bool(tz), tz)

    compiled = await context_compiler.compile(uid, [], capabilities=snap)
    check("capability snapshot reaches ConversationState",
          "calendar" in compiled.text.lower() or "gmail" in compiled.text.lower(),
          f"{compiled.est_tokens} tokens, layers {[b.layer for b in compiled.blocks]}")
    check("clock context reaches ConversationState", any(b.layer == "now" for b in compiled.blocks))

    # The alpha must still be closed after all of this.
    async with user_session(uid) as s:
        live = (await s.execute(select(schema.StagingTestEnrollment).where(
            schema.StagingTestEnrollment.user_id == uid,
            schema.StagingTestEnrollment.revoked_at.is_(None)))).scalars().all()
        ent = (await s.execute(select(schema.ProductionAccountEntitlement).where(
            schema.ProductionAccountEntitlement.user_id == uid))).scalars().all()
    check("no enrollment created by bootstrap", not live, f"{len(live)} live")
    check("no entitlement created by bootstrap", not ent, f"{len(ent)} entitlements")

    print(f"\nVERDICT: {'ALL_PASS' if not failures else 'FAILED: ' + ', '.join(failures)}\n")
    return 0 if not failures else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Founder-alpha bootstrap (idempotent, audited).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("prepare", "resolve user/identity/timezone and print the OAuth URL"),
                           ("diagnose", "non-mutating readiness check")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--label", required=True, help="stable founder label -> reproducible user_id")
        sp.add_argument("--handle", required=True, help="iMessage handle, e.g. +15551234567")
        if name == "prepare":
            sp.add_argument("--timezone", default="America/Chicago")
            sp.add_argument("--locale", default="en-US")
    a = p.parse_args()
    if a.cmd == "prepare":
        return asyncio.run(_prepare(a.label, a.handle, a.timezone, a.locale))
    return asyncio.run(_diagnose(a.label, a.handle))


if __name__ == "__main__":
    raise SystemExit(main())
