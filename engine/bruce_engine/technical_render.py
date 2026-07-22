"""Channel-aware technical-content renderer (Bite 1.6) — PRESENTATION ONLY.

The conversation model writes ``user_visible_response`` as prose that can contain LaTeX and Markdown.
iMessage/SMS render neither, so raw ``\\begin{bmatrix}`` / ``\\frac`` / ``**bold**`` reach the student
as broken noise even when the reasoning is correct. This module turns that into readable plain text /
Unicode for plain-text channels — WITHOUT ever changing a value, sign, unit, variable, matrix entry,
exponent, relation direction, probability, or conclusion.

Three guarantees:
  * ``to_readable``            — LaTeX/Markdown -> plain/Unicode (pure, deterministic, generic).
  * ``assert_expression_equivalent`` — the signed-number multiset (super/subscript digits normalized
    to ASCII) AND the relation-direction multiset must match before/after. Any mismatch means the
    render might have altered an answer, so ``render_for_channel`` FALLS BACK to a delimiter-stripped
    canonical form rather than risk it.
  * ``forbidden_tokens``      — a HARD outbound gate: no raw TeX/Markdown token may reach a plain-text
    channel. ``render_for_channel`` repairs; ``assert_channel_safe`` refuses.

Generic across every subject (arithmetic … linear algebra … physics … chemistry … finance). NO
subject-, variable-, or fixture-specific logic lives here.
"""

from __future__ import annotations

import re

# Channels that render plain text only (no Markdown, no LaTeX). iMessage/SMS today.
PLAIN_TEXT_CHANNELS = frozenset({"self_hosted_imessage", "imessage", "sms"})


class ExpressionEquivalenceError(Exception):
    """Rendering changed a numeric value / sign / relation direction — content-free message."""


class UnsupportedPresentationToken(Exception):
    """A raw TeX/Markdown token survived for a plain-text channel — content-free message."""


# --------------------------------------------------------------------------------------------------
# Symbol tables. Each LaTeX control word maps to its Unicode equivalent. Presentation only: these are
# never inserted or dropped, only substituted 1:1, so no value can change.
# --------------------------------------------------------------------------------------------------
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_OPS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "*", "star": "⋆",
    "bullet": "•", "oplus": "⊕", "otimes": "⊗",
}
_RELATIONS = {
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "sim": "~", "cong": "≅", "propto": "∝", "to": "→", "rightarrow": "→",
    "Rightarrow": "⇒", "longrightarrow": "→", "leftarrow": "←", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "mapsto": "↦", "implies": "⇒", "iff": "⇔",
}
_BIG = {
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "iint": "∬", "partial": "∂",
    "nabla": "∇", "infty": "∞", "sqrt": "√",  # sqrt is handled specially before this table
}
_SETS = {
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨",
}
_MISC = {
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "prime": "′", "cdots": "⋯",
    "ldots": "…", "dots": "…", "vdots": "⋮", "langle": "⟨", "rangle": "⟩", "hbar": "ℏ",
    "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "circ": "∘",
}
_SYMBOLS: dict[str, str] = {**_GREEK, **_OPS, **_RELATIONS, **_BIG, **_SETS, **_MISC}

# Spacing / grouping control words that become a single space or drop entirely.
_SPACE_CMDS = ("quad", "qquad", "thinspace", "medspace", "thickspace", "space")
_DROP_CMDS = ("left", "right", "big", "Big", "bigg", "Bigg", "displaystyle", "textstyle", "limits",
              "mathrm", "mathbf", "mathit", "text", "operatorname", "boldsymbol")
# Named operators/functions that render as their plain word (backslash dropped, value unchanged).
_FUNCTIONS = ("lim", "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "arcsin",
              "arccos", "arctan", "log", "ln", "exp", "max", "min", "det", "gcd", "lcm", "mod",
              "arg", "deg", "dim", "ker", "sgn")
# Accents -> base char + combining mark (x-bar, v-vector, x-hat, …).
_ACCENTS = {"bar": "̄", "overline": "̄", "hat": "̂", "widehat": "̂",
            "vec": "⃗", "tilde": "̃", "dot": "̇", "ddot": "̈"}
_CASES_ENV = re.compile(r"\\begin\s*\{cases\}(.*?)\\end\s*\{cases\}", re.DOTALL)


def _render_cases(body: str) -> str:
    rows = [r for r in re.split(r"\\\\", body) if r.strip() != ""]
    out = []
    for row in rows:
        cells = [_convert(c).strip() for c in re.split(r"&", row)]
        out.append("  " + ",  ".join(c for c in cells if c))
    return "\n" + "\n".join(out)


def _apply_accent(mark: str, body: str) -> str:
    body = body.strip()
    return (body[0] + mark + body[1:]) if body else body

_SUP = {**{str(d): c for d, c in zip(range(10), "⁰¹²³⁴⁵⁶⁷⁸⁹")},
        "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
        "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ",
        "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ",
        "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ", " ": " "}
_SUB = {**{str(d): c for d, c in zip(range(10), "₀₁₂₃₄₅₆₇₈₉")},
        "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
        "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ",
        "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ",
        "x": "ₓ", " ": " "}
# Reverse maps: Unicode super/subscript digits -> ASCII, so the equivalence guard sees "x²" as a 2.
_SUPSUB_DIGIT_TO_ASCII = {c: str(d) for d, c in zip(range(10), "⁰¹²³⁴⁵⁶⁷⁸⁹")}
_SUPSUB_DIGIT_TO_ASCII.update({c: str(d) for d, c in zip(range(10), "₀₁₂₃₄₅₆₇₈₉")})


def _map_script(body: str, table: dict[str, str]) -> str | None:
    """Return the sub/superscript rendering of ``body`` if EVERY char maps, else None (caller keeps a
    readable ``^(...)`` / ``_(...)`` form so nothing becomes ambiguous)."""
    out = []
    for ch in body:
        m = table.get(ch)
        if m is None:
            return None
        out.append(m)
    return "".join(out)


def _needs_parens(s: str) -> bool:
    s = s.strip()
    return len(s) > 1 and bool(re.search(r"[+\-*/×·÷ ]", s))


def _frac(num: str, den: str) -> str:
    n = f"({num.strip()})" if _needs_parens(num) else num.strip()
    d = f"({den.strip()})" if _needs_parens(den) else den.strip()
    return f"{n}/{d}"


def _superscript(body: str) -> str:
    body = body.strip()
    mapped = _map_script(body, _SUP)
    if mapped is not None:
        return mapped
    return f"^({body})" if _needs_parens(body) or len(body) > 1 else f"^{body}"


def _subscript(body: str) -> str:
    body = body.strip()
    mapped = _map_script(body, _SUB)
    if mapped is not None:
        return mapped
    return f"_({body})" if _needs_parens(body) or len(body) > 1 else f"_{body}"


_MATRIX_ENV = re.compile(r"\\begin\s*\{(b|p|v|B|V|)matrix\}(.*?)\\end\s*\{\1matrix\}", re.DOTALL)
_ARRAY_ENV = re.compile(r"\\begin\s*\{array\}\s*(?:\{[^{}]*\})?(.*?)\\end\s*\{array\}", re.DOTALL)


def _render_matrix(body: str, bracket: str) -> str:
    rows = [r for r in re.split(r"\\\\", body) if r.strip() != ""]
    grid = [[_convert(cell).strip() for cell in re.split(r"&", row)] for row in rows]
    if not grid:
        return ""
    ncol = max(len(r) for r in grid)
    grid = [r + [""] * (ncol - len(r)) for r in grid]
    widths = [max(len(r[c]) for r in grid) for c in range(ncol)]
    lb, rb = {"b": ("[", "]"), "p": ("(", ")"), "v": ("|", "|"), "": ("[", "]")}.get(bracket.lower(), ("[", "]"))
    lines = [f"{lb} " + "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r)) + f" {rb}" for r in grid]
    return "\n" + "\n".join(lines)


def _convert(text: str) -> str:
    """The core LaTeX -> plain/Unicode transform. Deterministic 1:1 substitution + structural layout;
    never inserts or removes a value."""
    # 1) environments (matrices / arrays / piecewise) first — recurse into their cells via _convert.
    text = _MATRIX_ENV.sub(lambda m: _render_matrix(m.group(2), m.group(1)), text)
    text = _ARRAY_ENV.sub(lambda m: _render_matrix(m.group(1), "b"), text)
    text = _CASES_ENV.sub(lambda m: _render_cases(m.group(1)), text)

    # 2) \frac and \sqrt, innermost-first (the [^{}] class matches only brace-free = innermost groups).
    frac_re = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
    sqrt_n_re = re.compile(r"\\sqrt\s*\[([^\][]*)\]\s*\{([^{}]*)\}")
    sqrt_re = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
    for _ in range(40):  # bounded: each pass resolves one nesting level
        new = frac_re.sub(lambda m: _frac(m.group(1), m.group(2)), text)
        new = sqrt_n_re.sub(lambda m: f"{_superscript(m.group(1))}√"
                            + (f"({m.group(2).strip()})" if _needs_parens(m.group(2)) else m.group(2).strip()), new)
        new = sqrt_re.sub(lambda m: "√" + (f"({m.group(1).strip()})" if _needs_parens(m.group(1)) else m.group(1).strip()), new)
        if new == text:
            break
        text = new

    # 2b) accents: \bar{x} -> x̄, \vec{v} -> v⃗ (base char + combining mark).
    for name, mark in _ACCENTS.items():
        text = re.sub(rf"\\{name}\s*\{{([^{{}}]*)\}}", lambda m, mk=mark: _apply_accent(mk, m.group(1)), text)
        text = re.sub(rf"\\{name}\s+([A-Za-z0-9])", lambda m, mk=mark: _apply_accent(mk, m.group(1)), text)

    # 3) super/subscripts: braced groups then single tokens.
    text = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _superscript(m.group(1)), text)
    text = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _subscript(m.group(1)), text)
    text = re.sub(r"\^\s*(\\?[A-Za-z0-9])", lambda m: _superscript(m.group(1).lstrip("\\")), text)
    text = re.sub(r"_\s*(\\?[A-Za-z0-9])", lambda m: _subscript(m.group(1).lstrip("\\")), text)

    # 4) drop grouping/formatting commands (keep their argument) and spacing commands.
    for c in _DROP_CMDS:
        text = re.sub(rf"\\{c}\b", "", text)
    for c in _SPACE_CMDS:
        text = re.sub(rf"\\{c}\b", " ", text)
    text = re.sub(r"\\[,;:> ]", " ", text)   # thin spaces
    text = text.replace("\\!", "")

    # 5) named functions -> plain word (drop backslash), then symbol control words -> Unicode.
    text = re.sub(r"\\(" + "|".join(_FUNCTIONS) + r")\b", r"\1", text)

    def _sym(m: re.Match) -> str:
        return _SYMBOLS.get(m.group(1), m.group(0))
    text = re.sub(r"\\([A-Za-z]+)", _sym, text)

    # 6) delimiters: math-mode markers vanish; escaped braces/&/% become literal.
    text = text.replace("\\[", "\n").replace("\\]", "\n").replace("\\(", "").replace("\\)", "")
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\([{}&%#_])", r"\1", text)
    return text


# --------------------------------------------------------------------------------------------------
# Markdown -> plain
# --------------------------------------------------------------------------------------------------
def _strip_markdown(text: str, *, is_code: bool) -> str:
    # fenced code blocks: keep the inner lines; drop the ``` fences. (Backslashes inside are left as-is
    # when is_code, so the validator won't flag legitimate code.)
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: m.group(1), text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)                      # inline code
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # ATX headings
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)               # **bold**
    text = re.sub(r"__([^_]+)__", r"\1", text)                   # __bold__
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)  # *italic*
    text = _convert_md_tables(text)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "• ", text, flags=re.MULTILINE)  # bullet lists
    return text


def _convert_md_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            block = []
            j = i
            while j < len(lines) and "|" in lines[j]:
                block.append(lines[j])
                j += 1
            rows = [[c.strip() for c in re.split(r"\|", r.strip().strip("|"))] for k, r in enumerate(block) if k != 1]
            if rows:
                ncol = max(len(r) for r in rows)
                rows = [r + [""] * (ncol - len(r)) for r in rows]
                widths = [max(len(r[c]) for r in rows) for c in range(ncol)]
                for r in rows:
                    out.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r)).rstrip())
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------
def to_readable(text: str, *, is_code: bool = False) -> str:
    """LaTeX + Markdown -> readable plain text / Unicode. Pure and deterministic."""
    if not text:
        return text
    out = _convert(text)
    out = _strip_markdown(out, is_code=is_code)
    out = re.sub(r"⟨\s+", "⟨", out)                     # tidy angle brackets: ⟨ 3, 4 ⟩ -> ⟨3, 4⟩
    out = re.sub(r"\s+⟩", "⟩", out)
    out = re.sub(r"[ \t]+\n", "\n", out)                # trailing spaces
    out = re.sub(r"\n{3,}", "\n\n", out)                # collapse blank runs (matrix alignment kept)
    return out.strip()


_SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"


def _signed_numbers(text: str) -> list[str]:
    # A super/subscript digit RUN is its own number, and a superscript run is separate from an adjacent
    # subscript run (∫₀¹ = lower 0 AND upper 1, not "01"; 10²³ = base 10 AND exponent 23, not "1023").
    for chars in (_SUP_DIGITS, _SUB_DIGITS):
        text = re.sub(f"[{chars}]+",
                      lambda m: " " + "".join(_SUPSUB_DIGIT_TO_ASCII[c] for c in m.group()), text)
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return sorted(nums, key=lambda s: (float(s), s))


def _inequalities(text: str) -> list[str]:
    """The DIRECTIONAL relation multiset (≤ ≥ ≠ < >), with LaTeX/ASCII forms normalized. ``=`` is
    excluded on purpose: it is ubiquitous (subscripts like i=1, matrix rows) and its direction cannot
    flip, so counting it only creates false alarms."""
    text = re.sub(r"\\leq?\b", "≤", text)
    text = re.sub(r"\\geq?\b", "≥", text)
    text = re.sub(r"\\neq?\b", "≠", text)
    text = text.replace("<=", "≤").replace(">=", "≥").replace("!=", "≠")
    return sorted(re.findall(r"[≤≥≠<>]", text))


def assert_expression_equivalent(canonical: str, rendered: str) -> None:
    """Raise ExpressionEquivalenceError if the render changed the signed-number multiset or a
    directional inequality. Conservative by design — a false alarm only costs a fallback to the
    canonical form, a missed change could ship a wrong answer. (Symbol substitution is a static 1:1
    table, so a relation can never be *flipped* at render time — the unit tests pin the table.)"""
    if _signed_numbers(canonical) != _signed_numbers(rendered):
        raise ExpressionEquivalenceError("numeric multiset changed during render")
    if _inequalities(canonical) != _inequalities(rendered):
        raise ExpressionEquivalenceError("inequality direction changed during render")


# Raw tokens that must NEVER reach a plain-text channel.
_FORBIDDEN = (
    (r"\\begin\s*\{", "\\begin{"), (r"\\end\s*\{", "\\end{"), (r"\\frac\b", "\\frac"),
    (r"\\sqrt\b", "\\sqrt"), (r"\\[a-zA-Z]+\b", "\\<cmd>"),  # any surviving control word
    (r"\\\[", "\\["), (r"\\\]", "\\]"), (r"\\\(", "\\("), (r"\\\)", "\\)"),
    (r"\*\*", "**"), (r"^\s{0,3}#{1,6}\s", "# heading"),
    (r"^\s*\|.*\|\s*$", "| table |"),
)


def forbidden_tokens(text: str, *, is_code: bool = False) -> list[str]:
    """Which unsupported presentation tokens survive in ``text`` (empty => channel-safe). ``$`` and
    lone backslashes inside explicit code are allowed."""
    found = []
    for pat, label in _FORBIDDEN:
        if is_code and label.startswith("\\"):
            continue
        if re.search(pat, text, flags=re.MULTILINE):
            found.append(label)
    return found


def assert_channel_safe(text: str, *, channel: str, is_code: bool = False) -> None:
    if channel in PLAIN_TEXT_CHANNELS:
        bad = forbidden_tokens(text, is_code=is_code)
        if bad:
            raise UnsupportedPresentationToken(f"{len(bad)} unsupported token type(s) for {channel}")


def _last_resort_strip(text: str) -> str:
    """If a rendered form still isn't channel-safe, remove the raw delimiters/markers WITHOUT
    restructuring, so at worst the student sees the plain math text, never TeX scaffolding."""
    t = text.replace("\\[", "\n").replace("\\]", "\n").replace("\\(", "").replace("\\)", "")
    t = t.replace("\\begin", "").replace("\\end", "")
    t = re.sub(r"\\left|\\right", "", t)
    t = re.sub(r"\*\*", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"\\([A-Za-z]+)", r"\1", t)   # \theta -> theta (readable word), never leaves a backslash
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def render_for_channel(text: str, *, channel: str, is_code: bool = False) -> str:
    """Render ``text`` for ``channel``. For plain-text channels: LaTeX/Markdown -> readable, guarded by
    fact-equivalence; if the render can't be proven equivalent OR isn't channel-safe, fall back to the
    canonical stripped form (facts intact, no scaffolding). Non-plain channels pass through unchanged."""
    if channel not in PLAIN_TEXT_CHANNELS or not text:
        return text
    rendered = to_readable(text, is_code=is_code)
    try:
        assert_expression_equivalent(text, rendered)
    except ExpressionEquivalenceError:
        rendered = _last_resort_strip(text)
    if forbidden_tokens(rendered, is_code=is_code):
        rendered = _last_resort_strip(rendered)
    return rendered


# Technical lines the voice pass must NOT lowercase (a leading `T`/`KE` is a variable name, not a
# sentence start). Used by conversation_style to protect variable case.
_TECH_LINE = re.compile(r"^\s*(?:[\[(|].*|.*[=<>≤≥≠→⇒].*|[A-Za-z][\w']*\s*[²³⁰-⁹]*\s*[=(].*)")


def is_technical_line(line: str) -> bool:
    """True for matrix rows, equations, and labelled expressions — lines whose leading capital is a
    symbol, not prose."""
    s = line.strip()
    if not s:
        return False
    if s[0] in "[(|⟨" or any(t in s for t in ("=", "<", ">", "≤", "≥", "≠", "→", "⇒")):
        return True
    return False
