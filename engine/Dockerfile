# Bruce engine — one image, two entrypoints (API service + worker pool).
# The API runs uvicorn; the Cloud Run Worker Pool overrides CMD to run the claim loop.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + migrations.
COPY bruce_engine ./bruce_engine
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Non-root.
RUN useradd --create-home --uid 10001 bruce
USER bruce

# Cloud Run injects $PORT (default 8080). The API is the default entrypoint; the private worker
# service overrides CMD with:  uvicorn bruce_engine.worker_api:app --host 0.0.0.0 --port $PORT
CMD ["sh", "-c", "uvicorn bruce_engine.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
