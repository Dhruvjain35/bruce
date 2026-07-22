"""Channel-aware technical-content renderer (Bite 1.6) — PRESENTATION ONLY.

The conversation model writes ``user_visible_response`` as prose that can contain LaTeX and Markdown.
iMessage/SMS render neither, so raw ``\\begin{bmatrix}`` / ``\\frac`` / ``**bold**`` reach the student
as broken noise even when the reasoning is correct. This module turns that into readable plain text /
Unicode for plain-text channels — WITHOUT ever changing a value, sign, unit, variable, matrix entry,
exponent, relation direction, probability, conclusion, OR the association between a label and its
expression.

Structure, not just numbers. The message is parsed into ordered TechnicalBlocks (text | matrix), each
rendered independently and reassembled in source order, so a label ("T²") always stays with its own
matrix. The equivalence guard compares the ORDERED, per-block signature — block kind + order, matrix
dimensions, row-major entries, and the interleaved number/identifier tokens — so a transposed matrix,
a reordered block, or a dropped/swapped label is caught even when the numeric multiset is unchanged.
On any mismatch ``render_for_channel`` FALLS BACK to a delimiter-stripped canonical form.

  * to_readable()            — LaTeX/Markdown -> readable plain text/Unicode (generic, deterministic).
  * assert_expression_equivalent() — structural guard (raises on any structural change).
  * forbidden_tokens()/assert_channel_safe() — HARD gate: no raw TeX/Markdown for a plain channel.

Generic across every subject (arithmetic … linear algebra … physics … chemistry … finance). NO
subject-, variable-, or fixture-specific logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Channels that render plain text only (no Markdown, no LaTeX). iMessage/SMS today.
PLAIN_TEXT_CHANNELS = frozenset({"self_hosted_imessage", "imessage", "sms"})


class ExpressionEquivalenceError(Exception):
    """Rendering changed a value / sign / position / label / block order — content-free message."""


class UnsupportedPresentationToken(Exception):
    """A raw TeX/Markdown token survived for a plain-text channel — content-free message."""


# --------------------------------------------------------------------------------------------------
# Symbol tables. Each LaTeX control word maps 1:1 to Unicode: never inserted or dropped, only
# substituted, so no value can change and no relation can flip.
# --------------------------------------------------------------------------------------------------
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_OPS = {"times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "*", "star": "⋆",
        "bullet": "•", "oplus": "⊕", "otimes": "⊗"}
_RELATIONS = {"leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
              "equiv": "≡", "sim": "~", "cong": "≅", "propto": "∝", "to": "→", "rightarrow": "→",
              "Rightarrow": "⇒", "longrightarrow": "→", "leftarrow": "←", "Leftarrow": "⇐",
              "leftrightarrow": "↔", "mapsto": "↦", "implies": "⇒", "iff": "⇔"}
_BIG = {"sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "iint": "∬", "partial": "∂",
        "nabla": "∇", "infty": "∞", "sqrt": "√"}
_SETS = {"in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
         "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
         "neg": "¬", "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨"}
_MISC = {"angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "prime": "′", "cdots": "⋯",
         "ldots": "…", "dots": "…", "vdots": "⋮", "langle": "⟨", "rangle": "⟩", "hbar": "ℏ",
         "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "circ": "∘"}
_SYMBOLS: dict[str, str] = {**_GREEK, **_OPS, **_RELATIONS, **_BIG, **_SETS, **_MISC}

_SPACE_CMDS = ("quad", "qquad", "thinspace", "medspace", "thickspace", "space")
_DROP_CMDS = ("left", "right", "big", "Big", "bigg", "Bigg", "displaystyle", "textstyle", "limits",
              "mathrm", "mathbf", "mathit", "text", "operatorname", "boldsymbol")
_FUNCTIONS = ("lim", "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "arcsin",
              "arccos", "arctan", "log", "ln", "exp", "max", "min", "det", "gcd", "lcm", "mod",
              "arg", "deg", "dim", "ker", "sgn")
_ACCENTS = {"bar": "̄", "overline": "̄", "hat": "̂", "widehat": "̂",
            "vec": "⃗", "tilde": "̃", "dot": "̇", "ddot": "̈"}

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
_SUP_DIGITS, _SUB_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹", "₀₁₂₃₄₅₆₇₈₉"
_SUPSUB_DIGIT_TO_ASCII = {c: str(d) for d, c in zip(range(10), _SUP_DIGITS)}
_SUPSUB_DIGIT_TO_ASCII.update({c: str(d) for d, c in zip(range(10), _SUB_DIGITS)})


def _map_script(body: str, table: dict[str, str]) -> str | None:
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


def _apply_accent(mark: str, body: str) -> str:
    body = body.strip()
    return (body[0] + mark + body[1:]) if body else body


# --------------------------------------------------------------------------------------------------
# Structural parse: ordered TechnicalBlocks (text | matrix). Each has a stable identity + source_order,
# so blocks render independently and reassemble in order (a label never migrates to another matrix).
# --------------------------------------------------------------------------------------------------
_ENV_RE = re.compile(
    r"\\begin\s*\{(bmatrix|pmatrix|vmatrix|Bmatrix|Vmatrix|smallmatrix|matrix|array|cases)\}"
    r"(.*?)\\end\s*\{\1\}", re.DOTALL)
_BRACKET = {"bmatrix": ("[", "]"), "matrix": ("", ""), "pmatrix": ("(", ")"), "vmatrix": ("|", "|"),
            "Bmatrix": ("{", "}"), "Vmatrix": ("‖", "‖"), "smallmatrix": ("[", "]"), "array": ("[", "]")}


@dataclass
class TechnicalBlock:
    """One render unit with a stable identity. kind='matrix' carries a row-major cell grid (canonical,
    pre-render); kind='text' carries prose/inline-math. plain_text_fallback is the safe rendering."""
    source_order: int
    kind: str                                       # "text" | "matrix"
    canonical: str
    grid: tuple[tuple[str, ...], ...] | None = None
    bracket: str = "bmatrix"
    is_cases: bool = False
    plain_text_fallback: str = ""


def parse_blocks(text: str) -> list[TechnicalBlock]:
    """Split into ordered text/matrix blocks. Matrices (bmatrix/pmatrix/…/array/cases) become grid
    blocks; everything else is text. Order is preserved so each label stays adjacent to its block."""
    out: list[TechnicalBlock] = []
    pos = order = 0
    for m in _ENV_RE.finditer(text):
        if m.start() > pos:
            out.append(TechnicalBlock(order, "text", text[pos:m.start()])); order += 1
        env, body = m.group(1), m.group(2)
        if env == "array":
            body = re.sub(r"\A\s*\{[^{}]*\}", "", body)     # drop the column spec {ccc}
        rows = tuple(tuple(c.strip() for c in re.split(r"&", r))
                     for r in re.split(r"\\\\", body) if r.strip() != "")
        out.append(TechnicalBlock(order, "matrix", m.group(0), grid=rows,
                                  bracket=env, is_cases=(env == "cases"))); order += 1
        pos = m.end()
    if pos < len(text) or not out:
        out.append(TechnicalBlock(order, "text", text[pos:]))
    return out


# --------------------------------------------------------------------------------------------------
# Rendering (per block)
# --------------------------------------------------------------------------------------------------
def _convert_textish(text: str) -> str:
    """LaTeX -> plain/Unicode for a NON-matrix fragment (matrices are handled as blocks)."""
    frac_re = re.compile(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
    sqrt_n_re = re.compile(r"\\sqrt\s*\[([^\][]*)\]\s*\{([^{}]*)\}")
    sqrt_re = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
    for _ in range(40):                                  # innermost-first; bounded
        new = frac_re.sub(lambda m: _frac(m.group(1), m.group(2)), text)
        new = sqrt_n_re.sub(lambda m: f"{_superscript(m.group(1))}√"
                            + (f"({m.group(2).strip()})" if _needs_parens(m.group(2)) else m.group(2).strip()), new)
        new = sqrt_re.sub(lambda m: "√" + (f"({m.group(1).strip()})" if _needs_parens(m.group(1)) else m.group(1).strip()), new)
        if new == text:
            break
        text = new
    for name, mark in _ACCENTS.items():                  # \bar{x} -> x̄
        text = re.sub(rf"\\{name}\s*\{{([^{{}}]*)\}}", lambda m, mk=mark: _apply_accent(mk, m.group(1)), text)
        text = re.sub(rf"\\{name}\s+([A-Za-z0-9])", lambda m, mk=mark: _apply_accent(mk, m.group(1)), text)
    text = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _superscript(m.group(1)), text)
    text = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _subscript(m.group(1)), text)
    text = re.sub(r"\^\s*(\\?[A-Za-z0-9])", lambda m: _superscript(m.group(1).lstrip("\\")), text)
    text = re.sub(r"_\s*(\\?[A-Za-z0-9])", lambda m: _subscript(m.group(1).lstrip("\\")), text)
    for c in _DROP_CMDS:
        text = re.sub(rf"\\{c}\b", "", text)
    for c in _SPACE_CMDS:
        text = re.sub(rf"\\{c}\b", " ", text)
    text = re.sub(r"\\[,;:> ]", " ", text)
    text = text.replace("\\!", "")
    text = re.sub(r"\\(" + "|".join(_FUNCTIONS) + r")\b", r"\1", text)
    text = re.sub(r"\\([A-Za-z]+)", lambda m: _SYMBOLS.get(m.group(1), m.group(0)), text)
    text = text.replace("\\[", "\n").replace("\\]", "\n").replace("\\(", "").replace("\\)", "")
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\([{}&%#_])", r"\1", text)
    return text


def _render_grid(grid: list[list[str]], bracket: str) -> str:
    if not grid:
        return ""
    ncol = max(len(r) for r in grid)
    grid = [list(r) + [""] * (ncol - len(r)) for r in grid]
    widths = [max(len(r[c]) for r in grid) for c in range(ncol)]
    lb, rb = _BRACKET.get(bracket, ("[", "]"))
    lines = [f"{lb} " + "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r)) + f" {rb}".rstrip()
             for r in grid]
    return "\n" + "\n".join(line if lb else line.strip() for line in lines)


def _render_cases(grid: list[list[str]]) -> str:
    return "\n" + "\n".join("  " + ",  ".join(c.strip() for c in row if c.strip()) for row in grid)


def _render_text_block(t: str, *, is_code: bool = False) -> str:
    return _strip_markdown(_convert_textish(t), is_code=is_code)


def _strip_markdown(text: str, *, is_code: bool) -> str:
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: m.group(1), text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = _convert_md_tables(text)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "• ", text, flags=re.MULTILINE)
    return text


def _convert_md_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            block, j = [], i
            while j < len(lines) and "|" in lines[j]:
                block.append(lines[j]); j += 1
            rows = [[c.strip() for c in re.split(r"\|", r.strip().strip("|"))] for k, r in enumerate(block) if k != 1]
            if rows:
                ncol = max(len(r) for r in rows)
                rows = [r + [""] * (ncol - len(r)) for r in rows]
                widths = [max(len(r[c]) for r in rows) for c in range(ncol)]
                for r in rows:
                    out.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r)).rstrip())
            i = j
            continue
        out.append(lines[i]); i += 1
    return "\n".join(out)


def to_readable(text: str, *, is_code: bool = False) -> str:
    """LaTeX + Markdown -> readable plain text/Unicode, block by block, reassembled in source order."""
    if not text:
        return text
    parts: list[str] = []
    for b in parse_blocks(text):
        if b.kind == "text":
            parts.append(_render_text_block(b.canonical, is_code=is_code))
        elif b.is_cases:
            parts.append(_render_cases([[_convert_textish(c) for c in row] for row in b.grid]))
        else:
            parts.append(_render_grid([[_convert_textish(c).strip() for c in row] for row in b.grid], b.bracket))
    out = "".join(parts)
    out = re.sub(r"⟨\s+", "⟨", out)
    out = re.sub(r"\s+⟩", "⟩", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --------------------------------------------------------------------------------------------------
# Structural equivalence guard
# --------------------------------------------------------------------------------------------------
def _tokens(s: str) -> list[str]:
    """Ordered (NOT sorted) tokens: numbers (super/subscript digit runs split by script) and
    identifier runs (Latin/Greek letters). Order encodes position, so a transposed matrix, a
    reordered block, or a moved label produces a different token list."""
    for chars in (_SUP_DIGITS, _SUB_DIGITS):
        s = re.sub(f"[{chars}]+", lambda m: " " + "".join(_SUPSUB_DIGIT_TO_ASCII[c] for c in m.group()), s)
    return re.findall(r"-?\d+(?:\.\d+)?|[A-Za-zµΑ-Ωα-ω]+", s.replace(",", ""))


_ROW_RE = re.compile(r"^\s*[\[(|{‖].*[\])|}‖]\s*$")


def _rendered_blocks(plain: str) -> list[tuple[str, object]]:
    """Parse ALREADY-RENDERED plain text back into ordered text/matrix blocks (a matrix = a run of
    consecutive bracketed rows), so the guard can compare structure against the canonical."""
    out: list[tuple[str, object]] = []
    txt: list[str] = []
    mat: list[list[str]] = []

    def flush_txt() -> None:
        if txt:
            out.append(("text", "\n".join(txt))); txt.clear()

    def flush_mat() -> None:
        if mat:
            out.append(("matrix", tuple(tuple(r) for r in mat))); mat.clear()

    for ln in plain.split("\n"):
        if _ROW_RE.match(ln):
            flush_txt()
            inner = re.sub(r"^\s*[\[(|{‖]\s*", "", ln.strip())
            inner = re.sub(r"\s*[\])|}‖]\s*$", "", inner)
            mat.append([c for c in re.split(r"\s{2,}", inner.strip()) if c != ""] or [""])
        else:
            flush_mat()
            txt.append(ln)
    flush_mat()
    flush_txt()
    return out


def _canonical_signature(text: str) -> list[str]:
    sig: list[str] = []
    for b in parse_blocks(text):
        if b.kind == "text":
            sig += _tokens(_render_text_block(b.canonical))
        elif b.is_cases:
            for row in b.grid:
                for c in row:
                    sig += _tokens(_convert_textish(c))
        else:
            conv = [[_convert_textish(c).strip() for c in row] for row in b.grid]
            ncol = max((len(r) for r in conv), default=0)
            sig.append(f"MAT{len(conv)}x{ncol}")
            for row in conv:
                for c in row:
                    sig += _tokens(c)
    return sig


def _rendered_signature(text: str) -> list[str]:
    sig: list[str] = []
    for kind, payload in _rendered_blocks(text):
        if kind == "text":
            sig += _tokens(payload)                       # type: ignore[arg-type]
        else:
            grid = payload                                # tuple of tuples
            ncol = max((len(r) for r in grid), default=0)  # type: ignore[arg-type]
            sig.append(f"MAT{len(grid)}x{ncol}")           # type: ignore[arg-type]
            for row in grid:                               # type: ignore[union-attr]
                for c in row:
                    sig += _tokens(c)
    return sig


def _inequalities(text: str) -> list[str]:
    """Directional relation multiset (≤ ≥ ≠ < >), LaTeX/ASCII normalized. ``=`` is excluded (ubiquitous
    and undirected)."""
    text = re.sub(r"\\leq?\b", "≤", text)
    text = re.sub(r"\\geq?\b", "≥", text)
    text = re.sub(r"\\neq?\b", "≠", text)
    text = text.replace("<=", "≤").replace(">=", "≥").replace("!=", "≠")
    return sorted(re.findall(r"[≤≥≠<>]", text))


def assert_expression_equivalent(canonical: str, rendered: str) -> None:
    """Raise ExpressionEquivalenceError unless the render preserved STRUCTURE: block kind + order,
    matrix dimensions, row-major entry positions, the interleaved number/identifier (label) tokens, and
    directional inequalities. Conservative — a false alarm only costs a fallback to canonical text."""
    if _canonical_signature(canonical) != _rendered_signature(rendered):
        raise ExpressionEquivalenceError("structural signature changed during render")
    if _inequalities(canonical) != _inequalities(rendered):
        raise ExpressionEquivalenceError("inequality direction changed during render")


# --------------------------------------------------------------------------------------------------
# Hard outbound gate
# --------------------------------------------------------------------------------------------------
_FORBIDDEN = (
    (r"\\begin\s*\{", "\\begin{"), (r"\\end\s*\{", "\\end{"), (r"\\frac\b", "\\frac"),
    (r"\\sqrt\b", "\\sqrt"), (r"\\[a-zA-Z]+\b", "\\<cmd>"),
    (r"\\\[", "\\["), (r"\\\]", "\\]"), (r"\\\(", "\\("), (r"\\\)", "\\)"),
    (r"\*\*", "**"), (r"^\s{0,3}#{1,6}\s", "# heading"), (r"^\s*\|.*\|\s*$", "| table |"),
)


def forbidden_tokens(text: str, *, is_code: bool = False) -> list[str]:
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
    """Remove raw delimiters/markers WITHOUT restructuring, guaranteeing no TeX scaffolding or stray
    backslash reaches the channel (facts survive; at worst the student sees plain math text)."""
    t = text.replace("\\[", "\n").replace("\\]", "\n").replace("\\(", "").replace("\\)", "")
    t = re.sub(r"\\begin\s*\{[^{}]*\}", "", t)
    t = re.sub(r"\\end\s*\{[^{}]*\}", "", t)
    t = t.replace("\\\\", "\n").replace("&", "  ")
    t = re.sub(r"\*\*", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"\\([A-Za-z]+)", r"\1", t)                # \theta -> theta (readable word)
    t = t.replace("\\", "")                               # nuke any stray backslash
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def render_for_channel(text: str, *, channel: str, is_code: bool = False) -> str:
    """Render ``text`` for ``channel``. Plain-text channels: LaTeX/Markdown -> readable, guarded by the
    structural equivalence check; if it can't be proven structure-preserving OR isn't channel-safe,
    fall back to the canonical stripped form. Non-plain channels pass through unchanged."""
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


# Technical lines the voice pass must NOT lowercase (a leading `T`/`KE` is a variable, not a sentence).
def is_technical_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s[0] in "[(|⟨‖{" or any(t in s for t in ("=", "<", ">", "≤", "≥", "≠", "→", "⇒")):
        return True
    return False
