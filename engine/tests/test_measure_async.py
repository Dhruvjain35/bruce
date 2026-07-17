"""Latency measurement for the async intake path — ack, end-to-end, OpenAI, persistence.

Opt-in (BRUCE_MEASURE=1) because it makes a REAL OpenAI call and needs Postgres. Not a CI test.
    BRUCE_MEASURE=1 python -m pytest tests/test_measure_async.py -s -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import os
import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import worker
from bruce_engine.intake_jobs import PostgresJobStore

pytestmark = pytest.mark.skipif(os.environ.get("BRUCE_MEASURE") != "1", reason="set BRUCE_MEASURE=1 to run")
client = TestClient(api.app)
RAW = ("Northgate Science Fair 2026. Open to grades 9-12. Registration closes Feb 28, 2026. "
       "Projects due Mar 14, 2026. Entry fee $25. A signed parent permission form is required. "
       "Judging begins the following Friday.")


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine", lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long!!")
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


def _auth(uid):
    return {"Authorization": f"Bearer {jwt.encode({'sub': str(uid), 'exp': int(time.time())+3600}, os.environ['BRUCE_JWT_SECRET'], algorithm='HS256')}"}


def test_measure(clean_db):
    uid = uuid4()

    t0 = time.perf_counter()
    r = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid))
    ack_ms = (time.perf_counter() - t0) * 1000
    assert r.status_code == 202
    mid = r.json()["mission_id"]

    # End-to-end: the worker runs the REAL extraction (OpenAI). Time the whole processing.
    telem_box = {}
    real_extract = worker._extract_for_job

    async def _timed(job):
        intake, telem = await real_extract(job)
        telem_box["telem"] = telem.as_dict()
        return intake, telem

    worker._extract_for_job = _timed
    t1 = time.perf_counter()
    asyncio.run(worker.process_one(PostgresJobStore(), worker_id="measure"))
    e2e_ms = (time.perf_counter() - t1) * 1000
    worker._extract_for_job = real_extract

    m = client.get(f"/v1/missions/{mid}", headers=_auth(uid)).json()
    tel = telem_box.get("telem", {})
    model_ms = tel.get("total_latency_ms", 0)
    persist_ms = e2e_ms - model_ms  # worker time minus the model leg ~= persistence + overhead

    print("\n================= ASYNC INTAKE LATENCY =================")
    print(f"  acknowledgement (POST -> 202)     : {ack_ms:7.1f} ms   [budget < 1000]")
    print(f"  end-to-end worker (extract+persist): {e2e_ms:7.1f} ms")
    print(f"    - model leg (OpenAI vis+extract) : {model_ms:7.1f} ms")
    print(f"    - persistence + overhead         : {persist_ms:7.1f} ms")
    print(f"  final mission phase                : {m['phase']}")
    print(f"  deadlines extracted                : {len(m.get('extracted', {}).get('deadlines', []))}")
    print(f"  telemetry                          : {tel}")
    print("=======================================================")
    assert ack_ms < 1000, "acknowledgement must be under the 1s budget"
    assert m["phase"] == "awaiting_approval"
