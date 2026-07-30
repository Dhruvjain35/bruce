# Brain-spine handoff — semantic negation attachment

**Written 2026-07-29. Branch `brain-spine`, clean at `f0f3e85`. Nothing from this branch is deployed.**

---

## 0. One-paragraph situation

Bruce is an iMessage-native personal agent. A real founder transcript exposed that it had no durable
state: 22 turns produced 0 goals, it re-asked for a recipient given one turn earlier, an inline "this"
resolved to nothing, and it finally claimed "i can't send messages for you" while `tool_broker` reported
`gmail.send_message ok=True`. A "brain spine" was built and wired live to fix that. It works, except for
one remaining blocker: a turn that both approves the operation and negates something else is classified as
a withdrawal, so the send never executes. Four deterministic attempts failed. The fix is a semantic
attachment reader, gated so it only runs on genuine ambiguity.

---

## 1. Repository facts

| | |
|---|---|
| repo | `/Users/dhruvjain/bruce` (engine at `engine/`) |
| branch | `brain-spine`, clean at **`f0f3e85`** |
| main | `af3f62e` |
| python | 3.14, venv at `engine/.venv/bin/python` |
| full suite | `cd engine && .venv/bin/python -m pytest -q -p no:randomly` (~5 min) |
| staging | GCP `bruce-staging-2645`, region `us-central1`, services `bruce-api` / `bruce-worker` |
| deployed | `af3f62e`, digest `sha256:1d1d9d176b3e0606c8065b6ae1930f4958396ee67d8ead9de25a7c7618d4ebc7` |
| staging alembic head | `0030_claim_lineage` (branch has `0031_agent_run_status_check`, unapplied) |
| founder user | `e1e0fcd8-27df-5971-af30-85db54362d42` (`alpha_bridge`, owns the handle + a connected Google integration with calendar.events / gmail.send / gmail.readonly) |
| founder enrollment | **EXPIRED** at 2026-07-29 05:33Z — must be reissued before any live test |
| founder flags | `BRUCE_FOUNDER_ALPHA` and `BRUCE_SEMANTIC_RESCUE` **unset**; `BRUCE_FOUNDER_USER_IDS=e1e0fcd8…` is set (inert alone) |
| relay | device `3fa50a3d`, healthy, one active device |

**`dhruv-founder` (`4eb5dfe1-e5a3-567d-9b5a-a2af1e9b39f7`) is vestigial** — it holds a second connected
Google integration but no messaging identity and no access. The live founder account is `e1e0fcd8`.

---

## 2. What is DONE and committed on this branch

| commit | what |
|---|---|
| `01a5fa1` | capability truth (layer 1) + `goal_slots`, `transitions`, `turn_context` |
| `771a5d0` | `goal_runtime`, `continuation`, `turn_context_assembler`, status enforcement + migration 0031, `memory_finalize` |
| `e8468c5` | **the spine went LIVE** — `GoalHandler` in `default_handlers()`, continuation before routing |
| `544debb` | cross-tool acceptance suite (committed deliberately red) |
| `565024b` | `CalendarCreateExecutor`, accepted memory writes, linked execution runs |
| `0bf04af` | clause-level `directive_scope` (rebuilt from scratch) |
| `18b07d1` | stale acceptance docstring corrected |
| `f0f3e85` | narrowed the scope override in `continuation` |

### Architecture actually in place

- **One entry point**: `messaging_inbound.handle_inbound` → `conversation_runtime.handle`.
- **`GoalHandler`** leads `conversation_outcomes.default_handlers()` at priority 90. ONE generic handler —
  email and calendar differ only by slot kind, capability id and adapter.
- **Continuation before routing**: `continuation.resolve` runs before `fast_router`, and when it resolves
  the router is **skipped** (not merely ignored — re-routing would re-enqueue the original lane and could
  send twice).
- **Goal = `AgentRun`.** No `goals` table. Typed slots live in `AgentRun.goal` JSONB under key `"slots"`
  with provenance; a model guess cannot overwrite a user-stated value, a later user statement can.
- **State machine**: `contract.MachineState`, enforced by `transitions.propose_transition`, which every
  status write in `agent_run_store` routes through. Migration 0031 adds the first `CheckConstraint`.
  `succeeded` is reachable only from `verifying` with a verified read-back.
- **Two status vocabularies share `agent_runs.status`** and are deliberately NOT unified:
  `MachineState` (mission) vs `queued/running/completed/dead_letter` (scheduler lease).
  **`completed` IS NOT `succeeded`** — aliasing them would let `complete_background`, a mere write, reach
  the one state the subsystem protects.
- **Memory** has a caller at last (`memory_finalize`), writing aggregate style + verified outcomes only.

---

## 3. THE ONE BLOCKER — **CLOSED**. Kept below because the reasoning is the fix's specification.

Closed by the semantic attachment seam (`directive_scope`) plus one optional argument on
`user_action_boundary.evaluate`. What actually shipped, and how it differs from what section 5 predicted:

| | |
|---|---|
| the gate | `directive_scope.turn_is_ambiguous` — BOTH an approving and a refusing clause, judged with `decision_resolver.resolve_approval`. 25 of the 238 corpus turns (10.5%) open it; every plain yes and no, and `cancel-pending-10`, never do. |
| the reader | injectable `ScopeReader`. **The wired default is `DeterministicScopeReader`** (`interpret` restated as a reading), so the seam costs ZERO provider calls. `ModelScopeReader` exists and is reached only by `BRUCE_SCOPE_READER=model`. |
| the checks | `directive_scope.validate` — 11 typed refusals. An approval needs a clause that is really a clause of this turn, NAMES the operation, is the LAST clause that does, itself reads as approved, has verbatim spans, and clears a 0.75 confidence floor. A rejection needs far less, because it ends in "nothing runs". |
| the binding | `decision_resolver.trusted_digest`. A proposal cannot be spent against different words — not a joined message, not a later turn. |
| the authority | `user_action_boundary._scoped`, consulted ONLY on the `rejected` branch and only after the cancellation branch has had its say. `scope=None` leaves `evaluate` byte-for-byte what it was, which is why the corpus cannot move. |
| `unclear` | withdrawal when the pending operation is destructive, ambiguous otherwise. Both satisfy `blocks_execution()`. |

Measured after: boundary **281/36**, corpus **314/36** (24 categories unmoved, `_OVER_BLOCKED` identical
at 14/36), acceptance **10 passed / 6 failed**, founder **1 authorization, 1 send, 1 fetch-back, 1 receipt**.
`tests/test_semantic_scope.py` runs the whole corpus through the seam with the gate forced open in both
text shapes: 0 forbidden cases gain an affirmative, 0 lose a block.

The rest of this section is the original diagnosis, unedited.

`user_action_boundary.evaluate` classified this as `withdrawal`:

```
"YES WRITE IT AND SEND IT NO MORE QUESTIONS"
```

`decision_resolver.resolve_approval` reports only *that* a negation exists; `_ACTION_VERB` then matches
"send"; the pair reads as a same-breath retraction. **The polarity travels inside the returned
`UserActionBoundary` object**, so every consumer inherits it: `try_grant` refuses, `grant` refuses, and
`authorization_store.record_refusal` closes the student's outstanding authorizations. Fixing it in any one
consumer leaves the wrong polarity in the other two — it must be fixed in the boundary.

### Why four deterministic attempts failed — measured, not guessed

```
"YES WRITE IT AND SEND IT NO MORE QUESTIONS"
   'yes write it'         -> approved
   'send it'              -> approved
   'no more questions'    -> rejected      <-- vetoes under any "any clause rejected" rule

"skip it for now, just show me what u wrote"   (corpus case cancel-pending-10)
   'skip it for now'      -> rejected      <-- MUST veto
   'show me what u wrote' -> unrelated
```

Judged clause-by-clause with the boundary's own vocabulary these are **the same shape**: exactly one
`rejected` clause, no cancel/existing-entity signal. No rule over clause verdicts separates them.

The real difference is **referential**: in "skip it for now", *it* refers to the pending send. In "no more
questions", *questions* is a different object. Nothing deterministic in this repo knows that.

Attempt log (diffs preserved at `/tmp/uab-partial-f0f3e85/uab-v{1..4}.diff`):

| v | change | result |
|---|---|---|
| 1 | scope override on the boundary gates | founder ✓, **4 corpus failures** incl. a forbidden `calendar_delete` |
| 2 | + normalized text, inline quotes stripped, hard cancel/entity block | 3 corpus failures |
| 3 | + span-backed approval (spans must independently read as approvals) | 1 failure: `cancel-pending-10` |
| 4 | + shared clause splitter, per-clause veto | `cancel-pending-10` ✓, **founder regressed** |

v3 also achieved: **acceptance 10 passed / 6 failed**, founder confirmation green, corpus 313/314.
It is one case short and that case involves a forbidden write, so it was reverted.

### Other bugs found by reading, not by tests (all fixed, listed as a warning)

The suite was green through every one of these:

1. `agent_run_store._derive_guard_ctx` compared SLOT names (`recipient`) against TOOL arg names (`to`), so
   a fully collected email goal reported missing and **`executing` was unreachable** — no send could ever
   have run.
2. `memory_finalize.after_verified_outcome` had a caller and refused every write (`subject="self"` +
   `kind=episodic` → `NOT_USER_SPECIFIC`). "Memory is wired" was true of the call graph, false of the DB.
3. `directive_scope._NEG` spelled `don't` with a straight quote, so `don’t add it` showed **no negation**,
   "add" read as approval, and a refusal deleted a calendar event. This repo had already been burned by
   that exact character once.

**Budget for reading, not just running.**

---

## 4. Remaining acceptance failures: 10 passed / 6 failed (was 8/8; **C is closed**)

Run: `cd engine && .venv/bin/python -m pytest tests/test_acceptance_cross_tool.py -q`

**A — `calendar.create_event` not reachable from the goal seam (3).** `CalendarCreateExecutor` exists
(committed at `565024b`) but is not in `goal_handler._EXECUTORS`, so the seam declines with
`capability_has_no_executor`. Symptoms: every calendar turn declined; "move it to friday" reaches no
handler; a 4pm the student set becomes all-day.

**B — deterministic multi-goal disambiguation (3).** `goal_runtime.resolve_capability` returns `""` when
two kinds are open rather than guessing — correct, but nothing then disambiguates. With a newer calendar
goal open: a drafted subject reaches neither goal, an email confirmation reaches nothing, an inline reply
is answered by the model. Needs inline-reply and Decision-id targeting to beat recency.

~~**C — semantic negation attachment (2).**~~ **CLOSED** — see section 3.
`test_the_exact_founder_sequence…` and `test_a_negation_that_governs_something_else…` both pass.

Also open, lower priority:
- `continuation._amend_patch` reduces "a lot less formal" to "less formal" — degree modifier lost.
- `memory_policy` contradiction: `INFERRED_PROVENANCE` contains `system_derived` while
  `KIND_FLOORS[episodic]` sets `inference_allowed=False`, making `system_derived` episodic writes
  **structurally unreachable**. Reported, deliberately not patched.
- `test_a_negation_that_governs_something_else_still_reads_as_refusing_the_operation` has a misleading
  NAME (its assertions are correct; only the name is stale).

---

## 5. The fix to build — **BUILT**. Read section 3 for what shipped; this is what was specified.

**Pattern**: the model resolves what the words refer to; the backend decides whether that resolution is
safe enough to authorize. This is the fourth instance of an asymmetry the codebase already runs on —
`semantic_rescue`, `AuthorizationEvidence.request_span`, and `transitions` all work this way.

**Where it goes**: `directive_scope` already has an injectable-reader seam that was specified and never
wired — only the deterministic default was built. The reader plugs in **there**, not into
`user_action_boundary`, which stays a pure validator receiving a *proposal*.

**Trigger condition** (this is the fast-path guarantee, and it falls out of the measured data):
call the reader ONLY when the clause verdicts contain **both** `approved` and `rejected`.
- "YES WRITE IT AND SEND IT NO MORE QUESTIONS" → has both → reader runs.
- "skip it for now, just show me what u wrote" → rejected + unrelated, no approval → **never reaches the
  model**, stays deterministic.
- "yes", "send it", "no", "dont send it", "cancel it" → single-polarity → never reach the model.

**One refinement to the spec below**: `unclear` must resolve to **`withdrawal`, not a neutral polarity**,
whenever a pending destructive operation exists (`gmail.send_message`, `gmail.reply_to_thread`,
`google_calendar.delete_event` — see `authorization_evidence.DESTRUCTIVE`). `unrelated`/`unclear` does not
satisfy `blocks_execution()`, and `blocks_execution()` is what actually stops the downstream write. Without
this, the clarification and the send race.

**Acceptance pair is the #120 corpus, not the founder test.** Run the corpus before and after; if any of
the 24 adversarial categories moves, the fix is wrong. The founder test going green while the corpus stays
green is the only acceptable outcome. `cancel-plus-delete-06` and `cancel-pending-10` are the two with
teeth.

---

## 6. Commands

```bash
cd /Users/dhruvjain/bruce/engine

# the five gates, in order
.venv/bin/python -m pytest tests/test_semantic_scope.py tests/test_directive_scope.py -q
.venv/bin/python -m pytest tests/test_action_boundary.py tests/test_authorization_zero_call.py -q  # 281/36
.venv/bin/python -m pytest tests/ -q -k "authorization"                                            # 314/36
.venv/bin/python -m pytest tests/test_acceptance_cross_tool.py::test_the_exact_founder_sequence_ends_in_one_verified_send -q
.venv/bin/python -m pytest tests/test_acceptance_cross_tool.py -q                                   # target 10/6

# one corpus case alone
.venv/bin/python -m pytest "tests/test_authorization_zero_call.py::test_no_corpus_case_that_forbids_writes_ever_reaches_an_adapter[cancel-pending-10]" -q

# staging DB (proxy must be running on 5433; secrets via Secret Manager, never printed)
/opt/homebrew/bin/cloud-sql-proxy bruce-staging-2645:us-central1:bruce-staging-db --address 127.0.0.1 --port 5433
```

**RLS trap**: a plain `SELECT` under FORCE ROW LEVEL SECURITY returns **0 rows without erroring**. `users`
and `integrations` are invisible to both worker and admin sessions. Always compare a count against
`pg_stat_user_tables.n_live_tup` before reporting it. I asserted an empty environment twice from filtered
reads and was wrong both times.

**Deploy rules** (when it eventually ships): one immutable digest, deployed to both services;
`--update-env-vars` only — `--set-env-vars` would wipe 12 `secretRef` bindings; verify one revision each
at 100%, `/health`, `BRUCE_COMMIT`, and relay heartbeat. Migration 0031 must run from that exact digest.
Pause for explicit approval before any real Gmail send or Calendar write.
