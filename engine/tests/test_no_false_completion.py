"""A failure to READ something must never be reported as "read it, found nothing".

These are the tests for the single most product-destroying bug class in Bruce: an extraction path
that swallows an error and returns an empty-but-successful intake. To a student that renders as
"Bruce read your flyer and there were no deadlines" — a confident lie about a document Bruce never
actually read. Bruce's whole claim is that it proves its results, so every read failure must be
loud and typed.

Also covers scanned-PDF -> vision routing, provider-outage mapping, and /ready vs /health.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

import bruce_engine.api as api
from bruce_engine import extraction
from bruce_engine.extraction import SourceParseError, UnsupportedSourceType, _pdf_to_text
from bruce_engine.models import ExtractedIntake, IntakeSourceKind
from bruce_engine.provider_status import ProviderUnavailable
from bruce_engine.repositories import InMemoryMissionRepository, InMemoryStore

SECRET = "test-secret-that-is-at-least-32-bytes-long!!"
client = TestClient(api.app)


class _NoopUserRepo:
    async def ensure(self, user_id, **k): return None
    async def delete(self, user_id): return None


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", SECRET)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    monkeypatch.setattr(api, "_mission_repo", InMemoryMissionRepository(InMemoryStore()))
    monkeypatch.setattr(api, "_user_repo", _NoopUserRepo())


def _auth(uid):
    return {"Authorization": f"Bearer {jwt.encode({'sub': str(uid), 'exp': int(time.time())+3600}, SECRET, algorithm='HS256')}"}


class _FakeTranscript:
    """Stand-in for TranscriptResult — only .text is read by the routing seam."""

    def __init__(self, text):
        self.text = text
        self.provider, self.model = "openai", "gpt-5.4-mini"
        self.input_tokens = self.output_tokens = self.latency_ms = 0


def _fake_transcriber(text):
    class _T:
        async def transcribe(self, data, mime):
            return _FakeTranscript(text)
    return lambda: _T()


# --------------------------------------------------------------------------- PDF


def test_non_pdf_bytes_raise_typed_unsupported_not_empty_text():
    """Previously returned "" -> empty intake -> a 200 claiming nothing was in the file."""
    with pytest.raises(UnsupportedSourceType) as e:
        _pdf_to_text(b"this is not a pdf at all")
    assert e.value.status_code == 415
    assert "pdf" in str(e.value).lower()


def test_corrupt_pdf_raises_typed_parse_error_not_empty_text():
    with pytest.raises(SourceParseError) as e:
        _pdf_to_text(b"%PDF-1.4\n<<<< garbage that is not a real pdf body >>>>")
    assert e.value.status_code == 422 and e.value.kind == "pdf"


def test_scanned_image_only_pdf_yields_empty_textlayer_for_vision_routing(monkeypatch):
    """A photographed flyer saved as PDF has no text layer, so pdfplumber returns "". That is NOT
    an error here — it is the signal that extract_from_pdf must route to vision. _pdf_to_text
    returns "" (never a raise); the routing test below proves it never surfaces as an empty read."""
    class _Page:
        def extract_text(self): return None

    class _PDF:
        pages = [_Page()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda *a, **k: _PDF())
    assert _pdf_to_text(b"%PDF-1.4 scanned") == ""


def test_scanned_pdf_routes_to_vision_never_a_silent_empty_read(monkeypatch):
    """The nastiest case, end to end: no text layer -> vision transcribes -> structured extract runs
    on the vision text. It must NEVER become 'read your PDF, found nothing'."""
    monkeypatch.setattr(extraction, "_pdf_to_text", lambda data: "")  # simulate no text layer
    monkeypatch.setattr(extraction, "OpenAIVisionTranscriber", _fake_transcriber("SCIENCE FAIR — forms due May 3"))
    seen = {}

    async def _stub_structured(text, source_kind, doc_type):
        seen["text"], seen["doc_type"] = text, doc_type
        return ExtractedIntake(source_kind=source_kind), object()

    monkeypatch.setattr(extraction, "_run_structured", _stub_structured)
    asyncio.run(extraction.extract_from_pdf_traced(b"%PDF-1.4 scanned"))
    assert seen["doc_type"] == "pdf_scanned"
    assert "SCIENCE FAIR" in seen["text"]


def test_scanned_pdf_with_empty_vision_is_an_error_not_empty_read(monkeypatch):
    monkeypatch.setattr(extraction, "_pdf_to_text", lambda data: "")
    monkeypatch.setattr(extraction, "OpenAIVisionTranscriber", _fake_transcriber("   "))
    with pytest.raises(SourceParseError) as e:
        asyncio.run(extraction.extract_from_pdf_traced(b"%PDF-1.4 scanned"))
    assert e.value.kind == "pdf"


def test_missing_pdfplumber_is_an_error_not_an_empty_result(monkeypatch):
    """If the dependency is ever trimmed from the deployment package, PDFs must FAIL, not silently
    return nothing."""
    real_import = __import__("builtins").__import__

    def fake_import(name, *a, **k):
        if name == "pdfplumber":
            raise ImportError("No module named 'pdfplumber'")
        return real_import(name, *a, **k)

    monkeypatch.setitem(__import__("builtins").__dict__, "__import__", fake_import)
    try:
        with pytest.raises(SourceParseError) as e:
            _pdf_to_text(b"%PDF-1.4 real")
        assert "not installed" in str(e.value)
    finally:
        monkeypatch.setitem(__import__("builtins").__dict__, "__import__", real_import)


# --------------------------------------------------------------------------- image


def test_unsupported_image_type_is_rejected_before_the_provider(monkeypatch):
    """A 415 up front beats an obscure provider error — and costs no tokens."""
    with pytest.raises(UnsupportedSourceType) as e:
        asyncio.run(extraction._image_to_text(b"\x00\x01", mime="image/tiff"))
    assert e.value.status_code == 415
    assert "image/png" in e.value.supported


def test_empty_transcription_is_an_error_not_a_successful_empty_intake(monkeypatch):
    """Provider answered but transcribed nothing -> must raise. Returning "" would flow into the
    extractor and surface as 'read the flyer, found no deadlines'."""
    monkeypatch.setattr(extraction, "OpenAIVisionTranscriber", _fake_transcriber("   "))
    with pytest.raises(SourceParseError) as e:
        asyncio.run(extraction._image_to_text(b"\x89PNG\r\n", mime="image/png"))
    assert e.value.kind == "image"


# --------------------------------------------------------------------------- API mapping


@pytest.mark.parametrize(
    "exc,expected",
    [
        (UnsupportedSourceType("nope", detected="image/tiff", supported=["image/png"]), 415),
        (SourceParseError("corrupt", kind="pdf"), 422),
    ],
)
def test_intake_maps_extraction_errors_to_precise_status_codes(monkeypatch, exc, expected):
    async def boom(**kw):
        raise exc

    monkeypatch.setattr(api, "_persist_intake", boom)
    r = client.post("/v1/intake", json={"text": "x"}, headers=_auth(uuid4()))
    assert r.status_code == expected
    assert r.json()["detail"]["error"] in ("unsupported_source_type", "source_parse_failed")


def test_intake_maps_provider_outage_to_503_provider_unavailable(monkeypatch):
    """A provider outage is a truthful 503 naming the provider — never a 200 empty read, and never
    silently answered by a different provider."""
    async def boom(**kw):
        raise ProviderUnavailable(provider="featherless", model="Qwen/Qwen3-32B", reason="rate limit", status_code=429)

    monkeypatch.setattr(api, "_persist_intake", boom)
    r = client.post("/v1/intake", json={"text": "x"}, headers=_auth(uuid4()))
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "provider_unavailable"


def test_extraction_failure_never_returns_a_successful_empty_intake(monkeypatch):
    """The invariant, stated once: no read failure produces a 200."""
    for exc in (
        UnsupportedSourceType("bad type"),
        SourceParseError("bad instance", kind="pdf"),
        ProviderUnavailable(provider="openai", model="gpt-5.4-mini", reason="down"),
    ):
        def make(e):
            async def boom(**kw):
                raise e
            return boom

        monkeypatch.setattr(api, "_persist_intake", make(exc))
        r = client.post("/v1/intake", json={"text": "x"}, headers=_auth(uuid4()))
        assert r.status_code != 200
        assert "source_id" not in r.text and "task_ids" not in r.text


def test_extraction_error_detail_leaks_no_student_content(monkeypatch):
    secret = "SECRET essay draft and parent phone 555-0100"

    async def boom(**kw):
        raise SourceParseError("could not parse the PDF (ValueError)", kind="pdf")

    monkeypatch.setattr(api, "_persist_intake", boom)
    r = client.post("/v1/intake", json={"text": secret}, headers=_auth(uuid4()))
    assert secret not in r.text and "555-0100" not in r.text


# --------------------------------------------------------------------------- /ready vs /health


def test_health_never_depends_on_the_database_or_a_provider():
    """/health must stay dumb: if it touched the DB, a DB blip would make the platform recycle a
    perfectly serving process."""
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ready_is_public_and_reports_per_check_state():
    r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "database" in body["checks"] and "auth_config" in body["checks"]


def test_ready_does_not_depend_on_a_model_provider():
    """A blocked model provider must NOT make the whole service unready — intake returns a truthful
    503, while missions/decisions/receipts keep working. Provider state is reported by
    /v1/diagnostics."""
    body = client.get("/ready").json()
    assert "provider" not in str(body["checks"]).lower()
    assert "qwen" not in str(body["checks"]).lower()


def test_ready_flags_a_weak_jwt_secret(monkeypatch):
    """A short secret on a public URL is a real exposure — readiness must not call that ok."""
    monkeypatch.setenv("BRUCE_JWT_SECRET", "short")
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    r = client.get("/ready")
    assert r.status_code == 503
    assert "weak" in r.json()["checks"]["auth_config"]


def test_ready_leaks_no_secrets():
    body = client.get("/ready").text.lower()
    for needle in ("sk-", "password", "postgresql", SECRET.lower()):
        assert needle not in body
