# Bruce staging — verification (Google Cloud)

Non-secret proof of the staging deployment. No secrets, passwords, tokens, or DB URLs are recorded
here (they live only in Secret Manager). Deployment + verification only — no product features.

## Environment

| | |
|---|---|
| GCP project | `bruce-staging-2645` (isolated; labels `app=bruce, env=staging`) |
| Region | `us-central1` (API, worker, Cloud SQL, Artifact Registry, Cloud Tasks all co-located) |
| Deployed commit | `0056988` (`BRUCE_COMMIT`), `BRUCE_ENV=staging` — adds the self-hosted iMessage relay + messaging endpoints (was `d080231`) |
| **API URL** | **https://bruce-api-3iwweh3bqa-uc.a.run.app** (public; only auth + health routes are open) |
| Worker URL | https://bruce-worker-3iwweh3bqa-uc.a.run.app (**private** — no unauthenticated access) |

## Architecture (as deployed)

```
iPhone app → Cloud Run bruce-api → Cloud Tasks (bruce-intake) → private Cloud Run bruce-worker
                     │                                                    │
                     └─ commits durable intake_jobs                       └─ claims w/ lease → OpenAI
                        (API never does model work inline)                   → persists to Cloud SQL
```

- **Cloud Run `bruce-api`** — revision `bruce-api-00003-4xr`; min instances **0**, max **3**, startup
  CPU boost, startup+liveness probes on `/health`. SA `bruce-api-staging`.
- **Cloud Run `bruce-worker`** — revision `bruce-worker-00002-vk4`; private (invoker: only
  `bruce-tasks-invoker-staging` has `run.invoker`); min **0**, max **3**. Entrypoint
  `uvicorn bruce_engine.worker_api:app`. SA `bruce-worker-staging`.
- **Cloud Tasks** queue `bruce-intake` (OIDC-authenticated dispatch to the private worker).
- **Cloud SQL** `bruce-staging-db` — PostgreSQL 16, `db-f1-micro`, ENTERPRISE edition, single-zone,
  no HA, 10 GB HDD, no automated backups. Connection name
  `bruce-staging-2645:us-central1:bruce-staging-db` (reached via the Cloud SQL connector — **not**
  exposed to the public internet).
- **Secret Manager** — `bruce-jwt-secret`, `bruce-openai-api-key`, `bruce-apple-client-id`,
  `bruce-db-root-password`, `bruce-db-app-password`, `bruce-database-url`, `bruce-app-database-url`
  (per-secret `secretAccessor` grants; **no downloadable SA keys** anywhere).

## Migration

- Applied by Cloud Run job `bruce-migrate` (execution `bruce-migrate-nmqqt`, succeeded) running
  `alembic upgrade head` as the **owner** role over the connector.
- Migration revision: head **`0009_relay_uploads`** (schema + FORCE-RLS policies + least-privilege
  `bruce_app` grants). `0006`–`0009` add the messaging domain, relay devices (worker-only RLS),
  outbound `to_handle`, and staged relay uploads. Confirmed live below by real table-backed queries.
  (Earlier `0005_intake_jobs` deploy was execution `bruce-migrate-cbjh5`.)

## RLS / least privilege (verified)

- Two DB roles: `postgres` (owner — migrations only) and `bruce_app` (runtime). `bruce_app` is a
  plain login role — **not** a superuser and Cloud SQL grants it **no `BYPASSRLS`** — and migration
  0002 gives it DML-only grants. The app connects **only** as `bruce_app`.
- Confirmed live: an authenticated intake persisted under the caller's RLS context, was visible only
  to that user, and account deletion cascaded it away (see smoke). RLS is enforced by Postgres, not
  app code.

## Sign in with Apple

- `BRUCE_APPLE_CLIENT_ID=com.brucedev.Bruce`; Apple JWKS verification + nonce/issuer/audience/exp
  checks unchanged. `BRUCE_DEV_AUTH` is **unset** in staging → no dev token path.
- **NOT yet verified from a real signed app** — live Apple auth requires the TestFlight build (blocked
  on the Apple Developer account). The smoke session below was minted with the staging JWT secret,
  which is exactly what the `/v1/auth/apple` exchange issues; it verifies the token path, not Apple.

## Smoke test (2026-07-18, commit d080231)

| # | Check | Result |
|---|---|---|
| 1 | `/health` returns the deployed commit + `staging` | ✅ `commit=d080231, env=staging` |
| 2 | `/ready` confirms database + mandatory config | ✅ `database=ok, auth_config=ok` |
| 3 | Protected endpoint without JWT → 401 | ✅ |
| 4 | Valid session via the supported token path | ✅ (minted with staging secret) |
| 5–6 | Real flyer → `POST /v1/intake` acknowledges fast | ✅ 202, ack ~1.5 s (cold start) |
| 7 | Worker (via Cloud Tasks) claims it | ✅ phase `understanding → extracting` |
| 8 | Grounded dates/tasks persist | ✅ Registration `2026-02-28`, Projects `2026-03-14` |
| 9 | No unsupported claims | ✅ "Judging…the following Friday" left `null` (not guessed) |
| 11 | Delete test account → data removed | ✅ 200, then `/v1/missions = []` |

Item **#10 (kill the worker mid-job → recovery)** is not run as an explicit staging step here; the
mechanism (lease expiry + Cloud Tasks at-least-once retry + idempotent phase-2 persist) is covered by
the Postgres test suite (`test_async_intake_pg`: outage→blocked→reclaim, dup-worker→no duplicates).
An explicit staging kill-test is a low-risk follow-up.

## Self-hosted iMessage — server endpoints (2026-07-18, commit 0056988)

The relay + messaging endpoints are now **live in staging**. This verifies the SERVER side only —
see [`self-hosted-imessage-alpha.md`](self-hosted-imessage-alpha.md). **Live iMessage remains
UNVERIFIED** until the dedicated-Mac dry-run passes; the dedicated Mac is not yet wired.

| # | Check | Result |
|---|---|---|
| 1 | `/health` reports the new commit | ✅ `commit=0056988, env=staging` |
| 2 | `/v1/relay/{inbound,outbound/claim,heartbeat,upload}` without a device credential → 401 | ✅ all 401 |
| 3 | `/v1/relay/inbound` with a bad Bearer secret → 401 | ✅ (hash lookup + `compare_digest` reject) |
| 4 | `GET /v1/messaging/identities` (valid session) → 200 | ✅ `[]` (queries `messaging_identities` under RLS — table live) |
| 5 | `POST /v1/messaging/link-code` (valid session) → 200 | ✅ 6-char code + `expires_at` (writes `account_link_codes` — table live) |

Checks 4–5 prove migrations `0006`–`0009` applied (the endpoints touch the messaging tables; a missing
table would 500, not 200). No relay device is registered in staging yet — provisioning is an operator
action (`scripts/register_relay_device.py`), deferred to the dedicated-Mac setup. The cloud exposes no
route that dials the Mac; the relay is the only initiator.

## Cost

- **~$10/mo**, dominated by Cloud SQL `db-f1-micro` (~$9, the only always-on cost). Cloud Run
  (min=0), Cloud Tasks (1M free ops), Artifact Registry + Secret Manager are ~$0 at alpha scale.
  Under the **$15 stop-gate**. OpenAI is billed separately (not GCP; the GCP credit doesn't cover it).
- Budget `bruce-staging` = $15 with alerts at **$5 / $10 / $15** — these are **warnings, not hard
  caps** (Google budgets do not stop spend; an automated shutdown would be a separate mechanism).

## Operational commands

- **Full teardown (deletes everything, stops all cost):**
  `gcloud projects delete bruce-staging-2645`
- **Database export (before teardown / rollback):**
  `gcloud sql export sql bruce-staging-db gs://<your-bucket>/bruce-$(date +%F).sql --database=bruce --project=bruce-staging-2645`
  (requires a GCS bucket and the Cloud SQL service agent granted `roles/storage.objectAdmin` on it)
- **Rollback API/worker:** re-deploy a prior image tag, or `gcloud run services update-traffic bruce-api --to-revisions=<older>=100`.
- **Log everyone out:** rotate `bruce-jwt-secret` (invalidates all sessions).
- **Pause extraction:** the worker is min=0 + Cloud-Tasks-driven; disabling the queue holds jobs
  durably (they resume when re-enabled).

## Remaining blocker for real-device testing

1. **Apple Developer account** — required to sign the app, provision the Sign in with Apple
   entitlement, and ship via TestFlight. This is the true gate (no code left).
2. **Point the iOS app at staging** — set `BRUCE_API_BASE=https://bruce-api-3iwweh3bqa-uc.a.run.app`
   in the TestFlight build scheme (default is `localhost`). One-line config, no code change.
3. Then verify **live Sign in with Apple** from the signed app (spec: not claimed until it succeeds).
