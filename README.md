# Bruce

> Internal codename. Not cleared for public/marketing use. Do not launch publicly as this name — trademark, domain, and App Store review pending.

**Bruce is the trusted handoff layer for ambitious students.**

The wedge (v1): a student says *"I want a research position / internship in X."* Bruce
finds the *right, real* professors whose recent work actually fits, drafts a genuinely
personalized outreach email for each one grounded in their real papers, and the student
reviews, edits, and sends every email themselves. No spam. No fabrication. No fake "done."

This is not "an AI assistant that does everything for students." It is one job — research
and internship outreach done *the right way* — done better than anyone. Depth in one job,
not breadth across eight. Breadth is where we lose to Poke; depth in a job Poke treats as a
side-recipe is where we win.

## Why this shape

- **Engine first, app second.** The value is the *brain* (grounded discovery + real
  personalization), which is a backend service with an API. The native iOS app is a thin,
  beautiful client on top of that API. A gorgeous UI over a mediocre engine is the AI slop
  we refuse to ship. We prove the brain, then dress it.
- **Grounding is non-negotiable.** Every factual claim about a professor traces to a
  verifiable source. Emailing a professor about a paper they didn't write is instant
  credibility death, so the engine never invents people, papers, or emails.
- **The student is always in the loop.** Bruce drafts; the student approves and sends.
  Anti-spam by construction.

## Layout

```
bruce/
├── engine/            # the brain — grounded professor discovery + outreach drafting (Python)
│   └── bruce_engine/  #   models.py · discovery.py · verify.py · drafting.py · pipeline.py
├── app/               # the native iOS client (built after the engine is proven)
└── docs/
    └── wedge.md       # the current build plan (supersedes the general-Bruce spec for v1)
```

## Status

`engine/` scaffolded — data models real, discovery/verify/drafting implementations pending
the grounding research pass. `app/` not started (waiting on a proven engine).

## Provenance of the plan

The original `bruce_product_engineering_spec_v0.7.md` describes *general Bruce* (a universal
action agent). Its **principles** — verified completion, grounding, draft-before-send,
human-in-the-loop approval, cost caps, no fake "done" — carry over and we keep them. Its
**roadmap and first slice** (screenshot → calendar event, Gmail/Drive/Temporal/OPA) are for
the broad product and are superseded for v1 by `docs/wedge.md`.
