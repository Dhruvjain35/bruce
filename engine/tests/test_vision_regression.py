"""Live-failure vision regression.

The worksheet that failed the first live test was an iPhone HEIC photo; the vision API rejected HEIC
with a hard 400 and Bruce falsely said "couldn't read that one". With the normalization pipeline, a
HEIC worksheet must be READ and helped with — never "resend".

fixtures/worksheet_precalc.png reproduces that worksheet (printed trig/polar problems, a tan(πθ) graph,
handwritten answers, slight perspective). The real-model assertion is OPT-IN (it costs a model call);
the structural half — HEIC decodes and normalizes to JPEG — always runs.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from bruce_engine.attachment_pipeline import HEIF_SUPPORTED, normalize_image

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "worksheet_precalc.png")


def _worksheet_as_heic() -> bytes:
    import pillow_heif

    pillow_heif.register_heif_opener()
    im = Image.open(FIXTURE).convert("RGB")
    buf = io.BytesIO(); im.save(buf, format="HEIF"); return buf.getvalue()


@pytest.mark.skipif(not HEIF_SUPPORTED, reason="pillow-heif not installed")
def test_heic_worksheet_normalizes_to_jpeg():
    """Structural regression (always runs): the HEIC worksheet decodes and comes out as JPEG, not raw
    HEIC — so it can never hit the vision API as an unsupported container again."""
    norm = normalize_image(_worksheet_as_heic(), "image/heic")
    assert norm.media_type == "image/jpeg" and norm.converted
    assert Image.open(io.BytesIO(norm.data)).format == "JPEG"


@pytest.mark.skipif(
    not (os.environ.get("BRUCE_RUN_REAL_MODEL") and os.environ.get("OPENAI_API_KEY") and HEIF_SUPPORTED),
    reason="opt-in real-model regression: set BRUCE_RUN_REAL_MODEL=1 + OPENAI_API_KEY")
def test_heic_worksheet_is_read_and_helped_not_resent():
    import asyncio

    from bruce_engine.conversation_model import VisionInput, production_reasoner
    from bruce_engine.conversation_style import ConversationStyleEngine

    norm = normalize_image(_worksheet_as_heic(), "image/heic")

    async def run():
        return await production_reasoner().decide(
            text="can u help me w this rq",
            images=[VisionInput(data=norm.data, media_type=norm.media_type)], context="")

    res = asyncio.run(run())
    reply = ConversationStyleEngine().render(res.decision.user_visible_response).lower()
    summary = (res.decision.attachment_summary or "").lower()
    assert summary, "model returned an empty attachment_summary — it didn't read the image"
    assert any(k in summary or k in reply for k in ("tan", "asymptote", "sec", "polar", "trig")), \
        "did not recognize the trig/polar content"
    assert not any(k in reply for k in ("couldn't read", "couldn't open", "resend", "clearer photo")), \
        f"falsely asked to resend a readable worksheet: {reply!r}"
    assert "—" not in reply, "reply contains an em dash"
