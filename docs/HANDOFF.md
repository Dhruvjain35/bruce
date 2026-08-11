# BRUCE — TECHNICAL CONTINUATION HANDOFF

**Written:** 2026-08-10, ~21:30 UTC
**Repo:** `/Users/dhruvjain/bruce` · remote `https://github.com/Dhruvjain35/bruce.git`
**For:** a Claude session with zero prior context that must continue building without asking the founder to reconstruct history.

---

## HOW TO READ THIS DOCUMENT

Every claim below is tagged with one of six categories. **Do not promote a claim from one category to another without re-running its verification command.**

| Tag | Means |
|---|---|
| `[DEPLOYED FACT]` | Observed in the live staging environment on 2026-08-10 with the command shown. |
| `[CERTIFIED]` | Passed its real verification gate on this machine on 2026-08-10, but is **not** live. |
| `[UNCOMMITTED]` | Exists only in the working tree. **Not done. Not committed. Not deployed.** |
| `[DEFECT]` | A proven bug or architectural failure, with the evidence that proves it. |
| `[TARGET]` | What Bruce is supposed to become. **Not current capability.** |
| `[UNVERIFIED]` | Nobody has checked. Includes the command that would settle it. |

Two standing rules from prior sessions, both learned the expensive way:

1. **A green suite has never caught a serious Bruce defect.** Every one was found by reading or by a live turn. See `CONTRIBUTING.md:8-20` for the table of eight.
2. **A read that proves a ZERO must first prove it can see rows.** Several tables are `FORCE ROW LEVEL SECURITY`; a `SELECT` under a session that has not set `app.user_id` returns **0 rows without erroring**. The session variable is **`app.user_id`** — not `bruce.user_id`. Compare against `pg_stat_user_tables.n_live_tup`, which RLS does not filter, or abort.

---

# §0 — IDENTITY AND WORKING-TREE FINGERPRINT

Verify all of this in the first two minutes. If any line disagrees with the repo, **this document is stale and you must say so before editing anything.**

```
branch            feat/semantic-executive
HEAD              4cb5ded9be0a83dc91b2d5fc4f37d531d07e8717
HEAD subject      fix(semantic): the language suite was measuring latency and my own rate limit,
                  not understanding
HEAD date         2026-07-31 13:52:36 -0500
upstream          NONE  (no upstream configured for this branch — it has never been pushed)
remote            origin  https://github.com/Dhruvjain35/bruce.git
stashes           none
```

**Tracked-diff fingerprint** (`git diff | git hash-object --stdin`):

```
ff44cbc1b60c55dba5de52b8fdc55c967b345ff6
```

If that hash differs, the working tree has moved since this document was written and §3 is no longer exact.

**Tracked modifications — 19 files, +3043 / −159:**

| blob (worktree) | file | Δ |
|---|---|---|
| `fd43eb18e1a656c33e327b3524449e32708629f1` | `engine/bruce_engine/conversation_runtime.py` | +138 |
| `d3692e92f1b6f10bc321257325fff4a050492471` | `engine/bruce_engine/conversation_store.py` | +37 |
| `09359656ce5d38107f6b1b440d5a27068ee7eb33` | `engine/bruce_engine/schema.py` | +107 |
| `412eea18f79285f88776db14c2fcb1b0bc3f37bf` | `engine/bruce_engine/semantic_contracts.py` | +9 |
| `3985747e65c37d448e7fd91d18fd7656c8cbee5d` | `engine/bruce_engine/semantic_executive.py` | +107 |
| `b61666120aaecac8efefcb28ae1e5a7b41945da7` | `engine/bruce_engine/semantic_shadow.py` | **+2272** |
| `ac45018efab1be26c978021b3c9157be64123b4b` | `engine/bruce_engine/semantic_triage.py` | +9 |
| `965a6695a7a3b264b5e2b0892658515eaf3da483` | `engine/bruce_engine/tool_broker.py` | +59 |
| `780f364ad7c8e7c97325d4e9c8d33e3d15573035` | `engine/bruce_engine/worker_api.py` | +101 |
| `22729ff2c70d873b1f5e65180aff29c98156bed9` | `engine/ci/gates.json` | +22 |
| `55e01a6ae511b532801ac30266e8bbfebda474e8` | `engine/eval/language/harness.py` | +62 |
| `397cc800567488dc63439fb3ed5822e687f5b9c5` | `engine/eval/language/shadow_eval.py` | +10 |
| `6fbb70c3afcd3a5b27034095ae2a3caca66ff23b` | `engine/migrations/lanes.py` | +19 |
| `a19328564045b9c66dd968a3bf8c8d6325f8cbc9` | `engine/scripts/run_gates.py` | +48 |
| `91f0df86ad861957de002bd9ac8bf862a22289d0` | `engine/tests/test_client_reuse.py` | +14 |
| `77e4b5355ef9e37a7a769bd41cb27c21fed4403b` | `engine/tests/test_language_generalization.py` | +52 |
| `35182806481b8e083cb0b67db7d3eb911e621650` | `engine/tests/test_migration_rls_context.py` | +4 |
| `7b71e226acbfc4dfee840ef794019942ddc58446` | `engine/tests/test_postgres_integration.py` | +65 |
| `d19131df6a64e6151f396bc7f97c917726c0bcbb` | `engine/tests/test_tool_broker.py` | +67 |

**Untracked — 19 files, all new, none committed:**

| blob | file | lines |
|---|---|---|
| `27c6745e4c9e836511d99c039b72a3aad29699ff` | `engine/migrations/versions/0033_semantic_shadow_jobs.py` | 243 |
| `71e2ac392394b9d349fc000fd7f270f7256dad1f` | `engine/migrations/versions/0034_known_people_row_security.py` | 64 |
| `e8103faee4f59acad1546e5e832aa3dd1adef3ba` | `engine/migrations/versions/0035_shadow_reconciliation.py` | 380 |
| `9068baf2979669aad891ea20d78253f24252389f` | `engine/scripts/mutation_runner.py` | 1402 |
| `1b7c85cd15729e0f7eb5d9967e7c8bab638410f3` | `engine/tests/test_migration_0033_semantic_shadow_jobs.py` | 442 |
| `d98da2c030cf88b7f2a9952be5b3fe5ba232f937` | `engine/tests/test_semantic_shadow_pg.py` | 174 |
| `d1cff8e7679f59329f7f00352279cea6e785f40a` | `engine/tests/test_shadow_canonical_turn_key.py` | 312 |
| `eabf79d28a9eeff9e465965312f217746fcc5845` | `engine/tests/test_shadow_capability_denial.py` | 534 |
| `224b7ae9316fabb619ea3e8850cfbd5b0811597b` | `engine/tests/test_shadow_health.py` | 444 |
| `fff53beecfc22493be787cc71587215e69ef439e` | `engine/tests/test_shadow_intake_coverage.py` | 434 |
| `8cc85e825174505552e093b6eb7e99565029c145` | `engine/tests/test_shadow_isolation.py` | 472 |
| `f9d558bad05dfaa6f710ffa2bd68b5b100a89110` | `engine/tests/test_shadow_lease_fencing.py` | 293 |
| `4884d10788b58fa5b9c9fe2cc868f40627bb7481` | `engine/tests/test_shadow_population_privilege.py` | 882 |
| `6d50318c0f7db221482ed01cf00b11d506d26282` | `engine/tests/test_shadow_privacy.py` | 282 |
| `02d8b5774edbcc5899599fdb130ddcfb59305a73` | `engine/tests/test_shadow_reconciliation_population.py` | 665 |
| `691e4ba984c57fadf6ae538fab5720a2b80b5133` | `engine/tests/test_shadow_retry_bound.py` | 318 |
| `fd35769e9a684e3d22e5f73b3515b9e623d5e975` | `engine/tests/test_shadow_retry_durability.py` | 132 |
| `b6cc792cd1451b05be4bd246d0943e4cf6a585d7` | `engine/tests/test_shadow_retry_state_pg.py` | 281 |
| `6e8914d5e097ca913b083f8ba33a6c5b1ffef522` | `engine/tests/test_shadow_worker_metrics.py` | 300 |

**Git worktrees currently attached** — four are leftovers from stalled agent runs. They are not part of the current work; do not commit from them.

```
/Users/dhruvjain/bruce                                4cb5ded  [feat/semantic-executive]   ← the real tree
/Users/dhruvjain/bruce/.claude/worktrees/a4           ea63c1d  [bite15-a4-mac-installer]
/Users/dhruvjain/bruce/.claude/worktrees/a4patch      ecd4b03  [bite15-a4-bootstrap-token-patch]
/Users/dhruvjain/bruce/.claude/worktrees/deploy       0b9f0da  [bite15-hotfix-migration-rls-seeds]
/Users/dhruvjain/bruce/.claude/worktrees/agent-a170a8b597d754828   3967b0b   ← stalled workflow
/Users/dhruvjain/bruce/.claude/worktrees/agent-a4af91a0fdf597d2d   7105df3   ← stalled workflow
/Users/dhruvjain/bruce/.claude/worktrees/agent-aacec576263d53c7d   52ad735   ← stalled workflow
/Users/dhruvjain/bruce/.claude/worktrees/agent-ad68cc8ec7a4a8390   e876e80   ← stalled workflow
```

**Interpreter:** the canonical one is `engine/.venv/bin/python` (Python 3.14.5, pytest 9.1.1), run with cwd `engine/`. There is a second venv at repo root `.venv` with a **different dependency set** (`pydantic-ai-slim 2.19.0` vs `2.9.0`, `openai 2.49.0` vs `2.45.0`, `fastapi 0.140.7` vs `0.139.0`). **Never report numbers from the root venv.**

---

# §1 — DEPLOYED FACT

Everything in this section was read from the live environment on 2026-08-10 with the command shown. Nothing here is inferred from the repo.

## 1.1 What is running

| | |
|---|---|
| GCP project | `bruce-staging-2645`, region `us-central1` |
| **Deployed commit** | **`218cc42`** — `curl -s https://bruce-api-3iwweh3bqa-uc.a.run.app/health` → `{"status":"ok","commit":"218cc42","env":"staging"}` |
| Image digest (both services) | `us-central1-docker.pkg.dev/bruce-staging-2645/bruce/bruce-engine@sha256:5dd18c1b257b5ee0f5c3f0a9bf0a248fbb36d03b5375d29adf2b6791c6ecb809` |
| `bruce-api` | revision `bruce-api-00066-xgg`, public URL `https://bruce-api-3iwweh3bqa-uc.a.run.app`, maxScale 3, Cloud SQL connector attached, `/ready` → **200** |
| `bruce-worker` | revision `bruce-worker-00058-sbt`, **private** (OIDC invoker only), request timeout **300s**, cpu 1000m / memory 512Mi |
| Cloud SQL | `bruce-staging-db`, POSTGRES_16, `db-f1-micro`, `us-central1-a`, RUNNABLE. Single-zone, **no HA, no automated backups.** |
| Cloud Tasks | queue `bruce-intake`, state RUNNING, 500 dispatches/s |
| **Cloud Scheduler** | **`bruce-worker-tick` — `POST https://bruce-worker-3iwweh3bqa-uc.a.run.app/process` every minute (`* * * * *`), OIDC as `bruce-tasks-invoker-staging@…`, ENABLED, last attempt `2026-08-10T21:24:16Z`** |
| Cloud Run jobs | `bruce-migrate` (`python -m alembic -c alembic.ini upgrade head`), `bruce-connect-url`, `bruce-enroll-a1`, `bruce-ops-b515b40` |

> **Correction to a belief you may find in agent notes or commit messages:** the repo contains no description of a Cloud Scheduler, so a repo-only analysis concludes "nothing drains the worker." That is **false for staging**. `bruce-worker-tick` has been firing `/process` every 60 seconds. The repo, not the environment, is what is missing the description.

**HEAD is three commits ahead of what is deployed.** `0076e10`, `85cbef3`, `4cb5ded` — the entire semantic-executive body of work — are **not live**.

## 1.2 Deployed migration head

```
gcloud run jobs execute bruce-migrate --region us-central1 \
  --args="-m,alembic,-c,alembic.ini,current" --wait
gcloud logging read 'resource.type="cloud_run_job"
  labels."run.googleapis.com/execution_name"="<execution>"' --limit 30 --format='value(textPayload)'
```

Execution `bruce-migrate-vwt8x`, exit(0), output:

```
0032_known_people (head)
```

**Staging DB head = `0032_known_people`.** `0033`/`0034`/`0035` exist only in the working tree and have never been applied anywhere but local test databases.

## 1.3 Flag state in staging — read from the live service definitions

`gcloud run services describe <svc> --region us-central1 --format='value(spec.template.spec.containers[0].env[].name)'`

| Flag | `bruce-api` | `bruce-worker` | Meaning at this setting |
|---|---|---|---|
| **`BRUCE_SEMANTIC_SHADOW`** | **not set** | **not set** | **Shadow observation OFF.** No shadow rows are written. (`semantic_shadow.py:309`, read at call time) |
| **`BRUCE_ROUTER_SEMANTIC`** | **not set** | **not set** | **Semantic-executive AUTHORITY OFF.** The executive decides nothing. (`semantic_triage.py:70`) |
| `BRUCE_ROUTER_STAGE1` | not set | not set | Stage-1 model router OFF → `fast_router._stage1` finds `provider is None` and returns the deterministic default `fast_conversation / answer / source="router_default"`. **This is the silent-default branch that swallowed 10 of 13 real founder turns.** (`router_model.py:41`, `fast_router.py:159`) |
| `BRUCE_ROUTER_AUTHORITY_PCT` | `50` | `50` | Deterministic per-user canary for skipping the reasoner on *skippable* route kinds only. `(direct_action, send)` is **not** skippable, so a send turn always runs the reasoner. (`router_authority.py:34-58`) |
| `BRUCE_FOUNDER_ALPHA` | `1` | `1` | Founder-only lanes enabled |
| `BRUCE_SEMANTIC_RESCUE` | `1` | `1` | Semantic rescue enabled — but **triple-gated**: also needs `BRUCE_FOUNDER_ALPHA` and membership in `BRUCE_FOUNDER_USER_IDS`. Not a user-facing feature. (`founder_alpha.py:66`) |
| `BRUCE_FOUNDER_ALPHA_KILL` | `0` | `0` | Kill switch not engaged |
| `BRUCE_FOUNDER_USER_IDS` | — | `e1e0fcd8-27df-5971-af30-85db54362d42` | |
| `BRUCE_INTERNAL_USER_IDS` | `e1e0fcd8-27df-5971-af30-85db54362d42` | — | The one id allowed on `/internal/test` |
| `BRUCE_TASKS_*`, `BRUCE_WORKER_URL`, `BRUCE_TASKS_INVOKER_SA` | all set | — | Async intake dispatch is live |
| `BRUCE_INPROC_WORKER` | not set | not set | In-process worker off; drain is Scheduler + Tasks only |

Secrets are bound by `secretKeyRef` (never literals): `bruce-app-database-url`, `bruce-database-url`, `bruce-jwt-secret`, `bruce-openai-api-key`, `bruce-apple-client-id`, `bruce-link-code-pepper`, `google-client-secret`, `bruce-encryption-key`.

## 1.4 The one user

`BRUCE_FOUNDER_USER_IDS` / `BRUCE_INTERNAL_USER_IDS` = `e1e0fcd8-27df-5971-af30-85db54362d42`. That UUID is what `scripts/create_link_code.py`'s namespace derives for label `dhruv-alpha` — **not** what `scripts/founder_bootstrap.py` derives for the same label (see §4, DEFECT-7).

## 1.5 The relay is not running on this Mac

```
launchctl list | grep -i bruce      → no bruce launchd agent loaded
ls ~/Library/Application\ Support/bruce ; ls ~/.bruce   → nothing
ps aux | grep bruce_engine.relay    → no process
```

`[DEPLOYED FACT]` The iMessage relay — the **only** ingress into the conversation brain — is **not installed or running on this machine.** Whether it runs on a different dedicated Mac is `[UNVERIFIED]`; settle it by checking `relay_devices` (`SELECT id, name, last_seen_at FROM relay_devices WHERE revoked_at IS NULL`) or `python -m scripts.relay_killswitch status`.

---

# §2 — CERTIFIED BUT NOT DEPLOYED

All numbers in this section come from a real run on **2026-08-10** on this machine, `cd engine && .venv/bin/python -m pytest … -p no:randomly`, against **real local Postgres** (`pg_isready` → accepting connections). They describe the **working tree** (committed + uncommitted), not staging.

| Gate | Command | Result | vs pinned |
|---|---|---|---|
| cross-tool acceptance | `tests/test_acceptance_cross_tool.py` | **16 passed, 0 failed** (92.7s) | **MATCH** (`CONTRIBUTING.md:65`) |
| execution boundary | `tests/test_action_boundary.py tests/test_authorization_zero_call.py` | **281 passed, 36 skipped** (72.1s) | **MATCH** (`CONTRIBUTING.md:63`) |
| #120 authorization corpus | `tests/ -k "authorization"` | **314 passed, 36 skipped** (83.2s) | **MATCH** (`CONTRIBUTING.md:64`) |
| corpus shape (read live, not from manifest) | — | **238 cases, 24 categories, 202 must_block / 36 may_authorize** | matches `ci/gates.json` |
| migration discipline + RLS head | `tests/test_migration_discipline.py tests/test_migration_rls_context.py` | **15 passed**, head `0035_shadow_reconciliation` | OK |
| the 15 new shadow suites | `tests/test_shadow_*.py tests/test_semantic_shadow_pg.py tests/test_migration_0033_*` | **139 passed, 0 skipped** (68.9s), all against real Postgres | no pin exists (all untracked) |
| **full suite** | `-q -p no:randomly --timeout=900` | **2 failed, 3316 passed, 77 skipped, 1 xfailed** in **951.7s**, exit 1 | see below |
| gate runner | `.venv/bin/python scripts/run_gates.py` | **13 OK, 1 FAIL**, exit 1 | the FAIL is by design |

**The 24 corpus categories** (unchanged): ambiguous-reversal 10, args-changed 10, attachment-injection 10, block-quote 10, broad-refusal 11, cancel-nothing 10, cancel-pending 10, cancel-plus-delete 10, duplicate-approval 9, email-body 10, expired 9, forwarded 10, inline-quote 10, no-to-yes 10, provider-execute 11, refusal-after 10, refusal-before 10, screenshot 10, split-approval 10, stale-approval 9, wrong-account 10, wrong-conversation 10, wrong-user 10, yes-to-no 9.

**The 77 skips are fully accounted for** — none is an environment failure hiding as a skip: 36 (`test_authorization_zero_call.py:254`, the `may_authorize` cases structurally skipped out of the zero-call parametrization — **this is what the pinned "36 skipped" means**), 36 (`test_semantic_scope.py:520`, same cases), 1 `BRUCE_MEASURE`, 1 Google OAuth live, 1 Google Calendar live, 1 macOS frameworks, 1 `BRUCE_RUN_REAL_MODEL`.

**The 1 xfail is a deliberately pinned open defect**, not flakiness: `test_query_counts.py::test_a_deterministic_turn_reads_each_kind_of_state_at_most_once`, `xfail(strict=True)` at `tests/test_query_counts.py:159` — "one warm deterministic turn issues 35 queries and opens 16 database sessions." The suite's own live measurement printed `TOTAL: 65 queries` for the run. **It will start FAILING the moment someone fixes the duplicate reads — that is intended.**

**`run_gates.py` output:**

```
  [OK ] focused-known-people     74 passed
  [OK ] goal-seam                122 passed
  [OK ] acceptance               16 passed
  [OK ] boundary                 281 passed
  [OK ] authorization-corpus     314 passed
  [OK ] migration                15 passed
  [OK ] corpus expectations      202 must-block, 36 may-authorize
  [OK ] adapter/over-block proofs ran
  [OK ] acceptance scenarios     16 collected, 16 pinned
  [OK ] migration chain          head=0035_shadow_reconciliation
  [OK ] language corpus          4 families, 1 known gap(s)
  [OK ] shadow inert             6 forbidden imports checked
  [OK ] hygiene                  source-inspection debt 13/13
  [FAIL] safety baseline         changed without acknowledgement
```

The FAIL is **correct behaviour**: `check_safety_baseline_unchanged` (`scripts/run_gates.py:299-314`) diffs `ci/gates.json` against `HEAD` and refuses when a `safety` key moved without acknowledgement. The uncommitted change is `"pinned_head": "0032_known_people" → "0035_shadow_reconciliation"`. The documented invocation to accept it is in §6. `[UNVERIFIED]`: nobody has confirmed that invocation returns exit 0.

## 2.1 The two failures — root cause proven, and it is NOT a code regression

```
FAILED tests/test_language_generalization.py::test_language_rates_meet_their_thresholds
        conversation-vs-action 0.1739 < 0.98
        goal-kind 0.0000 < 0.95
FAILED tests/test_language_generalization.py::test_paraphrase_families_are_internally_consistent
        families whose members disagree: {'email_thanks_person': 0.0, 'email_running_late': 0.0,
                                          'calendar_create_event': 0.0, 'calendar_move_event': 0.0}
```

Both are fed by one shared fixture that performs 310 live model calls. **Every one of those 310 calls was rejected by OpenAI:**

```
cd engine && .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import asyncio; from bruce_engine.semantic_triage import SemanticTriage
asyncio.run(SemanticTriage().read('can you email ms rivera and thank her for the rec letter'))"

→ ModelHTTPError status_code=429, model_name: gpt-5.4-mini,
  {'message': 'You have no credits remaining.', 'type': 'insufficient_quota',
   'code': 'credit_balance_exhausted'}
```

The key itself is valid — `GET https://api.openai.com/v1/models` with the same key returns **HTTP 200 in 698ms**. **The OpenAI account has no credits.**

> `[DEFECT]` **BRUCE CANNOT ANSWER ANY STUDENT RIGHT NOW.** The conversation reasoner (`llm.MODEL_CONVERSATION`) uses the same key. This is the single highest-priority item in §6 and it is a billing action, not a code change.

Note one agent in the mapping pass attributed this to "network egress blocked in the sandbox." That is wrong — the 200 from `/v1/models` disproves it. The 429 body is the evidence.

---

# §3 — UNCOMMITTED / PARTIAL

**None of this is done.** It is ~5,000 lines of working-tree change that has never been committed, never been reviewed, and never touched staging. Describe it to the founder as *in flight*, never as shipped.

## 3.1 What it is trying to be

A **durable, leased, retried, reconciled outbox for shadow-mode semantic observation** — so that the sample used to justify giving the semantic executive authority is unbiased. The thesis is stated at `semantic_shadow.py:1-188`. `semantic_shadow.py` went **165 → 2315 lines**.

| Bias it removes | Mechanism | Where |
|---|---|---|
| Restart drops slow/failing reads | outbox row + lease + retry | `semantic_shadow.py:922-1268`, migration 0033 |
| Continuations/approvals never observed | intake hoisted above routing branches | `conversation_runtime.py:399-437, 577-582` |
| Reconciliation agrees with itself | denominator from `conversation_turns`, two anti-joins in SQL | migration 0035, `semantic_shadow.py:1127-1222` |
| `false_capability_denial` counted off the global registry | per-user broker snapshot persisted on the row | `tool_broker.py:120-165` |
| Zombie worker overwrites a newer observation | per-claim `lease_token` fence in SQL | `semantic_shadow.py:1030-1080` |
| Model prose leaking into telemetry | `validation_codes` closed vocabulary + `missing_count` | `semantic_executive.py:206-220` |
| A leaked `OPENAI_API_KEY` silently degrading the language gate | harness owns its provider; AST gate | `eval/language/harness.py:220-268`, `run_gates.py:188-233` |

**Two changes in this tree are NOT shadow work and must not be lost in a `git stash`:**
- `0034_known_people_row_security.py` — fixes a **live cross-tenant data exposure** (§4 DEFECT-1).
- `schema.py:1621-1630` `RLS_TABLES` backfill (`intake_jobs`, `semantic_shadow_jobs`, `known_people`) — which is *how* the exposure was found.

## 3.2 What in it is complete and proven (locally, in the tree, uncommitted)

139 tests pass against real Postgres. Verified properties include: the lease-token fence is real SQL (`semantic_shadow.py:1051-1052`), `attempts` advances on **claim** not on record (closing the killed-mid-read unbounded path), `max_attempts > 0` is a database CHECK, `ON DELETE CASCADE` to `conversation_turns`, two distinct unique keys with untargeted `ON CONFLICT DO NOTHING`, the privacy narrowing (the student's words appear nowhere in what is persisted), and a `SECURITY DEFINER` aggregate-only reconciliation function that refuses unless `app_is_worker()`.

## 3.3 What is half-built, wrong, or will not work in production

**🔴 `shadow_reconciliation()` will not exist on Cloud SQL.** `0035:286-299` `_can_provision_bypassrls_role` returns false unless the migrator is `rolsuper` or has `rolcreaterole AND rolbypassrls`. Cloud SQL's `postgres` is `cloudsqlsuperuser`, not `rolsuper`. `0035:312-317` then logs a warning and **returns without creating the function**. Consequence in staging: `reconciliation_status: "unknown"` forever, every count NULL. The degradation is honest (never falsely `clean`), but the deliverable is not achieved. No runbook exists for provisioning `bruce_shadow_recon` out of band. `[UNVERIFIED]` — settle with `SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user;` as the migrator on Cloud SQL.

**🔴 `classify_turn` silently excludes every 6-character alphanumeric turn.** `semantic_shadow.py:1751` `_LINK_CODE = re.compile(r"^[A-Za-z0-9]{6}$")`. Executed against the real function:

```
'cancel' -> excluded_link_protocol      'delete' -> excluded_link_protocol
'thanks' -> excluded_link_protocol      'friday' -> excluded_link_protocol
'monday' -> excluded_link_protocol      'please' -> excluded_link_protocol
```

The rationale cites `messaging_inbound._CODE_RE`, but that lives inside the `user_id is None` (**unlinked**) branch, and `conversation_runtime.handle` is only reached for a **linked** user. A 6-char message there can never be a link code. **`"cancel"` — the most safety-critical one-word turn Bruce receives — is dropped from the observation sample by design, and counted as `explicitly_excluded_turns` so it reads as accounted for.** No test covers it.

**🟠 Reconciliation reports `failed` whenever the kill switch is off.** `intake()` returns before writing a row when `enabled()` is False, but `worker_api.process` calls `health()` unconditionally (`worker_api.py:162`) and `health`/`counts`/`backlog`/`reconcile_population` have **no `enabled()` gate**. With `BRUCE_SEMANTIC_SHADOW` unset (the deployed default) every turn writes a `conversation_turns` row and no shadow row → anti-join fires → `reconciliation_status: "failed"` on every wake, forever. "Deliberately off" and "losing turns" emit identically. Also costs 2 unconditional DB round-trips per wake, contradicting `worker_api.py:85-86`.

**🟠 `counts()` is an unbounded full-table scan** (`semantic_shadow.py:1116-1125`), called twice per wake, on a table that gets one row per student turn forever.

**🟠 Shadow reads run under the student's 2.5s deadline.** `claim_and_observe` calls `interpret()` with no `timeout_s`, so `TRIAGE_TIMEOUT_S=2.5` applies; `SHADOW_BUDGET_S=20` is only the outer `wait_for`. This directly contradicts the same tree's language-eval fix, whose premise is that measuring comprehension against a 2.5s wall scores slow calls as misunderstandings.

**🟠 `has_pending_decision` was quietly weakened.** `conversation_runtime.py:582` passes `bool(_pending_capability(open_goal_row))`, and `_pending_capability` returns `None` for a capability not in the registry. The correct predicate is `_pending_operation(...) is not None`. (It is still an improvement on the prior `locals().get("pending_decision")`, which was almost certainly always False.)

**🟡 Stale migration numbers in new test docstrings** — `test_shadow_canonical_turn_key.py:16,18,104`, `test_shadow_reconciliation_population.py:13,19,58,62,267,294,328`, `test_shadow_worker_metrics.py:250` reference migrations **0036/0037/0038 that do not exist**. The chain was collapsed to 0033/0034/0035 and the prose was not updated.

**🟡 `scripts/mutation_runner.py` (1402 lines) is wired to nothing** — no CI, no gate, no doc. `ci/gates.json` has a `proved_by_mutation` prose list that nothing executes. **Whether all 37 mutations are CAUGHT is `[UNVERIFIED]`; no report artifact is committed.** It is destructive to cluster-wide roles (`bruce_app`, `bruce_shadow_recon` outlive `DROP DATABASE`) — a failed `reset_cluster_roles()` silently disables RLS for every subsequent test in the session.

**Zero TODOs, zero FIXMEs, zero `NotImplementedError` in the ~5000 new lines.** The `Protocol` bodies using `...` are the correct idiom, not stubs.

---

# §4 — KNOWN DEFECT

Ranked by how badly each blocks a real paying user. Every one has evidence; none is speculation.

### DEFECT-1 · `known_people` has NO row-level security on any existing database — P0 CROSS-TENANT DATA EXPOSURE `[DEPLOYED FACT + UNCOMMITTED FIX]`

`0032_known_people.py:40-41` opens with `if bind.dialect.has_table(bind, TABLE): return`, and `0001_initial_schema` builds every table with `Base.metadata.create_all()`. So 0032 **returned before reaching its own `ENABLE ROW LEVEL SECURITY` (line 69-70) on every database that has ever been built** — which is all of them. Staging is at head `0032` right now.

The table is written on every turn (`conversation_runtime.py:287` `people.learn`) and read on every send (`goal_handler.py:794` `people.resolve`). It holds each student's recipient book: names, relationships, email addresses — the most sensitive relational data Bruce holds after the turn text itself.

The fix, `0034_known_people_row_security.py`, is **untracked**. Its downgrade is deliberately a no-op (rolling back must not strip row security from students' contact books).

**Confirm on staging before anything else** (this is a read, and it must self-check per the RLS rule):
```sql
SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'known_people';
```

### DEFECT-2 · Bruce cannot answer anyone — OpenAI account has zero credits `[DEPLOYED FACT]`
Proven in §2.1. `429 credit_balance_exhausted`. Every student-facing step (`MODEL_VISION`, `MODEL_EXTRACTION`, `MODEL_DRAFTING`, `MODEL_VERIFICATION`, `MODEL_CONVERSATION`, `MODEL_ROUTING`) is `gpt-5.4-mini` on that one key (`llm.py:34-39`).

Worse: `semantic_triage.py:322` maps `429` to `"transport"` (retryable), so an exhausted balance is retried once — **doubling the spend of a dead account while reporting "0.17 comprehension."** The failure looks like a code regression, not a billing event.

### DEFECT-3 · `access_control` denies every non-founder; the automatic grant path has no caller `[CERTIFIED — HARD BLOCK]`
`access_control.py:120` returns `no_grant`. `activate_production_entitlement` (`access_control.py:126`) — whose own docstring at `:132` says *"This is the AUTOMATIC path D1 calls on verified signup"* — is called from `scripts/capability_admin.py:67` and tests **only**. `POST /v1/auth/apple` (`api.py:214`) does not call it. **D1 does not exist.**

A brand-new user texts Bruce and gets `messaging_inbound.py:42` — *"i can't send email from this number yet, so i'm not going to pretend i started that."* Honest, and terminal. **Zero user journeys complete for anyone but the founder.**

### DEFECT-4 · The model is ordered to copy exact capability ids from a list the context never contains `[CERTIFIED — RECURRING ROOT CAUSE]`
`conversation_model.py:60-62` instructs: *"The context contains the operations Bruce can run right now, as exact ids… `required_capabilities` MUST contain only exact operation ids copied from that list."*
`capability_snapshot.render()` (`capability_snapshot.py:62-77`) emits **family names only**: `"Right now you CAN use: calendar, email."` `context_compiler.py:255` inserts exactly that string and nothing else.

**There is no such list in the context.** The product works at all only because the prompt itself names `gmail.send_message` and `calendar.create_event` in the same sentence that claims to name no capability — and those two ids are precisely the only two rows in `goal_handler._EXECUTORS`. `calendar.update_event` / `delete_event` can never be named by the model and reach execution only via regex in `CalendarMutationHandler`.

When the model emits free text ("sending messages") instead of an id: `goal_runtime.py:154` `NOT_AN_OPERATION_ID` → `GoalHandler` declines → `MissionHandoffHandler` declines → `EventCandidateHandler` declines → `DefaultReplyHandler` ships prose → `response_composer` downgrades any "i'll send it" to the honest no-promise line. **This is the single largest cause of "it feels like a demo."**

The ids are already computed and thrown away — `FamilyState.capabilities` holds them. The fix is ~6 lines in `render()`.

### DEFECT-5 · Relay HTTP timeout (20s) is shorter than the engine's own model budget (22s + 1 retry); a timed-out message is dropped with no reply `[CERTIFIED — SILENT LOSS]`
`relay/backend.py:82` `httpx.AsyncClient(timeout=20.0)`. `llm.py:43-44` `CONVERSATION_TIMEOUT_S=22`, `CONVERSATION_MAX_RETRIES=1` → 44s worst case, plus `BRUCE_COMPOSE_TIMEOUT_S=8`, plus a Gmail send and read-back — **all inside one synchronous `POST /v1/relay/inbound`** (`api.py:673`).

On timeout `relay/relay.py:298-300` logs `inbound_retry` and returns `"retry"` **without checkpointing and without creating a pending record** — `pending` is only populated on the deferred-attachment branch. **A plain text message that takes >20s is lost with no reply at all.** Same for any transient 5xx/429/network blip.

### DEFECT-6 · The approval turn depends on a vision-model call that has nothing to do with it `[CERTIFIED]`
On "yeah send it", `conversation_runtime.py:469-489` skips the router because the continuation already resolved deterministically — but `authoritative_decision` is only ever set inside the `else` branch (`:534`), so `:663-794` still runs `_prepare_images` + `context_compiler.compile` + `self.reasoner.decide`. **On any reasoner exception the turn returns `model_error` with the "ngl something glitched" fallback and the email never goes out**, from a turn whose meaning was fully determined 200 lines earlier.

### DEFECT-7 · The two onboarding CLIs derive DIFFERENT user ids from the same label `[CERTIFIED]`
`founder_bootstrap.py:44` `ALPHA_NS = 6f9619ff-8b86-d011-b42d-00c04fc964ff`; `create_link_code.py:30` `ALPHA_NS = b0b0a1fa-0000-4000-8000-000000000001`. Both compute `uuid5(NS, f"alpha:{label}")`. For `--label dhruv-alpha`: `5bc2ed4a-223f-5a06-b4c2-16cd3abf9e68` vs **`e1e0fcd8-27df-5971-af30-85db54362d42`** (the latter is what staging has in `BRUCE_FOUNDER_USER_IDS`).

The comment at `founder_bootstrap.py:42-43` claiming they share a namespace is **false**. Symptom when mixed: you connect Google to one user and grant the entitlement to another, and the relay keeps replying with the link prompt. **Use `create_link_code.py`'s namespace. Always.**

### DEFECT-8 · Calendar goal missing a title is an unrecoverable dead end `[CERTIFIED]`
`goal_handler._compose` is hard-wired to `DraftComposer`, which only ever returns `{subject, body}` (`goal_handler.py:502-538`). For `GoalKind.schedule_event` the generatable gap is `{title, end, timezone}`, so `values` filters to `{}`, `next_step` still says `COMPOSE`, control falls **past** the `PROPOSE_CONFIRMATION` check into `_run`, `_bind` catches the `ValueError` from `tool_arguments()`, and the student gets *"i lost the details for that one, mind saying it again?"* — while Bruce holds a perfectly good date. Saying it again reproduces it.

### DEFECT-9 · Whether a calendar write asks for consent is decided by which field the LLM filled `[CERTIFIED — CONSENT NONDETERMINISM]`
`GoalHandler` (priority 90) claims only when `required_capabilities` holds a real registry id, and then **always proposes a confirmation**. When the model omits the id, `CalendarScheduleHandler` (priority 70, `conversation_outcomes.py:229`) claims on `model_actionable and _wants_calendar` and **executes immediately** (`:290-330`). Identical student message, two different consent regimes.

### DEFECT-10 · A wordless photo can create a real Google Calendar event `[CERTIFIED — CONSENT GAP]`
`conversation_outcomes.py:316-323` mints `AuthorizationType.direct_explicit` with `text=octx.msg.text` (which is `None`) and leaves `explicit_operation_request` at its default `True`. `user_action_boundary.evaluate(None)` → `Polarity.unrelated`, which does not block. `create_event` is not in `TARGETS_EXISTING_ENTITY`, so no `request_span` is demanded. **The grant asserts the student "named this operation" when the student wrote nothing.**

### DEFECT-11 · Calendar reads do not exist; "what's on my calendar" is answered from Bruce's own writes `[CERTIFIED — SILENT WRONG ANSWER]`
`tool_registry.py:57` `calendar.search_events` is `live=False`. `calendar_adapter.py:149-152` declares only `insert/get/update/delete` — **there is no list or search method at the adapter at all.** The answer comes from `entity_store.active_events`: rows **Bruce itself created**, most-recent-first, **unfiltered by date, capped at 8**. Meanwhile `render()` has told the model it CAN use "calendar", so the model answers confidently and incompletely, with no hedge.

### DEFECT-12 · Timezone defaults to `America/Los_Angeles` for every user who has never said otherwise `[CERTIFIED — SILENT WRONG DATA]`
`calendar_schedule.py:209` (`DEFAULT_TZ`, with a `TODO` beside it), threaded through `goal_handler.py:716`, `conversation_outcomes.py:586`, `turn_context_assembler.py:478`. A Chicago student's "oct 14 7pm" becomes 7pm Pacific = **9pm Central** on the real calendar. Also, `context_compiler._now_block` correctly returns `None` rather than lying for that user, so the model gets **no date anchor** at all.

### DEFECT-13 · What the student approves is not byte-identical to what is sent `[CERTIFIED — INTEGRITY GAP]`
The proposal reply passes through `enforce_no_dashes` and `messaging_outbound.gate_outbound_text` (strips `PROHIBITED_PHRASES`, collapses whitespace, rewrites em dashes). The `body` slot on the goal — and therefore `gmail_send_args` and the execution fingerprint — holds the **raw** composer output. **The fingerprint binds the sent bytes, not the shown bytes.** An em dash the student never saw ships to the professor.

### DEFECT-14 · Connecting Google without granting Gmail send is an unrecoverable state `[CERTIFIED]`
`oauth_google.py:260-263` makes only `calendar.events` mandatory; `gmail.send`/`gmail.readonly` are requested but not required, and the integration is stored `status="connected"`. `tool_broker.availability` then returns `INSUFFICIENT_SCOPE` and `goal_handler._blocked` says *"reconnect it with that turned on"* — **but there is no user-reachable way to reconnect** (DEFECT-15).

### DEFECT-15 · Every onboarding step is a founder terminal command `[CERTIFIED — HARD BLOCK]`
- **Link code**: `api.py:387` requires an existing Bruce JWT and no client calls it. Real path = `scripts/create_link_code.py`.
- **Google OAuth**: `api.py:440` `/connect/start?t=` requires a JWT with `claims["purpose"] == "google_connect"`, and **nothing in the repo mints one** (grep `google_connect` → only the consumer at `api.py:450` and one test). Bruce can never text a user a connect link.
- **Entitlement**: DEFECT-3.
- **Relay**: `install_relay.sh` on a dedicated Mac, GUI login, operator-minted bootstrap token, Full Disk Access + Automation TCC grants approved by hand.

The iOS app participates in **none** of it: it calls exactly six routes (`/v1/auth/apple`, `/v1/account`, `/v1/missions`, `/v1/missions/{id}`, `/v1/missions/{id}/events`, `/v1/intake`) and never touches link-code, Google, or messaging.

### DEFECT-16 · One relay, one Apple ID, cross-user outbound claim `[CERTIFIED — SCALING CEILING]`
`messaging_outbound.claim` (`messaging_outbound.py:90`) has **no `user_id`, `channel`, or `relay_device_id` predicate** — the docstring itself says "(cross-user; worker session)". **Every student's replies leave from the one Mac's one Apple ID.** There is no per-user sender identity, no per-user send quota, no fairness, and no capacity number anywhere. Hard ceiling ≈ 20–50 users before Apple rate-limits or the single Mac becomes the single point of failure.

### DEFECT-17 · The language-eval harness cannot detect its own provider outage `[CERTIFIED — pre-existing, not from this branch]`
`eval/language/harness.py:120` retries only on `"reader unavailable"`, but the note the executive actually emits for a triage failure is `f"triage failed: {reason} ({ms}ms)"` (`semantic_executive.py:479`). So a `triage failed: transport` observation is **counted as a genuine reading**, `observations == expected_observations` holds at 100% transport failure, and the guard at `test_language_generalization.py:188` can never fire. **The sibling file already has it right** — `eval/language/shadow_eval.py:61` checks both strings. The weaker evaluator is the one wired into the pinned gate. This is exactly how a billing outage got reported as "Bruce understands 17% of turns."

### DEFECT-18 · `/healthz` does not exist `[CERTIFIED — cosmetic but constantly misleading]`
`brucectl.py:174` and `install_relay.sh:80` probe `/healthz`; the API serves `/health` and `/ready` and has no `/` route. `brucectl status` therefore always reports the API unreachable and the installer always warns.

### DEFECT-19 · A healthy relay reports STALE `[CERTIFIED]`
`supervisor_seen_at` is stamped only by `POST /v1/relay/heartbeat`, which is called only from `_send_gate` (**after** a job is claimed) and from the supervisor's park loop (**only while parked**). `claim()`/`post_inbound()` stamp `last_seen_at`, not `supervisor_seen_at`. **A healthy relay with an idle outbound queue reports STALE within 120s.** Check `last_seen_at`, not `supervisor_seen_at`.

### DEFECT-20 · Secret rotation is a data-loss event `[CERTIFIED]`
`crypto.py` uses a single `Fernet`, not `MultiFernet`, with no key id on the ciphertext column. **Rotating `BRUCE_ENCRYPTION_KEY` orphans every stored Google refresh token**, and per DEFECT-15 there is no user-facing reconnect path. Also: `BRUCE_ENCRYPTION_KEY` and `BRUCE_LINK_CODE_PEPPER` are hard-required and **absent from `.env.example`**.

### DEFECT-21 · `retention.sweep_expired` has no caller `[CERTIFIED]`
`expires_at` is written on every source; nothing ever sweeps. Raw student content is never erased, despite what `alpha-readiness.md` promises.

---

# §5 — PRODUCT TARGET

**Everything in this section is `[TARGET]`. None of it is current capability.** The founder's stated bar, verbatim, from 2026-08-10:

> "the product MAE has to be like poke or better in fact… it has to be literally texting like a human with teenage slang and brain to where it knows exactly what the user is talking about and is texting like their friend with 100% accuracy with an insanely fast harness and response time — if the user is waiting over 2-3 seconds for a simple response then we are cooked… cannot feel like im texting an ai."

Cohort sequence: **founder only → 3-5 hand-picked → anyone on the waitlist**, with infrastructure that survives the third step. Billing and self-serve onboarding are **explicitly deferred until after MAE** — then built as a fully automated launch-ready flow.

## 5.1 MAE definition (the acceptance bar)

Channel: **self-hosted iMessage relay** (chosen 2026-08-10). Not the iOS app, not SMS, not web.

| # | Capability | Acceptance criterion (observable by a person holding a phone) | Nearest current state |
|---|---|---|---|
| T1 | **Gmail: read, draft, send, reply** | "email coach smith about missing practice" → a draft in-thread → "yeah" → a real email arrives, and Bruce's receipt was proven by reading the message back from Gmail | send+verify path complete against a **fake** adapter; `[UNVERIFIED]` against real Gmail — see 5.4 |
| T2 | **Calendar: create, read, change, delete** | an event created in Google's web UI 5 minutes ago is named when the student asks "what's on my calendar tomorrow" | create/update/delete wired; **read does not exist** (DEFECT-11) |
| T3 | **Proactive notifications / briefings** | Bruce texts *first*: "3 emails need replies", morning brief, "that deadline is friday" | `notifier.RelayNotifier` and `briefing.py` exist; **nothing enqueues a durable step from a conversational turn** |
| T4 | **Memory: people, preferences, context** | "email my advisor" works without an address, because the student told Bruce who that is once | `known_people` write+read is live; `gmail.resolve_recipient` is `live=False`, so an unknown name always costs a round trip (DEFECT-15/§4) |
| T5 | **Voice: a teenage friend, not an assistant** | 20 consecutive replies that a peer would not flag as AI | `conversation_style.py` (227 lines), `humanize.py` (213, **zero production callers**), `response_render.py` (242, zero production callers), `product/voice_profiles.yaml` |
| T6 | **Latency: ≤2–3s for a simple turn** | p50 under 2s, p95 under 3s, measured on real turns | **not close** — see 5.2 |
| T7 | **Understanding: no silent default** | a phrasing `_stage0` does not pattern-match is still understood | **`BRUCE_ROUTER_STAGE1` and `BRUCE_ROUTER_SEMANTIC` are both OFF in staging** → any unmatched phrasing becomes generic chat (`fast_router.py:159`) |

## 5.2 The latency budget, measured — T6 is the hardest target in this document

| Stage | Current budget | Source |
|---|---|---|
| Stage-1 router (when enabled) | p50 **1383ms**, p95 **2002ms**, **12.5% timeouts** — gated off for this reason | `router_shadow.py:5` |
| Conversation reasoner | **22s** timeout × (1 + 1 retry) = **44s** worst case | `llm.py:43-44` |
| Draft composer (second model call) | 8s, no retry | `goal_handler.py:482` |
| Response render (humanizing pass) | 0.7s, falls back to deterministic text | `response_render.py:31` |
| Semantic triage | 2.5s hard deadline | `semantic_triage.py:54` |
| Relay HTTP client | **20s** — shorter than the engine's own budget (DEFECT-5) | `relay/backend.py:82` |
| DB work per warm turn | **35 queries, 16 sessions** (pinned `xfail`); suite measured **65 queries** total on one run | `tests/test_query_counts.py:159` |

A goal turn is **two sequential model calls plus a provider round trip inside one synchronous HTTP request.** Reaching 2–3s p50 requires, at minimum: one model call per turn instead of two, a real Stage-1 that is fast enough to keep, the duplicate state reads fixed (#127A `TurnStateSnapshot`), and a decision about what happens asynchronously. **Treat T6 as an architecture item, not a tuning item.**

## 5.3 Differentiators vs Poke `[TARGET — strategy, not shipped]`

`docs/competition.md` is about the **old** professor-outreach wedge and does not address Poke. This is the current framing; nothing here is implemented as a marketed feature:

1. **Verified receipts, never a fake "done."** Bruce reads the message back from Gmail and matches SENT-label + recipient + subject before saying "sent it ✅" (`gmail_adapter.py:215-221`). A general assistant says "done" when the API returned 200. **This machinery is real and shipped** — it is the single most defensible thing in the codebase.
2. **A refusal actually revokes consent, everywhere.** "actually don't send it" writes a durable refusal and invalidates **every** outstanding authorization for that user across all threads (`authorization_store.py:148-153`), and a woken background job re-checks it before spending (`refused_since`). Backed by a 238-case adversarial corpus, 202 of which must block.
3. **Per-tenant isolation enforced by Postgres, not by application code.** RLS with `FORCE`, a restricted runtime role with no `BYPASSRLS`. (Currently undermined by DEFECT-1 — fix that before claiming it.)
4. **Depth on student life** — assignments, deadlines, school connectors. Note honestly: `school_queries.py` (412 lines), `school_store`, `school_connector`, `school_capability`, `canvas_fake.py` (502 lines) form a **closed island with zero non-test importers**. This is a roadmap claim, not a feature.
5. **Memory with provenance.** A recipient exists only because the student said so, in their own words, with the source span and a non-destructive correction chain (`0032_known_people.py:1-17`).

## 5.4 The claim to stop repeating until it is proven

`CONTRIBUTING.md:5` says *"Bruce sends real mail and writes to real calendars for real students."* **No artifact anywhere proves one real Gmail send has ever happened.** Both live-credential tests skip because `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` are absent from `engine/.env`. Every green test uses `FakeGmailAdapter`. `product/integrations.yaml:127` says `live_verification: false`, and the same file contradicts itself 50 lines later with `connection_status: connected`.

Accurate phrasing until proven: **"the send path is complete and verified against a provider-semantics fake."**

## 5.5 Honest schedule note

The founder's window is two days. Against the evidence above — no model credits, no entitlement path, capability ids missing from the prompt, no calendar read, a 22s reasoner budget behind a 20s relay timeout, and live iMessage never confirmed — **Poke-parity in two days is not reachable.** What *is* reachable in two focused days is the sequence in §6: a system where a stranger's phone can be onboarded in one command, Bruce reliably understands send/schedule turns, an email demonstrably arrives, and no silent drops occur. Proactive briefings (T3), calendar read (T2), the voice layer (T5) and the latency target (T6) are week-2 items. Say this plainly rather than shipping a demo and calling it MAE.

---

# §6 — NEXT ACTION

The smallest exact sequence. **Do not restart repo discovery.** Do §7 first (30 minutes), then start at N1.

Every step is strict TDD: **failing production-surface test first → prove the exact failure reason → implement → revert the fix and prove the test goes red → restore.**

### N1 — Fund the OpenAI account `[blocks literally everything, ~5 min, not a code change]`
Set a hard usage cap while you are in the dashboard. Verify:
```bash
cd engine && .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import asyncio; from bruce_engine.semantic_triage import SemanticTriage
print(asyncio.run(SemanticTriage().read('can you email ms rivera and thank her for the rec letter')))"
```
Must return a `SemanticTurn`, not `ModelHTTPError 429`.

### N2 — Commit the working tree `[~20 min]`
Nothing else can safely proceed on top of ~5,000 uncommitted lines, and `git stash` would bury the DEFECT-1 fix.
```bash
cd engine
.venv/bin/python scripts/run_gates.py --accept-safety-change \
  "migration head advanced 0032 -> 0035 for the semantic-shadow queue (0033), \
known_people RLS (0034) and shadow_reconciliation (0035)"
```
Expect exit 0 (`[UNVERIFIED]` — nobody has run it with the flag). Then commit **all 19 tracked + 19 untracked files in one commit**, and in the message state plainly: shadow stays OFF, the reconciliation function will skip on Cloud SQL, and `classify_turn` excludes 6-char turns (§3.3) — these are known and deferred, not fixed.

Before committing, fix the three files whose docstrings reference migrations 0036/0037/0038 that do not exist (§3.3, 🟡).

### N3 — Ship the `known_people` RLS fix to staging `[P0 privacy, ~30 min]`
1. `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='known_people';` on staging — record the `false, false` before.
2. Apply `0033`→`0035` to a **scratch database on the same Cloud SQL instance first**, not staging. `0035` mutates the cluster-wide `pg_authid` via `CREATE ROLE` and has a retry loop for `tuple concurrently updated`.
3. Then `gcloud run jobs execute bruce-migrate --region us-central1 --wait` (default args = `alembic upgrade head`).
4. Re-run the `pg_class` query. **`relrowsecurity` and `relforcerowsecurity` must both be `t`.**
5. Deploy the new image to both services on **one immutable digest**. Use `--update-env-vars`, **never `--set-env-vars`** (which would wipe 12 `secretKeyRef` bindings). Confirm `curl /health` shows the new commit.

### N4 — Prove live iMessage before building anything on it `[30 min, no code]`
`api.py:534` states in-tree that live iMessage behaviour is unverified, and every relay test runs against `fake_imsg.py`. The relay is not running on this Mac (§1.5).

Install per `docs/relay-install.md` on the dedicated Mac (note: `install_relay.sh` **refuses a dirty working tree** and requires an exact 40-char SHA — N2 must be done first), then text the Bruce Apple ID from a second phone and watch `~/.bruce-relay/supervisor.log` for `inbound_ok` and `outbound_sent`.

Specifically unproven: whether `imsg send --to <chat_guid>` delivers — `messaging_inbound.py:138` sets `reply_target = msg.thread_id or msg.channel_identity` (the chat GUID for a 1:1) and `relay.py:582` passes it straight through.

**If this fails, stop the plan and fix the relay. Nothing else matters.**

### N5 — Put exact capability ids into the model's context `[~1.5h — highest leverage in the repo]`
Fixes DEFECT-4. `FamilyState.capabilities` already holds the ids; `capability_snapshot.render()` throws them away.

- **Test first:** in `tests/test_conversation_runtime.py`, a capturing reasoner driven through `conversation_runtime.handle`, asserting `"gmail.send_message" in context` and `"calendar.create_event" in context`.
- **Acceptance:** "can you email my teacher smith@stanford.edu about office hours" produces a drafted email and "want me to send it?" on the **first** turn.
- **Then measure, before building anything on top:** run 15 real phrasings through the deployed service and count how many reach `GoalHandler`. Under 12/15 means the root cause is elsewhere — fall back to making `GoalHandler.evaluate` also claim when the router named a capability (`rd.candidate_capabilities`, which `fast_router._stage0` produces deterministically).

### N6 — Grant the entitlement automatically on link-code redemption `[~1.5h]`
Fixes DEFECT-3. Hook `messaging_inbound.py:163` (`r.status == "linked"`) → `access_control.activate_production_entitlement(r.user_id, ...)`.
**Watch the session nesting:** `admin_session()` asserts `app.user_id` is unset (`db.py:73-78`) and refuses otherwise, so it must run **outside** the redemption's session.
**Acceptance:** a phone number that has never texted Bruce sends a 6-char code, gets "you're in", and its very next message gets a real conversational reply.

### N7 — Cap the latency budget below the relay's HTTP timeout `[~1h]`
Fixes DEFECT-5. Set `BRUCE_CONVERSATION_TIMEOUT_S=15` and `BRUCE_CONVERSATION_MAX_RETRIES=0` in the deployed env; raise `relay/backend.py:82` to `60.0`; add a bounded 2-attempt retry on `httpx.TimeoutException` only. A double-post is safe — `conversation_runtime.py:265` `_already_answered` and the `conv:{pmid}` outbound idempotency key both dedupe.
**Acceptance:** a 20-turn manual script produces exactly one reply per message. Zero silent drops.

### N8 — Kill the calendar title dead end `[~45 min]`
Fixes DEFECT-8. Derive `title` deterministically from the trusted text as `Source.model_derived`, which forces `PROPOSE_CONFIRMATION` so the student sees and can correct it.

### N9 — One consent regime for calendar `[~45 min]`
Fixes DEFECT-9 and DEFECT-10. Require non-empty trusted text for `CalendarScheduleHandler` to authorize; a wordless photo then falls to `EventCandidateHandler`, which **offers** instead of writing. **Run the 16 pinned acceptance scenarios immediately after** — if any breaks, revert and log it.

### N10 — Timezone before the first calendar write `[~1h]`
Fixes DEFECT-12. In `goal_handler._execute`, before `turn_slots`: if `kind is schedule_event` and `world_state` has no stored tz, ask once. `WorldStateHandler` (priority 75) already absorbs the answer and persists it.

### N11 — Calendar read `[~3h — the first thing to cut if time runs out]`
Fixes DEFECT-11. Add `GoogleCalendarAdapter.list(time_min, time_max)`, flip `calendar.search_events` live with `requires_scope=".../auth/calendar.events"`, add a `CalendarQueryHandler` at **priority 68** (free — existing are 90/80/75/72/70/65/60/50/0; a tie raises `OutcomeCollision` and degrades the student to the fallback).
A `list` is not a mutation, so the zero-call enumeration test (which matches five hard-coded mutation markers) is unaffected — do not spend an hour discovering that.
**If N5 is done, cutting this costs nothing dishonest:** the model is told it can create but not search, and will say so plainly instead of guessing.

### N12 — The 20-turn script from a phone that has never texted Bruce `[~1.5h, never cut this]`
Onboard, connect Google, send an email, add two events, read the calendar, refuse a send, send a photo, ask three chatty questions, ask one thing Bruce cannot do. **Record every reply verbatim.** Pass = no fallback line, no "i lost the details", no false completion, no unrequested write.

### Then, and only then
Proactive briefings (T3), the voice layer (T5), the latency architecture (T6), and the automated self-serve onboarding the founder wants after MAE.

---

# §7 — FIRST 30 MINUTES IN THE NEW SESSION

**Verify this document against the repo before editing anything.** If a check fails, say so and stop — do not silently work from a stale handoff.

### Minutes 0–5 · Identity
```bash
cd /Users/dhruvjain/bruce
git rev-parse --abbrev-ref HEAD                 # expect: feat/semantic-executive
git rev-parse HEAD                              # expect: 4cb5ded9be0a83dc91b2d5fc4f37d531d07e8717
git status --short | wc -l                      # expect: 38  (19 modified + 19 untracked)
git diff | git hash-object --stdin              # expect: ff44cbc1b60c55dba5de52b8fdc55c967b345ff6
git stash list                                  # expect: empty
```
If the diff hash differs, §3 is stale — re-derive it with `git diff --stat` before trusting any file list here.

### Minutes 5–10 · Deployment
```bash
curl -s https://bruce-api-3iwweh3bqa-uc.a.run.app/health
# expect: {"status":"ok","commit":"218cc42","env":"staging"}

gcloud run services describe bruce-worker --region us-central1 \
  --format='value(spec.template.spec.containers[0].env[].name)'
# expect: BRUCE_SEMANTIC_SHADOW and BRUCE_ROUTER_SEMANTIC ABSENT (both flags OFF)

gcloud scheduler jobs describe bruce-worker-tick --location us-central1 \
  --format='yaml(schedule,state,httpTarget.uri)'
# expect: * * * * *  ENABLED  https://bruce-worker-3iwweh3bqa-uc.a.run.app/process
```

### Minutes 10–15 · Is the model account alive?
```bash
cd engine && .venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import asyncio; from bruce_engine.semantic_triage import SemanticTriage
print(asyncio.run(SemanticTriage().read('can you email ms rivera and thank her for the rec letter')))"
```
`429 credit_balance_exhausted` → **DEFECT-2 still open; N1 is the whole next action.** A `SemanticTurn` → N1 is done, proceed.

### Minutes 15–25 · The three safety gates (do not skip; they are the trip-wire for every later change)
```bash
cd /Users/dhruvjain/bruce/engine
.venv/bin/python -m pytest tests/test_acceptance_cross_tool.py -q -p no:randomly
#   expect exactly: 16 passed
.venv/bin/python -m pytest tests/test_action_boundary.py tests/test_authorization_zero_call.py -q -p no:randomly
#   expect exactly: 281 passed, 36 skipped
.venv/bin/python -m pytest tests/ -q -p no:randomly -k "authorization"
#   expect exactly: 314 passed, 36 skipped
```
Any movement in these three numbers is a **safety change** and stops all other work until explained. ~4 minutes total.

### Minutes 25–30 · The one live privacy question
```sql
-- against staging, as the migrator/owner
SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'known_people';
```
`false, false` → DEFECT-1 is live and N3 is urgent. `true, true` → 0034 has been applied since this document was written; update §1.2 and §4.

**Then start at N1** (or the first N-step not already done). Do not re-explore the repo. The maps in this document were built from a full read of all **136 modules in `engine/bruce_engine/`, 179 `test_*.py` files (3,396 collected tests), 33 committed migrations + 3 uncommitted**, the macOS relay, the iOS app, `docs/`, `product/`, and the live GCP project — plus an actual run of the full test suite and live reads of Cloud Run, Cloud SQL, Cloud Tasks and Cloud Scheduler.

---

# §8 — REFERENCE

## 8.1 Architecture as deployed

```
 iPhone ──iMessage──► dedicated Mac
                       relay watch (imsg rpc, chat.db)
                          │  POST /v1/relay/inbound   (device bearer + ±5min replay window)
                          ▼
                    Cloud Run bruce-api  (public; only auth + health routes open)
                          │
      ┌───────────────────┴────────────────────┐
      │                                        │
 conversation_runtime.handle              intake_jobs (durable)
 (the whole turn: 2 model calls,               │ Cloud Tasks (OIDC) ──┐
  Gmail/Calendar write, DB writes,             │                      ▼
  all inside ONE synchronous request)          │       Cloud Run bruce-worker (PRIVATE)
      │                                        │        POST /process, 300s timeout
      ▼                                        │        ▲
 messaging_outbound.enqueue (durable row)      │        │ Cloud Scheduler bruce-worker-tick
      ▲                                        │        │  every 60s
      │  POST /v1/relay/outbound/claim         └────────┘
 relay outbound loop ──imsg send──► iPhone
```

- **The DB row is the send.** `QueueChannel.send_message` is a deliberate no-op returning `"queued"`.
- **The checkpoint is marked only after a 2xx**, so an outage retries rather than loses — except on the timeout path (DEFECT-5).
- **Outbound is at-most-once invocation with an explicit ambiguous state**: `send_intent_recorded` → `handed_to_imsg` → `server_acknowledged`, with `handoff_outcome_unknown` surfaced as `terminal_failed` rather than a blind resend or a false "sent".
- Three fail-closed directive checks before every send; the server-side claim gate treats **any** exception as paused.

## 8.2 Gate commands (the ones that matter)

```bash
cd /Users/dhruvjain/bruce/engine        # ALWAYS this cwd, ALWAYS this venv

# the seam you are changing
.venv/bin/python -m pytest tests/<your_new_test>.py -q -p no:randomly

# cross-tool acceptance — what a STUDENT experiences            expect 16 passed
.venv/bin/python -m pytest tests/test_acceptance_cross_tool.py -q -p no:randomly

# execution boundary — a refusal cannot cross into a write      expect 281 passed / 36 skipped
.venv/bin/python -m pytest tests/test_action_boundary.py tests/test_authorization_zero_call.py -q -p no:randomly

# #120 authorization corpus — 238 adversarial turns             expect 314 passed / 36 skipped
.venv/bin/python -m pytest tests/ -q -p no:randomly -k "authorization"

# migration discipline + pinned head                            expect 15 passed
.venv/bin/python -m pytest tests/test_migration_discipline.py tests/test_migration_rls_context.py -q -p no:randomly

# the whole manifest (13 checks + safety baseline)
.venv/bin/python scripts/run_gates.py
.venv/bin/python scripts/run_gates.py --accept-safety-change "<why this safety change is correct>"

# full suite                                                    ~16 min, expect 3316 passed
.venv/bin/python -m pytest -q -p no:randomly --timeout=900
```

Notes: `-p no:randomly` is a **no-op locally** (`pytest_randomly` is not installed in `engine/.venv`) — keep passing it to match CONTRIBUTING, but do not assume it is buying determinism. `tests/conftest.py:38` auto-loads `engine/.env`, so every local run picks up `OPENAI_API_KEY` — which is why the local full suite takes 16 minutes and CI takes ~3.

**The language gate is green-by-not-measuring without a key.** `unset OPENAI_API_KEY` makes the suite pass by skipping the only tests that read real language. `BRUCE_REQUIRE_LANGUAGE_EVAL=1` (set by the deploy path) turns a silent skip into a failure. **Never report a green suite that skipped the language gate as evidence of understanding.**

## 8.3 Environment and flags

Hard-required to serve traffic: `BRUCE_JWT_SECRET` (≥32 bytes), `BRUCE_APP_DATABASE_URL`, `BRUCE_DATABASE_URL`, `BRUCE_LINK_CODE_PEPPER` (**RuntimeError if unset; `.strip()` is load-bearing — Cloud Run secret payloads keep a trailing newline**), `BRUCE_APPLE_CLIENT_ID`, `BRUCE_ENCRYPTION_KEY`, `OPENAI_API_KEY`.

`BRUCE_ENV` ∈ {`local`,`staging`,`production`} is a **strict enum with no silent fallback**. A typo raises `InvalidEnvironment`, which is caught and converted to DENY in `conversation_access` and to "paused" in the relay claim gate. **Bruce will accept messages, log an error, and never send anything.**

Kill switches that do less than their names imply: `BRUCE_AUTHZ_OFF` disarms only the *adapter backstop* (the gateway still denies); `BRUCE_BROKER_AUTHORITY_OFF` does **not** gate `reachable_operations`.

Read at **module import** (so `monkeypatch.setenv` after import does nothing): `BRUCE_SHADOW_BUDGET_S` (20), `BRUCE_SHADOW_RECONCILE_WINDOW_S` (3600), `BRUCE_SHADOW_RECONCILE_LAG_S` (60), `BRUCE_TRIAGE_TIMEOUT_S` (2.5), `BRUCE_WORKER_DRAIN_MAX` (25). Read at **call time** (patchable): `BRUCE_SEMANTIC_SHADOW`.

Cloud Tasks needs **all five** or dispatch is a silent no-op: `BRUCE_TASKS_PROJECT`, `BRUCE_TASKS_LOCATION`, `BRUCE_TASKS_QUEUE`, `BRUCE_WORKER_URL`, `BRUCE_TASKS_INVOKER_SA`.

Absent from `.env.example` but required: `BRUCE_ENCRYPTION_KEY`, `BRUCE_LINK_CODE_PEPPER`, and all four `BRUCE_SHADOW_*`/`BRUCE_SEMANTIC_SHADOW` vars.

## 8.4 RLS and privacy constraints

- Runtime connects as the restricted role `bruce_app` (no `BYPASSRLS`, DML-only grants). Migrations use the owner URL.
- Session variables: **`app.user_id`** (tenant), **`app.worker='on'`** (worker), **`app.admin='on'`** (operator). `admin_session()` **refuses** to open if a tenant `app.user_id` is already set on the connection.
- Many tables are `FORCE ROW LEVEL SECURITY` — the owner is subject to the policy too. `ENABLE` alone is not enough.
- **A plain `SELECT` under a session with no `app.user_id` returns 0 rows without erroring.** Any read proving an absence must first prove it can see rows (compare `pg_stat_user_tables.n_live_tup`, which RLS does not filter) or abort. This has produced a false clean sheet in this project more than once.
- Migration `0034` replaces rather than joins the old policy — two permissive policies are OR'd, so leaving both would let the looser predicate silently widen the newer one. It uses the fail-closed helpers `app_current_user()` / `app_is_worker()` / `app_is_admin()`, not a raw `current_setting(...)::uuid`.
- Shadow telemetry is narrowed to booleans, counts, latencies, registry ids and repo-owned labels: `missing_information` → `missing_count`, `validation_notes` → `validation_codes` (a closed vocabulary of 14 `CODE_*` constants). The student's words are asserted absent from what is persisted.
- The reconciliation function is aggregate-only, `SECURITY DEFINER`, `SET search_path`, owned by a `NOLOGIN` role, `REVOKE ALL FROM PUBLIC`, and refuses unless `app_is_worker()`. **A row-level worker grant was rejected deliberately** — `FOR SELECT USING (app_is_worker())` would hand over every column of every student's turn, `text` included.

## 8.5 Migration history situation

- Committed chain ends at **`0032_known_people`**. Staging DB is at `0032`.
- The working tree adds **`0033_semantic_shadow_jobs` → `0034_known_people_row_security` → `0035_shadow_reconciliation`**, all untracked.
- Lane reservations added at `migrations/lanes.py:38-56`; head pin bumped in **two** places in `tests/test_migration_rls_context.py` (`:186` and `:301` — bumping only one leaves a passing test with a stale assertion); `ci/gates.json` pin bumped to `0035`.
- `test_migration_discipline.py` enforces both the lane reservation and the head bump **in the same commit as the migration**.
- Three new test files still reference migrations **0036/0037/0038 that do not exist** — the chain was collapsed and the prose was not.
- `0035` **skips creating its function entirely** when the migrator lacks `BYPASSRLS`-provisioning rights — the tested Cloud SQL shape.

## 8.6 TDD and mutation-proof workflow (non-negotiable in this repo)

1. Failing **production-surface** regression first — drives `conversation_runtime.handle` (or the real entry point) against real Postgres and asserts on **what the provider and the database hold afterwards**, never on what a handler believed.
2. Run it and prove the **exact** failure reason. If it fails for a different reason, you have not reproduced the bug.
3. Focused unit tests on the smallest broken seam.
4. Implement.
5. **Revert the fix (not the tests), run them, confirm they fail, restore.** A test that passes both ways proves nothing. Record which tests failed and why.
6. Focused → 7. acceptance → 8. boundary + corpus → 9. full suite → 10. deploy only when every gate is green → 11. replay the exact scenario live and **prove it from the database**, not from a reply that looks right.

The mutation harness (`scripts/mutation_runner.py`, uncommitted) is the stronger form of step 5 — 37 specs, 5 outcomes where `ZERO_EDIT` is a **failure** not a pass, rsync'd tree isolation, canonical fingerprint re-checked after every mutation. **It mutates cluster-wide roles** (`bruce_app`, `bruce_shadow_recon`) which outlive `DROP DATABASE`; never Ctrl-C it mid-run without re-running `reset_cluster_roles()`.

## 8.7 Documents that are STALE — do not act on them

| Document | Claims | Reality |
|---|---|---|
| `README.md` | professor-outreach wedge, "engine scaffolded, app not started, no send authority" | the code is a general iMessage action agent that sends real mail |
| `docs/wedge.md` | v1 = grounded professor outreach, "explicitly NOT building Gmail OAuth" | Gmail OAuth is live |
| `docs/staging-verification.md` | commit `0056988`, head `0009_relay_uploads` | commit `218cc42`, head `0032_known_people` |
| `docs/alpha-readiness.md` | head `0005_intake_jobs`, deployment "not done" | deployed since; head `0032` |
| `docs/brain-spine-handoff.md` | staging at `0030_claim_lineage`; founder enrollment EXPIRED 2026-07-29 | head is `0032`; enrollment state `[UNVERIFIED]` |
| `docs/reachability-audit.md` | `contract.py`, receipts, `calendar_adapter`, `oauth_google` marked DEAD | all four are live; the file is pinned to commit `9456b20` |
| `product/capabilities.yaml` | `CAP-EMAIL-001/002`, `CAP-CAL-001`, `CAP-APPROVE-001`, `CAP-NOTIFY-001` = `planned`; `CAP-Q-DUE/CHANGED/MISSING` = `implemented` | first five are live; the query capabilities have **zero importers** |
| `product/integrations.yaml` | "No provider is CONNECTED in any environment" **and** `connection_status: connected`, 50 lines apart | self-contradictory; `live_verification: false` is the honest line |
| `messaging.py:29-33` docstring | "NOTHING HERE IS LIVE… Do NOT describe iMessage as functional" | iMessage is the only live channel |
| `tool_registry.py:43-44` | calendar update/delete `live=False` | both are `live=True` and reachable |
| `tests/test_acceptance_cross_tool.py` docstring | several tests "expected to be red" (defects D1–D5) | **all 16 pass.** A newcomer told to ignore red there would ignore a genuine regression |
| `gmail.get_message` / `get_thread` / `verify_sent` | `live=True` in `tool_registry.py:71,73,78` | **no executor exists**; the strings appear nowhere else. They are counted as reachable by `tool_broker.reachable_operations`, which is the capability-truth snapshot the shadow metrics depend on |

---

# §9 — DO NOT DO

1. **Do not `git stash`.** It buries the `known_people` RLS fix (DEFECT-1), which is a live cross-tenant exposure.
2. **Do not report a green suite as evidence of understanding** if `OPENAI_API_KEY` was unset or out of credits. The language gate goes green by not measuring.
3. **Do not trust the 99.57% / 97.37% / 0-false-action numbers** from commit `0076e10`, or the shadow-eval `n=13 / n=46` numbers from `85cbef3`. Neither has a committed artifact; the shadow-eval input file (`production_turns.txt`) does not exist; the shadow code has been rewritten 165 → 2315 lines since. **Keep the thresholds, delete the numbers.**
4. **Do not weaken an assertion, a safety rule, or a gate to make CI green.** If a feature needs the boundary relaxed, decide out loud which is wrong first.
5. **Do not "fix" a moved gate number by editing `ci/gates.json`.** `check_safety_baseline_unchanged` will refuse, and it is right to.
6. **Do not use `inspect.getsource` as behavioural proof.** The one legitimate use is a structural invariant nothing else can express, and it must be labelled as such.
7. **Do not deploy inert architecture.** A module nothing calls is not shipped. Six spine layers were once built, tested green, and changed a student's experience by zero characters.
8. **Do not claim completion from reported results — including your own, including a subagent's — without re-running them on the exact code being merged.**
9. **Do not use `founder_bootstrap.py`'s namespace.** Use `create_link_code.py`'s. They derive different user ids from the same label (DEFECT-7).
10. **Do not deploy with `gcloud run services update --set-env-vars`** — it wipes the 12 `secretKeyRef` bindings. Use `--update-env-vars`.
11. **Do not run `scripts/mutation_runner.py` casually.** It mutates cluster-wide Postgres roles that survive `DROP DATABASE`, and a failed reset silently disables RLS for every subsequent test in the session.
12. **Do not turn on `BRUCE_ROUTER_SEMANTIC`.** It grants the executive authority on a metric that is currently unmeasurable, and adds a 2.5s call to a latency budget that is already over target.
13. **Do not turn on `BRUCE_SEMANTIC_SHADOW`** until the worker's `health()` path is gated on `enabled()` (§3.3, 🟠) and `classify_turn`'s 6-char exclusion is fixed — otherwise you get permanent `reconciliation_status: failed` and a sample missing every `"cancel"`.
14. **Do not split `semantic_shadow.py` into modules.** `run_gates.check_shadow_is_inert` substring-matches forbidden imports **in that one file**; splitting it moves half the pipeline outside the gate.
15. **Do not add a `DEFAULT '[]'` to `semantic_shadow_jobs.reachable`.** `NULL` means "we could not find out"; `[]` means "we looked, nothing reachable." A default silently converts every unknown into evidence of a capability denial.
16. **Do not widen `mission_planner`'s unconfirmed send lane** (`conversation_runtime.py:539-548`) — it can send real email inside a turn with no confirmation. One consent regime.
17. **Do not "fix" `reconciliation_status: failed` by making the anti-join tolerate missing rows.** That restores the self-agreeing invariant the whole round removed.
18. **Do not convert the internal-test routes to an `APIRouter`.** The pinned FastAPI 0.139 / Starlette 1.3 pair collapses a prefixed router into an unusable mount; `app.add_api_route` is deliberate.
19. **Do not chase a "relay is stale" alert** without checking `last_seen_at` — `supervisor_seen_at` is not refreshed while healthy (DEFECT-19).
20. **Do not hardcode one transcript.** Tests must describe the *shape* of the failure, not replay the founder's thread.
21. **Do not write a motivational product essay in place of evidence.** Every claim gets a category tag and a command.

---

# §10 — EVIDENCE LEDGER: what is trustworthy, and what went stale

| Result | Source | Status |
|---|---|---|
| boundary 281/36, corpus 314/36, acceptance 16/0 | local run **2026-08-10** on the current tree | **TRUSTWORTHY** — re-verified today |
| 139 new shadow tests passing against real Postgres | local run 2026-08-10 | **TRUSTWORTHY** — but they cover untracked code |
| full suite 2 failed / 3316 passed / 77 skipped / 1 xfailed | local run 2026-08-10 | **TRUSTWORTHY**; the 2 failures are a billing outage, not code |
| `run_gates.py` 13 OK / 1 FAIL | local run 2026-08-10 | **TRUSTWORTHY**; the FAIL is the deliberate migration-head bump |
| staging commit `218cc42`, head `0032`, both flags off | live GCP reads 2026-08-10 | **TRUSTWORTHY** |
| language rates 0.9957 / 0.9737, false_action 0 | commit message `4cb5ded` | **INVALID as current evidence** — commit-message only, no artifact, and the harness has since been modified (+62 lines) and cannot be re-run without credits |
| shadow-eval n=13 real turns / n=46 corpus | commit message `85cbef3` | **INVALID** — input file does not exist; `semantic_shadow.py` has been rewritten 14× larger since |
| "gates unchanged by the wiring: 297 passed / 36 skipped" | commit message `85cbef3` | **CONSISTENT** with today (281 + 16 = 297) |
| "the founder-sequence acceptance test is red" | `tests/test_acceptance_cross_tool.py` docstring, `docs/brain-spine-handoff.md` §3 | **INVALID** — all 16 pass today |
| "Bruce sends real mail for real students" | `CONTRIBUTING.md:5` | **UNPROVEN** — no artifact of a real Gmail send exists; both live-credential tests skip |
| "nothing wakes the worker" | repo-only analysis | **INVALID for staging** — `bruce-worker-tick` fires `/process` every 60s |
| the 2 language failures are "blocked network egress" | one mapping subagent | **INVALID** — `/v1/models` returns 200 with the same key; the 429 body says `credit_balance_exhausted` |
| `docs/staging-verification.md`, `alpha-readiness.md`, `brain-spine-handoff.md`, `reachability-audit.md`, `capabilities.yaml`, `integrations.yaml` | docs | **STALE** — see §8.7 |

---

# §11 — OPEN QUESTIONS ONLY THE FOUNDER CAN ANSWER

Ask these; do not guess. None blocks N1–N4.

1. **Is there a dedicated Mac running the relay right now, and where?** It is not this machine. Everything downstream of "text Bruce" depends on the answer.
2. **Does any `ProductionAccountEntitlement` row exist in staging?** (`python -m scripts.capability_admin list`.) Determines whether even the founder currently has conversation access — `docs/brain-spine-handoff.md:32` records the founder enrollment as **EXPIRED** at 2026-07-29.
3. **Has a real Gmail send ever succeeded?** If yes, produce the message id and the ledger row; that single artifact retires §5.4.
4. **Which Google account owns the OAuth client, and are `gmail.send` + `gmail.readonly` actually granted on the founder's integration** (not just `calendar.events`)? DEFECT-14 makes a partial grant unrecoverable.
5. **What is the monthly model budget ceiling?** Two model calls per goal turn, no per-user quota, and the current alert is a **$15/month warning, not a cap**.
6. **Legal posture for minors** — the stated users are high-school students; there is no ToS, no privacy policy, no consent flow, and no COPPA/FERPA position anywhere in the repo, for a product that reads their messages and sends mail as them.
7. **Cloud SQL is `db-f1-micro`, single-zone, no HA, no automated backups.** Losing it loses every authorization record, every open goal, and every learned recipient. Turn on backups?
