# Self-Hosted iMessage Alpha

Bruce's first messaging transport: a student texts Bruce a photo / flyer / PDF / link over
iMessage, and it becomes the **same durable mission** as the in-app HandoffSheet — immediate ack,
processing, grounded results, verified receipt. No paid provider (no Linq / Sendblue / Twilio /
Apple Messages for Business). The transport is a small **relay** that runs on a **dedicated Mac**
signed into a Bruce-owned Apple ID, driving Messages through the audited open-source
[`openclaw/imsg`](https://github.com/openclaw/imsg) CLI.

> **Status: LIVE iMESSAGE IS UNVERIFIED.** Everything here is built and tested against the audited
> `imsg` JSON-RPC contract and a fake `imsg` process. It is **not** confirmed against real Messages
> until the dedicated-Mac dry-run in [§7](#7-dedicated-mac-dry-run-the-verification-gate) passes.
> Do not describe iMessage as working live before then.

---

## 1. Architecture

```
   Student's iPhone                 Dedicated Mac (relay)                 Bruce cloud (GCP)
  ┌────────────────┐   iMessage    ┌──────────────────────┐   HTTPS     ┌─────────────────────┐
  │  Messages app  │ ◄──────────► │  Messages.app         │  (relay      │  bruce-api          │
  │  texts Bruce   │              │    ▲   watch / send    │   pulls,     │   /v1/relay/*       │
  └────────────────┘              │    │ (imsg JSON-RPC)   │   cloud      │   handle_inbound →  │
                                  │  ┌─┴──────────────┐    │   never      │   durable mission   │
                                  │  │  relay process │────┼─────────────►│   (intake queue)    │
                                  │  │  (transport    │    │   TLS +      │                     │
                                  │  │   only)        │◄───┼──────────────│  outbound queue     │
                                  │  └────────────────┘    │   claim/ack  │  (Cloud SQL + RLS)  │
                                  │  Keychain: device secret│             └─────────────────────┘
                                  └──────────────────────┘
```

**One rule governs the whole design: the cloud never initiates a connection to the Mac.** The relay
authenticates outbound to `bruce-api`, POSTs inbound events, and **pulls** outbound work to send. The
Mac exposes no inbound port and no service the cloud can reach.

### What runs where

| Concern                       | Mac relay | Bruce cloud |
|-------------------------------|:---------:|:-----------:|
| Watch Messages / send replies |     ✅     |      —      |
| Bruce mission logic / policy  |     —     |      ✅     |
| Model calls (OpenAI)          |     —     |      ✅     |
| Durable product state / DB    |     —     |      ✅     |
| OpenAI / DB / cloud SA keys   |     —     |      ✅     |
| Rotating device credential    |  ✅ (Keychain) | ✅ (hash only) |

---

## 2. Security boundaries (non-negotiable)

These are enforced in code and must stay true:

- **Transport only.** The relay (`engine/relay/`) contains no Bruce decision logic, no model calls,
  no mission policy, and no durable product state. It normalizes events and moves bytes.
- **No cloud secrets on the Mac.** The relay holds exactly one secret — a rotating device credential
  in the macOS Keychain. No OpenAI key, no DB credential, no cloud service-account key. The server
  stores only a SHA-256 hash of the credential (`relay_devices`, worker-only RLS).
- **SIP stays enabled.** `imsg` is driven over its SIP-safe surface only — `watch` (inbound),
  `send` (outbound), `message.send_status`. No `read`/typing/edit/unsend/private-bridge; no IMCore
  injection; SIP is never disabled.
- **Cloud never dials the Mac.** The relay is the only party that opens a connection; it always
  initiates outbound TLS to `bruce-api`. Certificate verification is mandatory.
- **Content-free logs.** Logs carry message ids and statuses only — never message text, sender
  handles, attachment paths, extracted content, emails, tokens, Apple data, URLs, or provider bodies.
- **Provisioning is an operator action.** There is deliberately **no** HTTP endpoint to register a
  relay. A device is created with `scripts/register_relay_device.py`, which prints the secret once.

---

## 3. Server endpoint contract (`/v1/relay/*`)

All relay endpoints require `Authorization: Bearer <device-secret>` over TLS plus an
`X-Bruce-Timestamp` (replay window); `X-Bruce-Nonce` / `X-Bruce-Request-Id` are carried for tracing.
A revoked or expired credential returns **401** → the relay stops and alerts.

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/relay/inbound` | POST | Ingest a normalized imsg event → same `handle_inbound` durable-mission flow. Ignores echoes (`is_from_me`); deduped by message GUID; resolves `upload_ref` → staged bytes → consumes them once the source is durable. |
| `/v1/relay/upload` | POST | Stage an inbound attachment's bytes (MIME allowlist, 15 MB cap, executable reject, dedup by content hash). Returns `upload_ref`. |
| `/v1/relay/outbound/claim` | POST | Claim the next queued outbound message (`204` when idle). Lease-guarded — two pollers never claim the same row. |
| `/v1/relay/outbound/{id}/ack` | POST | Report send result: `sent` → done; `terminal_failed` → no retry; anything else → server decides retry vs terminal by attempt count. |
| `/v1/relay/heartbeat` | POST | Device health; authenticating already stamped `last_seen_at`. |

**Idempotency & durability.** Inbound is deduped by imsg message GUID on the server *and* by a durable
local checkpoint on the Mac (a GUID is checkpointed only after the server acks). Outbound uses a
lease/claim queue so a relay crash mid-send re-leases the row after the lease expires; the relay acks
`sent` **only after** the `imsg send` command succeeds — so a mission is never falsely marked
delivered.

---

## 4. Relay components (`engine/relay/`)

| File | Responsibility |
|---|---|
| `imsg.py` | Thin wrapper over the audited `imsg` JSON-RPC 2.0 contract (`watch`/`send` only). `SubprocessImsg` drives one `imsg rpc` process; a `Protocol` lets tests inject a fake. |
| `backend.py` | The relay's **only** outbound connection. Mandatory TLS, Bearer + per-request timestamp/nonce/request-id, explicit 20 s timeout. `BackendError` → retry; `AuthError` (401) → stop. |
| `checkpoint.py` | Durable local cursor (bounded ring of recent GUIDs, atomic `os.replace`). A GUID is marked processed only after the backend acks. |
| `relay.py` | Supervised inbound-watch loop + outbound-poll loop. Skips echoes/duplicates, stages+uploads attachments, checkpoints after ack, reconnects on watch drop, acks outbound only after send succeeds. |
| `config.py` | Env + Keychain wiring. Refuses to start without an https base URL and a device secret. |
| `__main__.py` | Entrypoint (`python -m relay`); content-free logging. |
| `fake_imsg.py` | In-process `Imsg` double for tests + a standalone JSON-RPC subprocess for the dedicated-Mac dry-run. |

### Attachment handling (`relay.py::_stage_attachments`)

1. **Missing / still downloading** (`missing: true`) → defer the **whole message**; retry later (not
   checkpointed) so no message is posted with a half-arrived file.
2. MIME allowlist (`image/png|jpeg|heic|heif|webp`, `application/pdf`, `text/plain`); size cap 15 MB;
   client-side executable reject (ELF/PE/Mach-O/shebang/zip magic) — belt-and-suspenders with the
   server's `/v1/relay/upload` validation.
3. Copy to a private spool → upload → **delete the spool copy** (on success or failure). The forwarded
   event carries only `{kind, media_type, upload_ref}` — never the local path.

---

## 5. Setup on the dedicated Mac

Prerequisites: a dedicated Mac signed into a **Bruce-owned** Apple ID with iMessage enabled (do not
automate Apple ID creation), and the `imsg` CLI installed and granted Full Disk Access / Automation
permission for Messages.

```bash
# 1) Register a relay device (operator machine with DB access — prints the secret ONCE)
BRUCE_APP_DATABASE_URL=... python -m scripts.register_relay_device "mac-alpha" --handle "+15550000000"

# 2) On the Mac, store the secret in the login Keychain (server keeps only its hash)
security add-generic-password -s com.bruce.relay.device-secret -a default -w '<secret>'

# 3) Point the relay at staging and run it
export BRUCE_API_BASE_URL="https://<bruce-api-staging-url>"
export BRUCE_IMSG_BIN="imsg"          # or an absolute path
python -m relay
```

Config (env): `BRUCE_API_BASE_URL` (required, https), `BRUCE_RELAY_STATE_DIR` (default
`~/.bruce-relay`), `BRUCE_RELAY_ACCOUNT` (Keychain account, default `default`),
`BRUCE_RELAY_POLL_INTERVAL`, `BRUCE_RELAY_RECONNECT_DELAY`. `BRUCE_RELAY_SECRET` is a **dev-only**
fallback when the Keychain is unavailable.

**Credential rotation.** Re-run `register_relay_device` (or a rotate path), update the Keychain item,
restart the relay; revoke the old device server-side. The server rejects the old secret with 401 and
the previous relay stops.

---

## 6. Test coverage

- **`engine/tests/test_relay_component.py`** (19 tests, offline) — the relay's transport behavior with
  an in-process fake `imsg` + fake backend: direct/group text, URL, screenshot, PDF,
  delayed/oversized/unsupported/executable attachments, duplicate message, outbound echo, backend +
  upload outage (not checkpointed → retried), relay restart (checkpoint persists), watch reconnect,
  revoked credential (stops), outbound ack-after-send, send retry, terminal failure.
- **Server-side** (`test_relay_io.py`, `test_relay_upload.py`, `test_messaging_*.py`, real Postgres) —
  outbound lease expiry, duplicate claim, replay rejection, cross-user isolation, account deletion,
  and **no false completion** are enforced by RLS + the durable queues and tested there.

These fakes stand in for real Messages and the real API. Green tests prove the contract; they do
**not** prove live iMessage.

---

## 6.5 Account linking (private-alpha bridge — no app yet)

The native app and Sign in with Apple aren't built, and there's no Apple Developer account yet, so
there is **no in-app way to get a link code**. Until then, linking runs entirely through iMessage +
an operator CLI. This is a **temporary bridge** and will be replaced by the native onboarding flow.

**Flow**
1. An **unlinked** handle texts Bruce → generic private-alpha prompt (no app/profile mentioned):
   *"This is Bruce (private alpha). To connect this number, reply with the 6-character invite code the
   Bruce team gave you. Codes expire quickly and are single-use."*
2. The **operator** mints a one-time code for a user (out of band, DB access via the Cloud SQL proxy):
   ```
   BRUCE_APP_DATABASE_URL=... python -m scripts.create_link_code --label dhruv-alpha
   # or an existing id:  --user <uuid>
   ```
   `--label` derives a **stable, reproducible** user_id (uuid5) and creates the user row if needed.
   The plaintext code is printed **once**.
3. The user texts that code to Bruce **from the target number** → the handle binds to that user.
4. Any wrong/expired code → one **generic** reply (never reveals whether an account exists).

**Safety properties** (enforced in `messaging_store` + migration 0010):
- Codes are **short-lived** (10 min), **single-use** (`consumed_at`), and **stored as an HMAC-SHA256
  digest** keyed by a server-side **pepper** (`BRUCE_LINK_CODE_PEPPER`, held only in Secret Manager —
  never in the DB). A 6-char code is too low-entropy for a plain hash to resist offline brute-force if
  the DB leaks; the pepper makes the stored digest uninvertible unless the secret is *also* stolen.
  Redemption re-verifies with a constant-time compare.
- **Per-code** attempt cap (`MAX_REDEEM_ATTEMPTS`) and a **per-handle brute-force lockout**
  (`messaging_link_attempts`, worker-only RLS): 5 failed attempts in 15 min → the handle is locked
  out for 15 min; a successful link clears the counter.
- **No silent rebind**: a handle already bound to user A is never rebound by user B's code
  (`conflict`) — prevents one number from hijacking another user.
- **No account enumeration**: `invalid` / `expired` / `locked` / `conflict` all return the *same*
  generic text; `rate_limited` returns a generic wait message.
- Provisioning is an **operator action** — there is no public endpoint to mint a code without the app.

**Not yet done:** migration `0010_link_attempts` and this code must be deployed to staging before the
next live test (the current staging revision predates it).

---

## 7. Dedicated-Mac dry-run (the verification gate)

Live iMessage is **UNVERIFIED** until every scenario below is confirmed end-to-end on the dedicated
Mac against real Messages. Run first with `fake_imsg` as a subprocess
(`BRUCE_IMSG_BIN="python -m relay.fake_imsg"`, scripted events via `BRUCE_FAKE_IMSG_EVENTS`) to smoke
the wiring, then with the real `imsg`:

- [ ] Inbound **direct text** → durable mission + immediate ack reply.
- [ ] Inbound **group text** → mission created; reply goes to the **chat**, not the individual.
- [ ] Inbound **screenshot** and **PDF** → attachment staged, uploaded, mission has the bytes.
- [ ] Inbound **URL** → mission created from the link.
- [ ] **Duplicate** message (same GUID) → processed exactly once.
- [ ] **Outbound echo** (Bruce's own message) → ignored, no loop.
- [ ] **Delayed attachment** (still downloading) → deferred, then delivered once it lands.
- [ ] **Backend outage** → nothing lost, retried, no double-send.
- [ ] **Relay restart** and **Messages restart** → resumes; acked messages not reprocessed.
- [ ] **Expired / revoked credential** → relay stops, no forwarding.
- [ ] **Replayed request** → rejected server-side.
- [ ] **Outbound lease expiry / duplicate claim** → single delivery.
- [ ] **Send retry** and **terminal failure** → correct final state; no false "delivered".
- [ ] **Account deletion** and **cross-user isolation** → enforced.

Only after all of the above pass on the dedicated Mac may live iMessage be described as working.
