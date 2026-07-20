"""Two-tier relay supervisor (Bite 1.5 A3).

    launchd  ->  supervisor  ->  exactly one relay process  ->  one imsg watcher/send child set

A LaunchAgent-compatible FOREGROUND supervisor owns exactly one relay child. It:

  * RESTARTS the relay on any UNEXPECTED exit — including an accidental clean exit 0 — with bounded
    exponential backoff + jitter (reset after a healthy stability window);
  * PARKS (stays ALIVE, does not exit, does not restart-thrash) on an AUTHENTICATED stop (relay exit 75)
    or a revoked/invalid credential (exit 78). While PARKED it POLLS the authenticated control plane and
    RESUMES — starting exactly one relay child — when the directive is no longer `stop` (without
    reinstalling, re-registering, or killing anything);
  * holds a single-instance ownership lock (rejects a duplicate supervisor, recovers a stale lock), and
    reaps ONLY the owned relay/imsg PROCESS GROUP — safe against PID reuse via a process-start token;
  * shuts down gracefully on SIGTERM/SIGINT (operator shutdown), writes a content-free health status
    (pinned commit, uptime, restart count) for brucectl, NEVER git-pulls, NEVER wipes durable state, and
    NEVER logs message content, handles, attachment paths, or secrets.

Because PARKED stays alive and the backoff is bounded, neither launchd KeepAlive (which only relaunches
the whole supervisor if it truly dies) nor the internal backoff can create a restart loop.

The core is dependency-injected (spawn / lock / clock / directive_check / reaper helpers) so every path is
exercised deterministically without real processes; ``build_real_supervisor`` wires the production spawn.
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
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Awaitable, Callable, Protocol
import logging

from .relay import REVOKED_CREDENTIAL_EXIT, STOP, STOP_DIRECTIVE_EXIT

log = logging.getLogger("bruce.supervisor")

# Relay exit codes that mean "PARK, do not restart" (everything else, including 0, is a restart).
_PARK_EXITS = {STOP_DIRECTIVE_EXIT: "stop", REVOKED_CREDENTIAL_EXIT: "revoked"}


class State(str, enum.Enum):
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    PARKED = "parked"          # authenticated stop / revoked cred — alive, polling for resume, not restarting
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
    pgid: int
    start_token: str | None
    async def wait(self) -> int: ...
    async def terminate(self) -> None: ...   # graceful SIGTERM to the owned group, then reap
    async def reap(self) -> None: ...        # ensure the owned group is fully reaped (SIGKILL if needed)


# --------------------------------------------------------------------------- process helpers


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


def _is_group_leader(pid: int) -> bool:
    """Our relays are always session/group leaders (start_new_session); a randomly reused pid usually is
    not, so this is a cheap extra guard against reaping the wrong process."""
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _kill_group(pgid: int, sig: int) -> None:
    """Signal ONLY the owned process group (the relay + its imsg children) — never a broad kill."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _proc_start_token(pid: int) -> str | None:
    """A best-effort process-START token used to detect PID REUSE: a reaped orphan is only killed if the
    pid still maps to the SAME process (same start time). Linux: /proc/<pid>/stat starttime; macOS: ps
    lstart. None if undeterminable."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        return data.rsplit(")", 1)[1].split()[19]      # field 22 (starttime), robust to a ')' in comm
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


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
                 directive_check: Callable[[], Awaitable[str]] | None = None, resume_poll_s: float = 15.0,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 rng: random.Random | None = None,
                 kill_group: Callable[[int, int], None] = _kill_group,
                 pid_alive: Callable[[int], bool] = _pid_alive,
                 is_group_leader: Callable[[int], bool] = _is_group_leader,
                 proc_start_token: Callable[[int], "str | None"] = _proc_start_token) -> None:
        self.spawn = spawn
        self.lock = lock
        self.status_path = status_path
        self.pinned_commit = pinned_commit
        self.policy = policy
        self.directive_check = directive_check
        self.resume_poll_s = resume_poll_s
        self.clock = clock
        self.wall_clock = wall_clock
        self._rng = rng or random.Random()
        self.kill_group = kill_group
        self.pid_alive = pid_alive
        self.is_group_leader = is_group_leader
        self.proc_start_token = proc_start_token
        self.state = State.STARTING
        self.restart_count = 0
        self.backoff_sleeps: list[float] = []     # observable: the computed backoff per restart
        self.spawn_count = 0
        self.park_reason: str | None = None       # "stop" | "revoked" | None
        self._consecutive = 0
        self._started_at = clock()
        self._stop = asyncio.Event()
        self._child: Child | None = None
        self.relay_pid: int | None = None
        self.relay_pgid: int | None = None
        self.relay_start_token: str | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def _uptime(self) -> float:
        return round(self.clock() - self._started_at, 3)

    def _backoff(self) -> float:
        raw = min(self.policy.base_backoff_s * (2 ** max(0, self._consecutive - 1)), self.policy.max_backoff_s)
        jitter = raw * self.policy.jitter_frac
        return max(0.0, raw + self._rng.uniform(-jitter, jitter))

    def status(self) -> dict:
        """Content-free health snapshot (brucectl reads this): state / park reason / pinned commit /
        uptime / restart count / relay pid+pgid. NEVER message content, handles, paths, or secrets."""
        return {"state": self.state.value, "park_reason": self.park_reason,
                "pinned_commit": self.pinned_commit, "uptime_s": self._uptime(),
                "restart_count": self.restart_count, "relay_pid": self.relay_pid,
                "relay_pgid": self.relay_pgid, "relay_start_token": self.relay_start_token,
                "updated_at": round(self.wall_clock(), 3)}

    def _write_status(self) -> None:
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.status(), f)
        os.replace(tmp, self.status_path)

    def _recover_orphan(self) -> None:
        """A previous supervisor may have died without reaping its relay. If the last status names a
        still-alive relay whose START TOKEN still matches (i.e. the pid was NOT reused) — or, absent a
        token, a process that is still a group leader — reap ONLY that owned process group. A reused pid
        (token mismatch) is never killed."""
        prior = _read_json(self.status_path)
        if not prior:
            return
        pid = prior.get("relay_pid")
        pgid = prior.get("relay_pgid") or pid
        token = prior.get("relay_start_token")
        if not pid or not self.pid_alive(int(pid)):
            return
        now_token = self.proc_start_token(int(pid))
        same_process = token is not None and now_token is not None and token == now_token
        if same_process or (token is None and self.is_group_leader(int(pid))):
            log.warning("reaping orphan relay group pgid=%s", pgid)   # content-free
            self.kill_group(int(pgid), signal.SIGKILL)
        else:
            log.warning("recorded relay pid=%s appears reused — not reaping (safe)", pid)

    async def run(self) -> int:
        if not self.lock.acquire():
            self.state = State.DUPLICATE
            log.error("another supervisor already owns %s — refusing to start", self.lock.path)
            return 1
        try:
            self._recover_orphan()
            self.state = State.RUNNING
            while not self._stop.is_set():
                if self.state == State.PARKED:
                    await self._park_and_wait_for_resume()      # alive: poll control plane, resume on un-stop
                    continue

                # ---- run exactly one relay lifecycle ----
                child = await self.spawn()
                self.spawn_count += 1
                self._child = child
                self.relay_pid = getattr(child, "pid", None)
                self.relay_pgid = getattr(child, "pgid", None)
                self.relay_start_token = getattr(child, "start_token", None)
                self.park_reason = None
                start = self.clock()
                self.state = State.RUNNING
                self._write_status()
                log.info("relay_spawned pid=%s pgid=%s spawn=%s", self.relay_pid, self.relay_pgid, self.spawn_count)
                code = await self._wait_or_stop(child)
                ran = self.clock() - start

                if self._stop.is_set():                          # operator shutdown mid-run
                    self.state = State.STOPPING
                    await child.terminate()                      # graceful SIGTERM to the owned group
                    self.relay_pid = None
                    break                                        # finally reaps as a safety net

                await child.reap()
                self._child = None
                self.relay_pid = None

                if code in _PARK_EXITS:
                    self.park_reason = _PARK_EXITS[code]
                    self.state = State.PARKED
                    self._write_status()
                    log.warning("relay parked (%s, code=%s) — alive, polling for resume", self.park_reason, code)
                    continue                                     # PARKED branch handles polling/resume

                # ACCIDENTAL clean exit (0) OR crash -> restart with bounded backoff + jitter.
                self.restart_count += 1
                if ran >= self.policy.stability_window_s:
                    self._consecutive = 0                        # was healthy -> reset the backoff ladder
                self._consecutive += 1
                backoff = self._backoff()
                self.backoff_sleeps.append(backoff)
                self.state = State.BACKOFF
                self._write_status()
                log.warning("relay exited code=%s (unexpected) — restart=%s after backoff=%.3fs",
                            code, self.restart_count, backoff)
                await self._sleep_or_stop(backoff)
                self.state = State.RUNNING
            return 0
        finally:
            if self._child is not None:
                try:
                    await self._child.reap()                     # never leave an orphan
                except Exception:
                    pass
            self.lock.release()
            self.state = State.EXITED
            self._write_status()

    async def _park_and_wait_for_resume(self) -> None:
        """Stay ALIVE while parked: wait a BOUNDED poll interval, then check the authenticated control
        plane. Resume (state -> RUNNING, so the loop spawns exactly one relay) when the directive is no
        longer `stop`. A failed/unauthenticated check (revoked/network) keeps the supervisor parked —
        never a thrash, never a false resume. Nothing is reinstalled or killed."""
        self._write_status()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.resume_poll_s)
            return                                               # operator shutdown -> loop exits
        except asyncio.TimeoutError:
            pass
        if self.directive_check is None:
            return                                               # no control-plane check wired -> stay parked
        try:
            directive = await self.directive_check()
        except Exception:
            return                                               # cannot authenticate -> stay parked (no thrash)
        if directive is not None and directive != STOP:
            log.info("control plane no longer 'stop' (%s) — resuming", directive)
            self.park_reason = None
            self.state = State.RUNNING                           # resume: next loop iteration spawns ONE relay

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
    """Wraps a real ``python -m relay`` subprocess started in its OWN session/process group, so the relay
    and its imsg children are reaped together — and ONLY that owned group is ever signalled."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid
        try:
            self.pgid = os.getpgid(proc.pid)                     # == pid (start_new_session leader)
        except OSError:
            self.pgid = proc.pid
        self.start_token = _proc_start_token(proc.pid)

    async def wait(self) -> int:
        return await self._proc.wait()

    async def terminate(self) -> None:
        _kill_group(self.pgid, signal.SIGTERM)                   # graceful: the relay unwinds + reaps imsg
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                await self.reap()

    async def reap(self) -> None:
        _kill_group(self.pgid, signal.SIGKILL)                   # only the owned group
        if self._proc.returncode is None:
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
    """Wire the production supervisor: spawn ``python -m relay`` in its OWN process group (inheriting the
    env, which carries the pinned commit + relay config), and poll the SAME authenticated directive
    endpoint the relay uses for resume. Lock + status live under the relay state dir; durable state is
    never wiped."""
    from .backend import HttpBackend
    from .config import RelayConfig

    state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
    os.makedirs(state, exist_ok=True)
    setup_supervisor_logging(os.path.join(state, "supervisor.log"))
    cfg = RelayConfig.from_env()
    backend = HttpBackend(cfg.base_url, cfg.secret)

    async def _spawn() -> Child:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "relay", env={**os.environ},
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True)   # own session/process group
        return SubprocessChild(proc)

    async def _directive_check() -> str:
        return await backend.directive()                        # authenticated as the device; raises on failure

    return Supervisor(spawn=_spawn, lock=SingleInstanceLock(os.path.join(state, "supervisor.lock")),
                      status_path=os.path.join(state, "supervisor-status.json"),
                      pinned_commit=_pinned_commit(), directive_check=_directive_check)


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
