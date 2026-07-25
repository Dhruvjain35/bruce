"""Email quality layer — the deterministic floor produces a clean, grounded, real-person email for every
relationship and situation (teacher/coach/counselor/internship/support/friend, plus reply/attachment/apology/
follow-up/scheduling/clarification/declining), a slop draft from the model is REJECTED and replaced by the
floor, the validator catches every banned pattern, and corrections adjust the draft (and persist when asked).

The composer is profile + relationship + grounded brief + compact composer + deterministic validator + rewrite
fallback — not one fixed prompt. Fakes stand in for the model; the floor needs no model at all."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import email_compose, email_voice
from bruce_engine.email_brief import Attachment, EmailBrief, Relationship
from bruce_engine.email_quality import validate_email
from bruce_engine.email_voice import UserWritingProfile, apply_correction
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

FORBIDDEN = ("—", "i hope this email finds you well", "hope this finds you well", "wanted to reach out",
             "kindly", "at your earliest convenience", "please do not hesitate", "delve", "please find attached")


def _run(c):
    return asyncio.run(c)


def _forbidden_free(text: str):
    low = text.lower()
    return [p for p in FORBIDDEN if p in low]


# --- the varied scenarios: the deterministic floor is clean + grounded for each -------------------

SCENARIOS = {
    "teacher": EmailBrief(sender_name="Dhruv", recipient_name="Mr. Smith", recipient_relationship=Relationship.teacher,
                          purpose="missed the homework instructions in class",
                          requested_outcome="could you send me the assignment or tell me where it's posted",
                          source_context="I missed the homework instructions from class today"),
    "coach": EmailBrief(sender_name="Dhruv", recipient_name="Coach", recipient_relationship=Relationship.coach,
                        purpose="can't make practice today",
                        requested_outcome="I can't make practice today because I have an appointment at 4, but I'll be at the next one"),
    "counselor": EmailBrief(sender_name="Dhruv", recipient_name="Ms. Lee", recipient_relationship=Relationship.counselor,
                            purpose="want to add AP Bio next semester",
                            requested_outcome="could we meet this week to talk about adding AP Bio to my schedule"),
    "internship": EmailBrief(sender_name="Dhruv", recipient_name="Priya", recipient_relationship=Relationship.professional,
                             purpose="following up on the research assistant application",
                             requested_outcome="I wanted to ask if there's an update on the research assistant role",
                             source_context="I applied for the research assistant role two weeks ago"),
    "support": EmailBrief(sender_name="Dhruv", recipient_relationship=Relationship.support,
                          purpose="charged twice for the same order",
                          requested_outcome="can you refund the duplicate charge on order 4471",
                          facts=("order 4471",)),
    "friend": EmailBrief(sender_name="Dhruv", recipient_name="Sam", recipient_relationship=Relationship.peer,
                         purpose="see if they want to study saturday",
                         requested_outcome="wanna study for the bio test saturday afternoon"),
    "reply": EmailBrief(sender_name="Dhruv", recipient_name="Mr. Smith", recipient_relationship=Relationship.teacher,
                        purpose="answer the make-up date question", is_reply=True, thread_subject="Make-up exam",
                        requested_outcome="thursday after school works for the make-up, does that work for you"),
    "attachment": EmailBrief(sender_name="Dhruv", recipient_name="Priya", recipient_relationship=Relationship.professional,
                             purpose="send the updated resume",
                             requested_outcome="here's the updated resume, let me know if you need a different version",
                             attachments=(Attachment("the updated resume"),)),
    "apology": EmailBrief(sender_name="Dhruv", recipient_name="Coach", recipient_relationship=Relationship.coach,
                          purpose="apologize for missing the meet",
                          requested_outcome="sorry I missed the meet saturday, I mixed up the time, it won't happen again"),
    "followup": EmailBrief(sender_name="Dhruv", recipient_name="Priya", recipient_relationship=Relationship.professional,
                           purpose="follow up on my email from last week",
                           requested_outcome="just following up on my note from last week, is there any update"),
    "scheduling": EmailBrief(sender_name="Dhruv", recipient_name="Ms. Lee", recipient_relationship=Relationship.counselor,
                             purpose="set up a meeting",
                             requested_outcome="are you free tuesday or wednesday after school to meet"),
    "clarification": EmailBrief(sender_name="Dhruv", recipient_name="Mr. Smith", recipient_relationship=Relationship.teacher,
                                purpose="clarify the essay length",
                                requested_outcome="is the essay supposed to be 3 pages or 3 paragraphs",
                                facts=("3 pages", "3 paragraphs")),
    "declining": EmailBrief(sender_name="Dhruv", recipient_name="Sam", recipient_relationship=Relationship.peer,
                            purpose="decline the saturday plan",
                            requested_outcome="can't do saturday, I have work, but I'm free sunday"),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_floor_is_clean_and_grounded_for_every_scenario(name):
    brief = SCENARIOS[name]
    composed = _run(email_compose.compose_email(brief))          # no model -> the deterministic floor
    assert composed.report.ok, f"{name}: {composed.report.codes()}"
    assert not composed.used_model
    hits = _forbidden_free(composed.subject + "\n" + composed.body)
    assert not hits, f"{name}: forbidden {hits}"
    assert brief.sender_name.lower() in composed.body.lower()     # signed by the real sender
    assert composed.subject and composed.subject.lower() not in ("hi", "hello", "update", "question")


def test_floor_addresses_recipient_and_never_slangs_a_teacher():
    t = _run(email_compose.compose_email(SCENARIOS["teacher"]))
    assert "mr. smith" in t.body.lower() and t.body.lower().startswith("hi")
    assert "hey" not in t.body.lower() and " u " not in f" {t.body.lower()} "
    p = _run(email_compose.compose_email(SCENARIOS["friend"]))    # peer MAY be casual
    assert p.report.ok


# --- the model's slop is rejected and replaced by the floor ---------------------------------------

class _FakeModel:
    def __init__(self, subject, body, *, rewrite_to=None, raises=False):
        self._s, self._b, self._rw, self._raises = subject, body, rewrite_to, raises
        self.calls = 0
    async def draft(self, brief, profile, *, directives=()):
        if self._raises:
            raise RuntimeError("model down")
        return self._s, self._b
    async def rewrite(self, brief, profile, *, subject, body, issues, directives=()):
        self.calls += 1
        if self._rw is not None:
            return self._rw
        return subject, body        # stubbornly returns the same slop -> forces fallback


def test_model_slop_falls_back_to_floor():
    brief = SCENARIOS["teacher"]
    slop = ("Dear Mr. Smith, I hope this email finds you well. I wanted to reach out to kindly inquire "
            "about the assignment at your earliest convenience — please do not hesitate to advise.")
    composed = _run(email_compose.compose_email(brief, model=_FakeModel("Reaching out", slop)))
    assert composed.fell_back and not composed.used_model
    assert composed.report.ok and not _forbidden_free(composed.body)


def test_model_clean_draft_is_used():
    brief = SCENARIOS["teacher"]
    good = ("Hi Mr. Smith,\n\nI missed the homework instructions in class today. Could you send me the "
            "assignment or point me to where it's posted?\n\nThanks,\nDhruv")
    composed = _run(email_compose.compose_email(brief, model=_FakeModel("Missing homework instructions", good)))
    assert composed.used_model and not composed.fell_back and composed.report.ok


def test_model_em_dash_is_normalized_not_rejected():
    brief = SCENARIOS["support"]
    body = ("Hi,\n\nI was charged twice for order 4471 — can you refund the duplicate charge?\n\nThanks,\nDhruv")
    composed = _run(email_compose.compose_email(brief, model=_FakeModel("Duplicate charge on order 4471", body)))
    assert composed.used_model and "—" not in composed.body          # the one banned char is auto-fixed


def test_model_error_falls_back():
    composed = _run(email_compose.compose_email(SCENARIOS["coach"], model=_FakeModel("x", "y", raises=True)))
    assert composed.fell_back and composed.report.ok


# --- the validator catches each banned pattern ----------------------------------------------------

def test_validator_flags_forbidden_patterns():
    b = SCENARIOS["teacher"]
    def codes(subject, body):
        return validate_email(subject, body, b).codes()
    assert "em_dash" in codes("Homework", "Hi Mr. Smith, could you send it — thanks. Dhruv")
    assert "banned_phrase" in codes("Homework", "Hi Mr. Smith, i hope this email finds you well. could you send it? Dhruv")
    assert "generic_subject" in codes("Hello", "Hi Mr. Smith, could you send it? Dhruv")
    assert "attachment_lie" in codes("Resume", "Hi, please find attached the resume. Could you review it? Dhruv")
    assert "unsupported_completion" in codes("Done", "Hi Mr. Smith, I already added it to your calendar. Dhruv")
    assert "no_cta" in codes("Bio class", "Hi Mr. Smith, the class was interesting today. Dhruv")


def test_validator_flags_invented_fact():
    # body cites a room/time the brief never provided -> invented
    b = SCENARIOS["teacher"]
    r = validate_email("Homework", "Hi Mr. Smith, could you send the assignment for room 214 at 3:45? Thanks, Dhruv", b)
    assert "invented_fact" in r.codes()


def test_validator_passes_grounded_fact():
    b = SCENARIOS["clarification"]     # brief grounds "3 pages" and "3 paragraphs"
    r = validate_email("Essay length", "Hi Mr. Smith, is the essay 3 pages or 3 paragraphs? Thanks, Dhruv", b)
    assert r.ok, r.codes()


# --- corrections adjust the draft (and persist when asked) ----------------------------------------

def test_corrections_map_to_changes():
    p = UserWritingProfile(formality=4)
    less = apply_correction(p, "make it less formal")
    assert less.understood and less.profile.formality == 3 and not less.persist
    shorter = apply_correction(p, "keep it short")
    assert shorter.profile.sentence_length == "short"
    persistent = apply_correction(p, "always keep it less formal")
    assert persistent.persist and persistent.profile.formality == 3
    unknown = apply_correction(p, "add a smiley")               # unmapped -> passed through as a directive
    assert unknown.directives and not unknown.understood


def test_recipient_specific_override():
    p = UserWritingProfile(formality=2, recipient_style={"coach_smith": {"formality": 4}})
    assert p.for_recipient("coach_smith").formality == 4 and p.for_recipient(None).formality == 2


# --- voice memory persists (PG) -------------------------------------------------------------------

@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def test_writing_profile_round_trips(_pg):
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    assert _run(email_voice.get_writing_profile(uid)) == UserWritingProfile()   # empty default
    _run(email_voice.save_writing_profile(uid, UserWritingProfile(
        greeting="Hey", sign_off="Best", formality=2, disliked_phrases=("circle back",))))
    got = _run(email_voice.get_writing_profile(uid))
    assert got.greeting == "Hey" and got.sign_off == "Best" and got.formality == 2
    assert got.disliked_phrases == ("circle back",)
