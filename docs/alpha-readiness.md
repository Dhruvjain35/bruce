# Bruce alpha readiness (5–10 students)

Status: **auth is real and production-shaped; deployment is not done.** This is the checklist to get
a real student handing Bruce a real flyer. Nothing here deploys anything — it makes the requirements,
tradeoffs, and blockers explicit so the decisions are yours.

The one-line truth: **the gate is distribution (Apple Developer account + a hosted engine), not code.**

---

## 0. Exact remaining blockers (start here)

1. **Apple Developer Program account** — required for TestFlight AND for the Sign in with Apple
   capability to work on a device. You're a minor, so this needs a parent/guardian's Apple ID or a
   legal entity (the standing "task C"). Until this exists, Bruce cannot run on a student's phone and
   real Sign in with Apple cannot complete (the entitlement isn't provisioned).
2. **A hosted engine** — the app talks to `BRUCE_API_BASE`, default `http://127.0.0.1:8000`. Students
   can't reach localhost. The engine must be deployed with a managed Postgres. (Platform not chosen —
   see §2.)

Everything below is doable in parallel with chasing #1.

---

## 1. Apple Developer / TestFlight

- Enroll in the Apple Developer Program (needs the adult/entity Apple ID).
- App Store Connect: create the app record, bundle id **`com.brucedev.Bruce`**.
- Enable the **Sign in with Apple** capability on the App ID (matches `Sources/Bruce.entitlements`).
- TestFlight: add 5–10 testers by email (internal or external group). External needs a short Beta
  App Review; internal (up to 100) does not — prefer **internal** for the first cohort.
- Build: Xcode Archive → upload. `CODE_SIGNING_ALLOWED` is currently `NO` (unsigned sim builds);
  flip signing on for the archive with the team + provisioning profile.
- **Compliance follow-up**: the current "Continue with Apple" is the app's own styled button. App
  Review generally expects the official `SignInWithAppleButton`. Swap it (or confirm HIG compliance)
  before external review — it drives the same `AppSession.signInWithApple()`.

## 2. Backend deployment — requirements & tradeoffs (NOT chosen yet)

**Requirements (any platform must provide):**
- Python 3.14 runtime, run `uvicorn bruce_engine.api:app` (ASGI). Long-lived process (the in-proc
  worker polls) OR a separate worker process.
- Managed **Postgres 16** with two roles: the owner (migrations) and the restricted `bruce_app`
  (runtime, RLS-enforcing). The app connects as `bruce_app`.
- Outbound HTTPS to OpenAI (`api.openai.com`), OpenAlex, and Apple (`appleid.apple.com/auth/keys`).
- TLS termination (Apple requires HTTPS; drop the dev `NSAllowsLocalNetworking`).
- Secrets management for the env vars in §4.

**Platform tradeoffs (pick one — reporting, not recommending):**
| Platform | Fit | Watch-outs |
|---|---|---|
| **Google Cloud Run** | Scales to zero, cheap for alpha; good Python support | Needs Cloud SQL (Postgres) + connector; the in-proc worker dies on scale-to-zero → run a min-instance=1 or a separate worker/Cloud Scheduler |
| **Fly.io** | Simple always-on VM, managed Postgres, cheap | You manage the Postgres role split + backups |
| **Render** | Easiest managed web service + Postgres | Fewer regions; cost creeps past free tier |
| **Vercel** | Great if we later add a web surface; Python via Fluid Compute | Long-lived in-proc worker is a poor fit — would need a separate worker/queue |

**Worker note:** the durable `intake_jobs` table means work is never lost, but *something* must run
the worker. Options: `BRUCE_INPROC_WORKER=1` with min-instances=1, or a dedicated worker process/cron
calling the claim loop. Decide alongside the platform.

## 3. Database provisioning

```
createdb bruce                       # owner db
CREATE ROLE bruce_app LOGIN PASSWORD '…';   # restricted runtime role (no BYPASSRLS, no CREATE)
```
- Owner URL → `BRUCE_DATABASE_URL` (migrations). App URL (as `bruce_app`) → `BRUCE_APP_DATABASE_URL`.
- Extensions: `pgcrypto` (for `gen_random_uuid()`), created by migration `0001`.

## 4. Environment variables (production)

| Var | Purpose | Required |
|---|---|---|
| `BRUCE_JWT_SECRET` | HS256 secret Bruce signs sessions with (≥32 bytes) | **yes** |
| `BRUCE_APPLE_CLIENT_ID` | acceptable Apple `aud` — `com.brucedev.Bruce` | **yes** |
| `BRUCE_DATABASE_URL` | owner Postgres (migrations) | **yes** |
| `BRUCE_APP_DATABASE_URL` | `bruce_app` Postgres (runtime, RLS) | **yes** |
| `OPENAI_API_KEY` | vision + extraction + drafting + verification | **yes** |
| `OPENALEX_API_KEY` | grounded discovery (outreach) | yes (outreach) |
| `BRUCE_SESSION_TTL_SECONDS` | session lifetime (default 7d) | no |
| `BRUCE_INPROC_WORKER` | run the intake worker in-process | if no separate worker |
| `BRUCE_RAW_RETENTION_DAYS` | raw-content retention (default 30) | no |
| `FEATHERLESS_API_KEY` / `BRUCE_ENABLE_FEATHERLESS` | offline eval only — **leave unset in prod** | no |
| `BRUCE_DEV_AUTH` | **must be UNSET in prod** (dev token gate) | no |

## 5. Migration command

```
BRUCE_DATABASE_URL=<owner> python -m alembic -c engine/alembic.ini upgrade head
```
Run as the owner before first boot and on every deploy. Current head: `0005_intake_jobs`.

## 6. Health & readiness

- `GET /health` — process is up (never touches DB/providers). Use as the platform liveness probe.
- `GET /ready` — 200 only when the DB is reachable AND auth config is present (flags a weak/short
  `BRUCE_JWT_SECRET`); 503 otherwise, naming the failed check. Use as the readiness/gate probe.
- A model provider being down does **not** make the service unready — intake returns a truthful 503.

## 7. Sign in with Apple configuration

- App ID `com.brucedev.Bruce` with Sign in with Apple enabled; entitlement already in the repo.
- **Native app only** (no web redirect needed for the alpha), so **no Services ID / return URL** is
  required. `aud` = the bundle id. (If a web surface is added later, create a Services ID + set its
  return URL and add it to `BRUCE_APPLE_CLIENT_ID`.)
- Nonce is generated per-attempt on device and verified server-side — no config.

## 8. DevAuth shutdown procedure

- The dev token is compiled in **only** under `#if DEBUG` **and** `BRUCE_DEV_AUTH=1`. A Release
  (TestFlight/App Store) build strips it entirely — it cannot fall back to it.
- Checklist before shipping: build **Release**, confirm `BRUCE_DEV_AUTH` is unset in the run scheme,
  confirm the server has **no** `sub=1111…` dev user, and that `BRUCE_JWT_SECRET` is a fresh prod
  secret (not the dev one).

## 9. Tester onboarding

1. Send the TestFlight invite (internal group).
2. Tester installs, taps **Continue with Apple**, grants name/email once.
3. First screen after auth = the real Home; the "Hand something to Bruce…" bar is the capture entry.
4. Give each tester one concrete task: "photograph a real school flyer and hand it to Bruce."
5. Keep the onboarding config steps minimal for the alpha (grade + a couple protocols).

## 10. Privacy & account deletion

- **Account deletion exists**: `DELETE /v1/account` removes the user's row; FK `ON DELETE CASCADE`
  wipes everything they own. Surface a "Delete my account" control in Settings before external review
  (App Store requires an in-app deletion path).
- **Raw content** (`sources.raw_text`, job input bytes) is transient: job inputs are cleared on
  completion; raw text is swept per `BRUCE_RAW_RETENTION_DAYS` (default 30). Durable data is the
  derived, minimized extraction + lineage.
- **Analytics carry no content** — enum-typed events only (verified by test).
- Minimum identity stored: derived user_id + optional email (first authorization only). No raw Apple
  token stored.
- Write a one-page privacy note (what's collected, retention, deletion) for testers + App Review.

## 11. Error reporting

- Backend: structured logs already redact content (types/reasons only). Add a crash/error sink
  (Sentry or the platform's logs) — **must not** log request bodies or `raw_text`.
- iOS: add a lightweight crash reporter (or MetricKit) for the alpha; keep it content-free like the
  intake analytics. Not yet wired — a small alpha task.

## 12. Seed / demo data policy

- **No fabricated student data in production.** The mock `Mock.*` surfaces (Home list, outreach,
  decisions) are dev-only and must read empty for a real tester until wired to `/v1/missions`.
- The eval corpus is built from **real** tester flyers (with consent), not synthetic — that's also
  the demand signal. Do not seed the eval set with invented documents.

## 13. Five-to-ten-student test protocol

1. Recruit 5–10 students; get consent to store what they submit (short form).
2. Each: hand Bruce **≥3 real items** (a flyer photo, a screenshot, a PDF or pasted text).
3. Observe: does "Understanding…" appear within ~1s? Are the extracted dates correct? Did it refuse
   to guess an ambiguous date? Did anything read as a false completion?
4. Capture per submission: source type, correct/missed/hallucinated dates, latency, did they trust it.
   (This is exactly the eval harness's grounded-accuracy / missed / unsupported metrics — feed the
   real docs into `engine/eval/`.)
5. Exit question: "would you use this again this week?" — the real signal.

## 14. Rollback plan

- Deploys are migration-forward; keep the previous image/release to re-point traffic instantly.
- DB: take a snapshot before each `alembic upgrade`; every migration has a `downgrade()` (tested via
  the CI reset), but prefer restore-from-snapshot for a data-bearing rollback.
- Auth: rotating `BRUCE_JWT_SECRET` invalidates all sessions (a clean "log everyone out" lever).
- Feature kill: `BRUCE_INPROC_WORKER=0` pauses extraction (jobs queue durably and resume on re-enable).

---

## Immediate next engineering steps (parallel to chasing the Apple account)

1. Wire an in-app **Delete account** control (endpoint exists) — needed for review.
2. Swap to the official **SignInWithAppleButton** (or confirm HIG) for external review.
3. Make Home read **real** `/v1/missions` (retire the mock list) so a tester sees only their data.
4. Add a content-free crash reporter (iOS + backend).
5. Pick the platform (§2) and do a **staging** deploy behind `/ready` — do not put a dev-token API on
   the internet; auth being real is what makes deployment worth doing.
