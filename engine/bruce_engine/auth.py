"""Authentication boundary — derive the user from a VERIFIED JWT, never from client input.

The user_id is the token's `sub` claim. This module NEVER reads user_id from a request body,
query param, or client-supplied header — that is the whole point of the boundary.

Configure verification via env (Supabase-compatible; plug in the real project later):
  BRUCE_JWT_SECRET    HS256 shared secret (Supabase legacy JWT secret), OR
  BRUCE_JWKS_URL      JWKS endpoint for RS256/ES256 (Supabase .well-known/jwks.json)
  BRUCE_JWT_AUDIENCE  expected `aud` (Supabase uses "authenticated"); optional

For local dev/tests, set BRUCE_JWT_SECRET and mint HS256 tokens. Nothing here depends on a
live Supabase project — wiring the real IdP is only setting these env vars.
"""

from __future__ import annotations

import os
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

_bearer = HTTPBearer(auto_error=False)


class AuthenticatedUser(BaseModel):
    user_id: UUID
    session_id: UUID | None = None
    auth_provider: str = "supabase"


def _decode(token: str) -> dict:
    """Verify signature + claims and return the payload. Raises jwt.InvalidTokenError on failure."""
    audience = os.environ.get("BRUCE_JWT_AUDIENCE") or None
    jwks_url = os.environ.get("BRUCE_JWKS_URL")
    secret = os.environ.get("BRUCE_JWT_SECRET")
    options = {"require": ["sub", "exp"]}

    if jwks_url:
        signing_key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
        return jwt.decode(token, signing_key, algorithms=["RS256", "ES256"], audience=audience, options=options)
    if secret:
        return jwt.decode(token, secret, algorithms=["HS256"], audience=audience, options=options)
    # Fail closed: refuse to authenticate if no verification is configured.
    raise jwt.InvalidTokenError("no JWT verification configured (set BRUCE_JWT_SECRET or BRUCE_JWKS_URL)")


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedUser:
    """FastAPI dependency: the authenticated user, or 401. user_id comes from the verified token only."""
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = _decode(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")

    try:
        user_id = UUID(str(claims["sub"]))
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="token missing a valid subject")

    session_raw = claims.get("session_id")
    session_id: UUID | None = None
    if session_raw:
        try:
            session_id = UUID(str(session_raw))
        except ValueError:
            session_id = None

    return AuthenticatedUser(
        user_id=user_id,
        session_id=session_id,
        auth_provider=str(claims.get("iss") or "supabase"),
    )
