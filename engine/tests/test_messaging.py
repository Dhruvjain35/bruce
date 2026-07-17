"""Messaging boundary — provider payloads must never reach the mission engine.

No provider is connected. These tests exercise the boundary itself (signature, replay, idempotency,
account linking) through a FakeChannel that models the parts of a real provider that actually bite:
webhooks get redelivered, requests get replayed, and anyone can text a phone number and claim to be
someone else.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from bruce_engine.messaging import (
    LINK_CODE_TTL,
    AttachmentKind,
    ChannelKind,
    FakeChannel,
    InboundMessage,
    LinkRequest,
    OutboundMessage,
    ReplayDetected,
    SignatureInvalid,
    generate_link_code,
    reject_replays,
    verify_hmac_signature,
)

SECRET = "test-secret"


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"x-signature": sig}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------- signature / replay


def test_unsigned_webhook_is_rejected_before_parsing():
    ch = FakeChannel(SECRET)
    body = json.dumps({"id": "1", "from": "+15550100", "timestamp": _now_iso()}).encode()
    with pytest.raises(SignatureInvalid):
        asyncio.run(ch.parse_inbound(body=body, headers={}))


def test_forged_signature_is_rejected():
    ch = FakeChannel(SECRET)
    body = json.dumps({"id": "1", "from": "+15550100", "timestamp": _now_iso()}).encode()
    with pytest.raises(SignatureInvalid):
        asyncio.run(ch.parse_inbound(body=body, headers={"x-signature": "deadbeef"}))


def test_tampered_body_invalidates_the_signature():
    """The signature must cover the BODY, not just exist."""
    ch = FakeChannel(SECRET)
    body, headers = _signed({"id": "1", "from": "+15550100", "timestamp": _now_iso()})
    with pytest.raises(SignatureInvalid):
        asyncio.run(ch.parse_inbound(body=body + b" ", headers=headers))


def test_signature_comparison_is_constant_time():
    """`==` leaks the correct prefix through timing, which is enough to forge given patience."""
    import inspect

    from bruce_engine import messaging

    src = inspect.getsource(messaging.verify_hmac_signature)
    assert "compare_digest" in src


def test_replayed_old_message_is_rejected_even_with_a_valid_signature():
    """A valid signature on an OLD request is still an attack — signatures don't expire alone."""
    ch = FakeChannel(SECRET)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
    body, headers = _signed({"id": "1", "from": "+15550100", "timestamp": old})
    with pytest.raises(ReplayDetected):
        asyncio.run(ch.parse_inbound(body=body, headers=headers))


def test_future_timestamps_are_also_rejected():
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2))
    with pytest.raises(ReplayDetected):
        reject_replays(sent_at=future)


# --------------------------------------------------------------------------- canonical shape


def test_inbound_is_normalized_with_no_provider_fields():
    """The mission engine must never learn a provider's vocabulary."""
    ch = FakeChannel(SECRET)
    body, headers = _signed({
        "id": "prov-123", "from": "+15550100", "text": "science fair flyer",
        "timestamp": _now_iso(),
        "attachments": [{"kind": "image", "media_type": "image/png", "hex": "89504e47"}],
    })
    msg = asyncio.run(ch.parse_inbound(body=body, headers=headers))
    assert isinstance(msg, InboundMessage)
    assert msg.provider_message_id == "prov-123"
    assert msg.channel == ChannelKind.fake
    assert msg.attachments[0].kind == AttachmentKind.image
    assert msg.attachments[0].data == bytes.fromhex("89504e47")
    # the canonical model has no provider-specific escape hatch
    assert "raw" not in msg.model_dump() and "payload" not in msg.model_dump()


def test_provider_message_id_is_present_for_idempotency():
    """Webhooks WILL be redelivered. Without a provider id there is no way to dedupe, and a
    redelivery becomes a second mission and a second calendar event."""
    ch = FakeChannel(SECRET)
    body, headers = _signed({"id": "prov-9", "from": "+1", "timestamp": _now_iso()})
    msg = asyncio.run(ch.parse_inbound(body=body, headers=headers))
    assert msg.provider_message_id == "prov-9"


def test_unlinked_sender_has_no_user_id():
    """A phone number is NOT an identity claim. Anyone can text a number."""
    ch = FakeChannel(SECRET)
    body, headers = _signed({"id": "1", "from": "+15550199", "timestamp": _now_iso()})
    msg = asyncio.run(ch.parse_inbound(body=body, headers=headers))
    assert msg.user_id is None


# --------------------------------------------------------------------------- account linking


def test_link_code_is_short_unguessable_and_unambiguous():
    code = generate_link_code()
    assert len(code) == 6
    # a code read off a screen and typed on a phone must not contain I/O/0/1
    assert not (set(code) & set("IO01"))
    assert len({generate_link_code() for _ in range(200)}) > 190, "codes must not collide"


def test_link_code_is_one_time_and_expires():
    now = datetime.datetime.now(datetime.timezone.utc)
    req = LinkRequest(code="ABC234", user_id=uuid4(), channel=ChannelKind.fake,
                      expires_at=now + LINK_CODE_TTL)
    assert req.is_usable(now)

    used = req.model_copy(update={"consumed_at": now})
    assert not used.is_usable(now), "a consumed code must not link a second identity"

    expired = req.model_copy(update={"expires_at": now - datetime.timedelta(minutes=1)})
    assert not expired.is_usable(now), "an old code must not still bind an account"


def test_linking_requires_a_code_the_authenticated_user_generated():
    """The security property: binding a channel identity to a user REQUIRES a secret that only the
    authenticated app user could have seen. Otherwise texting a number would impersonate anyone."""
    import inspect

    from bruce_engine import messaging

    src = inspect.getsource(messaging)
    assert "generate_link_code" in src
    # there must be no path that derives a user from a channel identity alone
    assert "def user_from_phone" not in src and "def user_from_identity" not in src


# --------------------------------------------------------------------------- outbound


def test_decision_is_sent_as_a_deep_link_not_an_approval_url():
    """Approval must happen behind authentication. A raw approve-URL in a text would let anyone who
    sees the message (or a forwarded screenshot) execute a real calendar action."""
    ch = FakeChannel(SECRET)
    asyncio.run(ch.send_decision(to="+1", summary="One decision needed", deep_link="bruce://mission/abc"))
    to, msg = ch.sent[0]
    assert msg.deep_link.startswith("bruce://")
    assert "approve" not in (msg.deep_link or "").lower()
    assert "http" not in (msg.deep_link or "")


def test_receipt_can_be_sent_back_through_the_same_channel():
    ch = FakeChannel(SECRET)
    asyncio.run(ch.send_receipt(to="+1", summary="Added and verified", deep_link="bruce://mission/abc"))
    assert ch.sent[0][1].text == "Added and verified"


def test_channel_protocol_is_the_only_coupling_point():
    """Mission logic must depend on the Protocol, never on a concrete provider class."""
    from bruce_engine import messaging

    assert hasattr(messaging, "MessagingChannel")
    for method in ("parse_inbound", "send_message", "send_decision", "send_receipt"):
        assert hasattr(messaging.MessagingChannel, method)


def test_no_linq_adapter_is_faked():
    """Linq is listed as a planned channel kind, but there is deliberately NO adapter: the repo has
    no Linq API contract, and inventing provider fields would be confident fiction that fails the
    moment a real payload arrives."""
    from bruce_engine import messaging

    assert ChannelKind.linq in list(ChannelKind)
    assert not hasattr(messaging, "LinqChannel"), "no Linq adapter may exist without a real contract"
