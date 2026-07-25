"""EmailQualityValidator (email quality layer) — the DETERMINISTIC gate every draft passes before a send.

The model may write the draft, but this validator, not the model, decides whether it ships. It encodes the
non-negotiable product rule ("Bruce emails must sound like a competent real person wrote them") as concrete
checks. Detection is inflection- and paraphrase-aware (regex, not exact substrings), so "just circling back"
and "I trust this finds you well" are caught alongside their base forms. A draft that fails is rewritten or
replaced by the deterministic floor; slop never reaches the provider.

`scrub()` is the shared deterministic cleaner the floor uses so its own output can never trip these checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import response_composer
from .conversation_style import _EM_DASH, _fact_tokens
from .email_brief import EmailBrief, relationship_style

# --- banned language, as PATTERNS (inflection/paraphrase-aware) -----------------------------------

# the "finds you well" opener family (any leading verb), + other canonical AI/corporate openers & filler
_BANNED_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b(hope|trust|wish)\b[^.\n]{0,25}\bfinds you\b", r"\bfinds you well\b",
    r"\bhope (all is|everything is|things are|your (week|day)|you'?re doing)\b[^.\n]{0,15}\bwell\b",
    r"\btrust you'?re doing well\b",
    r"\b(wanted|just wanted|writing) to (reach out|touch base|follow up with)\b", r"\breach(?:ing)? out to\b",
    r"\bi am (writing|reaching out) to\b", r"\bi wanted to reach out\b",
    r"\bdelve\b", r"\bkindly\b", r"\b(please )?do not hesitate\b", r"\bat your earliest convenience\b",
    r"\bplease find attached\b", r"\bto whom it may concern\b", r"\bdear sir(?: or madam| ?/ ?madam)?\b",
    r"\b(as|per) (?:we |previously |our |your )?(discussed|mentioned|conversation|call|agreed|email)\b",
    r"\bfurther to (?:our|your)\b", r"\bcircl(?:e|ing) back\b", r"\btouch(?:ing)? base\b",
    r"\bper (?:your|our) request\b", r"\bas per\b", r"\bhappy to (assist|help)\b",
    r"\bi'?m here to help\b", r"\bfeel free to reach out\b", r"\blooking forward to hearing from you\b",
))
_AI_TELLS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bas an ai\b", r"\b(large )?language model\b", r"\bautomated (test|message)\b", r"\bthis is an automated\b"))
_INFLATED = tuple(re.compile(rf"\b{w}\b", re.IGNORECASE) for w in (
    "utilize", "endeavor", "facilitate", "commence", "aforementioned", "heretofore", "whilst", "hereby",
    "thereof", "leverage", "expedite", "myriad", "plethora", "cognizant", "utilization", "aforesaid"))
_FAKE_ENTHUSIASM = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"!{2,}", r"\bthrilled\s+(to|about|at|for)\b", r"\bsuper\s+(excited|stoked|pumped|psyched)\b",
    r"\bcan'?t\s+wait\b", r"\bbeyond excited\b", r"\babsolutely love\b", r"\bso (?:excited|stoked|pumped)\b"))
_EXCESS_GRATITUDE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bthanks?\s*(?:you\s*)?(?:so much|a million|a ton|a lot)?\s*in advance\b",
    r"\b(?:thanks a million|a million thanks|thanks a ton)\b", r"\bcan(?:'?t| ?not) thank you enough\b",
    r"\b(?:many many|a million) thanks\b", r"\beternally grateful\b"))
# weird / assistant-y sign-offs — anchored to a closing line (a line that is JUST the sign-off)
_WEIRD_SIGNOFF = re.compile(
    r"(?im)^\s*(warmest(?: regards| wishes)?|warm (?:regards|wishes)|cheers|sincerely|kind regards|"
    r"best wishes|yours (?:faithfully|truly|sincerely)|respectfully submitted|looking forward to hearing "
    r"from you|(?:i )?hope this helps)\s*[,.!]?\s*$")

_GENERIC_SUBJECT_STEMS = frozenset((
    "hi", "hello", "hey", "hi there", "update", "quick update", "an update", "question", "quick question",
    "a quick question", "reaching out", "checking in", "just checking in", "touching base", "follow up",
    "following up", "important", "info", "fyi", "email", "message", "a question", "meeting", "request", ""))

_ATTACH_CLAIM = re.compile(r"\b(i(?:'ve| have)?\s+attach(?:ed)?|attached (?:is|are|you'?ll find)|"
                           r"see (?:the )?attach|find attached|attaching|enclosed)\b", re.IGNORECASE)
_SLANG = re.compile(r"\b(hey+|yo|sup|hiya|lol|lmao|omg|ur|u|tbh|imo|gonna|wanna|thx|pls|gotta|kinda|"
                    r"yeah|nah|yep|idk|btw|lemme|gimme|dunno|y['’]?all|ya['’]?ll)\b", re.IGNORECASE)
# a clear call to action OR a clear next step (a decline/FYI's "i'll be back", "i'm sending it" also counts)
HAS_ASK = re.compile(
    r"\?|\b(can you|could you|would you|please (send|let|share|confirm|advise|review|take a look)|let me know|"
    r"i(?:'d| would) (?:like|appreciate)|i wanted to ask|when (?:can|could|is|are)|i need|are you able|"
    r"do you have|i(?:'ll| will)\b|i'?m (?:sending|attaching|available|free)|see you|talk (?:soon|then|later)|"
    r"by (?:tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next)|here'?s)\b", re.IGNORECASE)
# fabricated prior-interaction cues (grounded only if the brief references a prior interaction)
_PRIOR_INTERACTION = re.compile(
    r"\b(as (?:we )?discussed|per (?:our|your) (?:conversation|call|email|chat)|on (?:our|the) call|"
    r"following up on our (?:conversation|call|chat)|as you (?:mentioned|said|agreed)|"
    r"last time we (?:spoke|talked)|when we (?:spoke|met|talked))\b", re.IGNORECASE)

# spelled-out quantities that are FACTS when bound to a unit (e.g. "three pages") — checked against grounding
_WORDNUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
            "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
            "couple": "2", "dozen": "12"}
_UNIT = (r"pages?|paragraphs?|days?|weeks?|months?|hours?|minutes?|dollars?|copies|attachments?|questions?|"
         r"slides?|sections?|problems?|chapters?|points?|spots?|people|students?")
_WORDQTY = re.compile(rf"\b({'|'.join(_WORDNUM)})\s+({_UNIT})\b", re.IGNORECASE)
_TITLE_SPAN = re.compile(r"\b([A-Z][a-z]+(?:\.?\s+[A-Z][a-z]+)+)\b")   # multi-word Title-Case entity spans

_STOP_TITLE = frozenset(("Mr", "Mrs", "Ms", "Dr", "Prof", "Coach", "Best", "Thanks", "Thank", "Hi", "Hello",
                         "Hey", "Dear", "Regards", "Sincerely", "Cheers"))


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


def _canon_num(tok: str) -> str:
    """Canonicalize a numeric/money/time token for grounding comparison: drop currency + outer punctuation,
    remove thousands separators, collapse time spacing. So '$2,500', '2500', and '2,500' compare equal."""
    t = tok.strip(".,;:!?()[]'\"").lower().lstrip("$£€")
    t = re.sub(r"(?<=\d),(?=\d)", "", t)          # 2,500 -> 2500
    t = re.sub(r"\s+", "", t)                       # 3:00 pm -> 3:00pm
    return t


def _digits(tok: str) -> str:
    return re.sub(r"\D", "", tok)


def _grounded_numbers(body: str, grounding: str) -> set[str]:
    """Body numeric tokens that are NOT grounded. A body number is grounded if its canonical form matches, or
    its digit-run is a substring of a grounding digit-run (a reformatted phone/amount is still the same fact)."""
    g_canon = {_canon_num(t) for t in _fact_tokens(grounding)}
    g_digits = {d for d in (_digits(t) for t in _fact_tokens(grounding)) if len(d) >= 3}
    bad = set()
    for t in _fact_tokens(body):
        c = _canon_num(t)
        if not c or c in g_canon:
            continue
        d = _digits(t)
        if d and (d in g_digits or any(d in gd for gd in g_digits)):
            continue
        bad.add(c)
    return bad


def scrub(text: str) -> str:
    """Best-effort deterministic removal of the slop the floor might otherwise inherit from a brief field:
    banned openers/phrases and em/en dashes. Used by the composer's floor so its output passes THIS validator.
    Conservative — only deletes matched filler, never facts."""
    from .conversation_style import enforce_no_dashes
    out = text or ""
    for pat in _BANNED_PATTERNS:
        out = pat.sub("", out)
    out = _WEIRD_SIGNOFF.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"(^|[.!?]\s+)[.,;:]\s*", r"\1", out)     # tidy punctuation left by a deletion
    return enforce_no_dashes(out).strip()


def is_generic_subject(subject: str) -> bool:
    """A subject is generic if, after stripping leading filler words, it collapses to a contentless stem."""
    s = (subject or "").strip().lower().rstrip("?.!")
    s = re.sub(r"^(re|fwd):\s*", "", s)
    s = re.sub(r"^(just|a|an|the|quick|some|another)\s+", "", s).strip()
    return s in _GENERIC_SUBJECT_STEMS


def validate_email(subject: str, body: str, brief: EmailBrief, *, profile=None) -> QualityReport:
    """Return every quality violation. ok=True only when the draft is clean on ALL non-negotiables."""
    issues: list[QualityIssue] = []
    blob = f"{subject}\n{body}"
    style = relationship_style(brief.recipient_relationship)

    def add(code, detail):
        issues.append(QualityIssue(code, detail))

    # 1. no em dash (or en-dash-as-punctuation) anywhere
    if _EM_DASH in blob or re.search(r"(?<!\d)\s+–\s+(?!\d)|(?<=[A-Za-z])–(?=[A-Za-z])", blob):
        add("em_dash", "contains an em/en dash")

    # 2. banned filler / openers / AI tells / inflated / enthusiasm / gratitude / weird sign-off
    for pat in _BANNED_PATTERNS:
        if pat.search(blob):
            add("banned_phrase", pat.pattern[:48])
    for pat in _AI_TELLS:
        if pat.search(blob):
            add("ai_tell", pat.pattern[:32])
    for pat in _INFLATED:
        if pat.search(blob):
            add("inflated_vocab", pat.pattern.strip("\\b"))
    for pat in _FAKE_ENTHUSIASM:
        if pat.search(blob):
            add("fake_enthusiasm", pat.pattern[:24])
    for pat in _EXCESS_GRATITUDE:
        if pat.search(blob):
            add("excessive_gratitude", pat.pattern[:24])
    if _WEIRD_SIGNOFF.search(body):
        add("weird_signoff", "assistant-style sign-off")

    # 3. subject specific + human
    subj = (subject or "").strip()
    if is_generic_subject(subj):
        add("generic_subject", subj or "(empty)")
    elif len(subj) > 90:
        add("subject_too_long", f"{len(subj)} chars")

    # 4. facts grounded — numbers (canonicalized), spelled-out quantities, and multi-word entities
    grounding = brief.grounding_text()
    for c in _grounded_numbers(body, grounding):
        add("invented_fact", c)
    g_low = grounding.lower()
    for num_word, unit in _WORDQTY.findall(body):
        digit = _WORDNUM[num_word.lower()]
        if digit not in g_low and f"{num_word.lower()} {unit.lower()}" not in g_low:
            add("invented_fact", f"{num_word} {unit}")
    # flag a multi-word Title-Case entity only for its NON-stopword core words that aren't grounded, so a
    # greeting ("Hi Mr. Smith") or closing never trips it while a fabricated "Lincoln High" does.
    allowed_words = {w.lower() for w in re.findall(r"[A-Za-z][a-z]+", grounding)}
    for span in _TITLE_SPAN.findall(body):
        core = [w.strip(".") for w in span.split() if w.strip(".") not in _STOP_TITLE]
        ungrounded = [w for w in core if w.lower() not in allowed_words]
        if ungrounded:
            add("invented_entity", " ".join(ungrounded)[:60])

    # 5. fabricated prior interaction not present in the brief
    if _PRIOR_INTERACTION.search(body) and not _PRIOR_INTERACTION.search(grounding) \
            and not re.search(r"\b(discussed|call|conversation|spoke|met|agreed|mentioned)\b", g_low):
        add("invented_context", "claims a prior interaction not in the brief")

    # 6. attachment claim must match reality
    present = [a for a in brief.attachments if a.present]
    claims_attach = bool(_ATTACH_CLAIM.search(body))
    if claims_attach and not present:
        add("attachment_lie", "claims an attachment that isn't attached")
    if present and not claims_attach:
        add("attachment_unmentioned", present[0].name)

    # 7. recipient / relationship + wrong-recipient
    if not style.allow_slang and _SLANG.search(body):
        add("tone_mismatch", "slang in a non-casual email")
    if brief.recipient_name:
        salutation = (body.splitlines() or [""])[0]
        rtoks = [w.strip(".,").lower() for w in re.split(r"\s+", brief.recipient_name) if len(w.strip(".,")) > 1]
        if rtoks and not any(w in salutation.lower() for w in rtoks) and re.match(r"^\s*(hi|hey|hello|dear)\b",
                                                                                  salutation, re.IGNORECASE):
            add("wrong_recipient", "greeting does not name the intended recipient")

    # 8. clear call to action / next step
    if not HAS_ASK.search(body):
        add("no_cta", "no clear ask / next step")

    # 9. no duplicated sentences
    norm = [re.sub(r"\s+", " ", s.lower()) for s in _sentences(body) if len(s) > 12]
    if len(norm) != len(set(norm)):
        add("duplicate_sentence", "a sentence repeats")

    # 10. length proportional — no giant paragraph, no runaway email
    for para in re.split(r"\n\s*\n", body or ""):
        p = para.strip()
        if len(p) > 700 or len(_sentences(p)) > 6:
            add("giant_paragraph", f"{len(p)} chars in one block")
            break
    if len(body or "") > 1600:
        add("too_long", f"{len(body)} chars")

    # 11. names correct — the sign-off must be the real sender (guard a blank/whitespace sender_name)
    sender_toks = (brief.sender_name or "").split()
    if sender_toks and sender_toks[0].lower() not in blob.lower():
        add("missing_sender", brief.sender_name)

    # 12. no unsupported completion claim (reuse the runtime's completion-truth guard)
    if response_composer.claims_action_completion(body):
        add("unsupported_completion", "claims an action was completed")

    return QualityReport(ok=not issues, issues=tuple(issues))
