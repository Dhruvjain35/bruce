"""Email composer (email quality layer) — assemble {relationship profile + user writing profile + grounded
brief} into a real-person email, then let the DETERMINISTIC validator decide if it ships.

Two layers, deterministic-first (same discipline as the Phase-F response composer):
  * _deterministic_draft() is the always-available floor: a clean, grounded, correctly-addressed email built
    only from the brief. It is engineered to PASS the validator, so it is what ships if the model is off, slow,
    or keeps producing slop. Never pretty, always honest.
  * a compact model (gpt-5.4-mini, gated by BRUCE_EMAIL_MODEL) writes the natural version from the SAME brief +
    profiles + any correction directives. Its output is validated; on any violation it is rewritten (bounded),
    and if it still fails, we fall back to the floor. The model never invents facts, recipients, or a subject.

Not one giant fixed prompt: the system prompt is composed per-call from the relationship traits + the user's
profile + the brief, so the same composer covers teacher / coach / professional / peer / support without a
per-relationship template.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .conversation_style import enforce_no_dashes
from .email_brief import EmailBrief, Relationship, relationship_style
from .email_quality import (HAS_ASK, QualityReport, is_generic_subject, scrub, validate_email,
                            _ATTACH_CLAIM)
from .email_voice import UserWritingProfile

_TRUTHY = {"1", "true", "yes", "on"}


def email_model_enabled() -> bool:
    return os.environ.get("BRUCE_EMAIL_MODEL", "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ComposedEmail:
    subject: str
    body: str
    used_model: bool
    fell_back: bool          # True = the model's drafts failed validation and we shipped the deterministic floor
    report: QualityReport


def _ends_sentence(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?" else s + "."


def _subject(brief: EmailBrief) -> str:
    if brief.is_reply and brief.thread_subject:
        base = enforce_no_dashes(brief.thread_subject.strip())
        return base if base.lower().startswith("re:") else f"Re: {base}"
    # a specific, human subject from the purpose (fall back to the ask); scrubbed + de-dashed, never generic
    for src in (brief.purpose, brief.requested_outcome):
        raw = scrub(src or "").strip().rstrip(".?!")
        raw = re.sub(r"^\s*(please|can you|could you|would you|i want to|i need to|i wanted to ask|just|to)\s+",
                     "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\?.*$", "", raw).strip()
        subj = raw[:80].strip()
        if subj and not is_generic_subject(subj):
            return enforce_no_dashes(subj[0].upper() + subj[1:])
    # concrete fallback (only when both fields are empty/generic) — a recipient/topic anchor, never "Following up"
    who = brief.recipient_name or brief.recipient_relationship.value
    return enforce_no_dashes(f"Note for {who}")


def _greeting(brief: EmailBrief, profile: UserWritingProfile) -> str:
    style = relationship_style(brief.recipient_relationship)
    word = profile.greeting or ("hey" if style.allow_slang else "Hi")
    if profile.capitalization == "lowercase" and style.allow_slang:
        word = word.lower()
    elif not style.allow_slang:
        word = word[:1].upper() + word[1:]
    name = (brief.recipient_name or "").strip()
    return f"{word} {name}," if name else f"{word},"


def _deterministic_draft(brief: EmailBrief, profile: UserWritingProfile) -> tuple[str, str]:
    """The guaranteed-clean floor. Built only from the brief, engineered to pass validate_email()."""
    style = relationship_style(brief.recipient_relationship)
    present = [a for a in brief.attachments if a.present]
    lines: list[str] = [_greeting(brief, profile), ""]

    def _no_false_attach(txt: str) -> str:
        # never let a brief field assert an attachment the email isn't actually carrying
        return txt if present else _ATTACH_CLAIM.sub("mentioned", txt)

    # one line of necessary context (the reason), only if the brief supplied it; scrubbed, capped, own block
    context = scrub(_no_false_attach((brief.source_context or "").strip()))
    if context:
        for sent in re.split(r"(?<=[.!?])\s+", context)[:3]:    # cap to 3 sentences so no giant block forms
            s = sent.strip()
            if s:
                lines.append(_ends_sentence(s))
        lines.append("")

    # the actual request; scrub filler, then guarantee a clear ask (CTA) using the validator's own pattern
    ask = _ends_sentence(scrub(_no_false_attach(brief.requested_outcome.strip())))
    if not HAS_ASK.search(ask):
        ask = ask + " Let me know."
    lines.append(ask)

    # a grounded attachment line, only when something is really attached
    if present:
        names = present[0].name if len(present) == 1 else ", ".join(a.name for a in present)
        lines.append(f"I attached {names}.")

    # short natural closing
    signoff = profile.sign_off if profile.sign_off is not None else style.default_signoff
    lines.append("")
    if signoff:
        lines.append(_ends_sentence(signoff).rstrip(".") + ",")
    lines.append(brief.sender_name)

    body = enforce_no_dashes("\n".join(lines).strip())
    return _subject(brief), body


class CompactEmailComposer:
    """The production writer (gated by BRUCE_EMAIL_MODEL): a compact gpt-5.4-mini call whose prompt is
    assembled from the relationship traits + the user's profile + the brief + correction directives. It only
    expresses the grounded brief in a real-person voice — no new facts, recipients, or completion claims."""
    provider = "openai"

    async def draft(self, brief: EmailBrief, profile: UserWritingProfile, *,
                    directives: tuple[str, ...] = ()) -> tuple[str, str]:
        return await self._run(brief, profile, directives=directives, prior=None, issues=())

    async def rewrite(self, brief: EmailBrief, profile: UserWritingProfile, *, subject: str, body: str,
                      issues: tuple[str, ...], directives: tuple[str, ...] = ()) -> tuple[str, str]:
        return await self._run(brief, profile, directives=directives, prior=(subject, body), issues=issues)

    async def _run(self, brief, profile, *, directives, prior, issues):
        from pydantic import BaseModel
        from pydantic_ai import Agent, PromptedOutput

        from . import llm

        class _Out(BaseModel):
            subject: str
            body: str

        style = relationship_style(brief.recipient_relationship)
        rules = ("HARD RULES: no em dash anywhere. Never use: 'hope this email finds you well', 'wanted to "
                 "reach out', 'kindly', 'delve', 'please do not hesitate', 'at your earliest convenience', "
                 "'please find attached'. No fake enthusiasm, no excessive gratitude, no corporate filler, no "
                 "inflated words, no headings, no invented facts or names, no claim of any attachment that "
                 "isn't listed, no giant paragraphs, no weird assistant sign-off, no restating the request "
                 "back. Sound like a competent real person.")
        voice = _voice_directives(profile)
        want = [f"Relationship: {brief.recipient_relationship.value} ({', '.join(style.traits)}).",
                f"Purpose: {brief.purpose}", f"Exactly what to ask for: {brief.requested_outcome}"]
        if brief.source_context:
            want.append(f"Context (grounded, may reference): {brief.source_context}")
        if brief.facts:
            want.append("Only these concrete facts may appear: " + " | ".join(brief.facts))
        present = [a.name for a in brief.attachments if a.present]
        want.append("Attachments actually attached: " + (", ".join(present) if present else "none"))
        if brief.privacy_constraints:
            want.append("Never mention: " + " | ".join(brief.privacy_constraints))
        want.append(f"Sender name for the sign-off: {brief.sender_name}")
        if brief.recipient_name:
            want.append(f"Recipient: {brief.recipient_name}")
        if directives:
            want.append("Apply these adjustments: " + " | ".join(directives))
        if prior is not None:
            want.append(f"Your previous draft was rejected for: {', '.join(issues)}. Rewrite to fix ALL of "
                        f"them, keeping the facts. Previous subject: {prior[0]} / body: {prior[1]}")

        system = (f"You write short, natural emails for a student named {brief.sender_name}. {rules} {voice} "
                  "Structure: a natural greeting, one line of context if needed, the actual request, a clear "
                  "next step, a short closing. Two sentences is fine when that's enough. Return subject + body.")
        agent = Agent(llm.drafting_model(), output_type=PromptedOutput(_Out), system_prompt=system)
        result = await agent.run("\n".join(want))
        return result.output.subject.strip(), enforce_no_dashes(result.output.body.strip())


def _voice_directives(profile: UserWritingProfile) -> str:
    bits = []
    if profile.formality is not None:
        bits.append(f"formality {profile.formality}/5")
    if profile.sentence_length == "short":
        bits.append("short sentences")
    if profile.capitalization == "lowercase":
        bits.append("lowercase style")
    if profile.contractions is True:
        bits.append("use contractions")
    if profile.contractions is False:
        bits.append("avoid contractions")
    if profile.greeting:
        bits.append(f"greeting '{profile.greeting}'")
    if profile.sign_off is not None:
        bits.append(f"sign-off '{profile.sign_off or '(none)'}'")
    if profile.disliked_phrases:
        bits.append("never use: " + ", ".join(profile.disliked_phrases))
    return ("User voice: " + "; ".join(bits) + ".") if bits else ""


async def compose_email(brief: EmailBrief, *, profile: UserWritingProfile | None = None,
                        model: CompactEmailComposer | None = None, directives: tuple[str, ...] = (),
                        max_rewrites: int = 2) -> ComposedEmail:
    """Compose + validate an email. The deterministic floor is always available; the model only ships if its
    output passes the SAME validator, and any em dash is normalized away before judging."""
    profile = profile or UserWritingProfile()
    floor_subject, floor_body = _deterministic_draft(brief, profile)

    if model is None:
        report = validate_email(floor_subject, floor_body, brief, profile=profile)
        return ComposedEmail(floor_subject, floor_body, used_model=False, fell_back=False, report=report)

    subject, body, issues = floor_subject, floor_body, ()
    try:
        subject, body = await model.draft(brief, profile, directives=directives)
    except Exception:
        report = validate_email(floor_subject, floor_body, brief, profile=profile)
        return ComposedEmail(floor_subject, floor_body, used_model=False, fell_back=True, report=report)

    for attempt in range(max_rewrites + 1):
        subject, body = enforce_no_dashes(subject), enforce_no_dashes(body)   # de-dash BOTH before judging
        report = validate_email(subject, body, brief, profile=profile)
        if report.ok:
            return ComposedEmail(subject, body, used_model=True, fell_back=False, report=report)
        if attempt == max_rewrites:
            break
        try:
            subject, body = await model.rewrite(brief, profile, subject=subject, body=body,
                                                issues=report.codes(), directives=directives)
        except Exception:
            break

    # the model could not produce a clean draft -> ship the guaranteed-clean floor
    report = validate_email(floor_subject, floor_body, brief, profile=profile)
    return ComposedEmail(floor_subject, floor_body, used_model=False, fell_back=True, report=report)
