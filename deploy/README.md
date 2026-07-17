# Deploying Bruce to Alibaba Cloud Function Compute (ap-southeast-1)

**Status: NOT YET DEPLOYED.** See [`docs/deployment-verification.md`](../docs/deployment-verification.md)
for the exact blocker and evidence. Every command below is written to be run, but none has been run
against a live account. Nothing here should be read as proof of a deployment.

## Why Function Compute

New users get **150,000 CU/month for three months**, and there is no always-on instance. Bruce's API
is request-driven, so FC bills near zero for a hackathon demo. ECS would mean paying for idle time.

## Prerequisites

| Requirement | Status here | Notes |
|---|---|---|
| Alibaba Cloud account, no risk hold | **BLOCKED** | `RISK.RISK_CONTROL_REJECTION` |
| RAM AccessKey (`AccessKeyId`/`AccessKeySecret`) | **MISSING** | Not the DashScope key — a different credential |
| Function Compute activated (ap-southeast-1) | Unverified | Cannot check without an AccessKey |
| Container Registry (ACR) namespace, same region+account | Unverified | FC will not pull from Docker Hub/ghcr |
| ApsaraDB RDS for PostgreSQL 16 | Not provisioned | Postgres stays external — see below |
| `docker`, `s` (Serverless Devs), `aliyun` CLI | **NOT INSTALLED** | on this machine |

## 1. Credentials

```bash
# RAM AccessKey — create at https://ram.console.aliyun.com/manage/ak
# Grant AliyunFCFullAccess + AliyunContainerRegistryFullAccess (least privilege; NOT the root key).
s config add --AccessKeyID <id> --AccessKeySecret <secret> --AccountID <uid> -a default
```

Never commit these. They belong in your shell/CI secret store only.

## 2. Database (stays external, on purpose)

Bruce's security model is enforced by PostgreSQL, not by application code: RLS + `FORCE RLS`, a
restricted non-owner `bruce_app` role, and FK evidence lineage. The container therefore ships **no
database**, and there is **no in-memory fallback** — a stateless demo would silently discard the
architecture the product rests on.

```bash
# ApsaraDB RDS for PostgreSQL 16, same region (ap-southeast-1).
# Create the restricted role, then run migrations AS OWNER from your machine — never at container
# startup (FC runs many instances concurrently; racing Alembic corrupts schemas).
export BRUCE_DATABASE_URL='postgresql+asyncpg://<owner>:<pw>@<rds-host>:5432/bruce'
cd engine && python -m alembic -c alembic.ini upgrade head
```

## 3. Build and push the image

```bash
cd engine
export BRUCE_COMMIT=$(git rev-parse --short HEAD)
export ACR=registry-ap-southeast-1.aliyuncs.com/<namespace>/bruce-engine
export BRUCE_IMAGE=$ACR:$BRUCE_COMMIT

docker login registry-ap-southeast-1.aliyuncs.com -u <acr-user>
# linux/amd64 is required — FC will not run an arm64 image built on Apple Silicon.
docker build --platform linux/amd64 --build-arg BRUCE_COMMIT=$BRUCE_COMMIT -t $BRUCE_IMAGE .
docker push $BRUCE_IMAGE

# Verify no secret reached a layer:
docker history --no-trunc $BRUCE_IMAGE | grep -i -E 'sk-|secret|password' && echo "LEAK" || echo "clean"
```

Tagging by commit SHA (not `latest`) is what makes a live URL provably tie to an exact commit.

## 4. Deploy

```bash
# Secrets are read from YOUR shell by ${env(...)} in s.yaml and stored by FC — never in the image.
export BRUCE_JWT_SECRET=... BRUCE_JWT_AUDIENCE=...
export BRUCE_APP_DATABASE_URL='postgresql+asyncpg://bruce_app:<pw>@<rds-host>:5432/bruce'
export DASHSCOPE_API_KEY=... QWEN_BASE_URL=... QWEN_INTAKE_MODEL=qwen3.7-plus
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... GOOGLE_CALENDAR_ID=primary

s deploy -t deploy/s.yaml
```

For anything long-lived, prefer KMS Secrets Manager over plain FC environment variables.

## 5. Verify the deployment is real

```bash
export BRUCE_DEPLOY_URL=https://<generated>.ap-southeast-1.fcapp.run

# public liveness — must report the commit you just deployed
curl -s $BRUCE_DEPLOY_URL/health

# auth is genuinely enforced (expect 401, NOT 200)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BRUCE_DEPLOY_URL/v1/intake -d '{"text":"x"}'

# authenticated diagnostics — honest provider state
curl -s $BRUCE_DEPLOY_URL/v1/diagnostics -H "Authorization: Bearer <jwt>"

# the smoke test that asserts all of the above against the LIVE deployment
cd engine && BRUCE_DEPLOY_URL=$BRUCE_DEPLOY_URL .venv/bin/python -m pytest tests/test_deployment_smoke.py -v
```

Record the resulting URL, region, service name and commit SHA in
[`docs/deployment-verification.md`](../docs/deployment-verification.md). A deployment that is not
recorded there is not deployed.

## Qwen status in the deployment

The Qwen provider is configured and stays configured. While inference is account-blocked, the intake
path returns **503 `provider_unavailable`** naming the provider, model and cause. It does **not**
fall back to another provider — silently answering with OpenAI while claiming a Qwen-powered
workflow would make the demonstration dishonest. `/v1/diagnostics` reports `live: false` until a real
Qwen call succeeds.
