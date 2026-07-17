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
## Increments

Each commit is one concern. Tests were run before each; the full offline suite before each push.

| # | Commit | Goal | Verified | Blocked / limits |
|---|---|---|---|---|
| 1 | `3e7e6c8` | **P1** one-command deploy | dry-run passes end-to-end (tools, env, 31 preflight tests, 45.5MB package, sha256); 12 tests on failure diagnosis vs REAL Alibaba error strings; `s` 3.1.10 installed + `bruce` profile configured | live deploy blocked: FC not activated |
| 2 | `09cb490` | **P2** no false completion + `/ready` | 15 tests; native import profiled at **1.17s** (vs FC's 15s limit) so no risky trimming/lazy-imports were done | cold start on real FC still **inferred**, not measured |
| 3 | `8fdd5bb` | **P3** Google OAuth (PKCE, one-time state, encrypted tokens) | 24 tests vs **real Postgres** + mocked Google; migration 0004 verified on BOTH fresh and pre-existing DBs (rls=true force=true policy present) | nothing has touched Google |
| 4 | `2d66804` | **P3** calendar provider + verified undo | 30 tests; normalization keeps Google types out of the domain | live execution needs `GOOGLE_*` |
| 5 | `6432387` | **P4** server-authorized contract + **P9** smoke script | 16 tests | — |
| 6 | `1e58837` | **P7** messaging boundary | 16 tests via FakeChannel | **no provider connected**; iMessage NOT functional |
| 7 | `cf6eb15` | **P8** README, readiness table, demo script, screenshots checklist, LICENSE | docs cross-checked against measured status | — |

**Suite: 244 → 342 passed, 12 skipped.** Every skip names its blocker.

### Contradictions found and fixed (by my own tests)

1. **`failed` was both terminal and retryable** (`contract.py`). Both cannot be true. Fixed in the
   model, not the test: `TERMINAL = {succeeded, cancelled}`; `failed`/`blocked` are RECOVERABLE.
   Treating them as terminal offers no way out of a transiently-failed mission.
2. **`test_pdf_to_text_rejects_non_pdf` asserted `== ""`** — the false-completion bug encoded as a
   test. Updated to assert the raise. This strengthens the test; it does not weaken it.

### P9 — Qwen smoke, run once at the end (as instructed)

```
2026-07-17T06:43:54Z  ws-5xgdxnbet67n8x8e.ap-southeast-1.maas.aliyuncs.com
  qwen3.7-plus  403 AccessDenied.Unpurchased  req b78e28ef-e7d5-9a64-96a1-51b0f619e4e8  1161ms
  qwen-turbo    403 AccessDenied.Unpurchased  req 6d8832b3-b5d1-937f-9d91-03896feca547   693ms
```

Two calls, no enumeration. A basic text model is refused too → **account-level hold**, not
entitlement. **Zero successful Qwen calls. Blocker stands.** Stopped testing, as instructed.

### Not done this session — stated plainly

| Priority | Status | Why |
|---|---|---|
| **P5** iOS ↔ live API | **NOT DONE** | The app still renders mock data. Wiring it without a live URL or live Qwen would produce a client I could compile but not exercise against anything real. |
| **P6** Live Activity / Dynamic Island | **NOT DONE** | Needs a new app-extension target in `project.yml`. Nothing exists today. |
| P4 endpoints | **PARTIAL** | `contract.py` (states/actions/refs) is done and tested; the remaining journey endpoints are not yet wired to it. |

These are the honest gaps. The iOS app **builds** (Xcode 26.4, `BUILD SUCCEEDED`) but is not
connected.

### Security notes from this session

- Both the RAM AccessKey and the DashScope key are in the chat transcript. **Rotate both** after the
  hackathon; the RAM key can provision and bill infrastructure.
- `deploy_fc.sh` refuses to deploy a `BRUCE_JWT_SECRET` under 32 bytes or containing "test" — on a
  public FC URL with an anonymous gateway trigger, that secret is the only thing in front of student
  data.
- `BRUCE_ENCRYPTION_KEY` is new and **required** before any Google refresh token can be stored. There
  is no plaintext fallback; connect will refuse without it.
