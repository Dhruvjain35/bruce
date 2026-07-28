"""InputEnvelope — the student's own words, kept apart from everything else that arrived with them.

WHAT GOES WRONG WITHOUT IT. One turn can carry six kinds of text: what the student typed, OCR from a
screenshot, extracted attachment text, a pasted quote, a forwarded email, and content Bruce fetched from
a provider. Five of those were written by somebody else. The moment they are concatenated into one
string, "yes add it" spoken by a coach in a screenshot is indistinguishable from the student saying it —
and this repository has now produced that exact failure twice: once in the #120 authorization corpus,
and once in the semantic-rescue suite, where `[screenshot] Coach: yes ur good to add it` approved a
pending proposal.

Both times the tempting fix was to teach a matcher one more marker to recognise. That loses, because the
markers are unbounded and the attacker picks them. The structural fix is to never join the strings: the
authorization path is handed `trusted_text` and nothing else, and the untrusted material travels beside
it as attributed evidence.

    reasoning may READ untrusted evidence.
    authorization may not SEE it.

THE ASYMMETRY IS DELIBERATE. A refusal is honoured wherever it appears, because the cost of over-reading
a "don't" is one extra question; an affirmative is honoured only from trusted text, because the cost of
over-reading a "yes" is an irreversible act. `authorizing_text()` and `evidence_text()` exist so a caller
picks a side explicitly rather than reaching for whichever field is nearest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Provenance of every non-trusted field, in the order a reply should attribute them. Named rather than
# inferred so a reply can say "from an email you forwarded" without a caller inventing the phrase.
OCR = "ocr"
ATTACHMENT = "attachment"
QUOTED = "quoted"
FORWARDED = "forwarded"
PROVIDER = "provider"

UNTRUSTED_KINDS = (OCR, ATTACHMENT, QUOTED, FORWARDED, PROVIDER)


@dataclass(frozen=True)
class InputEnvelope:
    """One inbound turn, with its sources kept separate end to end.

    `trusted_text` is what the student typed into the message box. Nothing else in this object is their
    authorship, and nothing else may authorize.
    """

    trusted_text: str = ""
    ocr_text: str = ""
    attachment_text: str = ""
    quoted_text: str = ""
    forwarded_text: str = ""
    provider_content: str = ""
    source_message_id: str | None = None

    def untrusted(self) -> dict[str, str]:
        """Every non-authored source that carries text, by kind. Empty ones are dropped so a caller
        iterating this cannot accidentally attribute a claim to a source that said nothing."""
        pairs = ((OCR, self.ocr_text), (ATTACHMENT, self.attachment_text), (QUOTED, self.quoted_text),
                 (FORWARDED, self.forwarded_text), (PROVIDER, self.provider_content))
        return {kind: text for kind, text in pairs if (text or "").strip()}

    def has_untrusted(self) -> bool:
        return bool(self.untrusted())

    def untrusted_blob(self) -> str:
        """All untrusted material as one string, for GROUNDING CHECKS ONLY.

        This is what `authorization_evidence._grounded` and `semantic_rescue.resolve_confirmation` use to
        ask "did this affirmative actually come from somebody else". It exists so a checker can compare
        against it — never so a caller can feed it to the boundary, which is why it is a different method
        from `authorizing_text` and not a default anywhere.
        """
        return "\n".join(self.untrusted().values())

    def evidence_text(self) -> str:
        """Attributed untrusted material, for REASONING. A model may read a forwarded email to work out
        what a deadline is; it may not treat the sender's "sure, go ahead" as consent, which is enforced
        by that path never reaching `authorizing_text`."""
        return "\n".join(f"[{kind}] {text}" for kind, text in self.untrusted().items())

    def authorizing_text(self) -> str:
        """THE ONLY thing the authorization path may see. Never the evidence, never a join of the two."""
        return self.trusted_text or ""


def from_message(msg, *, ocr_text: str = "", attachment_text: str = "",
                 provider_content: str = "") -> InputEnvelope:
    """Build an envelope from an inbound message plus whatever was extracted alongside it.

    Quoted and forwarded regions are identified by `decision_resolver.trusted_reply_text`, which already
    owns that definition for the refusal boundary — so there is one answer to "which part of this did
    the student write", rather than a second implementation that can disagree with the first.
    """
    from . import decision_resolver

    raw = getattr(msg, "text", None) or ""
    trusted = decision_resolver.trusted_reply_text(raw)
    # Whatever `trusted_reply_text` stripped was a quoted or forwarded region. Keeping it as evidence
    # rather than discarding it is what lets a reply say where a fact came from.
    stripped = raw[len(trusted):] if raw.startswith(trusted) else ""
    return InputEnvelope(
        trusted_text=trusted.strip(), ocr_text=ocr_text, attachment_text=attachment_text,
        quoted_text=stripped.strip(), forwarded_text="", provider_content=provider_content,
        source_message_id=getattr(msg, "provider_message_id", None))
