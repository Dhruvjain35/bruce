"""Channel-aware technical renderer (Bite 1.6): LaTeX/Markdown -> readable plain text for iMessage,
with a fact-equivalence guard and a HARD no-raw-TeX/Markdown outbound gate. Generic across subjects;
no fixture-specific production logic. Covers the 25-category matrix + the live transition-matrix
regression from the HEIC test.
"""

from __future__ import annotations

import pytest

from bruce_engine.technical_render import (
    ExpressionEquivalenceError, UnsupportedPresentationToken, PLAIN_TEXT_CHANNELS,
    assert_channel_safe, assert_expression_equivalent, forbidden_tokens, is_technical_line,
    render_for_channel, to_readable,
)

IM = "self_hosted_imessage"


def r(text, **kw):
    return render_for_channel(text, channel=IM, **kw)


def _no_raw(text):
    assert forbidden_tokens(text) == [], f"raw tokens leaked: {forbidden_tokens(text)!r} in {text!r}"


# 25-CATEGORY MATRIX -------------------------------------------------------------------------------

def test_01_simple_algebra():
    out = r(r"$3x + 2 = 11$")
    assert "3x + 2 = 11" in out
    _no_raw(out)


def test_02_fractions():
    assert r(r"\(\frac{a}{b}\)") == "a/b"
    assert "(x + 1)/2" in r(r"\frac{x + 1}{2}")
    _no_raw(r(r"\frac{p}{q} + \frac{1}{2}"))


def test_03_square_roots():
    assert "√x" in r(r"\sqrt{x}")
    assert "√(x + 1)" in r(r"\sqrt{x + 1}")
    _no_raw(r(r"\sqrt{2}"))


def test_04_exponents_and_subscripts():
    assert "x²" in r("$x^2$")
    assert "10²³" in r(r"10^{23}")
    assert "vᵢ" in r("$v_i$")
    # f has no Unicode subscript -> readable underscore fallback, never a raw brace
    assert "v_f" in r("$v_f$")
    _no_raw(r(r"a_1 + b^{n+1}"))


def test_05_inequalities():
    assert "x ≤ 5" in r(r"x \leq 5")
    assert "a ≥ b" in r(r"a \geq b")
    assert "y ≠ 0" in r(r"y \neq 0")


def test_06_matrices_2x2_and_3x3():
    out = r(r"\begin{bmatrix} 0.60 & 0.40 \\ 0.45 & 0.55 \end{bmatrix}")
    assert "[ 0.60  0.40 ]" in out
    assert "[ 0.45  0.55 ]" in out
    out3 = r(r"\begin{bmatrix}1 & 2 & 3\\4 & 5 & 6\\7 & 8 & 9\end{bmatrix}")
    assert "[ 1  2  3 ]" in out3 and "[ 7  8  9 ]" in out3
    _no_raw(out)


def test_07_matrix_multiplication_alignment():
    out = r(r"T^2 = \begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}")
    assert "T² =" in out
    assert "[ 0.54    0.46   ]" in out       # entries padded to the 0.5175 / 0.4825 column widths
    assert "[ 0.5175  0.4825 ]" in out
    _no_raw(out)


def test_08_vectors():
    assert "⟨3, 4⟩" in r(r"v = \langle 3, 4 \rangle")


def test_09_systems_of_equations():
    out = r("2x + y = 7\nx - y = 2")
    assert "2x + y = 7" in out and "x - y = 2" in out
    _no_raw(out)


def test_10_piecewise():
    out = r(r"f(x) = \begin{cases} x^2 & x \geq 0 \\ -x & x < 0 \end{cases}")
    assert "x²,  x ≥ 0" in out
    assert "-x,  x < 0" in out
    _no_raw(out)


def test_11_derivatives():
    assert "dy/dx" in r(r"\frac{dy}{dx}")
    _no_raw(r(r"\frac{d}{dx} f(x)"))


def test_12_integrals():
    out = r(r"\int_0^1 x^2 \, dx")
    assert "∫" in out and "x²" in out
    assert "₀" in out and "¹" in out
    _no_raw(out)


def test_13_summations():
    out = r(r"\sum_{i=1}^{n} i")
    assert "∑" in out
    _no_raw(out)


def test_14_limits():
    out = r(r"\lim_{x \to 0} f(x)")
    assert "lim" in out and "→" in out
    _no_raw(out)


def test_15_probability_notation():
    out = r(r"$P(F \to N) = 0.40$")
    assert "P(F → N) = 0.40" in out
    _no_raw(out)


def test_16_statistics_notation():
    out = r(r"\bar{x} = 5 and \sigma^2 = 4")
    assert "σ²" in out and "5" in out and "4" in out
    _no_raw(out)


def test_17_physics_with_units():
    out = r(r"a = 9.8 \, m/s^2, \quad F = 12 N")
    assert "9.8" in out and "m/s²" in out and "12 N" in out
    _no_raw(out)


def test_18_chemical_equation():
    out = r(r"2H_2 + O_2 \to 2H_2O")
    assert "2H₂ + O₂ → 2H₂O" in out
    _no_raw(out)


def test_19_scientific_notation():
    out = r(r"6.02 \times 10^{23}")
    assert "6.02 × 10²³" in out
    _no_raw(out)


def test_20_long_derivation_stays_readable():
    # visual equation cards are a follow-up; for now a long derivation must still be raw-token-free.
    src = r"\frac{a}{b} = \frac{c}{d} \implies ad = bc \implies x = \frac{bc}{a}"
    _no_raw(r(src))


def test_21_malformed_latex_does_not_crash_or_leak_backslash():
    out = r(r"\frac{a}{ and \sqrt{ oops")   # unbalanced braces
    assert "\\" not in out                  # last-resort strip removes any surviving backslash


def test_22_markdown_mixed_with_math():
    out = r(r"**answer:** the value is \frac{1}{2}")
    assert "**" not in out
    assert "answer:" in out and "1/2" in out
    _no_raw(out)


def test_23_unicode_accessibility_fallback():
    # a subscript with no Unicode form must stay a readable underscore, never a raw brace or guess
    out = r(r"C_{eq} = 5")
    assert "C_(eq)" in out or "C_eq" in out
    assert "{" not in out and "}" not in out


def test_24_facts_preserved_before_and_after():
    # good render: numbers + relations identical -> no raise
    assert_expression_equivalent(r"x \leq 5 and y = 0.40", "x ≤ 5 and y = 0.40")
    # a render that changed a value must be caught
    with pytest.raises(ExpressionEquivalenceError):
        assert_expression_equivalent("the entry is 0.40", "the entry is 0.45")
    # a flipped inequality must be caught
    with pytest.raises(ExpressionEquivalenceError):
        assert_expression_equivalent("x <= 5", "x >= 5".replace("<=", "≤").replace(">=", "≥"))


def test_25_no_raw_tex_or_markdown_reaches_imessage():
    samples = [
        r"\[ E = mc^2 \]", r"**bold** and _ital_", r"\begin{bmatrix}1&2\\3&4\end{bmatrix}",
        r"\frac{1}{2} + \sqrt{2}", "# Heading\nbody", "| a | b |\n|---|---|\n| 1 | 2 |",
        r"\theta = \frac{\pi}{4}", r"P(A \cap B) = 0.5",
    ]
    for s in samples:
        _no_raw(r(s))


# HARD OUTBOUND VALIDATOR --------------------------------------------------------------------------

def test_validator_flags_raw_tex_and_markdown():
    assert "\\begin{" in forbidden_tokens(r"\begin{bmatrix}1\end{bmatrix}")
    assert "\\frac" in forbidden_tokens(r"\frac{1}{2}")
    assert "**" in forbidden_tokens("**bold**")
    assert forbidden_tokens("| a | b |\n|---|---|") != []


def test_validator_allows_backslash_in_explicit_code():
    # a code response may legitimately contain backslashes (regex, escapes)
    assert forbidden_tokens(r"re.match(r'\d+', s)", is_code=True) == []
    # but NOT when it isn't code
    assert forbidden_tokens(r"re.match(r'\d+', s)", is_code=False) != []


def test_assert_channel_safe_raises_only_for_plain_channels():
    with pytest.raises(UnsupportedPresentationToken):
        assert_channel_safe(r"\frac{1}{2}", channel=IM)
    # a non-plain channel is not gated here (e.g. a future rich channel)
    assert_channel_safe(r"\frac{1}{2}", channel="rich_web")


def test_render_passthrough_for_non_plain_channel():
    assert render_for_channel(r"\frac{1}{2}", channel="rich_web") == r"\frac{1}{2}"


# LIVE REGRESSION (the exact HEIC transition-matrix reply) ------------------------------------------

def test_live_transition_matrix_regression():
    # The model's raw reply from the live HEIC test (LaTeX + Markdown), rendered for iMessage.
    src = (
        "**b)** the entry in the **F row, N column** is $0.40$.\n\n"
        r"$$T^2 = \begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}$$"
        "\n\nso the answer is $0.54$, or 54%."
    )
    out = r(src)
    _no_raw(out)                              # 25: nothing raw reaches iMessage
    assert "**" not in out
    assert "0.40" in out                      # fact preserved
    assert "[ 0.54    0.46   ]" in out        # aligned matrix
    assert "[ 0.5175  0.4825 ]" in out
    assert "0.54, or 54%" in out
    # every original number survives
    for n in ("0.40", "0.54", "0.46", "0.5175", "0.4825", "54"):
        assert n in out


# is_technical_line (voice-pass variable-case protection) ------------------------------------------

def test_is_technical_line_protects_variable_case():
    assert is_technical_line("T = ...")
    assert is_technical_line("[ 0.60  0.40 ]")
    assert is_technical_line("2H₂ + O₂ → 2H₂O")   # has → relation
    assert not is_technical_line("look at the F row and N column")
    assert not is_technical_line("")
