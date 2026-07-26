"""Reply detection must be FACTUALLY correct. Built from two real production failures.

Both were one-line rules, and both shipped:

  1. "no SENT label" — a student replying to their OWN address produces a reply carrying SENT, so it was
     discarded forever. The mission polled to exhaustion in silence.
  2. "any message after ours" — a self-send is stored TWICE by Gmail (the outgoing copy and its delivered
     twin) with different storage ids but ONE RFC Message-ID. The twin was reported as a reply and Bruce
     texted "dhruvhydrox@gmail.com replied to the email i sent for you" when nobody had replied.

A Gmail message id is a STORAGE id; RFC Message-ID is the MESSAGE's identity. Detection is an RFC-header
question, and these tests are written against `select_reply` (pure) plus the fake adapter, which now
models the twin so a fake-only pass is impossible.
"""

from __future__ import annotations

import asyncio

from bruce_engine.gmail_adapter import FakeGmailAdapter, select_reply

ME = "student@gmail.com"
OTHER = "coach@school.edu"


def _run(c):
    return asyncio.run(c)


def _msg(mid, *, rfc, frm, to=ME, labels=None, in_reply_to=None, refs=None, seq=1):
    return {"id": mid, "threadId": "t", "labelIds": labels or ["INBOX"], "from": frm, "to": to,
            "subject": "s", "rfc_message_id": rfc, "in_reply_to": in_reply_to, "references": refs,
            "internal_date": str(1000 + seq)}


OURS = _msg("m1", rfc="<a@mail.gmail.com>", frm=ME, labels=["SENT"], seq=1)
TWIN = _msg("m1_twin", rfc="<a@mail.gmail.com>", frm=ME, labels=["SENT", "INBOX"], seq=2)
REPLY = _msg("r1", rfc="<b@mail.example>", frm=ME, in_reply_to="<a@mail.gmail.com>", seq=3)


# --- 1. the exact production false positive ---------------------------------------------------------

def test_sent_message_plus_inbox_twin_is_not_a_reply():
    """THE regression. The twin shares our RFC Message-ID, so it is our own message, not a reply."""
    assert select_reply([OURS, TWIN], after_message_id="m1") is None


def test_sent_message_alone_is_not_a_reply():
    assert select_reply([OURS], after_message_id="m1") is None


# --- 2. the genuine reply, in the same self-thread ---------------------------------------------------

def test_twin_plus_genuine_self_reply_detects_exactly_the_reply():
    """The student's own reply also carries SENT (same account), so labels cannot decide authorship.
    Header linkage can."""
    got = select_reply([OURS, TWIN, REPLY], after_message_id="m1")
    assert got is not None and got["id"] == "r1"


def test_self_reply_carrying_sent_label_is_still_detected():
    reply = dict(REPLY, labelIds=["SENT", "INBOX"])
    got = select_reply([OURS, TWIN, reply], after_message_id="m1")
    assert got is not None and got["id"] == "r1"


# --- 3. external recipient --------------------------------------------------------------------------

def test_external_recipient_reply_is_detected():
    ours = _msg("m1", rfc="<a@x>", frm=ME, to=OTHER, labels=["SENT"], seq=1)
    reply = _msg("r1", rfc="<c@y>", frm=OTHER, to=ME, in_reply_to="<a@x>", seq=2)
    got = select_reply([ours, reply], after_message_id="m1")
    assert got is not None and got["id"] == "r1"


# --- 4. duplicate polling ---------------------------------------------------------------------------

def test_repeated_polling_returns_the_same_reply_not_a_new_one():
    thread = [OURS, TWIN, REPLY]
    a = select_reply(thread, after_message_id="m1")
    b = select_reply(thread, after_message_id="m1")
    assert a["id"] == b["id"] == "r1"


# --- 5. forwarded / unrelated messages ---------------------------------------------------------------

def test_forward_in_the_same_thread_that_does_not_reference_us_is_ignored():
    fwd = _msg("f1", rfc="<f@x>", frm=OTHER, in_reply_to="<someone-else@x>", refs="<someone-else@x>", seq=4)
    assert select_reply([OURS, TWIN, fwd], after_message_id="m1") is None


def test_unrelated_later_message_with_no_linkage_is_ignored():
    later = _msg("u1", rfc="<u@x>", frm=OTHER, seq=9)          # no In-Reply-To / References at all
    assert select_reply([OURS, TWIN, later], after_message_id="m1") is None


# --- 6. fail closed ----------------------------------------------------------------------------------

def test_missing_headers_fails_closed():
    """No linkage to judge by means NO notification. A late notification is recoverable; a false one is
    a message the student already read."""
    headerless = {"id": "x1", "threadId": "t", "labelIds": ["INBOX"], "from": OTHER,
                  "rfc_message_id": None, "in_reply_to": None, "references": None, "internal_date": "9"}
    assert select_reply([OURS, headerless], after_message_id="m1") is None


def test_our_own_headers_unknown_fails_closed():
    ours_no_rfc = {"id": "m1", "threadId": "t", "labelIds": ["SENT"], "from": ME,
                   "rfc_message_id": None, "internal_date": "1"}
    stranger = _msg("s1", rfc="<s@x>", frm=OTHER, seq=5)
    assert select_reply([ours_no_rfc, stranger], after_message_id="m1") is None


# --- 7. restart must not re-detect --------------------------------------------------------------------

def test_a_consumed_reply_is_not_detected_again_after_restart():
    """The mission records which reply it acted on. A worker restart re-reading the thread must not treat
    the same message as new and notify twice."""
    assert select_reply([OURS, TWIN, REPLY], after_message_id="m1",
                        consumed_reply_ids=("r1",)) is None


def test_a_second_genuine_reply_after_a_consumed_one_is_detected():
    second = _msg("r2", rfc="<d@x>", frm=ME, in_reply_to="<a@mail.gmail.com>", seq=7)
    got = select_reply([OURS, TWIN, REPLY, second], after_message_id="m1", consumed_reply_ids=("r1",))
    assert got is not None and got["id"] == "r2"


# --- 8. our own follow-up sends -----------------------------------------------------------------------

def test_our_own_later_send_is_not_a_reply():
    second_send = _msg("m2", rfc="<a2@mail.gmail.com>", frm=ME, labels=["SENT"], seq=6)
    assert select_reply([OURS, TWIN, second_send], after_message_id="m1",
                        own_message_ids=("m2",)) is None


def test_latest_reply_wins_when_several_arrive():
    r2 = _msg("r2", rfc="<e@x>", frm=ME, in_reply_to="<a@mail.gmail.com>", seq=8)
    got = select_reply([OURS, TWIN, REPLY, r2], after_message_id="m1")
    assert got["id"] == "r2"


# --- 9. through the FAKE ADAPTER, which now models the twin -------------------------------------------

def test_fake_adapter_self_send_produces_a_twin_and_no_reply():
    """If the fake did not store the twin, the production bug would pass green here. It does store it."""
    async def go():
        fake = FakeGmailAdapter(account=ME)
        ref = await fake.send(to=ME, subject="s", body="b", thread_id=None, marker="mk1")
        thread = await fake.get_thread(ref.thread_id)
        assert len(thread) == 2, "a self-send must be stored twice, like Gmail does"
        assert len({m["rfc_message_id"] for m in thread}) == 1, "the twin shares our RFC Message-ID"
        assert await fake.find_reply(ref.thread_id, after_message_id=ref.message_id) is None
    _run(go())


def test_fake_adapter_detects_a_genuine_reply_in_a_self_thread():
    async def go():
        fake = FakeGmailAdapter(account=ME)
        ref = await fake.send(to=ME, subject="s", body="b", thread_id=None, marker="mk2")
        rid = fake.inject_incoming(ref.thread_id, from_addr=ME, subject="Re: s", body="yes",
                                   in_reply_to=fake.rfc_of(ref.message_id), labels=["SENT", "INBOX"])
        got = await fake.find_reply(ref.thread_id, after_message_id=ref.message_id)
        assert got is not None and got["id"] == rid
    _run(go())


def test_fake_adapter_external_thread_has_no_twin():
    async def go():
        fake = FakeGmailAdapter(account=ME)
        ref = await fake.send(to=OTHER, subject="s", body="b", thread_id=None, marker="mk3")
        assert len(await fake.get_thread(ref.thread_id)) == 1
        assert await fake.find_reply(ref.thread_id, after_message_id=ref.message_id) is None
    _run(go())
