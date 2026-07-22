"""ReplyContextEnvelope + the relay-side exact-reference enrichment builder (A3.1).

When an inbound message explicitly references an earlier one (reply_to / thread_originator guid), the
relay resolves ONLY that exact referenced message from the local chat.db, stages ONLY its explicitly
joined attachment into the relay spool, and sends a bounded, PATH-FREE envelope to the server. It never
scans or uploads conversation history and never places a local filesystem path in the envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chatdb import ChatDb, ChatDbUnavailable

RELAY_EXACT_LOOKUP = "relay_exact_lookup"
UNRESOLVED = "unresolved"


@dataclass
class ReplyAttachmentRef:
    """A referenced attachment as it crosses to the server: NEVER a local path/filename. Either staged
    bytes (available, via upload_ref) or an honest unavailable reason."""
    mime_type: str | None
    total_bytes: int | None
    available: bool
    upload_ref: str | None = None            # staged-bytes handle when available; never a path
    unavailable_reason: str | None = None    # not_downloaded | stage_failed


@dataclass
class ReplyContextEnvelope:
    current_message_guid: str
    referenced_message_guid: str | None
    referenced_chat_guid: str | None
    relationship_type: str                   # reply_to | thread_root | unresolved
    referenced_direction: str | None         # inbound | outbound
    referenced_sent_at: str | None
    referenced_service: str | None
    referenced_edited: bool
    referenced_unsent: bool
    referenced_attachment_refs: list[ReplyAttachmentRef] = field(default_factory=list)
    resolution_source: str = UNRESOLVED
    resolution_confidence: float = 0.0
    unavailable_reason: str | None = None

    def to_wire(self) -> dict:
        """Serialize for POST to the server. Asserts no path/filename leaked into the payload."""
        atts = [{"mime_type": a.mime_type, "total_bytes": a.total_bytes, "available": a.available,
                 "upload_ref": a.upload_ref, "unavailable_reason": a.unavailable_reason}
                for a in self.referenced_attachment_refs]
        return {
            "current_message_guid": self.current_message_guid,
            "referenced_message_guid": self.referenced_message_guid,
            "referenced_chat_guid": self.referenced_chat_guid,
            "relationship_type": self.relationship_type,
            "referenced_direction": self.referenced_direction,
            "referenced_sent_at": self.referenced_sent_at,
            "referenced_service": self.referenced_service,
            "referenced_edited": self.referenced_edited,
            "referenced_unsent": self.referenced_unsent,
            "referenced_attachment_refs": atts,
            "resolution_source": self.resolution_source,
            "resolution_confidence": self.resolution_confidence,
            "unavailable_reason": self.unavailable_reason,
        }


def _unresolved(current_guid: str, referenced_guid: str | None, relationship_type: str,
                reason: str) -> ReplyContextEnvelope:
    return ReplyContextEnvelope(
        current_message_guid=current_guid, referenced_message_guid=referenced_guid,
        referenced_chat_guid=None, relationship_type=relationship_type, referenced_direction=None,
        referenced_sent_at=None, referenced_service=None, referenced_edited=False,
        referenced_unsent=False, resolution_source=UNRESOLVED, resolution_confidence=0.0,
        unavailable_reason=reason)


async def build_reply_envelope(chatdb: ChatDb, *, current_guid: str, referenced_guid: str | None,
                               relationship_type: str, stage_fn) -> ReplyContextEnvelope:
    """Resolve the EXACT referenced message from chat.db and stage ONLY its joined attachment.

    ``stage_fn`` is an async (local_path, mime_type, transfer_name) -> upload_ref | None (the relay's
    backend.upload). A missing target, an unreadable DB, or a still-downloading attachment each degrade to
    an honest envelope — Bruce never claims to see content it did not resolve.
    """
    if not referenced_guid:
        return _unresolved(current_guid, None, relationship_type, "no_reference")
    try:
        row = chatdb.get_message_by_guid(referenced_guid)
    except ChatDbUnavailable as exc:
        return _unresolved(current_guid, referenced_guid, relationship_type, str(exc))
    if row is None:
        return _unresolved(current_guid, referenced_guid, relationship_type, "target_missing")

    refs: list[ReplyAttachmentRef] = []
    try:
        atts = chatdb.get_attachments_for_message_guid(referenced_guid)
    except ChatDbUnavailable:
        atts = []
    for a in atts:
        if a.downloaded:
            try:
                ref = await stage_fn(a.local_path, a.mime_type, a.transfer_name)
            except Exception:
                ref = None
            if ref:
                refs.append(ReplyAttachmentRef(mime_type=a.mime_type, total_bytes=a.total_bytes,
                                               available=True, upload_ref=ref))
            else:
                refs.append(ReplyAttachmentRef(mime_type=a.mime_type, total_bytes=a.total_bytes,
                                               available=False, unavailable_reason="stage_failed"))
        else:
            refs.append(ReplyAttachmentRef(mime_type=a.mime_type, total_bytes=a.total_bytes,
                                           available=False, unavailable_reason="not_downloaded"))

    return ReplyContextEnvelope(
        current_message_guid=current_guid, referenced_message_guid=referenced_guid,
        referenced_chat_guid=row.chat_guid, relationship_type=relationship_type,
        referenced_direction="outbound" if row.is_from_me else "inbound",
        referenced_sent_at=row.sent_at, referenced_service=row.service,
        referenced_edited=row.edited, referenced_unsent=row.unsent,
        referenced_attachment_refs=refs, resolution_source=RELAY_EXACT_LOOKUP, resolution_confidence=1.0)
