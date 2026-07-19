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
import stat
import time

from .backend import AuthError, Backend, BackendError
from .checkpoint import FileCheckpoint
from .imsg import Imsg, ImsgEvent, parse_event
from .pending import PendingStore

log = logging.getLogger("bruce.relay")

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 10
ALLOWED_MIME = {"image/png", "image/jpeg", "image/heic", "image/heif", "image/webp", "application/pdf", "text/plain"}
_EXE_MAGIC = (b"\x7fELF", b"MZ", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"#!", b"PK\x03\x04")


def _kind_for(mime: str) -> str | None:
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    return None


def _event_to_dict(e: ImsgEvent) -> dict:
    """ImsgEvent -> raw dict that parse_event reconstructs exactly (for the restart-safe pending store)."""
    return {"guid": e.guid, "chat_guid": e.chat_guid, "sender": e.sender, "is_from_me": e.is_from_me,
            "is_group": e.is_group, "text": e.text, "created_at": e.created_at,
            "attachments": e.attachments, "reply_to_guid": e.reply_to_guid}


class Relay:
    def __init__(self, imsg: Imsg, backend: Backend, checkpoint: FileCheckpoint, *,
                 spool_dir: str, poll_interval: float = 2.0, reconnect_delay: float = 3.0,
                 sent_ledger: FileCheckpoint | None = None, pending: PendingStore | None = None,
                 attachment_max_wait_s: float = 120.0, attachment_sweep_interval_s: float = 3.0,
                 attachment_max_events: int = 8) -> None:
        self.imsg = imsg
        self.backend = backend
        self.checkpoint = checkpoint
        # Durable at-most-once ledger of outbound ids we've already attempted to send. Guarantees a
        # reclaimed row (ack lost, lease expired, relay crashed) is NEVER sent to a real person twice.
        self.sent_ledger = sent_ledger
        # Restart-safe store of messages whose attachment is still downloading — resolved by a later
        # imsg watch event (imsg 0.13.1 has no message.get) or timed out honestly.
        self.pending = pending
        self.spool_dir = spool_dir
        self.poll_interval = poll_interval
        self.reconnect_delay = reconnect_delay
        self.attachment_max_wait_s = attachment_max_wait_s
        self.attachment_sweep_interval_s = attachment_sweep_interval_s
        self.attachment_max_events = attachment_max_events
        self._stop = asyncio.Event()
        # Serializes the check-checkpoint -> stage -> post -> mark-checkpoint critical section so the
        # watch loop and the pending sweep can NEVER both resolve the same guid (which would double-post
        # -> two conversation turns). Created lazily per running loop (tests drive many short loops).
        self._lock_obj: asyncio.Lock | None = None
        self._lock_loop: object | None = None
        os.makedirs(spool_dir, exist_ok=True)
        try:
            os.chmod(spool_dir, 0o700)             # private spool: owner-only
        except OSError:
            pass

    @staticmethod
    def _now() -> float:
        return time.time()

    def _inbound_lock(self) -> asyncio.Lock:
        """One lock per running loop. In production there is a single persistent loop (run()), so this
        is a real mutex serializing inbound posting; across the many short-lived loops the tests spin up
        it just yields a fresh, uncontended lock — harmless, since a single loop has no concurrency."""
        loop = asyncio.get_running_loop()
        if self._lock_obj is None or self._lock_loop is not loop:
            self._lock_obj = asyncio.Lock()
            self._lock_loop = loop
        return self._lock_obj

    def stop(self) -> None:
        self._stop.set()

    # ---- inbound -----------------------------------------------------------------------------

    async def _stage_attachments(self, event: ImsgEvent, *, ignore_missing: bool = False) -> tuple[list[dict], bool]:
        """Upload the message's attachments. Returns (normalized_attachments, deferred). deferred=True
        means a file is still downloading. ignore_missing=True (sweep re-check) ignores the metadata
        flag and re-stats the real path instead — so a file that landed after the first event resolves.
        Hardened: reject symlinks and non-regular files, realpath the source, cap the count. Logs stay
        content-free (no paths)."""
        out: list[dict] = []
        for a in event.attachments[:MAX_ATTACHMENTS_PER_MESSAGE]:
            if a.get("missing") and not ignore_missing:
                return [], True  # still downloading -> defer the WHOLE message
            path = a.get("original_path") or a.get("converted_path")
            mime = a.get("mime_type") or a.get("converted_mime_type") or ""
            kind = _kind_for(mime)
            if not path or kind is None or mime not in ALLOWED_MIME:
                continue  # unsupported / unresolved -> skip this attachment (never fail the message)
            try:
                lst = os.lstat(path)                          # lstat: see the link itself, don't follow
            except OSError:
                continue
            if stat.S_ISLNK(lst.st_mode):
                continue  # reject symlinks — never follow a link out of Messages' attachment store
            real = os.path.realpath(path)
            if not os.path.isfile(real) or os.path.getsize(real) > MAX_ATTACHMENT_BYTES:
                continue  # reject non-regular files / oversize
            spool = os.path.join(self.spool_dir, hashlib.sha256(real.encode()).hexdigest())
            shutil.copy(real, spool)  # private spool copy
            try:
                with open(spool, "rb") as f:
                    data = f.read()
                if any(data.startswith(sig) for sig in _EXE_MAGIC):
                    continue  # reject executables client-side too
                ref = await self.backend.upload(data, mime, a.get("transfer_name"))
                out.append({"kind": kind, "media_type": mime, "upload_ref": ref})
            finally:
                try:
                    os.remove(spool)                          # delete the spool copy either way
                except OSError:
                    pass
        return out, False

    async def _post_and_checkpoint(self, event: ImsgEvent, atts: list[dict], *,
                                   attachment_unavailable: bool = False) -> str:
        resp = await self.backend.post_inbound({
            "provider_message_id": event.guid,
            "channel_identity": event.sender or "",
            "chat_guid": event.chat_guid,
            "is_group": event.is_group,
            "is_from_me": False,
            "text": event.text,
            "attachments": atts,
            "attachment_unavailable": attachment_unavailable,
            "reply_to_message_id": event.reply_to_guid,
            "timestamp": event.created_at,
        })
        self.checkpoint.mark(event.guid)                    # durable ack -> safe to not reprocess
        log.info("inbound_ok guid=%s status=%s", event.guid, resp.get("status"))
        return resp.get("status", "processed")

    def _timed_out(self, rec: dict, now: float) -> bool:
        return (int(rec.get("events", 1)) >= self.attachment_max_events
                or (now - float(rec.get("first_seen", now))) >= self.attachment_max_wait_s)

    async def process_inbound(self, event: ImsgEvent) -> str:
        if event.is_from_me:
            return "echo"                                   # Bruce's own message
        if not event.guid:
            return "duplicate"
        async with self._inbound_lock():                    # atomic vs. the concurrent pending sweep
            if self.checkpoint.has(event.guid):
                return "duplicate"                          # terminal already -> suppress duplicate events
            try:
                atts, deferred = await self._stage_attachments(event)
                if deferred:
                    # attachment still downloading: DON'T post, DON'T checkpoint, DON'T lose the message.
                    # Record it (restart-safe) and wait for a later watch event / the sweep. Bump the seen
                    # count on a duplicate event for the same guid. Time out honestly if it never resolves.
                    if self.pending is None:                # no store -> single-shot legacy behavior
                        return "deferred"
                    now = self._now()
                    rec = self.pending.upsert(event.guid, _event_to_dict(event), now)
                    if self._timed_out(rec, now):
                        return await self._timeout_terminal(event.guid)
                    log.info("inbound_pending guid=%s events=%s", event.guid, rec.get("events"))
                    return "deferred"
                # resolved (or no attachment): post exactly once + checkpoint, drop any pending record.
                status = await self._post_and_checkpoint(event, atts)
                if self.pending is not None:
                    self.pending.remove(event.guid)
                return status
            except BackendError:
                log.warning("inbound_retry guid=%s", event.guid)   # do NOT checkpoint -> retried
                return "retry"

    async def _timeout_terminal(self, guid: str) -> str:
        """The attachment never resolved: post the ORIGINAL message once, flagged attachment_unavailable,
        checkpoint (so the runtime sends exactly one honest 'resend?' reply), and drop the record."""
        rec = self.pending.get(guid) if self.pending else None
        event = parse_event(rec["event"]) if rec else parse_event({"guid": guid})
        try:
            await self._post_and_checkpoint(event, [], attachment_unavailable=True)
        except BackendError:
            log.warning("inbound_retry guid=%s (timeout post failed)", guid)
            return "retry"                                  # keep the record; try again next sweep
        if self.pending is not None:
            self.pending.remove(guid)
        log.info("inbound_attachment_unavailable guid=%s", guid)
        return "attachment_unavailable"

    async def sweep_pending(self, now: float | None = None) -> None:
        """Re-check every pending record: if the file has now landed (re-stat), post it once; if it has
        timed out, post attachment_unavailable once; otherwise leave it. Runs off the watch hot path."""
        if self.pending is None:
            return
        now = self._now() if now is None else now
        for guid, rec in self.pending.items():
            async with self._inbound_lock():                # atomic vs. a concurrent second watch event
                if self.checkpoint.has(guid):               # already resolved elsewhere -> drop
                    self.pending.remove(guid)
                    continue
                event = parse_event(rec["event"])
                try:
                    atts, _ = await self._stage_attachments(event, ignore_missing=True)
                except BackendError:
                    continue                                # transient upload error -> retry next sweep
                if atts:                                    # file landed -> resolve exactly once
                    try:
                        await self._post_and_checkpoint(event, atts)
                        self.pending.remove(guid)
                        log.info("inbound_resolved guid=%s", guid)
                    except BackendError:
                        continue
                elif self._timed_out(rec, now):
                    await self._timeout_terminal(guid)

    async def run_pending_sweep(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.attachment_sweep_interval_s)
                return                                      # stopping
            except asyncio.TimeoutError:
                pass
            try:
                await self.sweep_pending()
            except Exception:
                log.warning("pending_sweep error")          # never crash the relay on a sweep hiccup

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
        await asyncio.gather(self.run_inbound(), self.run_outbound(), self.run_pending_sweep())
