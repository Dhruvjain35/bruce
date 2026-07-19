# Build Execution Board (v1)

Ordered by **(1) dependency leverage → (2) user-visible value → (3) ability to validate → (4) security
risk → (5) implementation cost**. Grouped under shared primitives, not hundreds of independent tickets.
Every future PR cites capability IDs (see `product/capabilities.yaml`).

---

## P0 — Messaging conversation brain (Bite 1) — ✅ DONE (flag-gated, live UNVERIFIED)
The foundation: every linked message enters a real multimodal conversational agent.
- CAP-CONV-001 conversation + reasoning router · CAP-CONV-002 provider-neutral reasoner ·
  CAP-VOICE-001/002 Voice OS + fact guard · CAP-MSG-002 attachment transport ·
  CAP-MISSION-001 outbound exactly-once · CAP-CONV-005 model/attachment failure recovery ·
  CAP-CONV-004 event candidate capture.
- **State:** merged (PRs #22–#25 + event-detection fix); deployed to staging `8bec4a5`; migration
  `0011` applied; flag OFF; LEVEL A (fake) + LEVEL B (real gpt-5.4-mini) pass on staging.
- **Remaining P0 gate:** the tiny supervised live dry-run (enable flag for one handle → "yo what's
  up" / worksheet / ticket → 3 useful replies, exactly one per turn) — **awaiting explicit approval.**
- **P0 follow-up (from LEVEL C):** re-verify / redesign delayed-attachment resolution (imsg has no
  `message.get`); confirm real imsg attachment field names in the dry-run.

## P1 — Student command center (the north star: what's due / changed / missing)
Highest leverage next: Bruce knowing the student's real academic state without them checking portals.
Build the **primitives first**, then the three questions fall out cheaply.
1. **CAP-SCH-001/002 SchoolConnector framework** + connector access ladder (protocol + capability
   matrix; honest `unsupported` instead of faking every school). *Primitive → unlocks Canvas + others.*
2. **CAP-ACG-001 canonical academic graph** (courses/assignments/submissions/grades + sync cursors).
3. **CAP-CANVAS-001 Canvas adapter** (read/sync) — *blocked on founder Canvas OAuth*; build against
   provider fakes + contract tests until credentials exist.
4. **CAP-CHG-001 change detection** with original-object links.
5. **CAP-Q-DUE / CAP-Q-CHANGED / CAP-Q-MISSING** — "what do I have due / what changed / what am I
   missing?" answered from the graph (cheap once 1–4 exist).

## P2 — School work + communication
6. **CAP-GRD-001 grades + feedback + academic-risk.**
7. **CAP-EMAIL-001/002 Gmail/Outlook ingestion → mission**, then draft/send/verify (read-first; no
   send at connect; approval-gated).
8. **CAP-CAL-001 real Calendar execution** — turn an EventCandidate into a verified event (Bite 1 only
   captures candidates + says "not connected").
9. **CAP-APPROVE-001 / CAP-VERIFY-001 approval + external verification engine** — the safety spine for
   every consequential action.
10. **CAP-NOTIFY-001 durable follow-up notifications** — so Bruce can honestly say "I'll message you
    when it needs review" (Bite 1 never promises this because no notifier exists).

## Later waves (grouped, not exploded)
- Wave 4 visual/interactive tutoring (CAP-TUTOR-VISUAL) · Wave 3 opportunities + grounded outreach
  (CAP-OPP-001, `partial` — wire the existing discovery/drafting into a messaging mission) · Wave 5
  forms/browser automation · Wave 7 travel/commerce · Wave 8 financial authorization (guardian model
  for minors; provider-tokenized; **critical** risk — fakes + contract tests before any live rail) ·
  Wave 9 partner scale (Apple MfB, LTI/OneRoster) — all `planned`/`partner_required`.

## Sequencing rationale
P1 SchoolConnector+graph is the biggest **leverage** node (unlocks Canvas, the three questions, grades,
change detection, briefs). It is also the north star — "Bruce can chat" (P0) is table stakes; "Bruce
knows what you have due / changed / are missing" is the moat. Payments/browser/travel are deferred
because they are high-risk and low-leverage until the academic core exists.
