# Existing-Code Reachability Audit (Integration Invariant 5)

Audited against `main` @ `9456b20` (Merge PR #70: D-INT-4 migration-lane discipline). Every claim
below was verified by grepping for actual importers/call-sites in `engine/` — not by the module
existing on disk.

## What "reachable" means (and why file existence ≠ implementation)

A file existing, having tests, and being in a migration proves the code was *written*. It does **not**
prove any running process ever reaches it. The Core Differentiation Program reuses existing components,
so the load-bearing question is:

> Is there a real import/call chain from a **live entrypoint** to this component?

The live entrypoints are:

- `engine/bruce_engine/api.py` — the FastAPI app (all `/v1/*` routes), deployed on Cloud Run.
- `engine/bruce_engine/messaging_inbound.py` — the inbound gate (a normalized message → intake/runtime).
- `engine/bruce_engine/conversation_runtime.py` — the multimodal conversation brain (gated per-user).
- The worker path — `engine/bruce_engine/worker_api.py` → `worker.py` (Cloud Tasks drains `intake_jobs`).

`reachable_from_live_runtime = yes` requires a cited import line from one of those chains.
`DEAD — no importer` means I grepped for importers and found **only tests, or nothing at all**. A DEAD
component can still have green tests and a migrated table; that is exactly the trap this audit exists to
catch.

Two cross-cutting facts about the conversation cluster (they qualify several rows below):

- The whole conversation cluster (runtime/style/store/outcomes/handoff) ships inside the API image but
  is **flag-gated OFF per user** — `conversation_runtime.enabled_for()` fails closed unless the DB
  access gate returns an active `ProductionAccountEntitlement` or a live `StagingTestEnrollment`
  (`messaging_inbound.py:125-126`). No production user is enabled, so in prod the legacy static-ACK
  path runs instead.
- The reasoner is a **real** LLM (`OpenAIConversationReasoner`, `conversation_model.py:75`,
  `production_reasoner()` at `:101`), so the cluster is also **credential-blocked on `OPENAI_API_KEY`**.
  It ran for real in staging LEVEL B, but the end-to-end inbound→reply loop is **not live-verified**
  in prod (outbound delivery is additionally hardware-blocked: the relay Mac has SIP enabled → no
  IMCore outbound).

Legend for `reachable`: **LIVE** = reachable from an entrypoint; **GATED** = reachable but behind the
per-user conversation flag; **DEAD** = no non-test importer.

---

## Summary table

| Component | code_exists | tests | reachable_from_live_runtime | deployed | credential_blocked | mock_only | live_verified |
|---|---|---|---|---|---|---|---|
| `schema.Mission` (missions) | yes | `test_repositories_memory`, `test_postgres_integration`, `test_api` | **LIVE** — `api.py:96`→`repositories.py`; `intake_store.py:377` | yes | no | no | yes (at-most-once proven live) |
| `schema.MissionPhaseEvent` | yes | `test_api`, `test_postgres_integration` | **LIVE** — written `intake_store.py:327/386`, read `api.py:342` | yes | no | no | yes |
| `schema.TaskRow` (tasks) | yes | `test_intake_persistence`, `test_repositories_memory` | **LIVE** — `intake_store.py:206/430`, `repositories.py:229` | yes | no | no | yes |
| `schema.Approval` (approvals) | yes | none | **DEAD — no importer** | table only | n/a | n/a | no |
| `schema.Receipt` (receipts) | yes | none | **DEAD — no importer** | table only | n/a | n/a | no |
| `repositories.PostgresMissionRepository` | yes | `test_repositories_memory`, `test_postgres_integration` | **LIVE** — `api.py:68/96`, called `:161/168/280/322/351` | yes | no | no | yes |
| `contract.py` (`MachineState`/`VerificationState`) | yes | `test_contract` only | **DEAD — no importer** (test-only) | no | n/a | n/a | no |
| `api.py` mission endpoints | yes | `test_api` | **LIVE** — routes `:277/320/334/349` | yes | no | no | yes |
| `intake_store.py` | yes | `test_intake_persistence`, `test_async_intake_pg` | **LIVE** — `api.py:35`, `messaging_inbound.py:23`, `worker.py:26` | yes | no | no | yes |
| `tasks.py` | yes | `test_tasks` | **LIVE** — `api.py:47`, called `:809-813` (`/v1/tasks`, stateless) | yes | no | no | partial (stateless) |
| `school_connector.py` | yes | `test_school_connector_contract` | **DEAD — no importer** (only school-cluster peers) | no | no | no | no |
| `school_capability.py` | yes | `test_school_connector_contract` | **DEAD — no importer** (only school-cluster peers) | no | no | no | no |
| `school_store.py` (`sync_provider`) | yes | `test_school_rls_isolation`, `test_school_queries` | **DEAD — no importer** (only `school_queries`) | no | no | no | no |
| `school_queries.py` | yes | `test_school_queries` | **DEAD — no importer** (zero, incl. non-test) | no | no | no | no |
| `canvas_fake.py` | yes | 3 school tests | **DEAD — no importer** (only a docstring mention) | no | no | **yes (fake provider)** | no |
| `oauth_google.py` | yes | `test_oauth_google` | **DEAD** — only importer is `calendar_adapter` which is itself DEAD | no | yes (Google OAuth creds) | no | no |
| `briefing.py` (`compose_brief`) | yes | `test_briefing` | **LIVE** — `api.py:49`, called `:844` (`/v1/brief`) | yes | no | no | partial (stateless) |
| `conversation_style.py` | yes | `test_conversation_style` | **GATED** — `conversation_runtime.py:19`, `conversation_outcomes.py:36` | yes (gated) | yes (via runtime) | no | no |
| `conversation_store.py` | yes | `test_conversation_runtime`, `test_conversation_outcomes` | **GATED** — `conversation_runtime.py:14` | yes (gated) | no | no | no |
| `conversation_outcomes.py` | yes | `test_conversation_outcomes` | **GATED** — `conversation_runtime.py:14` | yes (gated) | yes (via runtime) | no | no |
| `handoff.py` | yes | `test_handoff` | **GATED + telemetry-only stub** — `conversation_outcomes.py:31`, called `:165` | yes (gated) | no | no | no |
| Multi-bubble (>1 outbound/inbound) | — | — | **does not exist** — every path enqueues exactly one | n/a | n/a | n/a | n/a |

---

## Mission kernel

**Live and load-bearing.** The reused mission persistence is genuinely wired:

- `repositories.PostgresMissionRepository` is imported (`api.py:68`), instantiated as `_mission_repo`
  (`api.py:96`), and called from routes: `create` (`:280`), `get_for_user` (`:322`), `list_for_user`
  (`:351`), `update_phase` (`:161`), `finish` (`:168/175`).
- Mission endpoints exist and are reachable: `POST /v1/missions` (`api.py:277`), `GET
  /v1/missions/{id}` (`:320`), `GET /v1/missions/{id}/events` (`:334`), `GET /v1/missions` (`:349`).
- `schema.Mission` (`schema.py:173`), `schema.MissionPhaseEvent` (`:188`), and `schema.TaskRow`
  (`:124`) are all written **and** read by live code (`intake_store.py`, `repositories.py`, `api.py`).
- `intake_store.py` is the core reused persistence path, reachable from three entrypoints
  (`api.py:35`, `messaging_inbound.py:23`, `worker.py:26`).
- `tasks.py` is reachable (`api.py:47`, called `:809-813`) but the `/v1/tasks` route is **stateless** —
  it computes from client-supplied input and persists nothing (see the api.py module docstring).

**Dead-but-looks-built inside the mission kernel:**

- **`contract.py` (`MachineState` / `VerificationState`) is DEAD.** It defines a full formal state
  machine (`contract.py:29+`) with terminal/recoverable states and display strings, but grepping for
  importers of `contract` / `MachineState` / `VerificationState` across `bruce_engine/`, `relay/`, and
  `scripts/` returns **nothing** — the only importer is `tests/test_contract.py:13`. The live mission
  status is a plain `phase` string (`MissionPhase` from `models.py`) on `Mission`/`MissionPhaseEvent`,
  **not** `contract.MachineState`. The verification-state / receipt-hash contract is unreached.
- **`schema.Approval` (approvals table) and `schema.Receipt` (receipts table) are DEAD.** Both are
  defined (`schema.py:200`, `:214`) and shipped in migrations, but grep finds **zero** non-`schema.py`
  references anywhere — not in live code, not even in tests. They are empty table stubs; the human-in-
  the-loop "approval" and cryptographic "receipt" story is schema-only.

---

## School domain — entirely DEAD from the live runtime

Confirmed by targeted grep: **none of `api.py`, `messaging_inbound.py`, `conversation_runtime.py`,
`worker.py`, or `worker_api.py` contains any reference to `school`, `canvas`, `oauth_google`, or
`calendar_adapter`** (grep returned empty). The domain forms a closed island whose only importers are
its own peers and tests:

- `school_queries.py` — **zero importers** (not even a non-test caller). Fully dead leaf.
- `school_store.py` — imported only by `school_queries.py:22`. Its `sync_provider()` function
  (`school_store.py:237`) has no live caller; only tests invoke it.
- `school_connector.py` — imported only by `school_store.py:29`, `school_queries.py:21`,
  `canvas_fake.py:21` (all in-island).
- `school_capability.py` — imported only by `canvas_fake.py:22`, `school_store.py:31`,
  `school_connector.py:33` (all in-island).
- `canvas_fake.py` — **no real importer at all**; the one grep hit (`school_queries.py:10`) is a
  docstring sentence, not an import. Instantiated only in tests. It is also a **fake/mock provider** by
  design.
- `oauth_google.py` — its only importer is `calendar_adapter.py:250/260` (local imports), and
  **`calendar_adapter.py` itself has zero importers** (dead). So `oauth_google` is stranded behind a
  dead bridge: DEAD from the live runtime, and additionally credential-blocked on real Google OAuth
  creds if it were ever wired.

The lone exception in this cluster is `briefing.py`, which is **not** part of the school island:
`compose_brief` is imported (`api.py:49`) and called (`api.py:844`) by `GET /v1/brief`. It is reachable
and deployed, but the route is **stateless** (the client hands back the task list each call).

> Bottom line for the Program: to reuse anything in the school/Canvas domain, it must be newly wired
> into a live entrypoint — today nothing imports it, so "we already have a Canvas connector" is true on
> disk and false at runtime.

---

## Humanity (conversation voice / memory)

Reachable only through the gated conversation runtime (`messaging_inbound.py:125` → `conversation_runtime`),
so all three rows are **GATED** (off in prod). The important honesty flags:

- **`conversation_style.py`** is reached (`conversation_runtime.py:19`, `conversation_outcomes.py:36`).
  `derive_profile()` **is** called live at `conversation_runtime.py:124`, but it produces an
  **ephemeral, in-memory `VoiceProfile`** recomputed from the recent-turns window on every turn. The
  base profiles are static (`DEFAULT_PROFILES` + optional `voice_profiles.yaml` override,
  `conversation_style.py:160`).
- **`conversation_store.py` does NOT persist a style/voice profile.** Verified end-to-end: the module
  persists `ConversationTurn` rows (user + assistant, with `text`, `intent`, `decision` JSONB) and
  `EventCandidate` rows — and nothing else. There is no `style_profile`/`voice_profile` table and no
  persist-style call anywhere (`grep style_profile/voice_profile` finds only in-memory `VoiceProfile`
  usage). So "Bruce learns and remembers your texting voice" is **not implemented**: the derived style
  is thrown away after each turn.
- **The multi-bubble behavior does not exist.** There is **no path that emits more than one outbound
  per inbound.** There are exactly two `messaging_outbound.enqueue` call sites in live code:
  `conversation_runtime.py:260` (in `_finalize`, whose own comment says *"enqueue EXACTLY ONE
  outbound"*, idempotent on `conv:{pmid}`) and `messaging_inbound.py:66` (the legacy static ACK).
  `enqueue()` (`messaging_outbound.py:44`) writes a single `OutboundMessageRow` and never splits text.
  Human-style "sends you 2-3 short texts" is unbuilt.

---

## Handoff / mission seam

- **`conversation_outcomes.py`** is reached (`conversation_runtime.py:14`, used `:78/86/96/189`) — GATED.
- **`handoff.py`** is reached via `conversation_outcomes.py:31` (`decide_handoff` called at
  `conversation_outcomes.py:165` inside `MissionHandoffHandler`). So the seam **does execute** when the
  runtime is enabled — but it is a **telemetry-only stub** (D-INT-3): the handler hardcodes
  `capability_supported=False`, always returns `Disposition.decline` with `reason="telemetry_only_stub"`,
  **creates no state, and never claims** (`conversation_outcomes.py:148-169`). The normal reply flow
  proceeds unchanged. The mission kernel is **not** yet wired to consume `HandoffDecision` /
  `authorizes_mutation`, so "a chat can start a durable mission" is scaffolded and measured but not
  functionally connected.

---

## Biggest "dead-but-looks-built" findings

1. **`contract.py` (MachineState/VerificationState)** — a complete mission state-machine contract with
   zero non-test importers. The live path uses a plain `phase` string instead.
2. **`approvals` + `receipts` tables** — defined and migrated, referenced by **no code at all** (not
   even tests). Pure schema stubs.
3. **Entire school/Canvas domain** (`school_queries`, `school_store`+`sync_provider`, `school_connector`,
   `school_capability`, `canvas_fake`) — a self-contained island with no entrypoint importer.
   `oauth_google` is dead-by-association (only importer `calendar_adapter` is itself dead).
4. **Conversation "voice memory"** — style is derived per-turn and never persisted; `conversation_store`
   has no style table. The adaptive-voice claim is runtime-ephemeral only.
5. **Multi-bubble replies** — no path emits >1 outbound per inbound; both live paths enqueue exactly one.
6. **`handoff.py`** — reachable but a deliberate telemetry-only no-op; the chat→mission seam is measured,
   not wired.
