# Hackathon readiness — submission truth table

**Deadline: July 20, 2026, 2:00 PM PDT.** Last updated 2026-07-17.

This file is written to be read by a judge who assumes we are overclaiming. Every row states what
is *verified* and what is not. If a row is not green here, do not claim it anywhere — not in the
Devpost description, not in the video, not in the README.

**Bottom line: the submission is NOT currently eligible.** It requires genuine Qwen Cloud usage.
Zero real Qwen inference calls have succeeded. Everything else is either done or one credential away.

---

## Status vocabulary

| Term | Means |
|---|---|
| **live-verified** | ran against the real external service and observed the result |
| **integration-tested** | exercised through the real client/transport stack, network mocked |
| **locally verified** | ran on a developer machine and observed the behaviour |
| **implemented** | code exists; not exercised end-to-end |
| **blocked** | cannot proceed without an external credential/activation |
| **inferred** | reasoned, NOT measured — unproven |

---

## Submission requirements

| # | Requirement | Implementation | Verification | Evidence | Blocker | Exact next action |
|---|---|---|---|---|---|---|
| 1 | Public repository | done | live-verified | github.com/Dhruvjain35/bruce | — | make the repo public before submitting |
| 2 | Open-source license | **MISSING** | — | — | — | add `LICENSE` (MIT) at repo root |
| 3 | **Qwen Cloud usage** | implemented | **BLOCKED — 0 successful calls** | `docs/deployment-verification.md`, `tests/test_qwen_provider.py` | account-level `RISK.RISK_CONTROL_REJECTION`; 144/144 models return 403 | organizers' sponsored key, or lift the hold. Then `make qwen-smoke` |
| 4 | Alibaba Cloud deployment | done, not deployed | **BLOCKED** | `deploy/`, `docs/deployment-verification.md` | FC not activated (needs SMS) | activate FC → `make deploy-fc` |
| 5 | Code proof URL (live) | ready | **BLOCKED** | `deploy/deploy_fc.sh` writes `docs/deployment-proof.json` | same as #4 | `make deploy-fc` prints the URL |
| 6 | Architecture diagram | done | locally verified | `docs/architecture.md` (Mermaid) | — | keep the "not live-verified" markers honest |
| 7 | Demo access / test credentials | partial | — | — | needs #4 | after deploy, mint a demo JWT |
| 8 | Under-3-minute video | script only | — | `docs/demo-script.md` | needs #3 + #4 | record after the journey runs live |
| 9 | Track selection | pending | — | — | — | founder selects on Devpost |
| 10 | Work completed during hackathon | done | locally verified | git log on `hackathon/qwen-cloud` | — | link the branch in the description |
| 11 | Test instructions | done | locally verified | `README.md`, `make test` | — | — |

---

## Component truth table

| Component | Status | Evidence |
|---|---|---|
| Auth (JWT), RLS + FORCE RLS, user scoping | **locally verified** | 17 adversarial tests vs real PostgreSQL 16 through the restricted `bruce_app` role |
| Durable intake: source → spans → tasks | **locally verified** | 15 tests, real Postgres; atomic + idempotent (DB-enforced) |
| Grounded extraction (span verification) | **locally verified** | hallucinated spans dropped; provider-independent |
| Qwen provider adapter | **integration-tested** | wire format asserted through the real client stack: model id, Bearer, `enable_thinking:false`, base64 image part, the literal word "json" |
| **Qwen live inference** | **BLOCKED — ZERO successful calls** | 144/144 models 403 incl. non-Qwen (`deepseek`, `glm`, `kimi`, `text-embedding-v3`) → account-wide hold, not entitlement |
| No silent provider fallback | **locally verified** | `503 provider_unavailable`; a test asserts the request is not retried elsewhere |
| No false completion | **locally verified** | 15 tests: read failures raise typed 415/422, never a 200 with zero findings |
| Google Calendar domain + verification | **integration-tested** | 30 tests: execute-once via caller-supplied id (409), read-back comparison, verified undo |
| Google OAuth (PKCE, one-time state, encrypted tokens) | **integration-tested** | 24 tests vs real Postgres + mocked Google |
| **Google Calendar live execution** | **BLOCKED** | no `GOOGLE_*` credentials issued |
| Mission contract (server-authorized actions) | **locally verified** | 16 tests |
| Messaging channel boundary | **implemented + tested** | 16 tests via FakeChannel; **no provider connected** |
| **iMessage / Linq** | **NOT FUNCTIONAL** | no adapter, no API contract in repo; no real message has passed through any provider |
| FC code package | **locally verified** | 45.5MB zip / 60.7MB base64; serves under an emulated Debian12/py3.11 contract; `/health` 200, `/v1/*` 401 |
| **FC deployment** | **BLOCKED** | `FC service is not enabled for current user` |
| One-command deploy | **locally verified (dry run)** | `make deploy-fc-dry` passes end-to-end; 12 tests on failure diagnosis |
| iOS app builds | **locally verified** | Xcode 26.4, `** BUILD SUCCEEDED **` |
| **iOS ↔ live API** | **MOCKED** | `BruceAPI.swift` is a skeleton; 7 view files still read `Mock.` data |
| **Live Activity / Dynamic Island** | **NOT IMPLEMENTED** | no ActivityKit code exists |

**Test suite: 342 passed, 12 skipped.** Every skip names its blocker and none is reported as a pass.

---

## What is still mocked (say this out loud in the video)

- The iOS app renders mock data; it is not yet wired to the live API.
- Dynamic Island / Live Activities do not exist yet.
- Messaging has a tested boundary but no connected provider — iMessage is **not** functional.

## Blocked on founder credentials

| Blocker | Who can fix | Effect if unfixed |
|---|---|---|
| Qwen account hold | organizers (sponsored key) or Alibaba CS | **submission ineligible** |
| FC activation (SMS) | founder, ~2 min | no live URL |
| Google OAuth client | founder, ~10 min | no live calendar execution/receipt |

---

## The honest position if Qwen never unblocks

Do **not** submit claiming Qwen works. The adapter is real, the wire format is tested, and the
blocker is documented with evidence — that is a truthful "built, blocked by account access" story.
A judge who runs one curl against a faked claim finds it in seconds, and that is worse than a
blocked submission. If the organizers issue a sponsored key, `make qwen-smoke` turns this row green
in about five seconds and the rest of the journey is already built.
