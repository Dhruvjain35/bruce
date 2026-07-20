"""brucectl — the operator CLI for the self-hosted iMessage relay supervisor (Bite 1.5 B1).

Run on the dedicated relay Mac, under the `bruce-relay` login account. It is the ONLY hands-on control
surface for the supervisor + relay: read health, start/stop/restart through launchd, upgrade/rollback the
pinned code, pause/resume outbound, and tail the (already content-free) supervisor log.

    python -m relay.brucectl status
    python -m relay.brucectl health [--json]
    python -m relay.brucectl diagnose
    python -m relay.brucectl start | stop | restart
    python -m relay.brucectl update --commit <approved_sha>
    python -m relay.brucectl rollback --commit <prior_sha>
    python -m relay.brucectl pause-outbound [--reason "..."] | resume-outbound
    python -m relay.brucectl logs [--lines N]

Contract it respects (never violates):
  * It NEVER kills or spawns the relay process directly — start/stop/restart go through
    ``launchctl bootstrap`` / ``bootout`` / ``kickstart`` for the supervisor LaunchAgent, and the
    supervisor owns the single relay child (respecting the A3 supervisor ownership contract).
  * update/rollback delegate to ``relay.state_manifest.safe_activate`` + the installer's version
    activation — an atomic symlink swap, compatibility-gated, with auto-restore. Durable state is NEVER
    wiped, and an INCOMPATIBLE rollback (on-disk state newer than the target supports) is refused.
  * pause-outbound / resume-outbound wrap the AUDITED ``bruce_engine.relay_control`` functions; the actor
    is derived SERVER-SIDE (shell user@host), never a client flag.
  * Ownership guard: before any mutating action it verifies the expected relay account
    (``BRUCE_RELAY_EXPECT_USER``), the supervisor lock owner's host, and that the state dir is owned by
    the current user — refusing (exit 3) on mismatch.
  * REDACTION: everything printed is content-free. It never prints handles, secrets, message content,
    attachment paths, operator-supplied reason free-text, or any API payload body. It surfaces only
    states / counts / ids / commit hashes / timestamps.

Structured exit codes (``status``/``health`` return the code matching the observed state):
    0 = healthy      1 = degraded      2 = stopped / parked      3 = unauthorized      4 = failed
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import socket
import sys
import time
from typing import Callable


def _login_name() -> str:
    """The real login user of THIS process, from the password DB by uid — NOT getpass.getuser(), which
    trusts $LOGNAME/$USER first and so lets `LOGNAME=ceo brucectl pause-outbound` forge the audit actor."""
    return pwd.getpwuid(os.getuid()).pw_name

# --------------------------------------------------------------------------- structured exit codes

EXIT_HEALTHY = 0        # supervisor running, relay live, nothing wrong
EXIT_DEGRADED = 1       # running but something is off (stale status, backoff, api down, paused, incompat)
EXIT_STOPPED = 2        # supervisor stopped (no/exited status) or PARKED (authenticated stop / revoked)
EXIT_UNAUTHORIZED = 3   # wrong relay account / lock-owner (host/user) mismatch — refused before acting
EXIT_FAILED = 4         # the requested action failed (bad commit, incompatible rollback, subprocess error)


# --------------------------------------------------------------------------- status file (A3 compatibility)


def _status_path() -> str:
    state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
    return os.path.join(state, "supervisor-status.json")


def read_status(path: str | None = None) -> dict | None:
    """Read the supervisor's content-free health status file (or None if missing/corrupt)."""
    path = path or _status_path()
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# fields we surface — an explicit allowlist so a status file can never leak an unexpected field.
_FIELDS = ("state", "park_reason", "pinned_commit", "uptime_s", "restart_count", "relay_pid",
           "relay_pgid", "updated_at")

# a status older than this (server clock skew aside) is treated as STALE (supervisor not writing).
STALE_AFTER_S = 60.0


def format_status(status: dict | None, *, now: float | None = None) -> str:
    """Legacy one-block formatter kept for A3 back-compat (the supervisor test + older callers)."""
    if status is None:
        return "supervisor: NO STATUS (not running or state dir unset)"
    now = time.time() if now is None else now
    age = now - float(status.get("updated_at", 0) or 0)
    stale = age > STALE_AFTER_S
    lines = [f"supervisor: {status.get('state', '?')}" + ("  [STALE]" if stale else "")]
    for k in _FIELDS:
        if k in status and k != "state":
            lines.append(f"  {k} = {status[k]}")
    lines.append(f"  status_age_s = {round(age, 1)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- content-free snapshots


@dataclasses.dataclass(frozen=True)
class DbSnapshot:
    """Content-free control-plane read: the global outbound switch + the outbound queue state counts.
    ``queue_counts`` maps a message STATUS label (pending/leased/sending/sent/…) to a count — never any
    message content, handle, or recipient. ``reason_set`` is a boolean only; the free-text reason is
    deliberately NOT surfaced by brucectl (it lives in the audited control-plane trail)."""

    outbound_paused: bool
    reason_set: bool
    directive: str            # effective global directive: run | pause_outbound
    queue_counts: dict[str, int]


@dataclasses.dataclass
class Health:
    """Aggregated, content-free health readout across every source (degrading to 'unknown' per source)."""

    status: dict | None
    stale: bool
    supervisor_state: str
    pinned_commit: str
    relay_pid: object
    restart_count: object
    uptime_s: object
    status_age_s: float | None
    api_ready: bool | None            # True/False, or None when not probed / unknown
    db: DbSnapshot | None             # None when the DB is not configured / unreachable (unknown)
    compat_blocked: list[str]
    compat_migrate: list[str]
    compat_known: bool
    lock_host: str | None
    unauthorized: str | None          # reason string when the ownership guard rejects, else None
    issues: list[str]

    @property
    def label(self) -> str:
        if self.unauthorized:
            return "UNAUTHORIZED"
        if self.status is None or self.supervisor_state == "exited":
            return "STOPPED"
        if self.supervisor_state == "parked":
            return "PARKED"
        return "DEGRADED" if self.issues else "HEALTHY"

    @property
    def exit_code(self) -> int:
        if self.unauthorized:
            return EXIT_UNAUTHORIZED
        if self.status is None or self.supervisor_state == "exited":
            return EXIT_STOPPED
        if self.supervisor_state == "parked":
            return EXIT_STOPPED
        return EXIT_DEGRADED if self.issues else EXIT_HEALTHY


# --------------------------------------------------------------------------- real default side effects


def _default_run_launchctl(cmd: list[str]) -> int:
    import subprocess
    try:
        return subprocess.run(cmd, check=False).returncode
    except OSError:
        return 1


def _default_probe_api(base_url: str | None) -> bool | None:
    """HTTP readiness probe of the API. Returns True (ready), False (reachable-but-not-ok / unreachable),
    or None when no base URL is configured (unknown). NEVER reads or prints the response body."""
    if not base_url:
        return None
    import urllib.error
    import urllib.request
    for path in ("/healthz", "/"):
        url = base_url.rstrip("/") + path
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:   # body intentionally never read
                if 200 <= getattr(resp, "status", 200) < 400:
                    return True
        except Exception:
            continue
    return False


async def _read_db_async() -> DbSnapshot:
    from sqlalchemy import func, select

    from bruce_engine import relay_control, schema
    from bruce_engine.db import worker_session

    paused, reason = await relay_control.global_state()
    async with worker_session() as s:
        rows = (await s.execute(
            select(schema.OutboundMessageRow.status, func.count())
            .group_by(schema.OutboundMessageRow.status))).all()
    counts = {str(st): int(n) for st, n in rows}   # STATUS labels only — content-free
    directive = relay_control.PAUSE_OUTBOUND if paused else relay_control.RUN
    return DbSnapshot(outbound_paused=bool(paused), reason_set=bool(reason),
                      directive=directive, queue_counts=counts)


def _default_read_db() -> DbSnapshot | None:
    """Content-free control-plane read, or None (unknown) when the DB is unconfigured/unreachable."""
    if not os.environ.get("BRUCE_APP_DATABASE_URL"):
        return None
    try:
        import asyncio
        return asyncio.run(_read_db_async())
    except Exception:
        return None                      # degrade gracefully: an unavailable source reports 'unknown'


def _default_read_lock_owner(lock_path: str) -> dict | None:
    """The supervisor single-instance lock carries {pid,start,host} (never a secret). None if absent."""
    try:
        with open(lock_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _default_tail_log(path: str, n: int) -> list[str]:
    """Last ``n`` lines of the (already content-free) supervisor log; [] if absent/unreadable."""
    try:
        with open(path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in lines[-max(0, n):]]


def _default_version_present(install_dir: str, commit: str) -> bool:
    return os.path.isdir(os.path.join(install_dir, "versions", commit))


def _default_poll_health(state_dir: str) -> bool:
    from . import installer
    return installer._poll_health(state_dir)


def _default_pause(reason: str | None, actor: str) -> None:
    import asyncio

    from bruce_engine import relay_control
    asyncio.run(relay_control.pause_all(reason=reason, actor=actor))


def _default_resume(actor: str) -> None:
    import asyncio

    from bruce_engine import relay_control
    asyncio.run(relay_control.resume_all(actor=actor))


# --------------------------------------------------------------------------- injectable dependencies


@dataclasses.dataclass
class Deps:
    """Every path + side effect the CLI touches, injected so the whole surface is deterministically
    testable without a live supervisor, a Mac, launchd, or Postgres. ``build_real_deps`` wires production."""

    state_dir: str
    install_dir: str
    status_path: str
    log_path: str
    lock_path: str
    home: str
    uid: int
    api_base_url: str | None
    python: str | None
    now: Callable[[], float] = time.time
    read_status: Callable[[str], "dict | None"] = read_status
    whoami: Callable[[], str] = _login_name    # pwd-by-uid, not env-spoofable (audit-actor integrity)
    hostname: Callable[[], str] = socket.gethostname
    run_cmd: Callable[[list[str]], int] = _default_run_launchctl
    probe_api: Callable[["str | None"], "bool | None"] = _default_probe_api
    read_db: Callable[[], "DbSnapshot | None"] = _default_read_db
    read_lock_owner: Callable[[str], "dict | None"] = _default_read_lock_owner
    read_manifest: Callable[[str], dict] = None            # wired below (state_manifest.read_manifest)
    plan_activation: Callable[[dict], dict] = None         # wired below (state_manifest.plan_activation)
    safe_activate: Callable[..., None] = None              # wired below (state_manifest.safe_activate)
    activate_version: Callable[[str, str], str] = None     # wired below (installer.activate_version)
    version_present: Callable[[str, str], bool] = _default_version_present
    tail_log: Callable[[str, int], list[str]] = _default_tail_log
    poll_health: Callable[[str], bool] = _default_poll_health
    pause_all: Callable[["str | None", str], None] = _default_pause
    resume_all: Callable[[str], None] = _default_resume

    def actor(self) -> str:
        """Server-derived operator identity (shell user@host) — never a client-supplied string."""
        try:
            user = self.whoami()
        except Exception:
            user = "unknown"
        return f"{user}@{self.hostname()}"


def build_real_deps() -> Deps:
    from . import installer, state_manifest
    state = os.environ.get("BRUCE_RELAY_STATE_DIR", os.path.expanduser("~/.bruce-relay"))
    install = os.environ.get("BRUCE_RELAY_INSTALL_DIR", os.path.expanduser("~/.bruce-relay-app"))
    home = os.environ.get("HOME", os.path.expanduser("~"))
    python = None
    cand = os.path.join(install, "current", "engine", ".venv", "bin", "python")
    if os.path.exists(cand):
        python = cand
    d = Deps(
        state_dir=state, install_dir=install,
        status_path=os.path.join(state, "supervisor-status.json"),
        log_path=os.path.join(state, "supervisor.log"),
        lock_path=os.path.join(state, "supervisor.lock"),
        home=home, uid=os.getuid(),
        api_base_url=os.environ.get("BRUCE_API_BASE_URL"), python=python,
    )
    d.read_manifest = state_manifest.read_manifest
    d.plan_activation = state_manifest.plan_activation
    d.safe_activate = state_manifest.safe_activate
    d.activate_version = installer.activate_version
    return d


# --------------------------------------------------------------------------- ownership guard


def authorize(deps: Deps) -> str | None:
    """Verify this invocation runs as the expected relay account and against the local supervisor. Returns
    None when authorized, else a content-free reason string (the caller maps that to exit 3). Checks:
      * ``BRUCE_RELAY_EXPECT_USER`` (if set) must equal the current login user;
      * the supervisor lock's recorded host must match this host (a lock from another Mac -> refuse);
      * the durable state dir must be owned by the current uid.
    """
    expect = os.environ.get("BRUCE_RELAY_EXPECT_USER")
    try:
        user = deps.whoami()
    except Exception:
        user = None
    if expect and user is not None and expect != user:
        return f"running as '{user}', expected relay account '{expect}'"
    owner = deps.read_lock_owner(deps.lock_path)
    if owner is not None:
        host = owner.get("host")
        if host and host != deps.hostname():
            return "supervisor lock is owned by a different host"
    try:
        st = os.stat(deps.state_dir)
        if st.st_uid != deps.uid:
            return "relay state dir is owned by a different user"
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- aggregation


def aggregate(deps: Deps, *, check_auth: bool = True) -> Health:
    """Read every source (status file, DB control plane, API probe, state manifest, ownership) and build a
    content-free Health readout. Any unavailable source degrades to 'unknown' rather than crashing."""
    now = deps.now()
    status = deps.read_status(deps.status_path)
    state = str((status or {}).get("state", "unknown"))
    age = None if status is None else now - float((status or {}).get("updated_at", 0) or 0)
    stale = bool(age is not None and age > STALE_AFTER_S)

    api_ready = deps.probe_api(deps.api_base_url)
    db = deps.read_db()

    compat_blocked: list[str] = []
    compat_migrate: list[str] = []
    compat_known = False
    try:
        plan = deps.plan_activation(deps.read_manifest(deps.state_dir))
        compat_blocked = list(plan.get("blocked", []))
        compat_migrate = list(plan.get("migrate", []))
        compat_known = True
    except Exception:
        compat_known = False

    owner = deps.read_lock_owner(deps.lock_path)
    lock_host = owner.get("host") if isinstance(owner, dict) else None

    unauthorized = authorize(deps) if check_auth else None

    issues: list[str] = []
    if status is not None:
        if stale:
            issues.append("supervisor status is stale (supervisor may not be writing)")
        if state == "backoff":
            issues.append("relay is restarting under backoff")
        if state == "duplicate":
            issues.append("another supervisor owns the lock")
        if state == "starting":
            issues.append("supervisor is still starting")
        if state == "running" and (status or {}).get("relay_pid") is None:
            issues.append("supervisor running but no relay pid recorded")
    if api_ready is False:
        issues.append("API readiness probe failed")
    if db is not None and db.outbound_paused:
        issues.append("outbound sending is paused")
    if compat_blocked:
        issues.append("on-disk durable state is incompatible with the pinned code")

    return Health(
        status=status, stale=stale, supervisor_state=state,
        pinned_commit=str((status or {}).get("pinned_commit", "unknown")),
        relay_pid=(status or {}).get("relay_pid"),
        restart_count=(status or {}).get("restart_count"),
        uptime_s=(status or {}).get("uptime_s"),
        status_age_s=None if age is None else round(age, 1),
        api_ready=api_ready, db=db,
        compat_blocked=compat_blocked, compat_migrate=compat_migrate, compat_known=compat_known,
        lock_host=lock_host, unauthorized=unauthorized, issues=issues)


# --------------------------------------------------------------------------- formatting (content-free)


def _api_word(v: bool | None) -> str:
    return "unknown" if v is None else ("ready" if v else "NOT ready")


def _health_dict(h: Health) -> dict:
    """A content-free dict for --json (allowlisted fields only)."""
    d = {
        "verdict": h.label,
        "exit_code": h.exit_code,
        "supervisor_state": h.supervisor_state,
        "stale": h.stale,
        "status_age_s": h.status_age_s,
        "pinned_commit": h.pinned_commit,
        "relay_pid": h.relay_pid,
        "restart_count": h.restart_count,
        "uptime_s": h.uptime_s,
        "api_ready": h.api_ready,
        "state_compat": ("unknown" if not h.compat_known
                         else ("blocked" if h.compat_blocked
                               else ("migrate_needed" if h.compat_migrate else "ok"))),
        "issues": h.issues,
    }
    if h.db is None:
        d["control_plane"] = "unknown"
    else:
        d["control_plane"] = {
            "directive": h.db.directive,
            "outbound_paused": h.db.outbound_paused,
            "reason_set": h.db.reason_set,
            "queue_counts": h.db.queue_counts,
        }
    if h.unauthorized:
        d["unauthorized"] = h.unauthorized
    return d


def _format_status(h: Health, *, verbose: bool) -> str:
    lines = [f"verdict: {h.label}  (exit {h.exit_code})"]
    if h.unauthorized:
        lines.append(f"  ownership: REFUSED — {h.unauthorized}")
    lines.append(f"  supervisor: {h.supervisor_state}" + ("  [STALE]" if h.stale else ""))
    lines.append(f"    pinned_commit = {h.pinned_commit}")
    lines.append(f"    relay_pid = {h.relay_pid}   restart_count = {h.restart_count}   "
                 f"uptime_s = {h.uptime_s}")
    if h.status_age_s is not None:
        lines.append(f"    status_age_s = {h.status_age_s}")
    lines.append(f"  api: {_api_word(h.api_ready)}")
    if h.db is None:
        lines.append("  control-plane: unknown (DB not configured or unreachable)")
    else:
        lines.append(f"  control-plane: directive={h.db.directive} "
                     f"outbound_paused={h.db.outbound_paused} reason_set={h.db.reason_set}")
        if h.db.queue_counts:
            counts = "  ".join(f"{k}={v}" for k, v in sorted(h.db.queue_counts.items()))
            lines.append(f"    outbound_queue: {counts}")
        else:
            lines.append("    outbound_queue: (empty)")
    compat = ("unknown" if not h.compat_known
              else ("BLOCKED " + str(sorted(h.compat_blocked)) if h.compat_blocked
                    else ("migrate-needed " + str(sorted(h.compat_migrate)) if h.compat_migrate else "ok")))
    lines.append(f"  state-compat: {compat}")
    if verbose:
        lines.append(f"  lock_host = {h.lock_host}")
        if h.issues:
            lines.append("  issues:")
            lines.extend(f"    - {i}" for i in h.issues)
        else:
            lines.append("  issues: none")
        lines.extend(_diagnose_hints(h))
    elif h.issues:
        lines.append("  issues: " + "; ".join(h.issues))
    return "\n".join(lines)


def _diagnose_hints(h: Health) -> list[str]:
    hints: list[str] = []
    if h.unauthorized:
        hints.append("  hint: run as the dedicated relay login account on the relay Mac.")
    if h.status is None:
        hints.append("  hint: supervisor not running — `brucectl start` (or check the LaunchAgent).")
    if h.stale:
        hints.append("  hint: status is stale — `brucectl restart` if the supervisor is wedged.")
    if h.api_ready is False:
        hints.append("  hint: API unreachable — inbound/outbound will stall until it recovers.")
    if h.compat_blocked:
        hints.append("  hint: on-disk state is newer than the pinned code — a rollback would be refused.")
    if h.db is not None and h.db.outbound_paused:
        hints.append("  hint: outbound is paused — `brucectl resume-outbound` to resume sending.")
    if not hints:
        hints.append("  hint: none — everything nominal.")
    return hints


# --------------------------------------------------------------------------- commands


def _supervisor_alive(deps: Deps) -> bool:
    """True when a supervisor appears to be running (fresh, non-exited status)."""
    st = deps.read_status(deps.status_path)
    if not st:
        return False
    if st.get("state") == "exited":
        return False
    age = deps.now() - float(st.get("updated_at", 0) or 0)
    return age <= STALE_AFTER_S


def _cmd_status(deps: Deps, args) -> int:
    h = aggregate(deps)
    if getattr(args, "json", False):
        print(json.dumps(_health_dict(h), indent=2))
    else:
        print(_format_status(h, verbose=(args.command == "diagnose")))
    return h.exit_code


def _launchctl(deps: Deps, commands: list[list[str]]) -> int:
    """Run a sequence of launchctl argv through the injected runner. Returns EXIT_FAILED on an OSError-like
    non-zero from the runner for a critical command; bootout failures are tolerated (idempotent)."""
    from . import installer  # noqa: F401  (kept for symmetry; argv built by caller)
    try:
        for cmd in commands:
            deps.run_cmd(cmd)
    except Exception:
        return EXIT_FAILED
    return EXIT_HEALTHY


def _plist_path(deps: Deps) -> str:
    from . import installer
    return installer.launchagent_path(deps.home)


def _cmd_start(deps: Deps) -> int:
    from . import installer
    reason = authorize(deps)
    if reason:
        print(f"refusing: {reason}")
        return EXIT_UNAUTHORIZED
    if _supervisor_alive(deps):
        print("supervisor already running — no-op")
        return EXIT_HEALTHY
    rc = _launchctl(deps, installer.load_argv(_plist_path(deps), uid=deps.uid))
    print("supervisor start requested (LaunchAgent bootstrapped)" if rc == EXIT_HEALTHY
          else "supervisor start FAILED")
    return rc


def _cmd_stop(deps: Deps) -> int:
    from . import installer
    reason = authorize(deps)
    if reason:
        print(f"refusing: {reason}")
        return EXIT_UNAUTHORIZED
    if not _supervisor_alive(deps):
        print("supervisor already stopped — no-op")
        return EXIT_STOPPED
    # bootout the LaunchAgent — launchd sends SIGTERM to the supervisor, which gracefully stops its relay
    # child. We NEVER signal the relay process directly.
    rc = _launchctl(deps, [["launchctl", "bootout", f"gui/{deps.uid}", _plist_path(deps)]])
    print("supervisor stop requested (LaunchAgent booted out)" if rc == EXIT_HEALTHY
          else "supervisor stop FAILED")
    return EXIT_STOPPED if rc == EXIT_HEALTHY else EXIT_FAILED


def _cmd_restart(deps: Deps) -> int:
    from . import installer
    reason = authorize(deps)
    if reason:
        print(f"refusing: {reason}")
        return EXIT_UNAUTHORIZED
    if _supervisor_alive(deps):
        rc = _launchctl(deps, [installer.kickstart_argv(deps.uid)])
        print("supervisor restart requested (launchctl kickstart)" if rc == EXIT_HEALTHY
              else "supervisor restart FAILED")
    else:
        rc = _launchctl(deps, installer.load_argv(_plist_path(deps), uid=deps.uid)
                        + [installer.kickstart_argv(deps.uid)])
        print("supervisor was not running — bootstrapped + kickstarted" if rc == EXIT_HEALTHY
              else "supervisor restart FAILED")
    return rc


def _is_full_sha(commit: str) -> bool:
    return len(commit) == 40 and all(c in "0123456789abcdef" for c in commit.lower())


def _activate_and_reload(deps: Deps, commit: str) -> None:
    """The production ``activate`` callback for safe_activate: flip ``current`` atomically to the target,
    re-render the SECRET-FREE plist pinned to the new commit, and reload the LaunchAgent so the supervisor
    re-execs the relay from the new code. Reuses the installer's tested primitives — no raw process work,
    no state wipe."""
    from . import installer
    deps.activate_version(deps.install_dir, commit)
    dest = _plist_path(deps)
    if deps.api_base_url and deps.python:
        engine_dir = os.path.join(deps.install_dir, "current", "engine")
        plist = installer.render_plist(python=deps.python, engine_dir=engine_dir, state_dir=deps.state_dir,
                                       api_base_url=deps.api_base_url, pinned_commit=commit)
        installer.write_plist(dest, plist)
    for cmd in installer.load_argv(dest, uid=deps.uid) + [installer.kickstart_argv(deps.uid)]:
        deps.run_cmd(cmd)


def _activate_command(deps: Deps, commit: str, *, action: str) -> int:
    """Shared body of update + rollback: ownership-guard, verify the EXACT commit is checked out, then
    delegate to ``safe_activate`` (compat-gated, atomic, auto-restoring, never wipes durable state). An
    incompatible rollback is REFUSED (IncompatibleRollback -> exit 4)."""
    from . import state_manifest
    reason = authorize(deps)
    if reason:
        print(f"refusing: {reason}")
        return EXIT_UNAUTHORIZED
    if not _is_full_sha(commit):
        print("error: pass the EXACT 40-char commit sha (a ref/branch/short sha is refused)")
        return EXIT_FAILED
    if not deps.version_present(deps.install_dir, commit):
        print(f"error: commit {commit[:12]} is not checked out under versions/ (install it first)")
        return EXIT_FAILED
    # friendly pre-check (safe_activate is the authoritative gate).
    try:
        plan = deps.plan_activation(deps.read_manifest(deps.state_dir))
        if plan.get("blocked"):
            print(f"error: {action} refused — on-disk durable state is newer than {commit[:12]} supports "
                  f"({sorted(plan['blocked'])})")
            return EXIT_FAILED
    except Exception:
        pass
    try:
        deps.safe_activate(install_dir=deps.install_dir, state_dir=deps.state_dir, commit=commit,
                           activate=lambda c: _activate_and_reload(deps, c),
                           health_check=lambda: deps.poll_health(deps.state_dir))
    except state_manifest.IncompatibleRollback:
        print(f"error: {action} refused — incompatible rollback (durable state newer than target); "
              f"durable state left intact")
        return EXIT_FAILED
    except state_manifest.ActivationFailed:
        print(f"error: {action} failed post-activation — prior version + state restored")
        return EXIT_FAILED
    except Exception:
        print(f"error: {action} failed (see supervisor status); durable state not wiped")
        return EXIT_FAILED
    print(f"{action} to {commit[:12]} complete — durable state preserved, health verified")
    return EXIT_HEALTHY


def _cmd_pause(deps: Deps, reason: str | None) -> int:
    result = authorize(deps)
    if result:
        print(f"refusing: {result}")
        return EXIT_UNAUTHORIZED
    if not os.environ.get("BRUCE_APP_DATABASE_URL"):
        print("error: BRUCE_APP_DATABASE_URL is not set (cannot reach the control plane)")
        return EXIT_FAILED
    actor = deps.actor()
    try:
        deps.pause_all(reason, actor)
    except Exception:
        print("error: pause-outbound failed")
        return EXIT_FAILED
    # deliberately do NOT echo the reason free-text (content-free output; it is in the audit trail).
    print(f"global outbound PAUSED (audited; actor={actor})")
    return EXIT_HEALTHY


def _cmd_resume(deps: Deps) -> int:
    result = authorize(deps)
    if result:
        print(f"refusing: {result}")
        return EXIT_UNAUTHORIZED
    if not os.environ.get("BRUCE_APP_DATABASE_URL"):
        print("error: BRUCE_APP_DATABASE_URL is not set (cannot reach the control plane)")
        return EXIT_FAILED
    actor = deps.actor()
    try:
        deps.resume_all(actor)
    except Exception:
        print("error: resume-outbound failed")
        return EXIT_FAILED
    print(f"global outbound RESUMED (audited; actor={actor})")
    return EXIT_HEALTHY


# hard cap on log lines so `logs` is always bounded regardless of the requested N.
LOGS_MAX_LINES = 500


def _cmd_logs(deps: Deps, lines: int) -> int:
    n = max(1, min(int(lines), LOGS_MAX_LINES))
    tail = deps.tail_log(deps.log_path, n)
    if not tail:
        print("(no supervisor log yet)")
        return EXIT_HEALTHY
    print(f"# supervisor.log (last {len(tail)} lines; content-free)")
    for ln in tail:
        print(ln)
    return EXIT_HEALTHY


# --------------------------------------------------------------------------- CLI wiring


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce relay operator CLI (supervisor + control plane).")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="content-free health readout + structured exit code")
    st.add_argument("--json", action="store_true", help="raw JSON (still content-free)")
    he = sub.add_parser("health", help="same as status (health verdict + exit code)")
    he.add_argument("--json", action="store_true", help="raw JSON (still content-free)")
    sub.add_parser("diagnose", help="verbose health readout with remediation hints")

    sub.add_parser("start", help="start the supervisor via the LaunchAgent (idempotent)")
    sub.add_parser("stop", help="stop the supervisor via the LaunchAgent (idempotent)")
    sub.add_parser("restart", help="restart the supervisor in place (launchctl kickstart)")

    up = sub.add_parser("update", help="activate an APPROVED pinned commit (compat-gated, atomic)")
    up.add_argument("--commit", required=True, help="the EXACT 40-char approved commit sha")
    rb = sub.add_parser("rollback", help="roll back to a prior pinned commit (blocks incompatible state)")
    rb.add_argument("--commit", required=True, help="the EXACT 40-char prior commit sha")

    po = sub.add_parser("pause-outbound", help="pause the GLOBAL outbound claim gate (audited)")
    po.add_argument("--reason", default=None, help="audited reason (never echoed to stdout)")
    sub.add_parser("resume-outbound", help="resume the GLOBAL outbound claim gate (audited)")

    lg = sub.add_parser("logs", help="tail the (content-free) supervisor log, bounded")
    lg.add_argument("--lines", type=int, default=50, help=f"lines to show (max {LOGS_MAX_LINES})")
    return p


def dispatch(deps: Deps, args) -> int:
    cmd = args.command
    if cmd in ("status", "health", "diagnose"):
        return _cmd_status(deps, args)
    if cmd == "start":
        return _cmd_start(deps)
    if cmd == "stop":
        return _cmd_stop(deps)
    if cmd == "restart":
        return _cmd_restart(deps)
    if cmd == "update":
        return _activate_command(deps, args.commit, action="update")
    if cmd == "rollback":
        return _activate_command(deps, args.commit, action="rollback")
    if cmd == "pause-outbound":
        return _cmd_pause(deps, args.reason)
    if cmd == "resume-outbound":
        return _cmd_resume(deps)
    if cmd == "logs":
        return _cmd_logs(deps, args.lines)
    return EXIT_FAILED


def main(argv: list[str] | None = None, *, deps: Deps | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return dispatch(deps or build_real_deps(), args)


if __name__ == "__main__":
    sys.exit(main())
