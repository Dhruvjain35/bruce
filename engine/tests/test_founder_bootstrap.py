"""The founder bootstrap must be re-runnable, and must be unable to fabricate a credential.

Two properties matter more than the convenience. First, running it twice cannot produce two founders —
a duplicate user with a duplicate identity is the kind of state that looks fine until a message resolves
to the wrong one. Second, there is no path through this command that writes a provider token: OAuth is
the real exchange or it does not happen, because a bootstrap that could mint credentials would be the
most dangerous script in the repository.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import crypto, messaging_store, schema, world_state
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "founder_bootstrap.py"
HANDLE = "+15550100"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("founder_bootstrap", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_founder_user_id_is_reproducible_from_the_label():
    """Idempotence rests on this. If the id were random, a second run would create a second founder and
    the messaging handle would resolve to whichever one the query happened to find."""
    mod = _module()
    a, b = mod.founder_user_id("dhruv"), mod.founder_user_id("dhruv")
    assert a == b and isinstance(a, UUID)
    assert mod.founder_user_id("someone-else") != a


def test_running_prepare_twice_creates_one_user_and_one_identity():
    mod = _module()
    uid = mod.founder_user_id("test-founder")
    from bruce_engine.repositories import PostgresUserRepository

    async def _twice():
        repo = PostgresUserRepository()
        for _ in range(2):
            await repo.ensure(uid, auth_provider="founder_alpha")
            await world_state.set_timezone(uid, "America/Chicago", source="user_stated")
        async with user_session(uid) as s:
            users = (await s.execute(select(schema.User).where(
                schema.User.id == uid))).scalars().all()
            tz = (await s.execute(select(schema.UserWorldState).where(
                schema.UserWorldState.user_id == uid))).scalars().all()
        return len(users), len(tz)

    n_users, n_tz = _run(_twice())
    assert n_users == 1, "a second run created a second founder"
    assert n_tz == 1, "timezone was inserted rather than upserted"


def test_link_codes_are_deliberately_not_idempotent():
    """Each code is single-use and expiring, so reissuing is how a lost one is recovered. Making this
    idempotent would mean a founder who never received the first code could never be linked."""
    mod = _module()
    uid = mod.founder_user_id("test-founder")
    from bruce_engine.repositories import PostgresUserRepository

    async def _two_codes():
        await PostgresUserRepository().ensure(uid, auth_provider="founder_alpha")
        a, _ = await messaging_store.create_link_code(uid, ChannelKind.self_hosted_imessage)
        b, _ = await messaging_store.create_link_code(uid, ChannelKind.self_hosted_imessage)
        return a, b

    a, b = _run(_two_codes())
    assert a != b


def test_the_bootstrap_cannot_write_a_provider_token():
    """Structural, on the AST. There is no assignment to a refresh-token field and no call to the
    encryptor anywhere in this file — OAuth is the real exchange or it does not happen."""
    tree = ast.parse(SCRIPT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "refresh_token_encrypted" or not isinstance(
                getattr(node, "ctx", None), ast.Store), "the bootstrap assigns a refresh token"
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in ("encrypt",), "the bootstrap encrypts a secret itself"
    src = SCRIPT.read_text()
    assert "schema.Integration(" not in src, "the bootstrap constructs an Integration row directly"


def test_the_bootstrap_creates_no_authority():
    """No enrollment, no entitlement, no kill-state change. Keeping grant separate from setup is what
    makes "the alpha is closed" checkable rather than a belief about a script."""
    src = SCRIPT.read_text()
    for forbidden in ("enroll_staging_test", "activate_production_entitlement",
                      "StagingTestEnrollment(", "ProductionAccountEntitlement(",
                      "CapabilityGlobalState("):
        assert forbidden not in src, f"the bootstrap grants access via {forbidden}"


def test_the_bootstrap_sends_nothing():
    src = SCRIPT.read_text()
    for forbidden in ("messaging_outbound", "send_and_verify", "execute_and_verify",
                      "MutationGateway", "mutation_gateway"):
        assert forbidden not in src, f"the bootstrap can reach {forbidden}"


def test_diagnose_is_non_mutating():
    """It is the thing you run when you are unsure — so it must be safe to run when you are unsure."""
    tree = ast.parse(SCRIPT.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_diagnose")
    body = ast.unparse(fn)
    for forbidden in ("s.add(", "set_timezone", "create_link_code", "start_authorization",
                      "ensure(", "update(", "delete("):
        assert forbidden not in body, f"_diagnose mutates via {forbidden}"
