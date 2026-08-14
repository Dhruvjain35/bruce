# VOICE_PIVOT_BASELINE

**Phase 0 deliverable.** Read-only audit. **No production code was modified to produce this document.**

**Produced:** 2026-08-13 · **Method:** direct repo reads, live GCP/Cloud SQL reads, and a 9-agent audit whose two highest-stakes areas were independently re-derived by a checker. Where the checker and the auditor disagreed, **the checker's correction is what appears below**, and the disagreement is named.

Every claim carries a file:line or a command. Where something is genuinely unknown, it is in RISKS, not asserted.

---

## VERIFIED CURRENT STATE

### Repo (items 1–4)

| | |
|---|---|
| branch | `feat/semantic-executive` |
| HEAD | `81050872141fb1ec4bf61c4a7f4587c2d361e708` |
| git status | **clean** — 0 uncommitted files |
| is `8105087` the baseline | **Yes.** HEAD *is* `8105087`; 0 commits since |
| `main` | `218cc42` — **the branch is 13 commits ahead and unmerged** |

The reported baseline is exact. Note the branch has never been merged to `main`, so `main` is 13 commits stale.

### Staging (items 5–9)

```
/health   {"status":"ok","commit":"8105087","env":"staging"}      /ready 200
digest    sha256:91edabd67754c7da83fe2ab04af98069c21599b792c84d283180c4f33eda9b1a
          IDENTICAL on bruce-api and bruce-worker
revisions bruce-api-00067-cmz · bruce-worker-00059-q5f
head      0034_known_people_row_security
          semantic_shadow_jobs PRESENT (0033 is 0034's ancestor)
          shadow_reconciliation ABSENT (0035 correctly not deployed)
```

**Flags (item 8).** `BRUCE_ROUTER_SEMANTIC` **absent**, `BRUCE_SEMANTIC_SHADOW` **absent**, `BRUCE_ROUTER_STAGE1` **absent**. `BRUCE_ROUTER_AUTHORITY_PCT=50`, `BRUCE_FOUNDER_ALPHA=1`, `BRUCE_SEMANTIC_RESCUE=1`, `BRUCE_FOUNDER_ALPHA_KILL=0`. api 23 env vars / 8 secretKeyRefs; worker 13 / 4.

**Founder access (item 9).** `allow=True source=staging` — verified through `conversation_access` itself, not from the write.

```
enrollment  capability=conversation  environment=staging  expires 2026-08-20T02:12:30Z
production_account_entitlements = 0   (n_live_tup=0 — a real zero, not a blinded read)
live_staging_enrollments = 1, all founder
kill switch = ('conversation','local',False)  — retained, not engaged
```

**Every reported handoff fact verified true.**

### What does NOT exist (Section D unknowns)

- **No cloud voice ingress.** No STT, no TTS, no audio anywhere.
- **No App Intents / AppShortcut / SiriKit / Speech / AVAudio code.** Zero.
- **An iOS app DOES exist** — 3,203 files, 30 Swift sources, `ios/Bruce.xcodeproj`. It calls exactly `/v1/intake` and `/v1/missions` (`ios/Sources/BruceAPI.swift:117,141`). *(I first reported "no iOS project" — that was my error: the `find` ran from `engine/`. Corrected.)*
- **Models:** all OpenAI `gpt-5.4-mini` via `pydantic_ai` (`llm.py:34-39`). Featherless/Qwen exists but is offline-only and flag-gated OFF (`llm.py:60`). **No Assistants API anywhere** — the deprecation is a non-issue.

---

## ARCHITECTURE MAP

### Current (item 10)

```
Mac relay (imsg + chat.db)
  │  POST /v1/relay/inbound        api.py:621   auth = current_relay_device (DEVICE bearer, not user JWT)
  │    ├ is_from_me echo guard     api.py:624
  │    ├ tapback/unsent/edited     api.py:632-648  → conversation_graph, returns early
  │    └ builds InboundMessage     api.py:666   channel HARDCODED self_hosted_imessage
  ▼
messaging_inbound.handle_inbound   api.py:673   ← the ONLY call site in bruce_engine
  ├ user_id ← (channel, handle)    messaging_inbound.py:162   ▲ the only way a turn gets a user today
  ├ link-code redemption lane      messaging_inbound.py:178-216
  ├ conversation_graph ingest      messaging_inbound.py:222
  ├ access gate (user, capability) messaging_inbound.py:232   ← no channel term
  ▼
conversation_runtime.handle(channel, msg, *, user_id, reply_target)     conversation_runtime.py:1022
  ├ ch, ident, pmid = msg.channel.value, msg.channel_identity, msg.provider_message_id   :254
  ├ claim_inbound_turn  INSERT..ON CONFLICT uq_turn_msg_role            :286
  ├ semantic executive / router → context compiler → reasoner
  ├ handlers → goals (AgentRun) → Decision → AuthorizationEvidence
  ├ execution_gate → MutationGateway → adapter → verified read-back
  ▼
_finalize → messaging_outbound.enqueue   channel HARDCODED   :1007
  ▼
outbound_messages row → relay pull-claim (no channel predicate) → imsg send
```

**`POST /v1/relay/inbound` is the only route that reaches the conversation core.** `/v1/intake` — which the iOS app already calls — is JWT-authed, returns 202, and defers to a worker, but lands in the **legacy mission/extraction path**, never in the semantic/goal core.

### Proposed minimal transition

```
ANY trusted ingress (voice / iOS App Intent / text / relay / test)
  │   POST /v1/turns   auth = current_user (JWT)         ← NEW route, ~20 lines
  ▼
submit_turn(user_id, source, trusted_text, source_turn_id, metadata)   ← NEW thin wrapper
  │   builds InboundMessage, no handle lookup (user_id already proven)
  ▼
messaging_inbound.submit_turn_for_message()   ← PURE MOVE of messaging_inbound.py:218-234
  ▼
conversation_runtime.handle(...)              ← UNCHANGED. Already takes user_id explicitly.
  ▼
… entire existing core, semantic → goal → Decision → execution → verification … UNCHANGED
  ▼
messaging_outbound.enqueue(channel=msg.channel, …)   ← one token changed
  ▼
outbound_messages row, claimed BY CHANNEL → relay OR voice adapter OR readback
```

**No rewrite is warranted.** The core boundary is already `handle(channel, msg, *, user_id, reply_target)` — an authenticated user id, a canonical message, an opaque reply target. Two files already drive that exact entry point with a synthetic message and no relay at all (`tests/test_conversation_runtime.py`, `scripts/verify_deploy_at_0034.py:132`). **That is the voice-ingress template, and it already runs green.**

---

## REUSABLE UNCHANGED

Verified free of any channel, provider, handle or iMessage reference.

| Component | Evidence |
|---|---|
| **Semantic executive / triage / contracts** | zero channel/provider refs; `interpret(context)` is duck-typed on trusted text + reachable operations + two state flags |
| **Continuation** | `continuation.py` — operates on goals and slots, not transports |
| **Durable goals / AgentRun** | `agent_run_store`, `goal_runtime`, `transitions` — no channel concept |
| **AuthorizationEvidence** | binds `(user_id, provider, operation, arguments_fingerprint, trusted_message_id, conversation_id)`. No channel, no phone number |
| **execution_gate / MutationGateway** | re-derives the fingerprint from what the adapter is about to send; surface-agnostic |
| **Provider verification / Receipts** | `ToolResult.verified` set only by an independent read-back (`matches_sent`); every layer reads it verbatim |
| **Async worker substrate** | `worker_api.process` drains 3 lease-based queues; Scheduler every 60s; Cloud Tasks is a redundant wake only, so a lost task self-heals |
| **Canonical turn idempotency** | `claim_inbound_turn` → `INSERT … ON CONFLICT` on `uq_turn_msg_role` (user_id, channel, provider_message_id, role). **Structurally already `submit_turn(user_id, source, source_turn_id)`** |
| **`consequential_payload`** | the typed voice/payload boundary — surface-neutral by construction |
| **RLS** | `user_session` sets `app.user_id` transaction-locally; a JWT `sub` is a *stronger* identity than a phone number, so RLS gets safer, not weaker |

**`conversation_turns.channel` is `String(32)` with no CheckConstraint** (`schema.py:1133,1145`). A second source needs **no migration**.

---

## MUST BE GENERALIZED

Ordered by whether they *break* a second source or merely constrain it.

### A. Correctness-breaking (a second source misbehaves immediately)

1. **Outbound claim has no channel predicate** — `messaging_outbound.py:141-149`, `api.py:714`. The first iMessage relay to poll **claims the voice row and speaks it into iMessage.** `RelayDevice.channel` already exists (`schema.py:805`, defaulted).
2. **Hardcoded egress channel** — `conversation_runtime.py:1007` asserts `self_hosted_imessage` though `ch = msg.channel.value` is already in scope at `:254`. Same defect at `messaging_inbound.py:116`, `messaging_outbound.py:208`, `notifier.py:50`.
3. **Outbound idempotency key omits channel** — `conversation_runtime.py:1008` `f"conv:{pmid}"` against globally-unique `uq_outbound_idem` (`schema.py:744`), and `enqueue` dedupes by **check-then-act** SELECT (`messaging_outbound.py:118-121`), not atomically. Two sources with the same `source_turn_id` **silently swallow the second reply**.
4. **The safety floor fails OPEN on an unknown channel** — `messaging_outbound.py:77`: `if channel_value not in _PLAIN_TEXT_CHANNELS: return text`. An unlisted channel **bypasses `PROHIBITED_PHRASES` and `enforce_no_dashes` entirely**, in the function whose own docstring calls itself the floor with "no bypasses". Same shape at `technical_render.py:31`. *This is a safety regression, not a rendering nicety.*
5. **Notifications are hardwired to iMessage** — `notifier.py:50,93-99`. A voice-only user has no `MessagingIdentity`, so `RelayNotifier` raises `NotifierUnavailable` and **the durable mission never reports back** — precisely the feature the pivot is built on.

### B. Semantics that must move (the expensive ones)

6. **`conversation_id` IS a phone number.** `conversation_runtime.py:254` sets `ident = msg.channel_identity`, passed as `conversation_id` into goals (`:838`), evidence, and telemetry (`turn_trace.py`). A transport identity is embedded in the **durable consent layer**, with a typed denial `WRONG_CONVERSATION`.
7. **History and goals are partitioned by `(channel, channel_identity)`** — `conversation_store.load_recent_turns` (`:136-138`) and `_open_goal_candidates`. **The auditor called `channel`-in-the-key "free namespacing"; the checker corrected it: the same key also partitions.** So a second source does **not** automatically satisfy Phase 1's *"the existing semantic/continuation/goal path remains ONE path."* Within-surface behaviour is correct; cross-surface continuity is a product decision, not an accident.
8. **The inline-reply primitive is the core's referent-binding mechanism** — consumed by seven semantic modules. **The auditor classified iMessage reply/quote as TRANSPORT_ONLY; the checker overturned that.** Voice has no quote block, so `decision_resolver.trusted_reply_text` / `strip_inline_quotes` become **no-ops** — safe, but the trusted/untrusted split for voice must come from somewhere else.
9. **`is_from_me` is SEMANTIC, not plumbing.** The mechanism is imsg-specific; the invariant — *the agent must not consume its own output as input* — is universal, and a voice loop (TTS → ASR) needs it more urgently than iMessage did.
10. **`msg.is_group` is a hard gate on the entire runtime** (`conversation_runtime.py:261`) — a privacy semantic wearing a transport field.
11. **The core never returns Bruce's words synchronously.** `InboundOutcome` (`messaging_inbound.py:65-86`) carries status/ids and **no reply text**. The largest single omission for a voice surface.

### C. Vertical naming (rename, not redesign)

Education leaks into core contracts in exactly **six** places: `IntentKind.educational_help`, `ResponseType.tutoring`, `Family.coursework` (**inert** — maps to no capability, always derives `family_not_live`), its mirror in the triage `Literal`, `RetentionPolicy.school_year`, and 2 of 10 `PROFILE_REGISTRY` entries. `GoalKind`, `OperationFamily`, `Mode`, `MemoryKind`, `TurnRole` and `tool_registry` are **already vertical-neutral**.

Prompt text names the surface *and* the vertical in one line — `conversation_model.py:53`: *"You are Bruce, a student's assistant that lives in iMessage."* That is **data, not code**. But `conversation_style.py:58-61,153` encodes texting affordances as **live deterministic code** (`lowercase`, `emoji_ok`, `avg_bubble_chars=200`).

**The school island claim is TRUE** — `school_queries`/`school_store`/`school_connector`/`school_capability`/`canvas_fake` have zero non-test importers.

---

## LEGACY RELAY-ONLY

Transport-only, never reaches the core (`bruce_engine` imports no module from `engine/relay/`):

- `engine/relay/*` — the Mac client
- `/v1/relay/register`, `/self-revoke`, `/heartbeat`, `/upload`, `/outbound/claim`, `/outbound/{id}/ack`
- `relay_auth.py`, `relay_control.py`, `relay_uploads.py`, `relay_devices` / `relay_control` tables
- imsg event shapes: tapbacks, unsend, edited, `chat_guid`, `service`

**Caveat the checker raised:** "cleanly quarantined" overstates it. `engine/relay/*` is never imported, but the *server-side* relay surface is interleaved with product paths in `api.py` and `messaging_outbound.py`, and the outbound **pause switch is environment-global, not per-channel** (`relay_control.py:95`, `schema.py:844`) — pausing the relay would pause voice too.

Keep it. It costs nothing, and it is the only working end-to-end proof of the durable outbound contract.

---

## DO NOT TOUCH

- `claim_inbound_turn` / `uq_turn_msg_role` — the exactly-once invariant, mutation-proved
- `execution_gate.require`, `MutationGateway`, `authorization_evidence.fingerprint` — 314-case corpus
- `consequential_payload` and its typed boundary
- Provider read-back verification / `ToolResult.verified`
- `gate_outbound_text`'s rules **for Bruce-authored text** (add voice to the set; never widen the bypass)
- The 14-gate manifest and `scripts/mutation_proofs.py`
- `0035` — leave undeployed

---

## PHASE 1 MINIMAL DIFF

**`submit_turn` already exists in two halves; neither needs rewriting.** The idempotency half is `claim_inbound_turn`. The orchestration half is `conversation_runtime.handle`, which already takes `user_id` explicitly. What is missing is a **route and a wrapper**, not an architecture.

| # | Edit | Size |
|---|---|---|
| 1 | `messaging.py:46-53` — add one `ChannelKind` member. **Do not call it `voice`** — see RISKS | 1 line |
| 2 | `messaging_inbound.py:218-234` — **pure move** into `submit_turn_for_message(user_id, msg, *, channel, reply_target)`; `handle_inbound` calls it | move only |
| 3 | `messaging_inbound.py` — add `submit_turn(user_id, *, source, trusted_text, source_turn_id, metadata, conversation_id)` building an `InboundMessage`; **no handle lookup** | ~12 lines |
| 4 | `api.py` — `POST /v1/turns`, `Depends(current_user)`. **Validate `source_turn_id` as a UUID in this route** — that one check closes three cross-tenant collisions below | ~20 lines |
| 5 | `conversation_runtime.py:1007` — `ChannelKind.self_hosted_imessage` → `msg.channel` | 1 token |
| 6 | `conversation_runtime.py:1008` — `f"conv:{pmid}"` → `f"conv:{ch}:{pmid}"` | 1 line |
| 7 | `messaging_outbound.py:133-152` + `api.py:714` — add `AND channel = :ch`; pass `device.channel` | ~3 lines |
| 8 | `technical_render.py:31` + `messaging_outbound.py:44` — add the new channel to **both** frozensets. **Safety, not cosmetics** | 2 lines |
| 9 | Reply readback **in the route only** — `SELECT text FROM outbound_messages WHERE idempotency_key = 'conv:<ch>:' || source_turn_id`. Reuses the row that already passed the gate; zero core edits | ~5 lines |

**Deliberately NOT in Phase 1:** `conversation_id` generalization (a stable opaque string gives correct within-surface behaviour immediately; cross-surface continuity is a product decision), the notifier, the attention contract, async submission, and any vertical rename.

**Proof obligation:** `tests/test_inbound_turn_claim.py:220-255` already proves the duplicate-execution invariant with two concurrent `handle` tasks. Extend it with a **two-source** case — that is exactly Phase 1's *"a test adapter and the existing messaging adapter create equivalent durable state."*

---

## RISKS / UNKNOWNS

**Cross-tenant collisions that a second source triggers.** Three global unique constraints are not scoped by user: `uq_outbound_idem` (`idempotency_key` alone, `schema.py:744`), `uq_conv_msg_provider` (`provider, provider_message_id`, `schema.py:1182` — whose upsert **returns another user's node id**), `uq_inbound_provider_msg` (`channel, provider_message_id`, `schema.py:695`). A UUID `source_turn_id` makes collision improbable; **it does not make it impossible.** Untested.

**No rate limit, throttle, or concurrency cap on any ingress.** `grep` for `rate_limit|throttle|429` in `api.py`/`auth.py` returns nothing. Today the relay is the natural limiter. An open JWT route removes it.

**The turn runs inline in the HTTP request** (`api.py:673`) with a 22s reasoner budget × 1 retry. Whether that survives a spoken-latency deadline, or a Cloud Run request timeout, **cannot be read from source** — it needs a live measurement. `turn_trace.py` exists precisely to produce that baseline and has never been run for it.

**The semantic executive is not wired to the production snapshot.** `semantic_executive.interpret` reads attributes `turn_context.TurnContext` does not have (`semantic_executive.py:476-484` vs `turn_context.py:122-144`). Its only live population is the shadow. **Whether it understands speech at all is unanswerable by reading.**

**Naming collision — "voice" already means persona here.** 50 occurrences across `bruce_engine`, all meaning style/register: `conversation_style.py:1` *"Bruce Voice OS"*, `VoiceProfile`, `voice_profiles.yaml`. A `ChannelKind.voice` would read as *persona* to every existing reader. Suggest `spoken` or `app_voice`.

**ASR output vs. deterministic text layers.** `_CHATTER_RE`, `_SEND_INTENT`, `trusted_reply_text`, `strip_inline_quotes` are calibrated on typed, punctuated phone text. An unpunctuated transcript may route differently. Measurable only with real transcripts.

**Audio has no representation.** `AttachmentKind` is a closed 3-member enum and `api.py:657-659` **silently drops unknown kinds**. Fine for transcript-only Phase 1; blocks "keep the recording".

**A second source contaminates the shadow measurement.** `semantic_shadow.intake` is unconditional per turn (`conversation_runtime.py:591`); a second channel enters the same sample the authority decision depends on.

**No CI gate asserts "exactly one inbound path."** Adding a second ingress has no structural guard.

---

## STOP

Phase 0 complete. **No production code modified.** Awaiting approval before any Phase 1 edit.
