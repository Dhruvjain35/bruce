# Screenshot / evidence checklist

Capture these for the Devpost submission. Each row says what makes the shot *proof* rather than
decoration — a screenshot of a UI proves nothing; a screenshot tying a live service to a public
commit proves something.

**Before every capture: no `.env`, no API keys, no RAM AccessKey, no `~/.s`, no refresh tokens, no
real student data.** Terminal scrollback leaks secrets more often than the command you meant to show.

| # | Shot | What makes it proof | Status |
|---|---|---|---|
| 1 | **Qwen live request + response** | `make qwen-smoke` output: host, model, HTTP 200, provider request id. Redact nothing else — there is no key in that output by design. | **BLOCKED** — currently returns 403 |
| 2 | **Qwen reading the flyer** | The flyer image beside the extracted JSON, with `source_span` visible. The span is the proof it read rather than guessed. | BLOCKED (needs #1) |
| 3 | **Function Compute console** | `bruce-engine` in **Singapore**, showing the trigger URL and the runtime (`custom.debian12`). | BLOCKED — activation pending |
| 4 | **Live health endpoint** | `curl <url>/health` **next to** `git log -1`, showing the SAME commit SHA. This is the shot that ties the running service to the public repo — the single most valuable one. | BLOCKED (needs #3) |
| 5 | **Auth enforced in production** | `curl -X POST <url>/v1/intake` returning **401**. Proves student data isn't publicly readable. | BLOCKED (needs #3) |
| 6 | **Deployment metadata** | `docs/deployment-proof.json` — URL, region, function, commit, package sha256. Non-secret by construction. | BLOCKED (needs #3) |
| 7 | **Google Calendar created event** | The real event in Google Calendar UI, with the `bruce:mission:<id>` marker visible in the description. | BLOCKED — no OAuth credentials |
| 8 | **Read-back receipt** | The receipt showing `event_id` + the read-back fields that were compared. **Never** stage this — if it isn't a real read-back, don't shoot it. | BLOCKED (needs #7) |
| 9 | **Execute-once** | Approve twice → one event. Best shown as the Google Calendar list with a single entry after two taps. | BLOCKED (needs #7) |
| 10 | **Source grounding** | Tapping a deadline → the exact span it came from in the source. | partially available (mock UI) |
| 11 | **Dynamic Island** | — | **NOT IMPLEMENTED — do not stage a mock-up** |
| 12 | **Architecture diagram** | `docs/architecture.md` rendered. Keep the dashed "not live-verified" edges honest. | READY |
| 13 | **Tests** | `make test` → `342 passed, 12 skipped`. Show the skip reasons — they're evidence of honesty, not weakness. | READY |
| 14 | **Repository license** | `LICENSE` (MIT) at repo root, repo public. | READY |

## Rules

1. **Never stage a receipt.** A fabricated verification screenshot in a product whose entire claim is
   "proves the result" is the one thing that ends the submission if a judge probes.
2. **Never screenshot a mock as if it were live.** If the UI shows mock data, either say so on camera
   or don't show it.
3. **If a row is blocked, submit it blocked.** "Built, blocked on account access, here's the 403" is
   a credible engineering story. A faked green row is not.
4. Cross-check every claim against `docs/hackathon-readiness.md` before uploading.
