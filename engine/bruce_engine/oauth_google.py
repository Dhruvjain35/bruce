"""Google OAuth 2.0 authorization-code flow (server-side, PKCE) for Google Calendar.

THE THREAT MODEL, stated plainly, because it drives every decision below:

The callback arrives from the STUDENT'S BROWSER. Every byte of its query string is attacker-
controllable — an attacker can craft a link, replay a captured callback, or swap a `code`. So:

  * Identity is NEVER read from the callback. Not from a `user_id` param, not from a cookie, not
    from the id_token. It is read from the ``oauth_states`` row that the opaque ``state`` points at,
    which was written server-side when an AUTHENTICATED user started the flow. A callback that
    cannot find its state row is rejected — it cannot name whose calendar to connect.
  * ``state`` is single-use (``consumed_at``). A replayed callback must fail, not silently
    re-authorize. Consumption happens BEFORE the code exchange, so even a racing replay loses.
  * ``state`` expires (default 10 minutes). A link left in a browser history is not a standing
    grant.
  * PKCE (S256): the ``code_verifier`` is generated server-side, stored in the state row, and never
    leaves this process. An intercepted authorization code is useless without it.

CREDENTIAL HANDLING:
  * The refresh token is encrypted (bruce_engine.crypto) before it touches a column.
  * Access tokens are fetched on demand and never persisted — short-lived by design; storing them
    only widens the blast radius.
  * client_secret / code / access_token / refresh_token appear in NO log line, NO exception message,
    NO API response, and NO model prompt. Google's token endpoint echoes request params back in
    error bodies, so token-exchange failures surface as status codes only.

Nothing here has run against Google — there are no credentials yet. Every path is exercised through
a mock transport against the real client stack (tests/test_oauth_google.py), and the live test
skips honestly until GOOGLE_* exists.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import secrets
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select

from . import crypto, schema
from .db import user_session

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

PROVIDER = "google_calendar"
# Least privilege: events only. NOT `calendar`, which would let Bruce delete whole calendars — it
# has no reason to, and a scope you don't request is one you can't be exploited through.
SCOPES = ("https://www.googleapis.com/auth/calendar.events",)
STATE_TTL = datetime.timedelta(minutes=10)


class OAuthError(Exception):
    """Base. Messages here reach the user's browser — they carry NO secrets, ever."""

    status_code = 400
    code = "oauth_error"

    def as_detail(self) -> dict:
        return {"error": self.code, "reason": str(self)}


class ConsentDenied(OAuthError):
    code = "consent_denied"


class InvalidState(OAuthError):
    """Unknown/forged/expired/reused state. All collapse to ONE message on purpose: distinguishing
    them would tell an attacker whether a state value exists."""

    status_code = 400
    code = "invalid_state"


class MissingCode(OAuthError):
    code = "missing_code"


class TokenExchangeFailed(OAuthError):
    status_code = 502
    code = "token_exchange_failed"


class InsufficientScope(OAuthError):
    code = "insufficient_scope"


class NotConnected(OAuthError):
    status_code = 409
    code = "not_connected"


class RefreshFailed(OAuthError):
    """The refresh token no longer works — student revoked it, or Google invalidated it."""

    status_code = 401
    code = "refresh_failed"


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def config() -> GoogleOAuthConfig:
    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        raise OAuthError(f"Google OAuth is not configured — missing {', '.join(missing)}")
    return GoogleOAuthConfig(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        redirect_uri=os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/v1/integrations/google/callback"),
    )


def is_configured() -> bool:
    try:
        config()
        return True
    except OAuthError:
        return False


def _pkce_pair() -> tuple[str, str]:
    """(verifier, challenge). S256 — the verifier never leaves the server."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def start_authorization(user_id: UUID, *, redirect_uri: str | None = None) -> str:
    """Begin the connect flow for an AUTHENTICATED user. Returns the URL to send them to.

    The state row is written first, inside the user's RLS context, so the callback can later
    recover WHOSE flow this is without trusting anything the browser sends.
    """
    cfg = config()
    redirect = redirect_uri or cfg.redirect_uri
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(48)

    async with user_session(user_id) as s:
        s.add(
            schema.OAuthState(
                user_id=user_id, provider=PROVIDER, state=state, code_verifier=verifier,
                redirect_uri=redirect, expires_at=_now() + STATE_TTL,
            )
        )
        await s.flush()

    params = {
        "client_id": cfg.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent are what actually yield a refresh token. Without them Google returns
        # only an access token on repeat authorizations and the integration silently dies in an hour.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return str(httpx.URL(AUTH_ENDPOINT, params=params))


async def _consume_state(state: str) -> schema.OAuthState:
    """Atomically claim a state row. Single-use, unexpired — else InvalidState.

    Runs with the OWNER connection deliberately: at this moment we do not yet know which user this
    callback belongs to, so we cannot open an RLS session. The lookup is by an unguessable 48-byte
    random token, and the row it returns is what establishes identity. This is the ONLY place that
    reads across users, and it reads exactly one row by secret.
    """
    from .retention import _owner_conn  # privileged connection helper

    conn = await _owner_conn()
    try:
        # Claim in a single statement: consumed_at IS NULL is the guard, so two concurrent replays
        # cannot both win. Expiry is enforced in the same predicate.
        row = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at = now() "
            "WHERE state = $1 AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING id, user_id, provider, code_verifier, redirect_uri",
            state,
        )
    finally:
        await conn.close()
    if row is None:
        # Unknown, expired, or already used — one message for all three, so probing learns nothing.
        raise InvalidState("this authorization link is invalid, expired, or has already been used")
    return row


async def handle_callback(
    *, state: str | None, code: str | None, error: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> UUID:
    """Complete the flow. Returns the user_id the integration was stored for.

    Order matters: the state is validated and CONSUMED before the code is exchanged, so a replayed
    callback cannot reach Google at all.
    """
    if error:
        # e.g. access_denied when the student clicks Cancel. Not an error condition to alarm about.
        raise ConsentDenied(f"consent was not granted ({error})")
    if not state:
        raise InvalidState("missing state")
    if not code:
        raise MissingCode("authorization code missing from the callback")

    claimed = await _consume_state(state)
    user_id: UUID = claimed["user_id"]
    cfg = config()
    client = http_client or httpx.AsyncClient(timeout=30)

    try:
        r = await client.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "code": code,
                "code_verifier": claimed["code_verifier"],
                "grant_type": "authorization_code",
                "redirect_uri": claimed["redirect_uri"],
            },
        )
        if r.status_code != 200:
            # Status only — the body echoes client_secret and the code back at us.
            raise TokenExchangeFailed(f"Google rejected the authorization code (HTTP {r.status_code})")
        payload = r.json()

        granted = tuple((payload.get("scope") or "").split())
        if not any(s in granted for s in SCOPES):
            raise InsufficientScope(
                "the Google account did not grant calendar access, so Bruce cannot add events"
            )

        refresh = payload.get("refresh_token")
        if not refresh:
            # Google omits it if the user previously consented and prompt=consent was dropped.
            raise TokenExchangeFailed(
                "Google returned no refresh token — Bruce cannot act while the app is closed. "
                "Disconnect the app at myaccount.google.com and reconnect."
            )

        account = None
        try:
            ui = await client.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            if ui.status_code == 200:
                account = ui.json().get("email")
        except Exception:
            account = None  # identity is a nicety; never fail the connect over it

        await _store_integration(
            user_id=user_id, refresh_token=refresh, scopes=list(granted), account=account
        )
        return user_id
    finally:
        if http_client is None:
            await client.aclose()


async def _store_integration(*, user_id: UUID, refresh_token: str, scopes: list[str], account: str | None) -> None:
    """Upsert the integration with the refresh token ENCRYPTED. Never stores plaintext."""
    ciphertext = crypto.encrypt(refresh_token)  # raises if no key — never silently plaintext
    async with user_session(user_id) as s:
        row = (
            await s.execute(
                select(schema.Integration).where(
                    schema.Integration.user_id == user_id, schema.Integration.provider == PROVIDER
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = schema.Integration(user_id=user_id, provider=PROVIDER)
            s.add(row)
        row.refresh_token_encrypted = ciphertext
        row.scopes = scopes
        row.provider_account_id = account
        row.status = "connected"
        row.revoked_at = None
        await s.flush()


async def get_integration(user_id: UUID) -> schema.Integration | None:
    async with user_session(user_id) as s:
        return (
            await s.execute(
                select(schema.Integration).where(
                    schema.Integration.user_id == user_id, schema.Integration.provider == PROVIDER
                )
            )
        ).scalar_one_or_none()


async def access_token_for(user_id: UUID, *, http_client: httpx.AsyncClient | None = None) -> str:
    """Exchange the stored refresh token for a short-lived access token.

    Raises NotConnected (never connected / disconnected) or RefreshFailed (revoked at Google). A
    revoked token marks the integration revoked so the UI can say "reconnect" instead of retrying
    forever against a credential that will never work again.
    """
    row = await get_integration(user_id)
    if row is None or not row.refresh_token_encrypted or row.status != "connected":
        raise NotConnected("Google Calendar is not connected for this account")

    cfg = config()
    refresh = crypto.decrypt(row.refresh_token_encrypted)
    client = http_client or httpx.AsyncClient(timeout=30)
    try:
        r = await client.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code == 400 or r.status_code == 401:
            await _mark_revoked(user_id)
            raise RefreshFailed(
                "Google no longer accepts this connection (it was revoked or expired). Reconnect "
                "Google Calendar in Settings."
            )
        if r.status_code != 200:
            raise TokenExchangeFailed(f"Google token refresh failed (HTTP {r.status_code})")
        return r.json()["access_token"]
    finally:
        if http_client is None:
            await client.aclose()


async def _mark_revoked(user_id: UUID) -> None:
    async with user_session(user_id) as s:
        row = (
            await s.execute(
                select(schema.Integration).where(
                    schema.Integration.user_id == user_id, schema.Integration.provider == PROVIDER
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "revoked"
            row.revoked_at = _now()
            await s.flush()


async def disconnect(user_id: UUID, *, http_client: httpx.AsyncClient | None = None) -> bool:
    """Revoke at Google AND delete the stored credential locally. Idempotent.

    Order is deliberate: we try Google first, but we delete locally EVEN IF that call fails. A
    student who presses Disconnect must never be left with Bruce still holding a usable token
    because a network blip ate the revoke request.
    """
    row = await get_integration(user_id)
    if row is None or not row.refresh_token_encrypted:
        return False

    client = http_client or httpx.AsyncClient(timeout=30)
    try:
        try:
            token = crypto.decrypt(row.refresh_token_encrypted)
            await client.post(REVOKE_ENDPOINT, data={"token": token})
        except Exception:
            pass  # best effort at Google; local deletion below is what actually protects the student
    finally:
        if http_client is None:
            await client.aclose()

    async with user_session(user_id) as s:
        cur = (
            await s.execute(
                select(schema.Integration).where(
                    schema.Integration.user_id == user_id, schema.Integration.provider == PROVIDER
                )
            )
        ).scalar_one_or_none()
        if cur is not None:
            cur.refresh_token_encrypted = None  # the credential is GONE, not merely flagged
            cur.status = "disconnected"
            cur.revoked_at = _now()
            await s.flush()
    return True
