# Bite 1.5 — Zero-Terminal Alpha Operations + Messaging Onboarding

Authoritative architecture. Supersedes any earlier framing where a temporary/expiring per-user
enrollment was the access model. **Production access is self-serve and persistent; the expiring
enrollment is a staging-only tool.** Nothing here is designed around founder-operated user activation.

## Three separated concepts (do not conflate)

| Concept | Who | Lifetime | Operator action per user |
|---|---|---|---|
| **Zero-touch production onboarding** | every real user | one-time flow | **none** |
| **ProductionAccountEntitlement** | every onboarded user | **persistent** | **none** |
| **StagingTestEnrollment** | internal test users only | temporary (optional expiry) | operator-enabled, revocable |

The staging system is **never required for production usage**.

## 1. Zero-touch production onboarding (self-serve, no operator)

A production user, with **zero** operator actions (no approval, no DB edit, no flag, no terminal, no
manual code, no relay restart, no hand-made enrollment):

1. signs up via Bruce web/native onboarding
2. provides required profile + onboarding info
3. verifies their phone number
4. optionally connects Canvas / Google Calendar / Gmail
5. is issued a **short-lived, single-use iMessage linking token** (auto-generated after signup)
6. taps an **"Open Messages"** button with the linking message prefilled (or texts the token to Bruce)
7. is **automatically linked** to their stable Bruce user
8. **immediately** uses Bruce through Messages

On successful link Bruce sends (Voice OS, **no em dashes**): `ur connected ✅\ntext me anything u need`

### Linking-token security
Auto-generated post-signup · short-lived · **single-use** · stored **hashed/HMAC** (raw token never
persisted or logged) · **bound to the intended user + onboarding session** · guessing/replay resistant ·
invalidated on successful link · rate-limited · audited without the raw token · **re-binding an
already-owned identity requires recovery verification** (no silent re-bind).

### Onboarding recovery (all supported, honestly)
expired-token regeneration · wrong messaging identity · phone-vs-AppleID-email mismatch · already-linked
identity · changed phone number · account recovery · unlink + relink · lost integration authorization ·
incomplete-signup continuation. **Preserve the user's original request during onboarding when safe, and
auto-continue it after linking** — the user never resends.

## 2. ProductionAccountEntitlement (persistent)

The durable production access record — **no 30-minute or other temporary expiration for normal users.**
Fields: `user_id`, `account_status`, `plan`, `messaging_enabled`, `verified_identity`, `created_at`,
`suspended_at`, `entitlement_reason`, `capability_availability`.

Conversation access **persists** until exactly one explicit event: user unlinks the messaging identity ·
user deletes the account · account suspended · subscription/entitlement ends · security/abuse
enforcement blocks the account · global emergency shutdown.

## 3. StagingTestEnrollment (internal only)

For staging / canary / **unreleased** capabilities only. Fields: `user_id`, `environment`, `capability`,
`enabled_at`, `expires_at` (optional), `enabled_by`, `audit_reason`, `revoked_at`/kill status. Optional
expiration, immediate revoke, full audit. **Must never gate production usage.**

## Runtime enforcement — `enabled_for(user_id, capability)`

Decided by **user_id in the DB**, never by fragile string comparison, and no model/relay process can
bypass it:

1. **Global emergency kill** for the capability → **DENY** (emergency shutdown wins over everything).
2. **Production:** `ProductionAccountEntitlement(user_id)` with `account_status = active` AND
   `messaging_enabled` AND capability in `capability_availability` → **ALLOW** (persistent).
3. **Staging:** a live (`revoked_at` null, not past `expires_at`) `StagingTestEnrollment(user_id,
   capability)` → **ALLOW** (internal test).
4. else **DENY**.

The env `BRUCE_CONVERSATION_RUNTIME` is demoted to a **global rollout/kill switch only** (default OFF
globally until rollout approval) — it is no longer a per-user allow-list. Per-user access comes from the
two DB concepts above.

## Owner experience (aggregate, not per-user approval)

The owner dashboard shows operational health and manages **releases**, never individual onboarding:
total signups · onboarding completion rate · linking success rate · relay/API/queue health · error rate ·
latency · cost · abuse alerts · integration health. It **must not** require manual approval for ordinary
users.

## Relay operations (unchanged by this correction)

- **LaunchAgent** under the `bruce-relay` GUI session (not a system daemon): one relay, auto-start on
  login, crash auto-restart with bounded backoff, preserves checkpoint + at-most-once ledger +
  pending-attachment store (never wipes durable state on a normal start), single-watcher lock, Keychain
  secret only, privacy-safe rotating logs, heartbeat + stale detection, remote emergency outbound kill,
  reaps imsg children. Pinned commit, no auto git-pull, rollback-capable.
- **One-time installer** (operator, on the physical Mac): validates macOS user + Messages sign-in +
  Full Disk Access + Keychain secret, installs+loads the LaunchAgent, proves heartbeat healthy, no
  secrets printed.
- **brucectl** operator CLI: status/start/stop/restart/health/logs/update/rollback/pause-outbound/
  resume-outbound/diagnose — redacted, ownership-checked, never wipes durable state without explicit
  destructive confirmation. Normal users never see it.

## UX failure states (Voice OS, no em dashes)

- Relay unavailable — only if the inbound is **durably stored** and a real wake/recovery path exists:
  `bruce is temporarily offline rn. ur message is saved and nothing was sent anywhere. i'll handle it
  when the connection is back.`
- Onboarding incomplete: state the exact next step, preserve the original request, auto-continue after
  linking.
- Integration missing: say what Bruce understood + what capability is missing + one secure connection
  link + preserve the proposed action.

## Build sequence (small, independently-mergeable, test-backed)

1. **Keystone — access model + enforcement:** `ProductionAccountEntitlement` + `StagingTestEnrollment`
   + global capability/kill state (RLS-isolated, migration), and `enabled_for(user_id, capability)`
   rewired to consult them. Removes per-user Cloud-Run env editing.
2. **Self-serve onboarding:** signup → phone verify → auto linking token → link → persistent entitlement,
   with the token security + recovery cases + auto-continue. (Web pages + Messages copy.)
3. **Relay supervisor + LaunchAgent + installer** (server/plist/scripts by Claude; one-time load on the
   operator's Mac).
4. **brucectl.**
5. **Owner dashboard** (aggregate health).
6. **Staging test mode** (E) over `StagingTestEnrollment` — the internal, no-redeploy test path.
7. **UX failure states** (durable inbound + copy).

### What Claude builds+tests vs the one-time operator Mac step
Claude: all schema/migrations/enforcement, onboarding server + web + tests, supervisor logic + plist +
installer script + brucectl + tests, dashboard, failure-state logic. **Operator (once, on the Mac):**
run the installer to load the LaunchAgent under `bruce-relay` (Messages/FDA/Keychain live in that GUI
session). After that: a user just opens Messages and texts Bruce.

## Acceptance tests — separated
Production zero-touch onboarding (no operator action) · persistent entitlement (survives, no expiry) ·
staging enrollment (temporary, expiry, revoke) are tested as **distinct** paths; a production user is
never gated by a staging enrollment, and a staging enrollment never grants persistent access.
