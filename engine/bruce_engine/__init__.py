"""Bruce outreach engine — grounded professor discovery + personalized outreach drafting."""

from .models import (
    DiscoveryResult,
    DraftStatus,
    Evidence,
    EvidenceKind,
    OutreachDraft,
    OutreachGoal,
    OutreachPlan,
    OutreachType,
    PaperRef,
    ProfessorCandidate,
    StudentLevel,
    StudentProfile,
)

__all__ = [
    "DiscoveryResult",
    "DraftStatus",
    "Evidence",
    "EvidenceKind",
    "OutreachDraft",
    "OutreachGoal",
    "OutreachPlan",
    "OutreachType",
    "PaperRef",
    "ProfessorCandidate",
    "StudentLevel",
    "StudentProfile",
]
