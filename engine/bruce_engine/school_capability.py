"""Provider capability matrix for the SchoolConnector framework (P1 primitive 2).

A student's school data lives behind wildly uneven surfaces: one district exposes a full Canvas API,
the next only a read-only ICS feed, the next nothing but a screenshot. So the FIRST thing any connector
must answer honestly is "can you actually answer X?" — before a caller builds a query on top of a
capability the provider does not have.

This module is that honest answer. A ``ProviderCapabilityMatrix`` maps every ``SchoolCapability`` to a
``CapabilityState`` (+ a plain-language reason). A connector NEVER fabricates a value for a capability
it lacks: it declares ``unsupported`` and the caller (query layer, sync) sees an explicit gap, not a
confident zero. This mirrors the messaging boundary's rule — no provider field, and no provider
*limitation*, is ever laundered into a fake success below the adapter.

Nothing here is provider-specific. Canvas (and any future Google Classroom / OneRoster / ICS adapter)
imports this and fills in its own matrix; the query layer imports this to ask before it answers.
"""

from __future__ import annotations

import dataclasses
import enum


class SchoolCapability(str, enum.Enum):
    """Every question a caller may put to a SchoolConnector. A provider declares a state for each.

    Kept flat and outcome-shaped (what a student wants to know) rather than mirroring any one
    provider's endpoint list — so the same enum describes Canvas, an ICS feed, or a screenshot import.
    """

    # course graph
    institution = "institution"
    terms = "terms"
    list_courses = "list_courses"
    instructors = "instructors"
    sections = "sections"
    # assignments + the honest due-state buckets the student actually asks for
    list_assignments = "list_assignments"
    assignment_detail = "assignment_detail"
    due_date_range = "due_date_range"
    upcoming = "upcoming"
    overdue = "overdue"
    undated = "undated"
    unsubmitted = "unsubmitted"
    graded = "graded"
    submission_state = "submission_state"
    # everything else a course carries
    materials = "materials"
    announcements = "announcements"
    grades = "grades"
    rubrics = "rubrics"
    feedback = "feedback"
    schedule_events = "schedule_events"
    # cross-cutting guarantees a caller depends on
    original_urls = "original_urls"        # every object links back to its real provider URL
    sync_cursors = "sync_cursors"          # restart-safe incremental sync
    change_detection = "change_detection"  # classify created / updated / deleted


class CapabilityState(str, enum.Enum):
    """How well a provider can answer a capability. ``unknown`` is honest too — it is NOT ``supported``.

    * ``supported``   — the provider answers this fully and reliably.
    * ``limited``     — the provider answers, but with a caveat the caller must respect (partial data,
                        coarse granularity, stale window). The ``reason`` says exactly how.
    * ``unsupported`` — the provider cannot answer at all. Data is ``None``; never a fabricated value.
    * ``unknown``     — not yet probed / not asserted. Treated as "do not rely on it" (fail closed).
    """

    supported = "supported"
    limited = "limited"
    unsupported = "unsupported"
    unknown = "unknown"


@dataclasses.dataclass(frozen=True)
class CapabilityDeclaration:
    """One provider's honest stance on one capability: the state + a plain-language reason."""

    capability: SchoolCapability
    state: CapabilityState
    reason: str | None = None  # required in spirit for limited/unsupported — say WHY, not just that

    @property
    def usable(self) -> bool:
        """True only when a caller may act on data for this capability (supported or limited)."""
        return self.state in (CapabilityState.supported, CapabilityState.limited)


class ProviderCapabilityMatrix:
    """The full set of a provider's capability declarations. Missing entries default to ``unknown``.

    Immutable after construction (the declarations dict is copied) — a connector's honesty about what
    it can do must not drift under a caller's feet mid-request.
    """

    def __init__(self, provider: str, declarations: list[CapabilityDeclaration]) -> None:
        self.provider = provider
        self._by_cap: dict[SchoolCapability, CapabilityDeclaration] = {d.capability: d for d in declarations}

    def declaration(self, cap: SchoolCapability) -> CapabilityDeclaration:
        """The provider's stance on ``cap`` — an explicit ``unknown`` when it never asserted one."""
        return self._by_cap.get(
            cap, CapabilityDeclaration(cap, CapabilityState.unknown, reason="not declared by provider")
        )

    def state(self, cap: SchoolCapability) -> CapabilityState:
        return self.declaration(cap).state

    def supports(self, cap: SchoolCapability) -> bool:
        """Does the provider support ``cap`` well enough to rely on? (supported or limited, never unknown)."""
        return self.declaration(cap).usable

    def as_dict(self) -> dict[str, dict[str, str | None]]:
        """Serializable snapshot (for provenance / audit): {capability: {state, reason}}."""
        return {
            cap.value: {"state": d.state.value, "reason": d.reason}
            for cap, d in self._by_cap.items()
        }
