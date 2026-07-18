"""The relay: one supervised inbound-watch loop + one outbound-poll loop. Transport only.

Inbound:  imsg.watch -> (skip echoes / already-processed) -> stage+upload attachments -> POST the
          normalized event -> checkpoint ONLY after the backend acknowledges (so a restart/outage
          never loses or double-sends).
Outbound: poll backend.claim -> imsg.send -> ack ONLY after the send command succeeds.

No Bruce logic, no model calls, no mission state, no cloud/DB credentials. Structured content-free
logs (message ids + statuses only — never text, handles, or file paths).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil

from .backend import AuthError, Backend, BackendError
from .checkpoint import FileCheckpoint
from .imsg import Imsg, ImsgEvent, parse_event

log = logging.getLogger("bruce.relay")

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
ALLOWED_MIME = {"image/png", "image/jpeg", "image/heic", "image/heif", "image/webp", "application/pdf", "text/plain"}
_EXE_MAGIC = (b"\x7fELF", b"MZ", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"#!", b"PK\x03\x04")


def _kind_for(mime: str) -> str | None:
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    return None


class Relay:
    def __init__(self, imsg: Imsg, backend: Backend, checkpoint: FileCheckpoint, *,
                 spool_dir: str, poll_interval: float = 2.0, reconnect_delay: float = 3.0,
                 sent_ledger: FileCheckpoint | None = None) -> None:
        self.imsg = imsg
        self.backend = backend
        self.checkpoint = checkpoint
        # Durable at-most-once ledger of outbound ids we've already attempted to send. Guarantees a
        # reclaimed row (ack lost, lease expired, relay crashed) is NEVER sent to a real person twice.
        self.sent_ledger = sent_ledger
        self.spool_dir = spool_dir
        self.poll_interval = poll_interval
        self.reconnect_delay = reconnect_delay
        self._stop = asyncio.Event()
        os.makedirs(spool_dir, exist_ok=True)

    def stop(self) -> None:
        self._stop.set()

    # ---- inbound -----------------------------------------------------------------------------

    async def _stage_attachments(self, event: ImsgEvent) -> tuple[list[dict], bool]:
        """Upload the message's attachments. Returns (normalized_attachments, deferred). deferred=True
        means a file is still downloading — the WHOLE message is retried later (not checkpointed)."""
        out: list[dict] = []
        for a in event.attachments:
            if a.get("missing"):
                return [], True  # still downloading -> defer the message
            path = a.get("original_path") or a.get("converted_path")
            mime = a.get("mime_type") or a.get("converted_mime_type") or ""
            kind = _kind_for(mime)
            if not path or not os.path.exists(path) or kind is None or mime not in ALLOWED_MIME:
                continue  # unsupported / unresolved -> skip this attachment (never fail the message)
            if os.path.getsize(path) > MAX_ATTACHMENT_BYTES:
                continue
            spool = os.path.join(self.spool_dir, hashlib.sha256(path.encode()).hexdigest())
            shutil.copy(path, spool)  # private spool copy
            try:
                with open(spool, "rb") as f:
                    data = f.read()
                if any(data.startswith(sig) for sig in _EXE_MAGIC):
                    continue  # reject executables client-side too
                ref = await self.backend.upload(data, mime, a.get("transfer_name"))
                out.append({"kind": kind, "media_type": mime, "upload_ref": ref})
            finally:
                # Delete the spool copy once the upload is confirmed (or on any failure — we retry).
                try:
                    os.remove(spool)
                except OSError:
                    pass
        return out, False

    async def process_inbound(self, event: ImsgEvent) -> str:
        if event.is_from_me:
            return "echo"                                   # Bruce's own message
        if not event.guid or self.checkpoint.has(event.guid):
            return "duplicate"
        try:
            atts, deferred = await self._stage_attachments(event)
            if deferred:
                return "deferred"                           # attachment still downloading; retry later
            resp = await self.backend.post_inbound({
                "provider_message_id": event.guid,
                "channel_identity": event.sender or "",
                "chat_guid": event.chat_guid,
                "is_group": event.is_group,
                "is_from_me": False,
                "text": event.text,
                "attachments": atts,
                "reply_to_message_id": event.reply_to_guid,
                "timestamp": event.created_at,
            })
        except BackendError:
            log.warning("inbound_retry guid=%s", event.guid)   # do NOT checkpoint -> retried
            return "retry"
        self.checkpoint.mark(event.guid)                    # durable ack -> safe to not reprocess
        log.info("inbound_ok guid=%s status=%s", event.guid, resp.get("status"))
        return resp.get("status", "processed")

    async def process_inbound_dict(self, raw: dict) -> str:
        """Convenience wrapper: parse a raw imsg watch object then process it (used by the dry-run
        harness + tests). Live watch events already arrive as ImsgEvent via imsg.watch()."""
        return await self.process_inbound(parse_event(raw))

    async def run_inbound(self) -> None:
        while not self._stop.is_set():
            try:
                async for event in self.imsg.watch():
                    if self._stop.is_set():
                        return
                    await self.process_inbound(event)
            except AuthError:
                log.error("relay credential rejected — stopping")
                self.stop()
                return
            except Exception:
                log.warning("watch_dropped — reconnecting")   # imsg/Messages restart
            if not self._stop.is_set():
                await asyncio.sleep(self.reconnect_delay)     # reconnect

    # ---- outbound ----------------------------------------------------------------------------

    async def process_one_outbound(self) -> bool:
        try:
            job = await self.backend.claim()
        except (BackendError, AuthError):
            return False
        if job is None:
            return False
        oid = str(job["id"])
        # At-most-once: if we've already attempted this id, DO NOT send it again — just re-ack. This
        # is what stops a reclaimed row (lost ack / expired lease / crash) from double-texting a human.
        if self.sent_ledger is not None and self.sent_ledger.has(oid):
            await self.backend.ack(job["id"], "sent", None, None)
            log.info("outbound_skip_resend id=%s (already attempted)", job["id"])
            return True
        # Mark BEFORE sending: if anything after the send throws (ack, response read), the row is never
        # resent. The trade-off is at-most-once (a genuinely failed first attempt isn't retried) — the
        # correct bias for real messaging, where a duplicate text is worse than a missed reply.
        if self.sent_ledger is not None:
            self.sent_ledger.mark(oid)
        try:
            guid = await self.imsg.send_text(job["to"], job["text"])   # attachments in outbound: future
        except Exception as exc:
            # Ledger already blocks a resend; report failure so the server can move it out of pending.
            await self.backend.ack(job["id"], "retryable_failed", None, f"{type(exc).__name__}")
            return True
        # Ack ONLY after the send command succeeded.
        await self.backend.ack(job["id"], "sent", guid, None)
        log.info("outbound_sent id=%s", job["id"])
        return True

    async def run_outbound(self) -> None:
        while not self._stop.is_set():
            try:
                handled = await self.process_one_outbound()
            except Exception:
                handled = False
            if not handled:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def run(self) -> None:
        await asyncio.gather(self.run_inbound(), self.run_outbound())
