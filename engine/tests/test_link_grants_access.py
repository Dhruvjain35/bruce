"""DEFECT-3 — redeeming a link code binds the phone but grants nothing, so the very next message is
refused by Bruce's own access gate.

THE SHAPE OF THE BUG. `access_control.activate_production_entitlement` exists, is idempotent, writes its
own `CapabilityAudit` row, and its docstring says:

    "This is the AUTOMATIC path D1 calls on verified signup — never an operator action
     ('every user needs a grant' must never mean 'an operator grants every user')."

A repo-wide grep for its callers finds `scripts/capability_admin.py` and tests. Nothing else. D1 does not
exist, `POST /v1/auth/apple` does not call it, and no migration seeds a row. So
`conversation_access` reaches its last line and returns `no_grant` for every human being who is not
already holding a hand-made entitlement.

What that costs, concretely: a person texts their code, Bruce says "you're in", and their next message
falls past `conversation_runtime.handle` into the legacy intake path — a canned acknowledgement from a
system that cannot send mail or write a calendar. The link succeeded and the product did not.

WHY THE LINK REDEMPTION IS THE RIGHT SEAM, and why this does not open the door. A link code is minted by
an operator (`scripts/create_link_code.py`) and is single-use and expiring. Holding one IS the invitation.
Granting on redemption therefore automates the grant WITHOUT widening who can get in — a stranger with no
code still gets `LINK_PROMPT` and nothing else, which these tests assert directly.

THE SESSION WARNING IN THE HANDOFF IS WRONG IN MECHANISM, and the correct shape is simpler than it says.
`admin_session()` refuses to open when a tenant `app.user_id` is already set, but `redeem_link_code` runs
in a `worker_session` (messaging_store.py:110) and never sets one, and it has already committed and
exited by the time the "linked" branch runs. So the grant is simply called after redemption returns; no
restructuring, and no RuntimeError to design around.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import access_control, messaging_store, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.messaging_inbound import LINK_PROMPT, handle_inbound

PHONE = "+15550101010"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _msg(*, mid, text, frm=PHONE):
    return InboundMessage(provider_message_id=mid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=frm, text=text, attachments=[], timestamp=_now())


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


def _new_user_with_code():
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    return uid, code


def _access(uid):
    return asyncio.run(access_control.conversation_access(uid, "conversation"))


# --- the defect ---------------------------------------------------------------------------------------

def test_a_freshly_linked_user_can_actually_reach_the_agent(clean_db):
    """THE WHOLE POINT. Link, then be allowed through Bruce's own gate — not a proxy for it."""
    uid, code = _new_user_with_code()

    before = _access(uid)
    assert not before.allow and before.source == "no_grant", (
        f"the fixture is not reproducing the defect: a brand-new user already has access ({before})")

    out = asyncio.run(handle_inbound(FakeChannel(), _msg(mid="link-1", text=code)))
    assert out.status == "linked" and out.user_id == uid

    after = _access(uid)
    assert after.allow, (
        f"the phone is linked and Bruce still refuses this user: {after}. Their next message falls to "
        f"the legacy intake path and comes back as a canned ack from a system that cannot act.")


def test_the_grant_is_audited_like_every_other_grant(clean_db):
    """An automatic grant is still a grant. It must leave the same audit trail the operator CLI does, or
    the audit log stops being the answer to 'how did this account get access'."""
    uid, code = _new_user_with_code()
    asyncio.run(handle_inbound(FakeChannel(), _msg(mid="link-2", text=code)))

    async def _audits():
        # ADMIN session, not worker. `capability_audit` is admin-read (app_is_admin) and FORCE RLS, so a
        # worker session gets 0 rows WITHOUT erroring — this assertion passed vacuously against a
        # perfectly good audit row on the first attempt. Any read that proves a count must first prove it
        # can see rows, which is what the total below does.
        from bruce_engine.db import admin_session
        async with admin_session() as s:
            mine = (await s.execute(select(schema.CapabilityAudit).where(
                schema.CapabilityAudit.target_user_id == uid,
                schema.CapabilityAudit.action == "grant_production"))).scalars().all()
            total = len((await s.execute(select(schema.CapabilityAudit))).scalars().all())
        return mine, total

    rows, total = asyncio.run(_audits())
    assert total, "this session cannot see ANY capability_audit row — the read is blind, not the table empty"
    assert len(rows) == 1, f"expected exactly one grant_production audit row, got {len(rows)}"
    assert rows[0].capability == "conversation"
    assert rows[0].actor and rows[0].actor != "system", (
        "the actor must name the automatic path that granted, so an audit reader can tell an automatic "
        f"link-redemption grant from an operator's CLI recovery: got {rows[0].actor!r}")


def test_the_entitlement_is_scoped_to_conversation(clean_db):
    uid, code = _new_user_with_code()
    asyncio.run(handle_inbound(FakeChannel(), _msg(mid="link-3", text=code)))

    async def _ent():
        async with worker_session() as s:
            return (await s.execute(select(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == uid))).scalar_one_or_none()

    ent = asyncio.run(_ent())
    assert ent is not None and ent.account_status == "active" and ent.messaging_enabled
    assert list(ent.capability_availability or []) == ["conversation"], (
        "redeeming a link code must not confer capabilities beyond the conversation the code was for")


# --- the door stays shut ------------------------------------------------------------------------------

def test_a_stranger_with_no_code_is_granted_nothing(clean_db):
    """PUBLIC ROLLOUT STAYS CLOSED. The invitation is the operator-minted code; a number that has never
    been given one gets the prompt and no entitlement of any kind."""
    # A LEGITIMATE user first, so the zero below is self-checking. `production_account_entitlement` is
    # FORCE RLS; a session that cannot see the table returns 0 rows without erroring, and a ZERO from a
    # blind read looks exactly like a ZERO from a closed door. Proving the read can see SOMETHING is the
    # difference between this test meaning "no grant happened" and meaning nothing at all.
    invited, code = _new_user_with_code()
    asyncio.run(handle_inbound(FakeChannel(), _msg(mid="invited-1", text=code)))

    out = asyncio.run(handle_inbound(FakeChannel(), _msg(mid="stranger-1", text="hey whats up",
                                                         frm="+15559998888")))
    assert out.status == "unlinked_prompt"

    async def _owners():
        async with worker_session() as s:
            return [e.user_id for e in
                    (await s.execute(select(schema.ProductionAccountEntitlement))).scalars().all()]

    owners = asyncio.run(_owners())
    assert owners == [invited], (
        f"expected exactly the invited user to hold an entitlement, got {owners} — if this is empty the "
        f"read is blind and the test proves nothing")
    assert LINK_PROMPT


def test_a_bad_code_grants_nothing(clean_db):
    """A wrong or expired code must not be a way in. Same generic reply, and no grant."""
    uid, _real = _new_user_with_code()
    out = asyncio.run(handle_inbound(FakeChannel(), _msg(mid="bad-1", text="ZZZZZZ")))
    assert out.status == "bad_code"

    assert not _access(uid).allow, "a rejected code granted access to the code's owner anyway"


def test_an_expired_code_grants_nothing(clean_db):
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    past = _now() - datetime.timedelta(days=2)
    code, _ = asyncio.run(messaging_store.create_link_code(uid, now=past))

    out = asyncio.run(handle_inbound(FakeChannel(), _msg(mid="exp-1", text=code)))
    assert out.status == "bad_code"
    assert not _access(uid).allow


# --- it must be safe to run twice, and safe when it fails ---------------------------------------------

def test_redeeming_is_idempotent_and_does_not_stack_entitlements(clean_db):
    """A single-use code cannot be redeemed twice, but the GRANT must be idempotent regardless — a user
    who already has an entitlement and links a second device must not end up with a second row."""
    uid, code = _new_user_with_code()
    asyncio.run(handle_inbound(FakeChannel(), _msg(mid="idem-1", text=code)))

    code2, _ = asyncio.run(messaging_store.create_link_code(uid))
    asyncio.run(handle_inbound(FakeChannel(), _msg(mid="idem-2", text=code2, frm="+15551112222")))

    async def _ents():
        async with worker_session() as s:
            return (await s.execute(select(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == uid))).scalars().all()

    assert len(asyncio.run(_ents())) == 1
    assert _access(uid).allow


def test_a_failing_grant_does_not_swallow_the_successful_link(clean_db, monkeypatch):
    """THE LINK IS ALREADY COMMITTED AND THE CODE IS ALREADY SPENT by the time the grant runs.

    If the grant raises, telling the student nothing happened would be a lie that also costs them their
    single-use code. So redemption still reports `linked`, and the failure is an operator alert.
    """
    uid, code = _new_user_with_code()

    async def _boom(*_a, **_k):
        raise RuntimeError("entitlement store unavailable")

    monkeypatch.setattr(access_control, "activate_production_entitlement", _boom)

    out = asyncio.run(handle_inbound(FakeChannel(), _msg(mid="fail-1", text=code)))
    assert out.status == "linked" and out.user_id == uid, (
        "a failed grant threw away a link that had already been committed and a code that is now spent")
    assert not _access(uid).allow      # honestly degraded: linked, not yet entitled
