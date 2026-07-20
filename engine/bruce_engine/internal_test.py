"""E1 — the minimal, secure, INTERNAL zero-terminal test surface (Bite 1.5).

The smallest authenticated internal webpage that completes the real live-testing workflow WITHOUT a
terminal, a Cloud Run redeploy, a relay restart, a manual DB edit, or Claude:

    start a staging enrollment  ->  watch privacy-safe live results  ->  end the enrollment

It is NOT a broad owner dashboard and it is NOT a production-approval system. The ONLY grant it can
ever create is a temporary, internal ``StagingTestEnrollment`` (via access_control.enroll_staging_test);
it can NEVER create or touch a ``ProductionAccountEntitlement`` (that is created automatically by
production signup, in D1). Production separation is enforced in code, not by convention.

SECURITY MODEL (server-side, strict — nothing trusts client input):
  * Authentication: an existing Bruce session JWT, carried in a short-lived, HttpOnly + Secure +
    SameSite=Strict cookie set by ``POST /internal/test/login`` (which verifies the JWT signature and
    derives the user from the ``sub`` claim only — the auth boundary rule). No public enrollment path.
  * Authorization: on top of a valid session, the user id MUST be on the server-side internal allowlist
    (``BRUCE_INTERNAL_USER_IDS``). Fail-closed: unset/empty => nobody is internal. A non-internal user
    gets a GENERIC 403 that reveals nothing (no account enumeration; the response is identical whether
    or not any given user exists).
  * CSRF: every state-changing POST (start/end) requires a session-bound HMAC CSRF token in the
    ``X-CSRF-Token`` header, verified server-side with a constant-time compare. A cross-site page can
    neither read the token (SOP) nor forge it (server secret), and SameSite=Strict already withholds
    the session cookie cross-site.
  * Target = SELF ONLY. Every action operates on the authenticated internal user's OWN user id + their
    OWN already-linked messaging identity. No target user id is ever accepted from the client, so there
    is no enumeration surface and RLS confines every content read to the caller's own rows.
  * Audit: start/end each append a content-free ``CapabilityAudit`` row whose actor is the
    SERVER-DERIVED ``internal_web:<user_id>`` — who started/ended a test, never any message content.

PRIVACY: the live-results view exposes ONLY derived, privacy-safe fields per turn (received time,
attachment detected + normalized format, intent, response type, latency, estimated cost, outbound
status, duplicate count, mission/event-candidate booleans, error CATEGORY). It NEVER exposes message
content, attachment contents/paths/filenames, model prompts, chain-of-thought, or full handles
(handles are masked to their last 4 characters).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import html
import json
import logging
import os
from uuid import UUID

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from . import auth, relay_control, schema
from .access_control import (
    InvalidEnvironment,
    current_environment,
    enroll_staging_test,
    revoke_staging_test,
)
from .auth import AuthenticatedUser
from .db import user_session, worker_session

log = logging.getLogger("bruce.internal_test")  # content-free: ids / statuses / counts only, never text

# NOTE: routes are attached with app.add_api_route in register() rather than an APIRouter — the pinned
# FastAPI 0.139 / Starlette 1.3 pair mishandles include_router of a prefixed router (it collapses the
# routes into an unusable mount). Direct registration is the reliable path on this stack.
PREFIX = "/internal/test"

CAPABILITY = "conversation"
SESSION_COOKIE = "bruce_its"          # the short-lived internal-test session (carries the Bruce JWT)
COOKIE_PATH = "/internal/test"        # scope the cookie to the internal surface only
_DEFAULT_SESSION_TTL = 30 * 60        # 30 min; short-lived, re-login to refresh
_STALE_THRESHOLD_S = 120              # supervisor heartbeat older than this => stale

# The chosen-duration -> TTL-seconds map. ``persistent`` means no expiry (revoke to end). This is the
# WHOLE set of allowed durations; anything else is rejected (never trust a client-supplied TTL).
_DURATIONS: dict[str, int | None] = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "persistent": None,
}


# --------------------------------------------------------------------------- authorization (allowlist)


def _internal_user_ids() -> set[UUID]:
    """The server-side internal allowlist from ``BRUCE_INTERNAL_USER_IDS`` (comma/space-separated UUIDs).

    Fail-closed: unset/empty => empty set => NOBODY is internal. A malformed entry is skipped, never a
    silent widening. This is the only authorization source — never a client-supplied flag."""
    raw = os.environ.get("BRUCE_INTERNAL_USER_IDS", "")
    out: set[UUID] = set()
    for tok in raw.replace(",", " ").split():
        try:
            out.add(UUID(tok.strip()))
        except ValueError:
            continue
    return out


def is_internal_user(user_id: UUID) -> bool:
    """True iff ``user_id`` is on the server-side internal allowlist (fail-closed)."""
    return user_id in _internal_user_ids()


# --------------------------------------------------------------------------- session cookie + CSRF


def _session_ttl() -> int:
    try:
        return int(os.environ.get("BRUCE_INTERNAL_SESSION_TTL_SECONDS", _DEFAULT_SESSION_TTL))
    except ValueError:
        return _DEFAULT_SESSION_TTL


def _csrf_key() -> bytes:
    """Server secret for the session-bound CSRF HMAC. Prefers a dedicated key; falls back to the JWT
    secret (always set in any environment that can authenticate) with a domain separator."""
    key = os.environ.get("BRUCE_INTERNAL_CSRF_KEY") or os.environ.get("BRUCE_JWT_SECRET") or ""
    return ("e1-internal-test-csrf::" + key).encode("utf-8")


def _csrf_for_session(session_token: str) -> str:
    """A CSRF token cryptographically bound to THIS session token. Stateless (no CSRF cookie needed):
    the server recomputes it from the session cookie on every POST. An attacker cannot read it (SOP)
    nor forge it (server secret)."""
    return hmac.new(_csrf_key(), session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def _set_session_cookie(response, token: str) -> None:
    """Set the short-lived internal-test session cookie: HttpOnly + Secure + SameSite=Strict, path-scoped
    to the internal surface, with a bounded Max-Age."""
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=_session_ttl(), path=COOKIE_PATH,
        httponly=True, secure=True, samesite="strict",
    )


def _decoded_session_user(request: Request) -> AuthenticatedUser | None:
    """Derive the user from the session COOKIE's verified JWT, or None. user_id comes only from the
    verified ``sub`` — never from any other request input."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        claims = auth._decode(token)
        return AuthenticatedUser(user_id=UUID(str(claims["sub"])), auth_provider=str(claims.get("iss") or "supabase"))
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


def _require_internal(request: Request) -> AuthenticatedUser:
    """Gate: a valid session cookie AND membership of the internal allowlist. 401 when unauthenticated;
    a GENERIC 403 (no enumeration) when authenticated-but-not-internal."""
    user = _decoded_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if not is_internal_user(user.user_id):
        # Deliberately generic: identical whether or not the user exists / what they are.
        raise HTTPException(status_code=403, detail="forbidden")
    return user


def _require_csrf(request: Request) -> None:
    """Verify the session-bound CSRF token on a state-changing POST (constant-time). The session cookie
    is guaranteed present here because _require_internal runs first."""
    token = request.cookies.get(SESSION_COOKIE) or ""
    provided = request.headers.get("X-CSRF-Token", "")
    expected = _csrf_for_session(token)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="invalid or missing CSRF token")


# --------------------------------------------------------------------------- magic-link browser sign-in
# The founder must never paste a JWT, use curl, open dev tools, or run Terminal. An authorized operator
# mints a SHORT-LIVED magic link (scripts/internal_magic_link.py); the founder just opens it in the
# browser. The magic token is a limited, short-TTL, e1_magic-scoped JWT that is EXCHANGED for a fresh
# HttpOnly session cookie — it is never usable as a general API session, and the URL-borne token expires
# quickly. Access still requires membership of the internal allowlist.

MAGIC_DEFAULT_TTL = 600   # seconds


def mint_magic_link_token(user_id: UUID, *, ttl_seconds: int = MAGIC_DEFAULT_TTL) -> str:
    """Mint the short-lived magic token (operator side — needs BRUCE_JWT_SECRET). e1_magic-scoped so it
    can only establish an internal-test session, never act as a general bearer."""
    secret = os.environ.get("BRUCE_JWT_SECRET")
    if not secret:
        raise RuntimeError("BRUCE_JWT_SECRET is not set")
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    payload = {"sub": str(user_id), "iat": now, "exp": now + int(ttl_seconds), "e1_magic": True}
    aud = os.environ.get("BRUCE_JWT_AUDIENCE")
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, secret, algorithm="HS256")


def _verify_magic_token(token: str) -> UUID | None:
    """Verify a magic token (signature + exp + the e1_magic scope). Returns the user id or None (generic
    failure — no distinction between bad/expired/wrong-scope, so nothing is enumerable)."""
    if not token:
        return None
    secret = os.environ.get("BRUCE_JWT_SECRET") or ""
    aud = os.environ.get("BRUCE_JWT_AUDIENCE") or None
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience=aud,
                            options={"require": ["exp"]})
        if not claims.get("e1_magic"):
            return None
        return UUID(str(claims["sub"]))
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


# --------------------------------------------------------------------------- privacy-safe helpers


def _mask_handle(handle: str | None) -> str | None:
    """Mask a channel handle to its last 4 characters (never the full phone/email). ``None`` stays None."""
    if not handle:
        return None
    tail = handle[-4:]
    return "•••" + tail


def _error_category(error: str | None) -> str | None:
    """The TYPE/category prefix of an error only (e.g. ``ProviderUnavailable``), never the message body.
    Bruce stores errors as ``Type: detail``; we keep only the token before the first colon."""
    if not error:
        return None
    return error.split(":", 1)[0].strip()[:64] or None


# --------------------------------------------------------------------------- readiness


async def _linked_identities(user_id: UUID) -> list[dict]:
    """The caller's OWN active linked messaging identities (masked). Read under user_session so RLS
    confines it to self — there is no way to see, or enumerate, anyone else's identities."""
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.MessagingIdentity).where(
            schema.MessagingIdentity.user_id == user_id,
            schema.MessagingIdentity.disconnected_at.is_(None),
            schema.MessagingIdentity.blocked_at.is_(None),
        ).order_by(schema.MessagingIdentity.created_at))).scalars().all()
    return [{"identity_id": str(r.id), "channel": r.channel, "handle_masked": _mask_handle(r.channel_identity)}
            for r in rows]


async def _current_enrollment(user_id: UUID, env: str) -> dict | None:
    """The caller's current LIVE staging enrollment for (self, conversation, env), if any (worker read)."""
    now = _utcnow()
    async with worker_session() as s:
        rows = (await s.execute(select(schema.StagingTestEnrollment).where(
            schema.StagingTestEnrollment.user_id == user_id,
            schema.StagingTestEnrollment.capability == CAPABILITY,
            schema.StagingTestEnrollment.environment == env,
            schema.StagingTestEnrollment.revoked_at.is_(None),
        ).order_by(schema.StagingTestEnrollment.enabled_at.desc()))).scalars().all()
    for r in rows:
        if r.expires_at is None or r.expires_at > now:
            return {
                "enrollment_id": str(r.id),
                "enabled_at": _iso(r.enabled_at),
                "expires_at": _iso(r.expires_at),
                "persistent": r.expires_at is None,
            }
    return None


async def _readiness(user_id: UUID) -> dict:
    """Assemble the content-free readiness snapshot. Each probe is isolated so one failure degrades that
    one field rather than 500-ing the whole screen."""
    try:
        env = current_environment()
    except InvalidEnvironment:
        env = "invalid"

    out: dict = {"environment": env, "deployment_version": os.environ.get("BRUCE_COMMIT", "unknown")}

    # API health (this process; never touches a dependency).
    out["api"] = {"status": "ok", "commit": os.environ.get("BRUCE_COMMIT", "unknown"), "env": env}

    # Database readiness.
    try:
        async with user_session(user_id) as s:
            await s.execute(select(func.now()))
        out["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — report degraded, never leak the exception body
        out["database"] = f"unavailable ({type(exc).__name__})"

    # Model readiness — a configured flag, NOT a live billed call.
    has_model = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("FEATHERLESS_API_KEY"))
    out["model"] = "configured" if has_model else "not_configured"

    # Relay heartbeat / supervisor status / pinned agent commit.
    try:
        devices = await relay_control.list_devices()
        stale = {d.id for d in await relay_control.stale_devices(_STALE_THRESHOLD_S)}
        now = _utcnow()
        dev_out = []
        for d in devices:
            age = None if d.supervisor_seen_at is None else int((now - d.supervisor_seen_at).total_seconds())
            dev_out.append({
                "name": d.name,
                "directive": d.directive,
                "outbound_paused": d.outbound_paused,
                "supervisor_seen_age_s": age,
                "pinned_agent_commit": d.agent_commit,
                "stale": d.id in stale,
                "revoked": d.revoked_at is not None,
            })
        active = [d for d in devices if d.revoked_at is None]
        out["relay"] = {
            "device_count": len(devices),
            "active_device_count": len(active),
            "stale_device_count": len(stale),
            "supervisor_status": "no_devices" if not active else ("degraded" if stale else "healthy"),
            "devices": dev_out,
        }
    except Exception as exc:  # noqa: BLE001
        out["relay"] = {"status": f"unavailable ({type(exc).__name__})"}

    # Global kill states: relay outbound pause AND the conversation-capability kill for this env.
    try:
        paused, reason = await relay_control.global_state()
        out["relay_outbound_paused"] = {"paused": paused, "reason": reason}
    except Exception as exc:  # noqa: BLE001
        out["relay_outbound_paused"] = {"status": f"unavailable ({type(exc).__name__})"}

    try:
        async with worker_session() as s:
            gs = (await s.execute(select(schema.CapabilityGlobalState).where(
                schema.CapabilityGlobalState.capability == CAPABILITY,
                schema.CapabilityGlobalState.environment == env))).scalar_one_or_none()
            out["capability_killed"] = bool(gs is not None and gs.killed)
            # Outbound queue depth by status (content-free counts).
            rows = (await s.execute(select(
                schema.OutboundMessageRow.status, func.count()).group_by(
                schema.OutboundMessageRow.status))).all()
        by_status = {status: n for status, n in rows}
        out["outbound_queue"] = {"total": sum(by_status.values()), "by_status": by_status}
    except Exception as exc:  # noqa: BLE001
        out["capability_killed"] = None
        out["outbound_queue"] = {"status": f"unavailable ({type(exc).__name__})"}

    out["linked_identities"] = await _linked_identities(user_id)
    out["current_enrollment"] = await _current_enrollment(user_id, env)
    out["can_start"] = bool(out["linked_identities"])  # a linked staging identity is required to test
    return out


# --------------------------------------------------------------------------- privacy-safe live results


async def _live_results(user_id: UUID, *, limit: int = 25) -> dict:
    """Per-turn, PRIVACY-SAFE derived rows for the caller's OWN staging conversation, newest first.

    Read entirely under user_session(self): RLS guarantees only the caller's rows are visible, and we
    project ONLY derived fields — never turn text / attachment contents / decision JSONB (prompt/CoT) /
    full handles / attachment paths."""
    async with user_session(user_id) as s:
        user_turns = (await s.execute(select(schema.ConversationTurn).where(
            schema.ConversationTurn.user_id == user_id,
            schema.ConversationTurn.role == "user",
        ).order_by(schema.ConversationTurn.created_at.desc()).limit(limit))).scalars().all()

        # Assistant turns keyed by (channel, provider_message_id) for pairing.
        assistants = (await s.execute(select(schema.ConversationTurn).where(
            schema.ConversationTurn.user_id == user_id,
            schema.ConversationTurn.role == "assistant"))).scalars().all()
        by_key: dict[tuple[str, str], schema.ConversationTurn] = {
            (a.channel, a.provider_message_id): a for a in assistants}

        results = []
        for u in user_turns:
            a = by_key.get((u.channel, u.provider_message_id))

            # Attachment detection + NORMALIZED FORMAT only (kind/media_type — never filename/url/path).
            att_detected, att_format = False, None
            inbound = (await s.execute(select(schema.InboundMessageRow).where(
                schema.InboundMessageRow.user_id == user_id,
                schema.InboundMessageRow.channel == u.channel,
                schema.InboundMessageRow.provider_message_id == u.provider_message_id,
            ))).scalar_one_or_none()
            if inbound is not None:
                atts = (await s.execute(select(schema.MessageAttachment).where(
                    schema.MessageAttachment.inbound_message_id == inbound.id))).scalars().all()
                if atts:
                    att_detected = True
                    first = atts[0]
                    att_format = first.media_type or first.kind  # normalized format, NOT the bytes/path

            # Duplicate suppression count: extra user turns sharing this provider_message_id (dedup => 0).
            dup = (await s.execute(select(func.count()).select_from(schema.ConversationTurn).where(
                schema.ConversationTurn.user_id == user_id,
                schema.ConversationTurn.channel == u.channel,
                schema.ConversationTurn.provider_message_id == u.provider_message_id,
                schema.ConversationTurn.role == "user"))).scalar_one() - 1

            # Latency proxy: time from the inbound user turn to its assistant reply.
            latency_ms = None
            if a is not None and a.created_at is not None and u.created_at is not None:
                latency_ms = max(0, int((a.created_at - u.created_at).total_seconds() * 1000))

            # Cost + outbound status linked via the reply's mission (when there is one).
            cost_usd, outbound_status, error_category = None, None, None
            mission_id = a.mission_id if a is not None else None
            if mission_id is not None:
                cost = (await s.execute(select(func.coalesce(func.sum(schema.ModelCost.cost_usd), 0.0)).where(
                    schema.ModelCost.user_id == user_id,
                    schema.ModelCost.mission_id == mission_id))).scalar_one()
                cost_usd = float(cost) if cost else None
                ob = (await s.execute(select(schema.OutboundMessageRow).where(
                    schema.OutboundMessageRow.user_id == user_id,
                    schema.OutboundMessageRow.mission_id == mission_id,
                ).order_by(schema.OutboundMessageRow.created_at.desc()))).scalars().first()
                if ob is not None:
                    outbound_status = ob.status
                mrow = (await s.execute(select(schema.Mission).where(
                    schema.Mission.id == mission_id))).scalar_one_or_none()
                if mrow is not None:
                    error_category = _error_category(mrow.error)

            results.append({
                "turn_id": str(u.id),
                "received_at": _iso(u.created_at),
                "channel": u.channel,
                "handle_masked": _mask_handle(u.channel_identity),
                "attachment_detected": att_detected,
                "attachment_format": att_format,
                "intent": a.intent if a is not None else None,
                "response_type": a.response_type if a is not None else None,
                "risk_level": a.risk_level if a is not None else None,
                "model_latency_ms": latency_ms,
                "estimated_cost_usd": cost_usd,
                "outbound_status": outbound_status,
                "duplicate_count": max(0, dup),
                "mission_created": mission_id is not None,
                "event_candidate_created": bool(a is not None and a.event_candidate_id is not None),
                "error_category": error_category,
                "awaiting_reply": a is None,
            })
    return {"count": len(results), "turns": results}


# --------------------------------------------------------------------------- small utils


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


# --------------------------------------------------------------------------- endpoints


async def login(request: Request) -> JSONResponse:
    """Bootstrap the browser session from an existing Bruce JWT.

    The token may be supplied in the JSON body ``{"token": ...}`` or the ``Authorization: Bearer``
    header. Its signature is verified; the user is derived from the ``sub`` claim ONLY. Internal ->
    set the short-lived Secure/HttpOnly/SameSite=Strict session cookie. Non-internal -> generic 403
    (no enumeration). Invalid/missing token -> generic 401. Creates NO DB state."""
    token = None
    authz = request.headers.get("Authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
    if not token:
        try:
            body = await request.json()
            if isinstance(body, dict):
                token = body.get("token")
        except (json.JSONDecodeError, ValueError):
            token = None
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")

    try:
        claims = auth._decode(token)
        user_id = UUID(str(claims["sub"]))
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="invalid token")

    if not is_internal_user(user_id):
        raise HTTPException(status_code=403, detail="forbidden")

    resp = JSONResponse({"ok": True, "environment": _safe_env(), "next": COOKIE_PATH})
    _set_session_cookie(resp, token)
    log.info("internal_test_login user=%s env=%s", user_id, _safe_env())
    return resp


async def magic_auth(request: Request) -> HTMLResponse:
    """Browser sign-in via an operator-minted magic link: verify the short-lived e1_magic token, confirm
    the user is internal, then EXCHANGE it for a fresh HttpOnly/Secure/SameSite session cookie and
    redirect into the app. The founder never pastes a token / uses Terminal. Invalid/expired/non-internal
    -> a GENERIC denied page (no enumeration; identical regardless of why)."""
    uid = _verify_magic_token(request.query_params.get("t", ""))
    if uid is None or not is_internal_user(uid):
        return HTMLResponse(_denied_html(), status_code=403)
    session = auth.mint_bruce_jwt(uid, ttl_seconds=_session_ttl())   # fresh, normal session cookie
    resp = RedirectResponse(COOKIE_PATH, status_code=303)
    _set_session_cookie(resp, session)
    log.info("internal_test_magic_auth user=%s env=%s", uid, _safe_env())   # content-free
    return resp


async def logout(request: Request) -> JSONResponse:
    """Clear the internal-test session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    return resp


async def readiness(request: Request) -> JSONResponse:
    """Content-free readiness snapshot for the internal test surface (internal session required)."""
    user = _require_internal(request)
    return JSONResponse(await _readiness(user.user_id))


async def results(request: Request) -> JSONResponse:
    """Privacy-safe live results for the caller's OWN staging conversation (internal session required)."""
    user = _require_internal(request)
    return JSONResponse(await _live_results(user.user_id))


async def start_test(request: Request) -> JSONResponse:
    """Start a staging test: create a temporary StagingTestEnrollment for SELF + capability=conversation
    with the chosen TTL. CSRF-protected, internal-only, audited. NEVER touches production."""
    user = _require_internal(request)
    _require_csrf(request)

    duration_key = "persistent"
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("duration"):
            duration_key = str(body["duration"])
    except (json.JSONDecodeError, ValueError):
        pass
    if duration_key not in _DURATIONS:
        raise HTTPException(status_code=400, detail="invalid duration")

    # A linked staging identity is required — we enroll an ALREADY-LINKED user (self), never create one.
    identities = await _linked_identities(user.user_id)
    if not identities:
        raise HTTPException(status_code=409, detail="no linked staging identity")

    ttl = _DURATIONS[duration_key]
    duration = datetime.timedelta(seconds=ttl) if ttl is not None else None
    actor = f"internal_web:{user.user_id}"
    expires_at = await enroll_staging_test(
        user.user_id, duration=duration, reason="e1_internal_test", capability=CAPABILITY, actor=actor)
    log.info("internal_test_start user=%s duration=%s env=%s", user.user_id, duration_key, _safe_env())
    return JSONResponse({
        "ok": True, "duration": duration_key,
        "expires_at": _iso(expires_at), "persistent": expires_at is None,
    })


async def end_test(request: Request) -> JSONResponse:
    """End the staging test: immediately revoke every live enrollment for SELF. CSRF-protected,
    internal-only, audited. Production access (if any) is persistent and untouched."""
    user = _require_internal(request)
    _require_csrf(request)
    actor = f"internal_web:{user.user_id}"
    n = await revoke_staging_test(user.user_id, capability=CAPABILITY, actor=actor)
    log.info("internal_test_end user=%s revoked=%s env=%s", user.user_id, n, _safe_env())
    return JSONResponse({"ok": True, "revoked": n})


async def page(request: Request) -> HTMLResponse:
    """The internal test page. Unauthenticated/non-internal -> the login view (a token field). Internal
    -> the readiness + controls + live-results view, with the session-bound CSRF token embedded."""
    user = _decoded_session_user(request)
    if user is None or not is_internal_user(user.user_id):
        return HTMLResponse(_login_html())
    csrf = _csrf_for_session(request.cookies.get(SESSION_COOKIE, ""))
    return HTMLResponse(_page_html(user.user_id, csrf))


def register(app) -> None:
    """Attach the E1 internal-test routes to ``app``. Uses ``add_api_route`` directly (see the PREFIX
    note above — include_router of a prefixed APIRouter is unreliable on the pinned FastAPI/Starlette)."""
    app.add_api_route(f"{PREFIX}/login", login, methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{PREFIX}/auth", magic_auth, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route(f"{PREFIX}/logout", logout, methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{PREFIX}/readiness", readiness, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{PREFIX}/results", results, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{PREFIX}/start", start_test, methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{PREFIX}/end", end_test, methods=["POST"], include_in_schema=False)
    app.add_api_route(PREFIX, page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route(f"{PREFIX}/", page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)


def _safe_env() -> str:
    try:
        return current_environment()
    except InvalidEnvironment:
        return "invalid"


# --------------------------------------------------------------------------- inline HTML (no CDNs)

_STYLE = """
:root{color-scheme:light dark;--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--bd:#30363d;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--bg);color:var(--fg)}
.wrap{max-width:960px;margin:0 auto;padding:24px}
h1{font-size:18px;margin:0 0 2px}.sub{color:var(--mut);margin:0 0 18px}
.env{display:inline-block;padding:2px 10px;border:1px solid var(--acc);border-radius:12px;color:var(--acc);
font-weight:700;text-transform:uppercase;letter-spacing:.05em;font-size:12px}
.envbanner{margin:-24px -24px 18px;padding:8px 24px;background:var(--warn);color:#0d1117;font-weight:700;
text-transform:uppercase;letter-spacing:.05em;font-size:12px}.envbanner b{text-transform:uppercase}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin:14px 0}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kv{border:1px solid var(--bd);border-radius:8px;padding:8px 10px}
.kv .k{color:var(--mut);font-size:11px;text-transform:uppercase}.kv .v{font-size:15px;font-weight:600;word-break:break-word}
.pill{padding:1px 8px;border-radius:10px;font-size:12px;font-weight:700}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button{font:inherit;font-weight:700;padding:8px 14px;border-radius:8px;border:1px solid var(--bd);
background:#21262d;color:var(--fg);cursor:pointer}button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);color:#04101f;border-color:var(--acc)}
button.danger{border-color:var(--bad);color:var(--bad)}
select,input{font:inherit;padding:8px;border-radius:8px;border:1px solid var(--bd);background:#0d1117;color:var(--fg)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;overflow-x:auto;display:block}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);font-size:12px;white-space:nowrap}
th{color:var(--mut);text-transform:uppercase;font-size:10px}
.note{color:var(--mut);font-size:12px;margin-top:8px}
#msg{min-height:18px;font-weight:700}
.scroll{overflow-x:auto}
"""


def _env_banner() -> str:
    env = html.escape(_safe_env())
    return f'<div class="envbanner">environment: <b>{env}</b></div>'


def _login_html() -> str:
    """The founder-facing landing: NO raw-token paste. Sign-in is via the secure single-use link an
    operator generates; the founder just opens it. (The JWT `POST /login` still exists as an internal
    implementation detail, but the browser experience never asks the founder to paste a token.)"""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bruce — Internal Test (sign in)</title><style>{_STYLE}</style></head><body><div class="wrap">
{_env_banner()}
<h1>Bruce · Internal Test Surface</h1><p class="sub">Authorized internal users only.</p>
<div class="card"><h2>Sign in</h2>
<p class="note">Open the secure sign-in link your operator generated for you (it expires shortly). No
token to copy, no Terminal — just open the link in this browser and you'll land here signed in.</p>
</div></div></body></html>"""


def _denied_html() -> str:
    """Generic not-authorized page (no enumeration — identical for an invalid/expired link and a
    non-internal user)."""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bruce — Internal Test</title><style>{_STYLE}</style></head><body><div class="wrap">
{_env_banner()}
<h1>Bruce · Internal Test Surface</h1>
<div class="card"><h2>Not authorized</h2>
<p class="note">This sign-in link is invalid or has expired. Ask your operator for a fresh link.</p>
</div></div></body></html>"""


def _page_html(user_id: UUID, csrf: str) -> str:
    env = html.escape(_safe_env())
    csrf_js = html.escape(csrf)
    uid = html.escape(str(user_id))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bruce — Internal Test Surface</title><style>{_STYLE}</style></head><body><div class="wrap">
<div class="row" style="justify-content:space-between">
  <div><h1>Bruce · Internal Test Surface</h1>
  <p class="sub">Staging live-test — zero terminal. Signed in as <code>{uid}</code>.</p></div>
  <div style="text-align:right"><span class="env">{env}</span><br>
  <button onclick="logout()" style="margin-top:8px">Sign out</button></div>
</div>

<div class="card"><h2>Readiness</h2><div id="ready" class="grid"><div class="kv"><div class="v">loading…</div></div></div>
<div id="relay" class="scroll"></div></div>

<div class="card"><h2>Test controls</h2>
<div class="row">
  <label>Duration
    <select id="dur"><option value="15m">15 min</option><option value="30m">30 min</option>
    <option value="1h">1 hour</option><option value="persistent">Persistent (until ended)</option></select>
  </label>
  <button class="pri" onclick="startTest()">Start test</button>
  <button class="danger" onclick="endTest()">End test</button>
  <span id="msg"></span>
</div>
<p class="note">Start creates a temporary staging enrollment for your linked identity (capability
<code>conversation</code>). It never grants production and never needs a redeploy, relay restart, DB
edit, Claude, or Terminal. End revokes it immediately.</p></div>

<div class="card"><h2>Live results (privacy-safe)</h2>
<p class="note">Derived fields only — never message content, attachment contents/paths, prompts,
chain-of-thought, or full handles.</p>
<div class="scroll"><table id="results"><thead><tr>
<th>received</th><th>handle</th><th>att</th><th>format</th><th>intent</th><th>response</th>
<th>latency</th><th>cost</th><th>outbound</th><th>dupes</th><th>mission</th><th>event</th><th>error</th>
</tr></thead><tbody><tr><td colspan="13">no turns yet</td></tr></tbody></table></div></div>

<script>
const CSRF="{csrf_js}",BASE="{COOKIE_PATH}";
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
function cls(v){{return v==='ok'||v==='healthy'||v==='configured'?'ok':(v==='degraded'||v==='not_configured'?'warn':'bad');}}
async function post(p,b){{return fetch(BASE+p,{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},
  credentials:'same-origin',body:JSON.stringify(b||{{}})}});}}
async function logout(){{await post('/logout');location.href=BASE;}}
async function startTest(){{const d=document.getElementById('dur').value;const m=document.getElementById('msg');
  const r=await post('/start',{{duration:d}});const j=await r.json().catch(()=>({{}}));
  m.className=r.ok?'ok':'bad';m.textContent=r.ok?('started ('+d+')'+(j.expires_at?' → '+j.expires_at:'')):('start failed: '+(j.detail||r.status));refresh();}}
async function endTest(){{const m=document.getElementById('msg');const r=await post('/end');const j=await r.json().catch(()=>({{}}));
  m.className=r.ok?'ok':'bad';m.textContent=r.ok?('ended (revoked '+j.revoked+')'):('end failed: '+(j.detail||r.status));refresh();}}
function kv(k,v,c){{return '<div class="kv"><div class="k">'+esc(k)+'</div><div class="v '+(c||'')+'">'+esc(v)+'</div></div>';}}
async function refresh(){{
  const rd=await (await fetch(BASE+'/readiness',{{credentials:'same-origin'}})).json().catch(()=>null);
  if(rd){{
    const enr=rd.current_enrollment;
    let h=kv('database',rd.database,cls(rd.database))+kv('model',rd.model,cls(rd.model))
      +kv('deployment',rd.deployment_version)
      +kv('capability killed',rd.capability_killed?'YES':'no',rd.capability_killed?'bad':'ok')
      +kv('outbound queue',(rd.outbound_queue&&rd.outbound_queue.total!=null)?rd.outbound_queue.total:'—')
      +kv('relay supervisor',rd.relay?rd.relay.supervisor_status:'—',rd.relay?cls(rd.relay.supervisor_status):'')
      +kv('relay outbound',rd.relay_outbound_paused&&rd.relay_outbound_paused.paused?'PAUSED':'run',rd.relay_outbound_paused&&rd.relay_outbound_paused.paused?'bad':'ok')
      +kv('enrollment',enr?(enr.persistent?'persistent':('until '+enr.expires_at)):'none',enr?'ok':'warn');
    document.getElementById('ready').innerHTML=h;
    let rows=(rd.relay&&rd.relay.devices||[]).map(d=>'<tr><td>'+esc(d.name)+'</td><td>'+esc(d.directive)+'</td><td>'
      +(d.supervisor_seen_age_s==null?'never':d.supervisor_seen_age_s+'s')+'</td><td>'+esc(d.pinned_agent_commit||'—')
      +'</td><td class="'+(d.stale?'bad':'ok')+'">'+(d.stale?'stale':'live')+'</td></tr>').join('');
    document.getElementById('relay').innerHTML=rows?('<table><thead><tr><th>device</th><th>directive</th><th>seen</th><th>pinned commit</th><th>state</th></tr></thead><tbody>'+rows+'</tbody></table>'):'';
  }}
  const lr=await (await fetch(BASE+'/results',{{credentials:'same-origin'}})).json().catch(()=>null);
  if(lr){{const tb=document.querySelector('#results tbody');
    tb.innerHTML=lr.turns.length?lr.turns.map(t=>'<tr><td>'+esc(t.received_at)+'</td><td>'+esc(t.handle_masked)
      +'</td><td>'+(t.attachment_detected?'yes':'—')+'</td><td>'+esc(t.attachment_format||'—')+'</td><td>'+esc(t.intent||'—')
      +'</td><td>'+esc(t.response_type||'—')+'</td><td>'+(t.model_latency_ms==null?'—':t.model_latency_ms+'ms')
      +'</td><td>'+(t.estimated_cost_usd==null?'—':'$'+t.estimated_cost_usd.toFixed(4))+'</td><td>'+esc(t.outbound_status||'—')
      +'</td><td>'+t.duplicate_count+'</td><td>'+(t.mission_created?'yes':'—')+'</td><td>'+(t.event_candidate_created?'yes':'—')
      +'</td><td class="'+(t.error_category?'bad':'')+'">'+esc(t.error_category||'—')+'</td></tr>').join('')
      :'<tr><td colspan="13">no turns yet</td></tr>';}}
}}
refresh();setInterval(refresh,4000);
</script></div></body></html>"""
