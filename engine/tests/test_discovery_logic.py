"""Tests for the pure/deterministic discovery helpers (no network).

These exercise only the offline parsing/normalization helpers that turn raw OpenAlex
payloads into grounded fields — nothing here touches the API.
"""

from __future__ import annotations

from bruce_engine.discovery import (
    _is_preprint_doi,
    _norm_title,
    _person_like,
    _short_id,
    reconstruct_abstract,
)


# ---------- reconstruct_abstract ----------


def test_reconstruct_abstract_orders_tokens_by_position():
    # dict insertion order is scrambled on purpose; output must follow positions, not keys
    inv = {"world": [2], "Hello": [0], "brave": [1]}
    assert reconstruct_abstract(inv) == "Hello brave world"


def test_reconstruct_abstract_handles_repeated_tokens():
    inv = {"the": [0, 2], "cat": [1], "sat": [3]}
    assert reconstruct_abstract(inv) == "the cat the sat"


def test_reconstruct_abstract_none_and_empty_return_none():
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_reconstruct_abstract_single_token():
    assert reconstruct_abstract({"Solo": [0]}) == "Solo"


# ---------- _person_like ----------


def test_person_like_accepts_real_names():
    assert _person_like("John Smith") is True
    assert _person_like("Jane A. Doe") is True  # single-letter middle ignored, two real tokens
    assert _person_like("María García") is True  # accented latin range


def test_person_like_rejects_single_token_orgs_and_junk():
    assert _person_like("inquantio") is False  # one token, lowercase org/garble
    assert _person_like("Consortium") is False  # single token, even if capitalized


def test_person_like_rejects_all_lowercase_two_token():
    # two tokens but no capitalization -> looks like a consortium/garble, not a person
    assert _person_like("quantum collaboration") is False


def test_person_like_rejects_none_and_empty():
    assert _person_like(None) is False
    assert _person_like("") is False


def test_person_like_needs_two_multichar_tokens():
    # "J Smith" -> "J" dropped (len 1), only one usable token -> reject
    assert _person_like("J Smith") is False


# ---------- _norm_title ----------


def test_norm_title_strips_punctuation_and_lowercases():
    assert _norm_title("Attention Is All You Need") == "attentionisallyouneed"
    assert _norm_title("Deep Learning: A Study!") == "deeplearningastudy"


def test_norm_title_keeps_digits():
    assert _norm_title("GPT-4 in 2024") == "gpt4in2024"


def test_norm_title_dedup_key_ignores_formatting_differences():
    # the whole point: two renderings of the same title collapse to one key
    assert _norm_title("The Cat, Sat.") == _norm_title("the cat sat")


# ---------- _is_preprint_doi ----------


def test_is_preprint_doi_detects_arxiv_and_zenodo():
    assert _is_preprint_doi("10.48550/arxiv.2401.00001") is True
    assert _is_preprint_doi("10.5281/zenodo.1234567") is True


def test_is_preprint_doi_false_for_published_doi():
    assert _is_preprint_doi("10.1038/s41586-024-00001-2") is False


def test_is_preprint_doi_false_for_none_and_empty():
    assert _is_preprint_doi(None) is False
    assert _is_preprint_doi("") is False


# ---------- _short_id ----------


def test_short_id_strips_openalex_prefix():
    assert _short_id("https://openalex.org/A5023888391") == "A5023888391"
    assert _short_id("https://openalex.org/W999") == "W999"


def test_short_id_passthrough_when_no_slash():
    assert _short_id("A5023888391") == "A5023888391"


def test_short_id_none_returns_none():
    assert _short_id(None) is None
