"""Tests for the HUMANIZER lint (deterministic, no network).

These lock in the two things that matter: AI tells get softened, and the spans that
carry grounding or identity (quoted titles, the student placeholder, greeting, sign-off)
are never touched.
"""

from bruce_engine.drafting import STUDENT_QUESTION_PLACEHOLDER
from bruce_engine.humanize import humanize_body


def test_replaces_stock_words():
    src = (
        "We utilize the data and delve into the realm of physics. "
        "The results underscore progress and were done meticulously. "
        "I want to leverage this method."
    )
    out = humanize_body(src)
    assert "use the data" in out
    assert "look at the area of physics" in out
    assert "results show progress" in out
    assert "carefully" in out
    assert "leverage" not in out and "use this method" in out
    # originals gone entirely (no protected spans in this input)
    for tell in ("utilize", "delve", "realm", "underscore", "meticulous"):
        assert tell not in out.lower()


def test_preserves_sentence_case_on_replacement():
    assert humanize_body("Utilize the corpus.") == "Use the corpus."
    assert humanize_body("Delve into the topic.") == "Look at the topic."


def test_cuts_filler_openers():
    src = (
        "I am reaching out to express my interest. "
        "It is worth noting that your lab is active. "
        "I wanted to take a moment to thank you."
    )
    out = humanize_body(src)
    assert "reaching out to" not in out.lower()
    assert "worth noting" not in out.lower()
    assert "take a moment" not in out.lower()
    # infinitive-openers are REPLACED (grammar kept), not deleted into a fragment
    assert out.startswith("I am writing to express my interest.")
    assert "I wanted to thank you." in out
    # clause-leading filler is deleted and the next word re-capitalized
    assert "Your lab is active." in out


def test_infinitive_opener_is_not_stranded():
    # Regression: deleting "I am reaching out to" used to strand the verb ("Inquire about...").
    out = humanize_body("I am reaching out to inquire about research opportunities.")
    assert out == "I am writing to inquire about research opportunities."
    assert not out.startswith("Inquire")


def test_quoted_title_left_byte_for_byte():
    # A double-quoted title packed with stock words must survive untouched.
    title = '"Leveraging Meticulous Realm Utilization to Delve into Data"'
    src = f'I read your paper {title} and want to utilize its method.'
    out = humanize_body(src)
    assert title in out                       # exact quoted span preserved
    assert "utilize its method" not in out    # ...but prose outside is still cleaned
    assert "use its method" in out


def test_single_quoted_span_preserved_and_contractions_untouched():
    src = "I can't utilize the 'delve into realm' phrase here."
    out = humanize_body(src)
    assert "'delve into realm'" in out   # single-quoted span untouched
    assert "can't" in out                # contraction apostrophe not mistaken for a quote
    assert "use" in out                  # unquoted 'utilize' still replaced


def test_placeholder_preserved_including_internal_em_dash():
    src = f"I have relevant skills. {STUDENT_QUESTION_PLACEHOLDER} I want to utilize them."
    out = humanize_body(src)
    assert STUDENT_QUESTION_PLACEHOLDER in out   # byte-for-byte, em-dash and all
    assert "—" in out                            # the placeholder's em-dash survived
    assert "use them" in out                     # surrounding prose still cleaned


def test_reduces_em_dashes_outside_protected_spans():
    src = "I work on optics — it is exciting — and want to help."
    out = humanize_body(src)
    assert "—" not in out
    assert "," in out
    assert "I work on optics" in out and "want to help" in out


def test_greeting_and_signature_are_never_touched():
    body = "\n\n".join(
        [
            "Dear Professor Realm,",  # 'Realm' here must NOT become 'Area'
            "I am reaching out to utilize a moment of your time.",
            "Best regards,\nDhruv Meticulous",  # name word must NOT be rewritten
        ]
    )
    out = humanize_body(body)
    assert out.startswith("Dear Professor Realm,")
    assert out.endswith("Best regards,\nDhruv Meticulous")
    # the middle line was still humanized
    assert "reaching out to" not in out.lower()
    assert "I am writing to use a moment" in out  # opener replaced (not deleted), 'utilize'->'use'


def test_full_assembled_body_matches_drafting_shape():
    # Mirrors drafting.draft_one's body assembly to prove integration safety.
    greeting = "Dear Professor Ying,"
    body = "\n\n".join(
        [
            greeting,
            "I am reaching out to express my interest and utilize this chance to delve into your findings.",
            'Your paper "Leveraging Meticulous Data" underscores a key result — it matters to me.',
            f"I have relevant skills. {STUDENT_QUESTION_PLACEHOLDER}",
            "Could we schedule a ~15-minute chat?",
            "Best regards,\nDhruv Jain",
        ]
    )
    out = humanize_body(body)
    # protected spans intact
    assert out.startswith(greeting)
    assert out.endswith("Best regards,\nDhruv Jain")
    assert STUDENT_QUESTION_PLACEHOLDER in out
    assert '"Leveraging Meticulous Data"' in out   # quoted title byte-for-byte
    assert "~15-minute chat" in out                # hyphen (not em-dash) untouched
    # tells cleaned in the prose
    assert "reaching out to" not in out.lower()
    assert "use this chance to look at your findings" in out
    assert "shows a key result" in out
    assert "—" not in out.replace(STUDENT_QUESTION_PLACEHOLDER, "")
