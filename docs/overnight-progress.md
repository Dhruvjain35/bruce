# Overnight work log — 2026-07-17

Running log for the autonomous session. Status vocabulary is used strictly:

| Term | Means |
|---|---|
| **implemented** | code exists |
| **locally verified** | ran on this machine and observed the behaviour |
| **integration-tested** | exercised through the real client/transport stack (mocked network) |
| **live-verified** | ran against the real external service |
| **blocked** | cannot proceed without an external credential/activation |
| **inferred** | reasoned, NOT measured — treat as unproven |

`docs/deployment-verification.md` remains the source of truth for deployment/live claims.

---

## Priority 0 — Audit / baseline

**Starting commit: `ef7f021`** (branch `hackathon/qwen-cloud`, up to date with origin)

### Test baseline (locally verified)

```
244 passed, 11 skipped
```

All 11 skips are honest and name their blocker:

| Count | Skip reason |
|---|---|
| 8 | `BRUCE_DEPLOY_URL` / `BRUCE_DEPLOY_JWT` unset — no live Alibaba deployment to smoke test |
| 1 | Google Calendar not configured — `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN` missing |
| 1 | Qwen account not entitled (403 `AccessDenied.Unpurchased`) |
| 1 | (deployment smoke, authed diagnostics) |

Postgres integration tests **do** run (local PostgreSQL 16 present): `test_postgres_integration.py`
(17) + `test_retention.py` (8) + `test_intake_persistence.py` (15) all execute against a real
disposable `bruce_test` DB through the restricted `bruce_app` role. They are **not** skipped.

### Uncommitted on entry

`ios/Sources/Choose.swift`, `ios/Sources/MockData.swift` — pre-existing, not mine, left alone.

### Environment (locally verified)

| Tool | Status |
|---|---|
| Xcode | **26.4** present — iOS build CAN be verified |
| xcodegen | present |
| docker (colima, arm64 + qemu-x86_64 binfmt) | present |
| aliyun CLI | 3.4.7, profile `bruce` configured |
| PostgreSQL 16 | running locally |
| `s` (Serverless Devs) | **NOT installed** — needed for `s deploy` |

### Component status on entry

| Component | Status | Evidence |
|---|---|---|
| Backend auth/RLS/persistence | locally verified | 244-test suite, real Postgres |
| Qwen provider adapter | integration-tested (wire format), **live BLOCKED** | 0/144 models callable |
| Google Calendar domain logic | integration-tested (fake + mock transport) | `test_calendar_adapter.py` 15 tests |
| Google **OAuth flow** | **DOES NOT EXIST** | adapter reads `GOOGLE_REFRESH_TOKEN` from env only — no authorization-code flow, no state, no encryption, no persistence |
| FC code package | locally verified | 44MB zip / 60.7MB base64; serves under emulated Debian12/py3.11 |
| FC deployment | **blocked** | `FC service is not enabled for current user` (activation pending, SMS) |
| iOS networking | skeleton | `BruceAPI.swift` 59 lines; mock data in 7 view files |
| Live Activity / ActivityKit | **DOES NOT EXIST** | no ActivityKit reference anywhere in `ios/` |
| Messaging channel | **DOES NOT EXIST** | no webhook/inbound boundary in `bruce_engine/` |

### Contradictions with the handoff — none material

The handoff is accurate. Two clarifications:
- "Google Calendar verification domain logic" exists and is well tested, but **OAuth does not** —
  the adapter takes a refresh token from the environment. Priority 3 is therefore mostly greenfield.
- The handoff says ~244 passing; exact count is **244 passed / 11 skipped**. Confirmed.

---
