"""The execution gate — the one place a provider write is permitted, and the backstop that proves it.

There are exactly five functions in this engine that mutate a provider:

    calendar_adapter.execute_and_verify   -> adapter.insert
    calendar_adapter.undo                 -> adapter.delete
    calendar_tools.update_event           -> adapter.update
    calendar_tools.delete_event           -> adapter.delete
    gmail_adapter.send_and_verify         -> adapter.send

Every one of them now calls `require()` immediately before touching the adapter. That is the whole
mechanism. It is small on purpose: a boundary you can enumerate is a boundary you can actually verify,
and the enumeration is asserted by a test that greps the engine for raw adapter mutations, so a sixth one
added later fails the suite instead of quietly becoming a hole.

TWO LAYERS, DIFFERENT JOBS.

`open_authorization()` is the gate proper. An orchestrator resolves the exact operation and arguments,
presents its AuthorizationEvidence, and gets a typed verdict. A denial is an ordinary outcome: the student
gets an honest reply, nothing executes.

`require()` is the backstop. It asks a narrower question — is there, right now, an OPEN authorization for
precisely this operation and these arguments — and raises if not. It exists because a gate at the
orchestration layer only protects the paths that go through the orchestrator, and the failure mode worth
defending against is a future path that does not. Reaching a provider with no authorization open is not a
user-facing condition to report; it is a defect, so it raises rather than returning.

WHY A CONTEXTVAR AND NOT A PARAMETER. Threading an authorization through insert/update/delete/send and
every executor, planner and runner between them would touch about seventy call sites, and the ones most
likely to be added later are the ones most likely to forget. The token here is opened and closed around a
single dispatch by `open_authorization`, and `require` re-derives the fingerprint from the arguments it
was actually called with — so an open token permits exactly one operation with exactly one set of
arguments, not "anything that happens while we are inside an authorized turn".
"""

from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from . import authorization_evidence as ae

log = logging.getLogger("bruce.execution_gate")   # CONTENT-FREE: ids, operations, verdicts

_KILL_ON = {"1", "true", "yes", "on"}


def armed() -> bool:
    """Whether the backstop refuses unauthorized provider writes. BRUCE_AUTHZ_OFF disarms it.

    The switch exists because a safety boundary that cannot be turned off in an incident is one that gets
    reverted in an incident, and a revert loses the audit trail with it. Disarming still evaluates and
    still logs every verdict, so the hole is measured while it is open.
    """
    return os.environ.get("BRUCE_AUTHZ_OFF", "").strip().lower() not in _KILL_ON


def unchecked() -> bool:
    """True only inside `unchecked_provider_writes_for_test`. Callers use it to skip authorization-shaped
    preconditions that a provider-semantics test has no way to satisfy."""
    return _UNCHECKED.get()


class UnauthorizedExecution(RuntimeError):
    """A provider mutation was reached with no open authorization covering it. Always a defect."""


@dataclass(frozen=True)
class _Token:
    user_id: UUID
    provider: str
    operation: str
    fingerprint: str
    authorization_id: str
    attempt_key: str | None


# Opened and closed around ONE dispatch. Never set at request scope, never inherited by a background task
# that outlives the turn: a contextvar copied into a task that runs later would be exactly the stale
# authorization this module exists to prevent.
_OPEN: contextvars.ContextVar[_Token | None] = contextvars.ContextVar("bruce_authz_open", default=None)

# See `unchecked_provider_writes_for_test`. Physically unreachable outside pytest.
_UNCHECKED: contextvars.ContextVar[bool] = contextvars.ContextVar("bruce_authz_unchecked", default=False)


@dataclass(frozen=True)
class Verdict:
    """The gate's answer. `reason` is one of `authorization_evidence`'s typed denial constants."""
    allowed: bool
    reason: str
    authorization_id: str | None = None
    destructive: bool = False

    @property
    def denied(self) -> bool:
        return not self.allowed


def evaluate(evidence: ae.AuthorizationEvidence | None, *, user_id: UUID, provider: str, operation: str,
             arguments: dict | None, conversation_id: str | None = None,
             attempt_key: str | None = None, now: datetime | None = None) -> Verdict:
    """Pure verdict — no side effects, no context opened. Used by the gate, the evaluation harness, and
    anything that wants to know whether a write WOULD be permitted without performing it."""
    if _UNCHECKED.get():
        return Verdict(allowed=True, reason=ae.ALLOW, destructive=ae.is_destructive(provider, operation))
    reason = ae.check(evidence, user_id=user_id, provider=provider, operation=operation,
                      arguments=arguments, conversation_id=conversation_id,
                      attempt_key=attempt_key, now=now)
    return Verdict(allowed=reason == ae.ALLOW, reason=reason,
                   authorization_id=evidence.authorization_id if evidence else None,
                   destructive=ae.is_destructive(provider, operation))


@contextmanager
def open_authorization(evidence: ae.AuthorizationEvidence | None, *, user_id: UUID, provider: str,
                       operation: str, arguments: dict | None, conversation_id: str | None = None,
                       attempt_key: str | None = None, now: datetime | None = None):
    """Permit exactly one operation with exactly these arguments for the duration of the block.

    Yields a `Verdict`. On a denial nothing is opened, so a caller that ignores the verdict and executes
    anyway is stopped by `require()` at the adapter rather than succeeding — the two layers are
    independent on purpose, and neither is trusted to be the only one that ran.
    """
    v = evaluate(evidence, user_id=user_id, provider=provider, operation=operation, arguments=arguments,
                 conversation_id=conversation_id, attempt_key=attempt_key, now=now)
    if v.denied:
        log.warning("authz_denied op=%s.%s reason=%s destructive=%s", provider, operation, v.reason,
                    v.destructive)
        yield v
        return

    normalized = ae.normalize_arguments(provider, operation, arguments)
    token = _Token(user_id=user_id, provider=provider, operation=operation,
                   fingerprint=ae.fingerprint(normalized),
                   authorization_id=v.authorization_id or "", attempt_key=attempt_key)
    reset = _OPEN.set(token)
    try:
        yield v
    finally:
        _OPEN.reset(reset)


def require(user_id: UUID, *, provider: str, operation: str, arguments: dict | None) -> None:
    """The backstop, called immediately before every raw adapter mutation.

    Deliberately re-derives the fingerprint from the arguments the adapter is ACTUALLY about to send,
    rather than trusting the ones the orchestrator presented. If a layer in between rewrote a recipient,
    a time, or a subject after the authorization was checked, the fingerprints diverge and the write stops
    here — which is the only place that particular substitution can still be caught.
    """
    if _UNCHECKED.get():
        return
    token = _OPEN.get()
    normalized = ae.normalize_arguments(provider, operation, arguments)
    fp = ae.fingerprint(normalized)

    if token is None:
        reason = ae.NO_AUTHORIZATION
    elif token.user_id != user_id:
        reason = ae.WRONG_USER
    elif token.provider != provider:
        reason = ae.PROVIDER_MISMATCH
    elif token.operation != operation:
        reason = ae.OPERATION_MISMATCH
    elif token.fingerprint != fp:
        reason = ae.ARGUMENTS_CHANGED
    else:
        return

    destructive = ae.is_destructive(provider, operation)
    if not armed():
        log.error("authz_backstop_disarmed op=%s.%s reason=%s destructive=%s — WRITE ALLOWED THROUGH",
                  provider, operation, reason, destructive)
        return
    log.error("authz_backstop_blocked op=%s.%s reason=%s destructive=%s", provider, operation, reason,
              destructive)
    raise UnauthorizedExecution(f"{provider}.{operation}: {reason}")


# --- canonical argument shapes ---------------------------------------------------------------------------
# The orchestrator that presents an authorization and the adapter that spends it MUST describe the same
# operation the same way, or every write would fail as ARGUMENTS_CHANGED. One builder per operation, used
# by both ends, is what keeps that true without either side knowing about the other.
#
# What belongs in here is what the student would care about being different. A calendar update carries the
# target and the new time; it does not carry the calendar id, because moving the same event on the same
# account is the same act. A send carries the body, because an email whose text was rewritten after
# approval is not the email that was approved.


def calendar_create_args(*, title: str, start: str, end: str | None, timezone: str | None,
                         location: str | None = None) -> dict:
    return {"title": title, "start": start, "end": end, "timezone": timezone, "location": location}


def calendar_update_args(*, provider_event_id: str, new_start: str, new_end: str | None,
                         new_timezone: str | None) -> dict:
    return {"provider_event_id": provider_event_id, "new_start": new_start, "new_end": new_end,
            "new_timezone": new_timezone}


def calendar_delete_args(*, provider_event_id: str) -> dict:
    return {"provider_event_id": provider_event_id}


def gmail_send_args(*, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    return {"to": to, "subject": subject, "body": body, "thread_id": thread_id}


# --- test seam -----------------------------------------------------------------------------------------


@contextmanager
def granted_for_test(user_id: UUID, *, provider: str, operation: str, arguments: dict | None):
    """Open a real authorization for a test that is exercising provider I/O, not consent.

    Named so it is greppable and so its presence in a file is a visible claim: "this file executes
    provider writes without going through the production authorization path". The evidence it mints is
    genuine and passes every rule — there is no bypass here, only a stated grantor. Tests of the boundary
    itself must not use it, and `test_authorization_zero_call.py` does not.
    """
    ev = ae.grant(user_id=user_id, provider=provider, operation=operation, arguments=arguments,
                  authorization_type=ae.AuthorizationType.direct_explicit,
                  boundary=_test_boundary(), trusted_message_id="test-trusted-message",
                  source_message_timestamp=datetime.now(timezone.utc),
                  explicit_operation_request=True)
    with open_authorization(ev, user_id=user_id, provider=provider, operation=operation,
                            arguments=arguments) as v:
        if v.denied:                                   # a test grant that does not pass its own rules is a bug
            raise AssertionError(f"granted_for_test produced a denied verdict: {v.reason}")
        yield v


def _test_boundary():
    from . import user_action_boundary as uab
    return uab.evaluate("yes do it", has_pending_decision=True)


@contextmanager
def unchecked_provider_writes_for_test():
    """Suspend the gate for a test that is exercising PROVIDER semantics, not consent.

    About twenty-five existing tests call `execute_and_verify` / `send_and_verify` / `update_event` /
    `delete_event` directly to prove read-back verification, 409 handling, idempotent retry and account
    binding. Those are tests of the provider contract; making each of them mint an authorization would add
    ceremony to twenty-five assertions that are not about authorization, and would tempt the next person
    to add a real bypass.

    So this is the one and only suspension, and it is unreachable outside a test run: without pytest in
    the environment it raises rather than yielding. That is a runtime property, not a naming convention —
    a production import of this function fails on the first call, in every environment, always.

    Files that use it are declaring "this file does not exercise the execution boundary". The files that
    DO exercise it — `test_authorization_evidence.py`, `test_authorization_zero_call.py` — must never
    import it, and a test in the zero-call suite asserts they do not.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("unchecked_provider_writes_for_test is reachable only under pytest")
    reset = _UNCHECKED.set(True)
    try:
        yield
    finally:
        _UNCHECKED.reset(reset)
