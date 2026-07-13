# Bruce outreach-engine design

_Grounded from a research pass (workflow `bruce-engine-research`, 2026-07-13). This is the
implementation bible for the engine. Faithful condensation of the synthesized design._

## What it is

A grounded, human-in-the-loop, anti-spam outreach copilot for **high-school and early-undergrad**
students. Every fact in a draft traces to a stored API payload or a scraped page with a URL.
The engine never fabricates a person, paper, finding, or email, and it never sends anything.

```
student interest
  → [1] resolve topic (OpenAlex Topics) — fall back to works keyword search
  → [2] candidate discovery (recent works → authorships)   ← ACTIVE authors, not superstars
  → [3] enrich + FIT-GATE (stats, on-topic recent papers, abstracts, ORCID confirm)
  → [4] email resolve OUT OF BAND (faculty page → PDF → lab site)  + provenance
  → [5] Evidence Ledger (typed facts, each with a source URL)
  → [6] draft grounded skeleton → student fills the personalization slot → verify entailment
  → [7] anti-spam gates (volume cap, name-swap test, authenticity score)
  → student sends from their OWN inbox  (engine has NO send authority)
```

## 1. Data sources (not interchangeable)

| Source | Role | Notes |
|---|---|---|
| **OpenAlex** | Primary backbone: topics, candidate discovery, recent works, author stats, affiliation | 2026: free API key expected (credit-metered; **search calls are most expensive**). Abstracts are an inverted index (reconstruct). No emails. |
| **Semantic Scholar** | Enrich: plaintext abstracts, TLDRs, author `homepage` (email lead) | ~1 rps w/ key; anon pool heavily throttled. |
| **arXiv** | Newest preprints (CS/physics/math), full abstracts, PDF (corresp. email inside) | 1 conn, ≥3s between calls. Preprints only. |
| **Crossref** | Authoritative DOI/venue/date; ORCID/ROR pivot | 50 rps. Sparse abstracts. |
| **ORCID** | Confirm identity + current affiliation only | Needs client-credentials token. Emails ~never public. |

**Discovery query flow:** resolve interest → OpenAlex Topic id (fall back to `works?search=` for
multi-word phrases that don't match a Topic); pull recent *works* on the topic and aggregate
`authorships[]` into per-author seeds (captures active authors + early-career co-authors);
per candidate pull `/authors/{id}` (stats, `last_known_institutions`, orcid) and their recent
**on-topic** works; reconstruct/ backfill abstracts.

## 2. Ranking + FIT-GATING (a wedge differentiator — not yet implemented)

Do not sort by citation impact. Compute and gate:
```
relevance  = topic overlap (student ↔ author)
recency    = decay(months since most recent on-topic paper)
depth      = min(on-topic recent papers / 3, 1)
mentoring  = fraction of recent papers with early-career co-authors + lab-site-exists
seniority_fit: titan (h-index ≫ field P95) → PENALTY; inactive (no work 3yr) → EXCLUDE;
               assistant/associate → BONUS
fit_score = 0.40*relevance + 0.20*recency + 0.15*depth + 0.15*mentoring + 0.10*seniority_fit
```
- `fit_score < 0.45` → label **"Not a strong fit — we recommend NOT emailing"** with the reason.
  ("Tell them not to send" is a feature; every competitor pushes send.)
- For HS students, surface the **early-career first authors** (PhD/postdoc) on the PI's relevant
  papers as better-bandwidth targets — a segment no competitor addresses.

## 3. Email discovery (no scholarly API gives emails — out of band, never guessed)

Hard rule: **never construct `first.last@univ.edu`.** Only emit an address literally scraped
from a page/PDF, with the source URL stored; else `email_status: not_found`.

Tiered resolver (stop at first validated hit, record tier + URL):
1. Official university faculty page (canonical). Get institution domain from OpenAlex ROR →
   ROR API `domains`; site-restricted search `"<name>" <dept> site:<domain>`; extract + de-obfuscate.
2. Corresponding-author email inside the paper PDF (OpenAlex `oa_url`/arXiv PDF).
3. Lab / personal homepage (S2 `homepage`).
4. ORCID `/email` (rare).
Validate: email domain ∈ ROR domains AND professor name on page. Realistic hit rate ~60–80%
for tenured US faculty, lower for early-career/non-US. Respect robots.txt; cache per author.

## 4. Drafting: strict grounding + verification

- Drafter is handed **only** the frozen `Evidence[]` for one candidate + the `StudentProfile`.
  No web, no memory. Exact entities (name, title, DOI, email) are **template-injected from
  Evidence**, not written by the model → structurally impossible to hallucinate.
- **Two stages:** (1) LLM produces a grounded skeleton + 2–3 hooks, each bound to a
  `paper_finding` evidence id, as structured JSON (each sentence lists its evidence ids);
  (2) **required human slot** — the student writes the genuine specific question AI can't fake;
  the tool coaches but won't write it and won't mark the draft "ready" until it's filled.
- **Verification pass (separate model call, fails closed):** citation completeness (every
  factual sentence has ≥1 evidence id) → entailment check (SUPPORTED/NOT_SUPPORTED/OVERSTATED,
  block anything unsupported) → deterministic entity guard (every proper noun/DOI/year/email in
  the rendered email must appear verbatim in Evidence) → template integrity (name spelled
  exactly as in `author_identity`).
- **Voice:** capture a short student writing sample → style guidance + few-shot anchor; final
  `humanizer` lint (kill rule-of-three, em-dash overuse, "delve/leverage", negative parallelisms).
- Constraints: 150–200 words, ~3 short paragraphs, one small ask (~15-min chat), correct
  greeting, no same-day meeting, no flattery, no "stepping stone" framing.

## 5. Anti-spam / human-in-the-loop (nothing sends automatically)

- **No send capability at all.** Terminal action = render finalized email + resolved address +
  provenance, then open the student's own Gmail/Outlook compose prefilled (or copy-paste). Never CC/BCC.
- **Volume gates (server-side against an outbox log):** ≤2 professors per department; small
  batches (~≤5 ready drafts / 7 days) then a 1–2 week cooldown; fit-gate default is "don't send".
- **Name-swap test:** embed + shingle similarity of the new body vs prior bodies (strip
  templates); >0.85 non-template similarity → block, force re-personalize. Personalization must
  cite a `paper_finding` unique to this candidate.
- **Authenticity score** must clear before "Send" enables: verification passed + student slot
  filled (not a paraphrase of the abstract) + humanizer lint + length + one small ask + correct name.
- **Follow-up = reminders only** (student writes/sends; cap 2, then move on).

## Stack decision

Engine is **Python** (FastAPI when it needs an API for the iOS client), using `pydantic` +
`pydantic-ai` (provider-neutral, validated structured outputs — the anti-hallucination gate) —
consistent with the founder's spec and the existing tested models. (The research synthesis
suggested TS/Next on Vercel; rejected for the engine because the product is iOS-native — the
backend language is invisible to the app — and Python is stronger for the PDF/scientific/data
work here. Revisit only if a web surface becomes a real requirement.)

## Implementation status

- **Done + tested:** data models + grounding contract; OpenAlex discovery (topic + works-search
  fallback, recent on-topic papers, dedup, junk-filter); pipeline spine.
- **Known defects:** affiliations noisy (`last_known_institutions` unreliable — needs §3 ROR +
  faculty-page verification); `fit_score` not real yet (uniform; §2 formula not implemented;
  titans not down-ranked).
- **Not started:** email resolver (§3), fit-gating (§2), drafting + verification (§4, needs LLM
  key), anti-spam (§5), the iOS client.
