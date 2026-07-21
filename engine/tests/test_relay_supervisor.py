"""Bite 1.5 A3 — two-tier relay supervisor, offline (injected spawn/lock/clock/directive, fake children).

launchd -> supervisor -> exactly one relay -> one imsg child set. Exercises the state machine, restart
policy, single-instance lock, owned-process-group reaping (safe against PID reuse), exit-code handling
(75 = authenticated stop -> park+poll+resume; 78 = revoked -> park; ANY OTHER incl. 0 -> restart),
graceful shutdown, durable-state preservation, rotating content-free logs, and brucectl. Deterministic
fakes — no real subprocesses except the process-group reap test. Each review gate has a named test.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time

import pytest

from relay import brucectl
from relay import supervisor as S
from relay.relay import MISCONFIGURED_EXIT, REVOKED_CREDENTIAL_EXIT, STOP_DIRECTIVE_EXIT
from relay.supervisor import RestartPolicy, SingleInstanceLock, State, Supervisor


def _run(coro):
    return asyncio.run(coro)


class FakeChild:
    _seq = 20000

    def __init__(self, *, exit_code: int = 1, run_delay: float = 0.0, forever: bool = False) -> None:
        FakeChild._seq += 1
        self.pid = FakeChild._seq
        self.pgid = self.pid
        self.start_token = f"tok-{self.pid}"
        self.exit_code = exit_code
        self.run_delay = run_delay
        self.forever = forever
        self.terminated = False
        self.reaped = False
        self._done = asyncio.Event()

    async def wait(self) -> int:
        if self.forever:
            await self._done.wait()
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
        c = q.pop(0) if q else FakeChild(forever=True, exit_code=0)
        made.append(c)
        return c

    spawn.made = made
    return spawn


def _sup(tmp_path, spawn, *, policy=None, directive_check=None, resume_poll_s=0.01, **kw):
    return Supervisor(
        spawn=spawn, lock=SingleInstanceLock(str(tmp_path / kw.pop("lock", "sup.lock"))),
        status_path=str(tmp_path / kw.pop("status", "status.json")), pinned_commit="pin-abc1234",
        directive_check=directive_check, resume_poll_s=resume_poll_s,
        policy=policy or RestartPolicy(base_backoff_s=0.001, max_backoff_s=1.0, jitter_frac=0.0,
                                       stability_window_s=100.0), **kw)


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


# ------------------------------------------------- 3 repeated crash backoff grows


def test_3_repeated_crash_backoff_grows(tmp_path):
    sp = _spawner([FakeChild(exit_code=1), FakeChild(exit_code=1), FakeChild(exit_code=1), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: len(sup.backoff_sleeps) >= 3)
        await _stop(sup, task)
    _run(go())
    b = sup.backoff_sleeps[:3]
    assert b[0] < b[1] < b[2]


# ------------------------------------------------- 4 stability window resets backoff


def test_4_stability_window_resets_backoff(tmp_path):
    policy = RestartPolicy(base_backoff_s=0.001, max_backoff_s=1.0, jitter_frac=0.0, stability_window_s=0.01)
    sp = _spawner([FakeChild(exit_code=1), FakeChild(exit_code=1),
                   FakeChild(exit_code=1, run_delay=0.05), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, policy=policy)

    async def go():
        task = await _drive_until(sup, lambda: len(sup.backoff_sleeps) >= 3)
        await _stop(sup, task)
    _run(go())
    assert sup.backoff_sleeps[2] < sup.backoff_sleeps[1]


# ------------------------------------------------- 5 duplicate supervisor rejected


def test_5_duplicate_supervisor_rejected(tmp_path):
    path = str(tmp_path / "sup.lock")
    a = SingleInstanceLock(path); assert a.acquire() is True
    b = SingleInstanceLock(path); assert b.acquire() is False
    a.release()
    assert b.acquire() is True
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
    with open(path, "w") as f:
        json.dump({"pid": 999999, "start": 0, "host": "old"}, f)
    lock = SingleInstanceLock(path)
    assert lock.acquire() is True
    assert lock.owner()["pid"] == os.getpid()
    lock.release()


# ------------------------------------------------- 7 stale relay child reaped (owned group)


def test_7_stale_relay_child_group_reaped(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "running", "relay_pid": 4242, "relay_pgid": 4242,
                                       "relay_start_token": "tok-match", "pinned_commit": "x"}))
    reaped = []
    sup = _sup(tmp_path, _spawner([FakeChild(forever=True)]),
               kill_group=lambda pgid, sig: reaped.append((pgid, sig)),
               pid_alive=lambda pid: True, proc_start_token=lambda pid: "tok-match")   # token MATCHES

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 1)
        await _stop(sup, task)
    _run(go())
    assert reaped and reaped[0][0] == 4242 and reaped[0][1] == signal.SIGKILL   # owned group reaped


# ------------------------------------------------- 7b stale PID reuse is NOT reaped


def test_7b_stale_pid_reuse_not_reaped(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "running", "relay_pid": 4242, "relay_pgid": 4242,
                                       "relay_start_token": "tok-OLD", "pinned_commit": "x"}))
    reaped = []
    sup = _sup(tmp_path, _spawner([FakeChild(forever=True)]),
               kill_group=lambda pgid, sig: reaped.append(pgid),
               pid_alive=lambda pid: True, is_group_leader=lambda pid: False,
               proc_start_token=lambda pid: "tok-NEW")            # token MISMATCH -> pid was reused

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 1)
        await _stop(sup, task)
    _run(go())
    assert reaped == []                                           # reused pid is NEVER killed


# ------------------------------------------------- 8 child reaped on stop (owned group)


def test_8_child_reaped_on_stop(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        await _stop(sup, task)
    _run(go())
    assert sp.made[0].terminated is True and sp.made[0].reaped is True


# ------------------------------------------------- 9 ACCIDENTAL exit 0 RESTARTS (does not park)


def test_9_accidental_exit_0_restarts_not_parks(tmp_path):
    sp = _spawner([FakeChild(exit_code=0), FakeChild(forever=True)])   # accidental clean exit
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2)
        assert sup.restart_count >= 1 and sup.state != State.PARKED    # RESTARTED, not parked
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 10 authenticated stop (75) parks, stays alive


def test_10_authenticated_stop_parks_alive_no_restart(tmp_path):
    sp = _spawner([FakeChild(exit_code=STOP_DIRECTIVE_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, directive_check=_const("stop"), resume_poll_s=0.2)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED and sup.park_reason == "stop")
        assert sup.spawn_count == 1 and sup.restart_count == 0 and not task.done()   # alive, no restart
        await _stop(sup, task)
    _run(go())
    assert sup.state == State.EXITED


# ------------------------------------------------- 11 revoked (78) parks without thrash


def test_11b_misconfigured_76_parks_no_crash_loop(tmp_path):
    """A fatal misconfig (e.g. the pinned imsg binary vanished -> relay exits MISCONFIGURED_EXIT) PARKS
    the supervisor alive instead of crash-looping; it resumes on a kickstart once fixed."""
    sp = _spawner([FakeChild(exit_code=MISCONFIGURED_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED and sup.park_reason == "misconfigured")
        await asyncio.sleep(0.05)                                # let a few cycles pass
        assert sup.spawn_count == 1 and sup.restart_count == 0 and not task.done()   # parked, no thrash
        await _stop(sup, task)
    _run(go())


def test_11_revoked_78_parks_no_thrash(tmp_path):
    async def _revoked():
        raise RuntimeError("401")                               # a revoked credential can't authenticate
    sp = _spawner([FakeChild(exit_code=REVOKED_CREDENTIAL_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, directive_check=_revoked, resume_poll_s=0.02)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED and sup.park_reason == "revoked")
        await asyncio.sleep(0.1)                                 # let several poll cycles pass
        assert sup.spawn_count == 1 and sup.restart_count == 0   # never restart-thrashes
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 12 PARKED polls control plane and RESUMES exactly one child


def test_12_parked_polls_and_resumes_one_child(tmp_path):
    flip = {"d": "stop"}

    async def _check():
        return flip["d"]
    sp = _spawner([FakeChild(exit_code=STOP_DIRECTIVE_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, directive_check=_check, resume_poll_s=0.01)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED)
        assert sup.spawn_count == 1
        flip["d"] = "run"                                        # control plane un-stops (remote resume)
        await _wait_for(lambda: sup.state == State.RUNNING and sup.spawn_count == 2)
        assert sup.spawn_count == 2                              # resumed with EXACTLY ONE new relay child
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 13 resume also works after a PAUSE_OUTBOUND directive


def test_13_resume_on_pause_outbound_directive(tmp_path):
    flip = {"d": "stop"}

    async def _check():
        return flip["d"]
    sp = _spawner([FakeChild(exit_code=STOP_DIRECTIVE_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, directive_check=_check, resume_poll_s=0.01)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED)
        flip["d"] = "pause_outbound"                             # not 'stop' -> relay should run (pause is internal)
        await _wait_for(lambda: sup.spawn_count == 2)
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 14 network outage recovery


def test_14_network_outage_recovery(tmp_path):
    sp = _spawner([FakeChild(exit_code=1), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2 and sup.state == State.RUNNING)
        assert sup.restart_count >= 1
        await _stop(sup, task)
    _run(go())


# ------------------------------------------------- 15 graceful shutdown


def test_15_graceful_shutdown(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        await _stop(sup, task)
    _run(go())
    assert sup.state == State.EXITED and sp.made[0].terminated is True
    assert SingleInstanceLock(sup.lock.path).acquire() is True   # lock released -> reacquirable


# ------------------------------------------------- 16 launchd-style restart of the supervisor


def test_16_launchd_style_restart(tmp_path):
    sp1 = _spawner([FakeChild(forever=True)])
    sup1 = _sup(tmp_path, sp1)

    async def first():
        task = await _drive_until(sup1, lambda: sup1.spawn_count == 1)
        await _stop(sup1, task)                                  # supervisor exits (launchd will relaunch)
    _run(first())
    assert (tmp_path / "status.json").exists()

    sp2 = _spawner([FakeChild(forever=True)])
    sup2 = _sup(tmp_path, sp2)

    async def second():
        task = await _drive_until(sup2, lambda: sup2.spawn_count == 1 and sup2.state == State.RUNNING)
        assert sup2.state == State.RUNNING                       # fresh supervisor acquired the released lock
        await _stop(sup2, task)
    _run(second())


# ------------------------------------------------- 17 durable state survives every path


@pytest.mark.parametrize("fname,content", [
    ("checkpoint.json", {"processed": ["g1"]}),
    ("outbound_sent.json", {"version": 2, "entries": {"o1": {"phase": "server_acknowledged"}}}),
    ("pending_attachments.json", {"pending": {"heic-1": {"event": {"guid": "heic-1"}}}}),
])
def test_17_durable_state_survives_park_resume_shutdown(tmp_path, fname, content):
    (tmp_path / fname).write_text(json.dumps(content))
    flip = {"d": "stop"}

    async def _check():
        return flip["d"]
    sp = _spawner([FakeChild(exit_code=STOP_DIRECTIVE_EXIT), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp, directive_check=_check, resume_poll_s=0.01)

    async def go():
        task = await _drive_until(sup, lambda: sup.state == State.PARKED)   # start -> park
        flip["d"] = "run"
        await _wait_for(lambda: sup.spawn_count == 2)                       # resume
        await _stop(sup, task)                                              # shutdown
    _run(go())
    assert json.loads((tmp_path / fname).read_text()) == content           # never wiped across any path


# ------------------------------------------------- 18 logs rotate


def test_18_supervisor_logs_rotate(tmp_path):
    log_path = str(tmp_path / "supervisor.log")
    S.setup_supervisor_logging(log_path, max_bytes=800, backups=3)
    for i in range(300):
        S.log.info("relay_spawned pid=%s pgid=%s spawn=%s", 30000 + i, 30000 + i, i)
    for h in S.log.handlers:
        h.flush()
    assert os.path.exists(log_path + ".1")


# ------------------------------------------------- 19 logs contain no private content


def test_19_supervisor_logs_no_private_content(tmp_path):
    log_path = str(tmp_path / "supervisor.log")
    S.setup_supervisor_logging(log_path, max_bytes=10_000_000, backups=1)
    sp = _spawner([FakeChild(exit_code=1), FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count >= 2)
        await _stop(sup, task)
    _run(go())
    for h in S.log.handlers:
        h.flush()
    text = open(log_path).read().lower()
    assert text
    for forbidden in ("secret", "bearer", "+1555", "@", "/users/", ".png", ".heic", "text="):
        assert forbidden not in text


# ------------------------------------------------- 20 pinned commit reported


def test_20_pinned_commit_reported(tmp_path):
    sp = _spawner([FakeChild(forever=True)])
    sup = _sup(tmp_path, sp)

    async def go():
        task = await _drive_until(sup, lambda: sup.spawn_count == 1)
        st = json.loads((tmp_path / "status.json").read_text())
        assert st["pinned_commit"] == "pin-abc1234"
        out = brucectl.format_status(st, now=st["updated_at"])
        assert "pin-abc1234" in out and "restart_count" in out
        await _stop(sup, task)
    _run(go())


def test_20b_pinned_commit_from_env(monkeypatch):
    monkeypatch.setenv("BRUCE_RELAY_PINNED_COMMIT", "deadbeefcafe")
    assert S._pinned_commit() == "deadbeefcafe"
    monkeypatch.delenv("BRUCE_RELAY_PINNED_COMMIT", raising=False)
    assert S._pinned_commit() == "unpinned"


# ------------------------------------------------- 21 real process-GROUP reap kills the whole owned group


def test_21_process_group_reap_kills_whole_group(tmp_path):
    from relay.supervisor import SubprocessChild

    async def go():
        # a session leader that forks a child; both are in the new session/process group
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "sleep 300 & echo $!; wait", start_new_session=True,
            stdout=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        child_pid = int(line.strip())
        assert S._pid_alive(proc.pid) and S._pid_alive(child_pid)
        await SubprocessChild(proc).reap()                       # killpg the owned group
        await asyncio.sleep(0.2)
        return child_pid, proc.pid
    child_pid, parent_pid = _run(go())
    assert not S._pid_alive(parent_pid) and not S._pid_alive(child_pid)   # ENTIRE group reaped


# --------------------------------------------------------------------------- test helpers


def _const(value):
    async def _c():
        return value
    return _c


async def _wait_for(cond, timeout=3.0):
    start = time.monotonic()
    while not cond() and time.monotonic() - start < timeout:
        await asyncio.sleep(0.005)
    assert cond(), "condition not met in time"
