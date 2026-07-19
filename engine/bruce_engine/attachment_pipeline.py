"""AttachmentUnderstandingPipeline — stage 1: NORMALIZE a student photo/screenshot/document into a
web-safe raster the vision model actually accepts.

Why this exists: iPhone camera photos are HEIC, and OpenAI's vision API rejects HEIC/HEIF with a hard
HTTP 400. Before this, HEIC bytes were forwarded raw and the 400 surfaced as a generic "couldn't read
that one, resend" — a false negative on a perfectly readable photo. Screenshots (PNG) worked; camera
photos didn't. This module converts HEIC/HEIF (and any non-web-safe raster) to JPEG, applies EXIF
orientation, and bounds oversized images — so a healthy photo is never rejected for its container.

Stage 2 (separate PR) adds quality metrics, multi-scale tiling for tiny text, and OCR fallback. This
stage is deliberately conservative: it never destroys detail beyond a single sane downscale, and it
distinguishes a genuinely-undecodable file (UnreadableAttachment) from a model outage, so Bruce only
says "i can't open that" when the bytes truly can't be opened — never for a format the model dislikes.
"""

from __future__ import annotations

import dataclasses
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("bruce.attachments")

try:  # HEIC/HEIF support (iPhone camera default). Wheel ships libheif; no system package needed.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - environment without the wheel
    HEIF_SUPPORTED = False

# Formats the OpenAI-style vision API accepts directly. Anything else is converted to JPEG (or PNG).
WEB_SAFE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}

# Cap the long edge. 2048 matches the vision model's own high-detail bound, so we lose no effective
# resolution vs. what the provider does internally, while shrinking uploads and latency. Tiny-text
# rescue via tiling is stage 2, not an excuse to send 12-megapixel originals here.
MAX_EDGE = 2048
JPEG_QUALITY = 90
# Reject absurd images (decompression-bomb guard) rather than let Pillow raise deep in a worker.
MAX_PIXELS = 60_000_000


class UnreadableAttachment(Exception):
    """The bytes genuinely could not be decoded as an image (corrupt / truncated / unknown container).
    Distinct from a vision-model outage: this means 'i can't open this file', which IS honest."""


@dataclasses.dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    media_type: str          # always web-safe (image/jpeg or image/png)
    width: int
    height: int
    source_format: str       # what the bytes actually were (JPEG/HEIF/PNG/…), sniffed — not the claimed MIME
    converted: bool          # container was changed (e.g. HEIF -> JPEG)
    downscaled: bool


def normalize_image(data: bytes, media_type: str | None = None) -> NormalizedImage:
    """Decode by CONTENT (not the claimed extension/MIME), fix EXIF orientation, convert non-web-safe
    formats to JPEG, and bound the long edge. Raises UnreadableAttachment if the bytes can't be opened."""
    if not data:
        raise UnreadableAttachment("empty attachment")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()                                   # force full decode now so truncation errors surface here
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise UnreadableAttachment(f"cannot decode image ({type(e).__name__})") from e

    src_format = (im.format or "UNKNOWN").upper()
    if im.width * im.height > MAX_PIXELS:
        raise UnreadableAttachment("image exceeds pixel budget")

    im = ImageOps.exif_transpose(im)                # camera photos are rotated in metadata only -> bake it in

    downscaled = max(im.size) > MAX_EDGE
    if downscaled:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    # Target container: keep JPEG/PNG/WEBP; GIF -> PNG (vision treats it as a static frame); everything
    # else (HEIF/HEIC/TIFF/BMP/…) -> JPEG.
    if src_format in {"JPEG", "PNG", "WEBP"}:
        target = src_format
    elif src_format == "GIF":
        target = "PNG"
    else:
        target = "JPEG"

    if target == src_format and not downscaled:
        # already web-safe and reasonably sized: return the ORIGINAL bytes (no recompression artifacts)
        return NormalizedImage(data=data, media_type=FORMAT_MIME[src_format], width=im.width,
                               height=im.height, source_format=src_format, converted=False, downscaled=False)

    out = io.BytesIO()
    if target == "PNG":
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGB")
        im.save(out, format="PNG", optimize=True)
        mime = "image/png"
    else:  # JPEG
        if im.mode != "RGB":
            im = im.convert("RGB")                  # JPEG has no alpha; flatten palettes/RGBA
        im.save(out, format="JPEG", quality=JPEG_QUALITY)
        mime = "image/jpeg"

    log.info("attachment_normalized src=%s target=%s converted=%s downscaled=%s",
             src_format, target, target != src_format, downscaled)
    return NormalizedImage(data=out.getvalue(), media_type=mime, width=im.width, height=im.height,
                           source_format=src_format, converted=(target != src_format), downscaled=downscaled)
