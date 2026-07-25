"""EmailQualityValidator (email quality layer) — the DETERMINISTIC gate every draft passes before a send.

The model may write the draft, but this validator, not the model, decides whether it ships. It encodes the
non-negotiable product rule ("Bruce emails must sound like a competent real person wrote them") as concrete
checks: no em dash, no banned filler, grounded facts only, an attachment claim that matches reality, a clear
ask, a specific human subject, proportional length, no invented context, no unsupported completion claim. A
draft that fails is rewritten or replaced by the deterministic floor — slop never reaches the provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import response_composer
from .conversation_style import _EM_DASH, _fact_tokens
from .email_brief import EmailBrief, relationship_style

# substring, case-insensitive — the explicit banned filler + AI tells + corporate/ inflated vocabulary
_BANNED_PHRASES = (
    "i hope this email finds you well", "hope this email finds you", "hope this finds you well",
    "i wanted to reach out", "wanted to reach out", "just wanted to reach out",
    "delve", "kindly", "please do not hesitate", "do not hesitate to", "at your earliest convenience",
    "please find attached", "i am writing to", "i am reaching out", "to whom it may concern",
    "dear sir or madam", "dear sir/madam", "as per your request", "per your request",
    "circle back", "touch base", "moving forward, i", "in order to facilitate",
)
_AI_TELLS = ("as an ai", "language model", "as a large language", "automated test", "this is an automated")
_INFLATED = (r"\butilize\b", r"\bendeavor\b", r"\bfacilitate\b", r"\bcommence\b", r"\baforementioned\b",
             r"\bheretofore\b", r"\bwhilst\b", r"\bhereby\b", r"\bthereof\b")
_FAKE_ENTHUSIASM = ("!!!", "so excited", "super excited", "thrilled to", "absolutely love", "beyond excited")
_EXCESS_GRATITUDE = ("thank you so much in advance", "can't thank you enough", "cannot thank you enough",
                     "many many thanks", "a million thanks", "eternally grateful")
_WEIRD_SIGNOFF = ("warm regards", "warmest regards", "yours faithfully", "yours truly", "respectfully submitted",
                  "i hope this helps", "hope this helps!", "yours sincerely,\nbruce", "kind regards and")
_GENERIC_SUBJECTS = ("hi", "hello", "hey", "update", "question", "quick question", "reaching out",
                     "checking in", "follow up", "follow-up", "important", "info", "email", "message", "")
_ATTACH_CLAIM = re.compile(r"\b(i(?:'ve| have)?\s+attach(?:ed)?|attached (?:is|are|you'?ll find)|"
                           r"see (?:the )?attach|find attached|attaching)\b", re.IGNORECASE)
_SLANG = re.compile(r"\b(hey+|yo|sup|hiya|lol|lmao|omg|ur|u|tbh|imo|gonna|wanna|thx|pls|ya'?ll)\b", re.IGNORECASE)
# a clear call to action OR a clear next step (a decline/FYI's "i'll be back", "i'm sending it" also counts)
_QUESTION_OR_ASK = re.compile(
    r"\?|\b(can you|could you|would you|please (send|let|share|confirm|advise)|let me know|"
    r"i(?:'d| would) (?:like|appreciate)|i wanted to ask|when (?:can|could|is|are)|i need|are you able|"
    r"do you have|i(?:'ll| will)\b|i'?m (?:sending|attaching|available|free)|see you|talk (?:soon|then|later)|"
    r"by (?:tomorrow|monday|tuesday|wednesday|thursday|friday|next)|here'?s)\b", re.IGNORECASE)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class QualityReport:
    ok: bool
    issues: tuple[QualityIssue, ...]

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def validate_email(subject: str, body: str, brief: EmailBrief, *, profile=None) -> QualityReport:
    """Return every quality violation. ok=True only when the draft is clean on ALL non-negotiables."""
    issues: list[QualityIssue] = []
    blob = f"{subject}\n{body}"
    low = blob.lower()
    style = relationship_style(brief.recipient_relationship)

    # 1. no em dash (or en-dash-as-punctuation) anywhere
    if _EM_DASH in blob or re.search(r"(?<!\d)\s+–\s+(?!\d)|(?<=[A-Za-z])–(?=[A-Za-z])", blob):
        issues.append(QualityIssue("em_dash", "contains an em/en dash"))

    # 2. banned filler / AI tells / inflated vocab / fake enthusiasm / excess gratitude / weird sign-off
    for p in _BANNED_PHRASES:
        if p in low:
            issues.append(QualityIssue("banned_phrase", p))
    for p in _AI_TELLS:
        if p in low:
            issues.append(QualityIssue("ai_tell", p))
    for pat in _INFLATED:
        if re.search(pat, low):
            issues.append(QualityIssue("inflated_vocab", pat.strip("\\b")))
    for p in _FAKE_ENTHUSIASM:
        if p in low:
            issues.append(QualityIssue("fake_enthusiasm", p))
    for p in _EXCESS_GRATITUDE:
        if p in low:
            issues.append(QualityIssue("excessive_gratitude", p))
    for p in _WEIRD_SIGNOFF:
        if p in low:
            issues.append(QualityIssue("weird_signoff", p))

    # 3. subject specific + human
    subj = (subject or "").strip()
    if subj.lower() in _GENERIC_SUBJECTS:
        issues.append(QualityIssue("generic_subject", subj or "(empty)"))
    elif len(subj) > 90:
        issues.append(QualityIssue("subject_too_long", f"{len(subj)} chars"))

    # 4. facts grounded — every concrete token (number/date/time/email/url/price) must trace to the brief.
    # Normalize surrounding sentence punctuation on BOTH sides so "4471." (end of sentence) matches "4471".
    def _norm(toks):
        return {t.strip(".,;:!?()[]'\"").lower() for t in toks} - {""}
    invented = _norm(_fact_tokens(body)) - _norm(_fact_tokens(brief.grounding_text()))
    if invented:
        issues.append(QualityIssue("invented_fact", ", ".join(sorted(invented))[:120]))

    # 5. attachment claim must match reality
    present = [a for a in brief.attachments if a.present]
    claims_attach = bool(_ATTACH_CLAIM.search(body))
    if claims_attach and not present:
        issues.append(QualityIssue("attachment_lie", "claims an attachment that isn't attached"))
    if present and not claims_attach:
        issues.append(QualityIssue("attachment_unmentioned", present[0].name))

    # 6. tone / relationship match — slang only where the relationship (or the user) allows it
    allow_slang = style.allow_slang or bool(getattr(profile, "contractions", False) and style.formality <= 2)
    if not style.allow_slang and _SLANG.search(body):
        issues.append(QualityIssue("tone_mismatch", "slang in a non-casual email"))

    # 7. clear call to action
    if not _QUESTION_OR_ASK.search(body):
        issues.append(QualityIssue("no_cta", "no clear ask / next step"))

    # 8. no duplicated sentences
    sents = _sentences(body)
    norm = [re.sub(r"\s+", " ", s.lower()) for s in sents if len(s) > 12]
    if len(norm) != len(set(norm)):
        issues.append(QualityIssue("duplicate_sentence", "a sentence repeats"))

    # 9. length proportional — no giant paragraph, no runaway email
    for para in re.split(r"\n\s*\n", body or ""):
        p = para.strip()
        if len(p) > 700 or len([s for s in _sentences(p)]) > 6:
            issues.append(QualityIssue("giant_paragraph", f"{len(p)} chars in one block"))
            break
    if len(body or "") > 1600:
        issues.append(QualityIssue("too_long", f"{len(body)} chars"))

    # 10. names correct — the sign-off must be the real sender, never an invented name
    if brief.sender_name and brief.sender_name.split()[0].lower() not in low:
        issues.append(QualityIssue("missing_sender", brief.sender_name))

    # 11. no unsupported completion claim (reuse the runtime's completion-truth guard)
    if response_composer.claims_action_completion(body):
        issues.append(QualityIssue("unsupported_completion", "claims an action was completed"))

    return QualityReport(ok=not issues, issues=tuple(issues))
