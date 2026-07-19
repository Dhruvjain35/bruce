"""AttachmentUnderstandingPipeline (stage 1) — normalization tests.

Covers the structural half of the live-failure regression matrix: HEIC->JPEG (the exact live bug),
EXIF orientation, oversize downscale, format passthrough, corrupt/empty -> honest UnreadableAttachment,
and multi-attachment handling. The real-model "does Bruce actually read the worksheet" check runs as a
synthetic staging test (needs the model); here we prove the bytes reaching the model are web-safe.
"""

from __future__ import annotations

import datetime
import io

import pytest
from PIL import Image

from bruce_engine.attachment_pipeline import (
    HEIF_SUPPORTED, NormalizedImage, UnreadableAttachment, normalize_image)


def _img(fmt, size=(120, 90), color="white", mode="RGB", exif=None):
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    if exif is not None:
        im.save(buf, format=fmt, exif=exif)
    else:
        im.save(buf, format=fmt)
    return buf.getvalue()


# --- format handling ------------------------------------------------------------------------------

def test_png_screenshot_passthrough():
    data = _img("PNG")
    n = normalize_image(data, "image/png")
    assert n.media_type == "image/png" and not n.converted and not n.downscaled
    assert n.data == data                       # web-safe + small -> original bytes, no recompress


def test_jpeg_photo_passthrough():
    n = normalize_image(_img("JPEG"), "image/jpeg")
    assert n.media_type == "image/jpeg" and not n.converted


def test_webp_is_web_safe_passthrough():
    n = normalize_image(_img("WEBP"), "image/webp")
    assert n.media_type == "image/webp" and not n.converted


def test_gif_converted_to_png():
    n = normalize_image(_img("GIF"), "image/gif")
    assert n.media_type == "image/png" and n.converted


@pytest.mark.skipif(not HEIF_SUPPORTED, reason="pillow-heif not installed")
def test_heic_converted_to_jpeg():
    """THE live bug: an iPhone HEIC photo must come out as JPEG the vision model accepts, not raw HEIC."""
    im = Image.new("RGB", (200, 150), "white")
    buf = io.BytesIO(); im.save(buf, format="HEIF")
    n = normalize_image(buf.getvalue(), "image/heic")
    assert n.media_type == "image/jpeg" and n.converted
    # and the output is a real, decodable JPEG
    assert Image.open(io.BytesIO(n.data)).format == "JPEG"


# --- geometry -------------------------------------------------------------------------------------

def test_exif_orientation_applied():
    exif = Image.Exif(); exif[0x0112] = 6      # orientation 6 = rotate 90 for display
    data = _img("JPEG", size=(160, 80), exif=exif)
    n = normalize_image(data, "image/jpeg")
    assert (n.width, n.height) == (80, 160)    # baked in -> dimensions transposed


def test_oversized_downscaled():
    n = normalize_image(_img("JPEG", size=(4032, 3024)), "image/jpeg")
    assert n.downscaled and max(n.width, n.height) == 2048


def test_small_image_not_upscaled():
    n = normalize_image(_img("PNG", size=(50, 50)), "image/png")
    assert (n.width, n.height) == (50, 50) and not n.downscaled


# --- honest failures (distinct from a model outage) -----------------------------------------------

def test_corrupt_bytes_raise_unreadable():
    with pytest.raises(UnreadableAttachment):
        normalize_image(b"this is definitely not an image", "image/png")


def test_empty_bytes_raise_unreadable():
    with pytest.raises(UnreadableAttachment):
        normalize_image(b"", "image/png")


def test_truncated_image_raises_unreadable():
    data = _img("PNG", size=(300, 300))
    with pytest.raises(UnreadableAttachment):
        normalize_image(data[: len(data) // 3], "image/png")   # cut mid-file -> decode fails on load()


def test_wrong_claimed_mime_still_decodes_by_content():
    """Decode by CONTENT: a JPEG mislabeled image/png must still normalize (not trust the extension)."""
    n = normalize_image(_img("JPEG"), "image/png")
    assert n.media_type == "image/jpeg"        # sniffed real format wins


# --- runtime wiring: _prepare_images ---------------------------------------------------------------

def _msg(attachments):
    from bruce_engine.messaging import ChannelKind, InboundMessage
    return InboundMessage(provider_message_id="p", channel=ChannelKind.self_hosted_imessage,
                          channel_identity="+1", text="help", attachments=attachments,
                          timestamp=datetime.datetime.now(datetime.timezone.utc))


def _att(media_type, data):
    from bruce_engine.messaging import Attachment, AttachmentKind
    kind = AttachmentKind.pdf if media_type == "application/pdf" else AttachmentKind.image
    return Attachment(kind=kind, media_type=media_type, data=data, filename="f")


def test_prepare_images_multiple_and_unreadable():
    from bruce_engine.conversation_runtime import _prepare_images
    msg = _msg([_att("image/png", _img("PNG")), _att("image/jpeg", _img("JPEG")),
                _att("image/png", b"garbage")])
    images, unreadable = _prepare_images(msg)
    assert len(images) == 2 and unreadable == 1
    assert all(v.media_type in ("image/png", "image/jpeg") for v in images)


def test_prepare_images_pdf_passthrough():
    from bruce_engine.conversation_runtime import _prepare_images
    images, unreadable = _prepare_images(_msg([_att("application/pdf", b"%PDF-1.4 ...")]))
    assert len(images) == 1 and images[0].media_type == "application/pdf" and unreadable == 0


@pytest.mark.skipif(not HEIF_SUPPORTED, reason="pillow-heif not installed")
def test_prepare_images_heic_regression():
    """Regression for the live failure: a HEIC attachment must reach the model as JPEG, never raw HEIC."""
    from bruce_engine.conversation_runtime import _prepare_images
    im = Image.new("RGB", (200, 150), "white"); buf = io.BytesIO(); im.save(buf, format="HEIF")
    images, unreadable = _prepare_images(_msg([_att("image/heic", buf.getvalue())]))
    assert unreadable == 0 and len(images) == 1 and images[0].media_type == "image/jpeg"
