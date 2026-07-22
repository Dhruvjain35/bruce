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
    # The model's raw reply from the live HEIC test (LaTeX + Markdown), rendered for iMessage — with
    # the CORRECT labels: T is the transition matrix, T² is its square.
    src = (
        "**b)** the entry in the **F row, N column** is $0.40$.\n\n"
        r"T = $$\begin{bmatrix} 0.60 & 0.40 \\ 0.45 & 0.55 \end{bmatrix}$$"
        "\n\nsquaring it:\n\n"
        r"T^2 = $$\begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}$$"
        "\n\nso the answer is $0.54$, or 54%."
    )
    out = r(src)
    _no_raw(out)
    assert "**" not in out
    # each label sits with ITS OWN matrix, in order
    assert out.index("T =") < out.index("[ 0.60  0.40 ]") < out.index("T² =") < out.index("[ 0.54    0.46   ]")
    assert "[ 0.45  0.55 ]" in out and "[ 0.5175  0.4825 ]" in out
    for n in ("0.40", "0.60", "0.54", "0.46", "0.5175", "0.4825", "54"):
        assert n in out


# LABEL-ASSOCIATION / STRUCTURAL PRESERVATION (the T/T² bug + guard strengthening) -----------------

def test_la1_T_and_Tsquared_correct_matrix_under_each_label():
    src = (r"T = \begin{bmatrix} 0.60 & 0.40 \\ 0.45 & 0.55 \end{bmatrix}" "\n\n"
           r"T^2 = \begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}")
    out = r(src)
    assert out.index("T =") < out.index("[ 0.60  0.40 ]")
    assert out.index("[ 0.45  0.55 ]") < out.index("T² =") < out.index("[ 0.54    0.46   ]")


def test_la2_A_Asquared_Ainverse_no_label_swap():
    src = (r"A = \begin{bmatrix}2 & 0\\0 & 2\end{bmatrix}" "\n\n"
           r"A^2 = \begin{bmatrix}4 & 0\\0 & 4\end{bmatrix}" "\n\n"
           r"A^{-1} = \begin{bmatrix}0.5 & 0\\0 & 0.5\end{bmatrix}")
    out = r(src)
    assert out.index("A =") < out.index("A² =") < out.index("A⁻¹ =")   # labels in order, none swapped
    assert out.index("A² =") < out.index("4")                          # 4 is unique to A²'s matrix
    assert out.index("A⁻¹ =") < out.index("0.5")                       # 0.5 is unique to A⁻¹'s matrix
    _no_raw(out)


def test_la3_x_and_xsquared_values_associated():
    out = r(r"if $x = 3$ then $x^2 = 9$")
    assert "x = 3" in out and "x² = 9" in out


def test_la4_force_mass_acceleration_do_not_swap():
    out = r(r"$F = 12 N$, $m = 3 kg$, $a = 4 m/s^2$")
    assert "F = 12 N" in out and "m = 3 kg" in out and "a = 4 m/s²" in out


def test_la5_multiple_equations_similar_numbers_keep_labels():
    out = r("a = 5\nb = 5\nc = 5")
    assert "a = 5" in out and "b = 5" in out and "c = 5" in out and out.count("= 5") == 3


def test_la6_units_stay_with_correct_value():
    out = r(r"$v = 3 m/s$ and $t = 5 s$")
    assert "v = 3 m/s" in out and "t = 5 s" in out


def test_la7_same_multiset_different_positions_not_equivalent():
    # transposed first row: same numbers, different positions -> MUST NOT be equivalent
    with pytest.raises(ExpressionEquivalenceError):
        assert_expression_equivalent(r"\begin{bmatrix}1 & 2\\3 & 4\end{bmatrix}", "[ 2  1 ]\n[ 3  4 ]")


def test_la8_reordered_blocks_fail_validation():
    with pytest.raises(ExpressionEquivalenceError):
        assert_expression_equivalent(
            r"A = \begin{bmatrix}1 & 2\end{bmatrix} then B = \begin{bmatrix}3 & 4\end{bmatrix}",
            "B =\n[ 3  4 ]\nA =\n[ 1  2 ]")


def test_la9_missing_label_fails_validation():
    with pytest.raises(ExpressionEquivalenceError):
        assert_expression_equivalent(r"T = \begin{bmatrix}1 & 2\\3 & 4\end{bmatrix}", "[ 1  2 ]\n[ 3  4 ]")


def test_la10_live_fixture_exact_output():
    src = (r"T = \begin{bmatrix} 0.60 & 0.40 \\ 0.45 & 0.55 \end{bmatrix}" "\n\n"
           r"T^2 = \begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}")
    expected = ("T =\n[ 0.60  0.40 ]\n[ 0.45  0.55 ]\n\n"
                "T² =\n[ 0.54    0.46   ]\n[ 0.5175  0.4825 ]")
    assert r(src) == expected


def test_la_present_pipeline_preserves_labels_and_case():
    # the full render -> voice-styling pipeline must keep variable case (T, not t) and alignment
    from bruce_engine.conversation_style import ConversationStyleEngine, VoiceProfile
    eng = ConversationStyleEngine()
    src = (r"T = \begin{bmatrix} 0.60 & 0.40 \\ 0.45 & 0.55 \end{bmatrix}" "\n\n"
           r"T^2 = \begin{bmatrix} 0.54 & 0.46 \\ 0.5175 & 0.4825 \end{bmatrix}")
    styled = eng.render(render_for_channel(src, channel=IM), protect_technical=True,
                        profile=VoiceProfile(lowercase=True))
    assert "T =" in styled and "T² =" in styled and "t =" not in styled.split("\n")[0]
    assert "[ 0.60  0.40 ]" in styled
    _no_raw(styled)


# is_technical_line (voice-pass variable-case protection) ------------------------------------------

def test_is_technical_line_protects_variable_case():
    assert is_technical_line("T = ...")
    assert is_technical_line("[ 0.60  0.40 ]")
    assert is_technical_line("2H₂ + O₂ → 2H₂O")   # has → relation
    assert not is_technical_line("look at the F row and N column")
    assert not is_technical_line("")


# P0.2 regressions — the exact live-failure shapes (guard false-positive collapse + ,quad + \text braces)

def test_p0_two_matrices_separated_by_quad_do_not_collapse():
    raw = (r"A = \[\begin{bmatrix} 3 & 4 \\ 1 & 2 \end{bmatrix},\quad "
           r"B = \begin{bmatrix} 1 & -2 \\ -0.5 & 1.5 \end{bmatrix}\]")
    out = r(raw)
    assert "[ 3  4 ]" in out and "[ 1  2 ]" in out            # matrix A keeps brackets + BOTH rows
    assert "[ 1     -2  ]" in out and "[ -0.5  1.5 ]" in out   # matrix B intact
    assert ",quad" not in out and "\\" not in out             # no lossy fallback corruption
    _no_raw(out)


def test_p0_quad_adjacent_to_nonspace_converts():
    assert to_readable(r"1\quad2") == "1 2"
    assert "\\quad" not in r(r"x\quad2\quad3") and "quad" not in r(r"x\quad2")


def test_p0_text_command_strips_its_braces():
    out = to_readable(r"\text{if } x \geq 0")
    assert "{" not in out and "}" not in out and "if" in out


def test_p0_matrix_followed_by_text_keeps_structure():
    out = r(r"A = \begin{bmatrix}1 & 2\\3 & 4\end{bmatrix} then B follows")
    assert "[ 1  2 ]" in out and "[ 3  4 ]" in out             # matrix not split by trailing text
    _no_raw(out)
