"""Deterministic HUMANIZER lint: strip common AI-writing tells from draft prose.

This runs on an already-assembled email body. It only touches *cosmetic* word choice
and filler — it never rewrites a grounded claim's meaning, and it refuses to touch the
spans that carry grounding or identity: quoted paper titles, the ``[[...]]`` student
placeholder, the greeting line, and the sign-off/name line. Those are masked out before
any transform and restored byte-for-byte afterward, so this stays grounding-safe.

Everything here is a pure string transform: no LLM, no network, fully deterministic.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# AI-stock word / phrase -> plain equivalent. Phrases are ordered before the
# bare words so the more specific match wins (e.g. "a testament to" before
# "testament"). Word-boundary anchoring keeps inflections from colliding.
# ---------------------------------------------------------------------------
_PHRASE_REPLACEMENTS: list[tuple[str, str]] = [
    # Cliché openers that END IN an infinitive ("...to <verb>"): REPLACE to keep grammar,
    # never delete, or the following verb is stranded ("Inquire about..."). Longer first.
    ("i am writing to reach out to", "i am writing to"),
    ("i wanted to reach out to", "i wanted to"),
    ("i just wanted to take a moment to", "i just wanted to"),
    ("i wanted to take a moment to", "i wanted to"),
    ("i would like to take a moment to", "i would like to"),
    ("i am reaching out to", "i am writing to"),
    ("i'm reaching out to", "i'm writing to"),
    ("delve into", "look at"),
    ("delves into", "looks at"),
    ("delved into", "looked at"),
    ("delving into", "looking at"),
    ("is a testament to", "shows"),
    ("are a testament to", "show"),
    ("was a testament to", "showed"),
    ("were a testament to", "showed"),
    ("a testament to", "a sign of"),
    ("testament to", "a sign of"),
    ("tapestry of", "mix of"),
]

_WORD_REPLACEMENTS: list[tuple[str, str]] = [
    ("delve", "look"),
    ("delves", "looks"),
    ("delved", "looked"),
    ("delving", "looking"),
    ("leverage", "use"),
    ("leverages", "uses"),
    ("leveraged", "used"),
    ("leveraging", "using"),
    ("underscore", "show"),
    ("underscores", "shows"),
    ("underscored", "showed"),
    ("underscoring", "showing"),
    ("utilize", "use"),
    ("utilizes", "uses"),
    ("utilized", "used"),
    ("utilizing", "using"),
    ("utilization", "use"),
    ("meticulously", "carefully"),
    ("meticulous", "careful"),
    ("realms", "areas"),
    ("realm", "area"),
    ("tapestry", "mix"),
    ("testament", "example"),
]

# Filler openers to cut wholesale. When one leads a sentence the following word is
# re-capitalized; mid-sentence occurrences are simply removed.
# Only fillers that lead a FULL CLAUSE are safe to delete + re-capitalize. Openers ending in
# an infinitive are grammatical REPLACEMENTS in _PHRASE_REPLACEMENTS above, not deletions.
_FILLERS: list[str] = [
    "it is worth noting that",
    "it's worth noting that",
]

_GREETING_LINE_RE = re.compile(
    r"^[ \t]*(?:hi|hello|dear|greetings|good (?:morning|afternoon|evening))\b[^\n]*",
    re.IGNORECASE,
)
_SIGNOFF_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:best regards|kind regards|warm regards|best|sincerely|regards|"
    r"respectfully|cheers|thank you|thanks)\b.*$"
)

_SENTINEL = "\x00{}\x00"
_SENTINEL_RE = re.compile(r"\x00\d+\x00")


def _phrase_pattern(phrase: str) -> str:
    """Word-boundary-anchored regex for ``phrase`` with flexible internal whitespace."""
    inner = r"\s+".join(re.escape(w) for w in phrase.split())
    return r"\b" + inner + r"\b"


def _match_case(src: str, repl: str) -> str:
    """Cast ``repl`` into the surrounding case of the matched ``src``."""
    if src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _compile_replacements() -> list[tuple[re.Pattern[str], str]]:
    compiled: list[tuple[re.Pattern[str], str]] = []
    for phrase, repl in _PHRASE_REPLACEMENTS:
        compiled.append((re.compile(_phrase_pattern(phrase), re.IGNORECASE), repl))
    for word, repl in _WORD_REPLACEMENTS:
        compiled.append((re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE), repl))
    return compiled


_COMPILED_REPLACEMENTS = _compile_replacements()


def _replace_stock_words(text: str) -> str:
    for pattern, repl in _COMPILED_REPLACEMENTS:
        text = pattern.sub(lambda m, r=repl: _match_case(m.group(0), r), text)
    return text


def _cut_fillers(text: str) -> str:
    for filler in _FILLERS:
        body = _phrase_pattern(filler)
        # Sentence-leading occurrence: drop it and capitalize the next word.
        text = re.sub(
            r"(?im)(^|[.!?]\s+)" + body + r"\s+(\w)",
            lambda m: m.group(1) + m.group(2).upper(),
            text,
        )
        # Any remaining (mid-sentence) occurrence: just remove it.
        text = re.sub(r"(?i)" + body + r"\s+", "", text)
    return text


def _reduce_em_dashes(text: str) -> str:
    """Convert em-dashes (and em-dash-style double hyphens) to commas — a known tell.

    Commas preserve the clause meaning; a following capital gets a period instead so a
    new sentence still reads as one.
    """
    text = re.sub(r"\s*(?:—|--)\s*([A-Z])", r". \1", text)
    text = re.sub(r"\s*(?:—|--)\s*", ", ", text)
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"\s+,", ",", text)          # no space before a comma
    text = re.sub(r",\s*,", ", ", text)         # collapse doubled commas
    text = re.sub(r",\s*([.!?;:])", r"\1", text)  # comma butting other punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)      # collapse runs of spaces
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)  # space before punctuation
    text = re.sub(r"[ \t]+\n", "\n", text)      # trailing spaces on a line
    return text


def humanize_body(text: str) -> str:
    """Return ``text`` with AI-writing tells reduced, protected spans left intact.

    Cosmetic only: swaps stock words for plain ones, cuts filler openers, and tames
    em-dash overuse. Quoted titles, the ``[[...]]`` placeholder, the greeting line, and
    the sign-off/name line are masked out first and restored byte-for-byte, so grounded
    content and identity lines never change.
    """
    if not text:
        return text

    protected: list[str] = []

    def _stash(value: str) -> str:
        protected.append(value)
        return _SENTINEL.format(len(protected) - 1)

    def _mask(pattern: re.Pattern[str], src: str) -> str:
        return pattern.sub(lambda m: _stash(m.group(0)), src)

    masked = text

    # 1. Greeting line (first line only, and only if it reads as a salutation).
    gm = _GREETING_LINE_RE.match(masked)
    if gm:
        masked = _stash(gm.group(0)) + masked[gm.end() :]

    # 2. Sign-off + name: from the last sign-off line through end of text.
    last_signoff = None
    for sm in _SIGNOFF_LINE_RE.finditer(masked):
        last_signoff = sm
    if last_signoff is not None:
        masked = masked[: last_signoff.start()] + _stash(masked[last_signoff.start() :])

    # 3. Student placeholder(s): any [[...]] span (contains its own em-dash/apostrophe).
    masked = _mask(re.compile(r"\[\[.*?\]\]", re.DOTALL), masked)

    # 4. Quoted spans (paper titles): straight + curly double quotes, then single quotes.
    masked = _mask(re.compile(r'"[^"]*"'), masked)
    masked = _mask(re.compile(r"“[^”]*”"), masked)
    masked = _mask(re.compile(r"(?<![A-Za-z0-9])'[^']*'(?![A-Za-z0-9])"), masked)
    masked = _mask(re.compile(r"‘[^’]*’"), masked)

    # Cosmetic transforms on the unprotected remainder.
    masked = _cut_fillers(masked)
    masked = _replace_stock_words(masked)
    masked = _reduce_em_dashes(masked)
    masked = _tidy(masked)

    # Restore protected spans byte-for-byte.
    def _restore(m: re.Match[str]) -> str:
        return protected[int(m.group(0).strip("\x00"))]

    return _SENTINEL_RE.sub(_restore, masked)
