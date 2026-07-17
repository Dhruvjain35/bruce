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

## Blocker 2 — no RAM AccessKey

Deploying to Function Compute needs an Alibaba Cloud **RAM AccessKey** (`AccessKeyId` /
`AccessKeySecret`). That is a **different credential** from the DashScope/Model Studio API key we
hold. Without it the FC and ACR APIs cannot be called at all, so we cannot even reach — and
therefore cannot report — the FC activation error. Function Compute activation status is
**unverified**, not "blocked": we have not been able to ask.

Given that risk control blocks inference account-wide, the working assumption is that it also blocks
FC/ACR provisioning, but that is an **assumption and is labelled as one** until an AccessKey exists
to test it.

## Blocker 3 — no Google Calendar credentials

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
