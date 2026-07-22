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
from .technical_render import is_technical_line

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
    """Lowercase only the first alpha of each line (casual register) - never touch mid-line words, so
    proper nouns / fact tokens are left intact (the guard covers the rest)."""
    return "\n".join(re.sub(r"^(\s*)([A-Z])", lambda m: m.group(1) + m.group(2).lower(), ln)
                     for ln in text.split("\n"))


_EM_DASH = "—"
_EN_DASH = "–"


def enforce_no_dashes(text: str) -> str:
    """Student-facing Bruce never uses em dashes, nor en dashes as sentence punctuation. Rewrite them
    to a comma. A numeric range (digit-en dash-digit, e.g. "25–26" or "9:00 – 10:00") is a FACT
    and is left untouched. Idempotent; safe to run after generation AND after styling."""
    if _EM_DASH not in text and _EN_DASH not in text:
        return text
    out = re.sub(r"\s*—\s*", ", ", text)                         # em dash -> comma (always)
    out = re.sub(r"(?<!\d)\s+–\s+(?!\d)", ", ", out)             # spaced en dash, NOT a numeric range
    out = re.sub(r"(?<=[A-Za-z])–(?=[A-Za-z])", ", ", out)       # tight en dash between words
    out = re.sub(r",\s*,", ",", out)                                  # tidy doubled commas
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"^\s*,\s*", "", out)                                # drop a leading comma from a lead dash
    return re.sub(r"[ \t]{2,}", " ", out).strip()


_OFFER_RE = re.compile(
    r"(?:^|(?<=[.?!\n]))\s*(?:if (?:you|u|ya)(?:'?d| would)?\s+(?:want|like)|want me to|"
    r"lmk if|let me know if|if (?:you|u|ya)\s+want|should i\b|i can (?:also|help)|"
    r"wanna|do (?:you|u) want|want (?:a|me)\b)[^.?!\n]*[.?!]?\s*$", re.IGNORECASE)


def strip_redundant_offer(text: str) -> str:
    """Remove a TRAILING 'want me to…' / 'if you want, i can…' offer so Bruce doesn't end every reply
    with one. Conservative: only the final sentence, and only if it carries NO fact token (never drops a
    number/date/price/url/@handle) and something meaningful remains before it."""
    m = _OFFER_RE.search(text)
    if not m:
        return text
    tail = text[m.start():]
    if _fact_tokens(tail):
        return text
    low = tail.lower()
    # KEEP genuine tutoring choices/next-steps (they add value); only strip a generic low-value closer.
    if " or " in low or any(k in low for k in
                            ("hint", "walkthrough", "walk through", "example", "practice", "step",
                             "check your", "next problem", "harder one")):
        return text
    kept = text[:m.start()].rstrip()
    return kept if kept else text


# AUTHORITATIVE defaults live in code — the safety-critical fact-locked copy must exist in every
# deployment regardless of whether product/*.yaml shipped. product/*.yaml is an optional human-facing
# override (merged over these). Keeping the "never claims added" copy in code is deliberate.
DEFAULT_TEMPLATES: dict[str, str] = {
    "event_saved_calendar_unavailable": (
        "got it, saved this event:\n{title}\n{when}{where}\n"
        "heads up: i can't add it to your calendar yet (not connected). "
        "i've kept it so you don't have to resend."),
    "could_not_read_attachment": "couldn't open that one 😕 can you resend it? (a clearer photo or the file works)",
    "reply_attachment_pending": "i can see which pic ur replying to, but the file isn't downloaded on the Bruce Mac yet. give it a sec and try again",
    "reply_target_unavailable": "i can tell u replied to an earlier msg but i can't pull that exact one up anymore. mind resending it so i'm looking at the right thing?",
    "unsupported_capability": "can't do {capability} yet, that's not wired up on my end. i can still {alternative}.",
    "tutoring_offer": "looks like {topic}. want a hint, a full walkthrough, or should i just check your answers?",
    "needs_clarification_wrapper": "{question}",
    "mission_started_no_push": "on it: {what}. i'll have it ready next time you check in.",
    "mission_handoff_ack": "gotchu, i've got {what} from here. i won't do anything that needs your ok without asking first, and i'll only ping you when i actually need your call.",
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
        return enforce_no_dashes(raw.format(**slots).strip())    # never ship an em dash, even from copy

    def derive_profile(self, sample_texts: list[str]) -> VoiceProfile:
        """Best-effort mirror from a bounded recent window (NOT persisted in Bite 1)."""
        samples = [t for t in sample_texts if t]
        if not samples:
            return VoiceProfile()
        joined = " ".join(samples)
        lowercase = sum(1 for t in samples if t == t.lower()) >= len(samples) / 2
        return VoiceProfile(lowercase=lowercase, emoji_ok=bool(_EMOJI.search(joined)), slang_ok=True,
                            avg_bubble_chars=max(40, int(sum(len(t) for t in samples) / len(samples))))

    def _style_segment(self, text: str, *, serious: bool, profile: VoiceProfile) -> str:
        """Voice styling for one PROSE segment: strip filler, collapse spaces, emoji/lowercase per
        register, no em dashes. Never applied to technical lines (whose alignment + variable case must
        survive verbatim)."""
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
        return enforce_no_dashes(styled)                 # HARD: student-facing Bruce uses no em dashes

    def render(self, text: str, *, risk_level: RiskLevel = RiskLevel.none,
               profile: VoiceProfile | None = None, protect_technical: bool = False) -> str:
        """Style the model's user_visible_response. Presentation-only; facts are guarded.

        ``protect_technical`` styles PROSE lines only and passes technical lines (matrix rows,
        equations, labelled expressions) through verbatim — so matrix alignment and variable case
        (``T`` never becomes ``t``) survive the voice pass. Set it once technical content has already
        been rendered to plain text/Unicode (see technical_render.render_for_channel)."""
        profile = profile or VoiceProfile()
        serious = risk_level in (RiskLevel.sensitive, RiskLevel.high)
        if protect_technical:
            styled = "\n".join(
                ln if (not ln.strip() or is_technical_line(ln))
                else self._style_segment(ln, serious=serious, profile=profile)
                for ln in text.split("\n"))
        else:
            styled = self._style_segment(text, serious=serious, profile=profile)
        # HARD channel rule: an em dash NEVER ships to a student, even on a technical/protected line
        # (matrices/equations contain none, so this can't touch a numeric range) — the failure was a
        # casual "= " line being treated as technical and skipping the voice pass.
        styled = enforce_no_dashes(styled)
        assert_facts_preserved(text, styled)             # HARD invariant: never ship altered facts
        return styled
