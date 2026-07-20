"""Bite 1.5 A3 — two-tier relay supervisor, offline (injected spawn/lock/clock, fake children).

launchd -> supervisor -> exactly one relay -> one imsg child set. The supervisor's state machine, restart
policy (bounded exponential backoff + jitter, stability-window reset), single-instance lock, child
reaping, exit-code handling (78 = revoked -> park; 0 = clean stop -> park), graceful shutdown, durable-
state preservation, rotating content-free logs, and brucectl health readout are exercised deterministically
with fake children — no real subprocesses (except one real orphan for the reap test). Each required
scenario is a named test.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

import pytest

from relay import brucectl
from relay import supervisor as S
from relay.supervisor import (
    REVOKED_CREDENTIAL_EXIT,
    RestartPolicy,
    SingleInstanceLock,
    State,
    Supervisor,
)


def _run(coro):
    return asyncio.run(coro)


class FakeChild:
    _seq = 20000

    def __init__(self, *, exit_code: int = 1, run_delay: float = 0.0, forever: bool = False) -> None:
        FakeChild._seq += 1
        self.pid = FakeChild._seq
        self.exit_code = exit_code
        self.run_delay = run_delay
        self.forever = forever
        self.terminated = False
        self.reaped = False
        self._done = asyncio.Event()

    async def wait(self) -> int:
        if self.forever:
            await self._done.wait()             # only completes when terminated/reaped
            return self.exit_code
        if self.run_delay:
            await asyncio.sleep(self.run_delay)
        return self.exit_code

    async def terminate(self) -> None:
        self.terminated = True
        self._done.set()

    async def reap(self) -> None:
        self.reaped = True
        self._done.set()


def _spawner(children):
    q = list(children)
    made = []

    async def spawn():
        c = q.pop(0) if q else FakeChild(forever=True, exit_code=0)   # after the script, a running child
        made.append(c)
        return c

    spawn.made = made
    return spawn


def _sup(tmp_path, spawn, *, policy=None, status="status.json", lock="sup.lock", kill_orphan=None):
    return Supervisor(
        spawn=spawn, lock=SingleInstanceLock(str(tmp_path / lock)),
        status_path=str(tmp_path / status), pinned_commit="pin-abc1234",
        policy=policy or RestartPolicy(base_backoff_s=0.001, max_backoff_s=1.0, jitter_frac=0.0,
                                       stability_window_s=100.0),
        kill_orphan=kill_orphan or (lambda pid: None))


async def _drive_until(sup, cond, timeout=3.0):
    task = asyncio.ensure_future(sup.run())
    start = time.monotonic()
    while not cond() and time.monotonic() - start < timeout:
        await asyncio.sleep(0.005)
    return task


async def _stop(sup, task):
    sup.request_stop()
    await asyncio.wait_for(task, timeout=3.0)


@pytest.fixture(autouse=True)
def _clean_supervisor_log():
    for h in list(S.log.handlers):
        S.log.removeHandler(h); h.close()
    yield
    for h in list(S.log.handlers):
        S.log.removeHandler(h); h.close()


# ------------------------------------------------- 1 normal start


def test_1_normal_start(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.RUNNING and sup.spawn_count == 1)
        assert sup.state == State.RUNNING
        st = json.loads((tmp_path / "status.json").read_text())
        assert st["pinned_commit"] == "pin-abc1234" and st["relay_pid"] == sp.made[0].pid
        await _stop(sup, task)
    _run(go())
    assert sp.made[0].terminated is True


# ------------------------------------------------- 2 relay crash and restart


def test_2_relay_crash_and_restart(tmp_path):
    sp = _spawner([FakeChild(exit_code=1), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2)
        assert sup.restart_count >= 1
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 3 repeated crash backoff (exponential)


def test_3_repeated_crash_backoff_grows(tmp_path):
    sp = _spawner([FakeChild(exit_code=1), FakeChild(exit_code=1), FakeChild(exit_code=1), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: len(sup.backoff_sleeps) >= 3)
        await _stop(sup, task)
    _run(go())
    b = sup.backoff_sleeps[:3]
    assert b[0] < b[1] < b[2]                     # bounded exponential (jitter=0 in this policy)


# ------------------------------------------------- 4 stability window resets backoff


def test_4_stability_window_resets_backoff(tmp_path):
    policy = RestartPolicy(base_backoff_s=0.001, max_backoff_s=1.0, jitter_frac=0.0, stability_window_s=0.01)
    sp = _spawner([FakeChild(exit_code=1), FakeChild(exit_code=1),
                   FakeChild(exit_code=1, run_delay=0.05), FakeChild(forever=True)])   # 3rd runs "stable"
    sup = _sup(tmp_path, sp, policy=policy)

    async def go():
        task = await _drive_until(sup, lambda: len(sup.backoff_sleeps) >= 3)
        await _stop(sup, task)
    _run(go())
    assert sup.backoff_sleeps[2] < sup.backoff_sleeps[1]   # reset after the healthy run


# ------------------------------------------------- 5 duplicate supervisor rejected


def test_5_duplicate_supervisor_rejected(tmp_path):
    path = str(tmp_path / "sup.lock")
    a = SingleInstanceLock(path); assert a.acquire() is True
    b = SingleInstanceLock(path); assert b.acquire() is False   # a LIVE holder -> rejected
    a.release()
    assert b.acquire() is True                                  # now free
    b.release()


def test_5b_duplicate_supervisor_run_returns_duplicate(tmp_path):
    held = SingleInstanceLock(str(tmp_path / "sup.lock")); assert held.acquire()
    sup = _sup(tmp_path, _spawner([FakeChild(forever=True)]))
    code = _run(asyncio.wait_for(sup.run(), timeout=2.0))
    assert code == 1 and sup.state == State.DUPLICATE and sup.spawn_count == 0
    held.release()


# ------------------------------------------------- 6 stale lock recovery


def test_6_stale_lock_recovered(tmp_path):
    path = str(tmp_path / "sup.lock")
    with open(path, "w") as f:                                  # a dead prior supervisor: file remains,
        json.dump({"pid": 999999, "start": 0, "host": "old"}, f)  # flock auto-released on its death
    lock = SingleInstanceLock(path)
    assert lock.acquire() is True                              # stale lock recovered
    assert lock.owner()["pid"] == os.getpid()
    lock.release()


# ------------------------------------------------- 7 stale relay child reaped


def test_7_stale_relay_child_reaped(tmp_path):
    orphan = subprocess.Popen(["sleep", "30"])
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "running", "relay_pid": orphan.pid,
                                       "pinned_commit": "x", "uptime_s": 1, "restart_count": 0}))
    killed = []
    sup = _sup(tmp_path, _spawner([FakeChild(forever=True)]), kill_orphan=lambda pid: killed.append(pid))
    try:
        async def go():
            task = await _drive_until(sup, lambda: sup.spawn_count >= 1)
            await _stop(sup, task)
        _run(go())
        assert orphan.pid in killed                            # orphan from a crashed supervisor reaped
    finally:
        orphan.terminate(); orphan.wait()


# ------------------------------------------------- 8 imsg children reaped (relay child terminated on stop)


def test_8_child_reaped_on_stop(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        await _stop(sup, task)
    _run(go())
    # the relay child (which reaps its own imsg subtree via aclose) is terminated + reaped
    assert sp.made[0].terminated is True and sp.made[0].reaped is True


# ------------------------------------------------- 9 stop directive does not restart-loop


def test_9_stop_directive_parks_no_restart(tmp_path):
    sp = _spawner([FakeChild(exit_code=0)])                     # relay parked cleanly (stop directive)
    sup = _sup(tmp_path, sp)
    code = _run(asyncio.wait_for(sup.run(), timeout=2.0))
    assert code == 0 and sup.state == State.PARKED and sup.spawn_count == 1 and sup.restart_count == 0


# ------------------------------------------------- 10 credential rejection exit 78 does not restart-loop


def test_10_revoked_credential_78_parks_no_restart(tmp_path):
    sp = _spawner([FakeChild(exit_code=REVOKED_CREDENTIAL_EXIT)])
    sup = _sup(tmp_path, sp)
    code = _run(asyncio.wait_for(sup.run(), timeout=2.0))
    assert code == 0 and sup.state == State.PARKED and sup.spawn_count == 1 and sup.restart_count == 0


# ------------------------------------------------- 11 network outage recovery


def test_11_network_outage_recovery(tmp_path):
    sp = _spawner([FakeChild(exit_code=1), FakeChild(forever=True)])   # outage crash -> restart -> healthy
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2 and sup.state == State.RUNNING)
        assert sup.state == State.RUNNING and sup.restart_count >= 1
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 12 API cold start recovery


def test_12_api_cold_start_recovery(tmp_path):
    sp = _spawner([FakeChild(exit_code=1, run_delay=0.01), FakeChild(forever=True)])   # cold-start fail then up
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2 and sup.state == State.RUNNING)
        await _stop(sup, task)
    _run(go())
    assert sup.restart_count >= 1


# ------------------------------------------------- 13 graceful shutdown


def test_13_graceful_shutdown(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        await _stop(sup, task)
    _run(go())
    assert sup.state == State.EXITED and sp.made[0].terminated is True
    assert sup.lock.owner() is not None                       # lock file remains but the flock is released
    assert SingleInstanceLock(sup.lock.path).acquire() is True  # released -> reacquirable


# ------------------------------------------------- 14 launchd-style restart of the supervisor


def test_14_launchd_style_restart(tmp_path):
    # first supervisor: relay parks cleanly, run() returns, lock released, status persisted
    sup1 = _sup(tmp_path, _spawner([FakeChild(exit_code=0)]))
    assert _run(asyncio.wait_for(sup1.run(), timeout=2.0)) == 0
    assert (tmp_path / "status.json").exists()
    # launchd relaunches the supervisor: a fresh one acquires the (released) lock and starts
    sp2 = _spawner([FakeChild(forever=True)])
    sup2 = _sup(tmp_path, sp2)

    async def go():
        task = await _drive_until(sup2, lambda: sup2.spawn_count == 1 and sup2.state == State.RUNNING)
        assert sup2.state == State.RUNNING
        await _stop(sup2, task)
    _run(go())


# ------------------------------------------------- 15-17 durable state survives a supervisor run


@pytest.mark.parametrize("fname,content", [
    ("checkpoint.json", {"processed": ["g1"]}),
    ("outbound_sent.json", {"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}}),
    ("pending_attachments.json", {"pending": {"heic-1": {"event": {"guid": "heic-1"}}}}),
])
def test_15_16_17_durable_state_survives_supervisor(tmp_path, fname, content):
    """The supervisor NEVER wipes durable state (checkpoint / outbound ledger / pending HEIC) on start."""
    (tmp_path / fname).write_text(json.dumps(content))
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        await _stop(sup, task)
    _run(go())
    assert json.loads((tmp_path / fname).read_text()) == content   # untouched


# ------------------------------------------------- 18 logs rotate


def test_18_supervisor_logs_rotate(tmp_path):
    log_path = str(tmp_path / "supervisor.log")
    S.setup_supervisor_logging(log_path, max_bytes=800, backups=3)
    for i in range(300):
        S.log.info("relay_spawned pid=%s spawn=%s restart=%s", 30000 + i, i, i)
    for h in S.log.handlers:
        h.flush()
    assert os.path.exists(log_path + ".1")                     # rotated


# ------------------------------------------------- 19 logs contain no private content


def test_19_supervisor_logs_no_private_content(tmp_path):
    log_path = str(tmp_path / "supervisor.log")
    S.setup_supervisor_logging(log_path, max_bytes=10_000_000, backups=1)
    sp = _spawner([FakeChild(exit_code=1), FakeChild(forever=True)])   # spawn + crash + backoff + respawn logs
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2)
        await _stop(sup, task)
    _run(go())
    for h in S.log.handlers:
        h.flush()
    text = open(log_path).read().lower()
    assert text                                                # something was logged
    for forbidden in ("secret", "bearer", "+1555", "@", "/users/", ".png", "text="):
        assert forbidden not in text


# ------------------------------------------------- 20 pinned commit reported correctly


def test_20_pinned_commit_reported(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        st = json.loads((tmp_path / "status.json").read_text())
        assert st["pinned_commit"] == "pin-abc1234"
        # brucectl surfaces it (content-free)
        out = brucectl.format_status(st, now=st["updated_at"])
        assert "pin-abc1234" in out and "restart_count" in out
        await _stop(sup, task)
    _run(go())


def test_20b_pinned_commit_from_env(monkeypatch):
    monkeypatch.setenv("BRUCE_RELAY_PINNED_COMMIT", "deadbeefcafe")
    assert S._pinned_commit() == "deadbeefcafe"
    monkeypatch.delenv("BRUCE_RELAY_PINNED_COMMIT", raising=False)
    assert S._pinned_commit() == "unpinned"
