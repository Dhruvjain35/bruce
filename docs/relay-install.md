# Relay Mac install / upgrade / rollback (Bite 1.5 A4)

One-time install of the self-hosted iMessage relay + supervisor on the dedicated Mac. After it, there is
**no manual relay-start command** — the supervisor and relay start at login and are controlled remotely.

## Install (run once)

Under the dedicated **`bruce-relay` graphical login** (a real desktop session — not ssh, not root),
from a checkout of this repo at the approved build:

```
./install_relay.sh --commit <approved_sha> --api-base-url https://<bruce-api-host>
```

Then, to test, just **open Messages on that Mac and text Bruce** — nothing else to run.

## Installer flow (`install_relay.sh` → `relay.installer`)

1. **Preconditions**: macOS only, not root, `--commit` present and resolvable in this repo, `--api-base-url`
   present. No network fetch — it only uses commits already in the local repo.
2. **Pin the code**: `git archive <approved_sha> | tar -x` into `~/.bruce-relay-app/versions/<sha>` — a
   detached snapshot of the *exact* approved commit. **No `git pull`, no moving ref.**
3. **Keychain**: if the device secret isn't already stored, `security add-generic-password` runs
   **interactively** (no `-w`), so the operator pastes the one-time secret at a hidden prompt and it is
   **never** placed in argv, env, or logs. `config.py` reads it from the Keychain at runtime.
4. **State + version + plist** (all in the tested Python core):
   - `ensure_state_dir` creates `~/.bruce-relay` (0700) + `spool/` (0700), **never wiping** existing
     durable files;
   - `activate_version` flips `~/.bruce-relay-app/current` → `versions/<sha>` (atomic symlink);
   - `render_plist` fills the LaunchAgent template (asserting it is **secret-free**) and writes it (0644)
     to `~/Library/LaunchAgents/com.bruce.relay.supervisor.plist`;
   - `launchctl bootout`/`bootstrap`/`enable` (+ `kickstart` on re-run) (re)loads it for the GUI session.

`--dry-run` prints every step and writes nothing.

## LaunchAgent lifecycle

- `RunAtLoad` + `KeepAlive{SuccessfulExit:false}`: launchd starts the **supervisor** at login and
  relaunches it only if the whole process dies (throttled 10 s). The supervisor owns the relay child and
  does the fine-grained restart/park/resume (see `docs/relay-supervisor.md`).
- **Stop/resume remotely**: an authenticated `stop` directive parks the supervisor (stays alive, polls);
  clearing it resumes — no reinstall, no login needed.
- Graceful shutdown on `launchctl bootout` (SIGTERM → the supervisor terminates + reaps the owned group).

## Keychain handling

- Service `com.bruce.relay.device-secret`, account `--account` (default `default`) — matches
  `relay/config.py`. Stored via the interactive `security` prompt; `-U` updates in place on re-install.
- The secret never appears in the plist, in argv (`security` is called without `-w`), in the environment,
  or in any log. The relay/​supervisor read it only through the Keychain.

## Directory permissions

| Path | Mode | Notes |
| --- | --- | --- |
| `~/.bruce-relay` (state dir) | `0700` | durable: checkpoint / outbound ledger / pending attachments / logs |
| `~/.bruce-relay/spool` | `0700` | transient attachment spool |
| `~/.bruce-relay-app/versions/<sha>` | inherited | pinned code snapshots |
| `~/.bruce-relay-app/current` | symlink | → the active version |
| `~/Library/LaunchAgents/…plist` | `0644` | secret-free (holds only paths / a URL / a commit) |

## Upgrade & rollback (durable state preserved)

- **Upgrade**: re-run `install_relay.sh --commit <new_sha>` → exports the new snapshot, flips `current`,
  repoints the plist's `BRUCE_RELAY_PINNED_COMMIT`, and `kickstart`s the supervisor. The state dir is
  **untouched** — checkpoint, delivery-phase ledger, and pending HEIC all carry over.
- **Rollback**: re-run with a previously-installed `<sha>` → flips `current` back and `kickstart`s. Same
  durable state. Both are a symlink swap + reload; nothing is wiped. (Automated update/rollback *hooks*
  are reserved for **B1**; A4 is the manual, safe primitive.)

Proven by `tests/test_relay_installer.py` (render secret-free, state 0700 + no-wipe, activate
upgrade→rollback, secret-free Keychain/launchctl argv, end-to-end install→upgrade→rollback preserving
durable state, dry-run writes nothing).

## Exact Mac-only steps still needing on-device approval

These cannot run in CI and require the dedicated Mac + the `bruce-relay` login:

1. **Device registration** (operator, DB side): `python -m scripts.register_relay_device "<name>"` to mint
   the one-time device secret (server keeps only its hash).
2. **Keychain write**: paste that secret at the `security add-generic-password` prompt during install.
3. **LaunchAgent load**: `launchctl bootstrap gui/<uid>` — requires a real GUI login session.
4. **Full-Disk-Access / Automation TCC** grants for the `imsg` binary + Messages (macOS privacy prompts).
5. **The dedicated-Mac dry-run** that finally verifies **live iMessage** end-to-end (send + receive) —
   the relay's live behavior remains **UNVERIFIED** until this passes.
