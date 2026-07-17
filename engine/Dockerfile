# Bruce engine — container image for Alibaba Cloud Function Compute (custom container runtime).
#
# Design rules, in priority order:
#   1. NO SECRETS IN THE IMAGE. Nothing is COPYed that isn't source: .env is excluded by
#      .dockerignore AND is gitignored, so a key cannot reach a layer even by accident. Every
#      credential (DASHSCOPE_API_KEY, BRUCE_JWT_SECRET, database URLs) is injected at runtime by
#      Function Compute environment variables / Secrets Manager. `docker history` on this image
#      must reveal nothing.
#   2. NOT ROOT. FC allows it; running as root anyway is how a container escape becomes an account
#      compromise.
#   3. Postgres stays EXTERNAL. This image contains no database. BRUCE_APP_DATABASE_URL points at a
#      managed PostgreSQL (ApsaraDB) so RLS, the restricted bruce_app role, and durable evidence
#      lineage work in the deployment exactly as they do locally. There is deliberately no
#      in-memory fallback: a stateless demo would quietly discard the persistence architecture the
#      product is built on.
#   4. Migrations do NOT run at startup. Alembic is owner-privileged and FC may run many instances
#      concurrently; racing migrations on cold start is how schemas get corrupted. Migrations are a
#      separate, deliberate step (see deploy/README.md).
#
# BRUCE_COMMIT is baked at build time so a live URL can be tied to an exact commit — the submission
# has to prove which code is actually running.

FROM python:3.13-slim

ARG BRUCE_COMMIT=unknown
ENV BRUCE_COMMIT=${BRUCE_COMMIT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first so source edits don't bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source only. See .dockerignore — .env, .venv, tests and fixtures never enter the image.
COPY bruce_engine/ ./bruce_engine/
COPY migrations/ ./migrations/
COPY alembic.ini .

# Non-root.
RUN useradd --create-home --uid 10001 bruce && chown -R bruce:bruce /app
USER bruce

# FC custom-container runtime sends requests to this port.
EXPOSE 9000
ENV BRUCE_PORT=9000

# Single worker: FC scales by spawning instances, not by in-process workers.
CMD ["sh", "-c", "exec uvicorn bruce_engine.api:app --host 0.0.0.0 --port ${BRUCE_PORT} --workers 1"]
