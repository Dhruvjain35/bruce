"""Two-tier relay supervisor (Bite 1.5 A3).

    launchd  ->  supervisor  ->  exactly one relay process  ->  one imsg watcher/send child set

A LaunchAgent-compatible FOREGROUND supervisor owns exactly one relay child, restarts it with bounded
exponential backoff + jitter (reset after a healthy stability window), and PARKS (no restart) on a clean
stop or a revoked-credential exit (78). It holds a single-instance ownership lock (rejecting a duplicate
supervisor, recovering a stale lock), reaps stale relay/imsg children, shuts down gracefully on
SIGTERM/SIGINT, and writes a content-free health status (pinned commit, uptime, restart count) that
brucectl reads. It NEVER git-pulls, NEVER wipes durable state (checkpoint / ledger / pending attachments),
and NEVER logs message content, handles, attachment paths, or secrets.

The core is fully dependency-injected (spawn / clock / lock) so the state machine, restart policy, and
child reaping are exercised deterministically without real processes; ``build_real_supervisor`` wires the
production subprocess spawn.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import os
import random
import signal
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Awaitable, Callable, Protocol
import logging

log = logging.getLogger("bruce.supervisor")

REVOKED_CREDENTIAL_EXIT = 78   # EX_CONFIG: revoked/invalid credential -> park, never restart-loop
CLEAN_EXIT = 0                 # stop directive / graceful signal -> park


class State(str, enum.Enum):
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    PARKED = "parked"          # stop directive or exit 78 — intentionally not restarting
    STOPPING = "stopping"
    EXITED = "exited"
    DUPLICATE = "duplicate"    # another supervisor already owns the lock


@dataclasses.dataclass(frozen=True)
class RestartPolicy:
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    jitter_frac: float = 0.2
    stability_window_s: float = 30.0   # a relay that ran healthy this long resets the backoff to base


class Child(Protocol):
    pid: int
    async def wait(self) -> int: ...
    async def terminate(self) -> None: ...   # graceful SIGTERM, then reap
    async def reap(self) -> None: ...        # ensure fully reaped (SIGKILL if needed)


# --------------------------------------------------------------------------- helpers


def _host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- single-instance lock


class SingleInstanceLock:
    """flock-based single-instance ownership. ``acquire()`` is NON-blocking: a LIVE holder -> rejected
    (duplicate supervisor); a dead holder's flock is auto-released by the OS -> a new supervisor recovers
    the stale lock. The file also carries {pid,start,host} for observability (never a secret)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        import fcntl
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False                       # a LIVE supervisor holds it -> duplicate rejected
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "start": time.time(), "host": _host()}).encode())
        os.fsync(fd)
        return True

    def owner(self) -> dict | None:
        return _read_json(self.path)

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


# --------------------------------------------------------------------------- supervisor


class Supervisor:
    def __init__(self, *, spawn: Callable[[], Awaitable[Child]], lock: SingleInstanceLock,
                 status_path: str, pinned_commit: str, policy: RestartPolicy = RestartPolicy(),
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 kill_orphan: Callable[[int], None] = _kill_pid, rng: random.Random | None = None) -> None:
        self.spawn = spawn
        self.lock = lock
        self.status_path = status_path
        self.pinned_commit = pinned_commit
        self.policy = policy
        self.clock = clock
        self.wall_clock = wall_clock
        self.kill_orphan = kill_orphan
        self._rng = rng or random.Random()
        self.state = State.STARTING
        self.restart_count = 0
        self.backoff_sleeps: list[float] = []     # observable: the computed backoff per restart
        self.spawn_count = 0
        self._consecutive = 0
        self._started_at = clock()
        self._stop = asyncio.Event()
        self._child: Child | None = None
        self.relay_pid: int | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def _uptime(self) -> float:
        return round(self.clock() - self._started_at, 3)

    def _backoff(self) -> float:
        raw = min(self.policy.base_backoff_s * (2 ** max(0, self._consecutive - 1)), self.policy.max_backoff_s)
        jitter = raw * self.policy.jitter_frac
        return max(0.0, raw + self._rng.uniform(-jitter, jitter))

    def status(self) -> dict:
        """Content-free health snapshot (brucectl reads this): state / pinned commit / uptime / restart
        count / relay pid. NEVER message content, handles, attachment paths, or secrets."""
        return {"state": self.state.value, "pinned_commit": self.pinned_commit, "uptime_s": self._uptime(),
                "restart_count": self.restart_count, "relay_pid": self.relay_pid,
                "updated_at": round(self.wall_clock(), 3)}

    def _write_status(self) -> None:
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.status(), f)
        os.replace(tmp, self.status_path)

    def _recover_orphan(self) -> None:
        """A previous supervisor may have died without reaping its relay child. If the last status names a
        still-alive relay pid, reap it before starting a new one (single imsg child set)."""
        prior = _read_json(self.status_path)
        pid = prior.get("relay_pid") if prior else None
        if pid and _pid_alive(int(pid)):
            log.warning("reaping orphan relay child pid=%s", pid)   # content-free
            self.kill_orphan(int(pid))

    async def run(self) -> int:
        if not self.lock.acquire():
            self.state = State.DUPLICATE
            log.error("another supervisor already owns %s — refusing to start", self.lock.path)
            return 1
        try:
            self._recover_orphan()
            while not self._stop.is_set():
                self.state = State.RUNNING
                child = await self.spawn()
                self.spawn_count += 1
                self._child = child
                self.relay_pid = getattr(child, "pid", None)
                start = self.clock()
                self._write_status()
                log.info("relay_spawned pid=%s spawn=%s", self.relay_pid, self.spawn_count)
                code = await self._wait_or_stop(child)
                ran = self.clock() - start

                if self._stop.is_set():                        # graceful shutdown requested
                    self.state = State.STOPPING
                    await child.terminate()
                    self.relay_pid = None
                    break

                await child.reap()
                self.relay_pid = None

                if code == REVOKED_CREDENTIAL_EXIT:
                    log.error("relay exited 78 (revoked/invalid credential) — parking, no restart")
                    self.state = State.PARKED
                    break
                if code == CLEAN_EXIT:
                    log.info("relay parked cleanly (stop directive) — no restart")
                    self.state = State.PARKED
                    break

                # crash -> restart with bounded exponential backoff + jitter; reset after a stable window.
                self.restart_count += 1
                if ran >= self.policy.stability_window_s:
                    self._consecutive = 0                       # was healthy -> reset the backoff ladder
                self._consecutive += 1
                backoff = self._backoff()
                self.backoff_sleeps.append(backoff)
                self.state = State.BACKOFF
                self._write_status()
                log.warning("relay crashed code=%s restart=%s backoff=%.3fs", code, self.restart_count, backoff)
                await self._sleep_or_stop(backoff)
            return 0
        finally:
            if self._child is not None:
                try:
                    await self._child.reap()                    # never leave an orphan
                except Exception:
                    pass
            self.lock.release()
            if self.state != State.PARKED:                      # a park is intentional -> keep it observable
                self.state = State.EXITED
            self._write_status()

    async def _wait_or_stop(self, child: Child) -> int:
        stop_task = asyncio.ensure_future(self._stop.wait())
        wait_task = asyncio.ensure_future(child.wait())
        try:
            await asyncio.wait({stop_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop_task, wait_task):
                if not t.done():
                    t.cancel()
        if wait_task.done() and not wait_task.cancelled():
            return wait_task.result()
        return -1                                               # stopped before the child exited

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)   # honor stop during backoff
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- real subprocess child + wiring


class SubprocessChild:
    """Wraps a real ``python -m relay`` subprocess as a Child."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid

    async def wait(self) -> int:
        return await self._proc.wait()

    async def terminate(self) -> None:
        if self._proc.returncode is None:
            try:
                self._proc.terminate()                          # SIGTERM: the relay unwinds gracefully
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                await self.reap()

    async def reap(self) -> None:
        if self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            try:
                await self._proc.wait()
            except Exception:
                pass


def setup_supervisor_logging(log_path: str, *, max_bytes: int = 1_000_000, backups: int = 5) -> None:
    """Content-free ROTATING supervisor log (ids/states/counts only). Never logs message content/paths."""
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backups)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)


def _pinned_commit() -> str:
    """The pinned APPROVED relay commit the supervisor runs — NEVER an automatic git pull. Read from
    BRUCE_RELAY_PINNED_COMMIT (or a pinned-commit file); update/rollback is reserved for B1."""
    commit = os.environ.get("BRUCE_RELAY_PINNED_COMMIT")
    if commit:
        return commit.strip()[:64]
    path = os.environ.get("BRUCE_RELAY_PINNED_COMMIT_FILE", "")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return f.read().strip()[:64]
        except OSError:
            pass
    return "unpinned"


def build_real_supervisor() -> Supervisor:
    """Wire the production supervisor: spawn ``python -m relay`` (inheriting the env, which carries the
    pinned commit + relay config), lock + status under the relay state dir. Durable state is never wiped."""
    state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
    os.makedirs(state, exist_ok=True)
    setup_supervisor_logging(os.path.join(state, "supervisor.log"))

    async def _spawn() -> Child:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "relay", env={**os.environ},
            stdin=asyncio.subprocess.DEVNULL)
        return SubprocessChild(proc)

    return Supervisor(spawn=_spawn, lock=SingleInstanceLock(os.path.join(state, "supervisor.lock")),
                      status_path=os.path.join(state, "supervisor-status.json"), pinned_commit=_pinned_commit())


def main() -> None:
    sup = build_real_supervisor()
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sup.request_stop)      # graceful shutdown
        except (NotImplementedError, ValueError):
            pass
    try:
        code = loop.run_until_complete(sup.run())
    finally:
        loop.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
