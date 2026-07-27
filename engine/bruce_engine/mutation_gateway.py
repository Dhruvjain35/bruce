"""MutationGateway — the one door to a provider mutation.

#120 gated the five functions in this engine that touch a provider. That worked and it is not an
architecture: five enumerated `require()` calls are five things that must each stay correct, and the
sixth connector — files, Canvas, whatever comes next — arrives on a day when nobody is thinking about
authorization. An enumeration defends against the mistakes you already know about.

So the shape changes. Orchestrators no longer authorize anything. They ask this gateway to perform an
operation, and it is the only code in the engine that opens an authorization. Everything a mutation needs
to be legitimate happens here, once, in one order:

    reload the authorization by id   ->  recheck all ten conditions against the world as it is now
    ->  open the authorization for exactly this operation and these arguments
    ->  perform the provider call    ->  consume on success, with the receipt
    ->  audit

`execution_gate.require()` stays exactly where it is, at the adapter. The two are not redundant: this
gateway decides, and `require` verifies that what actually reached the socket is what was decided. A
layer in between that rewrote a recipient after the decision is caught there and nowhere else.

WHAT MAKES THIS ENFORCEABLE RATHER THAN A CONVENTION. `test_mutation_gateway.py` asserts that
`execution_gate.open_authorization` is called from exactly one production module — this one. A future
connector that writes straight from an orchestrator does not fail in review or in production; it fails
in the suite, on the commit that introduces it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from uuid import UUID

from . import authorization_evidence as ae
from . import authorization_store, execution_gate

log = logging.getLogger("bruce.mutation_gateway")   # CONTENT-FREE: ids, operations, verdicts


@dataclass(frozen=True)
class GatewayResult:
    """What the gateway did. `performed` is the only honest source of "did this actually happen" — a
    caller must never infer it from the absence of an error, which is how a denial becomes a reply that
    says done."""
    performed: bool
    reason: str                       # ae.ALLOW, or exactly one typed denial
    authorization_id: str | None = None
    value: object = None              # whatever `perform` returned, untouched
    destructive: bool = False

    @property
    def denied(self) -> bool:
        return not self.performed


async def execute(user_id: UUID, *, authorization_id: str | None, provider: str, operation: str,
                  arguments: dict | None, perform: Callable[[], Awaitable],
                  conversation_id: str | None = None, attempt_key: str | None = None,
                  receipt_of: Callable[[object], str | None] | None = None,
                  revalidate_grant: bool = True, now: datetime | None = None) -> GatewayResult:
    """Perform one provider mutation, or refuse it and say precisely why.

    `authorization_id` — never an evidence object and never a boolean. A caller holding an id has to go
    through the store, which means a background mission and a foreground turn are held to the same
    standard: the record is reloaded and rejudged at the moment of execution, not at the moment of
    planning.

    `perform` is the provider call itself, unchanged. The gateway does not know how to talk to Google;
    it knows who is allowed to. Verification stays where it already is — inside the verified I/O that
    `perform` wraps — because a gateway that reported success on the strength of a write would undo the
    one guarantee this codebase has held from the beginning.
    """
    if execution_gate.unchecked():
        # A declared provider-semantics test (see `execution_gate.unchecked_provider_writes_for_test`,
        # which raises outside pytest). Handled HERE rather than at each caller so there is one
        # suspension point rather than one per orchestrator — the same reason this module exists.
        return GatewayResult(True, ae.ALLOW, value=await perform(),
                             destructive=ae.is_destructive(provider, operation))

    ev, reason = await authorization_store.recheck(
        user_id, authorization_id, provider=provider, operation=operation, arguments=arguments,
        conversation_id=conversation_id, attempt_key=attempt_key,
        revalidate_grant=revalidate_grant, now=now)
    destructive = ae.is_destructive(provider, operation)

    if reason != ae.ALLOW:
        log.warning("mutation_denied op=%s.%s reason=%s destructive=%s", provider, operation, reason,
                    destructive)
        return GatewayResult(False, reason, authorization_id=authorization_id, destructive=destructive)

    # The ONLY place in the engine that opens an authorization. Held open for exactly one call.
    with execution_gate.open_authorization(ev, user_id=user_id, provider=provider, operation=operation,
                                           arguments=arguments, conversation_id=conversation_id,
                                           attempt_key=attempt_key, now=now) as verdict:
        if verdict.denied:
            # The store said yes and the pure gate said no. They read the same record, so a disagreement
            # is a defect in one of them; refusing is the only safe way to be wrong here.
            log.error("mutation_gate_disagreement op=%s.%s store=allow gate=%s", provider, operation,
                      verdict.reason)
            return GatewayResult(False, verdict.reason, authorization_id=authorization_id,
                                 destructive=destructive)
        value = await perform()

    # Consumed only AFTER the call returns. Marking it first would turn a transport failure into a
    # permanently unusable authorization and strand work the student genuinely agreed to.
    if attempt_key:
        try:
            await authorization_store.mark_consumed(
                user_id, ev.authorization_id, attempt_key=attempt_key,
                receipt_id=receipt_of(value) if receipt_of else None)
        except Exception:
            # Audit is best-effort and must never precede or undo the real operation. An unconsumed
            # authorization is retryable by the same attempt and refused for any other, which is the
            # safe direction to fail.
            log.info("authz_consume_failed id=%s", ev.authorization_id)

    log.info("mutation_performed op=%s.%s authz=%s destructive=%s", provider, operation,
             ev.authorization_id, destructive)
    return GatewayResult(True, ae.ALLOW, authorization_id=ev.authorization_id, value=value,
                         destructive=destructive)


async def execute_with_evidence(user_id: UUID, evidence: ae.AuthorizationEvidence | None, *,
                                provider: str, operation: str, arguments: dict | None,
                                perform: Callable[[], Awaitable], conversation_id: str | None = None,
                                attempt_key: str | None = None, mission_id: UUID | None = None,
                                receipt_of: Callable[[object], str | None] | None = None,
                                now: datetime | None = None) -> GatewayResult:
    """Persist a freshly minted authorization, then go through the front door like everything else.

    The foreground convenience: a handler has just minted evidence from the student's trusted words and
    would otherwise have to write it, hold the id, and call `execute`. It does exactly that — including
    the reload — rather than short-circuiting to the in-memory object, so there is ONE execution path and
    the durable one is not a special case that only background code exercises.
    """
    if execution_gate.unchecked():
        # Checked before the None guard, not after: a declared provider-semantics test passes no evidence
        # at all, and refusing it here would make the suspension useless on exactly the paths that need it.
        return GatewayResult(True, ae.ALLOW, value=await perform(),
                             destructive=ae.is_destructive(provider, operation))
    if evidence is None:
        return GatewayResult(False, ae.NO_AUTHORIZATION,
                             destructive=ae.is_destructive(provider, operation))
    try:
        await authorization_store.record(evidence, mission_id=mission_id)
    except Exception:
        # A store outage must not silently downgrade to unauthorized execution, and must not strand a
        # student mid-turn either. Refusing is the only answer that is both safe and honest.
        log.exception("authz_record_failed op=%s.%s", provider, operation)
        return GatewayResult(False, "authorization_not_persisted",
                             destructive=ae.is_destructive(provider, operation))
    # `revalidate_grant=False`: this is a FOREGROUND turn, and the coarse access grant was checked at
    # intake one call ago. The wake path (`execute` with an id and nothing else) leaves it True, because
    # a mission has no intake gate and an entitlement revoked while it slept must stop it.
    return await execute(user_id, authorization_id=evidence.authorization_id, provider=provider,
                         operation=operation, arguments=arguments, perform=perform,
                         conversation_id=conversation_id, attempt_key=attempt_key,
                         receipt_of=receipt_of, revalidate_grant=False, now=now)
