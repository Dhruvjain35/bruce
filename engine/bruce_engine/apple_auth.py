"""Verify a Sign in with Apple identity token and derive a stable Bruce user.

Apple's identity token is an RS256 JWT signed by keys at ``https://appleid.apple.com/auth/keys``.
We verify: signature (against Apple's live public keys), issuer (``appleid.apple.com``), audience
(our app's client id), expiration, and the NONCE (bound to the client's one-time random value, so a
captured token can't be replayed). The Bruce user_id is derived DETERMINISTICALLY from Apple's
stable ``sub`` — the client never supplies a user id, and the same Apple account always maps to the
same Bruce user.

Minimum identity only: we keep the derived user_id + (if Apple sends it, first authorization only)
an email. We do NOT store the raw Apple token, and the derivation means we don't even need an
apple_sub column — the mapping is reproducible.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid

import jwt
from pydantic import BaseModel

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"

# Fixed namespace for the apple_sub -> Bruce user_id derivation. NEVER change it: it would orphan
# every existing account (a new id for the same Apple user).
_BRUCE_APPLE_NS = uuid.UUID("6f3d1c2a-9b47-5e08-a1d2-3c4b5a6e7f80")


class AppleAuthError(Exception):
    """Apple token verification failed. Carries a short reason, never the token or student data."""


class AppleIdentity(BaseModel):
    apple_sub: str
    email: str | None
    bruce_user_id: uuid.UUID


def allowed_audiences() -> list[str]:
    """Acceptable ``aud`` values — the app's bundle id (native) and/or a Services ID (web). Comma-
    separated in BRUCE_APPLE_CLIENT_ID."""
    return [a.strip() for a in os.environ.get("BRUCE_APPLE_CLIENT_ID", "").split(",") if a.strip()]


def derive_user_id(apple_sub: str) -> uuid.UUID:
    return uuid.uuid5(_BRUCE_APPLE_NS, apple_sub)


def _apple_signing_key(token: str, keys_url: str):
    return jwt.PyJWKClient(keys_url).get_signing_key_from_jwt(token).key


def verify_apple_token(
    identity_token: str,
    raw_nonce: str,
    *,
    audiences: list[str] | None = None,
    key_resolver=None,          # test seam: (token) -> key; default fetches Apple's live JWKS
    keys_url: str = APPLE_KEYS_URL,
) -> AppleIdentity:
    auds = audiences if audiences is not None else allowed_audiences()
    if not auds:
        raise AppleAuthError("BRUCE_APPLE_CLIENT_ID not configured")
    resolve = key_resolver or (lambda t: _apple_signing_key(t, keys_url))

    try:
        key = resolve(identity_token)
    except Exception as exc:  # network / unknown kid
        raise AppleAuthError(f"could not resolve Apple signing key ({type(exc).__name__})") from exc

    try:
        claims = jwt.decode(
            identity_token, key, algorithms=["RS256"], audience=auds, issuer=APPLE_ISSUER,
            options={"require": ["sub", "exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppleAuthError("apple token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AppleAuthError("apple token audience mismatch") from exc
    except jwt.InvalidIssuerError as exc:
        raise AppleAuthError("apple token issuer mismatch") from exc
    except jwt.InvalidTokenError as exc:
        raise AppleAuthError(f"apple token invalid ({type(exc).__name__})") from exc

    # Nonce: Apple echoes the value the client set on the request. The native client sets it to
    # sha256hex(rawNonce), so the token's nonce claim must equal sha256hex(the rawNonce we received).
    expected = hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()
    token_nonce = str(claims.get("nonce") or "")
    if not token_nonce or not hmac.compare_digest(token_nonce, expected):
        raise AppleAuthError("nonce mismatch")

    apple_sub = str(claims["sub"])
    email = claims.get("email")
    # email can be absent on returning sign-ins by design — never require it.
    return AppleIdentity(apple_sub=apple_sub, email=email, bruce_user_id=derive_user_id(apple_sub))
