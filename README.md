# Bruce

**Hand it to Bruce. Bruce does the work and proves the result.**

Bruce is an iPhone-native action system for the administrative half of student life — the deadlines,
fees, forms, and follow-ups that arrive as a flyer photo, a screenshot, or a forwarded email and
then quietly expire.

Bruce is not a chatbot, not a homework machine, and not an email summarizer. It doesn't give advice.
You hand it something; it organizes, executes, verifies, and shows proof.

> **Honest status.** See [`docs/hackathon-readiness.md`](docs/hackathon-readiness.md) for the full
> truth table. Short version: the backend is real and tested (342 passing against real PostgreSQL);
> **zero live Qwen inference calls have succeeded** (the Alibaba account is under a risk-control
> hold), there is **no live deployment yet** (Function Compute activation pending), and the iOS app
> still renders mock data. Nothing in this README claims otherwise.

---

## The problem

A student's deadlines don't arrive as calendar invites. They arrive as a photo of a flyer on a
hallway wall. Registration closes Feb 28. There's a $25 fee. A parent has to sign something. You
read it once, mean to deal with it, and it's gone.

Existing AI tools will happily *tell you about* your flyer. None of them will add the deadline, prove
they added it, and stop to ask you the one question that actually needed a human.

## The demonstrated workflow

```
Real flyer photo (no text layer)
  → Qwen 3.7 Plus reads the pixels
  → verbatim transcript (the source of truth for grounding)
  → grounded extraction: deadlines, fees, required forms — each carrying the exact text it came from
  → durable mission + tasks, persisted under row-level security
  → ONE decision: the exact calendar event, on the exact calendar, at the exact time
  → student approves once
  → Google Calendar events.insert
  → Google Calendar events.get — read back and compared field by field
  → verified receipt, showing the source and the evidence
```

**A write is a claim. A read-back is evidence.** Bruce says "done" only when it has evidence.

## Why this isn't a generic chatbot

| Generic assistant | Bruce |
|---|---|
| Tells you what your flyer says | Adds the deadline and proves it |
| "I've added that for you!" | Reads the event back out of Google and compares it before claiming success |
| Confidently invents a date | Drops any date it can't point at verbatim in the source |
| Asks about everything, or nothing | Asks only when judgment or permission is genuinely required |
| Retry double-books you | The provider itself rejects the duplicate (caller-supplied event id) |

The product metric is **useful student work completed per decision required** — not messages sent.

## Qwen's role

Qwen is the **multimodal intake brain**. It reads flyers, screenshots, forms, and PDF pages that
have no text layer, and turns them into grounded structure.

It is deliberately **not** trusted to act. Qwen never calls a tool, never writes to the database, and
never decides whether an action is allowed. Extraction is data; policy and execution are Bruce's.

- **`qwen3.7-plus`**, non-thinking (`enable_thinking: false`) — a thinking-mode response must never
  be relied on to *be* action JSON.
- **Two-pass on purpose**: transcribe the image verbatim, *then* extract structure from that
  transcript, so the same anti-hallucination gate that protects text protects pixels. A single
  image→JSON call yields spans checkable only against the model's own claim about the image — a
  hallucinated deadline would be unfalsifiable.
- **No silent fallback.** If Qwen is unavailable, intake returns `503 provider_unavailable` naming
  the real cause. Answering with a different provider while claiming a Qwen-powered workflow would
  make the whole demonstration a lie.

**Live status: BLOCKED.** The account authenticates and lists 149 models; all 144 tested return
`403 AccessDenied.Unpurchased`, including non-Qwen models — an account-level hold, not entitlement.
The adapter's wire format is tested against the real client stack. Zero real calls have succeeded.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full diagram.

```
Student → iOS / Share Sheet / (messaging, planned)
        → Alibaba Function Compute (Singapore) — FastAPI
        → Qwen Cloud (multimodal extraction)          [LLM proposal layer]
        → PostgreSQL: missions, evidence lineage, RLS  [durable state]
        → deterministic policy + approval              [decision layer — no LLM]
        → Google Calendar (execute)                    [external action]
        → Google Calendar read-back (verify)           [proof]
        → receipt / Dynamic Island (planned)
```

The split that matters: **an LLM proposes, deterministic code decides and executes, and an external
read-back verifies.** No LLM output becomes an executable action without typed validation.

## Security model

Bruce holds real student data and writes to real calendars. The guarantees are enforced by
PostgreSQL and by external providers — not by application code that a bug could bypass:

| Guarantee | Enforced by |
|---|---|
| Tenant isolation | Postgres RLS + `FORCE RLS`, restricted non-owner `bruce_app` role |
| User identity | derived only from a verified JWT — never from client input |
| Intake idempotency | `UNIQUE(user_id, idempotency_key)` — not check-then-insert |
| Calendar execute-once | Google rejects a duplicate caller-supplied event id (409) |
| Mission concurrency | optimistic `version` column |
| Evidence lineage | FK chain `sources → source_spans → tasks` |
| Refresh tokens | Fernet-encrypted before storage, key outside the DB |
| OAuth callback | identity read from a one-time server-side state row, never from the query string |

Cross-user access returns **404, never 403** — a 403 would confirm the object exists.

## Grounded extraction

Every extracted deadline carries the verbatim `source_span` it came from. After extraction, any
deadline whose span isn't literally present in the source is **dropped**. Ambiguous dates ("the
following Friday") are left unresolved and flagged, never guessed.

A failure to *read* something is never reported as "read it, found nothing" — a corrupt PDF returns
a typed `422`, not a cheerful 200 with zero findings.

## Setup

```bash
git clone https://github.com/Dhruvjain35/bruce && cd bruce/engine
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in; .env is gitignored and must never be committed

# Postgres 16 + the restricted app role
createdb bruce
psql bruce -c "CREATE ROLE bruce_app LOGIN PASSWORD '<pw>'"
.venv/bin/python -m alembic upgrade head    # run as OWNER, never at app startup

.venv/bin/python -m uvicorn bruce_engine.api:app --reload
```

### Environment

| Variable | Purpose |
|---|---|
| `BRUCE_JWT_SECRET` / `BRUCE_JWKS_URL` | JWT verification (≥32 bytes; the only thing in front of student data) |
| `BRUCE_DATABASE_URL` | owner URL — migrations only |
| `BRUCE_APP_DATABASE_URL` | restricted `bruce_app` role — the app connects as this, so RLS applies |
| `BRUCE_ENCRYPTION_KEY` | Fernet key for refresh tokens (`bruce_engine.crypto.generate_key()`) |
| `DASHSCOPE_API_KEY` / `QWEN_BASE_URL` / `QWEN_INTAKE_MODEL` | Qwen Cloud |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | Google OAuth |
| `BRUCE_RAW_RETENTION_DAYS` | raw-content retention window (default 30) |

Never commit `.env`. Never give the iOS client a service credential.

## Testing

```bash
make test            # full suite — uses real PostgreSQL when available
make deploy-fc-dry   # all deployment validation, contacts nothing
make qwen-smoke      # ONE bounded live Qwen call
make ios             # build the iOS app
```

**342 passed, 12 skipped.** Every skip names its blocker (no Qwen entitlement, no Google
credentials, no deployment URL). A skip is never reported as a pass.

Tests run against **real PostgreSQL** through the restricted role — RLS, isolation, idempotency and
durability are exercised against the actual database, not SQLite or mocks.

## Deployment

Alibaba Function Compute, Singapore. See [`deploy/README.md`](deploy/README.md).

```bash
make deploy-fc    # preflight → build → deploy → verify the LIVE url → write proof
```

It refuses to deploy on a weak JWT secret or failing preflight, verifies `/health` and that `/v1/*`
returns 401 unauthenticated **on the live URL**, and writes non-secret proof to
`docs/deployment-proof.json`. Deployment is not claimed until the live URL answers.

## Known limitations

- **Qwen inference is blocked** at the account level. Zero successful calls.
- **Not deployed.** Function Compute activation is pending.
- **iOS renders mock data.** The app builds; it is not wired to the live API.
- **No Live Activity / Dynamic Island yet.**
- **Messaging is a tested boundary, not a connected channel.** iMessage is **not** functional; no
  message has passed through any provider.
- Migration `0001` uses `create_all()` against live models rather than static DDL — known debt,
  documented in `docs/deployment-verification.md`. Later migrations defend against it explicitly.
- Image grounding verifies spans against the transcript, not raw pixels. A transcription error is
  still an error — just an auditable one rather than an invented deadline.

## What Bruce will not do

Graded work, invented achievements, impersonating a student without review, or claiming a completion
it cannot prove.

## License

MIT — see [LICENSE](LICENSE).
