# Bruce v1 — the wedge and the build plan

_Last updated: 2026-07-13. Supersedes the roadmap/first-slice in `bruce_product_engineering_spec_v0.7.md` for v1. Keeps that spec's safety principles._

## The wedge

**Bruce helps an ambitious student turn "I want a research position / internship in X" into
grounded, genuinely personalized outreach emails to the right professors — researched,
drafted, student-reviewed, and sent the right way, then tracked for one tasteful follow-up.**

Not "everything for students." One job, done better than anyone.

### Why this wedge

- **Lived pain.** The founder is doing real research (polariton ML w/ Notre Dame) and has
  done professor outreach. He knows a good email from slop. The incumbents' teams do not.
- **Underserved.** Poke treats "school" as 1 of 8 recipes. Generic ChatGPT produces the
  exact templated garbage professors delete. Nobody owns *grounded, personalized, ethical*
  student→professor outreach.
- **Adjacent to real money.** Students are broke, but the ecosystem around ambitious
  students (admissions/research consulting) is where parents already spend thousands.
- **Distribution the founder actually has.** School network, incubator, peers.

## Non-negotiables (the product IS these)

1. **Grounded.** Every claim about a professor traces to a verifiable source. Never fabricate
   a person, a paper, or an email.
2. **Anti-volume.** The value is that each email reads like the student spent an hour on it —
   not that 50 went out. Mass-identical email is banned by design; it torches the student's
   reputation and deliverability.
3. **Human-in-the-loop.** Bruce drafts. The student reviews, edits, and sends every email.
   Nothing auto-sends.
4. **Voice-matched.** Drafts sound like the student, not like an AI.
5. **Honest uncertainty.** Anything unsure is surfaced, never guessed and shown as fact.

## First slice (what we build first)

The **engine** (backend, no UI) that runs one flow end to end:

```
student profile + goal
  → grounded discovery of real, fitting professors (with cited recent work)
  → verification that every person + paper actually exists
  → one genuinely personalized, voice-matched draft per professor
  → (student reviews/edits/approves/sends — client layer, later)
  → follow-up tracking
```

**The test that decides everything:** an ambitious student reads the output and thinks
*"this is better than what I'd write, and every fact in it is real."* If yes → build the
native iOS client around it. If no → fix the engine; nothing else matters yet.

## Sequence

1. **Engine MVP** (in progress) — models done; discovery/verify/drafting land on the
   grounding research pass. Runs from a script; output inspected for quality.
2. **Prove the brain** — put raw engine output in front of ~10 real ambitious students;
   measure whether they'd actually send it.
3. **Native iOS client** — thin, beautiful SwiftUI client on the engine API. Only after (2).
4. **Distribution + follow-up loop, then expand** to the next adjacent student job.

## Explicitly NOT building yet

Gmail/Drive OAuth, browser automation, Temporal, OPA, gVisor, Messages for Business,
Dynamic Island, the general action-agent surface. All destination, not starting line.

## Open dependencies

- **LLM API key + budget** (Anthropic) to run the drafting step. Enforce a hard cap.
- Grounding research pass output → informs `discovery.py` / `verify.py` / `drafting.py`.
