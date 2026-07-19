"""Bruce Voice OS (Bite 1) — presentation-only styling of the model's reply.

HARD INVARIANT: styling may NEVER change facts, permissions, uncertainty, recipients, deadlines,
prices, or action scope. Enforced three ways:
  (a) fact-bearing / safety-critical copy comes VERBATIM from product/message_templates.yaml (slot
      interpolation only) — never model freeform;
  (b) assert_facts_preserved() rejects any styling that drops or alters a number/date/time/price/
      URL/@handle/email;
  (c) prohibited-phrase stripping only removes corporate/robotic filler, never content.

Serious context (risk_level sensitive/high) overrides all mirroring: full sentences, no emoji/slang.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import yaml

from .conversation_contract import RiskLevel

_PRODUCT = Path(__file__).resolve().parents[2] / "product"

# Corporate/robotic phrases Bruce never says (case-insensitive; stripped as filler).
PROHIBITED_PHRASES = (
    "i'd be happy to", "i would be happy to", "as an ai", "as a language model",
    "let me help you with that", "great question", "i apologize for any inconvenience",
    "feel free to", "happy to assist", "i'm here to help", "i hope this helps",
    "your request has been received", "i am processing your request",
    "your task has been completed successfully", "i understand that", "delve",
)

# Fact tokens that MUST survive styling verbatim.
_FACT_PATTERNS = [
    re.compile(r"https?://\S+"),                       # url
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),       # email
    re.compile(r"(?<![\w@])@\w[\w.]*"),                # @handle
    re.compile(r"\$\d[\d,]*(?:\.\d+)?"),               # price
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),              # ISO date
    re.compile(r"\b\d{1,2}:\d{2}\s?(?:[ap]m)?", re.I), # time
    re.compile(r"\d[\d,./:-]*"),                       # any other number (dates, counts, phone)
]

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE0F]"
)


class StyleViolation(Exception):
    """Styling dropped or altered a fact — refuse to send (message carries no content)."""


@dataclasses.dataclass
class VoiceProfile:
    lowercase: bool = True
    emoji_ok: bool = True
    slang_ok: bool = True
    avg_bubble_chars: int = 200


def _fact_tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for pat in _FACT_PATTERNS:
        toks.update(m.strip() for m in pat.findall(text))
    return {t for t in toks if t}


def assert_facts_preserved(src: str, styled: str) -> None:
    """Raise if any fact token in `src` is missing from `styled`. No content in the exception."""
    missing = [t for t in _fact_tokens(src) if t not in styled]
    if missing:
        raise StyleViolation(f"styling dropped/altered {len(missing)} fact token(s)")


def _lower_lead(text: str) -> str:
    """Lowercase only the first alpha of each line (casual register) — never touch mid-line words, so
    proper nouns / fact tokens are left intact (the guard covers the rest)."""
    return "\n".join(re.sub(r"^(\s*)([A-Z])", lambda m: m.group(1) + m.group(2).lower(), ln)
                     for ln in text.split("\n"))


# AUTHORITATIVE defaults live in code — the safety-critical fact-locked copy must exist in every
# deployment regardless of whether product/*.yaml shipped. product/*.yaml is an optional human-facing
# override (merged over these). Keeping the "never claims added" copy in code is deliberate.
DEFAULT_TEMPLATES: dict[str, str] = {
    "event_saved_calendar_unavailable": (
        "got it — saved this event:\n{title}\n{when}{where}\n"
        "heads up: i can't add it to your calendar yet (not connected). "
        "i've kept it so you don't have to resend."),
    "could_not_read_attachment": "couldn't read that one 😕 can you resend it? (a clearer photo or the file works)",
    "unsupported_capability": "can't do {capability} yet — that's not wired up on my end. i can still {alternative}.",
    "tutoring_offer": "looks like {topic}. want a hint, a full walkthrough, or should i just check your answers?",
    "needs_clarification_wrapper": "{question}",
    "mission_started_no_push": "on it — {what}. i'll have it ready next time you check in.",
}
DEFAULT_PROFILES: dict = {"base": {"register": "lowercase", "max_bubble_chars": 320, "result_first": True}}


def _load_yaml_override(name: str) -> dict:
    """Optional product/*.yaml override; {} if it didn't ship (e.g. inside the container image)."""
    try:
        with open(_PRODUCT / name) as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


class ConversationStyleEngine:
    def __init__(self, profiles: dict | None = None, templates: dict | None = None) -> None:
        self.profiles = profiles if profiles is not None else {**DEFAULT_PROFILES, **_load_yaml_override("voice_profiles.yaml")}
        self.templates = templates if templates is not None else {**DEFAULT_TEMPLATES, **_load_yaml_override("message_templates.yaml")}

    def template(self, name: str, **slots) -> str:
        """A fact-locked fragment, verbatim with slot interpolation only (no styling pass)."""
        raw = self.templates.get(name)
        if raw is None:
            raise KeyError(f"unknown message template {name!r}")
        return raw.format(**slots).strip()

    def derive_profile(self, sample_texts: list[str]) -> VoiceProfile:
        """Best-effort mirror from a bounded recent window (NOT persisted in Bite 1)."""
        samples = [t for t in sample_texts if t]
        if not samples:
            return VoiceProfile()
        joined = " ".join(samples)
        lowercase = sum(1 for t in samples if t == t.lower()) >= len(samples) / 2
        return VoiceProfile(lowercase=lowercase, emoji_ok=bool(_EMOJI.search(joined)), slang_ok=True,
                            avg_bubble_chars=max(40, int(sum(len(t) for t in samples) / len(samples))))

    def render(self, text: str, *, risk_level: RiskLevel = RiskLevel.none,
               profile: VoiceProfile | None = None) -> str:
        """Style the model's user_visible_response. Presentation-only; facts are guarded."""
        profile = profile or VoiceProfile()
        serious = risk_level in (RiskLevel.sensitive, RiskLevel.high)
        styled = text.strip()
        for p in PROHIBITED_PHRASES:                     # strip corporate/robotic filler
            styled = re.sub(re.escape(p), "", styled, flags=re.IGNORECASE)
        styled = re.sub(r"[ \t]{2,}", " ", styled).strip()
        if serious:
            styled = _EMOJI.sub("", styled).strip()      # serious: no emoji, no lowercasing
            styled = re.sub(r"[ \t]{2,}", " ", styled).strip()
        else:
            if not profile.emoji_ok:
                styled = _EMOJI.sub("", styled).strip()
            if profile.lowercase:
                styled = _lower_lead(styled)
        assert_facts_preserved(text, styled)             # HARD invariant — never ship altered facts
        return styled
