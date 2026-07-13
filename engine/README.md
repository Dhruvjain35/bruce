# Bruce engine

The brain: grounded professor discovery + personalized outreach drafting. Backend, no UI.
The native iOS app will be a thin client on this engine's API — built only after the engine
produces output that makes an ambitious student go *"this is better than what I'd write, and
every fact is real."*

## Setup

```bash
cd engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Secrets go in `engine/.env` (git-ignored, never committed):

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Test

```bash
cd engine
pytest
```

## Structure

| module | role |
|---|---|
| `bruce_engine/models.py` | typed data contracts + the grounding contract (done) |
| `bruce_engine/discovery.py` | find real professors matching the goal (pending research) |
| `bruce_engine/verify.py` | confirm person + papers exist — anti-hallucination gate (pending) |
| `bruce_engine/drafting.py` | one personalized, voice-matched, grounded email each (pending) |
| `bruce_engine/pipeline.py` | orchestration: discover → verify → draft (done) |

`discovery` / `verify` / `drafting` raise `NotImplementedError` on purpose — their real
implementations depend on the grounding research pass (which academic APIs to use, how to
find professor emails, what makes outreach effective) rather than on memorized API shapes.
