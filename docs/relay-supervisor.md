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
STARTING ──lock ok──▶ RUNNING ──child exits──▶ ┌ code 0  ─▶ PARKED   (clean stop directive)
   │  lock held                                 ├ code 78 ─▶ PARKED   (revoked/invalid credential)
   └─ DUPLICATE (refuse)                         └ crash   ─▶ BACKOFF ─(sleep)─▶ RUNNING (restart)
RUNNING/BACKOFF ──SIGTERM/SIGINT──▶ STOPPING ─(terminate+reap child)─▶ EXITED
```

- **PARKED** is intentional and sticky: a stop directive (relay exits 0) or a revoked credential (exit
  **78**, EX_CONFIG) parks the relay **without restarting** — no restart-loop.
- **DUPLICATE**: a second supervisor cannot acquire the single-instance lock and refuses to start.

## Restart policy

Bounded exponential backoff with jitter: `min(base·2^(n-1), max) ± jitter`. `n` (consecutive failures)
**resets to 0** after the relay ran healthy for `stability_window_s`, so an occasional crash doesn't
inflate the backoff, but a hot crash-loop backs off up to `max_backoff_s`. Defaults: base 1 s, max 60 s,
jitter 20%, stability window 30 s. launchd's own `ThrottleInterval` (10 s) is the outer floor.

## Single-instance ownership

`flock(LOCK_EX | LOCK_NB)` on `supervisor.lock`. A **live** holder → the new supervisor is rejected
(duplicate). A holder that **died** has its flock auto-released by the OS → the new supervisor recovers
the stale lock. The lock file also carries `{pid, start, host}` for observability (never a secret). On
start the supervisor also reaps a **stale relay child** whose pid the last status recorded but a crashed
supervisor never reaped.

## Durable state & pinned commit

The supervisor **never** wipes durable state — `checkpoint.json` (inbound dedup), `outbound_sent.json`
(delivery-phase ledger), `pending_attachments.json` (delayed HEIC/attachments) all survive restarts
untouched. It runs a **pinned approved commit** (`BRUCE_RELAY_PINNED_COMMIT`) and does **no automatic git
pull**; update/rollback hooks are reserved for **B1**.

## Health & ops

- Content-free rotating logs (`supervisor.log`, `RotatingFileHandler`): pids / states / counts only —
  never message content, handles, attachment paths, or secrets.
- Health status (`supervisor-status.json`, atomic): `state`, `pinned_commit`, `uptime_s`,
  `restart_count`, `relay_pid`. Read it with **brucectl**:

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
rejected · stale lock recovered · stale relay child reaped · child reaped on stop · stop directive doesn't
restart-loop · exit-78 doesn't restart-loop · network-outage recovery · API cold-start recovery · graceful
shutdown · launchd-style restart · checkpoint/ledger/pending-HEIC survive restart · logs rotate · logs
carry no private content · pinned commit reported.

## Not in A3 (next)

**A4** — the Mac installer: provision the LaunchAgent + state dir, register the device (`register_relay_
device.py`), store the secret in the Keychain, substitute the plist template, and run the dedicated-Mac
dry-run that finally verifies live iMessage (still **UNVERIFIED** until then).
