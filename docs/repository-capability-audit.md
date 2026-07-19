# Repository → Capability Audit (v1)

Honest mapping of the code at `main` `8bec4a5` to the capability registry. Deliverable 2 of the
universe master prompt. **Nothing is marked `implemented`/`deployed`/`live_verified` without code +
evidence.** Where a claim rests on a doc rather than a repo fact, it is labelled *(doc-asserted)*.

## Real, working, deployed
| Capability ID | Evidence (file/refs) | Status | Live? |
|---|---|---|---|
| CAP-MSG-001 relay transport | `engine/relay/*`, `/v1/relay/*` on staging | deployed | NO (dry-run pending) |
| CAP-MSG-002 attachment transport | `relay/imsg.py` `watch.subscribe{attachments:true}`, `relay/relay.py` | deployed | fake-verified only |
| CAP-MSG-003 account linking | `messaging_store.py`, `scripts/create_link_code.py` | **live_verified** | YES (a real handle linked live) |
| CAP-MSG-004 async intake | `intake_store.py`, `worker_api.py`, `task_dispatch.py` | deployed | — |
| CAP-CONV-001 conversation router | `conversation_runtime.py` (flag OFF) | deployed | NO (flag off) |
| CAP-CONV-002 reasoner | `conversation_model.py`, `conversation_contract.py`, `llm.py` | deployed | staging LEVEL B ran real gpt-5.4-mini |
| CAP-CONV-003 tutoring | runtime branches + reasoner academic boundary | deployed | LEVEL B worksheet→tutoring |
| CAP-CONV-004 event candidate | `EventCandidate`, `_is_event`, `event_saved_calendar_unavailable` | deployed | LEVEL B; never claims "added" |
| CAP-CONV-005 failure recovery | reasoner timeout/retry, runtime fallback | deployed | LEVEL B timeout + unreadable |
| CAP-VOICE-001/002 Voice OS + guard | `conversation_style.py`, `product/*.yaml` | deployed / implemented | — |
| CAP-MISSION-001 durable mission + at-most-once | `Mission`/`mission_phase_events`, `messaging_outbound` lease+ledger | deployed | at-most-once proven live (resend incident) |
| CAP-PRIV-001 FORCE-RLS isolation | migrations 0002/0011; `test_postgres_integration` 05/05b/08 | deployed | **live_verified** (two-user denial on staging DB) |
| CAP-PRIV-002 content-free logs | `relay.py`, runtime logging | deployed | — |

## Partial
- **CAP-OPP-001 opportunities/outreach** — real discovery/drafting code exists from the research wedge
  (`engine/bruce_engine/discovery.py`, `drafting.py`), but it is **not wired into the messaging brain**
  and not exposed as a mission the conversation runtime can start. Status `partial`.

## Reusable primitives already in the repo
- Provider-neutral model seam (`llm.py` role accessors + `pydantic_ai`; `intake_providers.py`).
- Durable job/queue with lease + at-most-once ledger (`intake_jobs`, `outbound_messages`, relay ledger).
- FORCE-RLS + `user_session`/`worker_session` context (every user table).
- Evidence/provenance (`sources`/`source_spans`; `EventCandidate.provenance`).
- Idempotency conventions (unique idempotency keys; per-message dedup).
- Fake-imsg + fake-reasoner test harnesses.

## Migrations / schema
Head **`0011_conversation_runtime`**. 0001 create_all → 0002 RLS → 0003 idempotency → 0004 OAuth →
0005 intake_jobs → 0006 messaging → 0007 relay_devices → 0008 outbound.to_handle → 0009 relay_uploads
→ 0010 link_attempts → 0011 conversation_turns + event_candidates (tenant_isolation RLS). Deployed to
staging (migrate job, applied once).

## Mock-only / not real
- **iOS surfaces** (Home/Missions/Dates/Decisions/You) are largely mock (`ios/Sources/MockData.swift`).
  iOS is out of scope for the current messaging-first bites.

## Blocked / credential-gated
- **Canvas adapter (CAP-CANVAS-001)** — blocked on the founder's Canvas OAuth credentials.
- **Live iMessage** — blocked on the dedicated-Mac dry-run (imsg `message.get` is absent, so delayed
  attachment re-resolution must be re-verified; see the LEVEL C audit).
- **Sign in with Apple** — not verified from a real signed app (Apple Developer account gate).

## Security gaps / notes
- imsg `{attachments:true}`/watch event **field names** are assumptions until the dry-run.
- relay `get_message` (`message.get`) is **inert against imsg 0.13.1** (method not found); degrades to
  `attachment_unavailable`, and the give-up path checkpoints — a delayed attachment arriving after the
  give-up window could be deduped. Needs a real re-fetch mechanism or a second-watch-push design.
- The conversation runtime is flag-gated OFF; the legacy static-ACK path remains active until enabled.

## Not marked implemented despite existing scaffolding
- Approvals/verification tables exist (`approvals`, `receipts`) but the conversation runtime does not
  yet drive an approval flow → CAP-APPROVE-001 / CAP-VERIFY-001 are `planned`, not `implemented`.
- `calendar_proposals` exists but is not the event-candidate queue and has no calendar write → CAP-CAL-001 `planned`.
