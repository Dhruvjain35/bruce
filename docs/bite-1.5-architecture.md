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

**Automatic entitlement (no operator, ever).** On successful signup + identity verification, D1 calls
`activate_production_entitlement(user_id, ...)` to **create or activate** the user's
`ProductionAccountEntitlement` automatically. No founder / operator / Claude session / CLI / DB edit /
admin approval is involved. "Every user needs a grant" must **never** become "an operator grants every
user." The keystone (C1) provides the entitlement store + the programmatic activation function + the
access decision; the operator CLI's `grant-production` is a **recovery/interim admin tool only**, never
the normal path.

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
bypass it. Production access resolves from: **verified linked identity + active
`ProductionAccountEntitlement` + account standing + plan/capability availability + global safety state.**

1. **Global emergency kill** for the capability → **DENY** (emergency shutdown wins over everything).
2. **Production:** `ProductionAccountEntitlement(user_id)` with `account_status = active` AND
   `messaging_enabled` AND `verified_identity` AND capability in `capability_availability` → **ALLOW**
   (persistent — it does **not** expire because a test timer elapsed).
3. **Staging:** a live (`revoked_at` null, not past `expires_at` when set) `StagingTestEnrollment(user_id,
   capability)` → **ALLOW** (internal test). Staging never makes production access persistent.
4. else **DENY**. (rollout_state never widens this — no one-call mass-enable.)

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

## Build sequence — usable vertical slice before the dashboard

Linear migration head; C1 lands first (it owns the runtime gate every other PR builds on).

1. **C1** access keystone (this = the access model above + `async enabled_for(user_id)`).
2. **A1** relay control-plane: enriched heartbeat + authoritative claim kill switch + directives.
3. **A2** relay client enforcement (honor kill switch, fail-closed auth exit, heartbeat status).
4. **A3** supervisor (two-tier watchdog, headless-testable).
5. **A4** LaunchAgent + one-time installer (installer runs once on the operator's Mac).
6. **B1** brucectl.
7. **E1** minimal **zero-terminal staging test surface** (see acceptance flow below).
8. **Focused live HEIC regression** through the new flow (no start-script, no env editing).
9. **D1** fully self-serve production onboarding (auto-creates the persistent entitlement). Required
   before any real external-user launch.
10. **F1 + F2** recovery flows (durable deferred-inbound + onboarding-preserve + connect-links).
11. **G** aggregate owner dashboard.

## Bite 1.5 final acceptance — the zero-terminal staging test

Bite 1.5 is NOT complete until the founder runs a staging test **without** asking Claude to enroll,
opening Terminal, changing Cloud Run vars, restarting the relay, or editing the DB. The finished flow:

1. founder opens the authenticated internal test surface (E1)
2. relay, API, queue, model, and pinned commit all show healthy
3. founder selects their own linked staging account
4. founder taps **start test**
5. a `StagingTestEnrollment` is **created automatically** with a chosen duration **or**
   persistent-until-manually-ended (and immediate-revoke available)
6. founder opens Messages and texts Bruce
7. privacy-safe turn results appear (counts/latency/status/dup-detection)
8. founder taps **end test**
9. the enrollment is revoked and outbound-queue cleanliness is confirmed
10. the relay stays running and healthy

Staging enrollment durations: chosen duration · persistent-until-manually-ended · immediate revoke.
**Production entitlements never expire from a test timer.**

## Emergency global shutdown — auth (security clarification)

The emergency stop must **not** rely on a long-lived static token in a URL or browser storage. It uses
strongly-authenticated, audited admin access: short-lived credentials · replay resistance · rate limits ·
explicit **environment display** · an explicit **confirmation** for global shutdown · immediate effect ·
fail-closed. (Server-side, the authoritative enforcement already lives in the claim path: a killed
capability / paused device yields nothing to send regardless of any client.)

## End state
- **New customer:** signs up, verifies, links Messages → Bruce works **indefinitely** (persistent
  entitlement, auto-created).
- **Founder testing staging:** open dashboard, tap start, text Bruce, tap end.
- **Launching Bruce:** deploy once and watch the system — never manually bless each person.

### What Claude builds+tests vs the one-time operator Mac step
Claude: all schema/migrations/enforcement, onboarding server + web + tests, supervisor logic + plist +
installer script + brucectl + tests, dashboard, failure-state logic. **Operator (once, on the Mac):**
run the installer to load the LaunchAgent under `bruce-relay` (Messages/FDA/Keychain live in that GUI
session). After that: a user just opens Messages and texts Bruce.

## Acceptance tests — separated
Production zero-touch onboarding (no operator action) · persistent entitlement (survives, no expiry) ·
staging enrollment (temporary, expiry, revoke) are tested as **distinct** paths; a production user is
never gated by a staging enrollment, and a staging enrollment never grants persistent access.
