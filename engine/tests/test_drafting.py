"""Tests for drafting helpers (no network)."""

from bruce_engine.drafting import _strip_greeting


def test_strips_model_written_greeting():
    # the exact double-greeting failure seen in a live run
    assert (
        _strip_greeting("Hi Professor Ying, I'm Dhruv Jain, a high school researcher.")
        == "I'm Dhruv Jain, a high school researcher."
    )
    assert _strip_greeting("Dear Dr. Huo, my name is Dhruv.") == "My name is Dhruv."
    assert _strip_greeting("Hello Professor Smith, I am interested in your work.") == (
        "I am interested in your work."
    )


def test_leaves_normal_opening_untouched():
    assert (
        _strip_greeting("My name is Dhruv Jain and I study polaritons.")
        == "My name is Dhruv Jain and I study polaritons."
    )
    assert _strip_greeting("I recently read your paper.") == "I recently read your paper."
