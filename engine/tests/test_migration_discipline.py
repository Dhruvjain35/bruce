"""Migration discipline (D-INT-4, integration invariant 3) — CI enforcement for parallel worktrees.

Offline structural checks over the Alembic chain + schema, so four lanes cutting migrations in parallel
can't silently fork the head, duplicate a revision id, forget a lane reservation, ship a tenant policy
without owner_user_id, or drift the pinned head. FORCE-RLS + policy PROOF lives in the real-Postgres
tests (test_migration_rls_context, test_postgres_integration); this file guards the chain + conventions.
"""

from __future__ import annotations

import pathlib
import re

from bruce_engine.schema import RLS_TABLES, Base

_ENGINE = pathlib.Path(__file__).resolve().parents[1]
_VERSIONS = _ENGINE / "migrations" / "versions"


def _migrations() -> dict[str, dict]:
    """stem -> {revision, down_revision, text}."""
    out: dict[str, dict] = {}
    for f in sorted(_VERSIONS.glob("[0-9]*.py")):
        txt = f.read_text()
        rev = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", txt, re.M)
        down = re.search(r"^down_revision\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", txt, re.M)
        assert rev, f"{f.name}: no `revision` assignment"
        out[f.stem] = {"revision": rev.group(1),
                       "down_revision": down.group(1) if (down and down.group(1)) else None,
                       "text": txt}
    return out


def test_exactly_one_alembic_head():
    m = _migrations()
    revs = [d["revision"] for d in m.values()]
    downs = {d["down_revision"] for d in m.values() if d["down_revision"]}
    heads = [r for r in revs if r not in downs]
    assert len(heads) == 1, f"expected exactly one Alembic head, found {sorted(heads)} (multi-head = merge fork)"


def test_no_duplicate_revision_ids():
    revs = [d["revision"] for d in _migrations().values()]
    dupes = sorted({r for r in revs if revs.count(r) > 1})
    assert not dupes, f"duplicate revision ids: {dupes}"


def test_chain_is_linear_and_rooted():
    m = _migrations()
    revs = {d["revision"] for d in m.values()}
    roots = [s for s, d in m.items() if d["down_revision"] is None]
    assert len(roots) == 1, f"expected exactly one root migration, found {sorted(roots)}"
    for stem, d in m.items():
        if d["down_revision"] is not None:
            assert d["down_revision"] in revs, f"{stem}: down_revision {d['down_revision']} is orphaned"


def test_revision_id_matches_file_stem_for_0011_onward():
    # naming convention: 0001-0010 use a short slug; 0011+ MUST use the full file stem, so a new author
    # copying an old file can't set revision='0019_short' and slip a head-name mismatch past the pin.
    bad = [s for s, d in _migrations().items() if s >= "0011" and d["revision"] != s]
    assert not bad, f"0011+ migrations whose revision != file stem: {bad}"


def test_every_migration_has_upgrade_and_downgrade():
    for stem, d in _migrations().items():
        assert re.search(r"^def upgrade\(", d["text"], re.M), f"{stem}: no upgrade()"
        assert re.search(r"^def downgrade\(", d["text"], re.M), f"{stem}: no downgrade() (compat undocumented)"


def test_tenant_policy_migrations_declare_owner_user_id():
    # a migration that creates a tenant policy (references app_current_user()) must also manage a user_id
    # column — a tenant table without owner_user_id is an isolation hole.
    for stem, d in _migrations().items():
        if "app_current_user()" in d["text"]:
            assert "user_id" in d["text"], f"{stem}: tenant policy without a user_id column reference"


def test_reserved_migration_numbers_are_owned_and_unused_beyond_head():
    from migrations import lanes
    assert lanes.RESERVED_FROM_HEAD == "0018_conversation_context_graph"
    # every reserved number maps to a known workstream lane
    for num, owner in lanes.MIGRATION_LANES.items():
        assert re.fullmatch(r"\d{4}", num) and owner
    # any migration file numbered >= 0019 MUST be a reserved lane (prevents an unowned fork)
    for stem in _migrations():
        num = stem[:4]
        if num >= "0019":
            assert lanes.lane_for(num) is not None, f"{stem}: migration >=0019 with no lane reservation"


def test_pinned_head_assertion_matches_real_head():
    # the head is pinned in exactly one test (test_migration_rls_context); if a new migration lands without
    # bumping it, THIS test names the drift clearly instead of a cryptic failure deep in the RLS suite.
    m = _migrations()
    downs = {d["down_revision"] for d in m.values() if d["down_revision"]}
    real_head = next(d["revision"] for d in m.values() if d["revision"] not in downs)
    pin_file = (_ENGINE / "tests" / "test_migration_rls_context.py").read_text()
    pinned = set(re.findall(r"['\"](\d{4}_[a-z0-9_]+)['\"]", pin_file))
    assert real_head in pinned, f"head {real_head} not pinned in test_migration_rls_context.py — bump it"


def test_rls_tables_all_exist_in_schema():
    # RLS_TABLES is the curated has-a-policy list; every entry must be a real table (a typo = silent gap).
    tables = set(Base.metadata.tables)
    missing = [t for t in RLS_TABLES if t not in tables]
    assert not missing, f"RLS_TABLES names not in schema: {missing}"
