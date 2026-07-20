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

## Measured in-flight windows (A2)

Measured by `test_inflight_window_measurement` with in-process fakes, so these are the pure **code-path**
costs; in production each directive check adds one authenticated heartbeat round-trip (network-dominated).

| Segment | Fake-measured (code path) | Production |
| --- | --- | --- |
| claim → first directive check | ~0.2 ms | + one heartbeat RTT |
| attachment-preparation duration | as injected (0 for text) | download/spool time, ≤ prep budget |
| final directive check → imsg invocation (**irreducible**) | **~0.3 ms** | ~unchanged (local: one ledger append + the call) |
| imsg invocation duration | ~µs (fake) | the real Messages hand-off |
| total maximum *preventable* window | claim → final check | dominated by prep + 2–3 directive RTTs |
| **irreducible handoff window** | **~0.3 ms** | ~0.3 ms local + the imsg hand-off itself |

**We do not claim zero risk after the imsg invocation.** Once `imsg.send_text` is entered the bytes are
committed to Messages/Apple and cannot be recalled. The final directive check is the last gate; the code
between it and the send is only the at-most-once ledger append (required — it must precede the send) plus
the call itself. `test_adversarial_pause_in_the_irreducible_window_still_sends` injects a pause into
exactly this window (via a barrier that runs only there) and shows the send still completes — the
irreducible local race — while `test_1_claimed_then_global_pause_before_send` (pause *before* the final
check) shows no send. Together they pin the exact boundary.

## A2 fail-closed send algorithm (IMPLEMENTED — `relay.Relay.process_one_outbound`)

The relay client implements `process_one_outbound` as:

```
1. job = claim()                      # A1 server gate already blocks NEW claims while paused
   on AuthError -> stop the relay (revoked); on BackendError -> back off, return
   if job is None: honor Retry-After from the paused 204, back off, return
2. recover from the durable delivery PHASE of job.id (see below) — never a blind resend:
      handed_to_imsg / server_acknowledged -> re-ack "sent" (at-most-once invocation), return
      send_intent_recorded / handoff_outcome_unknown -> ambiguous: ack "terminal_failed:handoff_unknown"
          (surface for reconciliation), return
      none / blocked_before_send / send_failed_before_handoff -> safe to send, fall through
3. gate = _send_gate()                # authenticated directive check (heartbeat -> directive)
      SEND only if the authenticated directive is exactly `run`
      STOP on a `stop` directive OR a rejected/revoked credential
      HOLD on pause_outbound, an UNKNOWN directive, OR any failure to get a clean
           authenticated answer (network / TLS / timeout / malformed / invalid env)
      -- a network/backend error is NEVER permission to send --
   if gate != SEND: release (step 6)
4. payload = prepare()                # text today; attachment download/spool is future (slow)
   gate = _send_gate()                # RE-CHECK after slow preparation
   if gate != SEND: release (step 6)
5. gate = _send_gate()                # FINAL check immediately before the send
   if gate != SEND: release (step 6)
   record(job.id, SEND_INTENT_RECORDED)   # DURABLE before the call (a crash from here is ambiguous)
   guid = imsg.send_text(...)             # <-- irreducible: bytes handed off, unrecallable
     on ImsgSendRejected (definite pre-handoff): record SEND_FAILED_BEFORE_HANDOFF; ack retryable; return
     on any other exception / no guid (ambiguous): record HANDOFF_OUTCOME_UNKNOWN;
        ack "terminal_failed:handoff_unknown" (surface, never resend, never false-sent); return
   record(job.id, HANDED_TO_IMSG, guid)   # DURABLE before the ack (lost ack recovers as sent)
   if ack(job.id, "sent", guid) landed: record(job.id, SERVER_ACKNOWLEDGED)
6. blocked / not sent:
   - record BLOCKED_BEFORE_SEND (no handed_to_imsg state) — safely retryable, never marked sent
   - release the lease: ack "retryable_failed" (server re-queues with a backoff)
   - on STOP: also stop claiming and park the relay (reap the imsg child)
   - bounded backoff (Retry-After) so a paused relay never hot-loops
```

## Durable delivery phases (A2 hardening — `relay/outbound_ledger.py`)

A single pre-send boolean "marked" state is unsafe: mark → crash before imsg → reclaim treats it as
attempted → the message is **silently lost**; or an ambiguous send exception is retried → **double-text**.
The relay instead records the explicit **delivery phase** of each outbound id and derives restart
recovery from it:

| Phase | Meaning | Restart / reclaim action |
| --- | --- | --- |
| *(none)* / `blocked_before_send` | never reached the send; gate blocked | **safe to (re)send** |
| `send_intent_recorded` | persisted immediately before the imsg call | **ambiguous** → surface `terminal_failed:handoff_unknown`, never resend |
| `send_failed_before_handoff` | imsg **definitely** declined before accepting bytes (`ImsgSendRejected`) | **safe to retry** (not suppressed forever) |
| `handoff_outcome_unknown` | a transport crash straddled the handoff | **ambiguous** → surface, never resend, never false-sent |
| `handed_to_imsg` | imsg returned a guid; persisted **before** the server ack | re-ack **sent**, never re-invoke imsg |
| `server_acknowledged` | server-row transition confirmed | terminal; re-ack sent if reclaimed |

Accurate terminology (do **not** overstate): **at-most-once imsg invocation**; **exactly-once server-row
state transition** where enforceable; an **ambiguous delivery state** whenever a crash occurs across the
external handoff boundary. This is **not** end-to-end exactly-once delivery — iMessage provides no
transactional handoff-plus-acknowledgement, so a crash in the ~0.3 ms between `send_intent_recorded` and a
durable outcome is surfaced for bounded reconciliation, not blindly resent. The legacy A2 boolean ledger
(`{"processed": [...]}`) is migrated to `handed_to_imsg` so an existing relay never suddenly resends.

Proven by `tests/test_relay_delivery_phases.py` (crash-after-intent, imsg reject, subprocess exit, crash
during invocation, ack-fail-after-send, per-phase restart recovery, no-blind-resend, safe-retry,
no-false-sent, pause-before-gate, irreducible-window classification, legacy migration).

Invariants A2 preserves:

- Directive re-checked **after claim**, **after slow preparation**, and **immediately before every** send.
- **Fail closed** on any directive/heartbeat lookup that cannot be authenticated; a network error is
  never permission to send.
- Never report a blocked or ambiguous message as confirmed **sent**; at-most-once imsg invocation.
- Restart behavior is **derived from the durable phase**: before-handoff → retry; handed/unknown → never
  blind-retry; acknowledged → terminal.
- Release/expire the lease so a definitely-unsent message recovers exactly once; back off (`Retry-After`)
  while paused.

## Is it an "emergency outbound kill switch" now? (A1 + A2)

With **A1** (authoritative server-side claim gate + audited operator control) **and A2** (fail-closed
client-side send enforcement — directive re-checked after claim, after preparation, and immediately
before every imsg send; fail-closed on any auth/network/malformed/unknown/invalid-env condition; blocked
messages released and recovered exactly once; paused relays back off; `stop` parks and reaps the child),
the combined system can honestly be called an **emergency outbound kill switch** — with one stated
caveat: it cannot recall bytes already handed to iMessage. The irreducible window is ~0.3 ms of local
code (one ledger append) plus the physical Messages hand-off; everything before that is prevented,
fail-closed. Full end-to-end liveness (keeping the relay running so a `run` fleet actually drains) is
**A3** (the supervisor), which is out of scope here.
