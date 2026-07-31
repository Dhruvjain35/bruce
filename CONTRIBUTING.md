# Contributing to Bruce

## Why this document is short and non-negotiable

Bruce sends real mail and writes to real calendars for real students. Every serious defect this project
has shipped was **invisible to a green test suite**, and several were found only after deployment. Not one
of them was a broken function:

| what shipped | what the suite said |
|---|---|
| `goal_slots` declared `calendar.create_event` and `_EXECUTORS` had no row for it — a table with a hole in it | green |
| `agent_run_store._derive_guard_ctx` compared SLOT names against TOOL arg names, so `executing` was unreachable and **no send could ever have run** | green |
| `memory_finalize` had a caller and refused every write — "memory is wired" was true of the call graph and false of the database | green |
| `directive_scope._NEG` spelled `don't` with a straight quote, so `don’t add it` deleted a calendar event | green |
| the model emitted `email.send_message`; the registry id is `gmail.send_message` — no goal, no Decision, and Bruce promised the send anyway | green |
| the model labelled an address `type='email'`; the slot is `recipient` — the address was dropped and Bruce asked for what it had just been given | green |
| the goal lane never passed `parent_run_id`, so every execution attempt recorded a provider write naming no goal | green |
| `response_composer` rewrote a **verified** calendar receipt into "i didn't actually put that on ur calendar yet" | green |

Every one was found by *reading* or by a *live turn*, never by CI. A suite that was written after the code
tests what the code does. The point of the workflow below is to make the suite test what a **student**
experiences, before the code exists to satisfy it.

## The workflow — strict TDD, no exceptions

### Before implementation

1. **Add a failing production-surface regression** reproducing the real user behaviour. Production-surface
   means it drives `conversation_runtime.handle` (or the equivalent real entry point) against real
   Postgres, and asserts on what the **provider and the database hold afterwards** — never on what a
   handler believed.
2. **Run it and prove the failure reason.** Not "it fails" — the exact assertion and the exact cause. If it
   fails for a different reason than the bug you are fixing, you have not reproduced the bug.
3. **Add focused unit tests for the smallest broken seam.** The one function where the behaviour is
   actually wrong.

### After implementation

4. **Prove the new tests fail when the fix is reverted.** Revert the fix (not the tests), run them, confirm
   they fail, restore. A test that passes both ways proves nothing. Record which tests failed and why.
5. **Run the focused tests.**
6. **Run cross-tool acceptance** — `tests/test_acceptance_cross_tool.py`.
7. **Run the safety gates** — the execution boundary and the #120 authorization corpus. These are pinned
   counts. If either moves, the change is wrong until proven otherwise.
8. **Run the full suite.**
9. **Deploy only when every gate is green.** One immutable digest, both services.
10. **Replay the exact scenario live** and prove it from the database, not from a reply that looks right.

```bash
cd engine
.venv/bin/python -m pytest tests/<your_new_test>.py -q -p no:randomly          # 1,2,3,5
.venv/bin/python -m pytest tests/test_acceptance_cross_tool.py -q -p no:randomly           # 6
.venv/bin/python -m pytest tests/test_action_boundary.py \
                          tests/test_authorization_zero_call.py -q -p no:randomly          # 7
.venv/bin/python -m pytest tests/ -q -p no:randomly -k "authorization"                     # 7
.venv/bin/python -m pytest -q -p no:randomly                                               # 8
```

### The pinned gates

| gate | expected |
|---|---|
| execution boundary | 281 passed / 36 skipped |
| #120 authorization corpus | 314 passed / 36 skipped, all 24 categories unchanged |
| cross-tool acceptance | 16 passed / 0 failed |

A change that moves any of these is a safety change and must say so in its own section of the PR, with the
reasoning. "It went up" is not automatically good: the corpus contains 36 legitimate requests, and a
boundary that refuses everything scores perfectly on the traps.

## Never

- **Never hardcode one transcript.** The founder transcript motivated the spine; a suite that only replays
  it measures replaying it. Tests must describe the *shape* of the failure.
- **Never use source-inspection tests as behavioural proof.** `inspect.getsource(...)` asserts that a
  string appears in a file. It cannot tell you the code runs, is reachable, or is correct. The one
  legitimate use is a structural invariant nothing else can express (e.g. "every provider mutation is
  preceded by the gate"), and it must be labelled as such.
- **Never weaken a safety rule to pass a feature test.** If a feature needs the boundary relaxed, the
  feature is wrong or the boundary is wrong; decide which, out loud, before touching either.
- **Never weaken an assertion to make CI green.** If a test fails, the fix is the code or the test's
  *premise* — never its strictness. A real example from this repo: a test asserted a reply must not contain
  the word "subject". It does — the proposal *shows* the draft, which is the opposite of asking for one.
  The right fix was to assert on the ask-shape. The wrong fix was to soften the copy.
- **Never commit debug tests.** Scratch harnesses go in a scratchpad, not in `tests/`.
- **Never deploy "inert" architecture.** A module nothing calls is not shipped. Six spine layers were built
  and tested green while nothing called them, and a student's experience did not change by one character.
  Wire it, or do not claim it.
- **Never claim completion from reported results without rerunning them.** Including your own, and
  including an agent's. Re-run the gates on the exact code you are about to commit — not on the code you
  had three edits ago.

## Reading is a review technique, not a fallback

Budget time to read the diff *and its neighbours*. Most defects in the table above are joins that do not
exist: a registry row missing, two vocabularies that never met, a caller that was never added. No test
finds an absence you did not think to look for; reading does.

Two habits that repeatedly paid off here:

- **Check RLS before believing a count.** Several tables are `FORCE ROW LEVEL SECURITY`, and a plain
  `SELECT` under the wrong session returns **0 rows without erroring**. Always compare against
  `pg_stat_user_tables.n_live_tup`, or read through the runtime's own accessor. "The environment is empty"
  has been asserted wrongly from a filtered read more than once in this repo — including by an agent that
  had just been warned about it.
- **Prefer deriving to hand-listing.** Alias maps, capability tables and slot vocabularies are generated
  from the registry wherever possible, so a rename cannot leave a stale entry pointing at nothing.

## Migrations

Reserve the number in `migrations/lanes.py` and bump the pinned head in `tests/test_migration_rls_context.py`
**in the same commit** as the migration. `tests/test_migration_discipline.py` enforces both.
