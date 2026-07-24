"""Gmail CapabilityExecutor (Phase G) — Gmail's plug into the SAME general AgentRun loop calendar uses.

This is what "inherited hand" means concretely: there is NO Gmail loop, NO Gmail approval flow, NO Gmail
response engine. A send is one CapabilityExecutor (exactly like CalendarMutationExecutor) that, on execute,
delegates to the already-verified gmail_adapter.send_and_verify — provider write + read-back proof — and
returns its ToolResult unchanged. The loop owns the state machine, the durable AgentRun, and validation; the
executor owns only the provider call. Adding reply is a `kind`, not a new runtime branch.

Exactly-once: a send is NOT idempotent-by-read-back the way a calendar upsert is (Google assigns the id), so
the executor is constructed with the caller's idempotency_key (run_id:stepN for a mission; a stable request
id for a foreground action) and hands it to the adapter, whose marker ledger collapses a retry to the one
already-sent message. Verification is the adapter's read-back, never fabricated here.
"""

from __future__ import annotations

from uuid import UUID

from . import gmail_adapter
from .runtime_contracts import ActionType, NextAction, Risk, ToolResult

_PROVIDER = "gmail"


class GmailSendExecutor:
    """Drives a send OR a reply-in-thread through the verified gmail_adapter. `adapter` is an injection seam
    (FakeGmailAdapter in tests); when None the real Google adapter is built for this user."""

    domain = "gmail"

    def __init__(self, *, to: str, subject: str, body: str, idempotency_key: str, kind: str = "send",
                 thread_id: str | None = None, adapter=None) -> None:
        if kind not in ("send", "reply"):
            raise ValueError(f"unsupported gmail kind: {kind}")
        if not idempotency_key:
            # a send with no exactly-once key is a duplicate-mail risk — fail loudly, never silently.
            raise ValueError("gmail send requires an idempotency_key")
        self.kind = kind
        self.to = to
        self.subject = subject
        self.body = body
        self.thread_id = thread_id
        self._key = idempotency_key
        self._adapter = adapter
        self._is_reply = kind == "reply"
        self.capability = "gmail.reply_to_thread" if self._is_reply else "gmail.send_message"
        self._operation = "reply_to_thread" if self._is_reply else "send_message"

    def goal(self) -> dict:
        # provider-neutral, secret-free summary the AgentRun/world model can read back
        return {"action": self.kind, "to": self.to, "subject": self.subject,
                "thread_id": self.thread_id, "desired_outcome": f"email {self.to}"}

    def build_action(self) -> NextAction:
        return NextAction(
            type=ActionType.call_tool, capability=self.capability, provider=_PROVIDER,
            operation=self._operation, target_entity_id=self.thread_id,
            arguments={"to": self.to, "subject": self.subject, "body": self.body, "thread_id": self.thread_id},
            verification_method="provider_readback", risk=Risk.high, reversible=False)

    async def execute(self, user_id: UUID) -> ToolResult:
        adapter = self._adapter or gmail_adapter.real_adapter(user_id)
        return await gmail_adapter.send_and_verify(
            adapter, user_id, to=self.to, subject=self.subject, body=self.body,
            idempotency_key=self._key, thread_id=self.thread_id)
