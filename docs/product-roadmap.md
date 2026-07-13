# Bruce — product roadmap: the operating system for student life

_Canonical build reference. Bruce receives the chaos around school and converts it into
organized, tracked, **verified** action. Not homework help — it organizes, prepares, tracks,
executes, and verifies the administrative work around school life._

**Core loop (the one that matters):**
> Forward or text anything school-related → Bruce understands it → creates the right mission →
> asks only when needed → tracks it until **verified** completion.

**North-star metric:** student work handled per decision the student has to make.

---

## The 25 systems (condensed)

1. **Opportunity engine** — find scholarships/competitions/internships/programs/research/
   hackathons/volunteering/fellowships/clubs; read opportunity emails (eligibility, deadline,
   cost, reqs, links); dedupe + de-spam; rank by grade/interests/location/availability/goals;
   explain the fit; spin up an application mission; track items; draft inquiries; track recs;
   warn on missing materials; monitor deadlines. Killer line: "4 genuinely relevant this week,
   two need no essay, one closes Friday" — not fifty links.
2. **Unified assignment engine** — merge assignments from email + Google Classroom + Canvas +
   Teams + PDFs/syllabi/screenshots; one canonical list; state (assigned→started→ready→
   submitted→graded→missing); due today/tomorrow/week; workload estimate; break into steps;
   calendar work-blocks; verify submission where an integration permits; warn when it can't.
3. **Student inbox engine** — turn school email into actions; identify senders/roles; extract
   deadlines + action items; draft replies in the student's tone; summarize threads; separate
   real opportunities from junk; track rec-letter threads; ask before sending; verify before send.
4. **Calendar & schedule intelligence** — build events from screenshots/emails/flyers/PDFs/texts;
   conflicts + alternatives; travel/prep time; work blocks; protect sleep/meals/school; rotating/
   block-day schedules; morning + evening brief. Maintain the calendar, don't be another calendar app.
5. **Deadline & document extractor** — accept ugly inputs (screenshots, whiteboard photos, flyers,
   PDFs, syllabi, webpages, forms); extract dates/reqs/costs/contacts/docs/eligibility → "found 3
   deadlines + 2 required docs — create a mission?"
6. **Study planning** — syllabus→semester plan; exam→study schedule; spaced review; practice Qs
   from the student's notes; quiz interactively; find weak topics; flashcards; "teach it back."
   Keep the action angle (scheduled sessions + review set + Thursday quiz), not just notes.
7. **School-document workspace** — classify/rename/connect docs to classes+applications; detect
   missing; track expiry; versioning; Context Capsules (only the docs one mission needs, deleted
   after); show exactly what will be shared.
8. **Application manager** — checklists for colleges/scholarships/programs; track essays/
   transcripts/forms/fees/recs/tests/deadlines; reusable approved background facts; outlines;
   authenticity review; word limits; contradiction detection; submission receipt.
9. **Academic progress engine** — track grades; performance by class; missing work; hypothetical
   scenarios; largest-impact upcoming item; recovery plan. Supportive, never shame/ranking.
10. **Teacher/counselor communication** — draft respectful emails; de-escalate emotional drafts;
    absence notes; clarifications; meeting/rec requests; thank-yous; post-meeting action capture.
11. **Daily command center** — 5 useful lines: morning (classes/due/events/priority/blocked),
    after-school (changed/needed/order/conflicts/closing soon), night (done/moved/at-risk/prepped).
12. **Dynamic Island mission tracking** — observable work only ("Extracting 7 deadlines",
    "Application 4/6 ready", "Teacher reply received", "One decision needed"). No fake thoughts.
13. **Decision Queue** — one place with only decisions Bruce can't safely make; everything
    mechanical already done. Each answerable in one tap/swipe/Face ID/short edit.
14. **Personal protocols** — explicit, suggested-then-approved, editable, reversible rules
    ("never schedule work before school", "formal with counselors", "Face ID for transcript").
15. **Relationship graph** — roles (teacher/counselor/coach/advisor/parent/recommender/admissions)
    drive different communication + approval rules.
16. **Group projects** — responsibilities, shared timeline, ownership, check-ins, dependencies,
    meeting notes. Not surveillance; student controls what's shared.
17. **Clubs & extracurriculars** — meetings/competitions/forms/dues(info only)/roles/volunteer
    hours/certificates → feed the activity résumé.
18. **Attendance & absence recovery** — after a miss: affected classes, gather posted work, draft
    "what did I miss" messages, reschedule study, track make-up deadlines. High-value mission.
19. **Forms & school admin** — extract requirements, pre-fill non-sensitive fields for review,
    flag missing signatures, track slips/registrations, receipt after submit. NO auto-signatures,
    legal attestations, or sensitive health disclosures.
20. **Portfolio & résumé** — approved record of activities/leadership/awards/projects/skills;
    résumé variants; brag sheet; match experiences to prompts. Never fabricate achievements.
21. **School search & course planning (later)** — electives/prereqs/schedules/grad requirements
    (show source, advise verifying with the school)/college shortlist.
22. **Wellness logistics (not therapy)** — protect sleep, detect impossible loads, encourage
    breaks, reduce noise, let the student pause everything, help draft an ask-for-help message.
23. **Parent/guardian mode (student-controlled)** — share selected deadlines/events; request
    signatures; weekly summary. Private by default; student sees exactly what's shared. Not surveillance.
24. **Search across student life** — "when's the physics project due?", "where's my debate cert?",
    "did I submit the summer app?" — answer from evidence, link each answer to its source.
25. **Verified receipts** — every completed action has proof (event ID, submission confirmation,
    sent-message record, source, version, timestamp, undo). Never say "done" for a mere draft.

---

## Build order

**Phase 1 — Student intake:** opportunity-email engine · screenshot/PDF deadline extraction ·
unified task list · calendar creation · daily briefing · Dynamic Island mission tracking.

**Phase 2 — School operations:** school-email action extraction · Google Classroom **or** Canvas
read integration · assignment-change detection · Decision Queue · Share Extension · verified
calendar + email actions.

**Phase 3 — Opportunity & application system:** opportunity matching · application missions ·
document checklist · recommendation tracking · résumé/activity record · submission receipts.

**Phase 4 — Compounding moat:** personal protocols · trust graph · mission replay · reliability
dataset · absence-recovery missions · cross-service student search.

---

## Do NOT build (yet / ever)

Generic AI tutor for every subject · automatic graded-work completion · automatic application
submission · fully autonomous teacher emails · parent surveillance · a social network · a
school-wide LMS replacement · hundreds of integrations · browser automation across arbitrary
sites · an AI "friend" personality layer.

**Hard integrity line (also our legal/reputational protection):** Bruce never completes graded
work dishonestly, never submits AI-generated work as the student's own, never fabricates
achievements, never impersonates the student without review. It explains, plans, prepares,
tracks, and verifies — the student owns the learning and the final work.

---

## Reality checks (engineering, honest)

- **Scope is enormous.** The phasing + do-not-build list are the discipline. Execute **Phase 1
  only** and prove the core loop with real students before touching Phase 2+ breadth.
- **Phase 2 integrations have walls.** Google Classroom / Canvas / Microsoft Graph education
  APIs need OAuth verification and often **school-admin permission** — same class of gate as
  Gmail. Defer; start from forwarded/shared content (zero-permission), exactly as the doc says.
- **iOS/native is gated on an Apple Developer account** the founder (a minor) can't get solo.

---

## Status vs. this roadmap (2026-07-13)

- **System 1 (Opportunity engine):** partially built for the *research/professor* slice —
  grounded discovery (OpenAlex), fit-gating, grounded+verified drafting, two-tier email
  resolution. Generalizing to scholarships/programs/etc. is Phase 1/3 work.
- **System 5 (deadline/document extractor), 2 (assignments), 3 (inbox), 4 (calendar), 11
  (daily brief), 12 (Dynamic Island):** not started — this is the rest of Phase 1.
- Backend engine works from scripts; next up: harden it (task B), then a FastAPI service
  (task A) so a client can call it. Native client blocked on the Apple account.
