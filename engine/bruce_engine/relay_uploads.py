"""Phase 7 (server) — staged attachment uploads from the relay.

The relay uploads an inbound attachment's bytes here (authenticated); the inbound handler consumes it
into the durable intake source, then the bytes are cleared. Strict validation at the boundary: a MIME
allowlist, a size cap, and an executable/magic-byte reject — the relay must never turn a texted file
into code execution or an oversized blob. Dedup by content hash so a redelivered file isn't re-stored.
"""

from __future__ import annotations

import datetime
import hashlib
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import worker_session

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/jpg", "image/heic", "image/heif", "image/webp",
    "application/pdf", "text/plain",
}
# Leading bytes of common executables/scripts — reject regardless of the declared MIME.
_EXECUTABLE_MAGIC = (
    b"\x7fELF",            # ELF
    b"MZ",                 # PE / DOS
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe",  # Mach-O
    b"#!",                 # shebang script
    b"PK\x03\x04",         # zip/jar/office-macro container — not an allowed intake type
)


class UploadRejected(Exception):
    """The upload failed validation (type / size / looks executable). Carries a short reason only."""


def _looks_executable(data: bytes) -> bool:
    return any(data.startswith(sig) for sig in _EXECUTABLE_MAGIC)


async def store_upload(*, relay_device_id: UUID | None, data: bytes, media_type: str,
                       filename: str | None = None) -> tuple[UUID, str]:
    """Validate + stage an upload. Returns (upload_ref, content_hash). Idempotent on content_hash for
    unconsumed uploads (a duplicate returns the existing ref)."""
    if not data:
        raise UploadRejected("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected("file too large")
    if media_type not in ALLOWED_MIME:
        raise UploadRejected(f"unsupported type {media_type!r}")
    if _looks_executable(data):
        raise UploadRejected("rejected: file looks executable")

    content_hash = hashlib.sha256(data).hexdigest()
    async with worker_session() as s:
        existing = (await s.execute(
            select(schema.RelayUpload).where(schema.RelayUpload.content_hash == content_hash,
                                             schema.RelayUpload.consumed_at.is_(None),
                                             schema.RelayUpload.data.isnot(None))
        )).scalars().first()
        if existing is not None:
            return existing.id, content_hash
        row = schema.RelayUpload(relay_device_id=relay_device_id, content_hash=content_hash,
                                 media_type=media_type, filename=filename, size_bytes=len(data), data=data)
        s.add(row)
        await s.flush()
        return row.id, content_hash


async def fetch_bytes(upload_ref: UUID) -> tuple[bytes, str] | None:
    async with worker_session() as s:
        row = (await s.execute(select(schema.RelayUpload).where(schema.RelayUpload.id == upload_ref))).scalar_one_or_none()
        if row is None or row.data is None:
            return None
        return row.data, row.media_type


async def consume(upload_ref: UUID) -> None:
    """Clear the staged bytes once the durable intake source has them (persistence confirmed)."""
    async with worker_session() as s:
        row = (await s.execute(select(schema.RelayUpload).where(schema.RelayUpload.id == upload_ref))).scalar_one_or_none()
        if row is not None:
            row.data = None
            row.consumed_at = datetime.datetime.now(datetime.timezone.utc)
