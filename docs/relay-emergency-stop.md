# Relay emergency-stop semantics (Bite 1.5 A1 / A2)

This documents exactly what the outbound pause/stop does — and, importantly, what it does **not** do —
so the guarantee is never overstated.

## What A1 is (and is not)

A1 is the **authoritative, server-side gate on new outbound CLAIMS**. When the global
`relay_control.outbound_paused` switch is set for the running `BRUCE_ENV`, or a device's directive is
`pause_outbound`/`stop`, `POST /v1/relay/outbound/claim` returns `204` **before** the lease/claim SQL
runs. No new or reclaimed message is ever leased to a paused/stopped device. This is enforced in the
server, independent of any relay client, and cannot be bypassed by a compromised or buggy client.

A1 **does not** — and cannot — retract a message a device **already claimed** before the pause. A
distributed system cannot recall bytes already handed to iMessage: once the relay calls
`imsg.send_text(...)` and macOS/Apple accept the message, it is gone. Preventing the send of an
already-claimed, not-yet-sent message is **A2's** responsibility (the relay client), via a pre-send
directive re-check that fails closed.

Do not call A1 an "outbound kill switch" without this qualification. It is a **claim gate**. The
end-to-end emergency stop is A1 (claim gate) **plus** A2 (pre-send re-check).

## The in-flight window

The window during which a message can still be sent after a pause is trigged is bounded by:

```
claim returns ──▶ [relay prepares: ledger mark, attachment download/prep] ──▶ imsg.send_text ──▶ Apple accepts
      ▲                                                                              ▲
      │ pause here → A1 already blocked this claim (never handed out)                │ pause here → bytes gone, unrecallable
      └──────────────────────── in-flight window ───────────────────────────────────┘
```

- **Maximum possible window (A1 only, no A2 re-check):** the entire time between the claim returning and
  `imsg.send_text` executing. For a text-only send that is milliseconds; but with slow attachment
  download/preparation it can approach `attachment_max_wait_s` (currently 120 s). During this window the
  current relay would still send a message that was claimed just before the pause.
- **Smallest practical boundary (with A2):** re-check the authoritative directive **immediately before**
  `imsg.send_text` (and **again after** any slow attachment/download preparation). This reduces the
  window to the gap between the final directive check and the imsg hand-off — sub-second, and it **fails
  closed** (no send) whenever the directive check cannot be authenticated. The only irreducible residue
  is the physical imsg hand-off itself, which no software can retract.

## What A1 *does* guarantee for an already-claimed message (tested)

If an already-claimed message is **not** sent (relay chose not to, or crashed), it recovers **without
duplication**:

1. While paused, the claim gate never re-hands it out — even after its lease expires (a reclaim is a new
   claim, which is blocked). It sits `sending` with an expired lease, undelivered.
2. On resume, it is reclaimable **exactly once** (same row, `attempts` increments). No duplicate row, no
   double delivery.

This is proven by `test_already_claimed_message_recovers_without_duplication`.

## Required A2 fail-closed send algorithm

A2 (the relay client, a separate PR — not implemented here) MUST implement `process_one_outbound` as:

```
1. job = claim()                      # server gate already blocks NEW claims while paused
   if job is None: back off (honor Retry-After on the 204), return
2. if ledger.has(job.id):             # durable at-most-once ledger
      ack(job.id, "sent"); return     # never re-send a previously-attempted id
3. directive = authenticated_directive_check()   # e.g. heartbeat -> directive
      - fail CLOSED: if this cannot be AUTHENTICATED (auth error, bad/expired
        credential, unexpected shape) → DO NOT SEND. Do not treat a network
        error as permission to send.
      - if directive in {pause_outbound, stop} → DO NOT SEND (go to step 6)
4. prepare attachments / slow work (if any)
   re-run step 3 AFTER preparation (a pause may have arrived during the slow work)
5. ledger.mark(job.id)                # mark BEFORE the send (at-most-once)
   guid = imsg.send_text(...)         # <-- the only irreducible in-flight point
   ack(job.id, "sent", guid)
6. blocked / not sent:
   - DO NOT ack "sent" (never mark a blocked message delivered)
   - release the lease (ack "retryable_failed") OR let it expire — the server keeps
     the durable row pending; on resume it is reclaimed exactly once
   - stop accepting new outbound work immediately; idle-poll with backoff (Retry-After)
     so a paused relay never hot-loops
```

Invariants A2 must preserve:

- Check the authoritative directive **immediately before every** imsg send, and **again after** slow
  attachment/download preparation.
- **Fail closed** when the directive/heartbeat lookup cannot be authenticated; a network error is never
  permission to send.
- Never mark a blocked message as successfully sent; preserve the durable ledger and the pending row.
- Release or safely expire the lease so the message recovers without duplication on resume.
- Back off (honor `Retry-After`) while paused to avoid a hot retry/poll loop.
