"""Run the Bruce engine API locally.

Loads engine/.env (provider keys) then starts uvicorn. The API holds all keys server-side;
clients never see them.

    PYTHONPATH=. python scripts/run_api.py    # -> http://127.0.0.1:8000  (/docs for Swagger)
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import uvicorn  # noqa: E402  (import after env load is intentional)

if __name__ == "__main__":
    uvicorn.run("bruce_engine.api:app", host="127.0.0.1", port=8000, reload=False)
