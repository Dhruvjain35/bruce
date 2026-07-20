# Relay Mac install / upgrade / rollback (Bite 1.5 A4)

One-time install of the self-hosted iMessage relay + supervisor on the dedicated Mac. After it, there is
**no manual relay-start command** — the supervisor and relay start at login and are controlled remotely.

## Install (one command)

Under the dedicated **`bruce-relay` graphical login** (a real desktop session — not ssh, not root), an
operator first mints a **short-lived, single-use bootstrap token** (the one authorized operator step),
then runs the installer once:

```
# operator, DB side (mints the temporary bootstrap material — NOT the permanent credential):
BRUCE_ENV=<env> BRUCE_APP_DATABASE_URL=... python -m scripts.relay_bootstrap mint --device mac-alpha --ttl 600

# on the Mac, once:
BRUCE_RELAY_BOOTSTRAP_TOKEN=<minted token> \
  ./install_relay.sh --commit <exact_sha> --api-base-url https://<bruce-api-host> --device mac-alpha
```

The installer OWNS the credential bootstrap — the operator never sees or pastes the permanent credential.
Then, to test, just **open Messages on that Mac and text Bruce**.

## Installer flow (`install_relay.sh` → `relay.installer` / `relay.bootstrap`)

1. **Preconditions**: macOS only; not root; **exact 40-char commit SHA** (a ref/branch is rejected); the
   working tree must be **clean** (dirty artifacts rejected); an **active GUI login** (`launchctl print
   gui/<uid>`); optional `BRUCE_RELAY_EXPECT_USER` wrong-account guard; an **API readiness** probe (body
   never printed).
2. **Pin the code**: `git archive <exact_sha> | tar -x` into `~/.bruce-relay-app/versions/<sha>` — a
   detached snapshot of that exact commit (**no `git pull`, no moving ref**). Extraction is verified safe
   (`verify_extracted_safe` rejects a path-traversal / a symlink escaping the version dir).
3. **Secure credential bootstrap** (`python -m relay.bootstrap`, GAP 1): with the single-use bootstrap
   token it registers the device over TLS, receives the permanent credential **in process memory**, writes
   it straight into the login Keychain via **Security.framework** (`SecItemAdd`), **verifies** it
   authenticates, and **self-revokes** on any failure so no active orphan credential remains. The permanent
   credential is never displayed, pasted, written to disk, placed in argv/env, or logged.
4. **Compatibility-checked activation** (`safe_activate`, GAP 2): back up durable state, run any required
   forward migrations, flip `~/.bruce-relay-app/current` → `versions/<sha>` (**atomic** symlink), write the
   **secret-free, absolute-path, no-shell** plist (0644), (re)load the LaunchAgent, and **verify health** —
   restoring the prior version + state if activation or health fails. An **incompatible rollback** (on-disk
   state newer than the target supports) is **blocked**. Durable state is **never wiped**.

`--dry-run` prints every step and writes nothing.

## Registration threat model (GAP 1)

| Property | How |
| --- | --- |
| authenticated operator authority | the bootstrap token is minted only via the DB-direct operator CLI (`scripts.relay_bootstrap`, worker session) |
| short-lived + single-use | token has a TTL and `max_uses=1`; consumed on first success — a replay is denied |
| bound to environment + device | token carries `(environment, device_name)`; a mismatch is denied |
| replay fails | consumed/expired/unknown token → `403`, generic reason, no secret |
| rate limited | ≤ N register attempts per env per window (bad attempts count) |
| no silent rebind | a same-named device is **rotated**, not duplicated; registration is explicit (needs a fresh token) |
| idempotent reinstall | re-registering the same device name yields one device row, rotated |
| rotation revokes previous | rotating replaces the credential hash → the old secret no longer authenticates |
| no orphan on failure | the installer self-revokes the just-created credential if store/verify fails |
| audit without secrets | `relay_registration_audit` records actor/env/device/result/time — never a token or secret |
| identity from credential | the API derives device identity from the credential hash (`authenticate`), never from client input |

## State compatibility & rollback failure behavior (GAP 2)

`state-manifest.json` records the schema version of each durable kind (checkpoint, outbound delivery-phase
ledger, pending attachments, supervisor state, installer metadata). Before activation, `safe_activate`:
compares on-disk versions to what the **target commit** supports; **blocks** a rollback whose on-disk state
is newer (never silently downgrades or reinterprets newer records — e.g. an ambiguous
`handoff_outcome_unknown` ledger phase is preserved, the rollback refused); runs required **forward
migrations atomically** after a **privacy-safe backup**; and if activation or the post-load **health check**
fails, **automatically restores the prior version + durable state**.

## LaunchAgent lifecycle

- `RunAtLoad` + `KeepAlive{SuccessfulExit:false}`: launchd starts the **supervisor** at login and
  relaunches it only if the whole process dies (throttled 10 s). The supervisor owns the relay child and
  does the fine-grained restart/park/resume (see `docs/relay-supervisor.md`).
- **Stop/resume remotely**: an authenticated `stop` directive parks the supervisor (stays alive, polls);
  clearing it resumes — no reinstall, no login needed.
- Graceful shutdown on `launchctl bootout` (SIGTERM → the supervisor terminates + reaps the owned group).

## Keychain handling

- Service `com.bruce.relay.device-secret`, account `--account` (default `default`) — matches
  `relay/config.py`. The permanent credential is written by `relay.keychain` via **Security.framework**
  (`SecItemAdd`/`SecItemUpdate`), so it exists only in process memory before the Keychain call.
- The permanent credential never appears in the plist, in argv, in the environment, on disk, or in any
  log. Only the **short-lived single-use bootstrap token** is passed to the install step (via
  `BRUCE_RELAY_BOOTSTRAP_TOKEN`), and it is consumed on first use. The relay/​supervisor read the
  credential only through the Keychain; the API derives device identity from it.

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

Proven by `tests/test_relay_installer.py`, `tests/test_relay_state_manifest.py`,
`tests/test_relay_bootstrap.py`, `tests/test_relay_bootstrap_client.py` (secure bootstrap + threat model,
state 0700 + no-wipe, extraction traversal/symlink rejection, plist secret-free + absolute + no-shell,
compat-checked activate with block/migrate/restore, upgrade→rollback preserving durable state, dry-run
writes nothing).

## Exact Mac-only steps still needing on-device approval

These cannot run in CI and require the dedicated Mac + the `bruce-relay` login:

1. **Mint the bootstrap token** (operator, DB side): `python -m scripts.relay_bootstrap mint --device
   <name>` — the short-lived single-use material handed to the installer. (The permanent credential is
   never minted by hand or shown.)
2. **Full-Disk-Access / Automation (TCC)** grants for the `imsg` binary + Messages (macOS privacy
   prompts). Verified at install by a real `~/Library/Messages/chat.db` read.
3. **Messages sign-in** verified (without printing the identity) and **`imsg` version/functionality**
   probed.
4. **LaunchAgent load** (`launchctl bootstrap gui/<uid>`) — requires a real GUI login session (checked by
   the installer preflight).
5. **The dedicated-Mac dry-run** that finally verifies **live iMessage** end-to-end (send + receive) —
   the relay's live behavior remains **UNVERIFIED** until this passes.

Per the review, the real Mac installation is **not performed yet** and requires **explicit approval**;
before the focused live HEIC photo test, A4 must be installed once, the LaunchAgent heartbeat healthy, and
E1 able to start/end the staging enrollment — then the founder's only action is opening Messages and
sending the photo.
