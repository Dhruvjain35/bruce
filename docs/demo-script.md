# Demo script — under 3 minutes

**Rule for this script: never say a component is real if it is mocked or blocked.** A judge who
curls one endpoint finds an inflated claim in seconds, and that costs more than the claim was worth.
Check `docs/hackathon-readiness.md` before recording — if a row there is not green, the line below
that depends on it must change or be cut.

Two versions are written for each blocked beat: **[LIVE]** if Qwen/Calendar are working by record
time, **[BLOCKED]** if not. Pick per beat, honestly.

---

## 0:00–0:20 — The problem and the promise

> "Every deadline in a student's life arrives as a flyer, a screenshot, or an email. Registration
> closes Feb 28. A permission form. A $25 fee. You read it once, you mean to deal with it, and it's
> gone.
>
> Bruce isn't a chatbot and it doesn't give advice. You hand it the flyer. It does the work — and
> then it proves it did."

*On screen: the real flyer. No UI yet.*

---

## 0:20–1:20 — Hand it over, Qwen reads the pixels

**[LIVE]**
> "This is a photo. No text layer, nothing to copy-paste. It goes to Qwen 3.7 Plus on Alibaba
> Cloud, which reads the image itself."
>
> "Two deadlines. A $25 fee. A parent permission form. And notice what it did *not* do: 'judging
> begins the following Friday' — that's ambiguous, so Bruce left it unresolved instead of inventing
> a date."

*Show: the flyer → the transcript → the grounded extraction. Point at `source_span`.*

> "Every date carries the exact words it came from. If Bruce can't point at the text, it drops the
> date. That's the difference between reading a document and guessing about one."

**[BLOCKED]** — if Qwen is still account-blocked:
> "Bruce's intake runs on Qwen 3.7 Plus for multimodal understanding. The adapter is built and its
> wire format is tested against the real client — but our Qwen Cloud account is under a risk-control
> hold, so I have zero successful inference calls and I'm not going to pretend otherwise. Here's the
> request Bruce sends, and here's the 403 we get back."

*Show: `make qwen-smoke` output — the real 403. Then show the grounded extraction running on the
text path so the pipeline is still demonstrated.*

> "Everything downstream of that call is real, and I'll show you."

---

## 1:20–2:05 — One decision, and only one

> "Bruce did the mechanical work. Now it stops — because this is the part that needs a human."

*Show: the Decision. Exact recipient/date/calendar. Not a summary — the actual thing.*

> "This isn't 'do you want me to add some events?'. It's the exact event, on the exact calendar,
> at the exact time. Approve or edit. Bruce doesn't guess on your behalf, and it doesn't ask you
> about things it can figure out itself."

*Tap Approve once.*

---

## 2:05–2:30 — The part everyone else skips

**[LIVE]**
> "Bruce created the event — and then it read it back out of Google Calendar and compared it, field
> by field, to what you approved. Only then does it say verified."

*Show: the receipt — event id, the read-back fields, the source it came from.*

> "Watch this." *Tap Approve again / re-run.* "Same event. Not two. Bruce hands Google its own event
> id, so Google itself rejects the duplicate. A double-tap can't double-book you."

**[BLOCKED]** — if Google OAuth isn't connected:
> "Execution and verification are built and tested — execute-once, read-back, verified undo — but I
> don't have Google OAuth credentials connected yet, so I'm showing it against the test harness, not
> a real calendar. I'm not going to show you a receipt for an event that doesn't exist."

*Show: the test run — including the negative cases where a mismatched read-back refuses to verify.*

> "That's the rule: a write is a claim. A read-back is evidence. Bruce only says 'done' when it has
> evidence."

---

## 2:30–2:50 — Where this is going

> "The app isn't the point. The point is you text Bruce a flyer, put your phone away, and the
> mission keeps running — Dynamic Island shows real state, you approve once, you get a receipt."

*Show: the architecture diagram — the messaging channel boundary.*

> "The channel layer is built so Messages is a drop-in. It isn't connected yet, so today this is the
> app. I'm telling you that rather than showing you a mock-up of a feature that doesn't exist."

---

## 2:50–3:00 — Architecture and impact

> "Qwen reads. Deterministic code decides. Postgres holds the evidence with row-level security.
> Google executes. Bruce verifies. Running on Alibaba Function Compute in Singapore."

*Show: `curl <url>/health` returning the commit SHA, next to `git log -1` with the same SHA.*

> "Hand it to Bruce. Bruce does the work and proves the result."

---

## Pre-record checklist

- [ ] `docs/hackathon-readiness.md` reviewed — every claim in the script has a green row
- [ ] Pick [LIVE] or [BLOCKED] per beat and **say the blocked ones out loud**
- [ ] No secrets on screen: no `.env`, no keys, no `~/.s`, no AccessKey in a terminal
- [ ] Use the real flyer fixture, not a contrived one
- [ ] If the demo shows a receipt, it must come from a real read-back — never a mock
- [ ] Under 3:00. Cut the architecture beat before cutting the verification beat.
