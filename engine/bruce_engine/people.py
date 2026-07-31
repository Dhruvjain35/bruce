"""People the student told Bruce about — the only way a recipient gets resolved without being typed.

WHAT THIS CLOSES. `goal_slots.Fill.resolvable` says a recipient may be LOOKED UP and never invented, and
until now nothing could look one up: `turn_context.people` was a shape nobody populated,
`gmail.resolve_recipient` is live=False, and the only resolution in the tree was an address in the
current sentence or a self-reference. So a student naming a recipient by their RELATIONSHIP rather than
their address could only ever be answered with a question, however well Bruce understood the rest of it.

(Deliberately no example sentence here. tests/data/paraphrase_families.json is the generalization corpus,
and `test_corpus_is_not_referenced_by_production_code` fails if any of its phrasings appears in a
production module — including in a comment. A phrase sitting in the source is an invitation to write a
rule for it, which is the difference between understanding a student and recognising them.)

THE ONE RULE EVERYTHING HERE SERVES: never invent an address. Every consequence follows from it.

  * LEARNING IS DETERMINISTIC AND TRUSTED-TEXT-ONLY. A row exists because the student wrote "my teacher is
    ms alvarez, her email is alvarez@school.edu" — not because a model inferred it, not because an address
    appeared in a signature, and never from quoted or forwarded material. `decision_resolver` already owns
    the definition of the student's own words and is reused rather than reimplemented. A model could read
    an introduction more fluently; it could also read one where none exists, and the cost of that is mail
    to a stranger.
  * RESOLUTION ANSWERS WITH ONE PERSON OR ASKS. Two matches is a question, zero matches is a question, and
    a low-confidence row does not answer on its own. "The best match" is a guess wearing a ranking.
  * PRONOUNS NEED ACTIVE CONTEXT. "her" resolves only when the recent conversation makes exactly one
    person the obvious referent. A pronoun against a full address book is the same guess with worse odds.

CORRECTION AND FORGETTING ARE NOT DELETES. A correction writes a new row and points the old one at it; a
forget stamps `forgotten_at`. Resolution reads neither, and both stay readable afterwards, because an
address is acted on far from the turn that supplied it and "why did Bruce think that" has to survive.

Nothing here logs a name, a relationship or an address. Ids, counts and outcomes only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from . import decision_resolver, schema
from .db import user_session

log = logging.getLogger("bruce.people")   # CONTENT-FREE: ids, counts, outcomes — never a name or address

# Resolution outcomes. Typed and closed, like every other refusal vocabulary in this tree.
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"          # several people match — ask which
NOT_FOUND = "not_found"          # nobody matches — ask who
NO_REFERENT = "no_referent"      # the turn names nobody at all

# A low-confidence row is still kept — it IS what the student said — but it does not answer on its own.
MIN_RESOLVE_CONFIDENCE = 0.6

_ADDRESS = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# "my teacher", "my coach" — a RELATIONSHIP the student claims. Deliberately requires the possessive:
# "the teacher" is somebody in a story, "my teacher" is a person in this student's life.
_RELATION = re.compile(r"\bmy\s+(?P<relation>[a-z][a-z\s-]{1,24}?)\b(?=\s|$|[,.:;!?])", re.IGNORECASE)

# The introduction shapes. Each requires a NAME or a RELATION *and* an address in the same trusted
# sentence — an address on its own is a recipient for this turn, not a fact about a person.
_INTROS = (
    # "my teacher is ms alvarez, her email is alvarez@school.edu"
    re.compile(r"\bmy\s+(?P<relation>[a-z][a-z\s-]{1,24}?)\s+is\s+(?P<name>[^,.;\n]{1,60}?)"
               r"(?:\s*[,;]|\s+and\b|\s*$|\s+(?:her|his|their)\b)", re.IGNORECASE),
    # "my teacher is alvarez@school.edu"  /  "my teacher's email is ..."
    re.compile(r"\bmy\s+(?P<relation>[a-z][a-z\s-]{1,24}?)(?:'s)?\s+(?:email\s+)?is\b", re.IGNORECASE),
    # "sam is sam@example.com"  /  "Sam's email is sam@example.com". Case-INSENSITIVE on purpose: a
    # sixteen-year-old types "sam is sam@example.com", and a rule that needed the capital would learn
    # nothing from the way people actually write. `_NOT_A_NAME` is what keeps "the"/"it" out.
    re.compile(r"(?<!my )\b(?P<name>[\w.'-]{2,30}(?:\s+[\w.'-]{2,30})?)(?:'s)?\s+"
               r"(?:email\s+(?:address\s+)?)?is\b", re.IGNORECASE),
)

_FORGET = re.compile(r"\b(?:forget|remove|delete|drop)\b[^.\n]{0,30}?\b(?:about\s+)?"
                     r"(?P<who>my\s+[a-z][a-z\s-]{1,24}|[A-Z][\w.'-]{1,30})", re.IGNORECASE)

_PRONOUN = re.compile(r"\b(?:her|him|them|she|he|they)\b", re.IGNORECASE)

# Words that are never a person's name, however the sentence is shaped.
_NOT_A_NAME = frozenset({
    "email", "e-mail", "mail", "message", "it", "this", "that", "the", "a", "an", "my", "his", "her",
    "their", "them", "him", "she", "he", "they", "there", "here", "subject", "body", "address"})


def normalize_name(name: str | None) -> str:
    """Match on words, not on punctuation or honorifics. "Ms. Alvarez" and "ms alvarez" are one person,
    and a student who typed one will later type the other."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    words = [w for w in text.split() if w and w not in ("mr", "mrs", "ms", "miss", "dr", "prof",
                                                        "professor", "coach")]
    return " ".join(words).strip()


def _clean_relation(raw: str | None) -> str:
    rel = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    return "" if rel in _NOT_A_NAME else rel[:120]


# Tokens that are part of the SENTENCE, not part of the name. "sam's email is ..." names sam, and a rule
# that kept the whole phrase would store a person called "sam's email" and never match "sam" again.
_NAME_TAIL = frozenset({"email", "e-mail", "mail", "address", "addr"})


def _clean_name(raw: str | None) -> str:
    name = re.sub(r"\s+", " ", str(raw or "").strip(" ,.;:'\"")).strip()
    words = [w for w in name.split() if w]
    while words and words[-1].lower().strip("'s") in _NAME_TAIL:
        words.pop()
    while words and words[0].lower() in ("the", "my", "a", "an"):
        words.pop(0)
    name = " ".join(words).rstrip("'s ").strip(" ,.;:'\"")
    return "" if not name or name.lower() in _NOT_A_NAME else name[:200]


@dataclass(frozen=True)
class Learned:
    """One person the student introduced, with the words that introduced them."""

    name: str
    relationship: str
    email: str
    stated_span: str
    confidence: float


def extract(text: str | None) -> list[Learned]:
    """Introductions in the student's OWN words. Deterministic; no model, no I/O.

    An address is required, and it must appear in the trusted text — quoted and forwarded regions are cut
    first, so a signature block in something the student pasted can never become a person Bruce writes to.
    A message with an address but no name or relationship teaches nothing: that is a recipient for this
    turn, not a fact worth keeping.
    """
    trusted = decision_resolver.strip_inline_quotes(decision_resolver.trusted_reply_text(text))
    addresses = list(dict.fromkeys(_ADDRESS.findall(trusted)))
    if len(addresses) != 1:
        # Two addresses in one introduction is ambiguous about which belongs to whom, and guessing here
        # writes a durable wrong answer rather than a recoverable one.
        return []
    email = addresses[0]

    name, relation, confidence = "", "", 0.0
    m = _INTROS[0].search(trusted)
    if m:
        relation = _clean_relation(m.group("relation"))
        name = _clean_name(m.group("name"))
        confidence = 1.0 if (relation and name) else 0.0
    if not relation:
        m = _INTROS[1].search(trusted)
        if m:
            relation = _clean_relation(m.group("relation"))
            confidence = max(confidence, 0.9 if relation else 0.0)
    if not name:
        for m in _INTROS[2].finditer(trusted):
            candidate = _clean_name(m.group("name"))
            if candidate and normalize_name(candidate):
                name = candidate
                confidence = max(confidence, 0.9)
                break
    if not (name or relation):
        return []
    return [Learned(name=name or relation, relationship=relation, email=email,
                    stated_span=trusted.strip()[:500], confidence=confidence or 0.9)]


# --- storage ------------------------------------------------------------------------------------------


async def learn(user_id: UUID, text: str | None, *, source_message_id: str | None = None) -> list[str]:
    """Persist what this turn introduced. Returns the ids written (content-free, so it can be logged).

    A CORRECTION SUPERSEDES rather than edits. "actually her email is X" writes a new row and points every
    live row for that person at it, so the address Bruce used last week is still answerable — which is the
    only way to explain a message that already went out.
    """
    people = extract(text)
    if not people:
        return []
    written: list[str] = []
    now = datetime.now(timezone.utc)
    async with user_session(user_id) as s:
        for p in people:
            row = schema.KnownPerson(
                user_id=user_id, name=p.name, normalized_name=normalize_name(p.name),
                relationship=p.relationship or None, email=p.email,
                source_message_id=source_message_id, stated_span=p.stated_span,
                provenance="user_stated", confidence=p.confidence)
            s.add(row)
            await s.flush()
            # Supersede any LIVE row for the same person — matched on the normalized name OR the stated
            # relationship, because "my teacher is now mr diaz" is a correction about the same slot in the
            # student's life even though the name changed.
            conditions = [schema.KnownPerson.normalized_name == normalize_name(p.name)]
            if p.relationship:
                conditions.append(schema.KnownPerson.relationship == p.relationship)
            from sqlalchemy import or_
            await s.execute(
                update(schema.KnownPerson)
                .where(schema.KnownPerson.user_id == user_id,
                       schema.KnownPerson.id != row.id,
                       schema.KnownPerson.superseded_by_id.is_(None),
                       schema.KnownPerson.forgotten_at.is_(None),
                       or_(*conditions))
                .values(superseded_by_id=row.id, updated_at=now))
            written.append(str(row.id))
    log.info("people_learned user=%s count=%d", user_id, len(written))
    return written


async def forget(user_id: UUID, text: str | None) -> int:
    """Stop resolving a person the student asked Bruce to forget. Returns how many rows were retired.

    Stamped, never deleted: forgetting removes them from RESOLUTION and leaves the audit trail, so a
    message that already went out can still be explained.
    """
    m = _FORGET.search(decision_resolver.trusted_reply_text(text) or "")
    if not m:
        return 0
    who = m.group("who")
    relation = _clean_relation(re.sub(r"^my\s+", "", who, flags=re.IGNORECASE)) if who.lower().startswith("my ") else ""
    name = normalize_name(who)
    now = datetime.now(timezone.utc)
    async with user_session(user_id) as s:
        from sqlalchemy import or_
        conditions = [schema.KnownPerson.normalized_name == name] if name else []
        if relation:
            conditions.append(schema.KnownPerson.relationship == relation)
        if not conditions:
            return 0
        result = await s.execute(
            update(schema.KnownPerson)
            .where(schema.KnownPerson.user_id == user_id,
                   schema.KnownPerson.forgotten_at.is_(None),
                   or_(*conditions))
            .values(forgotten_at=now))
    log.info("people_forgotten user=%s count=%s", user_id, result.rowcount)
    return int(result.rowcount or 0)


async def _live(user_id: UUID) -> list[schema.KnownPerson]:
    """Everyone still resolvable: not superseded, not forgotten."""
    async with user_session(user_id) as s:
        rows = (await s.execute(
            select(schema.KnownPerson).where(
                schema.KnownPerson.user_id == user_id,
                schema.KnownPerson.superseded_by_id.is_(None),
                schema.KnownPerson.forgotten_at.is_(None))
            .order_by(schema.KnownPerson.created_at.desc()))).scalars().all()
    return list(rows)


# --- resolution ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    status: str
    email: str = ""
    name: str = ""
    candidates: tuple[str, ...] = ()      # NAMES only — a question names people, never addresses

    @property
    def resolved(self) -> bool:
        return self.status == RESOLVED


def _matches(rows, referent: str) -> list:
    """People this referent could mean. Relationship first, then name — "my teacher" is a stronger claim
    than a name that happens to appear in the sentence."""
    text = decision_resolver.normalize(referent)
    if not text:
        return []
    by_relation = [r for r in rows if r.relationship
                   and re.search(r"\bmy\s+" + re.escape(r.relationship) + r"\b", text)]
    if by_relation:
        return by_relation
    return [r for r in rows
            if r.normalized_name and re.search(r"\b" + re.escape(r.normalized_name) + r"\b", text)]


async def resolve(user_id: UUID, text: str | None, *, recent_text: str = "") -> Resolution:
    """WHO this turn means, or a question. Never an invented address.

    `recent_text` is the active conversation, and it is the ONLY thing a pronoun may resolve against. "her"
    against a whole address book is a guess with worse odds than a name; "her" one turn after the student
    said "my teacher is ms alvarez" is the obvious referent and nothing else in the window competes.
    """
    rows = await _live(user_id)
    if not rows:
        return Resolution(NOT_FOUND)
    trusted = decision_resolver.strip_inline_quotes(decision_resolver.trusted_reply_text(text))

    hits = _matches(rows, trusted)
    if not hits and _PRONOUN.search(trusted or ""):
        # A PRONOUN, so the referent must come from the window rather than from the address book.
        hits = _matches(rows, recent_text)
        if len(hits) != 1:
            return Resolution(AMBIGUOUS if len(hits) > 1 else NO_REFERENT,
                              candidates=tuple(r.name for r in hits[:4]))
    if not hits:
        return Resolution(NO_REFERENT)
    confident = [r for r in hits if (r.confidence or 0.0) >= MIN_RESOLVE_CONFIDENCE]
    if len(confident) != 1:
        return Resolution(AMBIGUOUS if len(confident) > 1 else NOT_FOUND,
                          candidates=tuple(r.name for r in hits[:4]))
    person = confident[0]
    log.info("people_resolved user=%s id=%s", user_id, person.id)
    return Resolution(RESOLVED, email=person.email, name=person.name)


def clarifying_question(res: Resolution) -> str:
    """The ONE question an unresolved referent owes. Names people, never addresses — a student who has to
    be told an address back has not been asked anything useful."""
    if res.status == AMBIGUOUS and res.candidates:
        return f"which one do u mean, {', '.join(res.candidates)}?"
    return "who should i send it to?"
