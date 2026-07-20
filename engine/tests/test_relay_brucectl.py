"""brucectl operator CLI (Bite 1.5 B1) — deterministic, dependency-injected coverage.

Every side effect (status file, control-plane DB read, API probe, launchctl, activation, log tail) is
injected via ``brucectl.Deps`` so the whole CLI is exercised WITHOUT a live supervisor, a Mac, launchd,
or Postgres. The pause/resume audit + queue-count assertions use the REAL ``bruce_test`` Postgres exactly
like tests/test_relay_control.py (skips cleanly when PG isn't configured).

Covered:
  * status/health aggregation + the structured exit-code scheme (healthy/degraded/stopped/parked/unauth);
  * start/stop/restart emit the right launchctl argv (bootstrap/bootout/kickstart) + idempotent no-ops;
  * the ownership guard refuses the wrong relay account / a foreign lock host (exit 3);
  * update verifies the EXACT commit + state compatibility; rollback BLOCKS incompatible state, no wipe;
  * pause/resume call the AUDITED relay_control functions with a server-derived actor;
  * logs are bounded (hard cap) and content-free;
  * redaction: no forbidden substring (handle/message/secret/attachment path) appears in ANY output.
"""

from __future__ import annotations

import json
import os

import pytest

from relay import brucectl, installer, state_manifest

_NO = object()   # sentinel: "use the real default read_db"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # the ownership guard reads BRUCE_RELAY_EXPECT_USER from the process env — keep tests hermetic.
    monkeypatch.delenv("BRUCE_RELAY_EXPECT_USER", raising=False)
    yield


def _deps(tmp_path, *, status=None, api=True, db=_NO, whoami="relayop", hostname="relaymac",
          version_present=True, safe_activate=None, plan=None, manifest=None, poll=True, now=1000.0,
          tail=None):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    install_dir = str(tmp_path / "app")
    status_path = os.path.join(state_dir, "supervisor-status.json")
    if status is not None:
        with open(status_path, "w") as f:
            json.dump(status, f)
    calls: list[list[str]] = []
    d = brucectl.Deps(
        state_dir=state_dir, install_dir=install_dir, status_path=status_path,
        log_path=os.path.join(state_dir, "supervisor.log"),
        lock_path=os.path.join(state_dir, "supervisor.lock"),
        home=str(tmp_path / "home"), uid=os.getuid(),
        api_base_url="https://api.example", python="/usr/bin/python3",
        now=lambda: now,
        whoami=lambda: whoami, hostname=lambda: hostname,
        run_cmd=lambda cmd: (calls.append(cmd), 0)[1],
        probe_api=lambda url: api,
        read_db=(lambda: db) if db is not _NO else brucectl._default_read_db,
        version_present=lambda i, c: version_present,
        poll_health=lambda sd: poll,
        tail_log=tail or brucectl._default_tail_log,
    )
    d.read_manifest = manifest or state_manifest.read_manifest
    d.plan_activation = plan or state_manifest.plan_activation
    d.safe_activate = safe_activate or state_manifest.safe_activate
    d.activate_version = installer.activate_version
    d.calls = calls
    return d


def _running(now=1000.0, **over):
    st = {"state": "running", "park_reason": None, "pinned_commit": "pinabc1234",
          "uptime_s": 42.0, "restart_count": 0, "relay_pid": 4321, "relay_pgid": 4321,
          "updated_at": now}
    st.update(over)
    return st


def _snap(**over):
    kw = dict(outbound_paused=False, reason_set=False, directive="run",
              queue_counts={"pending": 1, "sent": 3})
    kw.update(over)
    return brucectl.DbSnapshot(**kw)


# --------------------------------------------------------------------------- status/health + exit codes


def test_status_healthy_exit_0(tmp_path, capsys):
    d = _deps(tmp_path, status=_running(), api=True, db=_snap())
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_HEALTHY
    out = capsys.readouterr().out
    assert "HEALTHY" in out and "exit 0" in out


def test_health_json_is_content_free_and_carries_exit(tmp_path, capsys):
    d = _deps(tmp_path, status=_running(), api=True, db=_snap())
    assert brucectl.main(["health", "--json"], deps=d) == brucectl.EXIT_HEALTHY
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "HEALTHY" and payload["exit_code"] == 0
    assert payload["state_compat"] == "ok"
    assert payload["control_plane"]["queue_counts"] == {"pending": 1, "sent": 3}


def test_status_stale_is_degraded_exit_1(tmp_path):
    d = _deps(tmp_path, status=_running(updated_at=1000.0 - 300), now=1000.0, api=True, db=_snap())
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_DEGRADED


def test_status_api_down_is_degraded_exit_1(tmp_path):
    d = _deps(tmp_path, status=_running(), api=False, db=_snap())
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_DEGRADED


def test_status_outbound_paused_is_degraded_exit_1(tmp_path):
    d = _deps(tmp_path, status=_running(), api=True, db=_snap(outbound_paused=True, directive="pause_outbound"))
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_DEGRADED


def test_status_incompatible_state_is_degraded(tmp_path):
    # on-disk durable state newer than the pinned code understands -> surfaced as degraded.
    d = _deps(tmp_path, status=_running(), api=True, db=_snap(),
              plan=lambda existing: {"blocked": ["outbound_ledger"], "migrate": []})
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_DEGRADED


def test_status_no_supervisor_is_stopped_exit_2(tmp_path):
    d = _deps(tmp_path, status=None, api=True, db=_snap())   # no status file at all
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_STOPPED


def test_status_parked_is_exit_2(tmp_path):
    d = _deps(tmp_path, status=_running(state="parked", park_reason="stop"), api=True, db=_snap())
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_STOPPED


def test_status_db_unknown_degrades_gracefully(tmp_path, capsys):
    d = _deps(tmp_path, status=_running(), api=None, db=None)   # DB + API unavailable -> unknown
    rc = brucectl.main(["status"], deps=d)
    out = capsys.readouterr().out
    assert rc == brucectl.EXIT_HEALTHY          # unknown sources never fail the verdict
    assert "control-plane: unknown" in out


def test_status_wrong_user_is_unauthorized_exit_3(tmp_path, monkeypatch):
    monkeypatch.setenv("BRUCE_RELAY_EXPECT_USER", "someone-else")
    d = _deps(tmp_path, status=_running(), whoami="relayop", api=True, db=_snap())
    assert brucectl.main(["status"], deps=d) == brucectl.EXIT_UNAUTHORIZED


def test_diagnose_prints_hints(tmp_path, capsys):
    d = _deps(tmp_path, status=_running(), api=False, db=_snap(outbound_paused=True))
    brucectl.main(["diagnose"], deps=d)
    out = capsys.readouterr().out
    assert "hint:" in out and "issues:" in out


# --------------------------------------------------------------------------- start / stop / restart argv


def _plist(d):
    return installer.launchagent_path(d.home)


def test_start_when_stopped_bootstraps(tmp_path):
    d = _deps(tmp_path, status=None)                                   # not running
    assert brucectl.main(["start"], deps=d) == brucectl.EXIT_HEALTHY
    assert ["launchctl", "bootstrap", f"gui/{d.uid}", _plist(d)] in d.calls


def test_start_when_running_is_noop(tmp_path):
    d = _deps(tmp_path, status=_running())                            # already running
    assert brucectl.main(["start"], deps=d) == brucectl.EXIT_HEALTHY
    assert d.calls == []                                              # idempotent: no launchctl issued


def test_stop_when_running_boots_out(tmp_path):
    d = _deps(tmp_path, status=_running())
    assert brucectl.main(["stop"], deps=d) == brucectl.EXIT_STOPPED
    assert ["launchctl", "bootout", f"gui/{d.uid}", _plist(d)] in d.calls


def test_stop_when_stopped_is_noop(tmp_path):
    d = _deps(tmp_path, status=None)
    assert brucectl.main(["stop"], deps=d) == brucectl.EXIT_STOPPED
    assert d.calls == []


def test_restart_kickstarts_the_supervisor(tmp_path):
    d = _deps(tmp_path, status=_running())
    assert brucectl.main(["restart"], deps=d) == brucectl.EXIT_HEALTHY
    assert ["launchctl", "kickstart", "-k", f"gui/{d.uid}/com.bruce.relay.supervisor"] in d.calls


def test_mutating_commands_refuse_wrong_user(tmp_path, monkeypatch):
    monkeypatch.setenv("BRUCE_RELAY_EXPECT_USER", "someone-else")
    for argv in (["start"], ["stop"], ["restart"], ["update", "--commit", "a" * 40],
                 ["rollback", "--commit", "a" * 40]):
        d = _deps(tmp_path, status=_running(), whoami="relayop")
        assert brucectl.main(argv, deps=d) == brucectl.EXIT_UNAUTHORIZED
        assert d.calls == []                                         # never touched launchctl


def test_foreign_lock_host_refuses(tmp_path):
    d = _deps(tmp_path, status=_running(), hostname="relaymac")
    with open(d.lock_path, "w") as f:
        json.dump({"pid": 9, "start": 1.0, "host": "some-other-mac"}, f)
    assert brucectl.main(["restart"], deps=d) == brucectl.EXIT_UNAUTHORIZED
    assert d.calls == []


# --------------------------------------------------------------------------- update / rollback


def test_update_rejects_non_exact_sha(tmp_path):
    seen = []
    d = _deps(tmp_path, status=_running(),
              safe_activate=lambda **kw: seen.append(kw))
    assert brucectl.main(["update", "--commit", "abc123"], deps=d) == brucectl.EXIT_FAILED
    assert seen == []                                               # never delegated activation


def test_update_rejects_uninstalled_version(tmp_path):
    seen = []
    d = _deps(tmp_path, status=_running(), version_present=False,
              safe_activate=lambda **kw: seen.append(kw))
    assert brucectl.main(["update", "--commit", "a" * 40], deps=d) == brucectl.EXIT_FAILED
    assert seen == []


def test_update_delegates_to_safe_activate_with_exact_commit(tmp_path):
    seen = []
    d = _deps(tmp_path, status=_running(), version_present=True,
              safe_activate=lambda **kw: seen.append(kw))
    sha = "b" * 40
    assert brucectl.main(["update", "--commit", sha], deps=d) == brucectl.EXIT_HEALTHY
    assert len(seen) == 1 and seen[0]["commit"] == sha
    assert seen[0]["install_dir"] == d.install_dir and seen[0]["state_dir"] == d.state_dir


def test_rollback_blocks_incompatible_state_and_preserves_durable(tmp_path):
    # record an on-disk manifest NEWER than this code supports (outbound_ledger 999 >> CURRENT 2).
    state_manifest.write_manifest(_deps(tmp_path).state_dir, {"outbound_ledger": 999}, commit="old")
    d = _deps(tmp_path, status=_running(), version_present=True,
              plan=lambda existing: {"blocked": [], "migrate": []})  # pre-check passes -> the AUTHORITATIVE
    #                                                                   safe_activate gate must still block
    durable = os.path.join(d.state_dir, "outbound_sent.json")
    with open(durable, "w") as f:
        f.write('{"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}}')

    assert brucectl.main(["rollback", "--commit", "c" * 40], deps=d) == brucectl.EXIT_FAILED
    # durable state left exactly intact (never wiped on a refused rollback)
    assert open(durable).read().startswith('{"version": 2')
    assert d.calls == []                                            # activation never reached launchctl


# --------------------------------------------------------------------------- logs (bounded + content-free)


def test_logs_are_bounded_by_hard_cap(tmp_path, capsys):
    seen_n = {}

    def _tail(path, n):
        seen_n["n"] = n
        return [f"2026-07-20 INFO bruce.supervisor tick={i}" for i in range(n)]

    d = _deps(tmp_path, tail=_tail)
    brucectl.main(["logs", "--lines", "999999"], deps=d)
    assert seen_n["n"] == brucectl.LOGS_MAX_LINES                   # capped regardless of request
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == brucectl.LOGS_MAX_LINES + 1             # +1 header line


def test_logs_default_lines(tmp_path):
    seen_n = {}
    d = _deps(tmp_path, tail=lambda p, n: seen_n.setdefault("n", n) and [] or [])
    brucectl.main(["logs"], deps=d)
    assert seen_n["n"] == 50


# --------------------------------------------------------------------------- redaction sweep


def test_no_forbidden_substrings_in_any_output(tmp_path, capsys, monkeypatch):
    handle, body = "+15550001111", "hi there this is private message text"
    secret, attach = "Bearer sk-livesecret", "/Users/relay/Library/Messages/Attachments/x.heic"
    forbidden = [handle, body, secret, attach, "device-secret-value"]

    d = _deps(tmp_path, status=_running(), api=True,
              db=_snap(outbound_paused=True, reason_set=True, directive="pause_outbound"))
    with open(d.log_path, "w") as f:
        f.write("2026-07-20 INFO bruce.supervisor relay_spawned pid=4321 pgid=4321 spawn=1\n" * 8)

    for argv in (["status"], ["diagnose"], ["health", "--json"], ["logs"]):
        brucectl.main(argv, deps=d)

    # a pause reason carrying forbidden content must be handed to the AUDITED fn but NEVER echoed.
    monkeypatch.setenv("BRUCE_APP_DATABASE_URL", "postgresql://dummy/redaction-test")
    recorded = {}
    d.pause_all = lambda reason, actor: recorded.update(reason=reason, actor=actor)
    assert brucectl.main(["pause-outbound", "--reason", f"{handle} {body}"], deps=d) == brucectl.EXIT_HEALTHY

    out = capsys.readouterr().out
    for bad in forbidden:
        assert bad not in out, f"forbidden substring leaked: {bad!r}"
    # proof the reason DID reach the audited control-plane call (just not stdout)
    assert recorded["reason"] == f"{handle} {body}"
    assert recorded["actor"] == "relayop@relaymac"                  # server-derived actor


# --------------------------------------------------------------------------- PG-backed: audited pause/resume + queue counts


@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    """Point the app at bruce_test with NullPool (safe across per-command asyncio.run loops)."""
    import bruce_engine.db as db
    from sqlalchemy.ext.asyncio import create_async_engine as _real
    from sqlalchemy.pool import NullPool
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_pause_resume_are_audited_with_server_derived_actor(clean_db, _pg, tmp_path):
    from bruce_engine import relay_control

    d = _deps(tmp_path, status=_running(), whoami="op", hostname="mac1")

    assert brucectl.main(["pause-outbound", "--reason", "triage"], deps=d) == brucectl.EXIT_HEALTHY
    assert _run(relay_control.global_state())[0] is True            # global switch tripped
    rows = _run(relay_control.list_audit(20))
    assert any(r.action == "pause_all" and r.actor == "op@mac1" for r in rows)

    assert brucectl.main(["resume-outbound"], deps=d) == brucectl.EXIT_HEALTHY
    assert _run(relay_control.global_state())[0] is False
    rows2 = _run(relay_control.list_audit(20))
    assert any(r.action == "resume_all" and r.actor == "op@mac1" for r in rows2)


def test_status_reads_real_queue_counts(clean_db, _pg, tmp_path):
    from uuid import uuid4

    from sqlalchemy import select

    from bruce_engine import schema
    from bruce_engine.db import user_session, worker_session
    from bruce_engine.messaging import ChannelKind
    from bruce_engine import messaging_outbound

    uid = uuid4()

    async def _seed():
        async with user_session(uid) as s:
            if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
                s.add(schema.User(id=uid, auth_provider="apple"))
        await messaging_outbound.enqueue(user_id=uid, to_handle="+15550002222",
                                         channel=ChannelKind.self_hosted_imessage, kind="acknowledged",
                                         text="hi", idempotency_key="k-brucectl-1")
    _run(_seed())

    snap = brucectl._default_read_db()
    assert snap is not None
    assert snap.queue_counts.get("pending") == 1                    # content-free STATUS label + count
    assert snap.outbound_paused is False
