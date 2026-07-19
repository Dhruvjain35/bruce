# Bruce Capability Universe — v1

Human-readable companion to the machine registry (`product/capabilities.yaml`). Source of truth for
the long-term product: `bruce_complete_capability_universe_voice_life_finance_v2.md`.

**North star (from the universe doc):** a student no longer manually checks five portals, remembers
every deadline, re-enters the same information, chases every follow-up, or wonders what they forgot.
Bruce feels like the smartest, most reliable person in the group chat — but operates like a *verified
system*, not an overconfident friend.

## Honest status legend
`implemented` (code+tests, maybe not deployed) · `partial` · `mock_only` · `deployed` (running in
staging, maybe flag-gated/not live) · `live_verified` (proven against the real external system with a
real user) · `planned` · `blocked` · `partner_required` · `unsupported`.

**Rule:** nothing is `implemented`/`deployed`/`live_verified` merely because an interface, schema,
prompt, fake adapter, or test fixture exists. Every capability needs a real outcome, evidence, honest
empty/blocked/failure states, verification, privacy-safe telemetry, and restart recovery.

## Organized by shared primitive, not by feature
The universe names **36 platform primitives**. We group capabilities under them so a primitive that
unlocks ten real outcomes beats ten disconnected tickets. Fake breadth (hundreds of mock buttons) is
explicitly forbidden.

## What is real today (Bite 1 — messaging-first conversation brain)
| Capability | Primitive | Status |
|---|---|---|
| Self-hosted iMessage relay transport | 1 Universal source intake | **deployed** (live UNVERIFIED) |
| Multimodal attachment transport | 1 | **deployed** (fake-verified) |
| Account linking (HMAC invite code) | 25 Permission/privacy | **live_verified** |
| Durable async intake | 5 Durable mission engine | **deployed** |
| Conversation + reasoning router | 29 | **deployed** (flag OFF, live UNVERIFIED) |
| Multimodal reasoner (13-field decision) | 29 | **deployed** (real gpt-5.4-mini tested on staging) |
| Tutoring path (no auto-complete of graded work) | 29 / academic boundary | **deployed** |
| Event-image → EventCandidate + honest "not connected" | 10 Evidence | **deployed** (never claims "added") |
| Model/attachment failure recovery | — | **deployed** |
| Bruce Voice OS + fact-preservation guard | 26 | **deployed** / **implemented** |
| Durable mission engine + at-most-once outbound | 5 | **deployed** (at-most-once proven live) |
| FORCE-RLS tenant isolation (incl. brain tables) | 25 | **live_verified** (two-user denial on staging DB) |

Everything else — SchoolConnector, Canvas, canonical academic graph, change detection, grades,
Gmail/Outlook, real Calendar write, approval/verification engine, notifications, visual tutoring,
opportunities/outreach (partial from the research wedge), forms/browser, travel, commerce, payments —
is `planned` / `partial` / `partner_required` / `unsupported`. See `product/capabilities.yaml` for
per-capability ID, autonomy (A0–A4), risk, primitives, dependencies, tests, and wave.

## Waves (universe §"Realistic build waves")
1 Student command center · 2 School work + communication · 3 Opportunities + outreach · 4 Learning +
visual tutoring · 5 Forms + browser execution · 6 Voice/personality/messaging polish · 7 Travel +
commerce + personal life · 8 Financial authorization · 9 Institutional + partner scale.

Bite 1 delivered the messaging + voice foundation (waves 1/6 slices). The next leverage is **Wave 1's
SchoolConnector + Canvas** so Bruce actually knows *what's due, what changed, what's missing* — see
`docs/build-execution-board.md`.
