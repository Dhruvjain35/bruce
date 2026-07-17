"""ONE bounded live Qwen Cloud call. Run: make qwen-smoke

Deliberately minimal. It does NOT enumerate models — that was already done once (144/144 returned
403 AccessDenied.Unpurchased) and repeating it learns nothing and burns time. It tries the
configured intake model, and falls back to exactly ONE cheap text model only if the first fails,
to distinguish "this model is not entitled" from "nothing on this account is entitled".

Prints a sanitized record: timestamp, host, model, HTTP status, provider request id, error code.
Never prints the key, the response body, or student content.

Exit 0 = a real inference call succeeded (Qwen is LIVE — go update the readiness docs).
Exit 1 = still blocked. Exit 2 = not configured.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENGINE = Path(__file__).resolve().parents[1]
load_dotenv(ENGINE / ".env")


def _host(url: str) -> str:
    return httpx.URL(url).host


def probe(model: str, base: str, key: str) -> tuple[bool, dict]:
    started = datetime.datetime.now(datetime.timezone.utc)
    record: dict = {
        "timestamp_utc": started.isoformat(timespec="seconds"),
        "endpoint_host": _host(base),
        "model": model,
    }
    try:
        r = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 5,
                "enable_thinking": False,
            },
            timeout=60,
        )
    except Exception as exc:
        record |= {"http_status": None, "error_code": f"transport:{type(exc).__name__}"}
        return False, record

    record["http_status"] = r.status_code
    record["provider_request_id"] = r.headers.get("x-request-id") or r.headers.get("request-id")
    record["latency_ms"] = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)

    if r.status_code == 200:
        body = r.json()
        record |= {
            "error_code": None,
            "usage": body.get("usage"),
            "response_id": body.get("id"),
        }
        return True, record
    try:
        record["error_code"] = (r.json().get("error") or {}).get("code") or str(r.status_code)
    except Exception:
        record["error_code"] = str(r.status_code)
    return False, record


def main() -> int:
    key = os.environ.get("DASHSCOPE_API_KEY")
    base = os.environ.get("QWEN_BASE_URL")
    model = os.environ.get("QWEN_INTAKE_MODEL", "qwen3.7-plus")
    if not key or not base:
        print("NOT CONFIGURED — set DASHSCOPE_API_KEY and QWEN_BASE_URL in engine/.env")
        return 2

    print(f"Qwen smoke — ONE bounded call (no model enumeration)\n")
    ok, rec = probe(model, base, key)
    for k, v in rec.items():
        print(f"  {k}: {v}")

    if not ok and rec.get("error_code") == "AccessDenied.Unpurchased":
        # One cheap fallback ONLY to separate "this model" from "this account".
        print(f"\n  intended model blocked — trying ONE basic text model to scope the block\n")
        ok2, rec2 = probe("qwen-turbo", base, key)
        for k, v in rec2.items():
            print(f"  {k}: {v}")
        if not ok2:
            print(
                "\nSTILL BLOCKED. Even a basic text model is refused, so this is an ACCOUNT-level "
                "hold (RISK.RISK_CONTROL_REJECTION), not a per-model entitlement.\n"
                "Do NOT keep probing. The blocker stands: zero successful Qwen calls.\n"
                "docs/hackathon-readiness.md must continue to say Qwen is BLOCKED."
            )
            return 1
        ok, rec = ok2, rec2

    if ok:
        print(
            "\nQWEN IS LIVE — a real inference call succeeded.\n"
            "Next, in order:\n"
            "  1. cd engine && .venv/bin/python -m pytest tests/test_qwen_provider.py -v\n"
            "     (the live flyer test now RUNS instead of skipping)\n"
            "  2. Update docs/hackathon-readiness.md + docs/deployment-verification.md:\n"
            "     Qwen blocked -> live-verified, and paste this record as evidence.\n"
            "  3. Run the real flyer -> intake -> calendar journey.\n"
            "Do NOT launch a big evaluation matrix before the vertical slice works."
        )
        return 0

    print(f"\nBLOCKED: {rec.get('error_code')}. Leaving the blocker documented as-is.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
