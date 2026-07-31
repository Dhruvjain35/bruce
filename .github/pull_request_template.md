<!--
Bruce sends real mail for real students, and every serious defect this project has shipped was invisible
to a green suite. This template exists so a reviewer can tell the difference between a change that was
PROVEN and one that merely passes. See CONTRIBUTING.md.

Delete a section only if it genuinely does not apply, and say why.
-->

## Failing test before

<!-- The production-surface regression, written FIRST, that reproduces the real user behaviour. Name the
     test and paste the failure. "It failed" is not enough — show the assertion. -->

```
tests/…::test_…
E   AssertionError: …
```

## Root cause

<!-- The exact seam, traced rather than guessed. If a value disappears between two layers, show the stages
     and mark the FIRST point of loss. Not "the model was wrong" — where the join is missing. -->

## Minimal fix

<!-- The smallest generic change. If it is a special case, say why the generic version is worse. If it
     touches a declared vocabulary (slots, capabilities, aliases), say whether the entry is DERIVED or
     hand-listed, and why. -->

## Mutation / revert proof

<!-- Revert the FIX (never the tests), run the new tests, confirm they fail, restore. A test that passes
     both ways proves nothing. List which tests failed and the reason they gave. -->

```
with <the fix> reverted:
  N failed, M passed
  tests/…::test_…  — failed because …
```

## Focused test counts

```
tests/<new>.py                     N passed
tests/<seam>.py                    N passed
```

## Safety gate counts

<!-- Pinned. If ANY of these moves, this is a safety change: add a section explaining exactly what moved
     and why that is correct. "It went up" is not automatically good. -->

| gate | expected | this PR |
|---|---|---|
| execution boundary | 281 passed / 36 skipped | |
| #120 authorization corpus | 314 passed / 36 skipped | |
| cross-tool acceptance | 16 passed / 0 failed | |

## Full suite

```
N passed, M skipped, 0 failed
```

<!-- Re-run on the EXACT code being merged, not on an earlier edit. -->

## Live proof

<!-- When this reaches a student: the replayed scenario, and what the DATABASE and the PROVIDER hold
     afterwards. A reply that looks right is not proof. For anything that writes to a provider, prove:
     zero provider calls before confirmation, exactly one operation after, one fetch-back verification,
     one receipt, success claimed only after verification, and no duplicate goal / Decision / execution
     attempt / provider action.

     If it does not reach a student yet, say so plainly rather than leaving this blank — an unwired
     module is not shipped. -->

## Checklist

- [ ] The regression test was written and failing **before** the implementation
- [ ] The new tests fail when the fix is reverted
- [ ] No assertion was weakened to make CI green
- [ ] No safety rule was relaxed to pass a feature test
- [ ] No `inspect.getsource` used as behavioural proof
- [ ] No debug/scratch tests committed
- [ ] Counts above were re-run on the exact code being merged
- [ ] Any DB count was read through the runtime's accessor or checked against `n_live_tup` (RLS returns 0 rows silently)
- [ ] Migration: number reserved in `migrations/lanes.py` and pinned head bumped, same commit
