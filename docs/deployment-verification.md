# Deployment verification

This document is the **single source of truth** for what is actually deployed and what actually
works. If a claim is not recorded here with evidence, it is not true. It is written to be read by
someone who assumes we are exaggerating.

Last verified: **2026-07-16** · branch `hackathon/qwen-cloud`

---

## Summary — what is and is not real

| Claim | Status | Evidence |
|---|---|---|
| Container image builds | **VERIFIED** | built locally, `docker build` succeeded, 480MB |
| Container serves HTTP | **VERIFIED** | ran it, `/health` 200, smoke suite green against the running container |
| Auth enforced in the container | **VERIFIED** | `/v1/intake`, `/v1/missions`, `/v1/diagnostics` all 401 without a token |
| Image contains no secrets | **VERIFIED** | no `.env` in `/app`, no secret-bearing layers in `docker history` |
| Runs as non-root | **VERIFIED** | `uid=10001(bruce)` |
| **Deployed to Alibaba Cloud** | **NO** | never run — see blockers |
| **Live service URL** | **NONE** | — |
| **One real Qwen Cloud inference call** | **NO — ZERO successful calls** | 144/144 models return 403 |
| Google Calendar live execution | **NO** | no `GOOGLE_*` credentials issued |
| RAM AccessKey valid | **VERIFIED** | `sts:GetCallerIdentity` 200 — RAM user `bruce-hackathon-deploy` |
| RAM policies attached | **VERIFIED** | `fc:ListFunctions` → `{"functions": []}` (was `ImplicitDeny`) |
| Function Compute **activated** | **NO** | `CreateFunction` → `AccessDenied: FC service is not enabled for current user` |
| FC blocked by risk control? | **NO** | no FC response has ever returned `RISK_CONTROL` or `Unpurchased` |
| FC code package builds | **VERIFIED** | 44MB zip / 60.7MB base64 / 138MB unpacked; `bootstrap` mode `0o755`; no `.env` |
| Package serves under FC's runtime contract | **VERIFIED (emulated)** | Debian 12 / py3.11 / amd64, `/health` 200, `/v1/*` 401 |
| Cold start within FC's 15s limit | **UNVERIFIED — known risk** | 26s emulated, but QEMU is ~12× (native import 2.0s) |

**Hackathon eligibility is NOT met.** It requires genuine Qwen Cloud usage and an Alibaba-deployed
backend. Neither has happened. Do not claim otherwise anywhere in the submission.

---

## Deployment record

To be filled in **only** by an actual deployment. Empty means not deployed.

| Field | Value |
|---|---|
| Service URL | _(none)_ |
| Region | _(intended: ap-southeast-1 / Singapore)_ |
| Service / function name | _(intended: `bruce-engine`)_ |
| Commit SHA | _(none deployed)_ |
| Deployed at | _(never)_ |
| Verified by | _(n/a)_ |

---

## Blocker 1 — Alibaba Cloud account is risk-suspended

The console reports, account-wide:

```
Error code:    RISK.RISK_CONTROL_REJECTION
Error message: To keep your account secure, your order is suspended.
               For more information, you can contact Customer Service.
```

### Evidence that this is account-wide, not a model/quota problem

Probed **144 models** on the workspace-scoped OpenAI-compatible endpoint
(`https://ws-5xgdxnbet67n8x8e.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`) with a freshly
created Singapore workspace key (key ID `929720`), smallest possible request (`max_tokens: 1`):

| Probe | Result |
|---|---|
| `GET /models` | **200** — key authenticates, lists 149 models |
| 131 Qwen models (incl. `qwen3.7-plus`, `qwen-turbo`) | **403 `AccessDenied.Unpurchased`** (all) |
| `qwen2-7b-instruct` | 404 `model_not_found` (deprecated id) |
| 12 non-Qwen models (`deepseek-v3.2`, `glm-5.2`, `kimi-k2.7-code`, `text-embedding-v3`, …) | **403 `AccessDenied.Unpurchased`** (all) |

**Successful inference calls: 0 of 144.**

The non-Qwen row is the proof. If this were a free-quota, entitlement, or model-access issue,
`text-embedding-v3` and `qwen-turbo` would work. Nothing does. Creating a new API key and a new
workspace changed nothing, because the hold sits above both.

## Blocker 2 — the RAM user has no permissions (NOT risk control)

A RAM AccessKey was issued on 2026-07-16 and **it works**. This blocker is now precisely
characterised, and it is *not* the same wall as Model Studio.

```
$ aliyun sts GetCallerIdentity
{"AccountId":"5550384261126497",
 "Arn":"acs:ram::5550384261126497:user/bruce-hackathon-deploy",
 "IdentityType":"RAMUser"}                                    <-- key is VALID

$ aliyun fc GET /2023-03-30/functions --region ap-southeast-1
ErrorCode: AccessDenied
Message:   the caller is not authorized to perform 'fc:ListFunctions' on resource
           'acs:fc:ap-southeast-1:5550384261126497:functions/*'
AccessDeniedDetail: NoPermissionType:ImplicitDeny
                    PolicyType:AccountLevelIdentityBasedPolicy   <-- NO POLICY ATTACHED

$ aliyun ram ListPoliciesForUser --UserName bruce-hackathon-deploy
ErrorCode: NoPermission  (ImplicitDeny)                        <-- cannot introspect itself
```

**`ImplicitDeny` means no policy grants the action** — the RAM user was created bare. Critically,
**no Function Compute response mentions `RISK_CONTROL` or `Unpurchased`**; the only codes returned
are `AccessDenied` / `ImplicitDeny`. That is a different failure from the Model Studio hold.

### What this does and does not tell us

- **Does:** the FC blocker so far is a fixable RAM permissions gap, self-serve, no CS ticket.
- **Does NOT:** prove FC is usable. Function Compute activation remains **unverified** — we still
  cannot ask, because the call is denied before activation is ever evaluated. Risk control is
  account-level and killed 144/144 model calls, so it *may* also block FC/ACR provisioning once
  permissions exist. **This is explicitly an open question, not a prediction.**

### Fix (root account)

Attach to RAM user `bruce-hackathon-deploy` — least privilege, **not** `AdministratorAccess`:

| Policy | Why |
|---|---|
| `AliyunFCFullAccess` | deploy/manage the function |
| `AliyunContainerRegistryFullAccess` | FC pulls images only from ACR, same region + account |
| `AliyunRDSFullAccess` | provision ApsaraDB PostgreSQL 16 |

Then re-run the probes above. If they return 200, deployment proceeds. If they return
`RISK.RISK_CONTROL_REJECTION`, the hold covers FC too and that must be recorded here.

## Blocker 3 — Function Compute is not activated

RAM policies were attached on 2026-07-17 and **the permissions blocker is resolved**:

```
$ aliyun fc GET /2023-03-30/functions --region ap-southeast-1
{"functions": []}                                   <-- was ImplicitDeny; permissions now OK

$ aliyun fc POST /2023-03-30/functions --region ap-southeast-1 ...
ErrorCode: AccessDenied
Message:   FC service is not enabled for current user.     <-- the remaining blocker
```

This is the standard "activate the service" step, **not** risk control: no Function Compute response
has ever contained `RISK_CONTROL` or `Unpurchased`. Activate FC in the Singapore console
(https://fcnext.console.aliyun.com/). Whether the account-level risk hold also blocks *activation*
is still **unknown** — activation is an "order", which is what the hold suspends. That is the next
thing we learn.

### Target changed: Custom Container -> Web Function (code package)

ACR **Personal Edition is not offered** in this account's Singapore console, and FC pulls
custom-container images only from ACR in the same region+account. Buying ACR Enterprise is not
justified for a hackathon. The container target (`deploy/s.yaml`, Dockerfile) is **retained for
later production use, not deleted** — it is verified working locally and is blocked solely on ACR.

The hackathon slice now ships as a **code package on `custom.debian12` (Python 3.11)** —
`deploy/s-webfunction.yaml`. This needs no ACR at all.

### Package sizing — measured, fits, no cuts needed

Built in a real `linux/amd64` `python:3.11-slim` container (macOS/arm64 wheels would not load on
FC: `asyncpg`, `cryptography`, `pydantic-core`, `pillow` are compiled extensions).

| | Unpacked | Zip | Base64 (API body) |
|---|---|---|---|
| **Full set (shipped)** | 138 MB | **44 MB** | **60.7 MB** |
| Without `pdfplumber` + plain `uvicorn` | 83 MB | 22.2 MB | 29.5 MB |

Limits: **500 MB** code package (Singapore; 100 MB in other regions), and **100 MB** for the
create/update *request body* including the base64 code — the latter is the binding constraint.
**60.7 MB fits with ~40% headroom, so no dependency reduction is required.**

`pdfplumber` (~40MB: pillow + pdfminer + pypdfium2) is deliberately **kept**: it fits, and
`extraction._pdf_to_text` swallows exceptions and returns `""`, so removing it would turn a PDF
into a successful-looking EMPTY intake — a false completion. If cold starts ever force the cut, fix
that swallow first.

### Verified locally against FC's actual runtime contract

`python:3.11-slim` **is** Debian 12 (bookworm) — the same base as `custom.debian12`. Package mounted
at `/code`, started via `./bootstrap` with `FC_SERVER_PORT=9000`:

```
/health            -> 200 {"status":"ok","service":"bruce-engine","commit":"28d142c","region":"ap-southeast-1"}
POST /v1/intake    -> 401      POST /v1/missions -> 401      GET /v1/diagnostics -> 401
bootstrap mode     -> 0o755 (executable — FC will not run it otherwise)
.env in package    -> no
```

### KNOWN RISK — cold start vs FC's 15-second limit

FC kills an instance whose HTTP server is not listening within **15s**. The emulated run took
**26s**, which would fail. Measured breakdown:

| | Time |
|---|---|
| `import bruce_engine.api`, QEMU-emulated amd64 | 24.8s |
| `import bruce_engine.api`, native | 2.0s |
| **QEMU penalty** | **~12×** |

So the 26s is overwhelmingly emulation, and real FC (native amd64) should import in ~2–4s. **This
is an inference, not a measurement** — it cannot be confirmed until a real deployment, and FC cold
start also includes fetching/extracting the 44MB package on hardware slower than a dev laptop.

If the deployed function times out on cold start, in order: (1) drop `pdfplumber` +
`uvicorn[standard]` → 83MB/22MB (fix the `_pdf_to_text` swallow first); (2) lazy-import
`pydantic_ai`/`openai` (5.7s emulated, the single heaviest import) off the startup path;
(3) enable FC provisioned instances to remove cold start entirely.

## Blocker 4 — no Google Calendar credentials

`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` are unset, so calendar
execution and read-back verification have never run against Google. The adapter and its 15 tests
(including the wire format, via a mock transport against the real client stack) pass; the live test
skips.

---

## What was verified locally, and how

Reproduce:

```bash
cd engine
docker build --build-arg BRUCE_COMMIT=$(git rev-parse --short HEAD) -t bruce-engine:local .
docker run -d --name bruce-test -p 9099:9000 -e BRUCE_JWT_SECRET=<32+ bytes> \
  -e BRUCE_REGION=ap-southeast-1 bruce-engine:local
curl -s localhost:9099/health
BRUCE_DEPLOY_URL=http://localhost:9099 python -m pytest tests/test_deployment_smoke.py
```

Observed:

```
$ curl -s localhost:9099/health
{"status":"ok","service":"bruce-engine","commit":"d055cba","region":"ap-southeast-1"}

GET  /v1/diagnostics -> 401      POST /v1/intake   -> 401      POST /v1/missions -> 401
$ docker exec bruce-test id
uid=10001(bruce) gid=10001(bruce) groups=10001(bruce)
$ docker run --rm --entrypoint sh bruce-engine:local -c 'ls -a /app | grep -c "^\.env$"'
0
$ BRUCE_DEPLOY_URL=http://localhost:9099 pytest tests/test_deployment_smoke.py
7 passed, 2 skipped
```

The smoke suite is the same one that will run against the live URL — it took a real HTTP round trip
to a real container, not a `TestClient`.

**Caveat, stated because it matters:** the image was built for **arm64** (Apple Silicon, via Colima;
x86 emulation needs QEMU which is not installed). Function Compute requires **linux/amd64**. The
push command in [`deploy/README.md`](../deploy/README.md) uses `--platform linux/amd64`, but that
build has **not** been performed or tested. Do not assume the amd64 image builds identically.

---

## Qwen provider status — precise

- Adapter implemented — `bruce_engine/llm.py` (`qwen()`, env-configured, no hard-coded host).
- Multimodal intake implemented — `bruce_engine/extraction.py`, replacing an OpenAI vision call.
- Wire format **tested** — `enable_thinking: false`, the base64 image part, the literal word
  "json", host and auth header asserted through the real client stack via `httpx.MockTransport`.
- Grounding gate preserved — hallucinated spans are dropped regardless of provider.
- Failure path **tested** — `503 provider_unavailable` naming provider/model/cause; **no fallback**
  to another provider; never an empty-but-200 intake.
- **Live inference: BLOCKED. Zero successful real Qwen Cloud calls.**

The tests prove Bruce **sends the correct request**. They do not prove Qwen works, and are not
presented as if they do. The live test skips with the 403 reason and will run unchanged the moment
the account is unblocked.

---

## What would flip each row to green

1. Alibaba CS lifts `RISK.RISK_CONTROL_REJECTION`, **or** the hackathon organizers issue a sponsored
   key/voucher (this path bypasses risk control and is likely faster).
2. Create a RAM AccessKey → activate Function Compute + Container Registry (ap-southeast-1).
3. Provision ApsaraDB PostgreSQL 16, create the restricted `bruce_app` role, run Alembic as owner.
4. `docker build --platform linux/amd64` → push to ACR → `s deploy -t deploy/s.yaml`.
5. Record URL / region / service / commit in the table above, and paste the live smoke output.
