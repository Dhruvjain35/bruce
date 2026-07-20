# Relay supervisor (Bite 1.5 A3)

Keeps the self-hosted iMessage relay alive without babysitting Terminal — a two-tier supervisor between
launchd and the relay.

## Process tree

```
launchd (LaunchAgent, KeepAlive)
  └─ supervisor            relay.supervisor — single-instance owner, restart policy, health status
       └─ relay            python -m relay — one process (inbound watch + outbound send loops)
            └─ imsg        one watch child + short-lived one-shot send children (reaped by the relay)
```

launchd is the outer tier (relaunches the **supervisor** if the whole process dies, throttled). The
supervisor is the inner tier: it owns **exactly one** relay child and does the fine-grained restart
backoff. The relay reaps its own imsg subtree on stop (`Relay.aclose`).

## State machine

```
STARTING ─lock ok─▶ RUNNING ─child exits─▶ ┌ code 75 ─▶ PARKED  (authenticated stop)
   │ lock held                             ├ code 78 ─▶ PARKED  (revoked/invalid credential)
   └ DUPLICATE (refuse)                     └ else (incl. 0) ─▶ BACKOFF ─(sleep)─▶ RUNNING (restart)
PARKED ─(poll authenticated control plane)─▶ directive != stop ─▶ RUNNING (resume: spawn ONE relay)
RUNNING/BACKOFF/PARKED ─SIGTERM/SIGINT─▶ STOPPING ─(terminate+reap owned group)─▶ EXITED
```

- **Exit codes decide restart vs park.** Only an **authenticated stop** (relay exits **75**) or a
  **revoked/invalid credential** (exit **78**, EX_CONFIG) parks. **Any other exit — including an
  accidental clean exit `0` — RESTARTS** (a clean-but-unintended exit must not silently park).
- **PARKED stays ALIVE and resumable.** The supervisor does not exit; it polls the *authenticated*
  control plane (the same device-authenticated directive the relay uses) on a bounded interval and
  **resumes — spawning exactly one relay child — when the directive is no longer `stop`** (a `run` or
  `pause_outbound` directive resumes it; `pause_outbound` is handled inside the relay). Resume
  reinstalls nothing, re-registers nothing, kills nothing. A revoked (78) park stays parked until the
  credential authenticates again (e.g. after re-registration) — never a restart-thrash.
- **DUPLICATE**: a second supervisor cannot acquire the single-instance lock and refuses to start.

Because PARKED stays alive and the backoff is bounded, **neither launchd KeepAlive** (which only
relaunches the whole supervisor if it truly dies) **nor the internal backoff can create a restart loop**.

## Restart policy

Bounded exponential backoff with jitter: `min(base·2^(n-1), max) ± jitter`. `n` (consecutive failures)
**resets to 0** after the relay ran healthy for `stability_window_s`, so an occasional crash doesn't
inflate the backoff, but a hot crash-loop backs off up to `max_backoff_s`. Defaults: base 1 s, max 60 s,
jitter 20%, stability window 30 s. launchd's own `ThrottleInterval` (10 s) is the outer floor.

## Single-instance ownership & safe reaping

`flock(LOCK_EX | LOCK_NB)` on `supervisor.lock`. A **live** holder → the new supervisor is rejected
(duplicate); a holder that **died** has its flock auto-released → the new supervisor recovers the stale
lock. The relay is spawned in its **own session/process group** (`start_new_session`), so the supervisor
signals **only the owned group** (`killpg`) — the relay *and* its imsg children, nothing else. On start it
reaps a **stale relay child** left by a crashed supervisor, but **safely against PID reuse**: it kills the
recorded group only when the process **start-token still matches** (or, absent a token, the pid is still a
session leader) — a reused pid is **never** killed. One supervisor + one owned group ⇒ **duplicate
supervisors and duplicate watchers are impossible**.

## Durable state & pinned commit

The supervisor **never** wipes durable state — `checkpoint.json` (inbound dedup), `outbound_sent.json`
(delivery-phase ledger), `pending_attachments.json` (delayed HEIC/attachments) all survive restarts
untouched. It runs a **pinned approved commit** (`BRUCE_RELAY_PINNED_COMMIT`) and does **no automatic git
pull**; update/rollback hooks are reserved for **B1**.

## Health & ops

- Content-free rotating logs (`supervisor.log`, `RotatingFileHandler`): pids / states / counts only —
  never message content, handles, attachment paths, or secrets.
- Health status (`supervisor-status.json`, atomic): `state`, `park_reason`, `pinned_commit`, `uptime_s`,
  `restart_count`, `relay_pid`, `relay_pgid`. Read it with **brucectl**:

  ```
  python -m relay.brucectl status          # human-readable (flags STALE)
  python -m relay.brucectl status --json    # raw, still content-free
  ```

- Graceful shutdown on SIGTERM/SIGINT: terminate + reap the relay child, release the lock, exit.
- LaunchAgent template: `relay/launchd/com.bruce.relay.supervisor.plist` (the A4 installer substitutes
  `@PYTHON@ / @ENGINE_DIR@ / @STATE_DIR@` and loads it). The device secret stays in the Keychain — never
  in the plist or in argv.

## Tested (`tests/test_relay_supervisor.py`, 20 scenarios)

normal start · crash+restart · repeated-crash backoff grows · stability window resets backoff · duplicate
rejected (lock + run) · stale lock recovered · stale relay child group-reaped · **stale PID reuse NOT
reaped** · child reaped on stop · **accidental exit-0 RESTARTS (not park)** · authenticated stop (75)
parks-alive-no-restart · revoked (78) parks-no-thrash · **PARKED polls + resumes exactly one child** ·
resume on `pause_outbound` · network-outage recovery · graceful shutdown · launchd-style restart ·
checkpoint/ledger/pending-HEIC survive park→resume→shutdown · logs rotate · logs carry no private content ·
pinned commit reported · **process-group reap kills the whole owned group** (real subprocess).

## Not in A3 (next)

**A4** — the Mac installer: provision the LaunchAgent + state dir, register the device (`register_relay_
device.py`), store the secret in the Keychain, substitute the plist template, and run the dedicated-Mac
dry-run that finally verifies live iMessage (still **UNVERIFIED** until then).
